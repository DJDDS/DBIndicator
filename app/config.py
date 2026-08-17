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

# Valid values for TIMEFRAME. "4hour" is synthesized by resampling Kite's
# native 60minute candles (Kite has no native 4H interval) - see
# scanner.py. "week" is synthesized by resampling daily candles.
VALID_TIMEFRAMES = ["15minute", "30minute", "60minute", "4hour", "day", "week"]
VALID_MACD_PRESETS = ["auto", "15min", "30min", "custom"]

_TUNABLE_FIELDS = [
    "WATCHLIST", "TIMEFRAME", "MACD_PRESET", "MACD_CUSTOM_FAST",
    "MACD_CUSTOM_SLOW", "MACD_CUSTOM_SIGNAL", "RSI_LENGTH",
    "RSI_SMOOTH_LENGTH", "EMA_LENGTH", "BB_LENGTH", "MIN_REQUIRED",
    "SCAN_INTERVAL_SECONDS",
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
        "MIN_REQUIRED": int(os.getenv("MIN_REQUIRED", 2)),
        "SCAN_INTERVAL_SECONDS": int(os.getenv("SCAN_INTERVAL_SECONDS", 180)),
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
                if mr not in (2, 3):
                    raise ValueError
                clean["MIN_REQUIRED"] = mr
            except (TypeError, ValueError):
                errors.append("Minimum required indicators must be 2 or 3.")

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
