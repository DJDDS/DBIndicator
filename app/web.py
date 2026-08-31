import functools
import logging
import json

import pandas as pd
from flask import Flask, jsonify, redirect, render_template, request, Response

from . import alerts, backtest, background, config, delivery, early_signal, indicators, journal, kite_auth, scanner, v8_dual, v9_playbooks, derivative_intelligence
from .background import get_state, start_background_scanner
from .config import settings
from .insights import generate_insights, insights_enabled
from .oi_view import select_oi_screener_rows, oi_history_readiness, serialize_oi_screener_row, live_market_state, live_opportunity_radar

log = logging.getLogger(__name__)

app = Flask(__name__)
_scanner_started = False
_STARTED_AT = scanner.now_ist().isoformat(timespec="seconds")


def _dashboard_counts(results):
    rows = list(results or [])
    opportunity = live_opportunity_radar(rows)
    return {
        "radar": sum(1 for r in rows if r.get("radar_rank") is not None),
        "opportunities": opportunity["counts"]["displayed"],
        "intraday": sum(1 for r in rows if r.get("shortlist_rank") is not None),
        "swing": sum(1 for r in rows if r.get("swing_rank") is not None),
        "bullish": sum(1 for r in rows if (r.get("trade_direction") or r.get("direction")) == "Bullish"),
        "bearish": sum(1 for r in rows if (r.get("trade_direction") or r.get("direction")) == "Bearish"),
    }


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
    all_results = state["results"]
    scan_health = v9_playbooks.scan_health_counts(all_results)
    scan_failures = v9_playbooks.scan_failure_details(all_results, state.get("scan_symbol_health") or {})
    market_state = live_market_state(all_results)
    opportunity_radar = live_opportunity_radar(all_results)

    return render_template(
        "index.html",
        logged_in=logged_in,
        login_url=login_url,
        results=all_results,
        total_scanned=scan_health["attempted"],
        valid_scanned=scan_health["valid"],
        scan_errors=scan_health["errors"],
        scan_failures=scan_failures,
        market_state=market_state,
        opportunity_radar=opportunity_radar,
        live_counts=_dashboard_counts(all_results),
        last_scan=state["last_scan"],
        last_error=state["last_error"],
        timeframe=config.WATCHLIST_TIMEFRAME,
        min_required=settings.MIN_REQUIRED,
        macd_preset=settings.MACD_PRESET,
        rsi_length=settings.RSI_LENGTH,
        rsi_smooth_length=settings.RSI_SMOOTH_LENGTH,
        macd_fast=settings.MACD_CUSTOM_FAST,
        macd_slow=settings.MACD_CUSTOM_SLOW,
        macd_signal=settings.MACD_CUSTOM_SIGNAL,
        bb_length=settings.BB_LENGTH,
        rel_volume_threshold=settings.REL_VOLUME_THRESHOLD,
        valid_timeframes=config.VALID_TIMEFRAMES,
        valid_presets=config.VALID_MACD_PRESETS,
        quick_error=request.args.get("quick_error"),
        insights_enabled=insights_enabled(),
        telegram_enabled=alerts.telegram_enabled(),
        min_early_score=settings.MIN_EARLY_SCORE,
        shortlist_max=settings.SHORTLIST_MAX,
        require_oi_agreement=settings.REQUIRE_OI_AGREEMENT,
        git_commit=config.GIT_COMMIT,
        git_message=config.GIT_MESSAGE,
        started_at=_STARTED_AT,
        btst_window=scanner.btst_read_window(),
        btst_now=scanner.now_ist().strftime("%H:%M"),
        btst_alert_time=settings.BTST_ALERT_TIME,
        screen_param_defs=background.SCREEN_PARAM_DEFS,
        opening_window_minutes=indicators.OPENING_WINDOW_MINUTES,
        opening_window_end="9:%02d" % (15 + indicators.OPENING_WINDOW_MINUTES),
        index_direction=state.get("index_direction"),
        index_close=state.get("index_close"),
        index_chg_pct=state.get("index_chg_pct"),
        require_index_agreement=settings.REQUIRE_INDEX_AGREEMENT,
        require_candle_pattern_agreement=settings.REQUIRE_CANDLE_PATTERN_AGREEMENT,
        require_sector_agreement=settings.REQUIRE_SECTOR_AGREEMENT,
        require_breadth_agreement=settings.REQUIRE_BREADTH_AGREEMENT,
        breadth=state.get("breadth"),
        breadth_threshold_pct=settings.BREADTH_THRESHOLD_PCT,
        atr_length=settings.ATR_LENGTH,
        atr_stop_multiplier=settings.ATR_STOP_MULTIPLIER,
        atr_target_multiplier=settings.ATR_TARGET_MULTIPLIER,
        risk_budget=journal.get_risk_budget_state(),
        vol_contraction_lookback=settings.VOL_CONTRACTION_LOOKBACK,
        max_entry_extension_atr=settings.MAX_ENTRY_EXTENSION_ATR,
        min_atr_pct=settings.MIN_ATR_PCT,
        # Drives the BTST-meaning honesty layer on the dashboard - see
        # config.BTST_TIMEFRAMES for why a Close@/NR7 reading taken on a
        # 15-minute bar must not be presented as the daily one.
        timeframe_label=config.TIMEFRAME_LABELS.get(config.WATCHLIST_TIMEFRAME, config.WATCHLIST_TIMEFRAME),
        is_btst_timeframe=config.WATCHLIST_TIMEFRAME in config.BTST_TIMEFRAMES,
    )


@app.route("/api/dashboard-state")
@require_dashboard_password
def api_dashboard_state():
    state = get_state()
    rows = state.get("results") or []
    health = v9_playbooks.scan_health_counts(rows)
    return jsonify({
        "last_scan": state.get("last_scan"),
        "last_error": state.get("last_error"),
        "total_scanned": health["attempted"],
        "valid_scanned": health["valid"],
        "error_count": health["errors"],
        "scan_health": health,
        "scan_failures": v9_playbooks.scan_failure_details(rows, state.get("scan_symbol_health") or {}),
        "market_state": live_market_state(rows),
        "opportunity_radar": live_opportunity_radar(rows),
        "counts": _dashboard_counts(rows),
        "scan_interval_seconds": settings.SCAN_INTERVAL_SECONDS,
        "market_open": scanner.is_market_open(),
    })


@app.route("/api/v8-dashboard")
@require_dashboard_password
def api_v8_dashboard():
    state = get_state()
    payload = v9_playbooks.dashboard_payload(state)
    payload["market_open"] = scanner.is_market_open()
    payload["market_state"] = live_market_state(state.get("results") or [])
    payload["opportunity_radar"] = live_opportunity_radar(state.get("results") or [])
    payload["scan_interval_seconds"] = settings.SCAN_INTERVAL_SECONDS
    payload["option_forward"] = derivative_intelligence.get_shadow_stats()
    payload["option_forward_swing"] = derivative_intelligence.get_shadow_stats("swing")
    return jsonify(payload)


@app.route("/api/option-shadow/export")
@require_dashboard_password
def api_option_shadow_export():
    """Download forward option-validation state before a Railway redeploy."""
    state = derivative_intelligence.load_shadow_state()
    body = json.dumps(state, indent=2, default=str)
    headers = {"Content-Disposition": "attachment; filename=v82_option_forward_validation.json"}
    return Response(body, mimetype="application/json", headers=headers)


@app.route("/quick-settings", methods=["POST"])
@require_dashboard_password
def quick_settings():
    """A compact settings panel lives on the dashboard itself (MACD preset, RSI/EMA/BB lengths, min-required) so the common tweaks
    don't need a trip to /settings. Only forwards fields that were
    actually submitted - unlike the /settings page's form, this never
    touches WATCHLIST/scan interval/MACD custom values, so there's no
    risk of accidentally wiping those from a partial submission."""
    form = request.form
    field_map = {
        "macd_preset": "MACD_PRESET",
        "rsi_length": "RSI_LENGTH",
        "rsi_smooth_length": "RSI_SMOOTH_LENGTH",
        "macd_fast": "MACD_CUSTOM_FAST",
        "macd_slow": "MACD_CUSTOM_SLOW",
        "macd_signal": "MACD_CUSTOM_SIGNAL",
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
            # Live F&O early-movement controls.
            "WATCHLIST": form.get("watchlist", ""),  # research/backtest universe only; live refreshes F&O from Kite
            "SCAN_INTERVAL_SECONDS": form.get("scan_interval_seconds", settings.SCAN_INTERVAL_SECONDS),
            "COMPRESSION_RADAR_SCORE": form.get("compression_radar_score", settings.COMPRESSION_RADAR_SCORE),
            "TOD_RVOL_MIN": form.get("tod_rvol_min", settings.TOD_RVOL_MIN),
            "TOD_RVOL_STRONG_NO_OI": form.get("tod_rvol_strong_no_oi", settings.TOD_RVOL_STRONG_NO_OI),
            "MAX_ENTRY_EXTENSION_ATR": form.get("max_entry_extension_atr", settings.MAX_ENTRY_EXTENSION_ATR),
            "SHORTLIST_MAX": form.get("shortlist_max", settings.SHORTLIST_MAX),
            # Risk/position-sizing controls are display guidance only; they never decide shortlist membership.
            "ATR_LENGTH": form.get("atr_length", settings.ATR_LENGTH),
            "ATR_STOP_MULTIPLIER": form.get("atr_stop_multiplier", settings.ATR_STOP_MULTIPLIER),
            "ATR_TARGET_MULTIPLIER": form.get("atr_target_multiplier", settings.ATR_TARGET_MULTIPLIER),
            "ACCOUNT_CAPITAL": form.get("account_capital", settings.ACCOUNT_CAPITAL),
            "RISK_PER_TRADE_PCT": form.get("risk_per_trade_pct", settings.RISK_PER_TRADE_PCT),
            "MAX_DAILY_RISK_PCT": form.get("max_daily_risk_pct", settings.MAX_DAILY_RISK_PCT),
            "MAX_CONCURRENT_POSITIONS": form.get("max_concurrent_positions", settings.MAX_CONCURRENT_POSITIONS),
        }
        errors = settings.update(**payload)
        saved = not errors
        if saved:
            background.trigger_rescan()
    scan_state = get_state()
    live_rows = scan_state.get("results") or []
    live_scan_health = v9_playbooks.scan_health_counts(live_rows)
    live_scan_count = live_scan_health["attempted"]
    valid_live_scan_count = live_scan_health["valid"]
    scan_error_count = live_scan_health["errors"]
    scan_failures = v9_playbooks.scan_failure_details(live_rows, scan_state.get("scan_symbol_health") or {})
    research_watchlist_count = len(settings.WATCHLIST)
    live_fno_count = None
    if kite_auth.is_logged_in_today():
        try:
            kite = kite_auth.get_kite_client()
            if kite is not None:
                live_fno_count = len(scanner.get_fno_stock_list(kite))
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not count live F&O universe on settings page: %s", exc)
    return render_template(
        "settings.html",
        s=settings.as_dict(),
        research_watchlist_count=research_watchlist_count,
        live_fno_count=live_fno_count,
        last_scan_count=live_scan_count,
        valid_live_scan_count=valid_live_scan_count,
        scan_error_count=scan_error_count,
        scan_failures=scan_failures,
        settings_last_scan=scan_state.get("last_scan"),
        errors=errors,
        saved=saved,
        fno_error=fno_error,
        valid_timeframes=config.VALID_TIMEFRAMES,
        valid_presets=config.VALID_MACD_PRESETS,
        logged_in=kite_auth.is_logged_in_today(),
        telegram_enabled=alerts.telegram_enabled(),
        min_early_score=settings.MIN_EARLY_SCORE,
        shortlist_max=settings.SHORTLIST_MAX,
        require_oi_agreement=settings.REQUIRE_OI_AGREEMENT,
        btst_window=scanner.btst_read_window(),
        btst_now=scanner.now_ist().strftime("%H:%M"),
        btst_alert_time=settings.BTST_ALERT_TIME,
        telegram_token_set=bool(config.TELEGRAM_BOT_TOKEN),
        delivery_status=delivery.get_status(),
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
        timeframe=config.WATCHLIST_TIMEFRAME,
        valid_timeframes=config.VALID_TIMEFRAMES,
    )


@app.route("/api/chart/<symbol>")
@require_dashboard_password
def chart_data(symbol):
    kite = kite_auth.get_kite_client()
    if kite is None:
        return jsonify({"error": "Not logged in to Kite today."}), 400

    timeframe = request.args.get("timeframe", config.WATCHLIST_TIMEFRAME)
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
            # Session VWAP (resets daily, intraday timeframes only - empty
            # on day/week) and anchored VWAP (since the current confluence
            # trend leg began - see indicators.compute_avwap_series,
            # meaningful on every timeframe) - same two lines the
            # dashboard's VWAP/AVWAP badges show, plotted here so you can
            # see exactly where they've been tracking, not just their
            # current value.
            "vwap": _points(indicators.session_vwap_series(df, timeframe)),
            "avwap": _points(indicators.compute_avwap_series(series)),
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
        state["results"], config.WATCHLIST_TIMEFRAME, settings.MIN_REQUIRED, state["last_scan"],
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
        timeframe=config.WATCHLIST_TIMEFRAME,
        min_required=settings.MIN_REQUIRED,
    )


@app.route("/api/oi-screener")
@require_dashboard_password
def api_oi_screener():
    # Base universe = the live NSE stock-F&O universe with a valid futures
    # OI quote. OI can lead price/technical alignment, so the OI radar must
    # not wait for a legacy 2+/3+/4 parameter tier before surfacing a name.
    state = get_state()
    threshold = early_signal.OI_Z_THRESHOLD
    selected = select_oi_screener_rows(
        state["results"], unusual_only=False, min_tier=None, z_threshold=threshold
    )
    rolling = oi_history_readiness(selected, min_tier=None)
    results = [serialize_oi_screener_row(row) for row in selected]
    return jsonify({
        "results": results,
        "min_required": settings.MIN_REQUIRED,
        "oi_z_threshold": threshold,
        "oi_history": scanner.oi_history_status(),
        "rolling_history": rolling,
    })


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
    _bt_bounds = backtest.backtest_day_bounds(config.WATCHLIST_TIMEFRAME)
    return render_template(
        "backtest.html",
        logged_in=kite_auth.is_logged_in_today(),
        valid_timeframes=config.VALID_TIMEFRAMES,
        default_timeframe=config.WATCHLIST_TIMEFRAME,
        param_defs=backtest.PARAM_DEFS,
        default_params=list(backtest.DEFAULT_PARAMS),
        default_required=backtest.DEFAULT_REQUIRED,
        filter_defs=backtest.FILTER_DEFS,
        state=backtest.get_backtest_state(),
        bt_days_min=_bt_bounds[0], bt_days_max=_bt_bounds[1], bt_days_default=_bt_bounds[2],
        backtest_day_bounds={tf: backtest.backtest_day_bounds(tf) for tf in config.VALID_TIMEFRAMES},
        weights_state=backtest.get_weights_state(),
        ablation_state=backtest.get_ablation_state(),
        early_research_state=backtest.get_early_research_state(),
        ablation_gate_count=len(backtest.ABLATION_GATES),
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


@app.route("/api/early-research/start", methods=["POST"])
@require_dashboard_password
def api_early_research_start():
    kite = kite_auth.get_kite_client()
    if kite is None:
        return jsonify({"started": False, "reason": "Not logged in to Kite today."}), 400
    timeframe = request.form.get("timeframe", config.WATCHLIST_TIMEFRAME)
    if timeframe not in ("15minute", "4hour"):
        return jsonify({"started": False, "reason": "primary research supports only 15minute or 4hour"}), 400
    try:
        days = int(request.form.get("days", 30))
    except ValueError:
        return jsonify({"started": False, "reason": "days must be a number"}), 400
    lo, hi, default = backtest.backtest_day_bounds(timeframe)
    days = max(lo, min(days or default, hi))
    try:
        symbols = scanner.get_fno_stock_list(kite)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"started": False, "reason": f"Could not load live F&O universe: {exc}"}), 400
    if not symbols:
        return jsonify({"started": False, "reason": "No NSE stock-F&O symbols returned by Kite."}), 400
    mode = request.form.get("mode", "legacy")
    if mode not in ("legacy", "v8_fast", "v9_fast", "v91_fast", "v91_bear_final"):
        return jsonify({"started": False, "reason": "unsupported research mode"}), 400
    if mode in ("v91_fast", "v91_bear_final"):
        timeframe = "15minute"
        days = 180
    return jsonify(backtest.start_early_movement_research(
        kite, symbols=symbols, timeframe=timeframe, days=days, universe_is_full_fno=True,
        fast_v8=(mode in ("v8_fast", "v9_fast", "v91_fast", "v91_bear_final")),
        research_mode=mode,
    ))


@app.route("/api/early-research/status")
@require_dashboard_password
def api_early_research_status():
    return jsonify(backtest.get_early_research_state())


@app.route("/api/backtest/start", methods=["POST"])
@require_dashboard_password
def api_backtest_start():
    kite = kite_auth.get_kite_client()
    if kite is None:
        return jsonify({"started": False, "reason": "Not logged in to Kite today."}), 400

    form = request.form
    timeframe = form.get("timeframe", config.WATCHLIST_TIMEFRAME)
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

    # The optional live-parity gates (FILTER_DEFS) - same comma-separated
    # convention as "params" above, sent as a "filters" field so a run that
    # opts into none of them (the default, every prior form submission)
    # behaves identically to before this was added.
    filters_raw = form.get("filters", "")
    filters = {f.strip() for f in filters_raw.split(",") if f.strip() and f.strip() in backtest.FILTER_IDS}

    lo, hi, _ = backtest.backtest_day_bounds(timeframe)
    if days < lo:
        return jsonify({"started": False, "reason":
            f"{days} days is too short for {timeframe} candles - the indicators can't warm up, "
            f"so every symbol would be skipped. Use at least {lo} days."}), 400
    days = min(days, hi)

    result = backtest.start_backtest(
        kite, symbols=_resolve_backtest_symbols(form), timeframe=timeframe, days=days, horizons=horizons,
        params=params, required=required,
        require_htf="require_htf" in filters,
        require_regime_volume="require_regime_volume" in filters,
        exclude_opening_window="exclude_opening_window" in filters,
        require_candle_pattern="require_candle_pattern" in filters,
        require_macd_hist="require_macd_hist" in filters,
        require_big_candle="require_big_candle" in filters,
        require_strong_close="require_strong_close" in filters,
        require_entry_location="require_entry_location" in filters,
        require_atr_floor="require_atr_floor" in filters,
        require_oi_agreement="require_oi_agreement" in filters,
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
        confidence=journal.get_confidence_stats(),
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
            row, timeframe=config.WATCHLIST_TIMEFRAME, horizon_bars=horizon_bars,
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
    timeframe = form.get("timeframe", config.WATCHLIST_TIMEFRAME)
    if timeframe not in config.VALID_TIMEFRAMES:
        return jsonify({"started": False, "reason": "invalid timeframe"}), 400
    try:
        days = int(form.get("days", 30))
    except ValueError:
        return jsonify({"started": False, "reason": "days must be a number"}), 400
    try:
        ref_horizon = int(form.get("ref_horizon", 3))
    except ValueError:
        return jsonify({"started": False, "reason": "ref_horizon must be a number"}), 400
    if ref_horizon <= 0:
        return jsonify({"started": False, "reason": "ref_horizon must be positive"}), 400

    lo, hi, _ = backtest.backtest_day_bounds(timeframe)
    if days < lo:
        return jsonify({"started": False, "reason":
            f"{days} days is too short for {timeframe} candles - the indicators can't warm up, "
            f"so every symbol would be skipped. Use at least {lo} days."}), 400
    days = min(days, hi)

    result = backtest.start_weight_computation(
        kite, symbols=_resolve_backtest_symbols(form), timeframe=timeframe, days=days, ref_horizon=ref_horizon,
    )
    return jsonify(result)


@app.route("/api/weights/status")
@require_dashboard_password
def api_weights_status():
    return jsonify(backtest.get_weights_state())


@app.route("/api/overnight/start", methods=["POST"])
@require_dashboard_password
def api_overnight_start():
    """Does the BTST/STBT premise hold? Enter at the signal bar's close,
    exit next open and next close - the trade as actually taken, which no
    other endpoint here can express."""
    kite = kite_auth.get_kite_client()
    if kite is None:
        return jsonify({"started": False, "reason": "not logged in to Kite"}), 400
    form = request.form
    universe = form.get("universe", "watchlist")
    symbols = list(settings.WATCHLIST) if universe == "watchlist" else list(settings.WATCHLIST)
    try:
        days = int(form.get("days", 365))
    except (TypeError, ValueError):
        days = 365
    lo, hi, _d = backtest.backtest_day_bounds("day")
    if not (lo <= days <= hi):
        return jsonify({"started": False,
                        "reason": f"days must be between {lo} and {hi} for daily candles"}), 400
    return jsonify(backtest.start_overnight_backtest(
        kite, symbols, timeframe="day", days=days,
        require_up_day=form.get("require_up_day", "on") == "on"))


@app.route("/api/overnight/status")
@require_dashboard_password
def api_overnight_status():
    return jsonify(backtest.get_overnight_state())


@app.route("/api/ablation/start", methods=["POST"])
@require_dashboard_password
def api_ablation_start():
    """Kicks off the gate-ablation sweep: one baseline backtest with every
    optional gate off, then one run per gate with only that gate on, so
    each gate's real contribution is measurable instead of assumed."""
    kite = kite_auth.get_kite_client()
    if kite is None:
        return jsonify({"started": False, "reason": "Not logged in to Kite today."}), 400

    form = request.form
    timeframe = form.get("timeframe", config.WATCHLIST_TIMEFRAME)
    if timeframe not in config.VALID_TIMEFRAMES:
        return jsonify({"started": False, "reason": "invalid timeframe"}), 400
    try:
        days = int(form.get("days", 30))
    except ValueError:
        return jsonify({"started": False, "reason": "days must be a number"}), 400
    try:
        ref_horizon = int(form.get("ref_horizon", 3))
    except ValueError:
        return jsonify({"started": False, "reason": "ref_horizon must be a number"}), 400
    if ref_horizon <= 0:
        return jsonify({"started": False, "reason": "ref_horizon must be positive"}), 400

    lo, hi, _ = backtest.backtest_day_bounds(timeframe)
    if days < lo:
        return jsonify({"started": False, "reason":
            f"{days} days is too short for {timeframe} candles - the indicators can't warm up, "
            f"so every symbol would be skipped. Use at least {lo} days."}), 400
    days = min(days, hi)

    return jsonify(backtest.start_gate_ablation(
        kite, symbols=_resolve_backtest_symbols(form), timeframe=timeframe,
        days=days, ref_horizon=ref_horizon,
    ))


@app.route("/api/ablation/status")
@require_dashboard_password
def api_ablation_status():
    return jsonify(backtest.get_ablation_state())


def create_app():
    return app
