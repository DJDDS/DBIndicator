"""V13.8 Daily-pool lab: is DOWN-only right, or should the pool also take UP calls (and with which exit)?

Out-of-sample by construction. The 09:25 models were frozen on data up to 2026-10-01, so replaying them over older days would grade them
on days they learned from. Instead this lab uses only what the desk recorded live after that: every stock's 09:25 model scores
(scores_<d>.json.gz) and every stock's 5-minute candles of the day (bars_<d>.json.gz, taken after the close). For every recorded day
it replays a FIXED list of pool rules (declared below, never tuned on these days) and reports picks, win %, average after cost and
how often at least 1 of the day's picks was right. The table grows by one day after each close.

Why UP gets an earlier exit in some variants: the models' own walk-forward research (meta, 2018-26, strong days, top 5) shows UP calls
winning about as often as DOWN up to 2 hours (30 min 59.7% vs 57.7%, 90 min 60.0% vs 59.7%, 120 min 59.1% vs 62.3%) but fading when
held to 15:15 (55.0% vs 63.6%). So 'UP exits 11:25' is the research-backed way to add the UP side.
Entry = the 09:25 price (close of the 09:20 candle), as in research; cost 0.06% a trade.
"""
from __future__ import annotations

import logging
import math
import threading

import numpy as np
import pandas as pd

from . import v133_direction as d

log = logging.getLogger(__name__)
CACHE = 'pool_lab.json'
K_1125, K_1515 = d.HORIZON_BARS['120'], d.K_EXIT
BOOK = 1.0
TOP = 3
# name -> rule. side: 'DOWN' | 'UP' | 'both'; up_exit / down_exit: candle index; when: 'all' | 'strong_up' (UP only on ★ strong UP-lean days)
VARIANTS = {
    'A. DOWN only, 15:15 (current)':            {'side': 'DOWN', 'down_exit': K_1515},
    'B. both sides, 15:15':                     {'side': 'both', 'down_exit': K_1515, 'up_exit': K_1515},
    'C. both sides, UP exits 11:25':            {'side': 'both', 'down_exit': K_1515, 'up_exit': K_1125},
    'D. UP only, 11:25':                        {'side': 'UP', 'up_exit': K_1125},
    'E. UP only, 15:15':                        {'side': 'UP', 'up_exit': K_1515},
    'F. DOWN 15:15; on ★ UP-lean days UP 11:25': {'side': 'DOWN', 'down_exit': K_1515, 'up_exit': K_1125, 'when': 'strong_up'},
    'G. both sides, UP 11:25, conf >= 0.14':    {'side': 'both', 'down_exit': K_1515, 'up_exit': K_1125, 'min_conf': 0.14},
}
_lock = threading.Lock()


def _scores(day: str) -> pd.DataFrame | None:
    raw = d._read_gz(f'scores_{day}.json.gz', None)
    if not raw or not raw.get('data'): return None
    X = pd.DataFrame(raw['data'], columns=raw['columns'])
    ps = [c for c in (f'p_{h}' for h in d.HORIZONS) if c in X]
    if 'symbol' not in X or 'p925' not in X or len(ps) < len(d.HORIZONS): return None
    X['p'] = X[ps].mean(axis=1); X['conf'] = (X.p - 0.5).abs(); X['side'] = np.where(X.p > 0.5, 'UP', 'DOWN')
    return X


def outcome(bars: list, p925: float, side: str, k_exit: int, book: float = BOOK, cost: float = d.COST) -> dict | None:
    """bars: [[k, o, h, l, c, v], ...] of one stock. Hold result from the 09:25 price to the close of candle k_exit, and the +book% booking."""
    C = {int(b[0]): b for b in bars}
    ks = [k for k in sorted(C) if 2 <= k <= k_exit]
    last = next((C[k][4] for k in sorted(C, reverse=True) if k <= k_exit), None)
    if not ks or not last or not p925: return None
    sg = 1 if side == 'UP' else -1
    hold = (math.log(last / p925) * sg - cost) * 100
    hit = any(((C[k][2] / p925 - 1) if sg > 0 else (1 - C[k][3] / p925)) * 100 >= book for k in ks)
    return {'hold': round(hold, 4), 'book': round(book - cost * 100, 4) if hit else round(hold, 4)}


def day_picks(X: pd.DataFrame, rule: dict, strong: bool, lean: str | None, top: int = TOP) -> list:
    """The day's pool under one rule: list of (symbol, side, p925, exit candle)."""
    Y = X[X.conf >= rule.get('min_conf', 0.0)]
    side = rule['side']
    if rule.get('when') == 'strong_up' and strong and lean == 'UP': side = 'UP'
    if side != 'both': Y = Y[Y.side == side]
    Y = Y.sort_values('conf', ascending=False).head(top)
    return [(r.symbol, r.side, float(r.p925), rule['up_exit'] if r.side == 'UP' else rule['down_exit']) for r in Y.itertuples()]


def evaluate_day(X: pd.DataFrame, bars: dict, strong: bool, lean: str | None) -> dict:
    """variant -> list of {'symbol','side','hold','book'} for one day."""
    out = {}
    for name, rule in VARIANTS.items():
        rows = []
        for sym, side, p925, k in day_picks(X, rule, strong, lean):
            o = outcome(bars.get(sym) or [], p925, side, k)
            if o: rows.append({'symbol': sym, 'side': side, **o})
        out[name] = rows
    return out


def summarize(per_day: dict) -> list:
    res = []
    for name in VARIANTS:
        days = [v.get(name) or [] for v in per_day.values()]
        rows = [r for x in days for r in x]; days = [x for x in days if x]
        row = {'variant': name, 'days': len(days), 'picks': len(rows)}
        for k in ('hold', 'book'):
            r = [x[k] for x in rows]
            if r: row[k] = {'win': round(100 * sum(v > 0 for v in r) / len(r), 1), 'avg_bps': round(float(np.mean(r)) * 100, 1),
                            'ge1': round(100 * sum(any(x[k] > 0 for x in dd) for dd in days) / len(days), 1)}
        up = [x['hold'] for x in rows if x['side'] == 'UP']; dn = [x['hold'] for x in rows if x['side'] == 'DOWN']
        row['up_n'], row['down_n'] = len(up), len(dn)
        row['up_win'] = round(100 * sum(v > 0 for v in up) / len(up), 1) if up else None
        row['down_win'] = round(100 * sum(v > 0 for v in dn) / len(dn), 1) if dn else None
        res.append(row)
    return res


def refresh() -> dict:
    """Evaluate every recorded day not yet in the cache (cheap after the first run: one new day per close)."""
    with _lock:
        cache = d._read_json(CACHE, {}) or {}
        per_day = cache.get('per_day') or {}
        flags = {x['date']: (x.get('lists') or {}).get('hc') or {} for x in d._read_jsonl('days.jsonl')}
        for p in sorted(d.ROOT.glob('bars_*.json.gz')) if d.ROOT.exists() else []:
            day = p.name[5:15]
            if day in per_day: continue
            X = _scores(day); bars = d._read_gz(p.name, None)
            if X is None or not bars: continue
            f = flags.get(day) or {}
            try: per_day[day] = evaluate_day(X, bars, bool(f.get('strong')), f.get('lean'))
            except Exception:  # noqa: BLE001
                log.exception('v138 pool lab %s', day)
        out = {'per_day': per_day, 'summary': summarize(per_day), 'first': min(per_day) if per_day else None,
               'last': max(per_day) if per_day else None, 'research': research_sides()}
        d._write_json(CACHE, out)
        return out


def research_sides() -> list:
    """The models' own walk-forward research (2018-26, strong days, top 5): DOWN vs UP win % per horizon."""
    try:
        m = d.meta()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for h, v in (m.get('horizons') or {}).items():
        r = (v.get('research') or {}).get('strong_top5') or {}; e = (v.get('research') or {}).get('every_top5') or {}
        out.append({'horizon': h, 'down_strong': r.get('short_win'), 'up_strong': r.get('long_win'), 'down_every': e.get('short_win'), 'up_every': e.get('long_win')})
    return out


def status() -> dict:
    c = d._read_json(CACHE, None)
    if c is None:
        try: c = refresh()
        except Exception as exc:  # noqa: BLE001
            log.exception('v138 status'); return {'error': str(exc)[:200]}
    c = dict(c); c.pop('per_day', None)
    return c
