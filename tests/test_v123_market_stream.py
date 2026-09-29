import datetime as dt

from app.v123_market_stream import UniverseMomentumStreamService


def _service():
    return UniverseMomentumStreamService(
        publish_callback=lambda payload: None,
        metadata_provider=lambda: [],
        access_token_getter=lambda: None,
        kite_client_getter=lambda: None,
        api_key="x",
        sleep_fn=lambda seconds: None,
    )


def _append(service, symbol, ts, price, volume, open_=100.0, high=101.0, low=99.0, prev=100.0):
    service._samples[symbol].append({
        "ts": ts,
        "price": price,
        "volume": volume,
        "open": open_,
        "high": high,
        "low": low,
        "prev_close": prev,
    })


def test_opening_drive_can_be_found_without_oi_confirmation():
    svc = _service()
    now = dt.datetime(2026, 9, 23, 9, 35)
    # stock: +0.6% over 5m, volume rate doubles, pressing high
    _append(svc, "ABC", now-dt.timedelta(minutes=10), 100.0, 1000, high=100.7)
    _append(svc, "ABC", now-dt.timedelta(minutes=5), 100.1, 1200, high=100.7)
    _append(svc, "ABC", now-dt.timedelta(minutes=2), 100.45, 1300, high=100.7)
    _append(svc, "ABC", now-dt.timedelta(minutes=1), 100.62, 1400, high=100.7)
    _append(svc, "ABC", now, 100.70, 1600, high=100.70)

    _append(svc, "NIFTY 50", now-dt.timedelta(minutes=5), 25000, 1)
    _append(svc, "NIFTY 50", now, 25020, 2)

    event = svc._event_for(
        "ABC", svc._samples["ABC"][-1],
        {"symbol": "ABC", "atr": 2.0, "prev_close": 100.0},
        {"5m": svc._return("NIFTY 50", now, 300)},
        now,
        direction_info={"state": "BULLISH", "direction": "Bullish", "phase": "CONTINUING"},
    )
    assert event is not None
    assert event["direction"] == "Bullish"
    assert event["event_family"] in ("OPENING_DRIVE", "RANGE_EXPANSION")
    assert event["oi_chg_15m_pct"] is None


def test_pullback_reclaim_is_a_separate_event_family():
    svc = _service()
    now = dt.datetime(2026, 9, 23, 11, 0)
    # Existing +2% day trend, pulled back at t-3m from t-8m, now reclaimed.
    _append(svc, "XYZ", now-dt.timedelta(minutes=10), 101.6, 1000, high=102.4, prev=100.0)
    _append(svc, "XYZ", now-dt.timedelta(minutes=8), 102.0, 1100, high=102.4, prev=100.0)
    _append(svc, "XYZ", now-dt.timedelta(minutes=5), 101.8, 1200, high=102.4, prev=100.0)
    _append(svc, "XYZ", now-dt.timedelta(minutes=3), 101.7, 1300, high=102.4, prev=100.0)
    _append(svc, "XYZ", now-dt.timedelta(minutes=2), 101.75, 1400, high=102.4, prev=100.0)
    _append(svc, "XYZ", now-dt.timedelta(minutes=1), 101.78, 1500, high=102.4, prev=100.0)
    _append(svc, "XYZ", now, 102.05, 1600, high=102.4, prev=100.0)

    _append(svc, "NIFTY 50", now-dt.timedelta(minutes=5), 25000, 1)
    _append(svc, "NIFTY 50", now, 25005, 2)

    event = svc._event_for(
        "XYZ", svc._samples["XYZ"][-1],
        {"symbol": "XYZ", "atr": 2.0, "prev_close": 100.0},
        {"5m": svc._return("NIFTY 50", now, 300)},
        now,
        direction_info={"state": "BULLISH", "direction": "Bullish", "phase": "CONTINUING"},
    )
    assert event is not None
    assert event["event_family"] == "PULLBACK_RECLAIM"
    assert event["direction"] == "Bullish"


def test_no_event_when_price_is_not_doing_anything():
    svc = _service()
    now = dt.datetime(2026, 9, 23, 12, 0)
    _append(svc, "FLAT", now-dt.timedelta(minutes=10), 100.0, 1000, high=100.2, low=99.8)
    _append(svc, "FLAT", now-dt.timedelta(minutes=5), 100.02, 1100, high=100.2, low=99.8)
    _append(svc, "FLAT", now-dt.timedelta(minutes=2), 100.01, 1200, high=100.2, low=99.8)
    _append(svc, "FLAT", now-dt.timedelta(minutes=1), 100.02, 1250, high=100.2, low=99.8)
    _append(svc, "FLAT", now, 100.03, 1300, high=100.2, low=99.8)
    event = svc._event_for(
        "FLAT", svc._samples["FLAT"][-1],
        {"prev_close": 100.0, "atr": 1.5}, {"5m": 0.0}, now,
        direction_info={"state": "BULLISH", "direction": "Bullish", "phase": "WEAKENING"},
    )
    assert event is None



def test_mover_diagnostic_explains_why_event_was_missed():
    svc = _service()
    now = dt.datetime(2026, 9, 23, 11, 0)
    _append(svc, "MISS", now-dt.timedelta(minutes=10), 101.65, 1000, high=102.0, prev=100.0)
    _append(svc, "MISS", now-dt.timedelta(minutes=5), 101.7, 1200, high=102.0, prev=100.0)
    _append(svc, "MISS", now-dt.timedelta(minutes=2), 101.78, 1280, high=102.0, prev=100.0)
    _append(svc, "MISS", now-dt.timedelta(minutes=1), 101.82, 1330, high=102.0, prev=100.0)
    _append(svc, "MISS", now, 101.88, 1370, high=102.0, prev=100.0)

    _append(svc, "NIFTY 50", now-dt.timedelta(minutes=5), 25000, 1)
    _append(svc, "NIFTY 50", now, 25040, 2)
    nifty = {"5m": svc._return("NIFTY 50", now, 300)}

    event = svc._event_for(
        "MISS", svc._samples["MISS"][-1],
        {"symbol":"MISS","prev_close":100.0,"atr":2.0},
        nifty, now,
        direction_info={"state": "BULLISH", "direction": "Bullish", "phase": "CONTINUING"},
    )
    assert event is None
    diag = svc._event_diagnostic(
        "MISS", svc._samples["MISS"][-1],
        {"symbol":"MISS","prev_close":100.0,"atr":2.0},
        nifty, now, event=event,
        direction_info={"state": "BULLISH", "direction": "Bullish", "phase": "CONTINUING"},
    )
    assert diag["qualified"] is False
    assert diag["reason"] == "NO_EVENT_FAMILY_QUALIFIED"
    assert diag["failed_gates"]



def test_lookback_sample_must_be_close_to_requested_age():
    svc = _service()
    now = dt.datetime(2026, 9, 24, 11, 0, 0)
    _append(svc, "ABC", now-dt.timedelta(minutes=20), 100.0, 1000)
    _append(svc, "ABC", now, 101.0, 1200)

    # A 20-minute-old point cannot masquerade as 3m/5m/10m history.
    assert svc._sample_at("ABC", now, 180) is None
    assert svc._sample_at("ABC", now, 300) is None
    assert svc._sample_at("ABC", now, 600) is None


def test_lookback_sample_accepts_nearby_timestamp_within_tolerance():
    svc = _service()
    now = dt.datetime(2026, 9, 24, 11, 0, 0)
    _append(svc, "ABC", now-dt.timedelta(minutes=5, seconds=20), 100.0, 1000)
    _append(svc, "ABC", now, 101.0, 1200)
    sample = svc._sample_at("ABC", now, 300)
    assert sample is not None
    assert sample["price"] == 100.0


def test_market_open_closes_exactly_at_1530():
    from app import v123_market_stream as m
    assert m._market_open(dt.datetime(2026, 9, 24, 15, 29, 59)) is True
    assert m._market_open(dt.datetime(2026, 9, 24, 15, 30, 0)) is False


def test_snapshot_carries_private_whole_universe_quant_rows():
    svc = _service()
    now = dt.datetime(2026, 9, 24, 11, 0)
    svc._connected = True
    svc._latest = {
        "ABC": {
            "last_price": 101.0,
            "volume_traded": 1500,
            "ohlc": {"open": 100.0, "high": 101.0, "low": 99.5, "close": 100.0},
        },
        "XYZ": {
            "last_price": 98.0,
            "volume_traded": 1800,
            "ohlc": {"open": 100.0, "high": 100.2, "low": 97.8, "close": 100.0},
        },
        "NIFTY 50": {
            "last_price": 25020.0,
            "volume_traded": 2,
            "ohlc": {"open": 25000.0, "high": 25030.0, "low": 24990.0, "close": 25000.0},
        },
    }
    for symbol, price, volume, prev in (
        ("ABC", 101.0, 1500, 100.0),
        ("XYZ", 98.0, 1800, 100.0),
        ("NIFTY 50", 25020.0, 2, 25000.0),
    ):
        _append(svc, symbol, now-dt.timedelta(minutes=5), prev, max(1, volume-200), prev=prev)
        _append(svc, symbol, now, price, volume, prev=prev)

    svc.metadata_provider = lambda: [
        {"symbol": "ABC", "prev_close": 100.0, "sector": "TEST1"},
        {"symbol": "XYZ", "prev_close": 100.0, "sector": "TEST2"},
    ]
    snap = svc._build_snapshot(now)
    assert snap["nifty"]["live_price"] == 25020.0
    assert {row["symbol"] for row in snap["quant_rows"]} == {"ABC", "XYZ"}
    assert {row["sector"] for row in snap["quant_rows"]} == {"TEST1", "TEST2"}
    by_symbol = {row["symbol"]: row for row in snap["quant_rows"]}
    assert by_symbol["ABC"]["day_change_pct"] == 1.0
    assert by_symbol["XYZ"]["day_change_pct"] == -2.0
    assert "discovery_reason" in by_symbol["ABC"]
    assert "ret_5m_pct" in by_symbol["XYZ"]


def test_tick_callback_path_is_lightweight_and_defers_publish():
    svc = _service()
    now = dt.datetime(2026, 9, 28, 12, 0)
    svc._token_to_symbol = {1: "ABC"}
    called = {"publish": 0}

    def _unexpected(*args, **kwargs):
        called["publish"] += 1

    svc._maybe_publish = _unexpected
    svc._handle_ticks([{
        "instrument_token": 1,
        "last_price": 101.0,
        "volume_traded": 1500,
        "ohlc": {"open": 100.0, "high": 101.0, "low": 99.5, "close": 100.0},
    }], now)

    assert called["publish"] == 0
    assert svc._latest["ABC"]["last_price"] == 101.0
    assert svc._last_tick_at == now


def test_snapshot_excludes_stale_symbols_and_reports_feed_health():
    svc = _service()
    now = dt.datetime(2026, 9, 28, 12, 0)
    svc._connected = True
    svc._active = True
    svc._last_tick_at = now - dt.timedelta(seconds=60)
    svc._tokens = {"ABC": 1, "NIFTY 50": 2}
    svc._latest = {
        "ABC": {"last_price": 101.0},
        "NIFTY 50": {"last_price": 25000.0},
    }
    _append(svc, "ABC", now-dt.timedelta(seconds=60), 101.0, 1500, prev=100.0)
    _append(svc, "NIFTY 50", now-dt.timedelta(seconds=60), 25000.0, 2, prev=24900.0)

    snap = svc._build_snapshot(now)
    assert snap["feed_health"]["fresh"] is False
    assert snap["fresh_symbol_count"] == 0
    assert snap["quant_rows"] == []
    assert snap["status"] == "CONNECTING"


def test_sector_index_is_context_only_and_enriches_stock_rows():
    svc = _service()
    now = dt.datetime(2026, 9, 29, 10, 15)
    svc._connected = True
    svc._active = True
    svc._last_tick_at = now
    svc._underlying_symbols = {"HEROMOTOCO"}
    svc._context_symbols = {"NIFTY 50", "NIFTY AUTO"}
    svc._tokens = {"HEROMOTOCO": 1, "NIFTY 50": 2, "NIFTY AUTO": 3}
    svc._latest = {
        "HEROMOTOCO": {
            "last_price": 540.5,
            "volume_traded": 1500,
            "ohlc": {"open": 538.0, "high": 541.0, "low": 536.0, "close": 538.0},
        },
        "NIFTY 50": {
            "last_price": 22450.0,
            "volume_traded": 1,
            "ohlc": {"open": 22550.0, "high": 22560.0, "low": 22440.0, "close": 22550.0},
        },
        "NIFTY AUTO": {
            "last_price": 25100.0,
            "volume_traded": 1,
            "ohlc": {"open": 25300.0, "high": 25320.0, "low": 25090.0, "close": 25300.0},
        },
    }
    for symbol, old_price, new_price, old_vol, new_vol, prev in (
        ("HEROMOTOCO", 539.0, 540.5, 1300, 1500, 538.0),
        ("NIFTY 50", 22500.0, 22450.0, 1, 1, 22550.0),
        ("NIFTY AUTO", 25250.0, 25100.0, 1, 1, 25300.0),
    ):
        _append(svc, symbol, now-dt.timedelta(minutes=5), old_price, old_vol, prev=prev)
        _append(svc, symbol, now, new_price, new_vol, prev=prev)

    svc.metadata_provider = lambda: [{
        "symbol": "HEROMOTOCO",
        "prev_close": 538.0,
        "sector": "NIFTY AUTO",
    }]
    snap = svc._build_snapshot(now)

    assert snap["universe_count"] == 1
    assert set(snap["sector_contexts"]) == {"NIFTY AUTO"}
    assert snap["sector_contexts"]["NIFTY AUTO"]["ret_5m_pct"] < 0
    assert {row["symbol"] for row in snap["quant_rows"]} == {"HEROMOTOCO"}
    row = snap["quant_rows"][0]
    assert row["sector_index"] == "NIFTY AUTO"
    assert row["sector_ret_5m_pct"] < 0
    assert row["relative_5m_vs_sector_pct"] > 0


def test_direction_lock_requires_repeated_evidence_and_ignores_single_pullback():
    svc = _service()
    t0 = dt.datetime(2026, 9, 29, 10, 0)

    states = []
    for i, ret in enumerate((0.18, 0.16, 0.17, 0.15)):
        states.append(svc._update_direction_lock(
            "ABC", t0 + dt.timedelta(minutes=i), ret, 0.03, 0.04
        ))
    assert states[0]["state"] == "NEUTRAL"
    assert states[-1]["state"] == "BULLISH"
    locked_since = states[-1]["since"]

    # One ordinary one-minute pullback cannot flip the visible direction.
    pullback = svc._update_direction_lock(
        "ABC", t0 + dt.timedelta(minutes=4), -0.12, -0.02, -0.03
    )
    assert pullback["state"] == "BULLISH"
    assert pullback["phase"] in ("PULLBACK", "WEAKENING", "CONTINUING")
    assert pullback["since"] == locked_since


def test_direction_lock_reversal_must_pass_through_neutral():
    svc = _service()
    t0 = dt.datetime(2026, 9, 29, 10, 0)

    for i, ret in enumerate((0.20, 0.18, 0.19, 0.17)):
        state = svc._update_direction_lock(
            "ABC", t0 + dt.timedelta(minutes=i), ret, 0.02, 0.03
        )
    assert state["state"] == "BULLISH"

    seen_neutral = False
    bearish = None
    for j, ret in enumerate((-0.35, -0.32, -0.34, -0.30, -0.28, -0.26), start=4):
        state = svc._update_direction_lock(
            "ABC", t0 + dt.timedelta(minutes=j), ret, -0.05, -0.06
        )
        if state["state"] == "NEUTRAL":
            seen_neutral = True
        if state["state"] == "BEARISH":
            bearish = state
            break

    assert seen_neutral is True
    assert bearish is not None


def test_event_is_hidden_until_direction_is_locked():
    svc = _service()
    now = dt.datetime(2026, 9, 29, 10, 0)
    _append(svc, "ABC", now-dt.timedelta(minutes=5), 100.0, 1000, high=101.0)
    _append(svc, "ABC", now-dt.timedelta(minutes=1), 100.5, 1200, high=101.0)
    _append(svc, "ABC", now, 100.8, 1500, high=100.8)

    event = svc._event_for(
        "ABC", svc._samples["ABC"][-1],
        {"prev_close": 100.0, "atr": 2.0},
        {"5m": 0.0}, now,
        direction_info={"state": "NEUTRAL", "direction": None, "phase": "NEUTRAL"},
    )
    assert event is None

    diag = svc._event_diagnostic(
        "ABC", svc._samples["ABC"][-1],
        {"prev_close": 100.0, "atr": 2.0},
        {"5m": 0.0}, now,
        event=None,
        direction_info={"state": "NEUTRAL", "direction": None, "phase": "NEUTRAL"},
    )
    assert diag["reason"] == "DIRECTION_NOT_LOCKED"
