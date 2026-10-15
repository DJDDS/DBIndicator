"""V13.7 Daily pool, watched all day (part of the 09:25 direction desk on /admin/intraday; paper only, never places orders).

The intraday desk keeps ONE short list instead of separate 30/60/90/120-minute lists, and follows it until 15:15:
  09:26  the 09:25 daily pool (3 stocks; status OPEN = paper-traded, WATCH = monitored only)
  09:45  + the hierarchy desk's PICK (only a side that is ACTIVE there; a SHADOW pick never joins)
  11:15 / 12:15 / 13:15  + relative-strength grade-A stocks (|z| >= 3.5, not the quiet-day reserve), strongest first
  at most max_items LIVE stocks (default 5). A stock that hits its trailing stop frees its slot; a strong signal that found the list
  full may then join at the CURRENT price if it is at most late_join_min old (max_day_items a day). A stock never re-enters the same day.
Each stock is tracked from its own entry on the price polls the desk already records (incremental reader, no extra Kite calls):
  live %, best / worst, +1% booking (first touch), hold + stop + trail sized by the stock's own 20-day range (stop 1x, trail 0.75x;
  research 2017-26 on DOWN picks: +35 bps/pick vs +8 for booking at +1%), and a MARKET GUARD shadow exit: leave when the F&O market
  (median stock) has moved guard_pct against the call since entry (short-detector research: shorts lost -90 bps on days the market
  rose over 0.5%). The guard is measured, not enforced, so the live ledger shows whether it lifts the win rate before it is used.
Everything closes at 15:15. Every entry and result is written to pool_watch_days.jsonl from day one.
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
DEFAULT_SETTINGS = {'enabled': True, 'max_items': 5, 'max_day_items': 8, 'late_join_min': 60, 'add_hierarchy': True, 'add_rs': True,
                    'exit_at': '15:15', 'up_exit_at': '15:15', 'book_pct': 1.0, 'guard_pct': 0.5, 'stop_k': 1.0, 'trail_k': 0.75}
_lock = threading.Lock()
_last = {'day': None, 'n': -1, 't': 0.0}
_bd_cache = {'t': 0.0, 'v': []}


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update({k: v for k, v in (d._read_json('pool_watch_settings.json', {}) or {}).items() if k in s})
        s['max_items'] = int(min(8, max(3, int(s['max_items'])))); s['max_day_items'] = int(min(15, max(s['max_items'], int(s['max_day_items']))))
        s['late_join_min'] = int(min(240, max(0, int(s['late_join_min']))))
        for k, lo, hi in (('book_pct', 0.2, 5.0), ('guard_pct', 0.1, 3.0), ('stop_k', 0.0, 5.0), ('trail_k', 0.0, 5.0)):
            s[k] = float(min(hi, max(lo, float(s[k]))))
        for k in ('enabled', 'add_hierarchy', 'add_rs'): s[k] = bool(s[k])
        if s['up_exit_at'] not in ('09:55', '10:25', '10:55', '11:25', '15:15'): s['up_exit_at'] = '15:15'
    except Exception:  # noqa: BLE001
        log.exception('v137: bad settings; using defaults'); return dict(DEFAULT_SETTINGS)
    return s


def _mins(t: str) -> int:
    """'HH:MM' or an ISO time -> minutes since midnight."""
    hm = t[11:16] if len(t) > 5 else t
    return int(hm[:2]) * 60 + int(hm[3:5])


def _hm_shift(hm: str, minutes: int) -> str:
    m = _mins(hm) + minutes
    return f'{m // 60:02d}:{m % 60:02d}'


# ================================================================== candidates (pure)
def candidates(day925: dict | None, h_rec: dict | None, rs_rec: dict | None, s: dict, rng: dict | None = None) -> list:
    """Every stock that may join today's list, in the order it became known (pool first). rng: symbol -> 20-day average range (%)."""
    out, rng = [], rng or {}
    st = ((day925 or {}).get('settings') or {})
    sk, tk = float(st.get('stop_k', s['stop_k']) or 0), float(st.get('trail_k', s['trail_k']) or 0)

    def stops(r):
        r = float(r or 0)
        return {'stop_pct': round(r * sk, 3) or None, 'trail_pct': round(r * tk, 3) or None}
    L = ((day925 or {}).get('lists') or {}).get('hc')
    if L:
        for p in L.get('picks') or []:
            ec = (p.get('entry_check') or {})
            out.append({'symbol': p['symbol'], 'side': p['side'], 'entry': p['entry'], 'added': str(day925.get('at'))[:19].replace(' ', 'T'),
                        'source': '09:25 pool', 'pod': bool(p.get('pod')), 'grade': p.get('grade'),
                        # traded = the desk's OPEN picks, minus a WEAK entry check (research 2019-26: extreme entries earned ~0; score >= 3 +13.7 bps vs +5.4 all)
                        'trade': p.get('status') == 'OPEN' and ec.get('label') != 'WEAK',
                        'why': f"conf {p.get('conf')}" + (f" · entry {ec['label']} {ec['score']}/4" if ec.get('score') is not None else '')
                               + ('' if p.get('of_agree') is None else (' · order flow agrees' if p['of_agree'] else ' · order flow against')),
                        **stops(p.get('rng20_pct') or rng.get(p['symbol'])),
                        **({'exit_at': s['up_exit_at']} if p['side'] == 'UP' and s['up_exit_at'] != s['exit_at'] else {})})
    if s['add_hierarchy'] and h_rec:
        for c in h_rec.get('picks') or []:
            if c.get('role') != 'PICK': continue
            out.append({'symbol': c['symbol'], 'side': c['side'], 'entry': c['entry'], 'added': c['entry_poll'][:19], 'source': '09:45 hierarchy', 'trade': True,
                        'why': f"leads {c.get('industry')} by {c.get('resid')}% · eff {c.get('eff')} · vol× {c.get('volrel')}", **stops(rng.get(c['symbol']))})
    if s['add_rs'] and rs_rec:
        for hm in sorted((rs_rec.get('slots') or {})):
            sl = rs_rec['slots'][hm]
            for p in sorted(sl.get('picks') or [], key=lambda x: -abs(x.get('z') or 0)):
                if p.get('grade') != 'A' or p.get('reserve'): continue
                out.append({'symbol': p['symbol'], 'side': p['side'], 'entry': p['entry'], 'added': f"{rs_rec['date']}T{hm}:00", 'source': f'{hm} strength', 'trade': True,
                            'why': f"z {p.get('z')} · {p.get('vs_mkt_pct', 0):+.2f}% vs market" + ('' if p.get('of_agree') is None else (' · OF+' if p['of_agree'] else ' · OF-')),
                            **stops(rng.get(p['symbol']))})
    return out


def _live(items: list) -> int:
    return sum(1 for i in items if not (i.get('track') or {}).get('stopped'))


def merge(items: list, cands: list, s: dict, notes: list | None = None, now_t: str | None = None, prices: dict | None = None) -> tuple[list, list]:
    """Add candidates in order while fewer than max_items stocks are live (a stopped stock frees its slot) and the day cap allows.
    A candidate turned away earlier because the list was full may join later at the CURRENT price, while its signal is at most
    late_join_min old. Existing items never change or leave; a stock never re-enters the same day. Returns (items, new notes)."""
    notes = list(notes or []); new = []
    have = {i['symbol']: i for i in items}
    noted = {(n['symbol'], n['source'], n.get('kind')) for n in notes}

    def note(c, kind, text):
        if (c['symbol'], c['source'], kind) not in noted:
            n = {'symbol': c['symbol'], 'source': c['source'], 'kind': kind, 'note': text}; new.append(n); noted.add((c['symbol'], c['source'], kind))
    was_full = {(n['symbol'], n['source']) for n in notes + new if n.get('kind') == 'full'}
    for c in cands:
        if c['symbol'] in have:
            h = have[c['symbol']]
            if h['side'] != c['side'] and h['source'] != c['source']: note(c, 'conflict', f"{c['source']} says {c['side']}, list holds {h['side']} - not added")
            continue
        if _live(items) >= s['max_items'] or len(items) >= s['max_day_items']:
            note(c, 'full', f"list full ({s['max_items']} live) - not added"); was_full.add((c['symbol'], c['source'])); continue
        it = dict(c)
        if (c['symbol'], c['source']) in was_full:                   # slot freed later: join now, at today's current price
            age = (_mins(now_t) - _mins(c['added'])) if now_t else 10 ** 6
            px = (prices or {}).get(c['symbol'])
            if age > s['late_join_min'] or not px:
                note(c, 'stale', f"slot freed but the signal is {age} min old - not added" if px else 'slot freed but no live price - not added'); continue
            it.update(entry=px, added=now_t[:19], signal_at=c['added'][11:16], late=True)
            it['why'] = it['why'] + f" · joined {now_t[11:16]} when a slot freed (signal {c['added'][11:16]})"
        items.append(it); have[c['symbol']] = it
    return items, new


# ================================================================== tracking (pure)
def _exit_poll_t(polls: list, exit_hm: str, day: str) -> str | None:
    """Time of the poll that closes the day: the one nearest exit_hm, not earlier than 3 minutes before it (a 15:10 poll never closes early)."""
    lo, hi = _hm_shift(exit_hm, -3), _hm_shift(exit_hm, 8); best = None
    for p in polls:
        t = str(p.get('t', ''))[:19]
        if t[:10] != day or not (lo <= t[11:16] <= hi): continue
        g = abs(_mins(t) - _mins(exit_hm))
        if best is None or g < best[0]: best = (g, t)
    return best[1] if best else None


def market_path(polls: list, t0: str, tx: str | None) -> dict:
    """poll time -> median move (%) of all stocks since the first poll at/after t0 (the F&O market as the desks define it)."""
    base, out = None, {}
    for p in polls:
        t = str(p.get('t', ''))[:19]
        if t < t0 or (tx and t > tx): continue
        q = p.get('q') or {}
        if base is None: base = {k: _px(v, 0) for k, v in q.items()}; base = {k: v for k, v in base.items() if v}
        mv = [(_px(v, 0) / base[k] - 1) * 100 for k, v in q.items() if k in base and _px(v, 0)]
        if len(mv) >= 30: out[t] = float(np.median(mv))
    return out


def track(item: dict, polls: list, exit_hm: str, book_pct: float, guard_pct: float = 0.5, cost_pct: float = d.COST * 100,
          mkt: dict | None = None) -> dict:
    """Follow one stock from its entry: live / best / worst, +book_pct booking, stop + trail, market guard, and the exit-time result."""
    sg = 1 if item['side'] == 'UP' else -1; e = float(item['entry'])
    t0 = item['added']; tx = _exit_poll_t(polls, exit_hm, t0[:10])
    mkt = mkt if mkt is not None else market_path(polls, t0, tx)
    path = []
    for p in polls:
        t = str(p.get('t', ''))[:19]
        if t < t0 or (tx and t > tx): continue
        px = _px((p.get('q') or {}).get(item['symbol']), 0)
        if px: path.append((t, px, sg * (px / e - 1) * 100))
    out = {'n_obs': len(path)}
    if not path: return out
    r = [x[2] for x in path]
    out.update(now_pct=round(r[-1], 3) + 0.0, now_px=path[-1][1], at=path[-1][0][11:16], best_pct=round(max(0.0, max(r)), 3), worst_pct=round(min(0.0, min(r)), 3))
    if path[-1][0] in mkt: out['mkt_pct'] = round(mkt[path[-1][0]], 3)
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
    g = next((x for x in path if x[0] in mkt and sg * mkt[x[0]] <= -guard_pct), None)
    out['guarded'] = g is not None
    if g: out['guarded_at'] = g[0][11:16]; out['guard_ret_pct'] = round(g[2] - cost_pct, 3)
    if tx and path[-1][0][11:16] >= _hm_shift(exit_hm, -8):
        out['closed'] = True; out['exit_px'] = path[-1][1]
        out['ret_pct'] = round(r[-1] - cost_pct, 3); out['win'] = out['ret_pct'] > 0
        out['book_ret_pct'] = round(book_pct - cost_pct, 3) if hit else out['ret_pct']
        if item.get('stop_pct') and not out.get('stopped'): out['stop_ret_pct'] = out['ret_pct']
        if not g: out['guard_ret_pct'] = out['ret_pct']
    return out


# ================================================================== live loop
def _ranges(day: str) -> dict:
    """symbol -> 20-day average daily range (%) from the morning prep (sizes the stop of stocks that join after 09:25)."""
    prep = d._read_json(f'prep_{day}.json', None) or {}
    return {k: round(float(v['rng20']) * 100, 3) for k, v in (prep.get('hist') or {}).items() if isinstance(v, dict) and v.get('rng20')}


def tick(now: dt.datetime | None = None, force: bool = False) -> None:
    """Called after each order-flow poll; works only when a NEW price poll has arrived (or every 5 min as a fallback)."""
    s = load_settings()
    if not s['enabled']: return
    now = now or d.now_ist(); hm = now.strftime('%H:%M'); today = str(now.date())
    if now.weekday() >= 5 or not ('09:26' <= hm <= '15:30'): return
    polls = d.lite_polls(today)
    if not force and _last['day'] == today and _last['n'] == len(polls) and time.time() - _last['t'] < 300: return
    _last.update(day=today, n=len(polls), t=time.time())
    with _lock:
        days = d._read_jsonl(FILE); rec = next((x for x in days if x.get('date') == today), None)
        if rec and rec.get('done'): return
        day925 = next((x for x in reversed(d._read_jsonl('days.jsonl')[-5:]) if x.get('date') == today), None)
        if rec is None:
            if not day925: return
            rec = {'date': today, 'items': [], 'notes': [], 'settings': s}; days.append(rec)
        if hm < s['exit_at']:                                         # nothing joins after the exit time
            h_rec = next((x for x in reversed(d._read_jsonl('v47_days.jsonl')[-5:]) if x.get('date') == today), None)
            rs_rec = next((x for x in reversed(d._read_jsonl('rs_days.jsonl')[-5:]) if x.get('date') == today), None)
            last = polls[-1] if polls else {}
            prices = {k: _px(v, 0) for k, v in (last.get('q') or {}).items()}
            rec['items'], new = merge(rec['items'], candidates(day925, h_rec, rs_rec, s, _ranges(today)), s, rec['notes'],
                                      str(last.get('t') or now.isoformat())[:19], prices)
            rec['notes'] += new
        if polls:
            mk: dict = {}
            for it in rec['items']:
                ex = it.get('exit_at') or s['exit_at']; key = (it['added'], ex)
                if key not in mk: mk[key] = market_path(polls, it['added'], _exit_poll_t(polls, ex, today))
                it['track'] = track(it, polls, ex, s['book_pct'], s['guard_pct'], mkt=mk[key])
        if hm >= s['exit_at'] and rec['items'] and all((it.get('track') or {}).get('closed') for it in rec['items']): rec['done'] = True
        rec['updated'] = str(now)[:19]
        d._rewrite_jsonl(FILE, days)


# ================================================================== scorecard
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
            'stop': _sc([i for i in traded if i.get('stop_pct')], 'stop_ret_pct'), 'guard': _sc(traded, 'guard_ret_pct'),
            'watch': _sc([i for i in done if not i.get('trade')], 'ret_pct'), 'late': _sc([i for i in traded if i.get('late')], 'ret_pct'),
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
            if not isinstance(p.get('ret_pct'), (int, float)): continue
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
    if time.time() - _bd_cache['t'] > 120:                            # the breakdown re-reads the direction ledger: at most every 2 min
        _bd_cache.update(t=time.time(), v=pool_breakdown(d._read_jsonl('days.jsonl')))
    return {'settings': load_settings(), 'today': next((x for x in days if x.get('date') == today), None), 'score': scorecard(days),
            'breakdown': _bd_cache['v']}
