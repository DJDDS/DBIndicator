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
