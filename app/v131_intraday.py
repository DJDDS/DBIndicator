"""V13.1 intraday paper lanes (ADMIN ONLY, spotting/paper - never places orders).

Lane 1  09:45 SHORT detector (bearish). At 09:45 IST every F&O stock gets a frozen LightGBM score from a snapshot of
        09:15-09:45 trading plus the stock's recent days. The 3 lowest scores are paper-SHORTED at the 09:45 price and
        covered at the 15:15 price (research: hold to 15:15 beat every target/stop tried). 10 bps round-trip cost.
        Research, walk-forward: 2019-26 +30 bps/short (58% win), every year 2017-26 positive, 2026 +14 bps.
        Each short also gets a paper ATM PUT (nearest expiry with 3+ days left), bought at the 09:45 ask and sold at the
        15:15 bid, skipped when the spread is above 3.5%. Modelled 2019-26 (not real prices): +11% of premium, 55% win.
Lane 2  Gap-down BOUNCE basket (bullish, cash). Only on days the F&O universe opens more than 0.5% lower on average.
        Buy up to 10 stocks that opened below yesterday's LOW by more than one 90-day sigma while their prior close
        was above the 20-day average; entry at the opening (pre-open auction) price, exits recorded at 09:45 and 10:15.
        15 bps cost. Research 2019-26: +131 bps (76%) at the 10:15 exit, ~11 days a year.
Recorder  09:09-09:14 pre-open snapshot of every F&O stock (indicative price, buy/sell quantities) for future research.

Daily cycle (IST, weekdays; every step fail-soft, retried, and idempotent per day):
  08:00-09:12  prep: F&O list, daily history (150 days) and 20-day first-30-minute volume per stock
  09:09-09:14  pre-open recorder
  09:16-09:30  gap basket: select + entry at the opening price
  09:45-09:58  short detector: snapshot -> score -> 3 paper shorts
  10:16-11:30  gap basket outcomes (09:45 and 10:15 closes from 5-minute bars)
  15:16-16:30  short outcomes (15:15 close from 5-minute bars, plus best/worst move after entry)
Storage: <V12 storage root>/v131/  (prep_<date>.json, preopen.jsonl, shorts.jsonl, gaps.jsonl, state.json)
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
ASSETS = Path(__file__).with_name('v131_assets')
ROOT = Path(config.V12_STORAGE_ROOT) / 'v131'
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
INDEX_NAMES = {'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'NIFTYNXT50', 'SENSEX', 'BANKEX'}
FE = ['gap', 'mv', 'fpc', 'nmv', 'ngap', 'rng_s', 'vrel', 'vwd', 'pos', 'r1', 'r5', 'd20h', 'mkt', 'brd', 'mgap', 'rel', 'nrel', 'atr20']
K_SHORT, COST_SHORT, COST_GAP = 3, 10.0, 15.0

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
        log.exception('v131: unreadable %s', name); return default


def _write_json(name: str, obj) -> None:
    p = _path(name); tmp = p.with_suffix(p.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, default=str)); tmp.replace(p)


def _append(name: str, obj) -> None:
    with _path(name).open('a') as f:
        f.write(json.dumps(obj, default=str) + '\n')


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


def model() -> TextGBM:
    global _model
    if _model is None:
        _model = TextGBM(ASSETS / 'short945_model.txt')
    return _model


def constants() -> dict:
    return json.loads((ASSETS / 'constants.json').read_text())


# ------------------------------------------------------------------------------------------ pure feature maths
def prep_from_history(daily: pd.DataFrame, first30_vol: pd.Series) -> dict | None:
    """Per-stock values known before the open. daily: completed sessions (index date; open/high/low/close),
    first30_vol: volume traded 09:15-09:45 on each prior session (index date). Mirrors early_model.py / chan.py."""
    d = daily.dropna(subset=['close', 'high', 'low']).sort_index()
    d = d[(d.close > 0) & (d.high > 0) & (d.low > 0)]
    if len(d) < 25: return None
    c = d.close.to_numpy(float); h = d.high.to_numpy(float); l = d.low.to_numpy(float)
    rng = np.log(h / l)
    ret = np.diff(np.log(c))
    pc = c[-1]
    out = {
        'pc': pc, 'ph': h[-1], 'pl': l[-1],
        'r1': float(np.log(c[-1] / c[-2])),
        'r5': float(np.log(c[-1] / c[-6])) if len(c) >= 6 else None,
        'd20h': float(np.log(pc / np.max(c[-20:]))),                    # max of the last 20 closes (incl. yesterday)
        'atr20': float(np.mean(rng[-20:])) if len(rng) >= 10 else None,  # mean daily log range, last 20 sessions
        'ma20': float(np.mean(c[-20:])) if len(c) >= 15 else None,       # gap basket trend filter
        'sd90': float(np.std(ret[-90:], ddof=1)) if len(ret) >= 60 else None,
        'last_date': str(d.index[-1])[:10],
    }
    fv = first30_vol.dropna().sort_index()
    fv = fv[fv > 0]
    out['vs20'] = float(np.median(fv.to_numpy()[-20:])) if len(fv) >= 10 else None
    return out


def snapshot_features(prep: dict, quotes: dict) -> pd.DataFrame:
    """09:45 features for every stock with prep + quote. quotes[sym] = dict(o, c, h, l, v, vwap)."""
    rows = []
    for s, q in quotes.items():
        p = prep.get(s)
        if not p or not q: continue
        o, c, h, l, v, vw = (q.get(k) for k in ('o', 'c', 'h', 'l', 'v', 'vwap'))
        if not all(isinstance(x, (int, float)) and x > 0 for x in (o, c, h, l)) or not p.get('atr20'): continue
        a = p['atr20']
        gap = math.log(o / p['pc']); mv = math.log(c / o)
        rows.append(dict(symbol=s, o=o, c=c, gap=gap, mv=mv, fpc=math.log(c / p['pc']), nmv=mv / a, ngap=gap / a,
                         rng_s=math.log(h / l) / a, vrel=(v / p['vs20']) if v and p.get('vs20') else np.nan,
                         vwd=math.log(c / vw) if vw and vw > 0 else np.nan, pos=(c - l) / (h - l) if h > l else np.nan,
                         r1=p.get('r1'), r5=p.get('r5'), d20h=p.get('d20h'), atr20=a))
    X = pd.DataFrame(rows)
    if X.empty: return X
    X['mkt'] = X.mv.mean(); X['brd'] = (X.mv > 0).mean(); X['mgap'] = X.gap.mean()
    X['rel'] = X.mv - X.mkt; X['nrel'] = X.rel / X.atr20
    return X.replace([np.inf, -np.inf], np.nan)


def pick_shorts(X: pd.DataFrame, banned: set, k: int = K_SHORT) -> pd.DataFrame:
    X = X[~X.symbol.isin(banned)].dropna(subset=['atr20', 'vrel', 'r1']).copy()
    if len(X) < 30: return X.iloc[0:0]
    X['score'] = model().predict(X[FE].to_numpy(float))
    return X.nsmallest(k, 'score')


def gap_candidates(prep: dict, opens: dict, k: int = 10) -> tuple[float | None, pd.DataFrame]:
    """Gap-down bounce basket. Returns (market gap, picks). Picks only when the market gap < -0.5%."""
    rows = []
    for s, o in opens.items():
        p = prep.get(s)
        if not p or not o or o <= 0: continue
        g = math.log(o / p['pc'])
        if abs(g) >= 0.15: continue                                       # corporate-action-sized gaps excluded
        rows.append(dict(symbol=s, o=o, g=g, gapL=math.log(o / p['pl']), sd90=p.get('sd90'), up=(p['pc'] > p['ma20']) if p.get('ma20') else False))
    G = pd.DataFrame(rows)
    if G.empty: return None, G
    mgap = float(G.g.mean())
    if mgap >= -0.005: return mgap, G.iloc[0:0]
    L = G[(G.sd90.notna()) & (G.gapL < -G.sd90) & G.up]
    return mgap, L.nsmallest(k, 'gapL')


def choose_put(contracts: list, spot: float, today: dt.date, min_days: int = 3) -> dict | None:
    """ATM put: strike nearest the 09:45 price, nearest expiry with at least `min_days` calendar days left (else the next one)."""
    if not contracts or not spot: return None
    ex = sorted({c['e'] for c in contracts})
    ok = [e for e in ex if (dt.date.fromisoformat(e) - today).days >= min_days]
    if not ok: return None
    cand = [c for c in contracts if c['e'] == ok[0]]
    return min(cand, key=lambda c: (abs(c['k'] - spot), -c['k']))


def put_from_quote(q: dict, max_spread_pct: float = 3.5) -> dict:
    dp = (q or {}).get('depth') or {}
    bid = ((dp.get('buy') or [{}])[0] or {}).get('price') or 0; ask = ((dp.get('sell') or [{}])[0] or {}).get('price') or 0
    if not bid or not ask or ask <= bid * 0.5:
        return {'put_status': 'NO_PUT', 'put_reason': 'no two-sided quote'}
    spr = (ask - bid) / ((ask + bid) / 2) * 100
    if spr > max_spread_pct:
        return {'put_status': 'NO_PUT', 'put_reason': f'spread {spr:.1f}% > {max_spread_pct}%', 'put_bid': bid, 'put_ask': ask}
    return {'put_status': 'OPEN', 'put_bid': bid, 'put_ask': ask, 'put_entry': ask, 'put_spread_pct': round(spr, 2)}


# ------------------------------------------------------------------------------------------ Kite I/O
def _fo_universe(kite) -> dict:
    """{symbol: NSE instrument token} for stocks with NFO futures."""
    nfo = kite.instruments('NFO')
    names = {i['name'] for i in nfo if i.get('instrument_type') == 'FUT' and i.get('segment') == 'NFO-FUT'} - INDEX_NAMES
    nse = kite.instruments('NSE')
    return {i['tradingsymbol']: i['instrument_token'] for i in nse if i.get('tradingsymbol') in names and i.get('segment') == 'NSE'}


def _bars(kite, token: int, a: dt.datetime, b: dt.datetime, interval: str, tries: int = 3) -> pd.DataFrame:
    for attempt in range(tries):                       # Kite allows ~3 history calls/s shared with other engines: back off and retry
        try:
            raw = kite.historical_data(token, a, b, interval); break
        except Exception:  # noqa: BLE001
            if attempt == tries - 1: raise
            time.sleep(1.5 * (attempt + 1))
    df = pd.DataFrame(raw)
    if df.empty: return df
    df['date'] = pd.to_datetime(df['date'])
    if getattr(df['date'].dt, 'tz', None) is not None:
        df['date'] = df['date'].dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
    return df


def run_prep(kite, today: dt.date | None = None) -> int:
    today = today or now_ist().date()
    name = f'prep_{today}.json'
    if _read_json(name, None): return 1
    uni = _fo_universe(kite)
    out, n = {}, 0
    start = dt.datetime.combine(today - dt.timedelta(days=150), dt.time(9, 0))
    end = dt.datetime.combine(today - dt.timedelta(days=1), dt.time(15, 30))
    start5 = dt.datetime.combine(today - dt.timedelta(days=35), dt.time(9, 0))
    for s, tok in sorted(uni.items()):
        try:
            d = _bars(kite, tok, start, end, 'day'); time.sleep(0.35)
            if d.empty: continue
            d = d[d.date.dt.date < today].set_index(d.date.dt.date)
            b = _bars(kite, tok, start5, end, '5minute'); time.sleep(0.35)
            fv = pd.Series(dtype=float)
            if not b.empty:
                t = b.date.dt.hour * 60 + b.date.dt.minute
                b = b[(t >= 555) & (t <= 580) & (b.date.dt.date < today)]              # bars 09:15 .. 09:40 (close at 09:45)
                fv = b.groupby(b.date.dt.date).volume.sum()
            p = prep_from_history(d, fv)
            if p: p['token'] = tok; out[s] = p; n += 1
        except Exception as exc:  # noqa: BLE001
            log.warning('v131 prep %s: %s', s, exc)
    if n < max(30, 0.8 * len(uni)):
        raise RuntimeError(f'prep incomplete: only {n} of {len(uni)} F&O stocks')
    try:                                               # put contracts per stock (two nearest expiries), for the paper put instruction
        nfo = kite.instruments('NFO'); puts = {}
        for i in nfo:
            if i.get('instrument_type') == 'PE' and i.get('segment') == 'NFO-OPT' and i.get('name') in out:
                e = i.get('expiry'); e = e.date() if isinstance(e, dt.datetime) else e
                if e and e >= today:
                    puts.setdefault(i['name'], []).append({'ts': i['tradingsymbol'], 'k': float(i['strike']), 'e': str(e), 'lot': i.get('lot_size')})
        for sname, lst in puts.items():
            ex = sorted({r['e'] for r in lst})[:2]
            out[sname]['puts'] = [r for r in lst if r['e'] in ex]
    except Exception as exc:  # noqa: BLE001
        log.warning('v131 put contracts: %s', exc)
    banned = None
    try:
        from .v130_swing import NSE
        banned = NSE().ban_day(today)
    except Exception as exc:  # noqa: BLE001
        log.warning('v131 ban list: %s', exc)
    _write_json(name, {'date': str(today), 'built_at': now_ist(), 'stocks': out, 'banned': sorted(banned) if banned else [],
                       'ban_known': banned is not None, 'universe': len(uni)})
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


def run_preopen(kite, today: dt.date | None = None) -> int:
    today = today or now_ist().date()
    st = load_state()
    if st.get('preopen') == str(today): return 1
    prep = _read_json(f'prep_{today}.json', None)
    if not prep: raise RuntimeError('prep not ready')
    q = _quotes(kite, sorted(prep['stocks']))
    rec = {s: {k: v.get(k) for k in ('last_price', 'total_buy_quantity', 'total_sell_quantity', 'volume', 'timestamp')} | {'open': (v.get('ohlc') or {}).get('open')}
           for s, v in q.items()}
    _append('preopen.jsonl', {'date': str(today), 'at': now_ist(), 'stocks': rec})
    st['preopen'] = str(today); save_state(st)
    return len(rec)


def run_gap(kite, today: dt.date | None = None) -> int:
    today = today or now_ist().date()
    st = load_state()
    if st.get('gap') == str(today): return 1
    prep = _read_json(f'prep_{today}.json', None)
    if not prep: raise RuntimeError('prep not ready')
    q = _quotes(kite, sorted(prep['stocks']))
    fresh = [s for s, v in q.items() if _fresh(v, today)]
    if len(fresh) < 50:
        raise RuntimeError('no fresh quotes yet (market holiday or Kite login needed)')
    opens = {s: (q[s].get('ohlc') or {}).get('open') for s in fresh}
    banned = set(prep.get('banned') or [])
    mgap, picks = gap_candidates(prep['stocks'], {s: o for s, o in opens.items() if s not in banned})
    day = {'date': str(today), 'at': now_ist(), 'market_gap_pct': None if mgap is None else round(mgap * 100, 3),
           'active': bool(len(picks)), 'picks': [{'symbol': r.symbol, 'entry': r.o, 'gap_vs_prev_low_pct': round(r.gapL * 100, 2),
                                                   'sigma_pct': round(r.sd90 * 100, 2), 'status': 'OPEN'} for r in picks.itertuples()]}
    _append('gaps.jsonl', day)
    st['gap'] = str(today); save_state(st)
    return len(picks)


def run_shorts(kite, today: dt.date | None = None) -> int:
    today = today or now_ist().date()
    st = load_state()
    if st.get('short') == str(today): return 1
    prep = _read_json(f'prep_{today}.json', None)
    if not prep: raise RuntimeError('prep not ready')
    q = _quotes(kite, sorted(prep['stocks']))
    quotes = {}
    for s, v in q.items():
        if not _fresh(v, today): continue
        o = v.get('ohlc') or {}
        quotes[s] = dict(o=o.get('open'), c=v.get('last_price'), h=o.get('high'), l=o.get('low'), v=v.get('volume'), vwap=v.get('average_price'))
    if len(quotes) < 50:
        raise RuntimeError('no fresh quotes yet (market holiday or Kite login needed)')
    X = snapshot_features(prep['stocks'], quotes)
    picks = pick_shorts(X, set(prep.get('banned') or []))
    put_info = {}
    try:
        chosen = {r.symbol: choose_put(prep['stocks'].get(r.symbol, {}).get('puts') or [], r.c, today) for r in picks.itertuples()}
        keys = ['NFO:' + c['ts'] for c in chosen.values() if c]
        pq = kite.quote(keys) if keys else {}
        for sym, c in chosen.items():
            if not c: put_info[sym] = {'put_status': 'NO_PUT', 'put_reason': 'no put contract'}; continue
            put_info[sym] = {'put': c['ts'], 'put_strike': c['k'], 'put_expiry': c['e'], 'put_lot': c.get('lot'), **put_from_quote(pq.get('NFO:' + c['ts']))}
    except Exception as exc:  # noqa: BLE001
        log.warning('v131 put instruction: %s', exc)
    mk = X[['mkt', 'brd', 'mgap']].iloc[0].to_dict() if len(X) else {}
    day = {'date': str(today), 'at': now_ist(), 'scored': int(len(X)), 'ban_known': prep.get('ban_known'),
           'market': {'move_since_open_pct': round(mk.get('mkt', 0) * 100, 2), 'share_up': round(mk.get('brd', 0) * 100, 1), 'gap_pct': round(mk.get('mgap', 0) * 100, 2)},
           'picks': [{'symbol': r.symbol, 'entry': r.c, 'score': round(float(r.score), 1), 'move_since_open_pct': round(r.mv * 100, 2),
                      'vs_market_atr': round(float(r.nrel), 2), 'volume_x_normal': None if pd.isna(r.vrel) else round(float(r.vrel), 2),
                      'status': 'OPEN', **put_info.get(r.symbol, {})} for r in picks.itertuples()]}
    _append('shorts.jsonl', day)
    st['short'] = str(today); save_state(st)
    return len(picks)


def _close_at(kite, token: int, day: dt.date, bar_minute: int) -> tuple[float | None, pd.DataFrame]:
    """Close of the 5-minute bar starting at bar_minute (minutes after midnight), plus the session bars."""
    b = _bars(kite, token, dt.datetime.combine(day, dt.time(9, 0)), dt.datetime.combine(day, dt.time(15, 30)), '5minute'); time.sleep(0.35)
    if b.empty: return None, b
    t = b.date.dt.hour * 60 + b.date.dt.minute
    x = b[t == bar_minute]
    return (float(x.close.iloc[0]) if len(x) else None), b.assign(t=t)


def run_short_outcomes(kite, today: dt.date | None = None) -> int:
    today = today or now_ist().date()
    rows = _read_jsonl('shorts.jsonl'); n = 0
    for day in rows:
        if day['date'] != str(today): continue
        prep = _read_json(f"prep_{day['date']}.json", {}).get('stocks', {})
        for p in day['picks']:
            if p.get('status') != 'OPEN': continue
            tok = (prep.get(p['symbol']) or {}).get('token')
            if not tok: continue
            ex, b = _close_at(kite, tok, today, 15 * 60 + 10)                 # bar 15:10-15:15 -> the 15:15 close
            if ex is None: raise RuntimeError(f"15:15 bar not available yet for {p['symbol']}")
            after = b[(b.t >= 9 * 60 + 45) & (b.t <= 15 * 60 + 10)]
            e = p['entry']
            if p.get('put_status') == 'OPEN' and p.get('put'):
                try:
                    pq = (kite.quote(['NFO:' + p['put']]) or {}).get('NFO:' + p['put']) or {}
                    dp = pq.get('depth') or {}; bid = ((dp.get('buy') or [{}])[0] or {}).get('price') or pq.get('last_price')
                    if bid:
                        p.update(put_exit=bid, put_status='CLOSED', put_ret_pct=round((bid / p['put_entry'] - 1) * 100, 1))
                except Exception as exc:  # noqa: BLE001
                    log.warning('v131 put exit %s: %s', p['put'], exc)
            p.update(exit=ex, status='CLOSED', ret_bps=round(-math.log(ex / e) * 1e4 - COST_SHORT, 1),
                     best_bps=round(math.log(e / after.low.min()) * 1e4, 1) if len(after) else None,
                     worst_bps=round(-math.log(after.high.max() / e) * 1e4, 1) if len(after) else None)
            n += 1
    _rewrite_jsonl('shorts.jsonl', rows)
    return n


def run_gap_outcomes(kite, today: dt.date | None = None) -> int:
    today = today or now_ist().date()
    rows = _read_jsonl('gaps.jsonl'); n = 0
    for day in rows:
        if day['date'] != str(today): continue
        prep = _read_json(f"prep_{day['date']}.json", {}).get('stocks', {})
        for p in day['picks']:
            if p.get('status') != 'OPEN': continue
            tok = (prep.get(p['symbol']) or {}).get('token')
            if not tok: continue
            x45, b = _close_at(kite, tok, today, 9 * 60 + 40)                 # bar 09:40-09:45
            x15 = b[b.t == 10 * 60 + 10].close if len(b) else pd.Series(dtype=float)  # bar 10:10-10:15
            if x45 is None or not len(x15): raise RuntimeError(f"10:15 bar not available yet for {p['symbol']}")
            e = p['entry']
            p.update(exit_0945=x45, exit_1015=float(x15.iloc[0]), status='CLOSED',
                     ret_0945_bps=round(math.log(x45 / e) * 1e4 - COST_GAP, 1), ret_bps=round(math.log(float(x15.iloc[0]) / e) * 1e4 - COST_GAP, 1))
            n += 1
    _rewrite_jsonl('gaps.jsonl', rows)
    return n


# ------------------------------------------------------------------------------------------ state, scheduler, status
def load_state() -> dict:
    return _read_json('state.json', {})


def save_state(st: dict) -> None:
    _write_json('state.json', st)


def _step(name: str, fn, *a) -> bool:
    try:
        r = fn(*a); _status['steps'][name] = {'ok': True, 'at': now_ist(), 'result': r}; return True
    except Exception as exc:  # noqa: BLE001
        log.warning('v131 step %s: %s', name, exc)
        _status['steps'][name] = {'ok': False, 'at': now_ist(), 'error': str(exc)[:300]}; _status['last_error'] = str(exc)[:300]
        return False


def _loop(get_kite):
    done: dict = {}
    while True:
        try:
            now = now_ist(); d = now.date(); hm = now.hour * 60 + now.minute
            if d.weekday() < 5 and 480 <= hm <= 990:
                kite = get_kite()
                if kite is not None:
                    key = lambda k: f'{d}:{k}'
                    def due(k, a, b, every=1):
                        return a <= hm <= b and not done.get(key(k)) and (hm - a) % every == 0
                    if due('prep', 480, 552, 4): done[key('prep')] = _step('prep', run_prep, kite)
                    if due('preopen', 549, 554): done[key('preopen')] = _step('preopen', run_preopen, kite)
                    if due('gap', 556, 570): done[key('gap')] = _step('gap', run_gap, kite)
                    if due('short', 585, 598): done[key('short')] = _step('short', run_shorts, kite)
                    if due('gap_out', 616, 690, 3): done[key('gap_out')] = _step('gap_out', run_gap_outcomes, kite)
                    if due('short_out', 916, 990, 3): done[key('short_out')] = _step('short_out', run_short_outcomes, kite)
        except Exception:  # noqa: BLE001
            log.exception('v131 scheduler tick failed')
        time.sleep(20)


def start_once(get_kite) -> None:
    with _lock:
        if _status.get('started'): return
        _status['started'] = now_ist()
    threading.Thread(target=_loop, args=(get_kite,), daemon=True, name='v131-intraday').start()


def _score(trades: list) -> dict:
    r = [t['ret_bps'] for t in trades if isinstance(t.get('ret_bps'), (int, float))]
    if not r: return {'n': 0, 'win': None, 'avg_bps': None, 'total_bps': None}
    return {'n': len(r), 'win': round(100 * sum(x > 0 for x in r) / len(r)), 'avg_bps': round(sum(r) / len(r), 1), 'total_bps': round(sum(r), 1)}


def status() -> dict:
    shorts = _read_jsonl('shorts.jsonl'); gaps = _read_jsonl('gaps.jsonl')
    s_tr = [p | {'date': d['date']} for d in shorts for p in d['picks']]
    g_tr = [p | {'date': d['date']} for d in gaps for p in d['picks']]
    today = str(now_ist().date())
    return {'started': _status.get('started'), 'steps': _status.get('steps', {}), 'last_error': _status.get('last_error'),
            'today_short': next((d for d in reversed(shorts) if d['date'] == today), None),
            'today_gap': next((d for d in reversed(gaps) if d['date'] == today), None),
            'short_recent': list(reversed(s_tr))[:30], 'gap_recent': list(reversed(g_tr))[:30],
            'gap_days': [{'date': d['date'], 'market_gap_pct': d.get('market_gap_pct'), 'active': d.get('active'), 'n': len(d['picks'])} for d in gaps][-10:][::-1],
            'score_short': _score(s_tr), 'score_gap': _score(g_tr), 'constants': constants(),
            'score_put': _score([{'ret_bps': t['put_ret_pct']} for t in s_tr if isinstance(t.get('put_ret_pct'), (int, float))]),
            'preopen_days': len(_read_jsonl('preopen.jsonl')) if _path('preopen.jsonl').exists() else 0}
