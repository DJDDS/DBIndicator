"""V13.3 09:25 direction desk (ADMIN ONLY, paper trading - never places orders).

At 09:25 IST every F&O stock gets an UP/DOWN call for the move from 09:25 to 15:15, from a frozen LightGBM model
(read with numpy).  Inputs are only what the scanner can compute live: Kite 5-minute history (daily bars are built
from it exactly as in research), the first two 5-minute candles of today, the cross-section of all F&O stocks at
09:25, INDIA VIX, NSE F&O bhavcopy (futures OI), the F&O ban list, NSE board meetings / overnight announcements
and FII participant OI.

Selection (all settings): every day the model's view of the whole market (mean P over all stocks) is compared with
its last 250 days; on STRONG days (view stronger than the trailing 70th percentile) the 5 most confident stocks are
paper-traded (entry = last price when the list is ready, exit = close of the 15:10-15:15 candle).  On other days the
list is shown as WATCH only.  An order-flow tag (all F&O stocks polled 09:15-09:25: book imbalance, tick-rule
volume delta, move) says whether order flow agrees with each call; it is recorded, not used, until it has been
judged on enough sessions.

Research (walk-forward by year 2018-26, live-feasible inputs): strong-day top-5 right 62.3% (3,020 calls, every year
55-70%), +0.47% per trade after 0.06% costs; DOWN calls are most of them.  Numbers in v133_assets/dir925_meta.json.

Daily cycle (IST, weekdays, fail-soft, idempotent per day):
  07:50-09:10  prep: 5-minute history (incremental cache) -> per-stock history inputs; market history; peers (yearly);
               NSE: F&O bhavcopy OI, ban list, board meetings, participant OI; INDIA VIX; days to expiry
  09:08-15:15  ORDER-FLOW LAB recorder (own thread): full quote of every F&O stock + its near-month future, pre-open every 60 s,
               09:15-09:39 every 10 s, then every 5 min -> of_<date>.jsonl (packed to .jsonl.gz after the close)
  09:25-09:40  list: first two candles of every stock -> features -> model -> day view -> list + paper entries + Telegram;
               every stock's inputs + P(up) -> scores_<date>.json.gz
  15:20-16:30  outcome: candles of the listed stocks -> exit at 15:15, returns at +30/60/90/120 min, best/worst
  15:35-16:40  labels: 5-minute candles of EVERY stock -> bars_<date>.json.gz; order-flow leaderboard -> of_lab.json
Storage: <V12 storage root>/v133/  (aggs.pkl, prep_<date>.json, futs_<date>.json, of_<date>.jsonl[.gz], scores_<date>.json.gz,
         bars_<date>.json.gz, of_lab.json, days.jsonl, dayview.json, settings.json)
"""
from __future__ import annotations

import datetime as dt
import gzip
import io
import json
import logging
import math
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import config
from .v130_gbm import TextGBM

log = logging.getLogger(__name__)
ASSETS = Path(__file__).with_name('v133_assets')
ROOT = Path(config.V12_STORAGE_ROOT) / 'v133'
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
K_EXIT = 71                     # 5-minute bar index of 15:10-15:15 (bar k covers 09:15+5k .. 09:20+5k)
HORIZON_BARS = {'30': 7, '60': 13, '90': 19, '120': 25, 'day': K_EXIT}
COST = 0.0006
DEFAULT_SETTINGS = {'top_n': 3, 'strong_quantile': 0.7, 'window': 250, 'trade_normal_days': False, 'telegram': True,
                    'book_target_pct': 0.75, 'options': True, 'opt_max_spread_pct': 3.0,
                    'hc_quantile': 0.8, 'hc_top_n': 3, 'hc_book_pct': 1.0, 'entry_check': True, 'ec_rsi': [46.6, 71.3], 'ec_stoch': [0.385, 0.943], 'ec_bbz': [-0.475, 2.167], 'ec_ema50': [-0.77, 4.4], 'ec_min_score': 3, 'stop_k': 1.0, 'trail_k': 0.75, 'hc_down_only': True, 'grade_a_conf': 0.14, 'hc_min_trades': 1}

_lock = threading.Lock()
_model = None
_status: dict = {'started': None, 'steps': {}, 'last_error': None}
_real_sleep = time.sleep         # scheduler threads keep the real sleep even if a test patches time.sleep
_of_buf: dict = {}              # date -> list of polls (in memory, flushed to of_<date>.json)


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST).replace(tzinfo=None)


# ================================================================== storage
def _path(name: str) -> Path:
    ROOT.mkdir(parents=True, exist_ok=True)
    return ROOT / name


def _read_json(name: str, default):
    p = _path(name)
    try:
        return json.loads(p.read_text()) if p.exists() else default
    except Exception:  # noqa: BLE001
        log.exception('v133: unreadable %s', name); return default


def _write_json(name: str, obj) -> None:
    p = _path(name); tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(obj, default=str)); tmp.replace(p)


def _read_jsonl(name: str) -> list:
    p = _path(name)
    if not p.exists(): return []
    out = []
    for line in p.read_text().splitlines():
        try: out.append(json.loads(line))
        except Exception: pass  # noqa: BLE001
    return out


def _rewrite_jsonl(name: str, rows: list) -> None:
    p = _path(name); tmp = p.with_suffix('.tmp')
    tmp.write_text(''.join(json.dumps(r, default=str) + '\n' for r in rows)); tmp.replace(p)


def _write_gz(name: str, obj) -> None:
    p = _path(name); tmp = p.with_name(p.name + '.tmp')
    with gzip.open(tmp, 'wt', encoding='utf-8') as fh: json.dump(obj, fh, default=str, separators=(',', ':'))
    tmp.replace(p)


def _read_gz(name: str, default):
    p = _path(name)
    try:
        if not p.exists(): return default
        with gzip.open(p, 'rt', encoding='utf-8') as fh: return json.load(fh)
    except Exception:  # noqa: BLE001
        log.exception('v133: unreadable %s', name); return default


def _read_of(day) -> list:
    """One day's order-flow polls: of_<d>.jsonl (today, appended live), of_<d>.jsonl.gz (packed after the close) or the old of_<d>.json."""
    out = []
    for name, op in ((f'of_{day}.jsonl', open), (f'of_{day}.jsonl.gz', gzip.open)):
        p = _path(name)
        if p.exists():
            try:
                with op(p, 'rt', encoding='utf-8') as fh:
                    for line in fh:
                        try: out.append(json.loads(line))
                        except Exception: pass  # noqa: BLE001
            except Exception:  # noqa: BLE001
                log.exception('v133: unreadable %s', name)
            if out: return out
    return list(_read_json(f'of_{day}.json', []) or [])


HORIZONS = ('30', '60', '90', '120', 'day')
LISTS = ('hc',) + HORIZONS          # 'hc' = DAILY POOL: mean of the 5 horizon models, top 3 every day, exit 15:15 (★ = strong day)
EXIT_LABEL = {'30': '09:55', '60': '10:25', '90': '10:55', '120': '11:25', 'day': '15:15', 'hc': '15:15'}
EXIT_BAR = {**HORIZON_BARS, 'hc': K_EXIT}


def meta() -> dict:
    return json.loads((ASSETS / 'dir925_meta.json').read_text())


def model(h: str = 'day') -> TextGBM:
    global _model
    if _model is None: _model = {}
    if h not in _model:
        m = TextGBM(ASSETS / meta()['horizons'][h]['model'])
        if m.feature_names != meta()['features']:
            raise RuntimeError(f'v133 model {h} / meta feature mismatch')
        _model[h] = m
    return _model[h]


def predict_up(X: pd.DataFrame, h: str = 'day') -> np.ndarray:
    raw = model(h).predict(X[meta()['features']].astype(float).to_numpy())
    return 1.0 / (1.0 + np.exp(-raw))


def load_settings() -> dict:
    s = {**DEFAULT_SETTINGS, **(_read_json('settings.json', {}) or {})}
    s['top_n'] = int(max(1, min(50, int(s['top_n']))))
    s['strong_quantile'] = float(min(0.95, max(0.0, float(s['strong_quantile']))))
    s['window'] = int(max(60, int(s['window'])))
    s['trade_normal_days'] = bool(s['trade_normal_days']); s['telegram'] = bool(s['telegram']); s['options'] = bool(s['options'])
    s['book_target_pct'] = float(min(5.0, max(0.1, float(s['book_target_pct']))))
    s['opt_max_spread_pct'] = float(min(20.0, max(0.5, float(s['opt_max_spread_pct']))))
    s['hc_quantile'] = float(min(0.97, max(0.5, float(s['hc_quantile'])))); s['hc_top_n'] = int(max(1, min(5, int(s['hc_top_n']))))
    s['hc_book_pct'] = float(min(5.0, max(0.1, float(s['hc_book_pct']))))
    s['stop_k'] = float(min(5.0, max(0.0, float(s['stop_k'])))); s['trail_k'] = float(min(5.0, max(0.0, float(s['trail_k'])))); s['hc_down_only'] = bool(s['hc_down_only']); s['grade_a_conf'] = float(min(0.5, max(0.0, float(s['grade_a_conf'])))); s['hc_min_trades'] = int(max(1, min(5, int(s['hc_min_trades'])))); s['hc_min_trades'] = int(max(1, min(5, int(s['hc_min_trades'])))); s['grade_a_conf'] = float(min(0.5, max(0.0, float(s['grade_a_conf']))))
    s['entry_check'] = bool(s['entry_check']); s['ec_min_score'] = int(max(1, min(4, int(s['ec_min_score']))))
    for k in ('ec_rsi', 'ec_stoch', 'ec_bbz', 'ec_ema50'):
        try: lo, hi = (float(x) for x in s[k]); s[k] = [min(lo, hi), max(lo, hi)]
        except Exception: s[k] = list(DEFAULT_SETTINGS[k])  # noqa: BLE001
    return s


def save_settings(new: dict) -> dict:
    cur = _read_json('settings.json', {}) or {}
    for k, v in (new or {}).items():
        if k in DEFAULT_SETTINGS: cur[k] = v
    _write_json('settings.json', cur)
    return load_settings()


# ================================================================== pure feature engine (research-identical)
def day_aggregates(bars: pd.DataFrame) -> pd.DataFrame:
    """bars: 5-minute candles of ONE stock, columns date (naive IST datetime), open, high, low, close, volume.
    One row per session with everything the history inputs need (same as research build.py / h925_build.py)."""
    if bars is None or bars.empty: return pd.DataFrame()
    b = bars.copy(); b['date'] = pd.to_datetime(b['date'])
    m = b.date.dt.hour * 60 + b.date.dt.minute
    b['k'] = ((m - 555) // 5).astype(int); b['d'] = b.date.dt.normalize()
    b = b[(b.k >= 0) & (b.k <= 74)].drop_duplicates(['d', 'k'], keep='last')
    rows = []
    for d, g in b.groupby('d', sort=True):
        A = np.full((5, 75), np.nan)
        for i, f in enumerate(('open', 'high', 'low', 'close', 'volume')):
            A[i, g.k.to_numpy()] = g[f].to_numpy(float)
        O, H, L, C, V = A
        if np.isnan(O[0]) or np.isnan(C[1]): continue
        Cf = pd.Series(C).ffill().to_numpy()
        o = O[0]; p = Cf[1]
        r = {'date': d.date(), 'o': o, 'h': np.nanmax(H), 'l': np.nanmin(L), 'c': C[~np.isnan(C)][-1], 'v': np.nansum(V),
             'p925': p, 'v925': np.nansum(V[:2]), 'mv925': math.log(p / o)}
        for hz, kx in HORIZON_BARS.items():
            r[f'r_{hz}'] = math.log(Cf[kx] / p) if np.isfinite(Cf[kx]) else np.nan
        rows.append(r)
    return pd.DataFrame(rows)


def today_state(bar0: dict, bar1: dict) -> dict | None:
    """Intraday 09:25 values from the 09:15-09:20 (bar0) and 09:20-09:25 (bar1) candles: dicts open/high/low/close/volume."""
    try:
        o = float(bar0['open']); c0, h0, l0, v0 = (float(bar0[k]) for k in ('close', 'high', 'low', 'volume'))
        c1, h1, l1, v1 = (float(bar1[k]) for k in ('close', 'high', 'low', 'volume'))
    except (TypeError, KeyError, ValueError):
        return None
    if min(o, c0, h0, l0, c1, h1, l1) <= 0: return None
    hT, lT = max(h0, h1), min(l0, l1)
    tp = ((h0 + l0 + c0) / 3 * v0 + (h1 + l1 + c1) / 3 * v1); vv = v0 + v1
    return {'o': o, 'p925': c1, 'mv925': math.log(c1 / o), 'hi925': math.log(hT / o), 'lo925': math.log(lT / o),
            'pos925': (c1 - lT) / (hT - lT) if hT > lT else np.nan, 'b0': math.log(c0 / o), 'b1': math.log(c1 / c0),
            'b0_rng': math.log(h0 / l0), 'b1_rng': math.log(h1 / l1), 'v925': vv, 'v_b1b0': math.log((v1 + 1) / (v0 + 1)),
            'vw925': math.log(c1 / (tp / vv)) if vv > 0 else np.nan}


def _mean_last(x: np.ndarray, w: int, mp: int) -> float:
    x = x[-w:]; x = x[~np.isnan(x)]
    return float(np.mean(x)) if len(x) >= mp else np.nan


def _ext_last(x: np.ndarray, w: int, mp: int, f) -> float:
    x = x[-w:]; x = x[~np.isnan(x)]
    return float(f(x)) if len(x) >= mp else np.nan


def _entry_indicators(o, h, l, c) -> dict:
    """Daily indicators as of the last completed session (research: entry-quality study on 7,200 DOWN picks, 2017-26):
    RSI14 (Wilder), position in the 20-day high/low range, Bollinger z (20, sample std), 50-day EMA and 14-day ATR."""
    out = {'ec_rsi': None, 'ec_stoch': None, 'ec_bbz': None, 'ec_ema50': None, 'ec_atr14': None}
    n = len(c)
    if n < 60: return out
    try:
        s = pd.Series(c); d = s.diff()
        up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]; dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
        out['ec_rsi'] = float(100 - 100 / (1 + up / dn)) if dn > 0 else 100.0
        hi20, lo20 = float(np.max(h[-20:])), float(np.min(l[-20:]))
        out['ec_stoch'] = float((c[-1] - lo20) / (hi20 - lo20)) if hi20 > lo20 else None
        w = c[-20:]; sd = float(np.std(w, ddof=1))
        out['ec_bbz'] = float((c[-1] - np.mean(w)) / sd) if sd > 0 else None
        out['ec_ema50'] = float(s.ewm(span=50, adjust=False).mean().iloc[-1])
        tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
        out['ec_atr14'] = float(np.mean(tr[-14:]))
    except Exception:  # noqa: BLE001
        return {k: None for k in out}
    return out


def entry_check(hp: dict | None, px: float | None, side: str, s: dict) -> dict:
    """Advisory entry-quality tag for a DOWN pick: how many of 4 'not extreme' conditions hold (RSI14, 20-day range position, Bollinger z,
    distance from the 50-day EMA in ATR units). Validated for DOWN picks only; extremes at either end earned ~0 (walk-forward 2019-26:
    score>=3 -> +13.7 bps/pick vs +5.4 for all picks). Advisory only: it never changes which stocks are picked."""
    if not s.get('entry_check', True): return {'label': 'off'}
    if side != 'DOWN': return {'label': 'n/a', 'note': 'validated for DOWN picks only'}
    if not hp or not px or hp.get('ec_rsi') is None or not hp.get('ec_atr14') or not hp.get('pc'): return {'label': 'n/a', 'note': 'indicators not available'}
    atr_rel = hp['ec_atr14'] / hp['pc']
    v = {'rsi': hp['ec_rsi'], 'stoch': hp['ec_stoch'], 'bbz': hp['ec_bbz'], 'ema50': (px / hp['ec_ema50'] - 1) / atr_rel if hp.get('ec_ema50') else None}
    rng = {'rsi': s['ec_rsi'], 'stoch': s['ec_stoch'], 'bbz': s['ec_bbz'], 'ema50': s['ec_ema50']}
    ok = {k: (v[k] is not None and rng[k][0] <= v[k] <= rng[k][1]) for k in v}
    n = sum(ok.values()); label = 'GOOD' if n >= s['ec_min_score'] else ('BORDERLINE' if n == s['ec_min_score'] - 1 else 'WEAK')
    return {'label': label, 'score': n, 'of': 4, 'ok': ok, 'values': {k: (None if x is None else round(float(x), 2)) for k, x in v.items()}}


def stock_history(A: pd.DataFrame) -> dict | None:
    """History inputs from the stock's completed sessions (day_aggregates rows strictly before today, ascending).
    Research: premover_final.py (daily), h925_ds.py (09:25 habits)."""
    A = A.dropna(subset=['o', 'h', 'l', 'c'])
    A = A[(A.o > 0) & (A.h > 0) & (A.l > 0) & (A.c > 0)]
    if len(A) < 25: return None
    o, h, l, c, v = (A[k].to_numpy(float) for k in ('o', 'h', 'l', 'c', 'v'))
    exc = np.maximum(np.log(h / o), np.log(o / l)); rng = np.log(h / l)
    pcs = np.r_[np.nan, c[:-1]]; ret = np.log(c / pcs); gap = np.log(o / pcs)
    pc = c[-1]
    p = {'pc': pc, 'exc5': _mean_last(exc, 5, 3), 'exc20': _mean_last(exc, 20, 10), 'rng20': _mean_last(rng, 20, 10), 'rng1': rng[-1],
         'r1': ret[-1], 'r5': float(np.log(pc / c[-6])), 'r20': float(np.log(pc / c[-21])) if len(c) >= 21 else np.nan}
    vv = np.where(v > 0, v, np.nan); med20 = _ext_last(vv, 20, 10, np.median)
    p['vol1'] = float(np.log(vv[-1] / med20)) if med20 and np.isfinite(vv[-1]) else np.nan
    m5 = _mean_last(vv, 5, 3); p['vol5'] = float(np.log(m5 / med20)) if med20 and np.isfinite(m5) else np.nan
    p['hi52'] = float(np.log(pc / _ext_last(c, 250, 120, np.max))); p['lo52'] = float(np.log(pc / _ext_last(c, 250, 120, np.min)))
    p['hi20'] = float(np.log(pc / _ext_last(h, 20, 10, np.max))); p['lo20'] = float(np.log(pc / _ext_last(l, 20, 10, np.min)))
    m7 = _ext_last(rng, 7, 5, np.min); p['nr7'] = float(rng[-1] <= m7) if np.isfinite(m7) else 0.0
    p['squeeze'] = p['rng1'] / p['rng20'] if p['rng20'] else np.nan
    e = p['exc20']
    # exc20 as it stood on each past day (for the gap-fill habit), then 09:25 habits
    ex20_t = pd.Series(exc).shift(1).rolling(20, min_periods=10).mean().to_numpy()
    oc = A['mv925'].to_numpy(float) + A['r_day'].to_numpy(float) if 'r_day' in A else np.full(len(A), np.nan)
    gf = -np.sign(gap) * oc / ex20_t
    p['oc20'] = _mean_last(oc, 20, 10) / e if e else np.nan
    p['gf60'] = _mean_last(gf, 60, 20)
    for hz in ('30', '60', 'day'):
        col = f'r_{hz}'
        p[f'hab_{hz}'] = _mean_last(A[col].to_numpy(float), 20, 10) / e if (col in A and e) else np.nan
    p['v925_med'] = _ext_last(A['v925'].to_numpy(float), 20, 10, np.median) if 'v925' in A else np.nan
    p.update(_entry_indicators(o, h, l, c))
    p['last_date'] = str(A['date'].iloc[-1])[:10]
    p['day_oc'] = float(oc[-1]) if np.isfinite(oc[-1]) else None; p['day_gap'] = float(gap[-1]) if np.isfinite(gap[-1]) else None
    return {k: (None if isinstance(x, float) and (math.isnan(x) or math.isinf(x)) else x) for k, x in p.items()}


def market_history(per_day: pd.DataFrame) -> dict:
    """per_day: one row per (symbol, date) for the universe with columns date, oc (open->15:15), gap, ret (close/prev close).
    Previous-session market values (research: premover m_ret/m_disp, h925_ds M_r*/M_intra*)."""
    if per_day is None or per_day.empty: return {}
    g = per_day.groupby('date')
    md = pd.DataFrame({'mr': g.oc.mean(), 'mg': g.gap.mean(), 'm_ret': g.ret.mean(), 'm_disp': g.ret.std()}).sort_index()
    md['m_day'] = md.mr + md.mg
    last = md.iloc[-1]
    tail = lambda c, n: float(md[c].iloc[-n:].sum()) if len(md) >= n and md[c].iloc[-n:].notna().all() else None
    f = lambda x: None if x is None or not np.isfinite(x) else float(x)
    return {'M_r1': f(last.m_day), 'M_r5': tail('m_day', 5), 'M_r20': tail('m_day', 20), 'M_intra1': f(last.mr), 'M_intra5': tail('mr', 5),
            'm_ret': f(last.m_ret), 'm_disp': f(last.m_disp), 'session': str(md.index[-1])[:10]}


def features_925(hist: dict, today: dict, peers: dict, ctx: dict, mk: dict, day: dt.date) -> pd.DataFrame:
    """hist: sym -> stock_history(); today: sym -> today_state(); peers: sym -> group id; mk: market_history();
    ctx: dict with per-stock 'oi1','oi5','ban' (sym->(inban, ban_new, ban_exit)), 'bm' (board_flags), 'news' (overnight_news),
    and market 'ivix','divix','fii_idx','fii_stk','fii_didx','fii_dstk','dte'."""
    rows = []
    for s, t in today.items():
        p = hist.get(s)
        if not p or not t or not p.get('pc') or not p.get('exc20') or not p.get('rng20'): continue
        e = p['exc20']
        if not e > 0 or abs(t['mv925']) >= 0.25: continue
        r = {k: p.get(k) for k in ('exc20', 'rng20', 'exc5', 'r1', 'r5', 'r20', 'hi20', 'lo20', 'hi52', 'lo52', 'squeeze', 'vol1', 'vol5', 'nr7',
                                   'oc20', 'gf60', 'hab_30', 'hab_60', 'hab_day')}
        r['symbol'] = s; r['o'] = t['o']; r['p925'] = t['p925']
        r['gap'] = math.log(t['o'] / p['pc'])
        for k in ('mv925', 'hi925', 'lo925', 'vw925', 'b0', 'b1', 'b0_rng', 'b1_rng'):
            r[k + '_e'] = t[k] / e if t.get(k) is not None and np.isfinite(t[k]) else np.nan
        r['mv925'] = t['mv925']; r['b1'] = t['b1']; r['b0'] = t['b0']
        r['pos925'] = t['pos925']; r['v_b1b0'] = t['v_b1b0']
        r['tot925_e'] = (r['gap'] + t['mv925']) / e; r['g_e'] = r['gap'] / e; r['agap_exc'] = abs(r['gap']) / e
        vm = p.get('v925_med'); r['rv925'] = math.log(t['v925'] / vm) if vm and vm > 0 and t['v925'] > 0 else np.nan
        r['oi1'] = (ctx.get('oi1') or {}).get(s); r['oi5'] = (ctx.get('oi5') or {}).get(s)
        ib = (ctx.get('ban') or {}).get(s, (0, 0, 0)); r['inban'], r['ban_new'], r['ban_exit'] = (float(x) for x in ib)
        r.update({k: float(v) for k, v in ((ctx.get('bm') or {}).get(s) or {'bm_today': 0, 'bm_today_res': 0, 'bm_yday': 0, 'bm_yday_res': 0}).items()})
        r['res_overnight'] = float(((ctx.get('news') or {}).get(s) or (0, 0))[1])
        r['peer'] = peers.get(s, -1)
        rows.append(r)
    X = pd.DataFrame(rows)
    if X.empty: return X
    X = X.astype({c: float for c in X.columns if c not in ('symbol',)}, errors='ignore')
    for c in ('ivix', 'divix', 'fii_idx', 'fii_stk', 'fii_didx', 'fii_dstk', 'dte'): X[c] = ctx.get(c)
    for c in ('M_r1', 'M_r5', 'M_r20', 'M_intra1', 'M_intra5', 'm_ret', 'm_disp'): X[c] = mk.get(c)
    X['dow'] = float(day.weekday())
    X['m_gap'] = X.gap.mean(); X['m_agap'] = X.gap.abs().mean()
    X['oib'] = np.sign(X.r1) * X.oi1
    X['M_mv925'] = X.mv925.mean(); X['M_br925'] = (X.mv925 > 0).mean(); X['M_tot925'] = X.tot925_e.mean()
    X['M_b1'] = X.b1.mean(); X['M_b0'] = X.b0.mean(); X['M_vw925'] = X.vw925_e.mean(); X['M_gap_e'] = X.g_e.mean()
    P = X[X.peer >= 0]; g = P.groupby('peer')
    for c, v in (('peer_gap', g.gap.transform('mean')), ('peer_r1', g.r1.transform('mean')), ('P_mv925', g.mv925.transform('mean')), ('P_b1', g.b1.transform('mean'))):
        X[c] = np.nan; X.loc[P.index, c] = v
    X['gap_vs_peer'] = (X.gap - X.peer_gap) / X.rng20
    X['rel925'] = (X.mv925 - X.M_mv925) / X.exc20; X['relp925'] = (X.mv925 - X.P_mv925) / X.exc20
    for c, src in (('cs_mv925', 'mv925_e'), ('cs_tot925', 'tot925_e'), ('cs_gap', 'g_e'), ('cs_rv925', 'rv925'), ('cs_vw925', 'vw925_e')):
        X[c] = X[src].rank(pct=True)
    return X.replace([np.inf, -np.inf], np.nan)


def day_view_threshold(hist: list, quantile: float, window: int) -> float | None:
    """Trailing strength bar for the day view: quantile of |mean P - 0.5| over the last `window` days (needs >= 60)."""
    h = [abs(x - 0.5) for x in hist[-window:] if isinstance(x, (int, float))]
    return float(np.quantile(h, quantile)) if len(h) >= 60 else None


def grade_picks(picks: list, settings: dict) -> None:
    """Pick of the day = the #1 DOWN pick, every day (2019-26 research: 61.9% held to 15:15, +51 bps; edge is gone by day 2-3, so intraday only).
    Grade A = confident AND entry check passes (~67% win, about 1 day in 4). Trades per day = max(hc_min_trades, number of A picks); the rest are WATCH."""
    for i, p in enumerate(picks):
        sc = (p.get('entry_check') or {}).get('score')
        p['pod'] = i == 0
        p['grade'] = 'A' if (p['conf'] >= float(settings.get('grade_a_conf', 0.14)) and (sc is None or sc >= int(settings.get('ec_min_score', 3)))) else 'B'
    n = max(int(settings.get('hc_min_trades', 1)), sum(p['grade'] == 'A' for p in picks))
    for i, p in enumerate(picks): p['status'] = 'OPEN' if i < n else 'WATCH'


def select(X: pd.DataFrame, p_up: np.ndarray, hist: list, settings: dict) -> dict:
    """Day view, strong/normal flag and the ranked list (most confident first)."""
    X = X.assign(p_up=p_up); X['conf'] = (X.p_up - 0.5).abs(); X['side'] = np.where(X.p_up > 0.5, 'UP', 'DOWN')
    view = float(X.p_up.mean()); thr = day_view_threshold(hist, settings['strong_quantile'], settings['window'])
    strong = thr is not None and abs(view - 0.5) > thr
    only = settings.get('only_side')       # research: only DOWN picks carry the edge (2019-26: DOWN 59-60% / +31..36 bps, UP no validated edge)
    L = (X[X.side == only] if only in ('UP', 'DOWN') else X).sort_values('conf', ascending=False).head(int(settings['top_n']))
    return {'view': view, 'view_strength': abs(view - 0.5), 'threshold': thr, 'strong': bool(strong),
            'market_lean': 'DOWN' if view < 0.5 else 'UP', 'list': L, 'n_scored': int(len(X))}


# ================================================================== order-flow score (all F&O stocks, 09:15-09:25)
def of_scores(polls: list, opens: dict) -> dict:
    """polls: [{'t': iso, 'q': {sym: [ltp, volume, buy_qty, sell_qty]}}] in time order. Pre-registered formula (not fitted):
    OFS = z(avg total-book imbalance) + z(tick-rule volume delta ratio) + 0.5*z(move open->last poll), z across stocks."""
    if not polls: return {}
    acc: dict = {}
    prev: dict = {}
    for pl in polls:
        for s, v in (pl.get('q') or {}).items():
            try: ltp, vol, bq, sq = (float(x) if x is not None else np.nan for x in list(v)[:4])
            except (TypeError, ValueError): continue
            a = acc.setdefault(s, {'imb': [], 'buy': 0.0, 'sell': 0.0, 'last': np.nan})
            if np.isfinite(bq) and np.isfinite(sq) and bq + sq > 0: a['imb'].append((bq - sq) / (bq + sq))
            if s in prev and np.isfinite(vol) and np.isfinite(prev[s][1]) and vol > prev[s][1]:
                dv = vol - prev[s][1]; dp = ltp - prev[s][0]
                if dp > 0: a['buy'] += dv
                elif dp < 0: a['sell'] += dv
            if np.isfinite(ltp): a['last'] = ltp
            prev[s] = (ltp, vol)
    rows = []
    for s, a in acc.items():
        o = opens.get(s)
        rows.append({'symbol': s, 'imb': float(np.mean(a['imb'])) if a['imb'] else np.nan,
                     'dr': (a['buy'] - a['sell']) / (a['buy'] + a['sell']) if a['buy'] + a['sell'] > 0 else np.nan,
                     'mv': math.log(a['last'] / o) if o and np.isfinite(a['last']) and a['last'] > 0 else np.nan})
    F = pd.DataFrame(rows)
    if F.empty: return {}
    z = lambda x: ((x - x.mean()) / x.std()).fillna(0) if x.std() and np.isfinite(x.std()) and x.std() > 0 else x * 0
    F['ofs'] = z(F.imb) + z(F.dr) + 0.5 * z(F.mv)
    return {r.symbol: {'ofs': round(float(r.ofs), 3), 'imb': None if np.isnan(r.imb) else round(float(r.imb), 3),
                       'dr': None if np.isnan(r.dr) else round(float(r.dr), 3)} for r in F.itertuples()}


# ================================================================== ORDER-FLOW LAB (pure part)
# Every poll stores, per stock (NSE) and its near-month future (NFO), one compact row in SNAP_COLS order.
SNAP_COLS = ('ltp', 'vol', 'bq', 'sq', 'vwap', 'lq', 'oi', 'bid', 'ask', 'bq5', 'aq5', 'bo5', 'ao5', 'open')
OF_PRE = (548, 555)       # 09:08-09:14  every 60 s  (pre-open book after the 09:08 match)
OF_FAST = (555, 580)      # 09:15-09:39  every 10 s
OF_SLOW = (580, 916)      # 09:40-15:15  every 5 min
LAB_FEATURES = {          # sign convention: positive value = order flow says UP
    'ofs': 'pre-registered score: z(book imbalance) + z(tick-rule delta) + 0.5 z(move)',
    'imb_avg': 'total bid qty vs ask qty, average 09:15-09:25', 'imb_last': 'total bid vs ask qty at 09:25',
    'imb_chg': 'change in total book imbalance 09:15 -> 09:25', 'd5_avg': 'top-5 depth qty imbalance, average',
    'ord5_avg': 'top-5 depth ORDER-COUNT imbalance (many small vs few large)', 'big_side': 'qty-per-order: bids heavier than asks (big players)',
    'dr': 'tick-rule volume delta (buy vs sell initiated)', 'vwap_gap': 'price vs today VWAP at 09:25', 'mv': 'move open -> 09:25',
    'pre_imb': 'pre-open (09:08-09:14) bid vs ask qty', 'fut_imb': 'near-month FUTURES book imbalance, average',
    'fut_dr': 'futures tick-rule volume delta', 'buildup': 'futures OI change x price direction (+ long build-up / short covering, - short build-up / long unwinding)',
    'basis_chg': 'futures premium change 09:15 -> 09:25 (bps)'}


def futures_map(nfo: list, names: set, today: dt.date) -> dict:
    """{sym: near-month futures tradingsymbol} (nearest FUT expiry on/after today)."""
    by: dict = {}
    for i in nfo or []:
        if i.get('instrument_type') != 'FUT' or i.get('name') not in names or not str(i.get('segment', 'NFO-FUT')).startswith('NFO'): continue
        e = i.get('expiry'); e = e if isinstance(e, dt.date) else pd.Timestamp(e).date()
        if e < today: continue
        if i['name'] not in by or e < by[i['name']][0]: by[i['name']] = (e, i['tradingsymbol'])
    return {s: ts for s, (e, ts) in by.items()}


def snap(v: dict) -> list:
    """Kite full quote -> compact row (SNAP_COLS)."""
    d = v.get('depth') or {}; b = d.get('buy') or []; a = d.get('sell') or []
    g = lambda L, k: sum(float(x.get(k) or 0) for x in L[:5])
    top = lambda L: (L[0].get('price') or None) if L else None
    r = lambda x: None if x is None else (round(float(x), 2) if isinstance(x, float) else x)
    return [r(v.get('last_price')), v.get('volume'), v.get('buy_quantity'), v.get('sell_quantity'), r(v.get('average_price')),
            v.get('last_quantity'), v.get('oi'), top(b), top(a), g(b, 'quantity'), g(a, 'quantity'), g(b, 'orders'), g(a, 'orders'),
            r((v.get('ohlc') or {}).get('open'))]


def _num(x) -> float:
    try: return float(x) if x is not None else np.nan
    except (TypeError, ValueError): return np.nan


def _ratio(a, b) -> float:
    a, b = _num(a), _num(b)
    return (a - b) / (a + b) if np.isfinite(a) and np.isfinite(b) and a + b > 0 else np.nan


def _tick_delta(rows: list) -> float:
    buy = sell = 0.0; prev = None
    for r in rows:
        p, v = _num(r[0]), _num(r[1])
        if prev is not None and np.isfinite(v) and np.isfinite(prev[1]) and v > prev[1] and np.isfinite(p) and np.isfinite(prev[0]):
            if p > prev[0]: buy += v - prev[1]
            elif p < prev[0]: sell += v - prev[1]
        prev = (p, v)
    return (buy - sell) / (buy + sell) if buy + sell > 0 else np.nan


def of_features(polls: list, cutoff: str = '09:25:00') -> pd.DataFrame:
    """All order-flow lab features per stock from one day's polls up to `cutoff` (positive = UP). polls: [{'t','ph','q','f'}]."""
    fast = [p for p in polls if p.get('ph', 'fast') == 'fast' and str(p.get('t', ''))[11:19] <= cutoff]
    pre = [p for p in polls if p.get('ph') == 'pre']
    if not fast: return pd.DataFrame()
    S: dict = {}; Fu: dict = {}
    for p in fast:
        for s, row in (p.get('q') or {}).items(): S.setdefault(s, []).append(row)
        for s, row in (p.get('f') or {}).items(): Fu.setdefault(s, []).append(row)
    pre_last = {}
    for p in pre:
        for s, row in (p.get('q') or {}).items(): pre_last[s] = row
    out = []
    for s, rows in S.items():
        rows = [list(r) + [None] * (len(SNAP_COLS) - len(r)) for r in rows]
        imb = [_ratio(r[2], r[3]) for r in rows]; imb = [x for x in imb if np.isfinite(x)]
        d5 = [_ratio(r[9], r[10]) for r in rows]; d5 = [x for x in d5 if np.isfinite(x)]
        o5 = [_ratio(r[11], r[12]) for r in rows]; o5 = [x for x in o5 if np.isfinite(x)]
        qpo = [(_num(r[9]) / _num(r[11]), _num(r[10]) / _num(r[12])) for r in rows if _num(r[11]) > 0 and _num(r[12]) > 0]
        last = rows[-1]; ltp = _num(last[0]); op = _num(last[13]); vw = _num(last[4])
        f = {'symbol': s, 'imb_avg': np.mean(imb) if imb else np.nan, 'imb_last': imb[-1] if imb else np.nan,
             'imb_chg': imb[-1] - imb[0] if len(imb) > 1 else np.nan, 'd5_avg': np.mean(d5) if d5 else np.nan,
             'ord5_avg': -np.mean(o5) if o5 else np.nan,          # more ASK orders for the same qty -> small sellers / big buyers
             'big_side': np.mean([math.log(a / b) for a, b in qpo if a > 0 and b > 0]) if qpo else np.nan,
             'dr': _tick_delta(rows), 'vwap_gap': math.log(ltp / vw) if ltp > 0 and vw > 0 else np.nan,
             'mv': math.log(ltp / op) if ltp > 0 and op > 0 else np.nan,
             'pre_imb': _ratio(pre_last[s][2], pre_last[s][3]) if s in pre_last and len(pre_last[s]) > 3 else np.nan}
        fr = Fu.get(s) or []
        if fr:
            fr = [list(r) + [None] * (len(SNAP_COLS) - len(r)) for r in fr]
            fi = [_ratio(r[2], r[3]) for r in fr]; fi = [x for x in fi if np.isfinite(x)]
            f['fut_imb'] = np.mean(fi) if fi else np.nan; f['fut_dr'] = _tick_delta(fr)
            o0, o1 = _num(fr[0][6]), _num(fr[-1][6]); p0, p1 = _num(fr[0][0]), _num(fr[-1][0])
            f['buildup'] = math.log(o1 / o0) * np.sign(p1 - p0) if o0 > 0 and o1 > 0 and p0 > 0 and p1 > 0 else np.nan
            s0, s1 = _num(rows[0][0]), ltp
            f['basis_chg'] = (math.log(p1 / s1) - math.log(p0 / s0)) * 1e4 if min(p0, p1, s0, s1) > 0 else np.nan
        out.append(f)
    F = pd.DataFrame(out)
    for c in LAB_FEATURES:
        if c not in F: F[c] = np.nan
    z = lambda x: ((x - x.mean()) / x.std()).fillna(0) if x.notna().sum() > 2 and x.std() > 0 else x * 0
    F['ofs'] = z(F.imb_avg) + z(F.dr) + 0.5 * z(F.mv)
    return F


def lab_outcomes(bars: dict) -> pd.DataFrame:
    """bars: {sym: [[k, o, h, l, c, v], ...]} -> log return from the 09:25 price (close of candle 1) to each horizon's exit candle."""
    rows = []
    for s, B in bars.items():
        C = pd.Series(np.nan, index=range(75))
        for r in B:
            if 0 <= int(r[0]) <= 74: C[int(r[0])] = r[4]
        C = C.ffill()
        if not (np.isfinite(C[1]) and C[1] > 0): continue
        rows.append({'symbol': s, **{f'r_{h}': math.log(C[k] / C[1]) if np.isfinite(C[k]) and C[k] > 0 else np.nan for h, k in HORIZON_BARS.items()}})
    return pd.DataFrame(rows)


def lab_leaderboard(daily: list, top_k: int = 5) -> list:
    """daily: [(date, features DataFrame, outcomes DataFrame)]. Per feature x horizon: mean daily rank-IC with t-stat, and the hit rate of
    calling UP on each day's top_k stocks by the feature and DOWN on its bottom_k (direction right at the horizon's exit)."""
    out = []
    for f in LAB_FEATURES:
        for h in HORIZON_BARS:
            ics, hits = [], []
            for _, F, O in daily:
                if f not in F or F[f].notna().sum() < 3 * top_k: continue
                J = F[['symbol', f]].merge(O[['symbol', f'r_{h}']], on='symbol').dropna()
                if len(J) < 3 * top_k or J[f].nunique() < 2 or J[f'r_{h}'].nunique() < 2: continue
                ic = J[f].rank().corr(J[f'r_{h}'].rank())
                if np.isfinite(ic): ics.append(ic)
                J = J.sort_values(f); r = J[f'r_{h}']
                hits += list(r.tail(top_k) > 0) + list(r.head(top_k) < 0)
            if not ics: continue
            sd = float(np.std(ics, ddof=1)) if len(ics) > 1 else np.nan
            out.append({'feature': f, 'h': h, 'days': len(ics), 'ic': round(float(np.mean(ics)), 4),
                        't': round(float(np.mean(ics)) / sd * math.sqrt(len(ics)), 2) if sd and np.isfinite(sd) and sd > 0 else None,
                        'hit': round(100 * float(np.mean(hits)), 1) if hits else None, 'calls': len(hits)})
    return out


# ================================================================== NSE parsers
def parse_participant_oi(raw: bytes) -> dict | None:
    """NSE fao_participant_oi_DDMMYYYY.csv -> {'fii_idx', 'fii_stk'} (net long share of FII index / stock futures)."""
    try:
        lines = raw.decode('utf-8', 'ignore').splitlines()
        h = next(i for i, ln in enumerate(lines) if 'Client Type' in ln)
        df = pd.read_csv(io.StringIO('\n'.join(lines[h:])))
        df.columns = [c.strip() for c in df.columns]
        r = df[df['Client Type'].astype(str).str.strip().str.upper() == 'FII'].iloc[0]
        num = lambda k: float(str(r[k]).replace(',', '').strip())
        il, is_, sl, ss = num('Future Index Long'), num('Future Index Short'), num('Future Stock Long'), num('Future Stock Short')
        return {'fii_idx': (il - is_) / (il + is_), 'fii_stk': (sl - ss) / (sl + ss)}
    except Exception:  # noqa: BLE001
        return None


def days_to_expiry(expiries: list, prev_session: dt.date) -> float | None:
    """Research (chain_build.py): calendar days from the previous session to the nearest stock-option expiry >= 3 days away."""
    ok = sorted(e for e in expiries if (e - prev_session).days >= 3)
    return float((ok[0] - prev_session).days) if ok else None


def oi_changes(fut_oi_by_day: list) -> tuple[dict, dict]:
    """fut_oi_by_day: list of {sym: total futures OI} for consecutive sessions, oldest first, ending with the previous session.
    Research (premover): oi1 = log((OI_t+1)/(OI_t-1 +1)), oi5 vs 5 sessions earlier."""
    oi1, oi5 = {}, {}
    if len(fut_oi_by_day) >= 2:
        a, b = fut_oi_by_day[-1], fut_oi_by_day[-2]
        oi1 = {s: math.log((a[s] + 1) / (b[s] + 1)) for s in a if s in b}
    if len(fut_oi_by_day) >= 6:
        a, b = fut_oi_by_day[-1], fut_oi_by_day[-6]
        oi5 = {s: math.log((a[s] + 1) / (b[s] + 1)) for s in a if s in b}
    return oi1, oi5


# ================================================================== Kite / NSE I/O
def _kite_5min(kite, token: int, a: dt.datetime, b: dt.datetime, tries: int = 3) -> pd.DataFrame:
    from .v132_movers import _auth_error
    for attempt in range(tries):
        try:
            raw = kite.historical_data(token, a, b, '5minute'); break
        except Exception as exc:  # noqa: BLE001
            if attempt == tries - 1 or _auth_error(exc): raise
            time.sleep(1.5 * (attempt + 1))
    df = pd.DataFrame(raw)
    if df.empty: return df
    df['date'] = pd.to_datetime(df['date'])
    if getattr(df['date'].dt, 'tz', None) is not None:
        df['date'] = df['date'].dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
    return df[['date', 'open', 'high', 'low', 'close', 'volume']]


AGGS = 'aggs.pkl'      # pandas pickle: no extra library (the server has no pyarrow; parquet failed live on 12-Oct-2026)


def _load_aggs() -> pd.DataFrame:
    p = _path(AGGS)
    try:
        return pd.read_pickle(p) if p.exists() else pd.DataFrame()
    except Exception:  # noqa: BLE001
        log.exception('v133 aggs unreadable'); return pd.DataFrame()


def _save_aggs(parts: list, today: dt.date) -> pd.DataFrame:
    A = pd.concat([x for x in parts if x is not None and not x.empty], ignore_index=True) if parts else pd.DataFrame()
    if A.empty: return A
    A['date'] = pd.to_datetime(A.date).dt.date
    A = A.drop_duplicates(['symbol', 'date'], keep='last')
    cut = today - dt.timedelta(days=430); A = A[A.date >= cut].sort_values(['symbol', 'date']).reset_index(drop=True)
    tmp = _path(AGGS + '.tmp'); A.to_pickle(tmp); tmp.replace(_path(AGGS))
    return A


def update_aggs(kite, uni: dict, today: dt.date, backfill_days: int = 380) -> pd.DataFrame:
    """Incremental cache of per-session aggregates for every F&O stock (Kite 5-minute history, <=100 days per request)."""
    A = _load_aggs(); have = {} if A.empty else A.groupby('symbol').date.max().to_dict()
    out = [A] if not A.empty else []
    for i, (s, tok) in enumerate(sorted(uni.items())):
        last = have.get(s)
        start = (pd.Timestamp(last).date() + dt.timedelta(days=1)) if last is not None else today - dt.timedelta(days=backfill_days)
        if start >= today: continue
        a = start
        while a < today:
            b = min(a + dt.timedelta(days=95), today - dt.timedelta(days=1))
            try:
                bars = _kite_5min(kite, tok, dt.datetime.combine(a, dt.time(9, 15)), dt.datetime.combine(b, dt.time(15, 30)))
            except Exception as exc:  # noqa: BLE001
                from .v132_movers import _auth_error
                if _auth_error(exc): raise RuntimeError('Kite login needed (access token expired)') from exc
                log.warning('v133 5min %s: %s', s, exc); bars = pd.DataFrame()
            time.sleep(0.35)
            ag = day_aggregates(bars)
            if not ag.empty: ag.insert(0, 'symbol', s); out.append(ag)
            a = b + dt.timedelta(days=1)
        if (i + 1) % 25 == 0:                       # checkpoint: a failure or restart mid-backfill keeps what was downloaded
            out = [_save_aggs(out, today)]
    return _save_aggs(out, today)


def _participant(nse, d: dt.date) -> dict | None:
    for host in ('https://nsearchives.nseindia.com/content/nsccl/', 'https://archives.nseindia.com/content/nsccl/'):
        raw = nse.get(f'{host}fao_participant_oi_{d:%d%m%Y}.csv')
        if raw:
            r = parse_participant_oi(raw)
            if r: return r
    return None


def run_prep(kite, today: dt.date | None = None) -> int:
    from . import v132_movers as v2
    today = today or now_ist().date()
    name = f'prep_{today}.json'
    if _read_json(name, None): return 1
    uni = v2._fo_universe(kite)
    A = update_aggs(kite, uni, today)
    if A.empty: raise RuntimeError('no 5-minute history')
    A = A[A.date < today]
    hist, per_day = {}, []
    for s, g in A.groupby('symbol'):
        g = g.sort_values('date')
        p = stock_history(g)
        if p and s in uni:
            p['token'] = uni[s]; hist[s] = p
        c = g.c.to_numpy(float); pcs = np.r_[np.nan, c[:-1]]
        per_day.append(pd.DataFrame({'date': g.date.values, 'oc': (g.mv925 + g.r_day).to_numpy(float), 'gap': np.log(g.o.to_numpy(float) / pcs),
                                     'ret': np.log(c / pcs), 'symbol': s}))
    if len(hist) < max(30, 0.7 * len(uni)): raise RuntimeError(f'prep incomplete: {len(hist)} of {len(uni)} stocks')
    PD = pd.concat(per_day); PD = PD[PD.symbol.isin(uni)]
    sessions = sorted(PD.date.unique()); prev = sessions[-1]
    mk = market_history(PD[PD.date >= sessions[max(0, len(sessions) - 25)]])
    # peers (re-clustered once a year from last calendar year's closes)
    peers = _read_json(f'peers_{today.year}.json', None)
    if peers is None:
        W = A[[d.year == today.year - 1 for d in A.date]].pivot_table(index='date', columns='symbol', values='c')
        peers = v2.peer_groups(np.log(W / W.shift(1))) if len(W) > 150 else (v2._read_json(f'peers_{today.year}.json', None) or {})
        _write_json(f'peers_{today.year}.json', peers)
    nse = v2._nse()
    fo = []
    for d in sessions[-6:]:
        try:
            f = nse.fo_day(d)
        except Exception as exc:  # noqa: BLE001
            log.warning('v133 fo bhav %s: %s', d, exc); f = None
        fo.append({} if f is None else {r.sym: float(r.fut_oi) for r in f.itertuples() if np.isfinite(r.fut_oi)})
    fo = [x for x in fo if x]
    oi1, oi5 = oi_changes(fo)
    b_today = nse.ban_day(today) or set(); b_prev = nse.ban_day(prev) or set()
    ban = {s: (int(s in b_today), int(s in b_today and s not in b_prev), int(s not in b_today and s in b_prev)) for s in uni}
    p1 = _participant(nse, prev); p0 = _participant(nse, sessions[-2]) if len(sessions) > 1 else None
    fii = {}
    if p1:
        fii = {'fii_idx': p1['fii_idx'], 'fii_stk': p1['fii_stk'],
               'fii_didx': (p1['fii_idx'] - p0['fii_idx']) if p0 else None, 'fii_dstk': (p1['fii_stk'] - p0['fii_stk']) if p0 else None}
    ivix = divix = None
    try:
        nse_inst = kite.instruments('NSE'); vt = next((i['instrument_token'] for i in nse_inst if i.get('tradingsymbol') == 'INDIA VIX'), None)
        if vt:
            vd = v2._daily(kite, vt, today - dt.timedelta(days=15), today - dt.timedelta(days=1)); vd = vd[vd.index < today]
            if len(vd) >= 2: ivix = float(vd.close.iloc[-1]); divix = float(math.log(vd.close.iloc[-1] / vd.close.iloc[-2]))
    except Exception as exc:  # noqa: BLE001
        log.warning('v133 vix: %s', exc)
    dte = None
    try:
        nfo = kite.instruments('NFO')
        exps = sorted({i['expiry'] for i in nfo if i.get('instrument_type') == 'CE' and i.get('name') in uni and i.get('expiry')})
        dte = days_to_expiry([e if isinstance(e, dt.date) else pd.Timestamp(e).date() for e in exps], prev)
        chains = option_chain_map(nfo, set(uni), today)
        _write_json(f'chains_{today}.json', chains)
        _write_json(f'futs_{today}.json', futures_map(nfo, set(uni), today))
    except Exception as exc:  # noqa: BLE001
        log.warning('v133 expiry/options: %s', exc)
    bm = v2._board_meetings(today)
    ctx = {'oi1': oi1, 'oi5': oi5, 'ban': ban, 'bm': v2.board_flags(bm or [], today, prev), 'ivix': ivix, 'divix': divix, 'dte': dte, **fii}
    _write_json(name, {'date': str(today), 'prev_session': str(prev), 'built_at': now_ist(), 'hist': hist, 'market': mk, 'peers': peers,
                       'ctx': ctx, 'universe': len(uni), 'missing': {'fii': not p1, 'vix': ivix is None, 'oi': not oi1, 'dte': dte is None}})
    return len(hist)


def _quote_full(kite, syms: list) -> dict:
    out = {}
    for i in range(0, len(syms), 450):
        q = kite.quote(['NSE:' + s for s in syms[i:i + 450]])
        for k, v in (q or {}).items(): out[k.split(':', 1)[1]] = v
    return out


_of_uni: dict = {}


def _of_universe(kite, today: dt.date) -> tuple[list, dict]:
    """Stocks + near-month futures to record. From the morning prep when it exists; otherwise built straight from Kite (F&O
    universe + NFO instruments), so a failed prep never silences the order-flow recorder (12-Oct-2026)."""
    prep = _read_json(f'prep_{today}.json', None)
    if prep and prep.get('hist'):
        futs = _read_json(f'futs_{today}.json', None)
        if futs: return sorted(prep['hist']), futs
    if str(today) not in _of_uni:
        from . import v132_movers as v2
        uni = v2._fo_universe(kite)
        futs = _read_json(f'futs_{today}.json', None) or futures_map(kite.instruments('NFO'), set(uni), today)
        _write_json(f'futs_{today}.json', futs)
        _of_uni[str(today)] = (sorted(prep['hist']) if prep and prep.get('hist') else sorted(uni), futs)
    return _of_uni[str(today)]


def run_of_poll(kite, today: dt.date | None = None, phase: str = 'fast') -> int:
    """One order-flow LAB poll: full quote (book totals, top-5 depth qty + order counts, VWAP, last trade qty) of every F&O stock AND its
    near-month future (OI, futures book) in one Kite call; appended as one line to of_<date>.jsonl (a restart never loses earlier polls).
    phase: 'pre' 09:08-09:14 (pre-open book), 'fast' 09:15-09:39 every 10 s, 'slow' 09:40-15:15 every 5 min."""
    today = today or now_ist().date()
    syms, futs = _of_universe(kite, today)
    keys = ['NSE:' + s for s in syms] + ['NFO:' + futs[s] for s in syms if futs.get(s)]
    raw = {}
    for i in range(0, len(keys), 490):
        raw.update(kite.quote(keys[i:i + 490]) or {})
    inv = {v: s for s, v in futs.items()}
    q, f = {}, {}
    for k, v in raw.items():
        ex, ts = k.split(':', 1)
        if ex == 'NSE': q[ts] = snap(v)
        elif ts in inv: f[inv[ts]] = snap(v)
    pl = {'t': now_ist().isoformat(timespec='seconds'), 'ph': phase, 'q': q, 'f': f}
    with _path(f'of_{today}.jsonl').open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(pl, separators=(',', ':'), default=str) + '\n')
    st = _of_buf.setdefault(str(today), {'polls': 0, 'stocks': 0, 'futures': 0, 'first': None, 'last': None, 'by_phase': {}})
    st['polls'] += 1; st['stocks'] = max(st['stocks'], len(q)); st['futures'] = max(st['futures'], len(f))
    st['first'] = st['first'] or pl['t']; st['last'] = pl['t']; st['by_phase'][phase] = st['by_phase'].get(phase, 0) + 1
    return len(q)


def run_list(kite, today: dt.date | None = None) -> int:
    from . import v132_movers as v2
    today = today or now_ist().date()
    days = _read_jsonl('days.jsonl')
    if any(d.get('date') == str(today) for d in days): return 1
    prep = _read_json(f'prep_{today}.json', None)
    if not prep: raise RuntimeError('prep not ready')
    st = {}
    for s, p in prep['hist'].items():
        try:
            b = _kite_5min(kite, p['token'], dt.datetime.combine(today, dt.time(9, 15)), dt.datetime.combine(today, dt.time(9, 25)))
        except Exception as exc:  # noqa: BLE001
            if v2._auth_error(exc): raise RuntimeError('Kite login needed') from exc
            log.warning('v133 bars %s: %s', s, exc); continue
        time.sleep(0.34)
        if b.empty: continue
        m = b.date.dt.hour * 60 + b.date.dt.minute; b0 = b[m == 555]; b1 = b[m == 560]
        if len(b0) and len(b1):
            t = today_state(b0.iloc[0].to_dict(), b1.iloc[0].to_dict())
            if t: st[s] = t
    if len(st) < max(30, 0.6 * len(prep['hist'])): raise RuntimeError(f'only {len(st)} stocks have both 09:15 and 09:20 candles')
    ann = v2._announcements(dt.date.fromisoformat(prep['prev_session']), today) or []
    ctx = dict(prep['ctx']); ctx['news'] = v2.overnight_news(ann, today)
    ctx['ban'] = {k: tuple(v) for k, v in (ctx.get('ban') or {}).items()}
    X = features_925(prep['hist'], st, prep['peers'], ctx, prep['market'], today)
    s = load_settings(); M = meta()
    hv = _read_json('dayview.json', None) or {}
    of = of_scores([p for p in _read_of(today) if p.get('ph', 'fast') == 'fast' and str(p.get('t', ''))[11:19] <= '09:25:00'],
                   {k: v['o'] for k, v in st.items()})
    sels, preds = {}, {}
    for h in HORIZONS:
        hv.setdefault(h, M['horizons'][h]['day_view_seed'])
        preds[h] = predict_up(X, h)
        sels[h] = select(X, preds[h], hv[h], s)
    try:      # LAB: every stock's 09:25 inputs + the 5 models' P(up), so order flow can later be tested on top of the model
        _write_gz(f'scores_{today}.json.gz', X.assign(**{f'p_{h}': np.round(preds[h], 5) for h in HORIZONS}).round(6).to_dict('split'))
    except Exception as exc:  # noqa: BLE001
        log.warning('v133 scores file: %s', exc)
    # HIGH-CONVICTION: ensemble of the 5 horizon models (research: strongest 20% of days, top 1, hold to 15:15 -> 66% 2018-23 / 66% 2024-26)
    hv.setdefault('hc', M['ensemble']['day_view_seed'])
    sels['hc'] = select(X, np.mean([preds[h] for h in HORIZONS], axis=0), hv['hc'],
                        {**s, 'strong_quantile': s['hc_quantile'], 'top_n': s['hc_top_n'], 'only_side': 'DOWN' if s['hc_down_only'] else None})
    syms = sorted({x for sel in sels.values() for x in sel['list'].symbol})
    q = _quote_full(kite, syms) if syms else {}
    opts: dict = {}
    if s['options']:
        try:
            chains = _read_json(f'chains_{today}.json', {}) or {}
            want = {(r.symbol, r.side) for sel in sels.values() for r in sel['list'].itertuples()}
            cands = {k: option_candidates(chains.get(k[0]), k[1], (q.get(k[0]) or {}).get('last_price') or 0) for k in want}
            keys = sorted({c['ts'] for cs in cands.values() for c in cs})
            oq = {}
            for i in range(0, len(keys), 450):
                for kk, vv in (kite.quote(['NFO:' + x for x in keys[i:i + 450]]) or {}).items(): oq[kk.split(':', 1)[1]] = vv
            for k, cs in cands.items():
                o = choose_option(cs, oq, s['opt_max_spread_pct'])
                if o.get('status') == 'OPEN': o.update(expiry=(chains.get(k[0]) or {}).get('expiry'), lot=(chains.get(k[0]) or {}).get('lot'))
                opts[k] = o
        except Exception as exc:  # noqa: BLE001
            log.warning('v133 options: %s', exc)
    lists = {}
    for h, sel in sels.items():
        trade = True if h == 'hc' else (sel['strong'] or s['trade_normal_days']); picks = []   # the daily pool is paper-traded every day
        for r in sel['list'].itertuples():
            ltp = (q.get(r.symbol) or {}).get('last_price')
            o = of.get(r.symbol); agree = None if not o else bool((o['ofs'] > 0) == (r.side == 'UP'))
            picks.append({'symbol': r.symbol, 'side': r.side, 'p_up': round(float(r.p_up), 4), 'conf': round(float(r.conf), 4), 'open': r.o,
                          'p925': r.p925, 'entry': ltp or r.p925, 'gap_pct': round(r.gap * 100, 2), 'exc20_pct': round(r.exc20 * 100, 2), 'rng20_pct': round(r.rng20 * 100, 2),
                          'of': o, 'of_agree': agree, 'status': 'OPEN' if trade else 'WATCH',
                          'entry_check': entry_check((prep['hist'] or {}).get(r.symbol), r.p925, r.side, s) if h == 'hc' else None,
                          'option': dict(opts[(r.symbol, r.side)]) if (r.symbol, r.side) in opts else None})
        if h == 'hc': grade_picks(picks, s)
        lists[h] = {'exit_at': EXIT_LABEL[h], 'view': round(sel['view'], 4), 'strength': round(sel['view_strength'], 4),
                    'book_pct': s['hc_book_pct'] if h == 'hc' else s['book_target_pct'],
                    'threshold': None if sel['threshold'] is None else round(sel['threshold'], 4), 'strong': sel['strong'],
                    'lean': sel['market_lean'], 'traded': trade, 'picks': picks}
        hv[h] = (hv[h] + [round(sel['view'], 6)])[-600:]
    day = {'date': str(today), 'at': now_ist(), 'n_scored': int(len(X)), 'lists': lists, 'missing': prep.get('missing'), 'settings': s}
    days.append(day); _rewrite_jsonl('days.jsonl', days)
    _write_json('dayview.json', hv)
    if s['telegram']: v2._telegram(telegram_text(day))
    return sum(len(v['picks']) for v in lists.values())


def telegram_text(day: dict) -> str:
    out = [f"V13.3 09:25 direction lists - {day['date']} (paper only, no orders)"]
    for h in LISTS:
        L = (day.get('lists') or {}).get(h)
        if not L: continue
        tag = ('★ STRONG day' if L['strong'] else 'normal day') if h == 'hc' else ('STRONG - paper trades' if L['strong'] else 'normal - watch only')
        name = f"DAILY POOL of {len(L['picks'])} (book +{L.get('book_pct')}% or 15:15)" if h == 'hc' else (h if h == 'day' else h + ' min')
        out.append(f"\n[{name} -> exit {L['exit_at']}] {tag}, lean {L['lean']}")
        out += [f"{i + 1}. {'PICK OF THE DAY ' + p.get('grade', '') + ' - ' if p.get('pod') else ''}{p['symbol']} {'▲ UP' if p['side'] == 'UP' else '▼ DOWN'} @ {p['entry']}"
                + ('' if p['of_agree'] is None else (' OF+' if p['of_agree'] else ' OF-'))
                + (f" entry {p['entry_check']['label']} {p['entry_check']['score']}/4" if (p.get('entry_check') or {}).get('score') is not None else '')
                + (f" | {p['option']['ts']} ask {p['option']['ask']}" if (p.get('option') or {}).get('status') == 'OPEN' else '') for i, p in enumerate(L['picks'])]
    return '\n'.join(out)


def run_outcomes(kite, today: dt.date | None = None) -> int:
    today = today or now_ist().date()
    days = _read_jsonl('days.jsonl'); n = 0
    prep = _read_json(f'prep_{today}.json', None) or {}
    bars_cache: dict = {}
    nifty = None
    try:                                                    # hedge leg: Nifty 50 5-minute bars (one call); a failure only drops the hedged figure
        from . import v131_intraday as v31
        if not v31._NIFTY['token']: v31._market_bps(kite, today, 560, 560)
        if v31._NIFTY['token']:
            nifty = _kite_5min(kite, v31._NIFTY['token'], dt.datetime.combine(today, dt.time(9, 15)), dt.datetime.combine(today, dt.time(15, 30)))
    except Exception as exc:  # noqa: BLE001
        log.warning('v133 market leg unavailable: %s', exc)
    for day in days:
        if day.get('date') != str(today): continue
        for h, L in (day.get('lists') or {}).items():
            for p in L['picks']:
                if p.get('exit') is not None: continue
                tok = ((prep.get('hist') or {}).get(p['symbol']) or {}).get('token')
                if not tok: continue
                if p['symbol'] not in bars_cache:
                    bars_cache[p['symbol']] = _kite_5min(kite, tok, dt.datetime.combine(today, dt.time(9, 15)), dt.datetime.combine(today, dt.time(15, 30)))
                    time.sleep(0.34)
                res = outcome_from_bars(bars_cache[p['symbol']], p['entry'], p['p925'], p['side'], EXIT_BAR[h], book_pct=L.get('book_pct') or day.get('settings', {}).get('book_target_pct'),
                                           stop_pct=(p.get('rng20_pct') or 0) * float((day.get('settings') or {}).get('stop_k', 0) or 0) if h == 'hc' else None,
                                           trail_pct=(p.get('rng20_pct') or 0) * float((day.get('settings') or {}).get('trail_k', 0) or 0) if h == 'hc' else None)
                if res:
                    p.update(res); n += 1
                    mk = market_pct_from_bars(nifty, EXIT_BAR[h]) if nifty is not None else None
                    if mk is not None and 'ret925_pct' in res:
                        sg = 1 if p['side'] == 'UP' else -1
                        p['mkt_pct'] = mk; p['vs_mkt_pct'] = round(res['ret925_pct'] - sg * mk, 3)           # pick's own result after removing Nifty's move
                op = p.get('option') or {}
                if op.get('status') == 'OPEN' and op.get('token'):
                    try:
                        kk = ('opt', op['token'])
                        if kk not in bars_cache:
                            bars_cache[kk] = _kite_5min(kite, op['token'], dt.datetime.combine(today, dt.time(9, 15)), dt.datetime.combine(today, dt.time(15, 30))); time.sleep(0.34)
                        op.update(option_outcome(bars_cache[kk], op, EXIT_BAR[h]) or {})
                    except Exception as exc:  # noqa: BLE001
                        log.warning('v133 option outcome %s: %s', op.get('ts'), exc)
    _rewrite_jsonl('days.jsonl', days)
    if n == 0: raise RuntimeError('no outcomes recorded yet')
    return n


def run_warm(kite, today: dt.date | None = None) -> int:
    """Evening (16:45-21:00): bring the 5-minute history cache up to today, so tomorrow's 07:50 prep only adds one day."""
    from . import v132_movers as v2
    today = today or now_ist().date()
    A = update_aggs(kite, v2._fo_universe(kite), today + dt.timedelta(days=1))
    if A.empty: raise RuntimeError('no 5-minute history yet')
    return int(A.symbol.nunique())


def run_labels(kite, today: dt.date | None = None) -> int:
    """LAB, after the close: today's 5-minute candles of EVERY F&O stock (the answers for every stock, not only the picks), then pack
    today's poll file and refresh the order-flow leaderboard (of_lab.json)."""
    today = today or now_ist().date()
    name = f'bars_{today}.json.gz'
    prep = _read_json(f'prep_{today}.json', None)
    if not prep: raise RuntimeError('no prep for today')
    if not _path(name).exists():
        from .v132_movers import _auth_error
        out = {}
        for s, p in prep['hist'].items():
            try:
                b = _kite_5min(kite, p['token'], dt.datetime.combine(today, dt.time(9, 15)), dt.datetime.combine(today, dt.time(15, 30)))
            except Exception as exc:  # noqa: BLE001
                if _auth_error(exc): raise RuntimeError('Kite login needed') from exc
                log.warning('v133 labels %s: %s', s, exc); continue
            time.sleep(0.34)
            if b.empty: continue
            k = ((b.date.dt.hour * 60 + b.date.dt.minute - 555) // 5).astype(int)
            out[s] = [[int(kk), r.open, r.high, r.low, r.close, int(r.volume)] for kk, r in zip(k, b.itertuples()) if 0 <= kk <= 74]
        if len(out) < max(30, 0.6 * len(prep['hist'])): raise RuntimeError(f'labels: only {len(out)} stocks have candles yet')
        _write_gz(name, out)
    pack_of(today)
    lab = refresh_lab()
    return lab.get('sessions', 0)


def pack_of(day) -> bool:
    """Compress the day's appended poll file (of_<d>.jsonl -> .jsonl.gz, ~5x smaller)."""
    src = _path(f'of_{day}.jsonl'); dst = _path(f'of_{day}.jsonl.gz')
    if not src.exists(): return False
    tmp = dst.with_name(dst.name + '.tmp')
    with src.open('rb') as a, gzip.open(tmp, 'wb') as b: b.write(a.read())
    tmp.replace(dst); src.unlink()
    return True


def lab_days() -> list:
    """Dates that have both order-flow polls and end-of-day labels."""
    ds = sorted({p.name[5:15] for p in ROOT.glob('bars_*.json.gz')}) if ROOT.exists() else []
    return [d for d in ds if any(_path(n).exists() for n in (f'of_{d}.jsonl.gz', f'of_{d}.jsonl', f'of_{d}.json'))]


def refresh_lab() -> dict:
    daily = []
    for d in lab_days():
        F = of_features(_read_of(d)); O = lab_outcomes(_read_gz(f'bars_{d}.json.gz', {}) or {})
        if not F.empty and not O.empty: daily.append((d, F, O))
    rows = lab_leaderboard(daily)
    lab = {'at': now_ist(), 'sessions': len(daily), 'first': daily[0][0] if daily else None, 'last': daily[-1][0] if daily else None,
           'target_sessions': 40, 'features': LAB_FEATURES, 'rows': rows}
    _write_json('of_lab.json', lab)
    return lab


def export_lab_zip() -> Path:
    """One zip (for the research formula fit): every day's polls, labels and 09:25 model scores + the leaderboard."""
    import zipfile
    p = _path('of_lab_export.zip'); tmp = p.with_name(p.name + '.tmp')
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_STORED) as z:
        seen = set()
        for pat in ('of_*.jsonl.gz', 'of_*.jsonl', 'of_*.json', 'bars_*.json.gz', 'scores_*.json.gz', 'futs_*.json', 'days.jsonl'):
            for f in sorted(ROOT.glob(pat)):
                if f.name not in seen and not f.name.endswith(('.zip', '.tmp')): seen.add(f.name); z.write(f, f.name)
    tmp.replace(p)
    return p


def market_pct_from_bars(b: pd.DataFrame, k_exit: int) -> float | None:
    """Nifty move (log %) from the 09:25 price (close of candle 1) to the close of candle k_exit; the hedge leg for a pick's raw result."""
    if b is None or b.empty: return None
    m = b.date.dt.hour * 60 + b.date.dt.minute; k = ((m - 555) // 5).astype(int)
    C = pd.Series(np.nan, index=range(75)); ok = (k >= 0) & (k <= 74); C[k[ok].values] = b.close[ok].values; Cf = C.ffill()
    if not (np.isfinite(Cf[1]) and np.isfinite(Cf[k_exit]) and Cf[1] > 0): return None
    return round(math.log(Cf[k_exit] / Cf[1]) * 100, 3)


def outcome_from_bars(b: pd.DataFrame, entry: float, p925: float, side: str, k_exit: int = K_EXIT, book_pct: float | None = None, stop_pct: float | None = None,
                       trail_pct: float | None = None) -> dict | None:
    """Exit at the close of candle k_exit (research: 09:55 / 10:25 / 10:55 / 11:25 / 15:15); win is judged from the 09:25 price
    exactly as in research, ret_pct is the paper result from the actual entry price after 0.06% costs."""
    if b is None or b.empty: return None
    m = b.date.dt.hour * 60 + b.date.dt.minute; k = ((m - 555) // 5).astype(int)
    C = pd.Series(np.nan, index=range(75)); ok = (k >= 0) & (k <= 74); C[k[ok].values] = b.close[ok].values; Cf = C.ffill()
    if not np.isfinite(Cf[k_exit]): return None
    sg = 1 if side == 'UP' else -1
    win_bars = b[(k >= 2) & (k <= k_exit)]
    out = {'exit': float(Cf[k_exit]), 'ret_pct': round((math.log(Cf[k_exit] / entry) * sg - COST) * 100, 3),
           'ret925_pct': round(math.log(Cf[k_exit] / p925) * sg * 100, 3)}
    out['win'] = bool(out['ret925_pct'] > 0)
    if len(win_bars):
        hi, lo = float(win_bars.high.max()), float(win_bars.low.min())
        out['best_pct'] = round((math.log(hi / entry) if sg > 0 else math.log(entry / lo)) * 100, 3)
        out['worst_pct'] = round((math.log(lo / entry) if sg > 0 else math.log(entry / hi)) * 100, 3)
    if book_pct:
        # profit-booking exit (research, whole-day calls: +0.75% target -> 78.5% win, +0.17%/trade vs hold 61% / +0.47%)
        T = book_pct / 100.0; booked = False
        for hh, ll in zip(win_bars.high.to_numpy(float), win_bars.low.to_numpy(float)):
            fav = (hh / entry - 1) if sg > 0 else (1 - ll / entry)
            if np.isfinite(fav) and fav >= T: booked = True; break
        out['book_pct'] = book_pct; out['booked'] = booked
        out['book_ret_pct'] = round((T - COST) * 100, 3) if booked else out['ret_pct']
        out['book_win'] = bool(out['book_ret_pct'] > 0)
    if stop_pct and stop_pct > 0:
        # paper-only 'hold + stop': hold to the exit, but leave early if the stock goes against the entry by stop_pct (research 2017-26, DOWN picks:
        # hold with a stop of ~1x the 20-day average daily range -> +28.6 bps/pick, Sharpe 2.6, vs +8.1 / 1.2 for booking at +1%).
        # trail_pct (research 2019-26, DOWN picks: stop 1x range + trail 0.75x range from the best price so far -> +35.0 vs +30.7 bps and worst year +11 vs +4):
        # once the trade moves our way the stop follows the best price at trail_pct, never loosening; the stop is checked before the bar's own extreme updates it.
        S = stop_pct / 100.0; Tr = (trail_pct or 0) / 100.0; stopped = False; stop_px = entry * (1 - S) if sg > 0 else entry * (1 + S); best = entry
        for hh, ll in zip(win_bars.high.to_numpy(float), win_bars.low.to_numpy(float)):
            if not (np.isfinite(hh) and np.isfinite(ll)): continue
            if (ll <= stop_px) if sg > 0 else (hh >= stop_px): stopped = True; break
            if Tr > 0:
                if sg > 0: best = max(best, hh); stop_px = max(stop_px, best * (1 - Tr))
                else: best = min(best, ll); stop_px = min(stop_px, best * (1 + Tr))
        out['stop_pct'] = round(stop_pct, 3); out['stopped'] = stopped
        if Tr > 0: out['trail_pct'] = round(trail_pct, 3)
        out['stop_ret_pct'] = round((math.log(stop_px / entry) * sg - COST) * 100, 3) if stopped else out['ret_pct']
        out['stop_win'] = bool(out['stop_ret_pct'] > 0)
    out['status'] = 'CLOSED'
    return out


# ================================================================== option suggestion (CALL for UP, PUT for DOWN) - paper only
def option_chain_map(nfo: list, names: set, today: dt.date, min_days: int = 3) -> dict:
    """{sym: {'expiry', 'lot', 'CE': [[strike, tradingsymbol, token], ...], 'PE': [...]}} for the nearest stock-option expiry at least
    `min_days` calendar days away (no last-days expiry gamma for an intraday idea)."""
    by: dict = {}
    for i in nfo or []:
        if i.get('segment') != 'NFO-OPT' or i.get('name') not in names or i.get('instrument_type') not in ('CE', 'PE'): continue
        e = i.get('expiry'); e = e if isinstance(e, dt.date) else pd.Timestamp(e).date()
        if (e - today).days < min_days: continue
        by.setdefault(i['name'], {}).setdefault(e, []).append(i)
    out = {}
    for s, exps in by.items():
        e = min(exps); rows = exps[e]
        out[s] = {'expiry': str(e), 'lot': int(rows[0].get('lot_size') or 0),
                  'CE': sorted([[float(r['strike']), r['tradingsymbol'], int(r['instrument_token'])] for r in rows if r['instrument_type'] == 'CE']),
                  'PE': sorted([[float(r['strike']), r['tradingsymbol'], int(r['instrument_token'])] for r in rows if r['instrument_type'] == 'PE'])}
    return out


def option_candidates(chain: dict, side: str, spot: float) -> list:
    """ATM and one strike out of the money for the call direction (UP -> CE, DOWN -> PE)."""
    typ = 'CE' if side == 'UP' else 'PE'; rows = (chain or {}).get(typ) or []
    if not rows or not spot: return []
    ks = [r[0] for r in rows]; a = int(np.argmin([abs(k - spot) for k in ks]))
    otm = a + 1 if typ == 'CE' else a - 1
    return [dict(label=lab, strike=rows[i][0], ts=rows[i][1], token=rows[i][2], type=typ) for lab, i in (('ATM', a), ('OTM1', otm)) if 0 <= i < len(rows)]


def choose_option(cands: list, quotes: dict, max_spread_pct: float) -> dict:
    """Best contract = ATM if its two-sided spread is within the limit, else the OTM1 strike; none if neither is tradable."""
    tried = []
    for c in cands:
        q = quotes.get(c['ts']) or {}; d = q.get('depth') or {}
        bid = ((d.get('buy') or [{}])[0] or {}).get('price'); ask = ((d.get('sell') or [{}])[0] or {}).get('price')
        if not (bid and ask and ask >= bid > 0): tried.append(f"{c['label']} no two-sided quote"); continue
        spr = (ask - bid) / ((ask + bid) / 2) * 100
        if spr > max_spread_pct: tried.append(f"{c['label']} spread {spr:.1f}%"); continue
        return {**c, 'bid': bid, 'ask': ask, 'entry': ask, 'spread_pct': round(spr, 2), 'oi': q.get('oi'), 'ltp': q.get('last_price'), 'status': 'OPEN'}
    return {'status': 'NO_OPTION', 'reason': '; '.join(tried) or 'no contract'}


def option_outcome(b: pd.DataFrame, op: dict, k_exit: int) -> dict | None:
    """Paper option exit: close of the option's candle at the exit bar, less half the entry spread (proxy for selling at the bid)."""
    if b is None or b.empty or not op.get('entry'): return None
    m = b.date.dt.hour * 60 + b.date.dt.minute; k = ((m - 555) // 5).astype(int)
    c = b.close[k <= k_exit]
    if c.empty: return None
    px = float(c.iloc[-1]) * (1 - (op.get('spread_pct') or 0) / 200.0)
    return {'exit': round(px, 2), 'ret_pct': round((px / op['entry'] - 1) * 100, 2), 'win': bool(px > op['entry']), 'status': 'CLOSED'}


# ================================================================== scheduler
def _step(name: str, fn, *a) -> bool:
    try:
        r = fn(*a); _status['steps'][name] = {'ok': True, 'at': now_ist(), 'result': r}; return True
    except Exception as exc:  # noqa: BLE001
        log.warning('v133 step %s: %s', name, exc)
        _status['steps'][name] = {'ok': False, 'at': now_ist(), 'error': str(exc)[:300]}; _status['last_error'] = str(exc)[:300]
        return False


def _loop(get_kite):
    done: dict = {}
    while True:
        try:
            now = now_ist(); d = now.date(); hm = now.hour * 60 + now.minute
            if d.weekday() < 5 and 470 <= hm <= 1260:
                kite = get_kite()
                if kite is not None:
                    key = lambda k: f'{d}:{k}'
                    def due(k, a, b, every=1):
                        return a <= hm <= b and not done.get(key(k)) and (hm - a) % every == 0
                    if due('prep', 470, 550, 4): done[key('prep')] = _step('prep', run_prep, kite)
                    if 565 <= hm <= 580 and now.second >= 20 and not done.get(key('list')):
                        done[key('list')] = _step('list', run_list, kite)
                    if due('outcome', 920, 990, 5): done[key('outcome')] = _step('outcome', run_outcomes, kite)
                    if due('labels', 935, 1000, 5): done[key('labels')] = _step('labels', run_labels, kite)
                    if due('warm', 1005, 1260, 15): done[key('warm')] = _step('warm', run_warm, kite)
        except Exception:  # noqa: BLE001
            log.exception('v133 scheduler tick failed')
        _real_sleep(5)


def _of_loop(get_kite):
    """Order-flow LAB recorder on its own thread, so the 09:25 list (about a minute of candle downloads) never pauses the polls."""
    last = {'pre': 0.0, 'fast': 0.0, 'slow': 0.0}
    every = {'pre': 60, 'fast': 10, 'slow': 300}
    while True:
        try:
            now = now_ist(); hm = now.hour * 60 + now.minute
            ph = next((p for p, (a, b) in (('pre', OF_PRE), ('fast', OF_FAST), ('slow', OF_SLOW)) if a <= hm < b), None)
            if now.weekday() < 5 and ph and time.time() - last[ph] >= every[ph]:
                kite = get_kite()
                if kite is not None:
                    last[ph] = time.time(); _step(f'of_poll_{ph}', run_of_poll, kite, None, ph)
                    try:
                        from . import v134_relstrength; v134_relstrength.tick(now)
                    except Exception:  # noqa: BLE001
                        log.exception('v134 relative-strength tick failed')
                    try:
                        from . import v135_hierarchy; v135_hierarchy.tick(now)
                    except Exception:  # noqa: BLE001
                        log.exception('v135 hierarchy tick failed')
                    try:
                        from . import v137_pool_watch; v137_pool_watch.tick(now)
                    except Exception:  # noqa: BLE001
                        log.exception('v137 pool watch tick failed')
        except Exception:  # noqa: BLE001
            log.exception('v133 order-flow tick failed')
        _real_sleep(1)


def start_once(get_kite) -> None:
    with _lock:
        if _status.get('started'): return
        _status['started'] = now_ist()
    threading.Thread(target=_loop, args=(get_kite,), daemon=True, name='v133-direction').start()
    threading.Thread(target=_of_loop, args=(get_kite,), daemon=True, name='v133-orderflow').start()


# ================================================================== status / scorecard
def scorecard(days: list) -> dict:
    def sc(ps):
        w = [p['win'] for p in ps if isinstance(p.get('win'), bool)]
        r = [p['ret_pct'] for p in ps if isinstance(p.get('ret_pct'), (int, float))]
        return {'n': len(w), 'win': round(100 * sum(w) / len(w), 1) if w else None, 'avg_pct': round(float(np.mean(r)), 3) if r else None}
    out = {'days': len(days)}
    for h in LISTS:
        Ls = [d['lists'][h] for d in days if (d.get('lists') or {}).get(h)]
        traded = [p for L in Ls if L.get('traded') for p in L['picks']]
        watch = [p for L in Ls if not L.get('traded') for p in L['picks']]
        bk = [{'win': p.get('book_win'), 'ret_pct': p.get('book_ret_pct')} for p in traded if 'book_win' in p]
        ops = [p['option'] for p in traded if (p.get('option') or {}).get('status') == 'CLOSED']
        out.setdefault('_extra', {})[h] = {'book': sc(bk), 'options': sc(ops)}
        out[h] = {'traded': sc(traded), 'watch': sc(watch), 'down': sc([p for p in traded if p['side'] == 'DOWN']),
                  'up': sc([p for p in traded if p['side'] == 'UP']), 'of_agree': sc([p for p in traded + watch if p.get('of_agree') is True]),
                  'of_against': sc([p for p in traded + watch if p.get('of_agree') is False]), 'strong_days': sum(1 for L in Ls if L.get('strong')),
                  'book': sc(bk), 'options': sc(ops)}
    out.pop('_extra', None)
    # daily pool: how often at least 1 / 2 of the day's picks were right (hold and booked)
    pd_days = [[p for p in d['lists']['hc']['picks'] if isinstance(p.get('win'), bool)] for d in days if (d.get('lists') or {}).get('hc')]
    pd_days = [x for x in pd_days if x]
    f = lambda key, k: round(100 * sum(1 for x in pd_days if sum(bool(p.get(key)) for p in x) >= k) / len(pd_days), 1) if pd_days else None
    out['pool'] = {'days': len(pd_days), 'ge1': f('win', 1), 'ge2': f('win', 2), 'ge1_book': f('book_win', 1), 'ge2_book': f('book_win', 2)}
    return out


def status() -> dict:
    days = _read_jsonl('days.jsonl'); today = str(now_ist().date())
    try:
        m = meta()
    except Exception:  # noqa: BLE001
        m = {}
    return {'started': _status.get('started'), 'steps': _status.get('steps', {}), 'last_error': _status.get('last_error'),
            'today': next((d for d in reversed(days) if d['date'] == today), None), 'recent': list(reversed(days))[:20],
            'score': scorecard(days), 'research': {h: v.get('research', {}) for h, v in (m.get('horizons') or {}).items()},
            'hc_research': (m.get('ensemble') or {}).get('research', {}), 'pool_research': (m.get('ensemble') or {}).get('pool', {}),
            'settings': load_settings(), 'horizons': list(HORIZONS), 'lists': list(LISTS), 'exit_label': EXIT_LABEL, 'of_today': of_status(today),
            'lab': lab_summary(), 'rs': _rs_status(), 'v47': _v47_status(), 'watch': _watch_status()}


_DESK = {'at': 0.0, 'value': None}


def desk_summary() -> dict | None:
    """One-line read for the main dashboard: today's lean and STRONG/normal flag from the daily pool. Cached 60 s."""
    import time as _t
    if _t.time() - _DESK['at'] < 60: return _DESK['value']
    v = None
    try:
        today = str(now_ist().date())
        d = next((d for d in reversed(_read_jsonl('days.jsonl')[-5:]) if d.get('date') == today), None)
        L = ((d or {}).get('lists') or {}).get('hc')
        if L:
            picks = L.get('picks') or []
            v = {'lean': L.get('lean'), 'strong': bool(L.get('strong')), 'view': L.get('view'),
                 'picks': [{'symbol': p['symbol'], 'side': p['side']} for p in picks[:3]]}
    except Exception:  # noqa: BLE001
        v = None
    _DESK.update(at=_t.time(), value=v)
    return v


_RESEARCH_CACHE = {'at': 0.0, 'ticker': []}
RESEARCH_CACHE_TTL = 20.0   # seconds - the main dashboard polls every 8s; RS refreshes 3x/day and the
                            # hierarchy desk once/day, so re-reading+scoring their whole history every poll
                            # is pure waste. desk_summary() above uses the same idea for the direction desk.


def _refresh_research_cache() -> None:
    import time as _t
    ticker = []
    try:
        from . import v134_relstrength as _rs
        rst = _rs.status() or {}
        today = rst.get('today') or {}
        for hm in sorted((today.get('slots') or {}), reverse=True):
            sl = today['slots'][hm]
            for p in (sl.get('picks') or []):
                ticker.append({'tag': 'REL-STRENGTH', 'symbol': p['symbol'], 'side': p['side'],
                               'text': f"z {p['z']} · grade {p.get('grade', '')}{' (reserve)' if p.get('reserve') else ''} · {hm} refresh",
                               'cls': 'up' if p['side'] == 'UP' else 'down'})
            if sl.get('picks'): break
    except Exception:  # noqa: BLE001
        log.exception('research cache: RS desk failed')
    try:
        from . import v135_hierarchy as _h
        hst = _h.status() or {}
        today = hst.get('today') or {}
        for pick in (today.get('picks') or []):
            out = pick.get('out') or {}
            latest = next((out[k] for k in ('exit', '60', '30', '15', '5', '3') if k in out), None)
            ticker.append({'tag': f"HIERARCHY {pick.get('role', 'PICK')} (research)", 'symbol': pick['symbol'], 'side': pick['side'],
                           'text': pick['industry'] + (f" · {latest:+.2f}%" if latest is not None else ' · entered ' + str(pick.get('entry', ''))),
                           'cls': 'up' if pick['side'] == 'UP' else 'down'})
    except Exception:  # noqa: BLE001
        log.exception('research cache: hierarchy desk failed')
    _RESEARCH_CACHE.update(at=_t.time(), ticker=ticker)


def research_ticker() -> list:
    """Member-facing read of today's live-signal desks (relative-strength and the V4.7 hierarchy desk only, by
    request - no direction desk, no scoreboard, just the ticker) for the dashboard ticker.
    Each item: {tag, symbol, side, text, cls}. Stock + direction only - no option instruction. Cached (see
    RESEARCH_CACHE_TTL): the dashboard polls every 8s and this would otherwise re-read + re-score each desk's
    whole history that often."""
    import time as _t
    if _t.time() - _RESEARCH_CACHE['at'] >= RESEARCH_CACHE_TTL: _refresh_research_cache()
    return _RESEARCH_CACHE['ticker']


def _v47_status():
    try:
        from . import v135_hierarchy; return v135_hierarchy.status()
    except Exception:  # noqa: BLE001
        log.exception('v135 status failed'); return None


def _watch_status():
    try:
        from . import v137_pool_watch; return v137_pool_watch.status()
    except Exception:  # noqa: BLE001
        log.exception('v137 status failed'); return None


def _rs_status():
    try:
        from . import v134_relstrength; return v134_relstrength.status()
    except Exception:  # noqa: BLE001
        log.exception('v134 status failed'); return None


def of_status(day: str) -> dict:
    if day in _of_buf: return dict(_of_buf[day])
    polls = _read_of(day)
    if not polls: return {'polls': 0, 'stocks': 0, 'futures': 0, 'first': None, 'last': None, 'by_phase': {}}
    st = {'polls': len(polls), 'stocks': max(len(p.get('q') or {}) for p in polls), 'futures': max(len(p.get('f') or {}) for p in polls),
          'first': polls[0]['t'], 'last': polls[-1]['t'], 'by_phase': {}}
    for p in polls: st['by_phase'][p.get('ph', 'fast')] = st['by_phase'].get(p.get('ph', 'fast'), 0) + 1
    _of_buf[day] = st
    return dict(st)


def lab_summary() -> dict:
    """Leaderboard for the page: per feature, the hit rate / IC t-stat at each horizon, best features first (by |t| at the day horizon)."""
    lab = _read_json('of_lab.json', None) or {}
    rows = lab.get('rows') or []
    by: dict = {}
    for r in rows: by.setdefault(r['feature'], {})[r['h']] = r
    order = sorted(by, key=lambda f: -abs((by[f].get('day') or {}).get('t') or 0))
    return {'sessions': lab.get('sessions', 0), 'target': lab.get('target_sessions', 40), 'first': lab.get('first'), 'last': lab.get('last'),
            'at': lab.get('at'), 'table': [{'feature': f, 'desc': LAB_FEATURES.get(f, ''), 'h': by[f]} for f in order]}
