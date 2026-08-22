import functools
import logging

import pandas as pd
from flask import Flask, jsonify, redirect, render_template, request, Response

from . import alerts, backtest, background, config, indicators, journal, kite_auth, scanner
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
        background.start_multi_tf_scanner()
        _scanner_started = True


@app.route("/")
@require_dashboard_password
def dashboard():
    logged_in = kite_auth.is_logged_in_today()
    login_url = kite_auth.get_login_url() if not logged_in else None
    state = get_state()
    all_results = state["results"]

    return render_template(
        "index.html",
        logged_in=logged_in,
        login_url=login_url,
        results=all_results,
        total_scanned=len(all_results),
        last_scan=state["last_scan"],
        last_error=state["last_error"],
        timeframe=settings.TIMEFRAME,
        min_required=settings.MIN_REQUIRED,
        macd_preset=settings.MACD_PRESET,
        rsi_length=settings.RSI_LENGTH,
        rsi_smooth_length=settings.RSI_SMOOTH_LENGTH,
        macd_fast=settings.MACD_CUSTOM_FAST,
        macd_slow=settings.MACD_CUSTOM_SLOW,
        macd_signal=settings.MACD_CUSTOM_SIGNAL,
        ema_length=settings.EMA_LENGTH,
        bb_length=settings.BB_LENGTH,
        rel_volume_threshold=settings.REL_VOLUME_THRESHOLD,
        valid_timeframes=config.VALID_TIMEFRAMES,
        valid_presets=config.VALID_MACD_PRESETS,
        quick_error=request.args.get("quick_error"),
        insights_enabled=insights_enabled(),
        telegram_enabled=alerts.telegram_enabled(),
        screen_param_defs=background.SCREEN_PARAM_DEFS,
        opening_window_minutes=indicators.OPENING_WINDOW_MINUTES,
        opening_window_end="9:%02d" % (15 + indicators.OPENING_WINDOW_MINUTES),
        index_direction=state.get("index_direction"),
        index_close=state.get("index_close"),
        index_chg_pct=state.get("index_chg_pct"),
        require_index_agreement=settings.REQUIRE_INDEX_AGREEMENT,
        require_volume_flow_agreement=settings.REQUIRE_VOLUME_FLOW_AGREEMENT,
        require_candle_pattern_agreement=settings.REQUIRE_CANDLE_PATTERN_AGREEMENT,
        require_sector_agreement=settings.REQUIRE_SECTOR_AGREEMENT,
        require_breadth_agreement=settings.REQUIRE_BREADTH_AGREEMENT,
        breadth=state.get("breadth"),
        breadth_threshold_pct=settings.BREADTH_THRESHOLD_PCT,
        multi_tf=background.get_multi_tf_state(),
    )


@app.route("/quick-settings", methods=["POST"])
@require_dashboard_password
def quick_settings():
    """A compact settings panel lives on the dashboard itself (timeframe,
    MACD preset, RSI/EMA/BB lengths, min-required) so the common tweaks
    don't need a trip to /settings. Only forwards fields that were
    actually submitted - unlike the /settings page's form, this never
    touches WATCHLIST/scan interval/MACD custom values, so there's no
    risk of accidentally wiping those from a partial submission."""
    form = request.form
    field_map = {
        "timeframe": "TIMEFRAME",
        "macd_preset": "MACD_PRESET",
        "rsi_length": "RSI_LENGTH",
        "rsi_smooth_length": "RSI_SMOOTH_LENGTH",
        "macd_fast": "MACD_CUSTOM_FAST",
        "macd_slow": "MACD_CUSTOM_SLOW",
        "macd_signal": "MACD_CUSTOM_SIGNAL",
        "ema_length": "EMA_LENGTH",
        "bb_length": "BB_LENGTH",
        "min_required": "MIN_REQUIRED",
        "rel_volume_threshold": "REL_VOLUME_THRESHOLD",
    }
    kwargs = {setting_key: form[form_key] for form_key, setting_key in field_map.items() if form_key in form}
    errors = settings.update(**kwargs)
    if errors:
        return redirect("/?quick_error=" + "; ".join(errors))
    # Wake the background scanner immediately instead of leaving it to
    # finish out its current SCAN_INTERVAL_SECONDS sleep - otherwise a
    # timeframe/indicator change can take up to 3 minutes to show up,
    # which looks like the change didn't take effect at all.
    background.trigger_rescan()
    return redirect("/")


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
            "REL_VOLUME_THRESHOLD": form.get("rel_volume_threshold", settings.REL_VOLUME_THRESHOLD),
            "SCAN_INTERVAL_SECONDS": form.get("scan_interval", settings.SCAN_INTERVAL_SECONDS),
            "ADX_LENGTH": form.get("adx_length", settings.ADX_LENGTH),
            "RANGING_VOL_MULTIPLIER": form.get("ranging_vol_multiplier", settings.RANGING_VOL_MULTIPLIER),
            # A checkbox that isn't checked simply isn't submitted at all,
            # so this can't use form.get(key, current-value) like every
            # other field above (that fallback would silently keep the
            # OLD value forever, making it impossible to ever uncheck) -
            # "on" only when Flask actually received the field.
            "REQUIRE_INDEX_AGREEMENT": form.get("require_index_agreement") == "on",
            "REQUIRE_VOLUME_FLOW_AGREEMENT": form.get("require_volume_flow_agreement") == "on",
            "REQUIRE_CANDLE_PATTERN_AGREEMENT": form.get("require_candle_pattern_agreement") == "on",
            "REQUIRE_SECTOR_AGREEMENT": form.get("require_sector_agreement") == "on",
            "REQUIRE_BREADTH_AGREEMENT": form.get("require_breadth_agreement") == "on",
            "BREADTH_THRESHOLD_PCT": form.get("breadth_threshold_pct", settings.BREADTH_THRESHOLD_PCT),
        }
        errors = settings.update(**payload)
        saved = not errors
        if saved:
            background.trigger_rescan()
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
            background.trigger_rescan()
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
        state["results"], settings.TIMEFRAME, settings.MIN_REQUIRED, state["last_scan"],
    )
    return jsonify(result)


@app.route("/api/alerts/recent")
@require_dashboard_password
def api_alerts_recent():
    return jsonify({"alerts": alerts.get_recent(limit=20)})


@app.route("/api/alerts/oi_recent")
@require_dashboard_password
def api_alerts_oi_recent():
    return jsonify({"alerts": alerts.get_recent_oi(limit=20)})


@app.route("/oi-screener")
@require_dashboard_password
def oi_screener_page():
    return render_template(
        "oi_screener.html",
        logged_in=kite_auth.is_logged_in_today(),
        timeframe=settings.TIMEFRAME,
        min_required=settings.MIN_REQUIRED,
    )


@app.route("/api/oi-screener")
@require_dashboard_password
def api_oi_screener():
    # Deliberately NOT every scanned symbol - only rows that are
    # currently in one of the 2/3/4-of-4 parameter screener tiers (see
    # background._apply_param_tier). An unfiltered "every F&O stock's
    # OI" list is mostly noise; this page is meant to answer "of the
    # stocks the confluence screener already flagged, what's their OI
    # actually doing" - not to be a second, independent universe.
    state = get_state()
    results = [r for r in state["results"] if not r.get("error") and r.get("param_tier")]
    return jsonify({"results": results, "min_required": settings.MIN_REQUIRED})


@app.route("/api/alerts/test", methods=["POST"])
@require_dashboard_password
def api_alerts_test():
    return jsonify(alerts.send_test_alert())


@app.route("/api/alerts/discover-chat-id")
@require_dashboard_password
def api_alerts_discover_chat_id():
    return jsonify(alerts.discover_chat_id())


@app.route("/backtest")
@require_dashboard_password
def backtest_page():
    return render_template(
        "backtest.html",
        logged_in=kite_auth.is_logged_in_today(),
        valid_timeframes=config.VALID_TIMEFRAMES,
        default_timeframe=settings.TIMEFRAME,
        param_defs=backtest.PARAM_DEFS,
        default_params=list(backtest.DEFAULT_PARAMS),
        default_required=backtest.DEFAULT_REQUIRED,
        filter_defs=backtest.FILTER_DEFS,
        state=backtest.get_backtest_state(),
        weights_state=backtest.get_weights_state(),
        index_symbols=backtest.INDEX_SYMBOLS,
        watchlist_count=len(settings.WATCHLIST),
    )


_BACKTEST_UNIVERSES = {
    "watchlist": None,       # resolved to settings.WATCHLIST below (evaluated live, not at import time)
    "nifty50": ["NIFTY 50"],
    "sensex": ["SENSEX"],
}


def _resolve_backtest_symbols(form):
    """Which symbols to backtest, per the "Backtest universe" radio on
    the Backtest page - shared by both /api/backtest/start and
    /api/weights/start. Exactly one of three options, not a mix:
    "watchlist" (your normal F&O WATCHLIST, the default), "nifty50"
    (NIFTY 50 alone), or "sensex" (SENSEX alone) - kept as separate,
    single-symbol runs rather than lumping an index in with 100+ F&O
    stocks, since mixing them together would dilute the index's own
    result into a huge stock-only trade list and make it hard to read
    on its own. An unrecognized/missing value falls back to the
    watchlist."""
    universe = form.get("universe", "watchlist")
    symbols = _BACKTEST_UNIVERSES.get(universe, _BACKTEST_UNIVERSES["watchlist"])
    return list(symbols) if symbols is not None else list(settings.WATCHLIST)


@app.route("/api/backtest/start", methods=["POST"])
@require_dashboard_password
def api_backtest_start():
    kite = kite_auth.get_kite_client()
    if kite is None:
        return jsonify({"started": False, "reason": "Not logged in to Kite today."}), 400

    form = request.form
    timeframe = form.get("timeframe", settings.TIMEFRAME)
    if timeframe not in config.VALID_TIMEFRAMES:
        return jsonify({"started": False, "reason": "invalid timeframe"}), 400
    try:
        days = int(form.get("days", 30))
    except ValueError:
        return jsonify({"started": False, "reason": "days must be a number"}), 400
    horizons_raw = form.get("horizons", "5,10,20")
    try:
        horizons = tuple(int(h.strip()) for h in horizons_raw.split(",") if h.strip())
    except ValueError:
        return jsonify({"started": False, "reason": "horizons must be comma-separated numbers"}), 400
    if not horizons:
        return jsonify({"started": False, "reason": "at least one horizon is required"}), 400

    params_raw = form.get("params", "")
    params = tuple(p.strip() for p in params_raw.split(",") if p.strip())
    params = tuple(p for p in params if p in backtest.PARAM_IDS)
    if not params:
        return jsonify({"started": False, "reason": "select at least one parameter"}), 400
    try:
        required = int(form.get("required", backtest.DEFAULT_REQUIRED))
    except ValueError:
        return jsonify({"started": False, "reason": "required must be a number"}), 400
    if not (1 <= required <= len(params)):
        return jsonify({
            "started": False,
            "reason": f"required must be between 1 and {len(params)} (the number of parameters you selected)",
        }), 400

    # The 3 optional live-parity gates (FILTER_DEFS) - same comma-separated
    # convention as "params" above, sent as a "filters" field so a run that
    # opts into none of them (the default, every prior form submission)
    # behaves identically to before this was added.
    filters_raw = form.get("filters", "")
    filters = {f.strip() for f in filters_raw.split(",") if f.strip() and f.strip() in backtest.FILTER_IDS}

    result = backtest.start_backtest(
        kite, symbols=_resolve_backtest_symbols(form), timeframe=timeframe, days=days, horizons=horizons,
        params=params, required=required,
        require_htf="require_htf" in filters,
        require_regime_volume="require_regime_volume" in filters,
        exclude_opening_window="exclude_opening_window" in filters,
        require_volume_flow="require_volume_flow" in filters,
        require_candle_pattern="require_candle_pattern" in filters,
    )
    return jsonify(result)


@app.route("/api/backtest/status")
@require_dashboard_password
def api_backtest_status():
    return jsonify(backtest.get_backtest_state())


@app.route("/journal")
@require_dashboard_password
def journal_page():
    return render_template(
        "journal.html",
        logged_in=kite_auth.is_logged_in_today(),
        default_horizon_bars=journal.DEFAULT_HORIZON_BARS,
        state=journal.get_journal_state(),
    )


@app.route("/api/journal/log", methods=["POST"])
@require_dashboard_password
def api_journal_log():
    """Logs a paper trade from a CURRENT live dashboard row - re-reads
    the row straight from background.get_state()["results"] server-side
    (looked up by symbol) rather than trusting any indicator values the
    client might submit, so a stale/tampered form can't log a trade with
    fabricated readings."""
    form = request.form
    symbol = form.get("symbol", "")
    row = next((r for r in get_state()["results"] if r.get("symbol") == symbol), None)
    if row is None or row.get("error") or row.get("direction") not in ("Bullish", "Bearish"):
        return jsonify({"logged": False, "reason": "No current signal for this symbol to log."}), 400

    try:
        horizon_bars = int(form.get("horizon_bars", journal.DEFAULT_HORIZON_BARS))
    except ValueError:
        return jsonify({"logged": False, "reason": "horizon_bars must be a number"}), 400
    try:
        cost_pct = max(0.0, float(form.get("cost_pct", 0) or 0))
    except ValueError:
        return jsonify({"logged": False, "reason": "cost_pct must be a number"}), 400
    try:
        slippage_pct = max(0.0, float(form.get("slippage_pct", 0) or 0))
    except ValueError:
        return jsonify({"logged": False, "reason": "slippage_pct must be a number"}), 400

    try:
        trade = journal.log_paper_trade(
            row, timeframe=settings.TIMEFRAME, horizon_bars=horizon_bars,
            cost_pct=cost_pct, slippage_pct=slippage_pct,
        )
    except ValueError as exc:
        return jsonify({"logged": False, "reason": str(exc)}), 400
    return jsonify({"logged": True, "trade": trade})


@app.route("/api/journal/delete", methods=["POST"])
@require_dashboard_password
def api_journal_delete():
    trade_id = request.form.get("id", "")
    removed = journal.delete_trade(trade_id)
    return jsonify({"deleted": removed})


@app.route("/journal/export.csv")
@require_dashboard_password
def journal_export_csv():
    import csv
    import io

    trades = journal.get_journal_state()["trades"]
    buf = io.StringIO()
    fieldnames = [
        "id", "symbol", "timeframe", "direction", "horizon_bars", "status",
        "signal_time", "entry_time", "entry_price", "exit_time", "exit_price",
        "return_pct", "mae_pct", "outcome", "cost_pct", "slippage_pct", "logged_at",
    ]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for t in trades:
        writer.writerow(t)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=signal_journal.csv"},
    )


@app.route("/api/weights/start", methods=["POST"])
@require_dashboard_password
def api_weights_start():
    kite = kite_auth.get_kite_client()
    if kite is None:
        return jsonify({"started": False, "reason": "Not logged in to Kite today."}), 400

    form = request.form
    timeframe = form.get("timeframe", settings.TIMEFRAME)
    if timeframe not in config.VALID_TIMEFRAMES:
        return jsonify({"started": False, "reason": "invalid timeframe"}), 400
    try:
        days = int(form.get("days", 30))
    except ValueError:
        return jsonify({"started": False, "reason": "days must be a number"}), 400
    try:
        ref_horizon = int(form.get("ref_horizon", 10))
    except ValueError:
        return jsonify({"started": False, "reason": "ref_horizon must be a number"}), 400
    if ref_horizon <= 0:
        return jsonify({"started": False, "reason": "ref_horizon must be positive"}), 400

    result = backtest.start_weight_computation(
        kite, symbols=_resolve_backtest_symbols(form), timeframe=timeframe, days=days, ref_horizon=ref_horizon,
    )
    return jsonify(result)


@app.route("/api/weights/status")
@require_dashboard_password
def api_weights_status():
    return jsonify(backtest.get_weights_state())


def create_app():
    return app
