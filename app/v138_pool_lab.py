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

MIXES (09:25 model x 09:45 hierarchy desk), also fixed in advance, entry at the 09:45 price, UP exits 11:25 and DOWN 15:15:
  H  hierarchy rule alone (both sides; H-DOWN = DOWN only)     M1 both agree, ranked by model confidence
  M2 model top 3 kept only if by 09:45 it moved its way and its industry agrees     M3 both agree, blended rank of confidence and hierarchy score
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
    # model choice for the 15:15 hold: the whole-day model alone vs the 5-model average (the desk's "Whole day" list vs its daily pool)
    'W1. whole-day model only, both sides, 15:15': {'side': 'both', 'down_exit': K_1515, 'up_exit': K_1515, 'model': 'day'},
    'W2. whole-day model only, DOWN only, 15:15':  {'side': 'DOWN', 'down_exit': K_1515, 'model': 'day'},
    'W3. average and whole-day agree, both, 15:15': {'side': 'both', 'down_exit': K_1515, 'up_exit': K_1515, 'model': 'agree'},
    'W4. 50/50 average + whole-day, both, 15:15':   {'side': 'both', 'down_exit': K_1515, 'up_exit': K_1515, 'model': 'blend'},
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


def outcome(bars: list, p925: float, side: str, k_exit: int, book: float = BOOK, cost: float = d.COST, k_from: int = 2) -> dict | None:
    """bars: [[k, o, h, l, c, v], ...] of one stock. Hold result from the entry price (p925 = the 09:25 price by default; with k_from=6 the
    09:45 price) to the close of candle k_exit, and the +book% booking on the candles after the entry."""
    C = {int(b[0]): b for b in bars}
    ks = [k for k in sorted(C) if k_from <= k <= k_exit]
    last = next((C[k][4] for k in sorted(C, reverse=True) if k <= k_exit), None)
    if not ks or not last or not p925: return None
    sg = 1 if side == 'UP' else -1
    hold = (math.log(last / p925) * sg - cost) * 100
    hit = any(((C[k][2] / p925 - 1) if sg > 0 else (1 - C[k][3] / p925)) * 100 >= book for k in ks)
    return {'hold': round(hold, 4), 'book': round(book - cost * 100, 4) if hit else round(hold, 4)}


def day_picks(X: pd.DataFrame, rule: dict, strong: bool, lean: str | None, top: int = TOP) -> list:
    """The day's pool under one rule: list of (symbol, side, p925, exit candle).
    rule['model']: None = the 5-model average (the live pool), 'day' = the whole-day model alone, 'agree' = both point the same way
    (ranked by the average's confidence), 'blend' = half average, half whole-day model."""
    m = rule.get('model')
    if m and 'p_day' in X:
        X = X.copy()
        if m == 'day': X['p'] = X['p_day']
        elif m == 'blend': X['p'] = (X['p'] + X['p_day']) / 2
        X['conf'] = (X.p - 0.5).abs(); X['side'] = np.where(X.p > 0.5, 'UP', 'DOWN')
        if m == 'agree': X = X[(X.p > 0.5) == (X.p_day > 0.5)]
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


# ================================================================== 09:25 model x 09:45 hierarchy (mixes)
K945 = 5                       # the 09:40-09:45 candle closes at 09:45: the hierarchy desk's entry


def hier_frame(X: pd.DataFrame, bars: dict, ind: dict, min_group: int = 4) -> pd.DataFrame:
    """Per stock at 09:45, rebuilt from the day's candles exactly as the hierarchy desk does from its polls (prices at 09:30/35/40/45 =
    closes of candles 2..5): r15, path efficiency, market (median r15 of all stocks), industry median (>= min_group stocks), the hierarchy
    side and whether the stock is eligible (industry agrees, market not against, leads its industry); plus the 09:25 model's call.
    The volume factor is left out: the recorded window is too short for its 20-day baseline (both H and the mixes are scored without it)."""
    rows = []
    for r in X.itertuples():
        b = {int(x[0]): x for x in (bars.get(r.symbol) or [])}
        if not all(k in b for k in (2, 3, 4, 5)): continue
        c = [float(b[k][4]) for k in (2, 3, 4, 5)]
        if min(c) <= 0: continue
        path = sum(abs(c[i + 1] - c[i]) for i in range(3))
        rows.append({'symbol': r.symbol, 'p925': float(r.p925), 'conf': float(r.conf), 'mside': r.side, 'p945': c[3],
                     'r15': (c[3] / c[0] - 1) * 100, 'since925': (c[3] / float(r.p925) - 1) * 100 if r.p925 else np.nan,
                     'eff': min(1.0, abs(c[3] - c[0]) / path) if path > 0 else np.nan, 'industry': ind.get(r.symbol)})
    H = pd.DataFrame(rows)
    if H.empty: return H
    H['mkt'] = H.r15.median()
    g = H.dropna(subset=['industry']).groupby('industry').r15
    H['ind_med'] = H.industry.map(g.median()); H['ind_n'] = H.industry.map(g.size())
    H['hside'] = np.where(H.r15 >= 0, 'UP', 'DOWN'); sg = np.where(H.r15 >= 0, 1, -1)
    H['resid'] = sg * (H.r15 - H.ind_med)
    H['eligible'] = (H.ind_n >= min_group) & (sg * H.ind_med > 0) & (sg * H.mkt >= 0) & (H.resid > 0) & H.eff.notna()
    H['hscore'] = np.where(H.eligible, H.resid * H.eff.clip(0, 1), np.nan)
    H['agree'] = H.eligible & (H.hside == H.mside)
    return H


def _exit(side: str) -> int:
    return K_1125 if side == 'UP' else K_1515


MIXES = {
    'H. hierarchy only (both sides), 09:45 → 15:15':  lambda H: [(r.symbol, r.hside, r.p945, K_1515) for r in H[H.eligible].nlargest(TOP, 'hscore').itertuples()],
    'H-DOWN. hierarchy DOWN only, 09:45 → 15:15':     lambda H: [(r.symbol, 'DOWN', r.p945, K_1515) for r in H[H.eligible & (H.hside == 'DOWN')].nlargest(TOP, 'hscore').itertuples()],
    'M1. model and hierarchy agree, by confidence':  lambda H: [(r.symbol, r.mside, r.p945, _exit(r.mside)) for r in H[H.agree].nlargest(TOP, 'conf').itertuples()],
    'M2. model top 3 confirmed at 09:45':            lambda H: [(r.symbol, r.mside, r.p945, _exit(r.mside)) for r in H.nlargest(TOP, 'conf').itertuples()
                                                            if (1 if r.mside == 'UP' else -1) * r.since925 > 0 and (1 if r.mside == 'UP' else -1) * (r.ind_med if r.ind_med == r.ind_med else 0) > 0],
    'M3. both agree, blended rank':                  lambda H: [(r.symbol, r.mside, r.p945, _exit(r.mside)) for r in
                                                            H[H.agree].assign(rk=lambda z: z.conf.rank(pct=True) + z.hscore.rank(pct=True)).nlargest(TOP, 'rk').itertuples()],
}


def evaluate_mix(X: pd.DataFrame, bars: dict, ind: dict) -> dict:
    """mix -> list of {'symbol','side','hold','book'} for one day (entries at the 09:45 price)."""
    H = hier_frame(X, bars, ind); out = {}
    for name, pick in MIXES.items():
        rows = []
        if not H.empty:
            for sym, side, entry, k in pick(H):
                o = outcome(bars.get(sym) or [], entry, side, k, k_from=K945 + 1)
                if o: rows.append({'symbol': sym, 'side': side, **o})
        out[name] = rows
    return out


def summarize(per_day: dict, names=None) -> list:
    res = []
    for name in (names or list(VARIANTS) + list(MIXES)):
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
        from . import v135_hierarchy
        ind = v135_hierarchy.industry_map()
        for p in sorted(d.ROOT.glob('bars_*.json.gz')) if d.ROOT.exists() else []:
            day = p.name[5:15]
            if day in per_day and all(k in per_day[day] for k in MIXES): continue
            X = _scores(day); bars = d._read_gz(p.name, None)
            if X is None or not bars: continue
            f = flags.get(day) or {}
            try: per_day[day] = {**evaluate_day(X, bars, bool(f.get('strong')), f.get('lean')), **evaluate_mix(X, bars, ind)}
            except Exception:  # noqa: BLE001
                log.exception('v138 pool lab %s', day)
        out = {'per_day': per_day, 'summary': summarize(per_day, list(VARIANTS)), 'mix': summarize(per_day, list(MIXES)),
               'overlap': overlap(per_day), 'first': min(per_day) if per_day else None,
               'last': max(per_day) if per_day else None, 'research': research_sides()}
        d._write_json(CACHE, out)
        return out


def overlap(per_day: dict) -> dict:
    """How often the 09:25 pool (rule A) and the hierarchy (H) pick the same stock - low overlap means the two see different things."""
    a = 'A. DOWN only, 15:15 (current)'; h = 'H. hierarchy only (both sides), 09:45 → 15:15'
    same = tot = 0
    for v in per_day.values():
        sa = {r['symbol'] for r in v.get(a) or []}; sh = {r['symbol'] for r in v.get(h) or []}
        if sa and sh: same += len(sa & sh); tot += len(sh)
    return {'shared_pct': round(100 * same / tot, 1) if tot else None, 'h_picks': tot}


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
