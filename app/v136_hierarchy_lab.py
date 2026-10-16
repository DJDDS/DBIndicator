"""V13.6 Hierarchy lab: does the V4.7 market > industry > stock rule (v135_hierarchy) work on the UP side, the DOWN side, both or neither?

RESEARCH ONLY. Runs on the server (it needs the Kite login), in a background thread, outside market hours.
  1. Data: Kite 5-minute candles for today's F&O stocks (survivorship bias: today's list, not point-in-time membership), reduced at once to one
     row per stock-day holding exactly what the live rule sees: prices at 09:30/09:35/09:40/09:45 (closes of the 09:25..09:40 bars),
     09:30-09:45 volume, and the outcomes after the 09:45 entry (+15/+30/+60 min, 15:15, high/low until 15:15). Cached per stock, incremental.
  2. Replay: the live rule exactly (market = median r15 of all stocks, industry median with >= min_group stocks, 20-day volume median with
     >= min_vol_days days), for UP and DOWN separately, over a small FIXED grid: efficiency gate {0, 0.5} x volume gate {0, 1.5} x market tailwind
     {0, 0.15%} x exit {+30 min, +60 min, 15:15, book at +1% else 15:15} x top {1, 3} = 64 configs per side.
  3. Walk-forward: each side's config is chosen on days before TEST_FROM only, then judged on the later days it never saw.
     A side is ACTIVE only when the test period is net positive after cost, t >= 1.5 over >= 100 test days, and positive in most test years.
     Otherwise it stays SHADOW: the live desk still records its best stock every day, but never as a pick.
"""
from __future__ import annotations

import datetime as dt
import itertools
import logging
import math
import threading
import time

import numpy as np
import pandas as pd

from . import v133_direction as d

log = logging.getLogger(__name__)
RESULT = 'v47lab_result.json'
STATE = 'v47lab_state.json'
CACHE_DIR = 'v47lab'
DEFAULT_START = dt.date(2019, 1, 1)
TEST_FROM = dt.date(2024, 1, 1)
EXITS = ('30', '60', 'exit', 'book1')
GRID = {'min_eff': (0.0, 0.5), 'min_volrel': (0.0, 1.5), 'min_mkt': (0.0, 0.15), 'exit': EXITS, 'top': (1, 3)}
BASE = {'min_eff': 0.0, 'min_volrel': 0.0, 'min_mkt': 0.0, 'exit': 'exit', 'top': 1}
MIN_TRAIN_DAYS, MIN_TEST_DAYS, MIN_T = 120, 100, 1.5
BOOK_PCT = 1.0
COLS = ('p930', 'p935', 'p940', 'p945', 'v15', 'p15', 'p30', 'p60', 'pexit', 'hi', 'lo')
_lock = threading.Lock()
_thread: threading.Thread | None = None


# ================================================================== data
def reduce_bars(bars: pd.DataFrame) -> pd.DataFrame:
    """5-minute candles of one stock -> one row per session with the live rule's inputs and outcomes (see module doc)."""
    if bars is None or bars.empty: return pd.DataFrame(columns=('date',) + COLS)
    b = bars.copy(); b['date'] = pd.to_datetime(b['date'])
    m = b.date.dt.hour * 60 + b.date.dt.minute
    b['k'] = ((m - 555) // 5).astype(int); b['d'] = b.date.dt.date       # k = bar index from 09:15 (k=2 is the 09:25 bar, ends 09:30)
    b = b[(b.k >= 0) & (b.k <= 74)].drop_duplicates(['d', 'k'], keep='last')
    rows = []
    for day, g in b.groupby('d', sort=True):
        C = np.full(75, np.nan); H = C.copy(); L = C.copy(); V = C.copy()
        k = g.k.to_numpy()
        C[k] = g.close.to_numpy(float); H[k] = g.high.to_numpy(float); L[k] = g.low.to_numpy(float); V[k] = g.volume.to_numpy(float)
        if np.isnan(C[2:6]).any() or np.isnan(V[3:6]).any(): continue
        Cf = pd.Series(C).ffill().to_numpy()
        rows.append({'date': day, 'p930': C[2], 'p935': C[3], 'p940': C[4], 'p945': C[5], 'v15': float(V[3:6].sum()),
                     'p15': Cf[8], 'p30': Cf[11], 'p60': Cf[17], 'pexit': Cf[71] if np.isfinite(Cf[71]) else np.nan,
                     'hi': np.nanmax(H[6:72]) if np.isfinite(H[6:72]).any() else np.nan, 'lo': np.nanmin(L[6:72]) if np.isfinite(L[6:72]).any() else np.nan})
    return pd.DataFrame(rows, columns=('date',) + COLS)


def _cache_path(sym: str):
    p = d._path(CACHE_DIR); p.mkdir(parents=True, exist_ok=True)
    return p / f'{sym.replace("/", "_")}.pkl'


def _load_cache(sym: str) -> pd.DataFrame:
    p = _cache_path(sym)
    try:
        return pd.read_pickle(p) if p.exists() else pd.DataFrame(columns=('date',) + COLS)
    except Exception:  # noqa: BLE001
        log.exception('v136: cache unreadable %s', sym); return pd.DataFrame(columns=('date',) + COLS)


def update_cache(kite, sym: str, token: int, start: dt.date, end: dt.date) -> pd.DataFrame:
    """Bring one stock's reduced rows up to `end` (incremental; 95-day Kite requests)."""
    old = _load_cache(sym)
    a = (max(old.date) + dt.timedelta(days=1)) if len(old) else start
    parts = [old] if len(old) else []
    while a <= end:
        b = min(a + dt.timedelta(days=95), end)
        bars = d._kite_5min(kite, token, dt.datetime.combine(a, dt.time(9, 15)), dt.datetime.combine(b, dt.time(15, 30)))
        time.sleep(0.35)
        r = reduce_bars(bars)
        if len(r): parts.append(r)
        a = b + dt.timedelta(days=1)
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=('date',) + COLS)
    df = df.drop_duplicates('date', keep='last').sort_values('date').reset_index(drop=True)
    tmp = _cache_path(sym).with_suffix('.tmp'); df.to_pickle(tmp); tmp.replace(_cache_path(sym))
    return df


# ================================================================== replay (pure)
def build_panel(frames: dict, ind: dict, s: dict) -> pd.DataFrame:
    """frames: symbol -> reduced rows. Returns one row per stock-day with side, gates and signed outcomes; same rule as v135_hierarchy.compute."""
    parts = []
    for sym, f in frames.items():
        if f is None or not len(f): continue
        f = f.sort_values('date').copy(); f['symbol'] = sym
        f['vbase'] = f.v15.shift(1).rolling(20, min_periods=s['min_vol_days']).median()   # strictly earlier days, as live
        parts.append(f)
    if not parts: return pd.DataFrame()
    P = pd.concat(parts, ignore_index=True)
    P['r15'] = (P.p945 / P.p930 - 1) * 100
    P = P[np.isfinite(P.r15)]
    P['industry'] = P.symbol.map(ind)
    nday = P.groupby('date').r15.transform('size')
    P['mkt'] = P.groupby('date').r15.transform('median')
    P = P[nday >= s['min_stocks']].copy()
    g = P.dropna(subset=['industry']).groupby(['date', 'industry']).r15
    P['ind_med'] = g.transform('median'); P['ind_n'] = g.transform('size')
    P['side'] = np.where(P.r15 >= 0, 1, -1)
    path = (P.p935 - P.p930).abs() + (P.p940 - P.p935).abs() + (P.p945 - P.p940).abs()
    P['eff'] = np.where(path > 0, np.minimum(1.0, (P.p945 - P.p930).abs() / path.where(path > 0)), np.nan)
    P['volrel'] = np.where(P.vbase > 0, P.v15 / P.vbase, np.nan)
    P['resid'] = P.side * (P.r15 - P.ind_med)
    ok = (P.ind_n >= s['min_group']) & np.isfinite(P.eff) & np.isfinite(P.volrel) & (P.side * P.ind_med > 0) & (P.side * P.mkt >= 0) & (P.resid > 0)
    P = P[ok].copy()
    P['score'] = P.resid * P.eff.clip(0, 1) * P.volrel.clip(0, 4)
    for h in ('15', '30', '60', 'exit'):
        P[f'r_{h}'] = P.side * (P[f'p{h}'] / P.p945 - 1) * 100
    P['mfe'] = np.where(P.side > 0, (P.hi / P.p945 - 1) * 100, (1 - P.lo / P.p945) * 100)
    P['r_book1'] = np.where(P.mfe >= BOOK_PCT, BOOK_PCT, P.r_exit)
    P['year'] = pd.to_datetime(P.date).dt.year
    return P.reset_index(drop=True)


def daily_returns(P: pd.DataFrame, side: int, cfg: dict) -> pd.Series:
    """Per-day mean return (%, gross) of the top-N candidates of one side under one config."""
    q = P[(P.side == side) & (P.eff >= cfg['min_eff']) & (P.volrel >= cfg['min_volrel']) & (P.side * P.mkt >= cfg['min_mkt'])]
    r = f'r_{cfg["exit"]}'
    q = q[np.isfinite(q[r])].sort_values(['date', 'score'], ascending=[True, False])
    q = q.groupby('date').head(cfg['top'])
    return q.groupby('date')[r].mean()


def stats(daily: pd.Series, cost_bps: float) -> dict:
    if daily is None or not len(daily): return {'days': 0}
    x = daily.to_numpy(float) * 100 - cost_bps                      # net bps per day
    n = len(x); sd = float(np.std(x, ddof=1)) if n > 1 else 0.0
    yrs = pd.Series(x, index=pd.to_datetime(pd.Index(daily.index)).year).groupby(level=0)
    return {'days': n, 'win': round(100 * float(np.mean(daily.to_numpy() * 100 > cost_bps)), 1),
            'gross_bps': round(float(np.mean(daily.to_numpy(float))) * 100, 1), 'net_bps': round(float(np.mean(x)), 1),
            't': round(float(np.mean(x)) / sd * math.sqrt(n), 2) if sd > 0 else None,
            'by_year': {int(y): {'days': int(v.size), 'net_bps': round(float(v.mean()), 1)} for y, v in yrs}}


def evaluate(P: pd.DataFrame, cost_bps: float, test_from: dt.date = TEST_FROM) -> dict:
    """Both sides: base rule, every grid config (train / test), the walk-forward choice and the verdict."""
    out = {}
    keys = list(GRID)
    tr_mask = lambda s: s[[dt.date.fromisoformat(str(i)[:10]) < test_from for i in s.index]] if len(s) else s  # noqa: E731
    te_mask = lambda s: s[[dt.date.fromisoformat(str(i)[:10]) >= test_from for i in s.index]] if len(s) else s  # noqa: E731
    for side, name in ((1, 'UP'), (-1, 'DOWN')):
        grid = []
        for vals in itertools.product(*(GRID[k] for k in keys)):
            cfg = dict(zip(keys, vals)); s = daily_returns(P, side, cfg)
            grid.append({'cfg': cfg, 'train': stats(tr_mask(s), cost_bps), 'test': stats(te_mask(s), cost_bps)})
        base = next(g for g in grid if g['cfg'] == BASE)
        fit = [g for g in grid if g['train'].get('days', 0) >= MIN_TRAIN_DAYS]
        best = max(fit, key=lambda g: g['train']['net_bps']) if fit else None
        verdict, why = 'SHADOW', []
        if best is None: why.append(f'no config had {MIN_TRAIN_DAYS}+ training days')
        else:
            te = best['test']; yrs = [v for v in (te.get('by_year') or {}).values() if v['days'] >= 20]
            if te.get('days', 0) < MIN_TEST_DAYS: why.append(f'only {te.get("days", 0)} test days (need {MIN_TEST_DAYS})')
            if not te.get('net_bps', 0) > 0: why.append(f'test net {te.get("net_bps")} bps after {cost_bps:g} bps cost')
            if (te.get('t') or 0) < MIN_T: why.append(f'test t {te.get("t")} < {MIN_T}')
            if yrs and sum(v['net_bps'] > 0 for v in yrs) * 2 <= len(yrs): why.append('not positive in most test years')
            if not why: verdict = 'ACTIVE'
        out[name] = {'base': base, 'best': best, 'verdict': verdict, 'why': why,
                     'top_test': sorted([g for g in grid if g['test'].get('days', 0) >= MIN_TEST_DAYS], key=lambda g: -g['test']['net_bps'])[:5]}
    return out


def market_buckets(P: pd.DataFrame, cost_bps: float) -> dict:
    """Base rule (top pick, 15:15) by how strongly the F&O market moved its way 09:30-09:45 - diagnostic only."""
    res = {}
    for side, name in ((1, 'UP'), (-1, 'DOWN')):
        s = daily_returns(P, side, BASE)
        if not len(s): continue
        mk = P[P.side == side].groupby('date').mkt.first().abs().reindex(s.index)
        for lo, hi in ((0, 0.1), (0.1, 0.3), (0.3, 99)):
            res[f'{name} {lo:g}-{hi:g}%' if hi < 99 else f'{name} {lo:g}%+'] = stats(s[(mk >= lo) & (mk < hi)], cost_bps)
    return res


# ================================================================== runner
def _set_state(**kw) -> dict:
    st = d._read_json(STATE, {}) or {}; st.update(kw); d._write_json(STATE, st); return st


def seed_volume_baseline(frames: dict, keep: int = 25) -> int:
    """Fill v135's 20-day 09:30-09:45 volume baseline from the lab's history (keeps any day already recorded live). Returns stocks seeded."""
    from . import v135_hierarchy as h
    hist = h._vol_hist(); n = 0
    for sym, f in frames.items():
        if f is None or not len(f): continue
        cur = {e[0]: e[1] for e in hist.get(sym, [])}
        for day, v in zip(f.date.tail(keep), f.v15.tail(keep)):
            cur.setdefault(str(day), float(v))
        hist[sym] = [[k, cur[k]] for k in sorted(cur)][-keep:]; n += 1
    d._write_json(h.VOL_FILE, hist)
    return n


def baseline_short(today: str | None = None) -> tuple[int, int]:
    """(days the hierarchy's volume baseline has for its best-covered stock, days it needs)."""
    from . import v135_hierarchy as h
    need = h.load_settings()['min_vol_days']; hist = h._vol_hist()
    have = max((len([e for e in v if e[0] != today]) for v in hist.values()), default=0)
    return have, need


def seed_recent_baseline(kite, days: int = 40, today: dt.date | None = None) -> int:
    """Fill the 20-day 09:30-09:45 volume baseline straight from Kite 5-minute history (last `days` calendar days, one request per stock),
    so the hierarchy desk does not wait 10 sessions after a fresh start. Keeps every day already recorded live. Returns stocks seeded."""
    from . import v132_movers as v2
    today = today or d.now_ist().date()
    a, b = today - dt.timedelta(days=days), today - dt.timedelta(days=1)
    frames = {}
    for sym, tok in sorted(v2._fo_universe(kite).items()):
        try:
            frames[sym] = reduce_bars(d._kite_5min(kite, tok, dt.datetime.combine(a, dt.time(9, 15)), dt.datetime.combine(b, dt.time(15, 30))))
        except Exception as exc:  # noqa: BLE001
            if v2._auth_error(exc): raise RuntimeError('Kite login needed (access token expired)') from exc
            log.warning('v136 baseline %s: %s', sym, exc)
        time.sleep(0.4)
    return seed_volume_baseline(frames)


_keeper: dict = {'started': False, 'last': None, 'result': None}


def _keeper_loop(get_kite):
    """Weekdays 08:00-09:40 and 15:40-18:00 IST: if the volume baseline is shorter than the hierarchy needs, fill it from Kite (once per window)."""
    while True:
        try:
            now = d.now_ist(); hm = now.hour * 60 + now.minute; today = str(now.date())
            win = 'am' if 480 <= hm <= 580 else ('pm' if 940 <= hm <= 1080 else None)
            if now.weekday() < 5 and win and _keeper.get('last') != f'{today}:{win}':
                have, need = baseline_short(today)
                if have >= need:
                    _keeper['last'] = f'{today}:{win}'; _keeper['ok'] = {'days': have, 'need': need, 'at': str(now)[:16]}
                else:
                    kite = get_kite()
                    if kite is not None:
                        n = seed_recent_baseline(kite, today=now.date())
                        _keeper.update(last=f'{today}:{win}', result={'at': str(now)[:19], 'stocks': n, 'had_days': have, 'now_days': baseline_short(today)[0]})
                        log.info('v136 baseline seeded: %s', _keeper['result'])
        except Exception as exc:  # noqa: BLE001
            log.warning('v136 baseline keeper: %s', exc); _keeper['result'] = {'error': str(exc)[:200], 'at': str(d.now_ist())[:19]}
        time.sleep(300)


def keeper_status() -> dict:
    have, need = baseline_short()
    return {'days': have, 'need': need, 'last_fill': _keeper.get('result'), 'running': _keeper.get('started')}


def start_baseline_keeper(get_kite) -> None:
    with _lock:
        if _keeper['started']: return
        _keeper['started'] = True
    threading.Thread(target=_keeper_loop, args=(get_kite,), daemon=True, name='v136-baseline').start()


def run(kite, start: dt.date = DEFAULT_START, end: dt.date | None = None) -> dict:
    from . import v132_movers as v2
    from . import v135_hierarchy as h
    s = h.load_settings(); end = end or (d.now_ist().date() - dt.timedelta(days=1))
    uni = v2._fo_universe(kite)
    _set_state(state='RUNNING', step='download', done=0, total=len(uni), started=str(d.now_ist())[:19], error=None, start=str(start), end=str(end))
    frames = {}
    for i, (sym, tok) in enumerate(sorted(uni.items())):
        try: frames[sym] = update_cache(kite, sym, tok, start, end)
        except Exception as exc:  # noqa: BLE001
            if v2._auth_error(exc): raise RuntimeError('Kite login needed (access token expired)') from exc
            log.warning('v136 %s: %s', sym, exc); frames[sym] = _load_cache(sym)
        if (i + 1) % 10 == 0: _set_state(done=i + 1, symbol=sym)
    _set_state(step='replay', done=len(uni))
    seeded = seed_volume_baseline(frames)
    P = build_panel(frames, h.industry_map(), s); del frames
    if P.empty: raise RuntimeError('no usable history')
    res = {'at': str(d.now_ist())[:19], 'start': str(min(P.date)), 'end': str(max(P.date)), 'test_from': str(TEST_FROM), 'stocks': int(P.symbol.nunique()),
           'cost_bps': s['cost_bps'], 'grid_per_side': int(np.prod([len(v) for v in GRID.values()])), 'seeded_volume': seeded,
           'sides': evaluate(P, s['cost_bps']), 'market_buckets': market_buckets(P, s['cost_bps']),
           'note': "Today's F&O list (survivorship bias); present-day industry map; 09:45 entry at the 09:40 bar close; cost per trade as in settings."}
    d._write_json(RESULT, res)
    _set_state(state='DONE', step=None, finished=str(d.now_ist())[:19])
    return res


def start(kite, start_day: dt.date = DEFAULT_START, now: dt.datetime | None = None) -> dict:
    """Start the lab in the background. Refused during market hours (it shares Kite's historical-data rate limit with the live desks)."""
    global _thread
    now = now or d.now_ist()
    if now.weekday() < 5 and dt.time(9, 0) <= now.time() <= dt.time(15, 35):
        return {'started': False, 'reason': 'market hours: run it after 15:35 (or before 09:00)'}
    with _lock:
        if _thread and _thread.is_alive(): return {'started': False, 'reason': 'already running', **status()}

        def work():
            try: run(kite, start_day)
            except Exception as exc:  # noqa: BLE001
                log.exception('v136 lab failed'); _set_state(state='ERROR', error=str(exc)[:300], finished=str(d.now_ist())[:19])
        _thread = threading.Thread(target=work, daemon=True, name='v136-hierarchy-lab'); _thread.start()
    return {'started': True}


def status() -> dict:
    st = d._read_json(STATE, {}) or {}
    if st.get('state') == 'RUNNING' and not (_thread and _thread.is_alive()): st['state'] = 'STOPPED'   # restarted mid-run; cache resumes
    return {'state': st, 'result': d._read_json(RESULT, None)}


def verdicts() -> dict:
    """side -> {'status': ACTIVE|SHADOW, 'cfg': chosen config or None} from the last lab result; {} when the lab has not run."""
    r = d._read_json(RESULT, None) or {}
    return {k: {'status': v.get('verdict'), 'cfg': (v.get('best') or {}).get('cfg')} for k, v in (r.get('sides') or {}).items()}
