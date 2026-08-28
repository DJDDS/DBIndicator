import functools
import logging

import pandas as pd
from flask import Flask, jsonify, redirect, render_template, request, Response

from . import alerts, config, indicators, kite_auth, scanner
from .background import get_state, start_background_scanner
from .config import settings
from .insights import generate_insights, insights_enabled

log = logging.getLogger(__name__)

app = Flask(__name__)
_scanner_started = False


def _check_auth(username, password):
    # Single shared password, not a real user system - fine for a
    # personal single-user dashboard. Username is ignored.
    return config.DASHBOARD_PASSWORD and password == config.DASHBOARD_PASSWORD


def _authenticate():
    return Response(
        "Login required.", 401, {"WWW-Authenticate": 'Basic realm="Scanner Dashboard"'}
    )


def require_dashboard_password(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not config.DASHBOARD_PASSWORD:
            # No password configured - warn loudly rather than silently
            # running an open dashboard. Set DASHBOARD_PASSWORD in .env.
            return (
                "DASHBOARD_PASSWORD is not set in your .env file. "
                "Set one before exposing this app publicly, then restart.",
                500,
            )
        auth = request.authorization
        if not auth or not _check_auth(auth.username, auth.password):
            return _authenticate()
        return view(*args, **kwargs)

    return wrapped


@app.before_request
def _ensure_scanner_running():
    global _scanner_started
    if not _scanner_started:
        start_background_scanner()
        _scanner_started = True


@app.route("/")
@require_dashboard_password
def dashboard():
    logged_in = kite_auth.is_logged_in_today()
    login_url = kite_auth.get_login_url() if not logged_in else None
    state = get_state()
    return render_template(
        "index.html",
        logged_in=logged_in,
        login_url=login_url,
        results=state["results"],
        last_scan=state["last_scan"],
        last_error=state["last_error"],
        timeframe=settings.TIMEFRAME,
        min_required=settings.MIN_REQUIRED,
        insights_enabled=insights_enabled(),
        telegram_enabled=alerts.telegram_enabled(),
    )


@app.route("/kite/callback")
def kite_callback():
    request_token = request.args.get("request_token")
    if not request_token:
        return "No request_token received from Kite - please try logging in again.", 400
    kite_auth.exchange_request_token(request_token)
    return redirect("/")


@app.route("/settings", methods=["GET", "POST"])
@require_dashboard_password
def settings_page():
    errors = []
    saved = False
    fno_error = request.args.get("error") == "notloggedin"
    if request.method == "POST":
        form = request.form
        payload = {
            "WATCHLIST": form.get("watchlist", ""),
            "TIMEFRAME": form.get("timeframe", settings.TIMEFRAME),
            "MACD_PRESET": form.get("macd_preset", settings.MACD_PRESET),
            "MACD_CUSTOM_FAST": form.get("macd_fast", settings.MACD_CUSTOM_FAST),
            "MACD_CUSTOM_SLOW": form.get("macd_slow", settings.MACD_CUSTOM_SLOW),
            "MACD_CUSTOM_SIGNAL": form.get("macd_signal", settings.MACD_CUSTOM_SIGNAL),
            "RSI_LENGTH": form.get("rsi_length", settings.RSI_LENGTH),
            "RSI_SMOOTH_LENGTH": form.get("rsi_smooth", settings.RSI_SMOOTH_LENGTH),
            "EMA_LENGTH": form.get("ema_length", settings.EMA_LENGTH),
            "BB_LENGTH": form.get("bb_length", settings.BB_LENGTH),
            "MIN_REQUIRED": form.get("min_required", settings.MIN_REQUIRED),
            "SCAN_INTERVAL_SECONDS": form.get("scan_interval", settings.SCAN_INTERVAL_SECONDS),
        }
        errors = settings.update(**payload)
        saved = not errors
    return render_template(
        "settings.html",
        s=settings.as_dict(),
        errors=errors,
        saved=saved,
        fno_error=fno_error,
        valid_timeframes=config.VALID_TIMEFRAMES,
        valid_presets=config.VALID_MACD_PRESETS,
        logged_in=kite_auth.is_logged_in_today(),
        telegram_enabled=alerts.telegram_enabled(),
        telegram_token_set=bool(config.TELEGRAM_BOT_TOKEN),
    )


@app.route("/settings/load-fno-list", methods=["POST"])
@require_dashboard_password
def load_fno_list():
    kite = kite_auth.get_kite_client()
    if kite is None:
        return redirect("/settings?error=notloggedin")
    try:
        symbols = scanner.get_fno_stock_list(kite)
        if symbols:
            settings.update(WATCHLIST=symbols)
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to load F&O list from Kite: %s", exc)
    return redirect("/settings")


@app.route("/chart/<symbol>")
@require_dashboard_password
def chart_page(symbol):
    return render_template(
        "chart.html",
        symbol=symbol.upper(),
        timeframe=settings.TIMEFRAME,
        valid_timeframes=config.VALID_TIMEFRAMES,
    )


@app.route("/api/chart/<symbol>")
@require_dashboard_password
def chart_data(symbol):
    kite = kite_auth.get_kite_client()
    if kite is None:
        return jsonify({"error": "Not logged in to Kite today."}), 400

    timeframe = request.args.get("timeframe", settings.TIMEFRAME)
    if timeframe not in config.VALID_TIMEFRAMES:
        return jsonify({"error": "invalid timeframe"}), 400

    try:
        instruments = scanner._load_instrument_map(kite)
        token = instruments.get(symbol.upper())
        if not token:
            return jsonify({"error": "symbol not found on NSE"}), 404

        df = scanner.fetch_candles(kite, token, timeframe)
        if df.empty:
            return jsonify({"error": "no candle data returned"}), 502

        series = indicators.compute_series(df, timeframe)
        if "error" in series:
            return jsonify({"error": series["error"], "candles": _candles(df)})

        def _points(s):
            return [
                {"time": int(idx.timestamp()), "value": round(float(v), 3)}
                for idx, v in s.items()
                if pd.notna(v)
            ]

        fast, slow, sig = series["macd_params"]
        payload = {
            "symbol": symbol.upper(),
            "timeframe": timeframe,
            "macd_params": f"{fast},{slow},{sig}",
            "candles": _candles(df),
            "ema9": _points(series["ema9"]),
            "bb_mid": _points(series["bb_mid"]),
            "rsi": _points(series["rsi_line"]),
            "rsi_smooth": _points(series["rsi_smooth"]),
            "macd": _points(series["macd_line"]),
            "macd_signal": _points(series["signal_line"]),
            "macd_hist": _points(series["macd_hist"]),
        }
        return jsonify(payload)
    except Exception as exc:  # noqa: BLE001
        log.exception("Chart data failed for %s", symbol)
        return jsonify({"error": str(exc)}), 500


def _candles(df):
    return [
        {
            "time": int(idx.timestamp()),
            "open": round(float(row["open"]), 2),
            "high": round(float(row["high"]), 2),
            "low": round(float(row["low"]), 2),
            "close": round(float(row["close"]), 2),
        }
        for idx, row in df.iterrows()
    ]


@app.route("/api/insights")
@require_dashboard_password
def api_insights():
    state = get_state()
    result = generate_insights(
        state["results"], settings.TIMEFRAME, settings.MIN_REQUIRED, state["last_scan"]
    )
    return jsonify(result)


@app.route("/api/alerts/recent")
@require_dashboard_password
def api_alerts_recent():
    return jsonify({"alerts": alerts.get_recent(limit=20)})


@app.route("/api/alerts/test", methods=["POST"])
@require_dashboard_password
def api_alerts_test():
    return jsonify(alerts.send_test_alert())


@app.route("/api/alerts/discover-chat-id")
@require_dashboard_password
def api_alerts_discover_chat_id():
    return jsonify(alerts.discover_chat_id())


def create_app():
    return app
