"""V14.0 NIFTY ACD desk (ADMIN ONLY, spotting/paper - never places orders).

Rule (Mark Fisher ACD, 30-minute opening range; research on NIFTY 50 index 1-minute bars, Oct 2021 - Oct 2026):
  Levels   A = 0.15 x ATR10 (mean daily true range of the 10 prior sessions).
           Pivot range from the prior session: P = (H+L+C)/3, M = (H+L)/2; top = max(P, M), bottom = min(P, M).
           Opening range (OR) = high/low of 09:15-09:44.
  SHORT    15 consecutive completed 1-minute closes below ORL - A, and the latest close below the pivot-range bottom,
           between 09:45 and 14:29 -> sell at the next 1-minute open.
  LONG     mirror: 15 closes above ORH + A and above the pivot-range top. SHORTS-ONLY DESK: a LONG first signal means no trade
           that day (it still uses one of the 2 weekly slots, exactly as in the tested shorts-only rule); no long call is ever given.
           The long is tracked as a SHADOW (same entry/stop/exit, no call, no alert) to collect live evidence; ACD longs made
           +10.1 / +12.6 / -5.7 per signal (2021-24 / 2024-25 / last 13 months) and no tested filter fixed the last 13 months.
  Stop     distance to the OR midpoint capped at 40 points (in practice always 40). No target. Exit at the 15:20 open.
  Limits   first signal of the day only; at most 2 per ISO week (longs and shorts count together, as in research).
Research (gross index points, no costs): all signals 2021-24 +14.7/trade, Apr-24..Aug-25 +11.0, Sep-25..Oct-26 (holdout) +12.0,
  ~1.7/week, 44% win, avg win ~+78, avg loss ~-37. SHORT signals +20.4 / +14.8 / +21.6 and positive in every year 2021-26;
  LONG signals +8.4 / +7.8 / -1.3 and alternate good/bad years. Desk policy: SHORT calls only. Shorts-only rule as implemented
  (first signal of the day decides; a LONG first signal = no trade): +20.4 / +14.8 / +21.6, ~0.9 trades/week, max drawdown -307.
Experimental lane (logged, NOT for trading): on NIFTY expiry days, if an ACD signal fired before 13:00 and NIFTY at 12:59 is >= 50
  points from the open in the same direction, a 13:00 continuation trade (stop 30, half at +40, runner to break-even, rest out at the
  15:20 close). Without look-ahead it made +14.9 / +29.0 / -1.2 (holdout), so it is only recorded for forward evaluation.
Context shown (not a filter): prior-day NSE participant OI - FII index-futures net and retail-client index-option bias.

Daily cycle (IST, weekdays; fail-soft, idempotent per day):
  08:30-09:14  prep: NIFTY daily history, ACD levels, nearest NIFTY future, expiry flags, prior-day participant OI
  09:45-15:25  every 30 s: 1-minute bars -> signal / entry fill / live P&L / stop; experimental lane at 13:00
  15:21-16:30  final outcomes from the full day's bars
Storage: <V12 storage root>/v140/  (day_<date>.json, trades.jsonl)
"""
from __future__ import annotations

import datetime as dt
import io
import json
import logging
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import config

log = logging.getLogger(__name__)
ROOT = Path(config.V12_STORAGE_ROOT) / 'v140'
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
INDEX_TOKEN_FALLBACK = 256265            # NSE:NIFTY 50
OR_MIN, HOLD_BARS, A_FRAC, STOP_CAP = 30, 15, 0.15, 40.0
LAST_SIGNAL_BAR = 314                    # 14:29 bar; entry at the latest 14:30
EXIT_BAR = 365                           # 15:20 bar (minute 0 = 09:15)
ADDON_BAR, ADDON_MOVE, ADDON_STOP, ADDON_HALF = 225, 50.0, 30.0, 40.0   # 13:00 entry
WEEK_CAP = 2

_lock = threading.Lock()
_status: dict = {'started': None, 'steps': {}, 'last_error': None}


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST).replace(tzinfo=None)


def hhmm(bar: int) -> str:
    return f'{(bar + 555) // 60:02d}:{(bar + 555) % 60:02d}'


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
        log.exception('v140: unreadable %s', name); return default


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


def _append(name: str, obj) -> None:
    with _path(name).open('a') as f:
        f.write(json.dumps(obj, default=str) + '\n')


# ------------------------------------------------------------------------------------------ pure rule (research parity)
def compute_levels(daily: pd.DataFrame) -> dict | None:
    """daily: prior sessions only (columns open/high/low/close, oldest first, >= 11 rows)."""
    if daily is None or len(daily) < 11: return None
    d = daily.tail(11).reset_index(drop=True)
    pc = d.close.shift()
    tr = (np.maximum(d.high, pc) - np.minimum(d.low, pc)).iloc[1:]
    atr10 = float(tr.mean())
    p = d.iloc[-1]
    piv, mid = (p.high + p.low + p.close) / 3, (p.high + p.low) / 2
    return {'atr10': round(atr10, 2), 'A': round(A_FRAC * atr10, 2), 'pr_top': round(max(piv, mid), 2), 'pr_bot': round(min(piv, mid), 2),
            'prev_high': float(p.high), 'prev_low': float(p.low), 'prev_close': float(p.close)}


def minute_arrays(bars: pd.DataFrame, day: dt.date) -> tuple[np.ndarray, ...] | None:
    """Kite 1-minute bars for one day -> (open, high, low, close) indexed by minute since 09:15, forward-filled, completed bars only."""
    if bars is None or bars.empty: return None
    b = bars[bars['date'].dt.date == day].copy()
    if b.empty: return None
    b['m'] = (b['date'].dt.hour * 60 + b['date'].dt.minute) - 555
    b = b[(b.m >= 0) & (b.m < 375)].drop_duplicates('m', keep='last').set_index('m').sort_index()
    if b.empty: return None
    b = b.reindex(range(int(b.index.max()) + 1)).ffill().bfill()
    return b.open.values, b.high.values, b.low.values, b.close.values


def opening_range(h: np.ndarray, l: np.ndarray) -> tuple[float, float] | None:
    if len(h) < OR_MIN: return None
    return float(h[:OR_MIN].max()), float(l[:OR_MIN].min())


def scan_signal(c: np.ndarray, lv: dict, orh: float, orl: float) -> dict | None:
    """First ACD signal on completed bars: {'bar': signal bar, 'side': +1/-1}. Entry is the open of bar + 1."""
    au, ad = orh + lv['A'], orl - lv['A']
    run_u = run_d = 0
    for j in range(OR_MIN, min(len(c), LAST_SIGNAL_BAR + 1)):
        run_u = run_u + 1 if c[j] > au else 0
        run_d = run_d + 1 if c[j] < ad else 0
        if run_u >= HOLD_BARS and c[j] > lv['pr_top']: return {'bar': j, 'side': 1}
        if run_d >= HOLD_BARS and c[j] < lv['pr_bot']: return {'bar': j, 'side': -1}
    return None


def acd_stop(px: float, side: int, orh: float, orl: float) -> float:
    mid = (orh + orl) / 2
    return min(max((px - mid) if side > 0 else (mid - px), 1.0), STOP_CAP)


def run_acd(o, h, l, i: int, side: int, px: float, stop: float) -> dict:
    """Stop-first from the entry bar; flat at the 15:20 open. Returns closed result or open state on the bars available."""
    sl = px - side * stop
    mfe = 0.0
    for j in range(i, min(len(o), EXIT_BAR)):
        hit = (l[j] <= sl) if side > 0 else (h[j] >= sl)
        if hit:
            fill = sl if j == i else (min(sl, o[j]) if side > 0 else max(sl, o[j]))
            return {'state': 'closed', 'exit_bar': j, 'exit_px': round(fill, 2), 'points': round((fill - px) * side, 1), 'reason': 'stop', 'mfe': round(mfe, 1)}
        mfe = max(mfe, (h[j] - px) if side > 0 else (px - l[j]))
    if len(o) > EXIT_BAR:
        return {'state': 'closed', 'exit_bar': EXIT_BAR, 'exit_px': round(float(o[EXIT_BAR]), 2),
                'points': round((o[EXIT_BAR] - px) * side, 1), 'reason': '15:20 exit', 'mfe': round(mfe, 1)}
    return {'state': 'open', 'points': None, 'mfe': round(mfe, 1)}


def run_addon(o, h, l, c, side: int, px: float) -> dict:
    """Experimental 13:00 lane: stop 30, half at +40 then runner stop at break-even, rest at the 15:20 close (research sim)."""
    half = None; stop = -ADDON_STOP
    for j in range(ADDON_BAR, min(len(o), EXIT_BAR + 1)):
        fav = (h[j] - px) if side > 0 else (px - l[j]); adv = (px - l[j]) if side > 0 else (h[j] - px)
        if -adv <= stop:
            return {'state': 'closed', 'exit_bar': j, 'points': round(stop if half is None else .5 * half + .5 * stop, 1), 'half_booked': half is not None}
        if half is None and fav >= ADDON_HALF: half, stop = ADDON_HALF, 0.0
    if len(o) > EXIT_BAR:
        last = (c[EXIT_BAR] - px) * side
        return {'state': 'closed', 'exit_bar': EXIT_BAR, 'points': round(last if half is None else .5 * half + .5 * last, 1), 'half_booked': half is not None}
    return {'state': 'open', 'points': None, 'half_booked': half is not None}


def iso_week(d: dt.date) -> str:
    y, w, _ = d.isocalendar(); return f'{y}-{w:02d}'


def week_count(trades: list, day: dt.date) -> int:
    k = iso_week(day)
    return sum(1 for t in trades if t.get('week') == k and t.get('date') != str(day))


# ------------------------------------------------------------------------------------------ participant OI (context only)
def parse_participant(raw: bytes) -> dict | None:
    try:
        lines = raw.decode('utf-8', 'ignore').splitlines()
        h = next(i for i, ln in enumerate(lines) if 'Client Type' in ln)
        df = pd.read_csv(io.StringIO('\n'.join(lines[h:])))
        df.columns = [c.strip() for c in df.columns]
        df['Client Type'] = df['Client Type'].astype(str).str.strip().str.upper()
        row = lambda who: df[df['Client Type'] == who].iloc[0]
        num = lambda r, k: float(str(r[k]).replace(',', '').strip())
        f, c = row('FII'), row('CLIENT')
        fl, fs = num(f, 'Future Index Long'), num(f, 'Future Index Short')
        cb = (num(c, 'Option Index Call Long') - num(c, 'Option Index Call Short')) - (num(c, 'Option Index Put Long') - num(c, 'Option Index Put Short'))
        return {'fii_fut_net': int(fl - fs), 'fii_fut_long_pct': round(100 * fl / (fl + fs), 1), 'client_opt_bias': int(cb),
                'client_opt_lean': 'bullish' if cb > 0 else 'bearish'}
    except Exception:  # noqa: BLE001
        return None


def _participant(prev_day: dt.date) -> dict | None:
    try:
        from .v132_movers import _nse
        nse = _nse()
        for host in ('https://nsearchives.nseindia.com/content/nsccl/', 'https://archives.nseindia.com/content/nsccl/'):
            raw = nse.get(f'{host}fao_participant_oi_{prev_day:%d%m%Y}.csv')
            if raw:
                r = parse_participant(raw)
                if r: return r | {'date': str(prev_day)}
    except Exception as exc:  # noqa: BLE001
        log.warning('v140 participant OI: %s', exc)
    return None


# ------------------------------------------------------------------------------------------ Kite I/O
def _auth_error(exc: Exception) -> bool:
    t = (type(exc).__name__ + ' ' + str(exc)).lower()
    return 'tokenexception' in t or 'access_token' in t or 'api_key' in t


def _bars(kite, token: int, a: dt.datetime, b: dt.datetime, interval: str, tries: int = 3) -> pd.DataFrame:
    for attempt in range(tries):
        try:
            raw = kite.historical_data(token, a, b, interval); break
        except Exception as exc:  # noqa: BLE001
            if attempt == tries - 1 or _auth_error(exc): raise
            time.sleep(1.5 * (attempt + 1))
    df = pd.DataFrame(raw)
    if df.empty: return df
    df['date'] = pd.to_datetime(df['date'])
    if getattr(df['date'].dt, 'tz', None) is not None:
        df['date'] = df['date'].dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
    return df


def _instruments(kite, today: dt.date) -> dict:
    token = INDEX_TOKEN_FALLBACK
    try:
        token = next(i['instrument_token'] for i in kite.instruments('NSE') if i.get('tradingsymbol') == 'NIFTY 50')
    except Exception:  # noqa: BLE001
        pass
    nfo = [i for i in kite.instruments('NFO') if i.get('name') == 'NIFTY']
    futs = sorted((i for i in nfo if i.get('instrument_type') == 'FUT' and i['expiry'] >= today), key=lambda i: i['expiry'])
    opt_exp = sorted({i['expiry'] for i in nfo if i.get('instrument_type') in ('CE', 'PE') and i['expiry'] >= today})
    fut = futs[0] if futs else None
    return {'index_token': token, 'fut_symbol': fut['tradingsymbol'] if fut else None, 'fut_expiry': str(fut['expiry']) if fut else None,
            'lot_size': fut.get('lot_size') if fut else None,
            'expiry_today': bool(opt_exp and opt_exp[0] == today), 'monthly_expiry_today': bool(fut and fut['expiry'] == today)}


def _alert(text: str) -> None:
    try:
        from . import alerts
        if alerts.telegram_enabled():
            alerts._telegram_call('sendMessage', chat_id=config.TELEGRAM_CHAT_ID, text=text)
    except Exception as exc:  # noqa: BLE001
        log.warning('v140 alert: %s', exc)


# ------------------------------------------------------------------------------------------ daily steps
def run_prep(kite, today: dt.date | None = None) -> int:
    today = today or now_ist().date()
    name = f'day_{today}.json'
    day = _read_json(name, None)
    if day and day.get('levels'): return 1
    ins = _instruments(kite, today)
    d = _bars(kite, ins['index_token'], dt.datetime.combine(today - dt.timedelta(days=40), dt.time(9, 0)),
              dt.datetime.combine(today - dt.timedelta(days=1), dt.time(15, 30)), 'day')
    d = d[d['date'].dt.date < today].sort_values('date')
    lv = compute_levels(d)
    if lv is None: raise RuntimeError('not enough NIFTY daily history for ACD levels')
    prev_day = d['date'].dt.date.iloc[-1]
    _write_json(name, {'date': str(today), 'week': iso_week(today), 'prepared_at': now_ist(), 'levels': lv, 'instruments': ins,
                       'prev_session': str(prev_day), 'participant': _participant(prev_day), 'signal': None, 'trade': None, 'addon': None})
    return 1


def run_tick(kite, now: dt.datetime | None = None) -> str:
    now = now or now_ist(); today = now.date()
    name = f'day_{today}.json'
    day = _read_json(name, None)
    if not day or not day.get('levels'): return 'no prep'
    if day.get('final'): return 'final'
    bars = _bars(kite, day['instruments']['index_token'], dt.datetime.combine(today, dt.time(9, 15)), now, 'minute')
    if not bars.empty:   # keep completed minutes only
        bars = bars[bars['date'] < now.replace(second=0, microsecond=0)]
    arr = minute_arrays(bars, today)
    if arr is None: return 'no bars'
    o, h, l, c = arr
    rng = opening_range(h, l)
    if rng is None: return 'opening range forming'
    orh, orl = rng; lv = day['levels']
    day['or'] = {'high': orh, 'low': orl, 'mid': round((orh + orl) / 2, 2), 'a_up': round(orh + lv['A'], 2), 'a_down': round(orl - lv['A'], 2)}
    day['last'] = {'bar': hhmm(len(c) - 1), 'close': round(float(c[-1]), 2)}
    trades = _read_jsonl('trades.jsonl')
    changed_msgs = []

    if day.get('signal') is None:
        sig = scan_signal(c, lv, orh, orl)
        if sig:
            capped = week_count(trades, today) >= WEEK_CAP
            day['signal'] = {'bar': hhmm(sig['bar']), 'side': 'SHORT' if sig['side'] < 0 else 'LONG', 'skipped_week_cap': capped,
                             'ignored_long': sig['side'] > 0, 'signal_close': round(float(c[sig['bar']]), 2), '_bar': sig['bar'], '_side': sig['side']}
            if not capped and sig['side'] < 0:
                changed_msgs.append(f"ACD SHORT NIFTY signal at {hhmm(sig['bar'])}, "
                                    f"close {c[sig['bar']]:.1f}. Enter {day['instruments'].get('fut_symbol') or 'NIFTY FUT'} at market now. "
                                    f"Stop 40 pts, exit 15:20. Paper desk - no order placed.")

    s = day.get('signal')
    if s and not s.get('skipped_week_cap') and not s.get('ignored_long'):
        i, side = s['_bar'] + 1, s['_side']
        if day.get('trade') is None and len(o) > i:
            px = float(o[i]); stop = acd_stop(px, side, orh, orl)
            day['trade'] = {'entry_time': hhmm(i), 'entry': round(px, 2), 'stop_pts': round(stop, 1), 'stop_level': round(px - side * stop, 2)}
        t = day.get('trade')
        if t and t.get('state') != 'closed':
            r = run_acd(o, h, l, i, side, t['entry'], t['stop_pts'])
            t.update(r)
            t['live_points'] = r['points'] if r['state'] == 'closed' else round((float(c[-1]) - t['entry']) * side, 1)
            if r['state'] == 'closed':
                t['exit_time'] = hhmm(r['exit_bar'])
                changed_msgs.append(f"ACD {s['side']} closed at {t['exit_time']} ({r['reason']}): {r['points']:+.1f} pts (paper).")

        ins = day['instruments']
        if ins.get('expiry_today') and s['_bar'] < ADDON_BAR - 1 and day.get('addon') is None and len(c) >= ADDON_BAR:
            move = float(c[ADDON_BAR - 1] - o[0])
            same = (move > 0) == (side > 0)
            day['addon'] = {'move_1259': round(move, 1), 'active': abs(move) >= ADDON_MOVE and same}
        a = day.get('addon')
        if a and a.get('active') and a.get('state') != 'closed' and len(o) > ADDON_BAR:
            a['entry'] = round(float(o[ADDON_BAR]), 2)
            a.update(run_addon(o, h, l, c, side, a['entry']))

    if s and s.get('ignored_long') and not s.get('skipped_week_cap'):
        # Shadow long: the same entry/stop/exit a long call would have had, recorded for evidence only (no call, no alert).
        i = s['_bar'] + 1
        if day.get('shadow') is None and len(o) > i:
            px = float(o[i]); stop = acd_stop(px, 1, orh, orl)
            day['shadow'] = {'entry_time': hhmm(i), 'entry': round(px, 2), 'stop_pts': round(stop, 1)}
        sh = day.get('shadow')
        if sh and sh.get('state') != 'closed':
            sh.update(run_acd(o, h, l, i, 1, sh['entry'], sh['stop_pts']))
            sh['live_points'] = sh['points'] if sh['state'] == 'closed' else round(float(c[-1]) - sh['entry'], 1)

    if len(o) > EXIT_BAR + 1:
        day['final'] = True
        t = day.get('trade')
        if s and s.get('ignored_long') and not s.get('skipped_week_cap'):     # uses a weekly slot, as in the tested rule; never shown as a trade
            sh = day.get('shadow') or {}
            _append('trades.jsonl', {'date': str(today), 'week': day['week'], 'side': 'LONG', 'ignored': True, 'signal_time': s['bar'],
                                     'shadow_entry': sh.get('entry'), 'shadow_points': sh.get('points'), 'shadow_mfe': sh.get('mfe')})
        if t and t.get('state') == 'closed':
            _append('trades.jsonl', {'date': str(today), 'week': day['week'], 'side': s['side'],
                                     'signal_time': s['bar'], 'entry_time': t['entry_time'], 'entry': t['entry'],
                                     'exit_time': t.get('exit_time'), 'exit': t.get('exit_px'), 'points': t['points'], 'reason': t.get('reason'),
                                     'mfe': t.get('mfe'), 'expiry': day['instruments'].get('expiry_today'),
                                     'addon_points': (day.get('addon') or {}).get('points')})
    _write_json(name, day)
    for m in changed_msgs: _alert('🟦 ' + m)
    return 'final' if day.get('final') else ('trade' if day.get('trade') else ('signal' if day.get('signal') else 'watching'))


# ------------------------------------------------------------------------------------------ scheduler
def _step(name: str, fn, *a):
    try:
        r = fn(*a); _status['steps'][name] = {'ok': True, 'at': now_ist(), 'result': r}; return r
    except Exception as exc:  # noqa: BLE001
        log.warning('v140 step %s: %s', name, exc)
        _status['steps'][name] = {'ok': False, 'at': now_ist(), 'error': str(exc)[:300]}; _status['last_error'] = str(exc)[:300]
        return None


def _loop(get_kite):
    while True:
        try:
            now = now_ist(); hm = now.hour * 60 + now.minute
            if now.weekday() < 5 and 510 <= hm <= 990:
                kite = get_kite()
                if kite is not None:
                    if hm <= 554 or not _read_json(f'day_{now.date()}.json', None):
                        _step('prep', run_prep, kite)
                    if 585 <= hm <= 990:
                        _step('tick', run_tick, kite)
        except Exception:  # noqa: BLE001
            log.exception('v140 scheduler tick failed')
        time.sleep(30)


def start_once(get_kite) -> None:
    with _lock:
        if _status.get('started'): return
        _status['started'] = now_ist()
    threading.Thread(target=_loop, args=(get_kite,), daemon=True, name='v140-acd').start()


# ------------------------------------------------------------------------------------------ dashboard
def _score(rows: list, key: str = 'points') -> dict:
    p = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
    if not p: return {'n': 0, 'avg': None, 'win': None, 'total': None}
    return {'n': len(p), 'avg': round(sum(p) / len(p), 1), 'win': round(100 * sum(x > 0 for x in p) / len(p)), 'total': round(sum(p), 1)}


def status() -> dict:
    today = now_ist().date()
    rows = _read_jsonl('trades.jsonl')
    trades = [t for t in rows if not t.get('ignored')]
    day = _read_json(f'day_{today}.json', None)
    sig = (day or {}).get('signal') or {}
    used_today = bool(day and (day.get('trade') or (sig.get('ignored_long') and not sig.get('skipped_week_cap'))))
    if day and day.get('signal'):
        day['signal'] = {k: v for k, v in day['signal'].items() if not k.startswith('_')}
    return {'started': _status.get('started'), 'steps': _status.get('steps', {}), 'last_error': _status.get('last_error'),
            'today': day, 'week_used': week_count(rows, today) + (1 if used_today else 0), 'week_cap': WEEK_CAP,
            'recent': list(reversed(trades))[:30],
            'score_all': _score(trades), 'score_short': _score([t for t in trades if t.get('side') == 'SHORT']),
            'long_ignored': sum(1 for t in rows if t.get('ignored')),
            'score_shadow_long': _score([t for t in rows if t.get('ignored')], 'shadow_points'),
            'score_addon': _score([t for t in trades if t.get('addon_points') is not None], 'addon_points')}
