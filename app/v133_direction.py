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
  09:15-09:25  order-flow polls (quote of every F&O stock every ~10 s)
  09:25-09:40  list: first two candles of every stock -> features -> model -> day view -> list + paper entries + Telegram
  15:20-16:30  outcome: candles of the listed stocks -> exit at 15:15, returns at +30/60/90/120 min, best/worst
Storage: <V12 storage root>/v133/  (aggs.parquet, prep_<date>.json, of_<date>.json, days.jsonl, dayview.json, settings.json)
"""
from __future__ import annotations

import datetime as dt
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
                    'hc_quantile': 0.8, 'hc_top_n': 1, 'hc_book_pct': 1.0}

_lock = threading.Lock()
_model = None
_status: dict = {'started': None, 'steps': {}, 'last_error': None}
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


HORIZONS = ('30', '60', '90', '120', 'day')
LISTS = ('hc',) + HORIZONS          # 'hc' = HIGH-CONVICTION pick: mean of the 5 horizon models, strongest days only, top 1, exit 15:15
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


def select(X: pd.DataFrame, p_up: np.ndarray, hist: list, settings: dict) -> dict:
    """Day view, strong/normal flag and the ranked list (most confident first)."""
    X = X.assign(p_up=p_up); X['conf'] = (X.p_up - 0.5).abs(); X['side'] = np.where(X.p_up > 0.5, 'UP', 'DOWN')
    view = float(X.p_up.mean()); thr = day_view_threshold(hist, settings['strong_quantile'], settings['window'])
    strong = thr is not None and abs(view - 0.5) > thr
    L = X.sort_values('conf', ascending=False).head(int(settings['top_n']))
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
            try: ltp, vol, bq, sq = (float(x) if x is not None else np.nan for x in v)
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


def _load_aggs() -> pd.DataFrame:
    p = _path('aggs.parquet')
    try:
        return pd.read_parquet(p) if p.exists() else pd.DataFrame()
    except Exception:  # noqa: BLE001
        log.exception('v133 aggs unreadable'); return pd.DataFrame()


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
    A = pd.concat(out, ignore_index=True) if out else pd.DataFrame()
    if not A.empty:
        A['date'] = pd.to_datetime(A.date).dt.date
        A = A.drop_duplicates(['symbol', 'date'], keep='last')
        cut = today - dt.timedelta(days=430); A = A[A.date >= cut].sort_values(['symbol', 'date']).reset_index(drop=True)
        tmp = _path('aggs.tmp.parquet'); A.to_parquet(tmp); tmp.replace(_path('aggs.parquet'))
    return A


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


def run_of_poll(kite, today: dt.date | None = None) -> int:
    """One order-flow poll (called every ~10 s between 09:15 and 09:25)."""
    today = today or now_ist().date()
    prep = _read_json(f'prep_{today}.json', None)
    if not prep: raise RuntimeError('prep not ready')
    q = _quote_full(kite, sorted(prep['hist']))
    pl = {'t': now_ist().isoformat(timespec='seconds'),
          'q': {s: [v.get('last_price'), v.get('volume'), v.get('buy_quantity'), v.get('sell_quantity')] for s, v in q.items()}}
    if str(today) not in _of_buf:                     # after a restart, continue the day's file instead of overwriting it
        _of_buf[str(today)] = list(_read_json(f'of_{today}.json', []) or [])
    buf = _of_buf[str(today)]; buf.append(pl)
    _write_json(f'of_{today}.json', buf)
    return len(pl['q'])


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
    of = of_scores(_read_json(f'of_{today}.json', []) or [], {k: v['o'] for k, v in st.items()})
    sels, preds = {}, {}
    for h in HORIZONS:
        hv.setdefault(h, M['horizons'][h]['day_view_seed'])
        preds[h] = predict_up(X, h)
        sels[h] = select(X, preds[h], hv[h], s)
    # HIGH-CONVICTION: ensemble of the 5 horizon models (research: strongest 20% of days, top 1, hold to 15:15 -> 66% 2018-23 / 66% 2024-26)
    hv.setdefault('hc', M['ensemble']['day_view_seed'])
    sels['hc'] = select(X, np.mean([preds[h] for h in HORIZONS], axis=0), hv['hc'],
                        {**s, 'strong_quantile': s['hc_quantile'], 'top_n': s['hc_top_n']})
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
        trade = sel['strong'] or (s['trade_normal_days'] and h != 'hc'); picks = []
        for r in sel['list'].itertuples():
            ltp = (q.get(r.symbol) or {}).get('last_price')
            o = of.get(r.symbol); agree = None if not o else bool((o['ofs'] > 0) == (r.side == 'UP'))
            picks.append({'symbol': r.symbol, 'side': r.side, 'p_up': round(float(r.p_up), 4), 'conf': round(float(r.conf), 4), 'open': r.o,
                          'p925': r.p925, 'entry': ltp or r.p925, 'gap_pct': round(r.gap * 100, 2), 'exc20_pct': round(r.exc20 * 100, 2),
                          'of': o, 'of_agree': agree, 'status': 'OPEN' if trade else 'WATCH',
                          'option': dict(opts[(r.symbol, r.side)]) if (r.symbol, r.side) in opts else None})
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
        if h == 'hc' and not L['strong']: out.append('\n[HIGH-CONVICTION] no pick today (market view not strong enough)'); continue
        tag = 'STRONG - paper trades' if L['strong'] else 'normal - watch only'
        name = 'HIGH-CONVICTION pick' if h == 'hc' else (h if h == 'day' else h + ' min')
        out.append(f"\n[{name} -> exit {L['exit_at']}] {tag}, lean {L['lean']}")
        out += [f"{i + 1}. {p['symbol']} {'▲ UP' if p['side'] == 'UP' else '▼ DOWN'} @ {p['entry']}"
                + ('' if p['of_agree'] is None else (' OF+' if p['of_agree'] else ' OF-'))
                + (f" | {p['option']['ts']} ask {p['option']['ask']}" if (p.get('option') or {}).get('status') == 'OPEN' else '') for i, p in enumerate(L['picks'])]
    return '\n'.join(out)


def run_outcomes(kite, today: dt.date | None = None) -> int:
    today = today or now_ist().date()
    days = _read_jsonl('days.jsonl'); n = 0
    prep = _read_json(f'prep_{today}.json', None) or {}
    bars_cache: dict = {}
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
                res = outcome_from_bars(bars_cache[p['symbol']], p['entry'], p['p925'], p['side'], EXIT_BAR[h], book_pct=L.get('book_pct') or day.get('settings', {}).get('book_target_pct'))
                if res: p.update(res); n += 1
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


def outcome_from_bars(b: pd.DataFrame, entry: float, p925: float, side: str, k_exit: int = K_EXIT, book_pct: float | None = None) -> dict | None:
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
    last_poll = 0.0
    while True:
        try:
            now = now_ist(); d = now.date(); hm = now.hour * 60 + now.minute
            if d.weekday() < 5 and 470 <= hm <= 990:
                kite = get_kite()
                if kite is not None:
                    key = lambda k: f'{d}:{k}'
                    def due(k, a, b, every=1):
                        return a <= hm <= b and not done.get(key(k)) and (hm - a) % every == 0
                    if due('prep', 470, 550, 4): done[key('prep')] = _step('prep', run_prep, kite)
                    if 555 <= hm < 565 and time.time() - last_poll >= 10:
                        last_poll = time.time(); _step('of_poll', run_of_poll, kite)
                    if 565 <= hm <= 580 and now.second >= 20 and not done.get(key('list')):
                        done[key('list')] = _step('list', run_list, kite)
                    if due('outcome', 920, 990, 5): done[key('outcome')] = _step('outcome', run_outcomes, kite)
        except Exception:  # noqa: BLE001
            log.exception('v133 scheduler tick failed')
        time.sleep(5)


def start_once(get_kite) -> None:
    with _lock:
        if _status.get('started'): return
        _status['started'] = now_ist()
    threading.Thread(target=_loop, args=(get_kite,), daemon=True, name='v133-direction').start()


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
            'hc_research': (m.get('ensemble') or {}).get('research', {}),
            'settings': load_settings(), 'horizons': list(HORIZONS), 'lists': list(LISTS), 'exit_label': EXIT_LABEL, 'of_today': of_status(today)}


def of_status(day: str) -> dict:
    polls = _of_buf.get(day) or (_read_json(f'of_{day}.json', []) or [])
    syms = {k for pl in polls for k in (pl.get('q') or {})}
    return {'polls': len(polls), 'stocks': len(syms), 'first': polls[0]['t'] if polls else None, 'last': polls[-1]['t'] if polls else None}
