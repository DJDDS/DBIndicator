"""V13.9 Market Pulse for the dashboard: how the F&O market is behaving through the day (replaces Confluence Watch).

Built only from the price polls the order-flow recorder already writes (v133 lite reader: last price, day volume, day open per
F&O stock, every 10 s to 09:40 and every 5 min to 15:15), so it costs no extra Kite calls:
  - through the day (one point per 5 min): breadth = % of F&O stocks above their open, and the median stock's move since the open
  - sector rotation now: each industry's median move since the open, with how many of its stocks are up / down (>= 3 stocks)
  - leaders and laggards vs their own sector: stock move minus its industry median (who is really strong / weak, not just
    riding its sector), top 5 each side
After the close (or before 09:15) it shows the last recorded session, labelled with its date.
"""
from __future__ import annotations

import datetime as dt
import logging
import time

import numpy as np

from . import v133_direction as d
from .v134_relstrength import _px

log = logging.getLogger(__name__)
MIN_STOCKS, MIN_SECTOR = 50, 3
_cache: dict = {'t': 0.0, 'v': None}


def _moves(poll: dict) -> dict:
    """symbol -> % move since the day's open for one poll."""
    out = {}
    for s, r in (poll.get('q') or {}).items():
        a, b = _px(r, 2), _px(r, 0)
        if a and b: out[s] = (b / a - 1) * 100
    return out


def compute(polls: list, ind: dict, top: int = 5) -> dict | None:
    """Pure: the pulse for one day's (lite) polls."""
    pts, last_bucket = [], None
    for p in polls:
        t = str(p.get('t', ''))
        hm = t[11:16]
        if not ('09:15' <= hm <= '15:30'): continue
        m = _moves(p)
        if len(m) < MIN_STOCKS: continue
        b = (int(hm[:2]) * 60 + int(hm[3:5])) // 5
        row = [hm, round(100 * sum(v > 0 for v in m.values()) / len(m), 1), round(float(np.median(list(m.values()))), 3)]
        if b == last_bucket: pts[-1] = row
        else: pts.append(row); last_bucket = b
    last = next((p for p in reversed(polls) if len(_moves(p)) >= MIN_STOCKS and '09:15' <= str(p.get('t', ''))[11:16] <= '15:30'), None)
    if not last: return None
    m = _moves(last)
    groups: dict = {}
    for s, v in m.items():
        g = ind.get(s)
        if g: groups.setdefault(g, []).append((s, v))
    sectors, rel = [], []
    for g, rows in groups.items():
        if len(rows) < MIN_SECTOR: continue
        med = float(np.median([v for _, v in rows]))
        best = max(rows, key=lambda x: x[1]); worst = min(rows, key=lambda x: x[1])
        sectors.append({'sector': g, 'pct': round(med, 2), 'up': sum(v > 0 for _, v in rows), 'down': sum(v < 0 for _, v in rows), 'n': len(rows),
                        'best': [best[0], round(best[1], 2)], 'worst': [worst[0], round(worst[1], 2)]})
        rel += [{'symbol': s, 'sector': g, 'pct': round(v, 2), 'vs_sector': round(v - med, 2)} for s, v in rows]
    sectors.sort(key=lambda x: -x['pct'])
    rel.sort(key=lambda x: -x['vs_sector'])
    vals = list(m.values())
    return {'date': str(last['t'])[:10], 'at': str(last['t'])[11:16], 'n': len(m), 'curve': pts,
            'breadth': round(100 * sum(v > 0 for v in vals) / len(vals), 1), 'up': sum(v > 0 for v in vals), 'down': sum(v < 0 for v in vals),
            'median': round(float(np.median(vals)), 2), 'sectors': sectors, 'leaders': rel[:top], 'laggards': list(reversed(rel[-top:]))}


def status(now: dt.datetime | None = None) -> dict | None:
    """Today's pulse, or the last recorded session's when today has no polls yet. Cached 60 s."""
    if time.time() - _cache['t'] < 60 and _cache['v'] is not None: return _cache['v']
    v = None
    try:
        from . import v135_hierarchy
        ind = v135_hierarchy.industry_map()
        day = (now or d.now_ist()).date()
        for back in range(0, 8):
            dd = day - dt.timedelta(days=back)
            if dd.weekday() >= 5: continue
            polls = d.lite_polls(dd) if back == 0 else [d._lite_row(x) for x in d._read_of(dd)]
            v = compute(polls, ind) if polls else None
            if v:
                v['live'] = back == 0 and (now or d.now_ist()).strftime('%H:%M') <= '15:30'
                break
    except Exception:  # noqa: BLE001
        log.exception('v139 market pulse failed')
    _cache.update(t=time.time(), v=v)
    return v
