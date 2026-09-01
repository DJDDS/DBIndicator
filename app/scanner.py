"""
Pulls candles from Kite for each stock in the watchlist and runs the
Scanner confluence logic on them. Kite's historical_data() needs an
instrument_token (not just the trading symbol), so the instrument list
is fetched once and cached in memory.
"""
import datetime as dt
import logging
import threading
import time
from zoneinfo import ZoneInfo

import pandas as pd

from .config import settings
from .config import WATCHLIST_TIMEFRAME
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
_fut_map_cache = {"date": None, "map": {}, "tokens": {}}
_fut_contracts_cache = {"date": None, "map": {}}


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
    # NFO occasionally contains derivative/index-family names that are not
    # tradeable NSE cash stocks (for example NIFTYFPI).  The live stock-F&O
    # universe must have a matching NSE cash instrument because every scanner
    # row starts from that underlying's NSE candles.  Filtering against the
    # same cached NSE instrument map used by scan_watchlist removes stale or
    # non-stock derivatives before they can become a permanent scan error.
    cash_symbols = set(_load_instrument_map(kite))
    symbols = sorted(name for name in names if name in cash_symbols)
    if symbols:
        _fno_cache["date"] = today
        _fno_cache["symbols"] = symbols
    return symbols


def _load_fut_contracts_map(kite) -> dict:
    """Underlying -> first three live stock-futures expiries, sorted nearest first."""
    today = dt.date.today()
    key = today.isoformat()
    if _fut_contracts_cache["date"] == key and _fut_contracts_cache["map"]:
        return _fut_contracts_cache["map"]
    grouped = {}
    for row in kite.instruments("NFO"):
        if row.get("instrument_type") != "FUT":
            continue
        name = (row.get("name") or "").strip()
        expiry = row.get("expiry")
        if not name or name.upper() in _NON_STOCK_FNO_NAMES or not expiry or expiry < today:
            continue
        grouped.setdefault(name, []).append({
            "expiry": expiry, "tradingsymbol": row.get("tradingsymbol"),
            "instrument_token": row.get("instrument_token"),
        })
    mapped = {name: sorted(rows, key=lambda x: x["expiry"])[:3] for name, rows in grouped.items()}
    _fut_contracts_cache["date"] = key
    _fut_contracts_cache["map"] = mapped
    return mapped


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
            nearest[name] = (expiry, row["tradingsymbol"], row.get("instrument_token"))
    fut_map = {name: symbol for name, (_, symbol, _t) in nearest.items()}
    if fut_map:
        _fut_map_cache["date"] = today.isoformat()
        _fut_map_cache["map"] = fut_map
        _fut_map_cache["tokens"] = {n: tok for n, (_, _s, tok) in nearest.items() if tok}
    return fut_map


def _fut_token_map(kite) -> dict:
    """Underlying name -> nearest-expiry futures INSTRUMENT TOKEN.

    fetch_oi_map works off trading symbols because kite.quote() takes
    symbols, but historical_data() takes a token, so the token is captured
    alongside during the same daily instruments() walk rather than paying
    for a second one."""
    _load_current_fut_map(kite)
    return _fut_map_cache.get("tokens") or {}


# --------------------------------------------------------------------------
# Daily OI history - the baseline behind every z-score in early_signal.py.
#
# The previous OI panel built its baseline by sampling live OI into an
# in-memory buffer every scan, which meant it could not compute a 30-minute
# acceleration until 60 minutes of samples had accumulated. It was blind
# from 09:15 to about 10:15 every single day - precisely the window where a
# day's trend gets set - and any redeploy reset the buffer and re-imposed
# the blackout mid-session.
#
# Kite will simply give us daily OI history for a futures contract, so the
# baseline exists the moment the app starts. continuous=True stitches the
# series across expiries too, which fixes a second bug: without it, the
# session after every monthly expiry compared the NEW contract's small
# opening OI against the OLD contract's settled OI and printed a huge
# spurious collapse.
#
# Fetched once per trading day and cached, because daily history only gains
# one bar a day. Throttled, because this is one call per symbol and Kite
# allows ~3 a second - an unthrottled sweep of a 190-name F&O universe
# would trip the rate limiter and take the live scan down with it.
# --------------------------------------------------------------------------

# How each timeframe's OI baseline is built. `interval` is what Kite is
# actually asked for; `resample` (4-hour only) is applied afterwards because
# Kite has no native 4-hour interval - same synthesis fetch_candles already
# does for price, anchored identically to the 09:15 session open so the OI
# bars line up bar-for-bar with the price bars they are judged against.
#
# `intraday` tells early_signal to exclude overnight transitions from the
# baseline. See _pct_changes there: OI genuinely re-forms between sessions,
# so the first bar of each day carries a change far larger than any
# within-session move, and leaving those in makes the standard deviation so
# wide that real intraday builds stop registering.
# `continuous` is per-timeframe because Kite does NOT accept it on every
# interval - a 15-minute request with continuous=True comes back "invalid
# interval for continuous data" and the whole intraday baseline silently
# ends up empty. It is only set where it is both accepted and needed.
#
# Losing it on the intraday specs costs less than it sounds. continuous
# exists to stitch a series across expiries; without it a request returns
# only that contract's own life. For the DAILY baseline that matters - 120
# days spans several expiries and the roll would otherwise print a large
# spurious OI collapse each month. For an intraday baseline it does not:
# a near-month contract already gives roughly 25 sessions, which at 25 bars
# a session is ~600 observations, far past what a stable sigma needs. And
# because a single contract is one continuous book, there is no roll
# artefact in the series at all.
#
# The honest cost: for a few sessions right after an expiry the new
# contract has too little history, so intraday rows get no OI baseline and
# drop off the intraday shortlist rather than being scored on nothing.
OI_HISTORY_SPEC = {
    "day":       {"interval": "day",      "days": 120, "resample": None, "intraday": False, "continuous": True},
    "15minute":  {"interval": "15minute", "days": 45,  "resample": None, "intraday": True,  "continuous": False},
    "4hour":     {"interval": "60minute", "days": 120, "resample": "4h", "intraday": True,  "continuous": False},
}

OI_HISTORY_DAYS = OI_HISTORY_SPEC["day"]["days"]

# Kite allows roughly 3 historical requests a second. This sweep is one call
# per symbol, and there are now THREE of them (daily, 15-minute, 4-hour) run
# by two independent threads - on the first scan of a day they can all come
# due at once, and three unthrottled sweeps plus the ordinary price scans
# would sail past the limit and start failing symbols. Two guards:
# a gentler per-call pause, and a lock so only one sweep is ever in flight.
_OI_HISTORY_THROTTLE_SECONDS = 0.5
_oi_history_lock = threading.Lock()

# Cached per (timeframe, date) - a day's history only gains bars as the
# session runs, and re-fetching 190 symbols every scan would be absurd.
_oi_history_cache = {}


def _resample_oi(rows, rule):
    """60-minute OI rows -> 4-hour, taking the LAST value in each bucket.

    This is the one place where OI must NOT be treated like volume. Volume
    is a flow and resamples with `sum`; Open Interest is a LEVEL - the count
    of contracts currently open - so summing it across four hourly bars
    would report roughly four times the real position. The correct
    aggregation is `last`, exactly as `close` is for price."""
    if not rows:
        return None
    idx = pd.to_datetime([r["date"] for r in rows])
    ser = pd.Series([r.get("oi") for r in rows], index=idx, dtype="float64").dropna()
    if ser.empty:
        return None
    if rule:
        ser = ser.resample(rule, origin="start_day", offset="9h15min").last().dropna()
    return ser


def fetch_oi_history(kite, symbols, timeframe="day", throttle=None, days_override=None, progress_cb=None):
    """{symbol: pandas Series of OI indexed by bar timestamp} for a timeframe.

    Never raises: a symbol Kite refuses simply gets no entry, and every
    downstream consumer treats a missing baseline as "cannot score this row"
    rather than guessing. That is deliberate - a wrong OI baseline is far
    more dangerous than an absent one, because it produces a confident
    z-score built on nothing."""
    spec = OI_HISTORY_SPEC.get(timeframe)
    if spec is None:
        return {}
    today = dt.date.today().isoformat()
    requested_days = int(days_override) if days_override is not None else int(spec["days"])
    # Keep research windows separate from the live cache. If a 120-day live
    # cache is reused by a 365-day backtest, most historical signal bars have
    # no OI and the gate is never genuinely tested.
    key = (timeframe, today, requested_days)
    entry = _oi_history_cache.get(key)
    if entry is None:
        entry = _oi_history_cache[key] = {}
        # drop other days so the cache cannot grow without bound
        for k in [k for k in _oi_history_cache if len(k) >= 2 and k[1] != today]:
            _oi_history_cache.pop(k, None)

    todo = [sym for sym in symbols if sym not in entry]
    if not todo:
        if progress_cb:
            progress_cb(len(symbols), len(symbols), None)
        return dict(entry)

    # Serialise the sweeps. Whichever thread gets here first does the work;
    # the others wait, then re-check the cache below and usually find their
    # symbols already fetched, so waiting costs nothing but a lock.
    with _oi_history_lock:
        todo = [sym for sym in symbols if sym not in entry]
        if not todo:
            if progress_cb:
                progress_cb(len(symbols), len(symbols), None)
            return dict(entry)
        spec_for_run = dict(spec)
        spec_for_run["days"] = requested_days
        return _sweep_oi_history(kite, todo, entry, spec_for_run, timeframe, throttle, progress_cb=progress_cb, total_symbols=len(symbols))


def _sweep_oi_history(kite, todo, entry, spec, timeframe, throttle, progress_cb=None, total_symbols=None):
    tokens = _fut_token_map(kite)
    to_date = now_ist()
    from_date = to_date - dt.timedelta(days=spec["days"])
    pause = _OI_HISTORY_THROTTLE_SECONDS if throttle is None else throttle

    fetched = 0
    total = int(total_symbols or (len(entry) + len(todo)))
    done = max(0, total - len(todo))
    if progress_cb:
        progress_cb(done, total, None)
    for sym in todo:
        # Emit a heartbeat before the network call so the UI shows the exact
        # symbol currently being requested even if Kite is slow on that call.
        if progress_cb:
            progress_cb(done, total, sym)
        token = tokens.get(sym)
        try:
            if token:
                rows = _fetch_historical_chunked(kite, token, from_date, to_date,
                                                 spec["interval"], oi=True,
                                                 continuous=spec.get("continuous", False))
                ser = _resample_oi(rows, spec["resample"])
                if ser is not None and len(ser) >= 3:
                    entry[sym] = ser
                    fetched += 1
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the sweep
            log.debug("OI history failed for %s (%s): %s", sym, timeframe, exc)
        finally:
            done += 1
            if progress_cb:
                progress_cb(done, total, sym)
        if pause:
            time.sleep(pause)

    if fetched:
        log.info("Fetched %s OI history for %d symbol(s); %d cached total",
                 timeframe, fetched, len(entry))
    return dict(entry)


def oi_is_intraday(timeframe):
    spec = OI_HISTORY_SPEC.get(timeframe)
    return bool(spec and spec["intraday"])


def oi_history_status(timeframe="day"):
    today = dt.date.today().isoformat()
    spec = OI_HISTORY_SPEC.get(timeframe) or {}
    entry = _oi_history_cache.get((timeframe, today, int(spec.get("days", 0)))) or {}
    return {"date": today, "timeframe": timeframe, "symbols": len(entry)}



_nifty_fut_cache = {"date": None, "token": None, "symbol": None}


def _load_nifty_future(kite):
    """Resolves the nearest-expiry (current month) NIFTY 50 INDEX futures
    contract - e.g. "NIFTY26AUGFUT" - (instrument_token, tradingsymbol),
    or (None, None) if unresolved. Deliberately separate from
    _load_current_fut_map above, which explicitly EXCLUDES index names
    (NIFTY included, via _NON_STOCK_FNO_NAMES) since that map is only
    ever used for STOCK Open Interest.

    scalper.py uses this rather than the raw NIFTY 50 index itself
    (scanner._load_index_token) because Kite reports 0 volume on index
    historical data - confirmed earlier in this app's own backtest work
    (a solo Relative Volume backtest on "NIFTY 50" produces zero trades)
    - which would permanently zero out the Relative Volume parameter of
    the scalp engine. The futures contract is a real, tradeable
    instrument with genuine volume, and realistically what you'd
    actually place a NIFTY 50 scalp trade through anyway. Cached for the
    day, same reasoning as _load_current_fut_map (the near-month
    contract only rolls over once a month, on expiry)."""
    today = dt.date.today()
    if _nifty_fut_cache["date"] == today.isoformat() and _nifty_fut_cache["token"]:
        return _nifty_fut_cache["token"], _nifty_fut_cache["symbol"]
    try:
        instruments = kite.instruments("NFO")
    except Exception as exc:  # noqa: BLE001
        log.warning("NIFTY future instrument lookup failed: %s", exc)
        return None, None
    nearest = None
    for row in instruments:
        if row.get("instrument_type") != "FUT":
            continue
        if (row.get("name") or "").strip().upper() != "NIFTY":
            continue
        expiry = row.get("expiry")
        if not expiry or expiry < today:
            continue
        if nearest is None or expiry < nearest[0]:
            nearest = (expiry, row["instrument_token"], row["tradingsymbol"])
    if nearest is None:
        return None, None
    _, token, symbol = nearest
    _nifty_fut_cache["date"] = today.isoformat()
    _nifty_fut_cache["token"] = token
    _nifty_fut_cache["symbol"] = symbol
    return token, symbol


def fetch_oi_map(kite, symbols: list) -> dict:
    """Fetch near/next/far stock-futures OI and an aggregate total.

    ``oi`` remains the near-month value for compatibility with the historical
    near-contract z-score baseline.  ``oi_total`` is the sum of the first
    three expiries and is what the live 15/30/60-minute acceleration engine
    should sample, because it is resilient to rollover transfer.
    """
    contracts_map = _load_fut_contracts_map(kite)
    requested = []
    owner = {}
    for symbol in symbols:
        for pos, c in enumerate(contracts_map.get(symbol, [])[:3]):
            ts = c.get("tradingsymbol")
            if not ts:
                continue
            key = f"NFO:{ts}"
            requested.append(key)
            owner[key] = (symbol, pos, c)
    if not requested:
        return {}
    quotes = {}
    for i in range(0, len(requested), 400):
        chunk = requested[i:i + 400]
        try:
            quotes.update(kite.quote(chunk))
        except Exception as exc:  # noqa: BLE001
            log.warning("OI quote fetch failed: %s", exc)
    per_symbol = {}
    for key, (symbol, pos, c) in owner.items():
        q = quotes.get(key)
        if not q or q.get("oi") is None:
            continue
        d = per_symbol.setdefault(symbol, {"contracts": []})
        item = {
            "expiry": c.get("expiry"), "tradingsymbol": c.get("tradingsymbol"),
            "oi": q.get("oi"), "oi_day_high": q.get("oi_day_high"), "oi_day_low": q.get("oi_day_low"),
            "last_price": q.get("last_price"),
        }
        d["contracts"].append((pos, item))
    out = {}
    for symbol, d in per_symbol.items():
        ordered = [item for _pos, item in sorted(d["contracts"], key=lambda x: x[0])]
        ois = [x.get("oi") for x in ordered if x.get("oi") is not None]
        if not ordered or not ois:
            continue
        near = ordered[0]
        # Historical order-book depth is not available from Kite.  Carry the
        # *live* near-futures five-level depth only as shadow research fields;
        # these must never make a candidate eligible until forward testing
        # clears the promotion benchmark.
        near_key = next((k for k, (sym, pos, _c) in owner.items() if sym == symbol and pos == 0), None)
        near_quote = quotes.get(near_key) if near_key else None
        try:
            from .stock_in_play import depth_shadow_metrics
            depth = depth_shadow_metrics(near_quote)
        except Exception:  # pragma: no cover - defensive isolation of shadow metrics
            depth = {"depth_imbalance": None, "spread_bps": None, "microprice_bias_bps": None}
        out[symbol] = {
            "oi": near.get("oi"),
            "oi_near": near.get("oi"),
            "oi_next": ordered[1].get("oi") if len(ordered) > 1 else None,
            "oi_far": ordered[2].get("oi") if len(ordered) > 2 else None,
            "oi_total": sum(ois),
            "fut_price_near": near.get("last_price"),
            "fut_price_next": ordered[1].get("last_price") if len(ordered) > 1 else None,
            "fut_price_far": ordered[2].get("last_price") if len(ordered) > 2 else None,
            "oi_day_high": near.get("oi_day_high"),
            "oi_day_low": near.get("oi_day_low"),
            "contracts": ordered,
            "fut_depth_imbalance": depth.get("depth_imbalance"),
            "fut_spread_bps": depth.get("spread_bps"),
            "fut_microprice_bias_bps": depth.get("microprice_bias_bps"),
            "fut_depth_shadow_only": True,
            "microstructure_shadow_only": True,
        }
    return out


def _oi_sample_at_or_before(history: list, cutoff: dt.datetime):
    """history is a list of {"ts": ISO-8601 string, "oi": number} dicts,
    oldest first. Returns the OI value of the most recent sample whose
    timestamp is <= cutoff, or None if every sample is newer than
    cutoff (not enough history yet)."""
    found = None
    for entry in history:
        ts = entry.get("ts")
        if not ts:
            continue
        try:
            sample_dt = dt.datetime.fromisoformat(ts)
        except ValueError:
            continue
        if sample_dt <= cutoff:
            found = entry.get("oi")
        else:
            break
    return found


def _pct_change(current, prior):
    if current is None or prior is None or prior == 0:
        return None
    return (current - prior) / prior * 100


def compute_oi_acceleration(history: list, now: "dt.datetime") -> dict:
    """Precise, time-windowed OI acceleration - replaces the old
    scan-to-scan-only comparison, which conflated "3 minutes apart" and
    "30 minutes apart" into the same signal depending on scan interval.

    history: this symbol's OI samples so far today, oldest first, as
    {"ts": ISO datetime string, "oi": number} dicts (the just-fetched
    current value is the last element). Callers own building/persisting
    this list (background.py keeps one per symbol).

    Rolling-window changes (each looks back from `now`, using the
    closest sample at or before that point - scans don't land on exact
    minute boundaries):
      - chg_15m / chg_30m / chg_60m: OI % change from ~N minutes ago to
        now - chg_30m is the primary early-alert window, chg_60m the
        stronger confirmation window, chg_15m the fastest/noisiest.
      - chg_prior_30m: OI % change over the 30-60-minutes-ago window
        (i.e. the 30-minute window immediately BEFORE the latest one) -
        used only to compute acceleration below.
      - chg_prior_60m: OI % change over the 60-120-minutes-ago window -
        a longer-baseline check on whether a buildup is strengthening
        or fading over the last couple of hours, not just the last
        half-hour.

    acceleration = chg_30m - chg_prior_30m, in percentage points: how
    much faster (or slower) OI is building right now vs the 30 minutes
    immediately before. A stock can show a big whole-day OI number while
    actually unwinding in the latest window - for a fresh positional
    entry, this recent-direction read matters more than the cumulative
    one alone.

    accel_label buckets that percentage-point figure:
      > +2.00        Strong acceleration
      +0.5 to +1.00  Moderate acceleration
      -0.30 to +0.30 Stable
      < -0.30        Weakening
      < -2.00        Possible exit / unwinding
    (the 1.00-2.00 and 0.30-0.5 gaps aren't specified, so they're folded
    into the nearer/stronger neighbouring bucket rather than left
    unlabeled - see the elif order below.)"""
    if not history:
        return {
            "chg_15m": None, "chg_30m": None, "chg_60m": None,
            "chg_prior_30m": None, "chg_prior_60m": None,
            "acceleration": None, "accel_label": None,
        }

    current = history[-1].get("oi")
    oi_15m_ago = _oi_sample_at_or_before(history, now - dt.timedelta(minutes=15))
    oi_30m_ago = _oi_sample_at_or_before(history, now - dt.timedelta(minutes=30))
    oi_60m_ago = _oi_sample_at_or_before(history, now - dt.timedelta(minutes=60))
    oi_120m_ago = _oi_sample_at_or_before(history, now - dt.timedelta(minutes=120))

    chg_15m = _pct_change(current, oi_15m_ago)
    chg_30m = _pct_change(current, oi_30m_ago)
    chg_60m = _pct_change(current, oi_60m_ago)
    # "Prior" windows are changes BETWEEN two past points, not vs. now.
    chg_prior_30m = _pct_change(oi_30m_ago, oi_60m_ago)
    chg_prior_60m = _pct_change(oi_60m_ago, oi_120m_ago)

    acceleration = None
    if chg_30m is not None and chg_prior_30m is not None:
        acceleration = chg_30m - chg_prior_30m

    accel_label = None
    if acceleration is not None:
        if acceleration > 2.00:
            accel_label = "Strong acceleration"
        elif acceleration >= 0.5:
            accel_label = "Moderate acceleration"
        elif acceleration >= -0.30:
            accel_label = "Stable"
        elif acceleration >= -2.00:
            accel_label = "Weakening"
        else:
            accel_label = "Possible exit/unwinding"

    return {
        "chg_15m": chg_15m, "chg_30m": chg_30m, "chg_60m": chg_60m,
        "chg_prior_30m": chg_prior_30m, "chg_prior_60m": chg_prior_60m,
        "acceleration": acceleration, "accel_label": accel_label,
    }


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
        "3minute": 6,   # ~125 bars/session on 3-min candles - 6 days is
                         # already 700+ bars, plenty for BB-20/MACD warm-up
                         # without pulling more than scalper.py needs
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


def _fetch_historical_chunked(kite, instrument_token, from_date, to_date, interval,
                              oi=False, continuous=False):
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
                chunk_rows = kite.historical_data(instrument_token, chunk_start, chunk_end,
                                                  interval, continuous=continuous, oi=oi)
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
        elif len(df) < 30:
            # The last row here can be a still-forming bucket (e.g. the
            # 13:15 bar before market close at 15:30) - that's intentional:
            # every other timeframe already shows its current in-progress
            # candle rather than waiting for it to close, so 4-hour does
            # the same for consistency ("live" data over a stable-but-stale
            # reading). Its RSI/MACD/EMA state can shift a bit as later
            # 60-minute candles arrive the same day, same as any other
            # intraday timeframe's most recent candle.
            log.info(
                "4-hour resample only produced %d bars (from %d raw candles) for "
                "token=%s - indicators may still be warming up",
                len(df), raw_count, instrument_token,
            )
    return df


def scan_watchlist(kite, timeframe: str = None, with_oi: bool = True, symbols=None) -> list:
    """Returns a list of per-stock result dicts, or an error dict per
    stock if that symbol's data couldn't be fetched (e.g. bad symbol,
    rate limit) - one bad symbol never aborts the whole scan.

    timeframe defaults to config.WATCHLIST_TIMEFRAME (daily), but can be
    overridden - e.g. the background scanner also runs a dedicated
    4-hour pass regardless of what timeframe the dashboard is set to.

    with_oi additionally attaches each stock's current Open Interest
    (from its near-month futures contract) via one batched quote() call
    for the whole watchlist, so it costs a single extra request per
    scan rather than one per stock."""
    timeframe = timeframe or WATCHLIST_TIMEFRAME
    instruments = _load_instrument_map(kite)
    universe = list(symbols) if symbols is not None else list(settings.WATCHLIST)
    oi_map = fetch_oi_map(kite, universe) if with_oi else {}
    results = []
    for symbol in universe:
        token = instruments.get(symbol)
        if not token:
            results.append({"symbol": symbol, "error": "symbol not found on NSE", "error_stage": "instrument_lookup"})
            continue
        stage = "candle_fetch"
        try:
            df = fetch_candles(kite, token, timeframe)
            if df.empty:
                results.append({"symbol": symbol, "error": "no candle data returned", "error_stage": "candle_fetch"})
                continue
            stage = "signal_compute"
            signal = compute_signal(df, timeframe, now=now_ist())
            signal["symbol"] = symbol
            stage = "oi_attach"
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
            signal["oi_near"] = oi.get("oi_near") if oi else None
            signal["oi_next"] = oi.get("oi_next") if oi else None
            signal["oi_far"] = oi.get("oi_far") if oi else None
            signal["oi_total"] = oi.get("oi_total") if oi else None
            signal["oi_contracts"] = oi.get("contracts") if oi else None
            signal["fut_price_near"] = oi.get("fut_price_near") if oi else None
            signal["fut_price_next"] = oi.get("fut_price_next") if oi else None
            signal["fut_price_far"] = oi.get("fut_price_far") if oi else None
            signal["fut_depth_imbalance"] = oi.get("fut_depth_imbalance") if oi else None
            signal["fut_spread_bps"] = oi.get("fut_spread_bps") if oi else None
            signal["fut_microprice_bias_bps"] = oi.get("fut_microprice_bias_bps") if oi else None
            signal["fut_depth_shadow_only"] = True
            results.append(signal)
        except Exception as exc:  # noqa: BLE001 - keep scanning the rest of the watchlist
            log.warning("Scan failed for %s during %s: %s", symbol, stage, exc)
            results.append({"symbol": symbol, "error": str(exc), "error_stage": stage})
    return results


def fetch_index_returns(kite, timeframe: str = None, lookbacks=(20, 10)):
    """The index's own N-bar returns, for the relative-strength axis.

    Relative strength needs one number per lookback for the whole market,
    not a series per stock: each row already carries its own ret_20/ret_10
    from compute_signal, so the comparison is a subtraction. Returns
    {lookback: pct} with None for any window there is not enough history
    for. Never raises - a failed index fetch just means no relative-
    strength component, which lowers coverage rather than inventing a
    reading."""
    timeframe = timeframe or WATCHLIST_TIMEFRAME
    out = {n: None for n in lookbacks}
    try:
        token = _load_index_token(kite, _INDEX_SYMBOL)
        if not token:
            return out
        df = fetch_candles(kite, token, timeframe)
        if df.empty or "close" not in df.columns:
            return out
        close = df["close"].dropna()
        for n in lookbacks:
            if len(close) > n:
                past = float(close.iloc[-(n + 1)])
                if past > 0:
                    out[n] = round((float(close.iloc[-1]) / past - 1.0) * 100.0, 2)
    except Exception as exc:  # noqa: BLE001 - never break a scan over the index
        log.debug("Index returns unavailable: %s", exc)
    return out


_INDEX_SYMBOL = "NIFTY 50"

# Which exchange each supported index's own instrument dump lives under -
# NIFTY 50 is an NSE index; SENSEX is a BSE index, so it's never in the
# NSE dump at all regardless of segment. Used by both the live Index/
# Market-trend filter (NIFTY 50 only) and the Backtest page's optional
# "also backtest NIFTY 50 / SENSEX" checkboxes (backtest.INDEX_SYMBOLS).
INDEX_EXCHANGES = {
    "NIFTY 50": "NSE",
    "SENSEX": "BSE",
}
_index_token_cache = {}


def _load_index_token(kite, tradingsymbol: str = _INDEX_SYMBOL):
    """Resolves an index's instrument token (e.g. "NIFTY 50", "SENSEX")
    for the Index/Market-trend filter (background._apply_index_filter)
    and the Backtest page's optional index symbols. Indices show up
    under segment "INDICES" on their own exchange (see INDEX_EXCHANGES
    above) rather than under a tradeable-equity segment like "NSE" -
    _load_instrument_map above deliberately excludes those (it's built
    for tradeable equities), so this does its own separate, permissive
    lookup by tradingsymbol across the whole relevant exchange dump
    regardless of segment. Cached in memory for the life of the
    process, same as _instrument_cache - an index's token never
    changes."""
    if tradingsymbol in _index_token_cache:
        return _index_token_cache[tradingsymbol]
    exchange = INDEX_EXCHANGES.get(tradingsymbol, "NSE")
    try:
        instruments = kite.instruments(exchange)
    except Exception as exc:  # noqa: BLE001 - the index filter is a bonus feature, never fatal
        log.warning("Could not fetch %s instrument list for index lookup: %s", exchange, exc)
        return None
    token = None
    for row in instruments:
        if row.get("tradingsymbol") == tradingsymbol:
            token = row.get("instrument_token")
            break
    if token:
        _index_token_cache[tradingsymbol] = token
    else:
        log.warning("Index instrument %r not found in %s instrument dump", tradingsymbol, exchange)
    return token


def fetch_instrument_direction(kite, tradingsymbol: str, timeframe: str):
    """Reads ANY index instrument's own confluence direction on the
    given timeframe - the shared implementation behind fetch_index_
    direction (NIFTY 50) and fetch_sector_directions (sector indices
    like NIFTY BANK/NIFTY IT below). Returns (direction, close,
    chg_pct) - any/all of which can be None (token unresolved, no
    candle data yet, or not enough history for the indicators to warm
    up) - callers should treat None as "no opinion available this
    scan", not an error. Reuses the exact same fetch_candles/
    compute_signal path used for every watchlist stock, so this costs
    exactly one extra Kite API call per scan cycle (two the first time,
    while the token is still being resolved) rather than a parallel/
    duplicate code path. Deliberately swallows every exception itself
    (rather than letting the caller's try/except handle it) so a
    transient fetch hiccup for one instrument can never cost the rest
    of that scan cycle's results."""
    try:
        token = _load_index_token(kite, tradingsymbol)
        if not token:
            return None, None, None
        df = fetch_candles(kite, token, timeframe)
        if df.empty:
            return None, None, None
        signal = compute_signal(df, timeframe)
        if "error" in signal:
            return None, None, None
        close = signal.get("close")
        chg_pct = None
        if len(df) >= 2 and close is not None:
            prev_close = float(df["close"].iloc[-2])
            if prev_close:
                chg_pct = round((close - prev_close) / prev_close * 100, 2)
        return signal.get("direction"), close, chg_pct
    except Exception as exc:  # noqa: BLE001 - see docstring
        log.warning("Instrument direction fetch failed for %r: %s", tradingsymbol, exc)
        return None, None, None


def fetch_index_direction(kite, timeframe: str):
    """Reads NIFTY 50's own confluence direction on the given timeframe,
    for the Index/Market-trend filter. Thin wrapper over
    fetch_instrument_direction (kept as its own function/signature so
    every existing caller - background.py's main scan loop and the
    multi-timeframe panel - is untouched)."""
    return fetch_instrument_direction(kite, _INDEX_SYMBOL, timeframe)


# --------------------------------------------------------------------------
# Sector relative strength (NEXT_HORIZON_RESEARCH.md Finding 5): maps each
# watchlist symbol to its NSE sectoral index, so the live dashboard can
# compare a stock's own direction against its sector's current confluence
# direction - genuinely different information from RSI/MACD/EMA-BB (all
# re-expressions of the stock's OWN closing price), since this is about
# the stock's context instead. Kite Connect doesn't expose any stock-to-
# sector classification, so this map is hand-built from NSE's own index
# constituent lists, covering a broad NSE F&O universe (not just the
# default 20-symbol watchlist) so most symbols a user might add later
# already resolve. A symbol not in this map simply gets sector_direction
# =None downstream (background._apply_sector_filter) - the same "None
# means agree, never blocks" convention used by every other optional
# gate in this app - so an incomplete map degrades gracefully rather
# than breaking anything.
#
# The index tradingsymbols below are NSE's standard sectoral indices, as
# published under Kite Connect's own "INDICES" segment. If a particular
# one doesn't resolve on your account's instrument dump (an unusual
# renaming, or an index not carried), fetch_sector_directions below
# swallows that failure per-sector exactly like fetch_index_direction
# does for NIFTY 50 - every stock mapped to it just reads
# sector_direction=None instead of erroring.
# --------------------------------------------------------------------------

SYMBOL_SECTOR_MAP = {
    # NIFTY BANK
    "HDFCBANK": "NIFTY BANK", "ICICIBANK": "NIFTY BANK", "KOTAKBANK": "NIFTY BANK",
    "AXISBANK": "NIFTY BANK", "SBIN": "NIFTY BANK", "INDUSINDBK": "NIFTY BANK",
    "BANKBARODA": "NIFTY BANK", "PNB": "NIFTY BANK", "AUBANK": "NIFTY BANK",
    "FEDERALBNK": "NIFTY BANK", "IDFCFIRSTB": "NIFTY BANK", "BANDHANBNK": "NIFTY BANK",
    "CANBK": "NIFTY BANK", "UNIONBANK": "NIFTY BANK", "RBLBANK": "NIFTY BANK",
    "IDBI": "NIFTY BANK", "INDIANB": "NIFTY BANK", "BANKINDIA": "NIFTY BANK",
    "YESBANK": "NIFTY BANK",
    # NIFTY IT
    "TCS": "NIFTY IT", "INFY": "NIFTY IT", "WIPRO": "NIFTY IT", "HCLTECH": "NIFTY IT",
    "TECHM": "NIFTY IT", "LTIM": "NIFTY IT", "MPHASIS": "NIFTY IT", "COFORGE": "NIFTY IT",
    "PERSISTENT": "NIFTY IT", "LTTS": "NIFTY IT", "OFSS": "NIFTY IT", "KPITTECH": "NIFTY IT",
    "TATAELXSI": "NIFTY IT",
    # NIFTY AUTO
    "MARUTI": "NIFTY AUTO", "TATAMOTORS": "NIFTY AUTO", "M&M": "NIFTY AUTO",
    "BAJAJ-AUTO": "NIFTY AUTO", "EICHERMOT": "NIFTY AUTO", "HEROMOTOCO": "NIFTY AUTO",
    "TVSMOTOR": "NIFTY AUTO", "ASHOKLEY": "NIFTY AUTO", "BHARATFORG": "NIFTY AUTO",
    "BALKRISIND": "NIFTY AUTO", "MRF": "NIFTY AUTO", "EXIDEIND": "NIFTY AUTO",
    "MOTHERSON": "NIFTY AUTO", "BOSCHLTD": "NIFTY AUTO", "APOLLOTYRE": "NIFTY AUTO",
    "AMARAJABAT": "NIFTY AUTO", "SONACOMS": "NIFTY AUTO", "UNOMINDA": "NIFTY AUTO",
    # NIFTY PHARMA
    "SUNPHARMA": "NIFTY PHARMA", "DRREDDY": "NIFTY PHARMA", "CIPLA": "NIFTY PHARMA",
    "DIVISLAB": "NIFTY PHARMA", "AUROPHARMA": "NIFTY PHARMA", "LUPIN": "NIFTY PHARMA",
    "TORNTPHARM": "NIFTY PHARMA", "ALKEM": "NIFTY PHARMA", "BIOCON": "NIFTY PHARMA",
    "GLENMARK": "NIFTY PHARMA", "ZYDUSLIFE": "NIFTY PHARMA", "LAURUSLABS": "NIFTY PHARMA",
    "IPCALAB": "NIFTY PHARMA", "GLAND": "NIFTY PHARMA", "ABBOTINDIA": "NIFTY PHARMA",
    "SYNGENE": "NIFTY PHARMA",
    # NIFTY FMCG
    "HINDUNILVR": "NIFTY FMCG", "ITC": "NIFTY FMCG", "NESTLEIND": "NIFTY FMCG",
    "BRITANNIA": "NIFTY FMCG", "TATACONSUM": "NIFTY FMCG", "DABUR": "NIFTY FMCG",
    "GODREJCP": "NIFTY FMCG", "MARICO": "NIFTY FMCG", "COLPAL": "NIFTY FMCG",
    "UBL": "NIFTY FMCG", "VBL": "NIFTY FMCG", "EMAMILTD": "NIFTY FMCG",
    "PGHH": "NIFTY FMCG", "RADICO": "NIFTY FMCG",
    # NIFTY METAL
    "TATASTEEL": "NIFTY METAL", "JSWSTEEL": "NIFTY METAL", "HINDALCO": "NIFTY METAL",
    "VEDL": "NIFTY METAL", "JINDALSTEL": "NIFTY METAL", "SAIL": "NIFTY METAL",
    "NMDC": "NIFTY METAL", "NATIONALUM": "NIFTY METAL", "HINDZINC": "NIFTY METAL",
    "APLAPOLLO": "NIFTY METAL", "JSL": "NIFTY METAL", "HINDCOPPER": "NIFTY METAL",
    "RATNAMANI": "NIFTY METAL",
    # NIFTY ENERGY
    "RELIANCE": "NIFTY ENERGY", "ONGC": "NIFTY ENERGY", "NTPC": "NIFTY ENERGY",
    "POWERGRID": "NIFTY ENERGY", "COALINDIA": "NIFTY ENERGY", "BPCL": "NIFTY ENERGY",
    "IOC": "NIFTY ENERGY", "GAIL": "NIFTY ENERGY", "TATAPOWER": "NIFTY ENERGY",
    "ADANIGREEN": "NIFTY ENERGY", "ADANIPOWER": "NIFTY ENERGY", "ADANIENSOL": "NIFTY ENERGY",
    "HINDPETRO": "NIFTY ENERGY", "PETRONET": "NIFTY ENERGY", "IGL": "NIFTY ENERGY",
    "OIL": "NIFTY ENERGY", "NHPC": "NIFTY ENERGY", "SJVN": "NIFTY ENERGY",
    # NIFTY REALTY
    "DLF": "NIFTY REALTY", "GODREJPROP": "NIFTY REALTY", "OBEROIRLTY": "NIFTY REALTY",
    "PRESTIGE": "NIFTY REALTY", "PHOENIXLTD": "NIFTY REALTY", "LODHA": "NIFTY REALTY",
    "BRIGADE": "NIFTY REALTY", "SOBHA": "NIFTY REALTY", "MAHLIFE": "NIFTY REALTY",
    # NIFTY FIN SERVICE (non-bank financials)
    "BAJFINANCE": "NIFTY FIN SERVICE", "BAJAJFINSV": "NIFTY FIN SERVICE",
    "HDFCLIFE": "NIFTY FIN SERVICE", "SBILIFE": "NIFTY FIN SERVICE",
    "ICICIGI": "NIFTY FIN SERVICE", "ICICIPRULI": "NIFTY FIN SERVICE",
    "SHRIRAMFIN": "NIFTY FIN SERVICE", "CHOLAFIN": "NIFTY FIN SERVICE",
    "PFC": "NIFTY FIN SERVICE", "RECLTD": "NIFTY FIN SERVICE",
    "MUTHOOTFIN": "NIFTY FIN SERVICE", "LICHSGFIN": "NIFTY FIN SERVICE",
    "SBICARD": "NIFTY FIN SERVICE", "M&MFIN": "NIFTY FIN SERVICE",
    "LICI": "NIFTY FIN SERVICE", "IRFC": "NIFTY FIN SERVICE",
    "MANAPPURAM": "NIFTY FIN SERVICE", "PAYTM": "NIFTY FIN SERVICE",
    "PNBHOUSING": "NIFTY FIN SERVICE", "CANFINHOME": "NIFTY FIN SERVICE",
    # NIFTY MEDIA
    "ZEEL": "NIFTY MEDIA", "SUNTV": "NIFTY MEDIA", "PVRINOX": "NIFTY MEDIA",
    "NETWORK18": "NIFTY MEDIA", "TV18BRDCST": "NIFTY MEDIA", "SAREGAMA": "NIFTY MEDIA",
    # NIFTY OIL AND GAS (overlaps a little with Energy, kept as its own
    # entry only for names not already assigned above)
    "MGL": "NIFTY OIL AND GAS", "GUJGASLTD": "NIFTY OIL AND GAS",
    "AEGISCHEM": "NIFTY OIL AND GAS",
    # NIFTY HEALTHCARE (hospitals/diagnostics - distinct from NIFTY
    # PHARMA's drugmakers above)
    "APOLLOHOSP": "NIFTY HEALTHCARE", "MAXHEALTH": "NIFTY HEALTHCARE",
    "FORTIS": "NIFTY HEALTHCARE", "METROPOLIS": "NIFTY HEALTHCARE",
    "LALPATHLAB": "NIFTY HEALTHCARE",
    # NIFTY CONSUMER DURABLES
    "TITAN": "NIFTY CONSR DURBL", "HAVELLS": "NIFTY CONSR DURBL",
    "VOLTAS": "NIFTY CONSR DURBL", "DIXON": "NIFTY CONSR DURBL",
    "CROMPTON": "NIFTY CONSR DURBL", "WHIRLPOOL": "NIFTY CONSR DURBL",
    "BLUESTARCO": "NIFTY CONSR DURBL", "KAJARIACER": "NIFTY CONSR DURBL",
    "RAJESHEXPO": "NIFTY CONSR DURBL",
    # NIFTY CHEMICALS
    "PIDILITIND": "NIFTY CHEMICALS", "SRF": "NIFTY CHEMICALS", "UPL": "NIFTY CHEMICALS",
    "AARTIIND": "NIFTY CHEMICALS", "DEEPAKNTR": "NIFTY CHEMICALS", "ATUL": "NIFTY CHEMICALS",
    "TATACHEM": "NIFTY CHEMICALS", "NAVINFLUOR": "NIFTY CHEMICALS", "PIIND": "NIFTY CHEMICALS",
    "COROMANDEL": "NIFTY CHEMICALS", "GNFC": "NIFTY CHEMICALS",
    # NIFTY INFRA (construction/engineering/cement conglomerates that
    # don't cleanly fit any sector above)
    "LT": "NIFTY INFRA", "ULTRACEMCO": "NIFTY INFRA", "GRASIM": "NIFTY INFRA",
    "SHREECEM": "NIFTY INFRA", "AMBUJACEM": "NIFTY INFRA", "ACC": "NIFTY INFRA",
    "ADANIPORTS": "NIFTY INFRA", "IRB": "NIFTY INFRA", "NCC": "NIFTY INFRA",
    "GMRINFRA": "NIFTY INFRA", "RVNL": "NIFTY INFRA", "CONCOR": "NIFTY INFRA",
    "SIEMENS": "NIFTY INFRA", "ABB": "NIFTY INFRA", "CUMMINSIND": "NIFTY INFRA",
    "POLYCAB": "NIFTY INFRA",
    # Telecom + a handful of large names without a dedicated NSE
    # sectoral index - deliberately left OUT of the map rather than
    # force-fit into a wrong sector, so they read sector_direction=None
    # (Airtel, defence PSUs, IT-adjacent conglomerates, etc.)
}
SECTOR_INDEXES = sorted(set(SYMBOL_SECTOR_MAP.values()))


def fetch_sector_directions(kite, sector_symbols, timeframe: str) -> dict:
    """Fetches each DISTINCT sector index's own current confluence
    direction, once per index per scan cycle (not once per stock) -
    same cost/caching shape as fetch_index_direction's single NIFTY 50
    call, just repeated for however many distinct sectors are actually
    present in the current watchlist (typically well under 20). Returns
    {sector_tradingsymbol: direction_or_None} - a sector whose fetch
    fails for any reason (bad index name, no data yet) simply maps to
    None, exactly like fetch_index_direction's own failure mode, so one
    bad sector lookup never affects any other sector or any stock's own
    result. `sector_symbols` should be the distinct sector tradingsymbols
    actually needed this cycle (see background._apply_sector_filter's
    caller), not necessarily all of SECTOR_INDEXES - no point fetching a
    sector nothing in the current watchlist maps to."""
    out = {}
    for sector in sector_symbols:
        direction, _close, _chg = fetch_instrument_direction(kite, sector, timeframe)
        out[sector] = direction
    return out


def fetch_sector_contexts(kite, sector_symbols, timeframe: str) -> dict:
    """Fetch direction plus current percentage change for each needed sector.

    This is the V6 live context equivalent of ``fetch_sector_directions``.
    It deliberately uses the same one-call-per-sector path so richer ranking
    does not double the API load.
    """
    out = {}
    for sector in sector_symbols:
        direction, close, chg_pct = fetch_instrument_direction(kite, sector, timeframe)
        out[sector] = {"direction": direction, "close": close, "chg_pct": chg_pct}
    return out


def is_market_open() -> bool:
    now = now_ist()
    if now.weekday() >= 5:  # Sat/Sun
        return False
    open_t = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t

# --------------------------------------------------------------------------
# Which part of the session are we in, as far as an OVERNIGHT read is
# concerned? The BTST/STBT panel's hard gate is a strong close on the daily
# bar - but during the session that bar is still being written (see
# _lookback_days' caller: to_date is always "now"). So "closed in the top 20%
# of its range" at 11am is not a weak version of the same statement, it's a
# different quantity that happens to share a name: where price sits in a range
# that still has hours left to move.
#
# The panel uses this to say which it is, rather than printing a fixed caveat
# and leaving the reader to check the clock themselves.
# --------------------------------------------------------------------------

def btst_read_window(now=None):
    """One of: "early" (bar has hours to run), "firming" (worth shortlisting),
    "settled" (act now), "closed" (final read, too late to trade)."""
    now = now or now_ist()
    if now.weekday() >= 5:
        return "closed"
    mins = now.hour * 60 + now.minute
    if mins >= 15 * 60 + 30:
        return "closed"
    if mins >= 15 * 60 + 10:
        return "settled"
    if mins >= 14 * 60 + 45:
        return "firming"
    return "early"
