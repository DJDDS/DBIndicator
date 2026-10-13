"""V13.4 Relative-Strength desk (paper only, spotting only; never places orders).

Research (2019-26, 5-min bars of the F&O stocks, hold to 15:15): from 11:15 on, stocks that are far stronger than the market keep
outperforming it and far weaker ones keep underperforming. The edge grows with strength and was positive in all 8 years at every
cut-off tried (z>=3: +0.18%/pick over the market, about 4 picks per refresh; z>=3.5: +0.22%, about 2.5). It is a market-relative
(paired) signal, so an index rally does not turn it into a one-sided bet. Early slots (09:25/10:15) behave the opposite way, so only
11:15 onward is used.

How it works, all from the order-flow polls the direction desk already records (no extra Kite calls):
  move   = last price / open - 1
  e      = move minus the all-stock average move
  z      = cross-sectional z-score of e
  picks  = every stock with |z| >= z_min (UP if z>0, DOWN if z<0), strongest first, at most max_picks; may be none.
Order flow is a CHECK, not a filter: each pick shows whether today's order-flow score agrees (OF+) or not (OF-). Picks are never removed
by it. Refreshes at 11:15, 12:15 and 13:15; results measured at 15:15 against the market's own move.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import threading

import numpy as np

from . import v133_direction as d

log = logging.getLogger(__name__)
FILE = 'rs_days.jsonl'
DEFAULT_SETTINGS = {'enabled': True, 'slots': ['11:15', '12:15', '13:15'], 'exit_at': '15:15', 'z_min': 3.0, 'max_picks': 5,
                    'min_stocks': 100, 'regime_pct': 0.5, 'persist_z': 1.5, 'persist_back_min': 60}
_lock = threading.Lock()


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update({k: v for k, v in (d._read_json('rs_settings.json', {}) or {}).items() if k in s})
        s['z_min'] = float(min(6.0, max(1.0, float(s['z_min'])))); s['max_picks'] = int(min(12, max(1, int(s['max_picks']))))
        s['min_stocks'] = int(max(20, int(s['min_stocks']))); s['regime_pct'] = float(max(0.1, float(s['regime_pct']))); s['persist_z'] = float(max(0.0, float(s['persist_z']))); s['persist_back_min'] = int(max(15, int(s['persist_back_min'])))
        s['slots'] = [x for x in s['slots'] if isinstance(x, str) and len(x) == 5 and x[2] == ':']
    except Exception:  # noqa: BLE001
        log.exception('v134: bad settings; using defaults'); return dict(DEFAULT_SETTINGS)
    return s


def _minutes(hm: str) -> int:
    return int(hm[:2]) * 60 + int(hm[3:5])


def _poll_at(polls: list, hm: str, tolerance: int = 6):
    """The poll nearest to hm (not earlier than hm - tolerance minutes, not later than hm + tolerance)."""
    best, gap = None, None
    for p in polls:
        t = str(p.get('t', ''))[11:16]
        if len(t) != 5: continue
        g = abs(_minutes(t) - _minutes(hm))
        if g <= tolerance and (gap is None or g < gap): best, gap = p, g
    return best


def _px(row, i):
    try:
        v = float(row[i]); return v if math.isfinite(v) and v > 0 else None
    except (TypeError, ValueError, IndexError):
        return None


def _zmap(polls: list, hm: str, min_stocks: int):
    """symbol -> z of (move since open minus market move) at hm, plus the rows and market move. None when quotes are too few."""
    pl = _poll_at(polls, hm)
    if not pl: return None
    rows = {}
    for sym, row in (pl.get('q') or {}).items():
        ltp, op = _px(row, 0), _px(row, 13)
        if ltp and op: rows[sym] = (ltp, op, (ltp / op - 1) * 100)
    if len(rows) < min_stocks: return {'pl': pl, 'rows': rows, 'z': None}
    syms = list(rows); r = np.array([rows[k][2] for k in syms]); e = r - r.mean(); sd = float(e.std())
    if not sd > 0: return {'pl': pl, 'rows': rows, 'z': None, 'm': float(r.mean())}
    z = (e - e.mean()) / sd
    return {'pl': pl, 'rows': rows, 'syms': syms, 'r': r, 'e': e, 'z': z, 'm': float(r.mean())}


def _hm_minus(hm: str, minutes: int) -> str:
    t = _minutes(hm) - minutes; return '%02d:%02d' % (t // 60, t % 60)


def slot_picks(polls: list, hm: str, s: dict) -> dict | None:
    """Pure: compute one refresh from today's polls."""
    Z = _zmap(polls, hm, s['min_stocks'])
    if not Z: return None
    pl, rows = Z['pl'], Z['rows']
    if Z['z'] is None: return {'at': hm, 'poll': pl['t'], 'n': len(rows), 'picks': [], 'note': 'too few quotes or no dispersion'}
    syms, r, e, z, m = Z['syms'], Z['r'], Z['e'], Z['z'], Z['m']
    Zp = _zmap(polls, _hm_minus(hm, s['persist_back_min']), s['min_stocks'])      # an hour earlier: strength must be building, not a sudden spike
    zp = dict(zip(Zp['syms'], Zp['z'])) if Zp and Zp.get('z') is not None else None
    ofs = d.of_scores([p for p in polls if str(p.get('t', ''))[11:16] <= hm], {k: v[1] for k, v in rows.items()})
    order = sorted(range(len(syms)), key=lambda i: -abs(z[i]))
    picks, skipped = [], []
    for i in order:
        if abs(z[i]) < s['z_min']: break
        side = 'UP' if z[i] > 0 else 'DOWN'
        prior = None if zp is None else zp.get(syms[i])
        if zp is not None and (prior is None or prior * z[i] <= 0 or abs(prior) < s['persist_z']):
            skipped.append({'symbol': syms[i], 'side': side, 'z': round(float(z[i]), 2), 'why': 'sudden spike (not building for the last hour)'}); continue
        if len(picks) >= s['max_picks']: break
        o = ofs.get(syms[i])
        picks.append({'symbol': syms[i], 'side': side, 'z': round(float(z[i]), 2), 'z_prev': None if prior is None else round(float(prior), 2),
                      'move_pct': round(float(r[i]), 2), 'vs_mkt_pct': round(float(e[i]), 2), 'entry': rows[syms[i]][0], 'of': o,
                      'of_agree': None if not o else bool((o['ofs'] > 0) == (side == 'UP'))})
    regime = 'UP' if m > s['regime_pct'] else 'DOWN' if m < -s['regime_pct'] else 'FLAT'
    return {'at': hm, 'poll': pl['t'], 'n': len(rows), 'market_pct': round(m, 3), 'regime': regime, 'picks': picks, 'skipped': skipped,
            'ups': sum(1 for p in picks if p['side'] == 'UP'), 'downs': sum(1 for p in picks if p['side'] == 'DOWN')}


def settle(day_rec: dict, polls: list, s: dict) -> bool:
    """Fill in 15:15 results (stock move and the market's move over the same window). Returns True when anything changed."""
    end = _poll_at(polls, s['exit_at'], 8)
    if not end: return False
    changed = False
    for hm, sl in (day_rec.get('slots') or {}).items():
        if sl.get('settled') or not sl.get('picks'): continue
        start = _poll_at(polls, hm)
        if not start: continue
        mk = []
        for sym, row in (end.get('q') or {}).items():
            a, b = _px(((start.get('q') or {}).get(sym)), 0), _px(row, 0)
            if a and b: mk.append((b / a - 1) * 100)
        mk_m = float(np.mean(mk)) if len(mk) >= s['min_stocks'] else None
        for p in sl['picks']:
            b = _px(((end.get('q') or {}).get(p['symbol'])), 0)
            if not b: continue
            sg = 1 if p['side'] == 'UP' else -1
            p['exit'] = b; p['ret_pct'] = round(sg * (b / p['entry'] - 1) * 100, 3); p['win'] = p['ret_pct'] > 0
            if mk_m is not None: p['edge_pct'] = round(p['ret_pct'] - sg * mk_m, 3)
        sl['market_fwd_pct'] = None if mk_m is None else round(mk_m, 3); sl['settled'] = True; changed = True
    return changed


def tick(now: dt.datetime | None = None) -> None:
    """Called after each order-flow poll: compute any due refresh once, settle after the exit time. Cheap when nothing is due."""
    s = load_settings()
    if not s['enabled']: return
    now = now or d.now_ist(); hm = now.strftime('%H:%M'); today = str(now.date())
    if now.weekday() >= 5: return
    with _lock:
        days = d._read_jsonl(FILE); rec = next((x for x in days if x.get('date') == today), None)
        due = [h for h in s['slots'] if _minutes(h) <= _minutes(hm) <= _minutes(h) + 25 and h not in ((rec or {}).get('slots') or {})]
        settle_due = _minutes(hm) >= _minutes(s['exit_at']) and rec and any(not x.get('settled') and x.get('picks') for x in rec['slots'].values())
        if not due and not settle_due: return
        polls = d._read_of(today)
        if not polls: return
        if rec is None:
            rec = {'date': today, 'slots': {}}; days.append(rec)
        for h in due:
            res = slot_picks(polls, h, s)
            if res: rec['slots'][h] = res
        if settle_due: settle(rec, polls, s)
        rec['settings'] = {k: s[k] for k in ('z_min', 'max_picks', 'slots', 'exit_at')}
        d._rewrite_jsonl(FILE, days[-120:])


def status() -> dict:
    days = d._read_jsonl(FILE); today = str(d.now_ist().date())
    done = [p for x in days for sl in x.get('slots', {}).values() for p in sl.get('picks', []) if 'ret_pct' in p]
    edge = [p['edge_pct'] for p in done if 'edge_pct' in p]

    def sc(ps):
        r = [p['ret_pct'] for p in ps]
        return {'n': len(r), 'win': round(100 * sum(1 for x in r if x > 0) / len(r), 1) if r else None, 'avg_pct': round(float(np.mean(r)), 3) if r else None}
    return {'settings': load_settings(), 'today': next((x for x in days if x.get('date') == today), None), 'recent': list(reversed(days))[:10],
            'score': {'all': sc(done), 'up': sc([p for p in done if p['side'] == 'UP']), 'down': sc([p for p in done if p['side'] == 'DOWN']),
                      'of_agree': sc([p for p in done if p.get('of_agree') is True]), 'of_against': sc([p for p in done if p.get('of_agree') is False]),
                      'edge_avg_pct': round(float(np.mean(edge)), 3) if edge else None, 'edge_n': len(edge)}}
