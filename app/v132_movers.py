"""V13.2 movement predictor (ADMIN ONLY, spotting/paper - never places orders, no direction call).

At 09:16 IST every F&O stock is scored by a frozen LightGBM model (read with numpy, no lightgbm needed) for the
chance of an UNUSUAL move today: travel of at least 2% from the 09:15 open (either way) AND at least 1.3x the stock's
own average daily travel of the last 20 sessions. The top 3 are shown; the top pick is flagged HIGH CONFIDENCE when
its probability beats the median of the last 250 trading days' top-pick probabilities.

Research (walk-forward by year, true per-date F&O list, inputs = Kite prices + NSE board meetings + NSE overnight
announcements): top pick 61% (2018-26), high-confidence days 76% (2018-26) / 81% (Oct-2024 onward), against ~25% for
an average stock on those same days. See constants in v132_assets/mover_meta.json.

Daily cycle (IST, weekdays, fail-soft, idempotent per day):
  08:10-09:08  prep: ~2 years of daily bars per F&O stock -> pre-open features; peer groups (once a year);
               NSE board meetings around today
  09:16-09:29  pick: opening prices + NSE overnight announcements -> score -> top 3 -> dashboard + Telegram
  15:31-16:30  outcome: day high/low from Kite quote -> did each pick make the unusual move?
Storage: <V12 storage root>/v132/  (prep_<date>.json, picks.jsonl, peers_<year>.json, conf_hist.json, state.json)
"""
from __future__ import annotations

import datetime as dt
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
ASSETS = Path(__file__).with_name('v132_assets')
ROOT = Path(config.V12_STORAGE_ROOT) / 'v132'
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
INDEX_NAMES = {'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'NIFTYNXT50', 'SENSEX', 'BANKEX'}
TOP_K, ABS_MOVE, REL_MOVE, CONF_WINDOW = 3, 0.02, 1.3, 250
RES_PAT = ('financial result', 'outcome of board')

_lock = threading.Lock()
_model = None
_status: dict = {'started': None, 'steps': {}, 'last_error': None}


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST).replace(tzinfo=None)


# ------------------------------------------------------------------------------------------ storage
def _path(name: str) -> Path:
    ROOT.mkdir(parents=True, exist_ok=True)
    return ROOT / name


def _read_json(name: str, default):
    try:
        return json.loads(_path(name).read_text())
    except FileNotFoundError:
        return default
    except Exception:  # noqa: BLE001
        log.exception('v132: unreadable %s', name); return default


def _write_json(name: str, obj) -> None:
    p = _path(name); tmp = p.with_suffix(p.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, default=str)); tmp.replace(p)


def _read_jsonl(name: str) -> list:
    p = _path(name)
    if not p.exists(): return []
    out = []
    for line in p.read_text().splitlines():
        try: out.append(json.loads(line))
        except ValueError: pass
    return out


def _rewrite_jsonl(name: str, rows: list) -> None:
    p = _path(name); tmp = p.with_suffix('.tmp')
    tmp.write_text(''.join(json.dumps(r, default=str) + '\n' for r in rows)); tmp.replace(p)


def meta() -> dict:
    return json.loads((ASSETS / 'mover_meta.json').read_text())


def model() -> TextGBM:
    global _model
    if _model is None:
        _model = TextGBM(ASSETS / 'mover_model.txt')
    return _model


def predict_proba(X: pd.DataFrame) -> np.ndarray:
    m = model()
    raw = m.predict(X[m.feature_names].to_numpy(float))
    return 1.0 / (1.0 + np.exp(-raw))


# ------------------------------------------------------------------------------------------ pure feature maths
def stock_pre(d: pd.DataFrame) -> dict | None:
    """Pre-open values from completed daily sessions (columns open/high/low/close/volume, ascending).
    Mirrors the research definitions (premover.py): rolling windows end with yesterday's session."""
    d = d.dropna(subset=['open', 'high', 'low', 'close'])
    d = d[(d.open > 0) & (d.high > 0) & (d.low > 0) & (d.close > 0)]
    if len(d) < 25: return None
    o, h, l, c = (d[k].to_numpy(float) for k in ('open', 'high', 'low', 'close'))
    v = d['volume'].to_numpy(float) if 'volume' in d else np.full(len(d), np.nan)
    exc = np.maximum(np.log(h / o), np.log(o / l)); rng = np.log(h / l)
    ret = np.r_[np.nan, np.diff(np.log(c))]

    def mean_last(x, w, mp):
        x = x[-w:]; x = x[~np.isnan(x)]
        return float(np.mean(x)) if len(x) >= mp else np.nan

    def ext_last(x, w, mp, f):
        x = x[-w:]; x = x[~np.isnan(x)]
        return float(f(x)) if len(x) >= mp else np.nan

    pc = c[-1]
    p = {'pc': pc, 'exc1': exc[-1], 'exc2': exc[-2], 'exc5': mean_last(exc, 5, 3), 'exc20': mean_last(exc, 20, 10), 'exc60': mean_last(exc, 60, 30),
         'rng1': rng[-1], 'rng20': mean_last(rng, 20, 10), 'r1': ret[-1], 'r5': float(np.log(pc / c[-6])), 'r20': float(np.log(pc / c[-21])) if len(c) >= 21 else np.nan}
    p['ar1'] = abs(p['r1'])
    vv = np.where(v > 0, v, np.nan); med20 = ext_last(vv, 20, 10, np.median)
    p['vol1'] = float(np.log(vv[-1] / med20)) if med20 and not np.isnan(vv[-1]) else np.nan
    m5 = mean_last(vv, 5, 3); p['vol5'] = float(np.log(m5 / med20)) if med20 and not np.isnan(m5) else np.nan
    p['hi52'] = float(np.log(pc / ext_last(c, 250, 120, np.max))); p['lo52'] = float(np.log(pc / ext_last(c, 250, 120, np.min)))
    p['hi20'] = float(np.log(pc / ext_last(h, 20, 10, np.max))); p['lo20'] = float(np.log(pc / ext_last(l, 20, 10, np.min)))
    m7 = ext_last(rng, 7, 5, np.min); p['nr7'] = float(rng[-1] <= m7) if not np.isnan(m7) else 0.0
    p['squeeze'] = p['rng1'] / p['rng20'] if p['rng20'] else np.nan
    p['last_date'] = str(d.index[-1])[:10]
    return {k: (None if isinstance(x, float) and (math.isnan(x) or math.isinf(x)) else x) for k, x in p.items()}


def peer_groups(R: pd.DataFrame) -> dict:
    """Data-driven peer groups: average-linkage clustering of last calendar year's daily returns (distance 1-corr),
    k = max(8, n/8). Same rule as research (sklearn AgglomerativeClustering, average linkage, precomputed)."""
    R = R.loc[:, R.notna().mean() > 0.6]
    n = R.shape[1]
    if n < 30: return {}
    C = R.corr(min_periods=60).fillna(0).to_numpy(); np.fill_diagonal(C, 1.0)
    Dm = 1.0 - C; k = max(8, n // 8)
    size = np.ones(n); alive = np.ones(n, bool); members = {i: [i] for i in range(n)}
    Dw = Dm.copy(); np.fill_diagonal(Dw, np.inf)
    for _ in range(n - k):
        M = np.where(alive[:, None] & alive[None, :], Dw, np.inf)
        i, j = np.unravel_index(np.argmin(M), M.shape)
        if i > j: i, j = j, i
        new = (size[i] * Dw[i] + size[j] * Dw[j]) / (size[i] + size[j])
        Dw[i, :] = new; Dw[:, i] = new; Dw[i, i] = np.inf
        alive[j] = False; Dw[j, :] = np.inf; Dw[:, j] = np.inf
        size[i] += size[j]; members[i] += members.pop(j)
    cols = list(R.columns)
    return {cols[m]: g for g, (_, ms) in enumerate(sorted(members.items())) for m in ms}


def overnight_news(rows: list, today: dt.date) -> dict:
    """{symbol: (n_overnight, res_overnight)} from NSE announcements (symbol, desc, sort_date), research rule:
    filed after 15:30 (counts for the next business day) or before 09:15 (counts the same day)."""
    out: dict = {}
    for r in rows or []:
        try:
            t = pd.Timestamp(r.get('sort_date') or r.get('an_dt'))
        except Exception:  # noqa: BLE001
            continue
        if pd.isna(t): continue
        mins = t.hour * 60 + t.minute
        if 555 <= mins < 930: continue
        eff = t.normalize() + (pd.offsets.BDay(1) if mins >= 930 else pd.Timedelta(0))
        if eff.date() != today: continue
        s = str(r.get('symbol') or '').strip()
        n, res = out.get(s, (0, 0))
        out[s] = (n + 1, max(res, int(any(p in str(r.get('desc') or '').lower() for p in RES_PAT))))
    return out


def _session_on_or_after(d: dt.date) -> dt.date:
    while d.weekday() >= 5: d += dt.timedelta(days=1)
    return d


def _known_session(ts: pd.Timestamp) -> dt.date:
    d = ts.date()
    if ts.weekday() >= 5 or ts.hour * 60 + ts.minute >= 555:
        d = _session_on_or_after(d + dt.timedelta(days=1)) if ts.weekday() < 5 else _session_on_or_after(d)
    return d


def board_flags(rows: list, today: dt.date, prev_session: dt.date) -> dict:
    """{symbol: dict(bm_today, bm_today_res, bm_yday, bm_yday_res)}. A meeting counts only if it was intimated before
    the open of the session it falls on (research rule)."""
    out: dict = {}
    for r in rows or []:
        try:
            md = pd.Timestamp(r.get('bm_date')).date(); ts = pd.Timestamp(r.get('bm_timestamp'))
        except Exception:  # noqa: BLE001
            continue
        if pd.isna(ts): continue
        ms = _session_on_or_after(md)
        if _known_session(ts) > ms: continue
        res = int('result' in str(r.get('bm_purpose') or '').lower())
        s = str(r.get('bm_symbol') or r.get('symbol') or '').strip()
        f = out.setdefault(s, {'bm_today': 0, 'bm_today_res': 0, 'bm_yday': 0, 'bm_yday_res': 0})
        if ms == today: f['bm_today'] = 1; f['bm_today_res'] = max(f['bm_today_res'], res)
        if ms == prev_session: f['bm_yday'] = 1; f['bm_yday_res'] = max(f['bm_yday_res'], res)
    return out


def open_features(pre: dict, opens: dict, peers: dict, news: dict, bm: dict, today: dt.date, market: dict) -> pd.DataFrame:
    rows = []
    for s, o in opens.items():
        p = pre.get(s)
        if not p or not o or o <= 0 or not p.get('pc') or not p.get('exc20') or not p.get('rng20'): continue
        r = dict(p); r['symbol'] = s; r['o'] = o; r['gap'] = math.log(o / p['pc'])
        n, res = news.get(s, (0, 0)); r['n_overnight'] = n; r['res_overnight'] = res
        r.update(bm.get(s, {'bm_today': 0, 'bm_today_res': 0, 'bm_yday': 0, 'bm_yday_res': 0}))
        r['peer'] = peers.get(s, -1)
        rows.append(r)
    X = pd.DataFrame(rows)
    if X.empty: return X
    X = X.astype({c: float for c in X.columns if c not in ('symbol', 'last_date', 'token')}, errors='ignore')
    X['m_exc'], X['m_ret'], X['m_disp'] = market.get('m_exc'), market.get('m_ret'), market.get('m_disp')
    X['dow'] = float(today.weekday())
    X['agap'] = X.gap.abs() / X.rng20; X['m_gap'] = X.gap.mean(); X['m_agap'] = X.gap.abs().mean()
    X['idgap'] = (X.gap - X.m_gap) / X.rng20
    X['agap_exc'] = X.gap.abs() / X.exc20; X['exc5_20'] = X.exc5 / X.exc20; X['exc20_60'] = X.exc20 / X.exc60; X['rng1_e20'] = X.rng1 / X.exc20
    X['brk_hi'] = X.hi20 + X.gap; X['brk_lo'] = X.lo20 + X.gap; X['brk52h'] = X.hi52 + X.gap; X['brk52l'] = X.lo52 + X.gap
    X['gap_r1'] = np.sign(X.gap) * X.r1; X['gap_r5'] = np.sign(X.gap) * X.r5
    X['news_gap'] = X.n_overnight * X.gap.abs(); X['res_gap'] = X.res_overnight * X.gap.abs()
    X['bm_today_gap'] = X.bm_today * X.gap.abs(); X['bm_yday_gap'] = X.bm_yday * X.gap.abs()
    P = X[X.peer >= 0]; g = P.groupby('peer')
    for c, v in (('peer_gap', g.gap.transform('mean')), ('peer_n', g.gap.transform('size')), ('peer_agap', g.gap.transform(lambda s: s.abs().mean())), ('peer_r1', g.r1.transform('mean'))):
        X[c] = np.nan; X.loc[P.index, c] = v
    X['gap_vs_peer'] = (X.gap - X.peer_gap) / X.rng20
    for c in ('agap', 'squeeze', 'vol1', 'exc5_20', 'idgap', 'gap_vs_peer', 'rng20'):
        src = X[c].abs() if c in ('idgap', 'gap_vs_peer') else X[c]
        X['cs_' + c] = src.rank(pct=True)
    return X.replace([np.inf, -np.inf], np.nan)


def market_from_pre(pre: dict) -> dict:
    """Previous session's market-wide values over the universe (research: m_exc, m_ret, m_disp shifted one day)."""
    last = max((p['last_date'] for p in pre.values() if p.get('last_date')), default=None)
    ex = [p['exc1'] for p in pre.values() if p.get('last_date') == last and p.get('exc1') is not None]
    rt = [p['r1'] for p in pre.values() if p.get('last_date') == last and p.get('r1') is not None]
    return {'m_exc': float(np.mean(ex)) if ex else None, 'm_ret': float(np.mean(rt)) if rt else None,
            'm_disp': float(np.std(rt, ddof=1)) if len(rt) > 1 else None, 'session': last}


def need_move(p_exc20: float) -> float:
    return max(ABS_MOVE, REL_MOVE * p_exc20)


def conf_threshold(hist: list) -> float | None:
    h = [x for x in hist[-CONF_WINDOW:] if isinstance(x, (int, float))]
    return float(np.quantile(h, 0.5)) if len(h) >= 120 else None


# ------------------------------------------------------------------------------------------ Kite / NSE I/O
def _fo_universe(kite) -> dict:
    nfo = kite.instruments('NFO')
    names = {i['name'] for i in nfo if i.get('instrument_type') == 'FUT' and i.get('segment') == 'NFO-FUT'} - INDEX_NAMES
    nse = kite.instruments('NSE')
    return {i['tradingsymbol']: i['instrument_token'] for i in nse if i.get('tradingsymbol') in names and i.get('segment') == 'NSE'}


def _auth_error(exc: Exception) -> bool:
    t = (type(exc).__name__ + ' ' + str(exc)).lower()
    return 'tokenexception' in t or 'access_token' in t or 'api_key' in t


def _daily(kite, token: int, a: dt.date, b: dt.date, tries: int = 3) -> pd.DataFrame:
    for attempt in range(tries):
        try:
            raw = kite.historical_data(token, dt.datetime.combine(a, dt.time(9, 0)), dt.datetime.combine(b, dt.time(15, 30)), 'day'); break
        except Exception as exc:  # noqa: BLE001
            if attempt == tries - 1 or _auth_error(exc): raise
            time.sleep(1.5 * (attempt + 1))
    df = pd.DataFrame(raw)
    if df.empty: return df
    df['date'] = pd.to_datetime(df['date'])
    if getattr(df['date'].dt, 'tz', None) is not None:
        df['date'] = df['date'].dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
    return df.set_index(df['date'].dt.date).drop(columns='date').sort_index()


def _nse():
    from .v130_swing import NSE
    return NSE()


def _board_meetings(today: dt.date) -> list | None:
    try:
        return _nse().board_meetings(today - dt.timedelta(days=10), today + dt.timedelta(days=3))
    except Exception as exc:  # noqa: BLE001
        log.warning('v132 board meetings: %s', exc); return None


def _announcements(a: dt.date, b: dt.date) -> list | None:
    try:
        j = _nse().get(f"https://www.nseindia.com/api/corporate-announcements?index=equities&from_date={a:%d-%m-%Y}&to_date={b:%d-%m-%Y}", json_ok=True)
        return j if isinstance(j, list) else (j or {}).get('data') if isinstance(j, dict) else None
    except Exception as exc:  # noqa: BLE001
        log.warning('v132 announcements: %s', exc); return None


def _telegram(text: str) -> bool:
    try:
        from . import alerts
        if not alerts.telegram_enabled(): return False
        alerts._telegram_call('sendMessage', chat_id=config.TELEGRAM_CHAT_ID, text=text)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning('v132 telegram: %s', exc); return False


# ------------------------------------------------------------------------------------------ steps
def load_state() -> dict:
    return _read_json('state.json', {})


def save_state(st: dict) -> None:
    _write_json('state.json', st)


def run_prep(kite, today: dt.date | None = None) -> int:
    today = today or now_ist().date()
    name = f'prep_{today}.json'
    if _read_json(name, None): return 1
    uni = _fo_universe(kite)
    start = dt.date(today.year - 1, 1, 1) - dt.timedelta(days=10)
    end = today - dt.timedelta(days=1)
    pre, rets, n = {}, {}, 0
    for s, tok in sorted(uni.items()):
        try:
            d = _daily(kite, tok, start, end); time.sleep(0.35)
            if d.empty: continue
            d = d[d.index < today]
            p = stock_pre(d)
            if p: p['token'] = tok; pre[s] = p; n += 1
            c = d['close'].astype(float); r = np.log(c / c.shift(1))
            rets[s] = r[[x.year == today.year - 1 for x in r.index]]
        except Exception as exc:  # noqa: BLE001
            if _auth_error(exc):
                raise RuntimeError('Kite login needed (access token expired) - log in on the scanner') from exc
            log.warning('v132 prep %s: %s', s, exc)
    if n < max(30, 0.8 * len(uni)):
        raise RuntimeError(f'prep incomplete: only {n} of {len(uni)} F&O stocks')
    peers_name = f'peers_{today.year}.json'
    if not _read_json(peers_name, None):
        _write_json(peers_name, peer_groups(pd.DataFrame(rets)))
    bm = _board_meetings(today)
    _write_json(name, {'date': str(today), 'built_at': now_ist(), 'pre': pre, 'market': market_from_pre(pre),
                       'board_meetings': bm, 'universe': len(uni)})
    return n


def _quotes(kite, syms: list) -> dict:
    out = {}
    for i in range(0, len(syms), 450):
        q = kite.quote(['NSE:' + s for s in syms[i:i + 450]])
        for k, v in (q or {}).items():
            out[k.split(':', 1)[1]] = v
        time.sleep(0.4)
    return out


def _fresh(q: dict, today: dt.date) -> bool:
    ts = q.get('last_trade_time') or q.get('timestamp')
    try:
        return pd.Timestamp(ts).date() == today
    except Exception:  # noqa: BLE001
        return False


def score_day(prep: dict, opens: dict, news_rows: list | None, today: dt.date, peers: dict) -> tuple[pd.DataFrame, dict]:
    pre = prep['pre']
    prev = max((p['last_date'] for p in pre.values() if p.get('last_date')), default=str(today))
    news = overnight_news(news_rows or [], today)
    bm = board_flags(prep.get('board_meetings') or [], today, dt.date.fromisoformat(prev))
    X = open_features(pre, opens, peers, news, bm, today, prep.get('market') or {})
    if X.empty: return X, {}
    X['prob'] = predict_proba(X)
    X = X.sort_values('prob', ascending=False).reset_index(drop=True)
    info = {'scored': len(X), 'news_ok': news_rows is not None, 'board_ok': prep.get('board_meetings') is not None,
            'market_gap_pct': round(float(X.gap.mean()) * 100, 2)}
    return X, info


def run_pick(kite, today: dt.date | None = None) -> int:
    today = today or now_ist().date()
    st = load_state()
    if st.get('pick') == str(today): return 1
    prep = _read_json(f'prep_{today}.json', None)
    if not prep: raise RuntimeError('prep not ready')
    q = _quotes(kite, sorted(prep['pre']))
    fresh = [s for s, v in q.items() if _fresh(v, today)]
    if len(fresh) < 50:
        raise RuntimeError('no fresh quotes yet (market holiday or Kite login needed)')
    opens = {s: (q[s].get('ohlc') or {}).get('open') for s in fresh}
    prev = max(p['last_date'] for p in prep['pre'].values() if p.get('last_date'))
    news_rows = _announcements(dt.date.fromisoformat(prev) - dt.timedelta(days=3), today)
    X, info = score_day(prep, opens, news_rows, today, _read_json(f'peers_{today.year}.json', {}))
    if X.empty: raise RuntimeError('nothing scored')
    hist = _read_json('conf_hist.json', None) or meta().get('conf_seed', [])
    thr = conf_threshold(hist)
    top_p = float(X.prob.iloc[0])
    conf = bool(thr is not None and top_p > thr)
    picks = []
    for r in X.head(TOP_K).itertuples():
        need = need_move(r.exc20)
        picks.append({'symbol': r.symbol, 'open': round(r.o, 2), 'prob_pct': round(r.prob * 100, 1), 'gap_pct': round(r.gap * 100, 2),
                      'normal_move_pct': round(r.exc20 * 100, 2), 'need_pct': round(need * 100, 2),
                      'up_level': round(r.o * math.exp(need), 2), 'down_level': round(r.o * math.exp(-need), 2),
                      'results_yday': bool(r.bm_yday_res), 'results_today': bool(r.bm_today_res), 'overnight_filings': int(r.n_overnight),
                      'status': 'OPEN'})
    day = {'date': str(today), 'at': now_ist(), 'confident': conf, 'threshold_pct': round(thr * 100, 1) if thr else None,
           'top_prob_pct': round(top_p * 100, 1), **info, 'picks': picks}
    rows = [r for r in _read_jsonl('picks.jsonl') if r.get('date') != str(today)] + [day]
    _rewrite_jsonl('picks.jsonl', rows)
    _write_json('conf_hist.json', (hist + [top_p])[-CONF_WINDOW * 2:])
    st['pick'] = str(today); save_state(st)
    _telegram(telegram_text(day))
    return len(picks)


def telegram_text(day: dict) -> str:
    head = ('🎯 MOVER - HIGH CONFIDENCE' if day['confident'] else '🎯 Mover (normal confidence)') + f" · {day['date']} 09:16"
    lines = [head, 'Unusual move expected (direction not predicted). Hit = touches either level today.']
    for i, p in enumerate(day['picks'], 1):
        tag = ' · results yday' if p['results_yday'] else (' · results today' if p['results_today'] else '')
        lines.append(f"{i}. {p['symbol']} open {p['open']} · needs ±{p['need_pct']}% (normal {p['normal_move_pct']}%) → {p['up_level']} / {p['down_level']} · {p['prob_pct']}%{tag}")
    if not day.get('news_ok') or not day.get('board_ok'):
        lines.append('⚠ NSE news/board-meeting feed unavailable today: scores are less reliable.')
    lines.append('Spotting/paper only - not an order.')
    return '\n'.join(lines)


def run_outcomes(kite, today: dt.date | None = None) -> int:
    today = today or now_ist().date()
    rows = _read_jsonl('picks.jsonl'); n = 0
    for day in rows:
        if day.get('date') != str(today): continue
        q = _quotes(kite, [p['symbol'] for p in day['picks']])
        for p in day['picks']:
            if p.get('status') == 'CLOSED': continue
            o = (q.get(p['symbol']) or {}).get('ohlc') or {}
            h, l = o.get('high'), o.get('low')
            if not h or not l: continue
            up = math.log(h / p['open']); dn = math.log(p['open'] / l)
            p['max_move_pct'] = round(max(up, dn) * 100, 2); p['dir'] = 'UP' if up >= dn else 'DOWN'
            p['hit'] = bool(max(up, dn) * 100 >= p['need_pct']); p['high'] = h; p['low'] = l; p['status'] = 'CLOSED'; n += 1
    _rewrite_jsonl('picks.jsonl', rows)
    if n == 0: raise RuntimeError('no outcomes recorded yet')
    return n


def _step(name: str, fn, *a) -> bool:
    try:
        r = fn(*a); _status['steps'][name] = {'ok': True, 'at': now_ist(), 'result': r}; return True
    except Exception as exc:  # noqa: BLE001
        log.warning('v132 step %s: %s', name, exc)
        _status['steps'][name] = {'ok': False, 'at': now_ist(), 'error': str(exc)[:300]}; _status['last_error'] = str(exc)[:300]
        return False


def _loop(get_kite):
    done: dict = {}
    while True:
        try:
            now = now_ist(); d = now.date(); hm = now.hour * 60 + now.minute
            if d.weekday() < 5 and 490 <= hm <= 990:
                kite = get_kite()
                if kite is not None:
                    key = lambda k: f'{d}:{k}'
                    def due(k, a, b, every=1):
                        return a <= hm <= b and not done.get(key(k)) and (hm - a) % every == 0
                    if due('prep', 490, 548, 3): done[key('prep')] = _step('prep', run_prep, kite)
                    if due('pick', 556, 569): done[key('pick')] = _step('pick', run_pick, kite)
                    if due('outcome', 931, 990, 5): done[key('outcome')] = _step('outcome', run_outcomes, kite)
        except Exception:  # noqa: BLE001
            log.exception('v132 scheduler tick failed')
        time.sleep(20)


def start_once(get_kite) -> None:
    with _lock:
        if _status.get('started'): return
        _status['started'] = now_ist()
    threading.Thread(target=_loop, args=(get_kite,), daemon=True, name='v132-movers').start()


def scorecard(rows: list) -> dict:
    def sc(ps):
        h = [p['hit'] for p in ps if isinstance(p.get('hit'), bool)]
        return {'n': len(h), 'hit': round(100 * sum(h) / len(h)) if h else None}
    top = [d['picks'][0] for d in rows if d.get('picks')]
    conf = [d['picks'][0] for d in rows if d.get('picks') and d.get('confident')]
    allp = [p for d in rows for p in d.get('picks', [])]
    return {'top1': sc(top), 'top1_confident': sc(conf), 'top3': sc(allp)}


def status() -> dict:
    rows = _read_jsonl('picks.jsonl'); today = str(now_ist().date())
    try:
        m = meta()
    except Exception:  # noqa: BLE001
        m = {}
    return {'started': _status.get('started'), 'steps': _status.get('steps', {}), 'last_error': _status.get('last_error'),
            'today': next((d for d in reversed(rows) if d['date'] == today), None), 'recent': list(reversed(rows))[:20],
            'score': scorecard(rows), 'research': m.get('research', {})}
