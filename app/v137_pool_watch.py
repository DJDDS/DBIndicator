"""V13.7 Daily pool, watched all day (part of the 09:25 direction desk on /admin/intraday; paper only, never places orders).

The intraday desk keeps ONE short list instead of separate 30/60/90/120-minute lists, and follows it until 15:15:
  09:26  the 09:25 daily pool (3 stocks; status OPEN = paper-traded, WATCH = monitored only)
  09:45  + the hierarchy desk's PICK (only a side that is ACTIVE there; a SHADOW pick never joins)
  11:15 / 12:15 / 13:15  + relative-strength grade-A stocks (|z| >= 3.5, not the quiet-day reserve), strongest first
  at most max_items (default 5); a stock already on the list is not added again (an opposite call is noted, not added).
Each stock is tracked from its own entry on the order-flow polls the desk already records (no extra Kite calls):
live %, best / worst, +1% booking (first touch), and for pool stocks the researched hold + stop + trail (stop 1x 20-day range,
trail 0.75x; 2017-26: +35 bps/pick vs +8 for booking at +1%). Everything closes at 15:15. Every entry and result is written to
pool_watch_days.jsonl from day one, so the list's own win rate can be judged.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time

import numpy as np

from . import v133_direction as d
from .v134_relstrength import _px

log = logging.getLogger(__name__)
FILE = 'pool_watch_days.jsonl'
DEFAULT_SETTINGS = {'enabled': True, 'max_items': 5, 'add_hierarchy': True, 'add_rs': True, 'exit_at': '15:15', 'book_pct': 1.0}
_lock = threading.Lock()
_last = {'t': 0.0}


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update({k: v for k, v in (d._read_json('pool_watch_settings.json', {}) or {}).items() if k in s})
        s['max_items'] = int(min(8, max(3, int(s['max_items'])))); s['book_pct'] = float(min(5.0, max(0.2, float(s['book_pct']))))
        for k in ('enabled', 'add_hierarchy', 'add_rs'): s[k] = bool(s[k])
    except Exception:  # noqa: BLE001
        log.exception('v137: bad settings; using defaults'); return dict(DEFAULT_SETTINGS)
    return s


# ================================================================== candidates (pure)
def candidates(day925: dict | None, h_rec: dict | None, rs_rec: dict | None, s: dict) -> list:
    """Every stock that may join today's list, in the order it became known (pool first)."""
    out = []
    L = ((day925 or {}).get('lists') or {}).get('hc')
    if L:
        st = (day925.get('settings') or {})
        for p in L.get('picks') or []:
            rng = float(p.get('rng20_pct') or 0)
            ec = (p.get('entry_check') or {})
            out.append({'symbol': p['symbol'], 'side': p['side'], 'entry': p['entry'], 'added': str(day925.get('at'))[:19].replace(' ', 'T'),
                        'source': '09:25 pool', 'pod': bool(p.get('pod')), 'grade': p.get('grade'),
                        # traded = the desk's OPEN picks, minus a WEAK entry check (research 2019-26: extreme entries earned ~0; score >= 3 +13.7 bps vs +5.4 all)
                        'trade': p.get('status') == 'OPEN' and ec.get('label') != 'WEAK',
                        'why': f"conf {p.get('conf')}" + (f" · entry {ec['label']} {ec['score']}/4" if ec.get('score') is not None else '')
                               + ('' if p.get('of_agree') is None else (' · order flow agrees' if p['of_agree'] else ' · order flow against')),
                        'stop_pct': round(rng * float(st.get('stop_k', 0) or 0), 3) or None, 'trail_pct': round(rng * float(st.get('trail_k', 0) or 0), 3) or None})
    if s['add_hierarchy'] and h_rec:
        for c in h_rec.get('picks') or []:
            if c.get('role') != 'PICK': continue
            out.append({'symbol': c['symbol'], 'side': c['side'], 'entry': c['entry'], 'added': c['entry_poll'][:19], 'source': '09:45 hierarchy', 'trade': True,
                        'why': f"leads {c.get('industry')} by {c.get('resid')}% · eff {c.get('eff')} · vol× {c.get('volrel')}"})
    if s['add_rs'] and rs_rec:
        for hm in sorted((rs_rec.get('slots') or {})):
            sl = rs_rec['slots'][hm]
            for p in sorted(sl.get('picks') or [], key=lambda x: -abs(x.get('z') or 0)):
                if p.get('grade') != 'A' or p.get('reserve'): continue
                out.append({'symbol': p['symbol'], 'side': p['side'], 'entry': p['entry'], 'added': f"{rs_rec['date']}T{hm}:00", 'source': f'{hm} strength', 'trade': True,
                            'why': f"z {p.get('z')} · {p.get('vs_mkt_pct', 0):+.2f}% vs market" + ('' if p.get('of_agree') is None else (' · OF+' if p['of_agree'] else ' · OF-'))})
    return out


def merge(items: list, cands: list, max_items: int) -> tuple[list, list]:
    """Add new candidates in order until max_items. Returns (items, notes). Existing items never change or leave."""
    have = {i['symbol']: i for i in items}; notes = []
    for c in cands:
        if c['symbol'] in have:
            if have[c['symbol']]['side'] != c['side'] and have[c['symbol']]['source'] != c['source']:
                notes.append({'symbol': c['symbol'], 'source': c['source'], 'note': f"{c['source']} says {c['side']}, list holds {have[c['symbol']]['side']} - not added"})
            continue
        if len(items) >= max_items:
            notes.append({'symbol': c['symbol'], 'source': c['source'], 'note': f'list full ({max_items}) - not added'}); continue
        it = dict(c); items.append(it); have[c['symbol']] = it
    return items, notes


# ================================================================== tracking (pure)
def _hm_shift(hm: str, minutes: int) -> str:
    m = int(hm[:2]) * 60 + int(hm[3:5]) + minutes
    return f'{m // 60:02d}:{m % 60:02d}'


def _exit_poll_t(polls: list, exit_hm: str, day: str) -> str | None:
    """Time of the poll that closes the day: the one nearest exit_hm, not earlier than 3 minutes before it (a 15:10 poll never closes early)."""
    lo, hi = _hm_shift(exit_hm, -3), _hm_shift(exit_hm, 8); best = None
    for p in polls:
        t = str(p.get('t', ''))[:19]
        if t[:10] != day or not (lo <= t[11:16] <= hi): continue
        g = abs((int(t[11:13]) * 60 + int(t[14:16])) - (int(exit_hm[:2]) * 60 + int(exit_hm[3:5])))
        if best is None or g < best[0]: best = (g, t)
    return best[1] if best else None


def track(item: dict, polls: list, exit_hm: str, book_pct: float, cost_pct: float = d.COST * 100) -> dict:
    """Follow one stock from its entry on the polls: live / best / worst, +book_pct booking, stop + trail, and the exit-time result."""
    sg = 1 if item['side'] == 'UP' else -1; e = float(item['entry'])
    t0 = item['added']; tx = _exit_poll_t(polls, exit_hm, t0[:10])
    path = []
    for p in polls:
        t = str(p.get('t', ''))[:19]
        if t < t0 or (tx and t > tx): continue
        px = _px((p.get('q') or {}).get(item['symbol']), 0)
        if px: path.append((t, px, sg * (px / e - 1) * 100))
    out = {'n_obs': len(path)}
    if not path: return out
    r = [x[2] for x in path]
    out.update(now_pct=round(r[-1], 3), now_px=path[-1][1], at=path[-1][0][11:16], best_pct=round(max(0.0, max(r)), 3), worst_pct=round(min(0.0, min(r)), 3))
    hit = next((x for x in path if x[2] >= book_pct), None)
    out['booked'] = hit is not None
    if hit: out['booked_at'] = hit[0][11:16]
    if item.get('stop_pct'):
        S = item['stop_pct'] / 100; Tr = (item.get('trail_pct') or 0) / 100
        stop = e * (1 - S) if sg > 0 else e * (1 + S); best = e; stopped = None
        for t, px, _ in path:
            if (px <= stop) if sg > 0 else (px >= stop): stopped = (t, stop); break
            if Tr > 0:
                if sg > 0: best = max(best, px); stop = max(stop, best * (1 - Tr))
                else: best = min(best, px); stop = min(stop, best * (1 + Tr))
        out['stop_px'] = round(stop, 2); out['stopped'] = stopped is not None
        if stopped: out['stopped_at'] = stopped[0][11:16]; out['stop_ret_pct'] = round(sg * (stopped[1] / e - 1) * 100 - cost_pct, 3)
    if tx and path[-1][0][11:16] >= _hm_shift(exit_hm, -8):
        out['closed'] = True; out['exit_px'] = path[-1][1]
        out['ret_pct'] = round(r[-1] - cost_pct, 3); out['win'] = out['ret_pct'] > 0
        out['book_ret_pct'] = round(book_pct - cost_pct, 3) if hit else out['ret_pct']
        if item.get('stop_pct') and not out.get('stopped'): out['stop_ret_pct'] = out['ret_pct']
    return out


# ================================================================== live loop
def tick(now: dt.datetime | None = None, force: bool = False) -> None:
    """Called after each order-flow poll (at most once a minute): add due stocks, refresh tracking, close at the exit time."""
    s = load_settings()
    if not s['enabled']: return
    now = now or d.now_ist(); hm = now.strftime('%H:%M'); today = str(now.date())
    if now.weekday() >= 5 or not ('09:26' <= hm <= '15:30'): return
    if not force and time.time() - _last['t'] < 60: return
    _last['t'] = time.time()
    with _lock:
        days = d._read_jsonl(FILE); rec = next((x for x in days if x.get('date') == today), None)
        if rec and rec.get('done'): return
        day925 = next((x for x in reversed(d._read_jsonl('days.jsonl')[-5:]) if x.get('date') == today), None)
        h_rec = next((x for x in reversed(d._read_jsonl('v47_days.jsonl')[-5:]) if x.get('date') == today), None)
        rs_rec = next((x for x in reversed(d._read_jsonl('rs_days.jsonl')[-5:]) if x.get('date') == today), None)
        if rec is None:
            if not day925: return
            rec = {'date': today, 'items': [], 'notes': [], 'settings': s}; days.append(rec)
        if hm < s['exit_at']:                                         # nothing joins after the exit time
            rec['items'], notes = merge(rec['items'], candidates(day925, h_rec, rs_rec, s), s['max_items'])
            seen = {(n['symbol'], n['source']) for n in rec['notes']}
            rec['notes'] += [n for n in notes if (n['symbol'], n['source']) not in seen]
        polls = d._read_of(today)
        if polls:
            for it in rec['items']: it['track'] = track(it, polls, s['exit_at'], s['book_pct'])
        if hm >= s['exit_at'] and rec['items'] and all((it.get('track') or {}).get('closed') for it in rec['items']): rec['done'] = True
        rec['updated'] = str(now)[:19]
        d._rewrite_jsonl(FILE, days)


def _sc(rows: list, key: str) -> dict:
    r = [(i.get('track') or {}).get(key) for i in rows]; r = [x for x in r if isinstance(x, (int, float))]
    if not r: return {'n': 0}
    return {'n': len(r), 'win': round(100 * sum(x > 0 for x in r) / len(r), 1), 'avg_bps': round(float(np.mean(r)) * 100, 1)}


def scorecard(days: list) -> dict:
    done = [i for x in days for i in x.get('items', []) if (i.get('track') or {}).get('closed')]
    traded = [i for i in done if i.get('trade')]
    by_src = {}
    for i in traded: by_src.setdefault(i['source'].split(' ', 1)[1] if ' ' in i['source'] else i['source'], []).append(i)
    per_day = [[i for i in x.get('items', []) if i.get('trade') and (i.get('track') or {}).get('closed')] for x in days]
    per_day = [p for p in per_day if p]
    return {'days': len(per_day), 'hold': _sc(traded, 'ret_pct'), 'book': _sc(traded, 'book_ret_pct'),
            'stop': _sc([i for i in traded if i.get('stop_pct')], 'stop_ret_pct'), 'watch': _sc([i for i in done if not i.get('trade')], 'ret_pct'),
            'by_source': {k: _sc(v, 'ret_pct') for k, v in by_src.items()},
            'ge1_day': round(100 * sum(any(i['track'].get('win') for i in p) for p in per_day) / len(per_day), 1) if per_day else None}


def _grp(ps: list) -> dict:
    """win % and average (bps, after cost) for hold to 15:15, book +1% and hold + stop on settled daily-pool picks."""
    out = {'n': 0}
    for k, key in (('hold', 'ret_pct'), ('book', 'book_ret_pct'), ('stop', 'stop_ret_pct')):
        r = [p[key] for p in ps if isinstance(p.get(key), (int, float))]
        if r: out[k] = {'win': round(100 * sum(x > 0 for x in r) / len(r), 1), 'avg_bps': round(float(np.mean(r)) * 100, 1)}; out['n'] = max(out['n'], len(r))
    return out


def pool_breakdown(days925: list) -> list:
    """Which filters lift the daily pool's win rate on the LIVE ledger (direction desk days.jsonl, settled picks only)."""
    rows = []
    for x in days925:
        L = (x.get('lists') or {}).get('hc')
        if not L: continue
        for p in L.get('picks') or []:
            if p.get('status') in (None,) or not isinstance(p.get('ret_pct'), (int, float)): continue
            rows.append({**p, '_strong': bool(L.get('strong'))})
    ec = lambda p: (p.get('entry_check') or {}).get('label')  # noqa: E731
    groups = [('all pool picks', lambda p: True), ('pick of the day', lambda p: p.get('pod')),
              ('grade A', lambda p: p.get('grade') == 'A'), ('grade B', lambda p: p.get('grade') == 'B'),
              ('entry GOOD', lambda p: ec(p) == 'GOOD'), ('entry BORDERLINE', lambda p: ec(p) == 'BORDERLINE'), ('entry WEAK', lambda p: ec(p) == 'WEAK'),
              ('not WEAK (new rule)', lambda p: ec(p) != 'WEAK'),
              ('order flow agrees', lambda p: p.get('of_agree') is True), ('order flow against', lambda p: p.get('of_agree') is False),
              ('★ strong day', lambda p: p['_strong']), ('normal day', lambda p: not p['_strong']),
              ('grade A + order flow agrees', lambda p: p.get('grade') == 'A' and p.get('of_agree') is True)]
    return [{'group': g, **_grp([p for p in rows if f(p)])} for g, f in groups]


def status() -> dict:
    days = d._read_jsonl(FILE); today = str(d.now_ist().date())
    return {'settings': load_settings(), 'today': next((x for x in days if x.get('date') == today), None), 'score': scorecard(days),
            'breakdown': pool_breakdown(d._read_jsonl('days.jsonl'))}
