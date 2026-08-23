"""
Central configuration for the Scanner app.

Two kinds of values live here:

1. Fixed/secret values (API key & secret, dashboard password, redirect
   URL, file paths, your Anthropic key for AI insights) - these come from
   environment variables only (see .env.example) and never change while
   the app is running. Editing these still requires a restart.

2. Tunable scanner settings (watchlist, timeframe, MACD/RSI/EMA/BB
   parameters, min-required, scan interval) - these start from your .env
   as defaults, but can then be changed live from the Settings page in
   the browser with no restart needed. Changes are persisted to
   scanner_settings.json so they survive a restart too.
"""
import json
import os
import threading

from dotenv import load_dotenv

load_dotenv()

KITE_API_KEY = os.getenv("KITE_API_KEY", "")
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "")
REDIRECT_URL = os.getenv("REDIRECT_URL", "http://localhost:5000/kite/callback")

# Simple shared password to keep your dashboard private once it's on a
# public server. Change this in your .env - do not leave it blank on a
# real deployment.
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")

# Where the day's access token is cached (plain file, not committed to
# git - see .gitignore). Re-created fresh every morning after login.
TOKEN_CACHE_FILE = os.getenv("TOKEN_CACHE_FILE", "kite_token_cache.json")

# Where live-editable scanner settings are persisted (also gitignored).
SETTINGS_FILE = os.getenv("SETTINGS_FILE", "scanner_settings.json")

# Where the most recent scan results are persisted (also gitignored), so
# the dashboard still shows the last scan for analysis after market
# hours even if the app restarts (a redeploy, a host restarting the
# container, etc.) - without this, results only lived in memory and a
# restart would silently wipe the day's data.
SCAN_RESULTS_FILE = os.getenv("SCAN_RESULTS_FILE", "last_scan_results.json")

# Where the last "Auto-Weight Parameters" run's backtest-derived weights
# are persisted (also gitignored) - background.py re-reads this file
# each scan cycle (see background._load_param_weights) so a weight
# recompute from the Backtest page takes effect on the very next scan,
# no restart needed.
PARAM_WEIGHTS_FILE = os.getenv("PARAM_WEIGHTS_FILE", "param_weights.json")

# Where the NIFTY 50 scalping screener's last scan is persisted (also
# gitignored) - same restart-resilience reasoning as SCAN_RESULTS_FILE
# above, kept in its own file since scalper.py runs its own independent
# background loop on its own faster cadence.
SCALP_RESULTS_FILE = os.getenv("SCALP_RESULTS_FILE", "scalp_results.json")

# Where the dashboard's always-on 15-minute/60-minute/4-hour panel persists
# its last scan per timeframe (also gitignored) - same restart-resilience
# reasoning as SCAN_RESULTS_FILE above. See background.py's
# MULTI_TF_TIMEFRAMES/_run_multi_tf_loop - this is a separate, additive
# scan loop from the single Settings > Timeframe pipeline, so it gets its
# own state/persistence file rather than sharing SCAN_RESULTS_FILE.
MULTI_TF_RESULTS_FILE = os.getenv("MULTI_TF_RESULTS_FILE", "multi_tf_results.json")

# Where the forward-testing signal journal's logged paper trades are
# persisted (also gitignored) - same restart-resilience reasoning as
# SCAN_RESULTS_FILE above, kept in its own file since journal.py owns an
# independent, small, hand-curated list rather than a full scan's worth
# of rows every cycle. NOTE (see journal.py's module docstring): unlike
# a real database, this is still just a local file on the container's
# disk - with no persistent Railway volume attached, any trade that is
# still OPEN (not yet resolved) at the moment of a redeploy is lost, the
# same limitation SCAN_RESULTS_FILE/PARAM_WEIGHTS_FILE already have.
# Already-RESOLVED trades survive a redeploy just fine (they're written
# back to this file the moment they resolve); the /journal page's CSV
# export exists specifically so you can save a permanent copy before a
# deploy if you have trades you don't want to risk.
JOURNAL_FILE = os.getenv("JOURNAL_FILE", "signal_journal.json")

# Optional - only needed for the "AI Insights" panel on the dashboard.
# Get one at console.anthropic.com. Leave blank to disable that panel.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")

# Optional - only needed for Telegram alerts when a signal fires. See
# README "Alerts" section for how to create a bot and find your chat id
# (Settings page has a "Find my chat ID" helper once the token is set).
# Both are credentials/identifiers, not tunable settings, so like the
# Kite/Anthropic keys they live in .env only, not the Settings UI.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Optional - only needed for the news feature (app/news.py). Get a free
# key at marketaux.com (100 requests/day, 3 articles/request on the free
# plan - see news.py's own docstring for how that budget is spent).
# Credential, not a tunable setting, like the Telegram/Anthropic keys
# above - lives in .env only.
MARKETAUX_API_TOKEN = os.getenv("MARKETAUX_API_TOKEN", "")
# Minimum seconds between live news fetches (throttle, not a hard
# schedule - the actual cadence also depends on the scan loop's own
# interval). Default 900s (15 min) keeps a full trading day's worth of
# polling comfortably under the free tier's 100/day cap even accounting
# for redeploys and manual testing.
NEWS_POLL_INTERVAL_SECONDS = int(os.getenv("NEWS_POLL_INTERVAL_SECONDS", 900))
# Hard ceiling on live Marketaux calls per calendar day, persisted so a
# redeploy can't reset it to zero and risk a burst - deliberately below
# the free plan's real 100/day limit as a safety margin.
NEWS_DAILY_CALL_CAP = int(os.getenv("NEWS_DAILY_CALL_CAP", 90))
NEWS_STATE_FILE = os.getenv("NEWS_STATE_FILE", "news_state.json")

DEFAULT_WATCHLIST = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "HINDUNILVR", "ITC",
    "SBIN", "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK", "BAJFINANCE",
    "MARUTI", "SUNPHARMA", "TITAN", "ULTRACEMCO", "WIPRO", "ONGC", "TATAMOTORS",
]

# Valid values for TIMEFRAME. 30-minute was dropped as an unnecessary
# in-between option; 60-minute was dropped for the same reason earlier
# but re-added on request - it's a native Kite interval (no resampling
# needed, unlike "4hour"/"week" below), gets its own HTF (daily) trend
# check in indicators._HTF_RESAMPLE, and its own OI/warmup lookback in
# scanner._lookback_days already. "4hour" is synthesized by resampling
# Kite's native 60-minute candles (Kite has no native 4H interval) -
# see scanner.py - and stays selectable here as a normal scan timeframe;
# it's the separate always-on 4-hour CROSS-CHECK scan (background.py's
# old parallel pass feeding "positional_qualified") that was removed,
# not 4-hour itself. "week" is synthesized by resampling daily candles.
VALID_TIMEFRAMES = ["15minute", "60minute", "4hour", "day", "week"]
VALID_MACD_PRESETS = ["auto", "15min", "30min", "custom"]

_TUNABLE_FIELDS = [
    "WATCHLIST", "TIMEFRAME", "MACD_PRESET", "MACD_CUSTOM_FAST",
    "MACD_CUSTOM_SLOW", "MACD_CUSTOM_SIGNAL", "RSI_LENGTH",
    "RSI_SMOOTH_LENGTH", "EMA_LENGTH", "BB_LENGTH", "MIN_REQUIRED",
    "REL_VOLUME_THRESHOLD", "SCAN_INTERVAL_SECONDS",
    "ADX_LENGTH", "RANGING_VOL_MULTIPLIER", "REQUIRE_INDEX_AGREEMENT",
    "REQUIRE_VOLUME_FLOW_AGREEMENT", "REQUIRE_CANDLE_PATTERN_AGREEMENT",
    "REQUIRE_SECTOR_AGREEMENT", "REQUIRE_BREADTH_AGREEMENT", "BREADTH_THRESHOLD_PCT",
    "REQUIRE_MACD_HIST_AGREEMENT",
    "ATR_LENGTH", "ATR_STOP_MULTIPLIER", "ATR_TARGET_MULTIPLIER",
    "ACCOUNT_CAPITAL", "RISK_PER_TRADE_PCT", "MAX_DAILY_RISK_PCT", "MAX_CONCURRENT_POSITIONS",
]


def _env_watchlist():
    raw = os.getenv("WATCHLIST", "")
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    return symbols or list(DEFAULT_WATCHLIST)


def _env_defaults():
    return {
        "WATCHLIST": _env_watchlist(),
        "TIMEFRAME": os.getenv("TIMEFRAME", "15minute"),
        "MACD_PRESET": os.getenv("MACD_PRESET", "auto"),
        "MACD_CUSTOM_FAST": int(os.getenv("MACD_CUSTOM_FAST", 12)),
        "MACD_CUSTOM_SLOW": int(os.getenv("MACD_CUSTOM_SLOW", 26)),
        "MACD_CUSTOM_SIGNAL": int(os.getenv("MACD_CUSTOM_SIGNAL", 9)),
        "RSI_LENGTH": int(os.getenv("RSI_LENGTH", 9)),
        "RSI_SMOOTH_LENGTH": int(os.getenv("RSI_SMOOTH_LENGTH", 9)),
        "EMA_LENGTH": int(os.getenv("EMA_LENGTH", 9)),
        "BB_LENGTH": int(os.getenv("BB_LENGTH", 20)),
        # 4-parameter confluence: RSI (vs its smoothing line), MACD (vs
        # signal line), EMA9 (vs Bollinger mid), and Relative Volume (vs
        # its own 20-bar average) - MIN_REQUIRED is how many of those 4
        # must currently agree for a "confirmed" signal (was 2/3-of-3
        # before Relative Volume joined the count as a real, equally-
        # weighted 4th parameter instead of an always-mandatory add-on).
        "MIN_REQUIRED": int(os.getenv("MIN_REQUIRED", 2)),
        "REL_VOLUME_THRESHOLD": float(os.getenv("REL_VOLUME_THRESHOLD", 1.2)),
        "SCAN_INTERVAL_SECONDS": int(os.getenv("SCAN_INTERVAL_SECONDS", 180)),
        # Regime-adaptive volume bar (see indicators.compute_signal): ADX
        # length used to classify each stock's current Trending/Ranging/
        # Transitional regime, and how much stricter (multiplier on
        # REL_VOLUME_THRESHOLD) the Relative Volume bar gets specifically
        # in a Ranging regime, where breakouts are more prone to false
        # starts.
        "ADX_LENGTH": int(os.getenv("ADX_LENGTH", 14)),
        "RANGING_VOL_MULTIPLIER": float(os.getenv("RANGING_VOL_MULTIPLIER", 1.3)),
        # Index/market-trend filter (see background._apply_index_filter):
        # when on, a row whose direction disagrees with NIFTY 50's own
        # current confluence direction loses its "Confirmed" status
        # (and, downstream, Positional Qualified / High Conviction) -
        # counter-trend trades have historically had a lower win rate.
        # Off by default so existing behaviour doesn't change until you
        # opt in.
        "REQUIRE_INDEX_AGREEMENT": os.getenv("REQUIRE_INDEX_AGREEMENT", "false").strip().lower() in ("1", "true", "on", "yes"),
        # Volume-flow filter (see background._apply_volume_flow_filter and
        # indicators.compute_signal's vol_flow_direction/vol_flow_agrees,
        # via Chaikin Money Flow - PARAMETER_ANALYSIS_2.md Finding #2):
        # when on, a row whose direction disagrees with its own recent CMF
        # sign (i.e. the volume backing the move looks like distribution,
        # not buying, for a Bullish row - or vice versa) loses its
        # "Confirmed" status. Off by default, same reasoning as
        # REQUIRE_INDEX_AGREEMENT above.
        "REQUIRE_VOLUME_FLOW_AGREEMENT": os.getenv("REQUIRE_VOLUME_FLOW_AGREEMENT", "false").strip().lower() in ("1", "true", "on", "yes"),
        # Candlestick-pattern filter (see background._apply_candle_pattern_
        # filter and indicators.compute_signal's candle_pattern/
        # candle_direction/candle_agrees, via _compute_candle_pattern -
        # engulfing/hammer-family/morning-evening-star): when on, a row
        # whose direction disagrees with its own most recent candlestick
        # pattern loses its "Confirmed" status. Off by default, same
        # reasoning as REQUIRE_INDEX_AGREEMENT/REQUIRE_VOLUME_FLOW_AGREEMENT
        # above.
        "REQUIRE_CANDLE_PATTERN_AGREEMENT": os.getenv("REQUIRE_CANDLE_PATTERN_AGREEMENT", "false").strip().lower() in ("1", "true", "on", "yes"),
        # Sector relative-strength filter (see background._apply_sector_
        # filter and scanner.SYMBOL_SECTOR_MAP/fetch_sector_directions,
        # NEXT_HORIZON_RESEARCH.md Finding 5): when on, a row whose
        # direction disagrees with its own NSE sectoral index's current
        # confluence direction loses its "Confirmed" status - genuinely
        # different information from RSI/MACD/EMA-BB, since it's about
        # the stock's sector context rather than another transform of
        # its own closing price. A symbol not in SYMBOL_SECTOR_MAP always
        # reads sector_direction=None (never blocks). Off by default,
        # same reasoning as REQUIRE_INDEX_AGREEMENT above.
        "REQUIRE_SECTOR_AGREEMENT": os.getenv("REQUIRE_SECTOR_AGREEMENT", "false").strip().lower() in ("1", "true", "on", "yes"),
        # Market-breadth filter (see background._compute_breadth/_apply_
        # breadth_filter, NEXT_HORIZON_RESEARCH.md Finding 5): when on, a
        # row whose direction is decisively against the CURRENT
        # watchlist's own advance/decline split (not full-NSE breadth -
        # Kite has no cheap full-market breadth endpoint, so this is a
        # watchlist-scoped proxy, labelled as such on the dashboard) loses
        # its "Confirmed" status. Off by default, same reasoning as every
        # other REQUIRE_* gate above.
        "REQUIRE_BREADTH_AGREEMENT": os.getenv("REQUIRE_BREADTH_AGREEMENT", "false").strip().lower() in ("1", "true", "on", "yes"),
        # Minimum % of the watchlist's resolved (Bullish/Bearish, error-
        # free) rows that must share a row's own direction for breadth to
        # be considered "agreeing" with it - e.g. the default 30 means a
        # Bullish row needs at least 30% of the watchlist also reading
        # Bullish; below that, breadth is considered decisively against
        # it. Only matters when REQUIRE_BREADTH_AGREEMENT is on; always
        # used to compute the informational breadth_agrees field either
        # way (shown as a small badge, matching every other gate's
        # always-attached-for-display convention).
        "BREADTH_THRESHOLD_PCT": float(os.getenv("BREADTH_THRESHOLD_PCT", 30.0)),
        # MACD histogram momentum filter (see indicators.compute_signal's
        # macd_hist_rising/macd_hist_agrees): when on, a row whose MACD
        # histogram is shrinking against its own direction (bullish but
        # momentum fading, or bearish but momentum fading) loses its
        # "Confirmed" status. Deliberately NOT "histogram > 0" - that's
        # identical to the existing macd_line > signal_line check, so it
        # would add zero new information; this reads the histogram's own
        # slope (is the crossover accelerating or already running out of
        # steam) instead. Off by default, same reasoning as every other
        # REQUIRE_* gate above.
        "REQUIRE_MACD_HIST_AGREEMENT": os.getenv("REQUIRE_MACD_HIST_AGREEMENT", "false").strip().lower() in ("1", "true", "on", "yes"),
        # ATR-based risk layer (see indicators.compute_signal's atr/stop/
        # target/risk_reward, and compute_atr - Wilder's Average True
        # Range, already used by scalper.py's own fixed-constant version
        # of the same idea for NIFTY futures). Pure DISPLAY information -
        # a suggested stop-loss/target scaled to each stock's own recent
        # volatility rather than a flat percentage - never gates
        # signal_confirmed and never places an order. ATR_LENGTH is the
        # lookback for the ATR itself; ATR_STOP_MULTIPLIER/
        # ATR_TARGET_MULTIPLIER scale it into a stop/target distance from
        # the current close (default 1.5x/3.0x = a 1:2 risk-reward, a
        # conventional swing-trading starting point - tune to taste).
        "ATR_LENGTH": int(os.getenv("ATR_LENGTH", 14)),
        "ATR_STOP_MULTIPLIER": float(os.getenv("ATR_STOP_MULTIPLIER", 1.5)),
        "ATR_TARGET_MULTIPLIER": float(os.getenv("ATR_TARGET_MULTIPLIER", 3.0)),
        # Risk-management layer (NEXT_HORIZON_RESEARCH.md Finding 4 - "the
        # research is unusually consistent that this matters more than
        # signal sophistication"). ACCOUNT_CAPITAL is a number YOU tell
        # this app, not something it reads from your real Zerodha
        # account (this app never touches your funds or positions) -
        # the default is a placeholder and should be changed on the
        # Settings page to your actual trading capital for the position-
        # size suggestion (indicators.compute_signal's position_qty) to
        # mean anything real. RISK_PER_TRADE_PCT is the fixed-fractional
        # risk per trade the research treats as close to baseline
        # discipline (1-2% typical, tighter than cash equities because
        # F&O's embedded leverage means a given price move is a bigger
        # swing in effective exposure - Kelly-criterion sizing is
        # explicitly NOT recommended by that same research until the
        # journal has enough resolved trades to estimate win-rate/payoff
        # honestly). MAX_DAILY_RISK_PCT and MAX_CONCURRENT_POSITIONS feed
        # journal.get_risk_budget_state's dashboard banner - informational
        # only, this app can't and doesn't block you from logging another
        # trade past either limit.
        "ACCOUNT_CAPITAL": float(os.getenv("ACCOUNT_CAPITAL", 100000.0)),
        "RISK_PER_TRADE_PCT": float(os.getenv("RISK_PER_TRADE_PCT", 1.0)),
        "MAX_DAILY_RISK_PCT": float(os.getenv("MAX_DAILY_RISK_PCT", 3.0)),
        "MAX_CONCURRENT_POSITIONS": int(os.getenv("MAX_CONCURRENT_POSITIONS", 5)),
    }


class Settings:
    """Thread-safe, live-editable scanner settings. Read via attribute
    access (settings.TIMEFRAME); change via settings.update(**kwargs),
    which validates, applies in-memory, and persists to SETTINGS_FILE."""

    def __init__(self):
        self._lock = threading.Lock()
        data = _env_defaults()
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE) as f:
                    saved = json.load(f)
                for k, v in saved.items():
                    if k in _TUNABLE_FIELDS:
                        data[k] = v
            except (json.JSONDecodeError, OSError):
                pass
        # A persisted TIMEFRAME from before VALID_TIMEFRAMES was trimmed
        # (e.g. "30minute", "60minute", "4hour") would otherwise silently
        # keep scanning on a now-unselectable value forever - fall back
        # to the default instead so removing an option can't strand a
        # running deployment on it.
        if data.get("TIMEFRAME") not in VALID_TIMEFRAMES:
            data["TIMEFRAME"] = "15minute"

        for k, v in data.items():
            setattr(self, k, v)

    def as_dict(self):
        with self._lock:
            return {k: getattr(self, k) for k in _TUNABLE_FIELDS}

    def update(self, **kwargs):
        errors = []
        clean = {}

        if "WATCHLIST" in kwargs:
            wl = kwargs["WATCHLIST"]
            if isinstance(wl, str):
                wl = [s.strip().upper() for s in wl.replace("\n", ",").split(",") if s.strip()]
            wl = [s.strip().upper() for s in wl if s.strip()]
            if not wl:
                errors.append("Watchlist can't be empty.")
            else:
                clean["WATCHLIST"] = wl

        if "TIMEFRAME" in kwargs:
            tf = kwargs["TIMEFRAME"]
            if tf not in VALID_TIMEFRAMES:
                errors.append(f"Timeframe must be one of {VALID_TIMEFRAMES}.")
            else:
                clean["TIMEFRAME"] = tf

        if "MACD_PRESET" in kwargs:
            mp = kwargs["MACD_PRESET"]
            if mp not in VALID_MACD_PRESETS:
                errors.append(f"MACD preset must be one of {VALID_MACD_PRESETS}.")
            else:
                clean["MACD_PRESET"] = mp

        for field in ("MACD_CUSTOM_FAST", "MACD_CUSTOM_SLOW", "MACD_CUSTOM_SIGNAL",
                      "RSI_LENGTH", "RSI_SMOOTH_LENGTH", "EMA_LENGTH", "BB_LENGTH",
                      "SCAN_INTERVAL_SECONDS"):
            if field in kwargs:
                try:
                    val = int(kwargs[field])
                    if val < 1:
                        raise ValueError
                    clean[field] = val
                except (TypeError, ValueError):
                    errors.append(f"{field} must be a positive whole number.")

        if "MIN_REQUIRED" in kwargs:
            try:
                mr = int(kwargs["MIN_REQUIRED"])
                if mr not in (2, 3, 4):
                    raise ValueError
                clean["MIN_REQUIRED"] = mr
            except (TypeError, ValueError):
                errors.append("Minimum required parameters must be 2, 3, or 4.")

        if "REL_VOLUME_THRESHOLD" in kwargs:
            try:
                rv = float(kwargs["REL_VOLUME_THRESHOLD"])
                if rv <= 0:
                    raise ValueError
                clean["REL_VOLUME_THRESHOLD"] = rv
            except (TypeError, ValueError):
                errors.append("Relative Volume threshold must be a positive number.")

        if "ADX_LENGTH" in kwargs:
            try:
                al = int(kwargs["ADX_LENGTH"])
                if al < 2:
                    raise ValueError
                clean["ADX_LENGTH"] = al
            except (TypeError, ValueError):
                errors.append("ADX length must be a whole number of at least 2.")

        if "RANGING_VOL_MULTIPLIER" in kwargs:
            try:
                rvm = float(kwargs["RANGING_VOL_MULTIPLIER"])
                if rvm < 1.0:
                    raise ValueError
                clean["RANGING_VOL_MULTIPLIER"] = rvm
            except (TypeError, ValueError):
                errors.append("Ranging-regime volume multiplier must be a number of at least 1.0.")

        if "REQUIRE_INDEX_AGREEMENT" in kwargs:
            val = kwargs["REQUIRE_INDEX_AGREEMENT"]
            if isinstance(val, str):
                clean["REQUIRE_INDEX_AGREEMENT"] = val.strip().lower() in ("1", "true", "on", "yes")
            else:
                clean["REQUIRE_INDEX_AGREEMENT"] = bool(val)

        if "REQUIRE_VOLUME_FLOW_AGREEMENT" in kwargs:
            val = kwargs["REQUIRE_VOLUME_FLOW_AGREEMENT"]
            if isinstance(val, str):
                clean["REQUIRE_VOLUME_FLOW_AGREEMENT"] = val.strip().lower() in ("1", "true", "on", "yes")
            else:
                clean["REQUIRE_VOLUME_FLOW_AGREEMENT"] = bool(val)

        if "REQUIRE_CANDLE_PATTERN_AGREEMENT" in kwargs:
            val = kwargs["REQUIRE_CANDLE_PATTERN_AGREEMENT"]
            if isinstance(val, str):
                clean["REQUIRE_CANDLE_PATTERN_AGREEMENT"] = val.strip().lower() in ("1", "true", "on", "yes")
            else:
                clean["REQUIRE_CANDLE_PATTERN_AGREEMENT"] = bool(val)

        if "REQUIRE_SECTOR_AGREEMENT" in kwargs:
            val = kwargs["REQUIRE_SECTOR_AGREEMENT"]
            if isinstance(val, str):
                clean["REQUIRE_SECTOR_AGREEMENT"] = val.strip().lower() in ("1", "true", "on", "yes")
            else:
                clean["REQUIRE_SECTOR_AGREEMENT"] = bool(val)

        if "REQUIRE_BREADTH_AGREEMENT" in kwargs:
            val = kwargs["REQUIRE_BREADTH_AGREEMENT"]
            if isinstance(val, str):
                clean["REQUIRE_BREADTH_AGREEMENT"] = val.strip().lower() in ("1", "true", "on", "yes")
            else:
                clean["REQUIRE_BREADTH_AGREEMENT"] = bool(val)

        if "REQUIRE_MACD_HIST_AGREEMENT" in kwargs:
            val = kwargs["REQUIRE_MACD_HIST_AGREEMENT"]
            if isinstance(val, str):
                clean["REQUIRE_MACD_HIST_AGREEMENT"] = val.strip().lower() in ("1", "true", "on", "yes")
            else:
                clean["REQUIRE_MACD_HIST_AGREEMENT"] = bool(val)

        if "BREADTH_THRESHOLD_PCT" in kwargs:
            try:
                bt = float(kwargs["BREADTH_THRESHOLD_PCT"])
                if not (0 < bt < 100):
                    raise ValueError
                clean["BREADTH_THRESHOLD_PCT"] = bt
            except (TypeError, ValueError):
                errors.append("Breadth threshold % must be a number between 0 and 100.")

        if "ATR_LENGTH" in kwargs:
            try:
                atl = int(kwargs["ATR_LENGTH"])
                if atl < 2:
                    raise ValueError
                clean["ATR_LENGTH"] = atl
            except (TypeError, ValueError):
                errors.append("ATR length must be a whole number of at least 2.")

        if "ATR_STOP_MULTIPLIER" in kwargs:
            try:
                asm = float(kwargs["ATR_STOP_MULTIPLIER"])
                if asm <= 0:
                    raise ValueError
                clean["ATR_STOP_MULTIPLIER"] = asm
            except (TypeError, ValueError):
                errors.append("ATR stop multiplier must be a positive number.")

        if "ATR_TARGET_MULTIPLIER" in kwargs:
            try:
                atm = float(kwargs["ATR_TARGET_MULTIPLIER"])
                if atm <= 0:
                    raise ValueError
                clean["ATR_TARGET_MULTIPLIER"] = atm
            except (TypeError, ValueError):
                errors.append("ATR target multiplier must be a positive number.")

        if "ACCOUNT_CAPITAL" in kwargs:
            try:
                cap = float(kwargs["ACCOUNT_CAPITAL"])
                if cap <= 0:
                    raise ValueError
                clean["ACCOUNT_CAPITAL"] = cap
            except (TypeError, ValueError):
                errors.append("Account capital must be a positive number.")

        if "RISK_PER_TRADE_PCT" in kwargs:
            try:
                rpt = float(kwargs["RISK_PER_TRADE_PCT"])
                if not (0 < rpt <= 100):
                    raise ValueError
                clean["RISK_PER_TRADE_PCT"] = rpt
            except (TypeError, ValueError):
                errors.append("Risk per trade % must be a number between 0 and 100.")

        if "MAX_DAILY_RISK_PCT" in kwargs:
            try:
                mdr = float(kwargs["MAX_DAILY_RISK_PCT"])
                if not (0 < mdr <= 100):
                    raise ValueError
                clean["MAX_DAILY_RISK_PCT"] = mdr
            except (TypeError, ValueError):
                errors.append("Max daily risk % must be a number between 0 and 100.")

        if "MAX_CONCURRENT_POSITIONS" in kwargs:
            try:
                mcp = int(kwargs["MAX_CONCURRENT_POSITIONS"])
                if mcp < 1:
                    raise ValueError
                clean["MAX_CONCURRENT_POSITIONS"] = mcp
            except (TypeError, ValueError):
                errors.append("Max concurrent positions must be a whole number of at least 1.")

        if errors:
            return errors

        with self._lock:
            for k, v in clean.items():
                setattr(self, k, v)
            data = {k: getattr(self, k) for k in _TUNABLE_FIELDS}
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=2)
        return []


settings = Settings()
