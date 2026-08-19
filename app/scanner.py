"""
Pulls candles from Kite for each stock in the watchlist and runs the
Scanner confluence logic on them. Kite's historical_data() needs an
instrument_token (not just the trading symbol), so the instrument list
is fetched once and cached in memory.
"""
import datetime as dt
import logging
import time
from zoneinfo import ZoneInfo

import pandas as pd

from .config import settings
from .indicators import compute_signal

log = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> dt.datetime:
    """Current wall-clock time in IST, as a naive datetime (this is what
    Kite's API expects - it always operates in IST regardless of where
    the request comes from). Cloud hosts typically run their system
    clock in UTC, so using plain datetime.now() here would silently
    shift market-hours checks and historical-data windows by 5.5 hours -
    e.g. the scanner would think the market was still closed at what is
    actually mid-afternoon IST."""
    return dt.datetime.now(_IST).replace(tzinfo=None)

_instrument_cache = {}

# Index/sector futures on NFO whose underlying isn't a tradeable stock -
# excluded when deriving the F&O stock universe from Kite's instrument
# list (see get_fno_stock_list below).
_NON_STOCK_FNO_NAMES = {
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
    "NIFTY NEXT 50", "SENSEX", "BANKEX", "NIFTYIT", "NIFTY IT",
}

_fno_cache = {"date": None, "symbols": []}
_fut_map_cache = {"date": None, "map": {}}


def _load_instrument_map(kite):
    global _instrument_cache
    if _instrument_cache:
        return _instrument_cache
    instruments = kite.instruments("NSE")
    _instrument_cache = {
        row["tradingsymbol"]: row["instrument_token"]
        for row in instruments
        if row.get("segment") == "NSE"
    }
    return _instrument_cache


def get_fno_stock_list(kite) -> list:
    """Returns the exact list of NSE stocks currently eligible for F&O
    trading, derived live from Kite's own NFO instrument list (the
    stock-futures "name" field) rather than a hardcoded list that could
    go stale as SEBI/NSE periodically revise F&O eligibility. Cached
    for the day since this rarely changes intraday."""
    today = dt.date.today().isoformat()
    if _fno_cache["date"] == today and _fno_cache["symbols"]:
        return _fno_cache["symbols"]
    instruments = kite.instruments("NFO")
    names = set()
    for row in instruments:
        if row.get("instrument_type") == "FUT":
            name = (row.get("name") or "").strip()
            if name and name.upper() not in _NON_STOCK_FNO_NAMES:
                names.add(name)
    symbols = sorted(names)
    if symbols:
        _fno_cache["date"] = today
        _fno_cache["symbols"] = symbols
    return symbols


def _load_current_fut_map(kite) -> dict:
    """Maps each F&O-eligible stock's underlying name (e.g. "RELIANCE")
    to its nearest-expiry (current month) NFO futures trading symbol
    (e.g. "RELIANCE26AUGFUT"). Open Interest lives on the futures
    contract, not the underlying cash-market stock, so this is what
    fetch_oi_map() uses to know which instrument to actually query.
    Cached for the day since the near-month contract only rolls over
    once a month, on expiry."""
    today = dt.date.today()
    if _fut_map_cache["date"] == today.isoformat() and _fut_map_cache["map"]:
        return _fut_map_cache["map"]
    instruments = kite.instruments("NFO")
    nearest = {}
    for row in instruments:
        if row.get("instrument_type") != "FUT":
            continue
        name = (row.get("name") or "").strip()
        expiry = row.get("expiry")
        if not name or name.upper() in _NON_STOCK_FNO_NAMES:
            continue
        if not expiry or expiry < today:
            continue
        current = nearest.get(name)
        if current is None or expiry < current[0]:
            nearest[name] = (expiry, row["tradingsymbol"])
    fut_map = {name: symbol for name, (_, symbol) in nearest.items()}
    if fut_map:
        _fut_map_cache["date"] = today.isoformat()
        _fut_map_cache["map"] = fut_map
    return fut_map


def fetch_oi_map(kite, symbols: list) -> dict:
    """Batch-fetches current Open Interest for the given underlying
    stock symbols via their near-month futures contract, one quote()
    call for the whole list (chunked defensively at 400, under Kite's
    500-per-request limit). Returns {symbol: {"oi", "oi_day_high",
    "oi_day_low"}} - symbols with no mapped futures contract, or where
    the quote call fails, are simply omitted rather than breaking the
    scan."""
    fut_map = _load_current_fut_map(kite)
    wanted = {symbol: fut_map[symbol] for symbol in symbols if symbol in fut_map}
    if not wanted:
        return {}
    oi_map = {}
    keys = list(wanted.items())
    for i in range(0, len(keys), 400):
        chunk = keys[i:i + 400]
        instrument_keys = [f"NFO:{fut_symbol}" for _, fut_symbol in chunk]
        try:
            quotes = kite.quote(instrument_keys)
        except Exception as exc:  # noqa: BLE001 - OI is a bonus field, never fatal
            log.warning("OI quote fetch failed: %s", exc)
            continue
        for symbol, fut_symbol in chunk:
            q = quotes.get(f"NFO:{fut_symbol}")
            if q:
                oi_map[symbol] = {
                    "oi": q.get("oi"),
                    "oi_day_high": q.get("oi_day_high"),
                    "oi_day_low": q.get("oi_day_low"),
                }
    return oi_map


def classify_oi_trend(history: list) -> dict:
    """Given a symbol's recent OI samples (oldest first, with the
    just-fetched current value as the last element), classifies how OI
    is moving scan-to-scan:

    - change / change_pct: raw move vs. the previous scan
    - label: "Accelerating" (this move is meaningfully bigger than the
      last one, same direction), "Stable" (steady move, similar size to
      the last one), "Weakening" (same direction but noticeably
      smaller), "Transitional" (direction just flipped), "Flat" (no
      change), or "New" (not enough history yet to compare)
    - unusual: True if this move is much bigger than this symbol's own
      recent scan-to-scan moves (>3x the recent average) - a possible
      spike, tracked independently of the label above.

    Callers own building/persisting `history` (background.py keeps one
    per symbol) - this function is a pure calculation over whatever
    list it's handed."""
    if len(history) < 2:
        return {"change": None, "change_pct": None, "label": "New", "unusual": False}

    current, prev = history[-1], history[-2]
    change = current - prev
    change_pct = (change / prev * 100) if prev else None

    deltas = [history[i] - history[i - 1] for i in range(1, len(history) - 1)]
    recent = deltas[-10:]
    avg_abs = (sum(abs(d) for d in recent) / len(recent)) if recent else 0
    unusual = bool(avg_abs) and abs(change) > 3 * avg_abs

    prev_change = deltas[-1] if deltas else None
    if change == 0:
        label = "Flat"
    elif prev_change is None or prev_change == 0 or (change > 0) != (prev_change > 0):
        # No prior move to compare against, or direction just flipped.
        label = "Stable" if prev_change is None else "Transitional"
    else:
        ratio = abs(change) / abs(prev_change)
        if ratio > 1.3:
            label = "Accelerating"
        elif ratio < 0.7:
            label = "Weakening"
        else:
            label = "Stable"

    return {"change": change, "change_pct": change_pct, "label": label, "unusual": unusual}


def classify_oi_structure(price_chg_pct, oi_chg_pct, threshold: float = 0.05) -> str:
    """Classic 4-quadrant OI+price read, using today's move in each
    (since session open, not scan-to-scan): both up is fresh longs
    being added ("Long Buildup"), both down is existing longs bailing
    ("Long Unwinding"), price down + OI up is fresh shorts ("Short
    Buildup"), price up + OI down is shorts covering ("Short
    Covering"). Moves under `threshold`% in either leg are too small to
    call confidently, so those come back "Neutral"."""
    if price_chg_pct is None or oi_chg_pct is None:
        return None
    price_up = price_chg_pct > threshold
    price_down = price_chg_pct < -threshold
    oi_up = oi_chg_pct > threshold
    oi_down = oi_chg_pct < -threshold
    if price_up and oi_up:
        return "Long Buildup"
    if price_down and oi_up:
        return "Short Buildup"
    if price_up and oi_down:
        return "Short Covering"
    if price_down and oi_down:
        return "Long Unwinding"
    return "Neutral"


def _lookback_days(timeframe: str) -> int:
    # Enough history for indicator warm-up (BB-20/MACD-slow) plus a
    # reasonable scanning window. Doesn't need to respect Kite's
    # per-call date-range limit itself - _fetch_historical_chunked below
    # always splits into safe chunks regardless of how big this is.
    return {
        "15minute": 15,
        "30minute": 30,
        "60minute": 90,
        "4hour": 120,
        "day": 400,
        "week": 730,
    }.get(timeframe, 30)


# Kite enforces a maximum date-range per historical_data() call that
# varies by interval (roughly 30/90/180/365/2000 days for
# minute/3-10minute/15-30minute/60minute/day, per their docs - but
# rather than trust one hardcoded number to always be exactly right,
# every fetch below is always chunked to a conservative size and
# concatenated, so a wide lookback (4-hour's 120-day window, day's
# 400-day window) can never silently fail or get truncated because of
# a per-call range limit we didn't know about.
_HISTORICAL_CHUNK_DAYS = {
    "minute": 25, "3minute": 80, "5minute": 80, "10minute": 80,
    "15minute": 150, "30minute": 150, "60minute": 300, "day": 1800,
}


def _fetch_historical_chunked(kite, instrument_token, from_date, to_date, interval):
    """Fetches historical_data() in safe chunks (see _HISTORICAL_CHUNK_DAYS)
    and concatenates the results. Each chunk gets one retry (short
    backoff) on a transient failure - a brief network blip or rate-limit
    hiccup on one chunk no longer fails that symbol's whole scan, it
    just costs one extra request. A chunk that still fails after the
    retry is logged and skipped rather than aborting the rest of the
    symbol's history."""
    chunk_days = _HISTORICAL_CHUNK_DAYS.get(interval, 80)
    rows = []
    chunk_start = from_date
    while chunk_start < to_date:
        chunk_end = min(chunk_start + dt.timedelta(days=chunk_days), to_date)
        chunk_rows = None
        for attempt in range(2):
            try:
                chunk_rows = kite.historical_data(instrument_token, chunk_start, chunk_end, interval)
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 0:
                    time.sleep(1)
                    continue
                log.warning(
                    "historical_data chunk failed (token=%s interval=%s %s -> %s): %s",
                    instrument_token, interval, chunk_start, chunk_end, exc,
                )
        if chunk_rows:
            rows.extend(chunk_rows)
        chunk_start = chunk_end
    return rows


def fetch_candles(kite, instrument_token, timeframe: str) -> pd.DataFrame:
    to_date = now_ist()
    from_date = to_date - dt.timedelta(days=_lookback_days(timeframe))
    if timeframe == "4hour":
        interval = "60minute"
    elif timeframe == "week":
        interval = "day"
    else:
        interval = timeframe
    data = _fetch_historical_chunked(kite, instrument_token, from_date, to_date, interval)
    df = pd.DataFrame(data)
    if df.empty:
        log.warning(
            "No candles returned for token=%s timeframe=%s interval=%s (%s -> %s)",
            instrument_token, timeframe, interval, from_date, to_date,
        )
        return df
    df = df.rename(columns={"date": "timestamp"}).set_index("timestamp")
    # Defensive against duplicate/out-of-order rows across chunk
    # boundaries (chunk edges are inclusive on both ends, so the
    # boundary candle can come back in two consecutive chunks).
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if timeframe == "week":
        df = df.resample("W").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()
    elif timeframe == "4hour":
        # Kite has no native 4-hour interval, so it's synthesized here by
        # resampling 60-minute candles into 4-hour blocks anchored to the
        # NSE session open (9:15 IST): 9:15-13:15, 13:15-15:30 (short bar).
        raw_count = len(df)
        df = df.resample("4h", origin="start_day", offset="9h15min").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()
        if df.empty:
            log.warning(
                "4-hour resample produced 0 bars from %d raw 60-minute candles for token=%s",
                raw_count, instrument_token,
            )
        else:
            # resample+dropna can't tell "genuinely complete bar" apart
            # from "today's bar that's still filling up" - a bucket with
            # even one 60-minute candle in it survives dropna() even
            # though it's only, say, 1 hour into its own 4-hour window.
            # Without this, compute_signal's "last CLOSED candle" would
            # sometimes actually be a STILL-FORMING one, whose
            # RSI/MACD/EMA state can keep changing bar-to-bar as more
            # 60-minute candles arrive later the same day - exactly the
            # kind of flip-flopping "always giving issues" symptom that
            # was reported. So the last row is dropped whenever it's
            # today's bucket and hasn't actually reached its own close
            # time yet (accounting for the final bucket of the day being
            # a short 13:15-15:30 bar, not a full 4 hours).
            # Kite's own timestamps come back timezone-AWARE (fixed IST
            # offset), while now_ist() is deliberately naive (see its
            # docstring - that's what the historical_data() request
            # params need) - comparing the two directly raises "can't
            # compare offset-naive and offset-aware datetimes". Strip
            # tzinfo from the candle timestamp before comparing; both
            # sides already represent the same IST wall-clock time, so
            # this is safe and isn't an actual timezone conversion.
            last_ts = df.index[-1]
            last_ts_cmp = last_ts.tz_localize(None) if last_ts.tzinfo is not None else last_ts
            now = now_ist()
            if last_ts_cmp.date() == now.date():
                session_close = last_ts_cmp.replace(hour=15, minute=30, second=0, microsecond=0)
                expected_close = min(last_ts_cmp + dt.timedelta(hours=4), session_close)
                if now < expected_close and len(df) > 1:
                    df = df.iloc[:-1]
            if len(df) < 30:
                log.info(
                    "4-hour resample only produced %d closed bars (from %d raw candles) for "
                    "token=%s - indicators may still be warming up",
                    len(df), raw_count, instrument_token,
                )
    return df


def scan_watchlist(kite, timeframe: str = None, with_oi: bool = True) -> list:
    """Returns a list of per-stock result dicts, or an error dict per
    stock if that symbol's data couldn't be fetched (e.g. bad symbol,
    rate limit) - one bad symbol never aborts the whole scan.

    timeframe defaults to the configured settings.TIMEFRAME, but can be
    overridden - e.g. the background scanner also runs a dedicated
    4-hour pass regardless of what timeframe the dashboard is set to.

    with_oi additionally attaches each stock's current Open Interest
    (from its near-month futures contract) via one batched quote() call
    for the whole watchlist, so it costs a single extra request per
    scan rather than one per stock."""
    timeframe = timeframe or settings.TIMEFRAME
    instruments = _load_instrument_map(kite)
    oi_map = fetch_oi_map(kite, settings.WATCHLIST) if with_oi else {}
    results = []
    for symbol in settings.WATCHLIST:
        token = instruments.get(symbol)
        if not token:
            results.append({"symbol": symbol, "error": "symbol not found on NSE"})
            continue
        try:
            df = fetch_candles(kite, token, timeframe)
            if df.empty:
                results.append({"symbol": symbol, "error": "no candle data returned"})
                continue
            signal = compute_signal(df, timeframe)
            signal["symbol"] = symbol
            # Always set these keys (None when unavailable) rather than
            # omitting them - the dashboard template checks "r.oi is not
            # none", which for a genuinely missing dict key evaluates
            # true against Jinja's Undefined and crashes the page. This
            # keeps every result dict shaped the same way regardless of
            # whether OI was fetchable for that symbol.
            oi = oi_map.get(symbol)
            signal["oi"] = oi["oi"] if oi else None
            signal["oi_day_high"] = oi["oi_day_high"] if oi else None
            signal["oi_day_low"] = oi["oi_day_low"] if oi else None
            results.append(signal)
        except Exception as exc:  # noqa: BLE001 - keep scanning the rest of the watchlist
            log.warning("Scan failed for %s: %s", symbol, exc)
            results.append({"symbol": symbol, "error": str(exc)})
    return results


def is_market_open() -> bool:
    now = now_ist()
    if now.weekday() >= 5:  # Sat/Sun
        return False
    open_t = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t
