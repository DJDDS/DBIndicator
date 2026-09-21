import datetime as dt

import pytest

from app import trial25_execution as ex


def _c(typ, strike, expiry="2026-10-27", lot=50):
    return {
        "instrument_type": typ,
        "strike": float(strike),
        "expiry": dt.date.fromisoformat(expiry),
        "tradingsymbol": f"ABC-{expiry}-{strike}-{typ}",
        "instrument_token": hash((typ, strike, expiry)) & 0xFFFFFF,
        "lot_size": lot,
    }


def test_nearest_expiry_requires_five_dte_and_survives_exit():
    entry = dt.date(2026, 10, 8)
    exit_day = dt.date(2026, 10, 12)
    rows = [
        _c("CE", 100, "2026-10-09"), _c("PE", 100, "2026-10-09"),
        _c("CE", 80), _c("PE", 80),
        _c("CE", 100), _c("PE", 100),
        _c("CE", 120), _c("PE", 120),
    ]
    out = ex.select_structure(
        rows, 101.0, entry, exit_day, implied_move_points=10.0
    )
    assert out["status"] == "OK"
    assert out["expiry"] == "2026-10-27"
    assert out["atm_strike"] == 100.0
    assert out["lower_put"]["strike"] == 80.0
    assert out["upper_call"]["strike"] == 120.0


def test_wing_inside_two_x_target_is_never_used_as_fallback():
    rows = [
        _c("CE", 90), _c("PE", 90),
        _c("CE", 100), _c("PE", 100),
        _c("CE", 110), _c("PE", 110),
    ]
    out = ex.select_structure(
        rows, 100.0, dt.date(2026, 10, 8), dt.date(2026, 10, 12),
        implied_move_points=10.0,
    )
    assert out["status"] == "UNAVAILABLE_WING_CONTRACT"


def test_structure_freezes_exact_four_contract_identities():
    rows = [
        _c("CE", 80), _c("PE", 80), _c("CE", 100), _c("PE", 100),
        _c("CE", 120), _c("PE", 120),
    ]
    out = ex.select_structure(
        rows, 100.0, dt.date(2026, 10, 8), dt.date(2026, 10, 12),
        implied_move_points=10.0,
    )
    identities = out["contract_identities"]
    assert set(identities) == {"atm_call", "atm_put", "lower_put", "upper_call"}
    assert all(x["tradingsymbol"] and x["instrument_token"] is not None for x in identities.values())


def _quote(*, bid=10.0, ask=10.1, bid_qty=500, ask_qty=500, timestamp=None, last_trade=None):
    return {
        "timestamp": timestamp,
        "last_trade_time": last_trade,
        "last_price": 10.05,
        "depth": {
            "buy": [{"price": bid, "quantity": bid_qty, "orders": 2}] if bid is not None else [],
            "sell": [{"price": ask, "quantity": ask_qty, "orders": 2}] if ask is not None else [],
        },
    }


def test_old_last_trade_does_not_reject_fresh_live_executable_book():
    req = dt.datetime(2026, 10, 8, 15, 10, 0)
    recv = dt.datetime(2026, 10, 8, 15, 10, 1)
    q = _quote(
        timestamp=recv,
        last_trade=dt.datetime(2026, 10, 8, 14, 30, 0),
    )
    snap = ex.normalize_live_quote(_c("CE", 100), q, req, recv)
    ok, reason = ex.validate_leg("SELL", snap, 50)
    assert ok is True
    assert reason is None
    assert snap["api_latency_ms"] == 1000.0
    assert snap["last_trade_stale_600s"] is True
    assert snap["two_sided"] is True


def test_latency_over_15_seconds_fails_closed():
    req = dt.datetime(2026, 10, 8, 15, 10, 0)
    recv = dt.datetime(2026, 10, 8, 15, 10, 15, 1000)
    snap = ex.normalize_live_quote(_c("CE", 100), _quote(), req, recv)
    assert ex.validate_leg("SELL", snap, 50) == (False, "QUOTE_LATENCY")


def test_exactly_15_seconds_is_allowed():
    req = dt.datetime(2026, 10, 8, 15, 10, 0)
    recv = dt.datetime(2026, 10, 8, 15, 10, 15)
    snap = ex.normalize_live_quote(_c("CE", 100), _quote(), req, recv)
    assert ex.validate_leg("SELL", snap, 50) == (True, None)


def test_one_sided_book_fails_closed():
    req = dt.datetime(2026, 10, 8, 15, 10, 0)
    recv = req + dt.timedelta(seconds=1)
    snap = ex.normalize_live_quote(_c("CE", 100), _quote(ask=None), req, recv)
    assert ex.validate_leg("SELL", snap, 50) == (False, "ONE_SIDED_BOOK")


def test_required_side_top_quantity_must_cover_one_lot():
    req = dt.datetime(2026, 10, 8, 15, 10, 0)
    recv = req + dt.timedelta(seconds=1)
    sell_snap = ex.normalize_live_quote(_c("CE", 100), _quote(bid_qty=49), req, recv)
    buy_snap = ex.normalize_live_quote(_c("CE", 100), _quote(ask_qty=49), req, recv)
    assert ex.validate_leg("SELL", sell_snap, 50) == (False, "INSUFFICIENT_TOP_QTY")
    assert ex.validate_leg("BUY", buy_snap, 50) == (False, "INSUFFICIENT_TOP_QTY")


def test_fee_model_components_are_pinned():
    fills = [
        {"side": "SELL", "price": 100.0, "quantity": 50},
        {"side": "BUY", "price": 40.0, "quantity": 50},
    ]
    out = ex.calculate_option_charges(fills)
    turnover = 7000.0
    assert out["model_version"] == "ZERODHA_NSE_EQ_OPT_2026_04_V1"
    assert out["brokerage"] == pytest.approx(40.0, abs=1e-8)
    assert out["stt"] == pytest.approx(8.0, abs=1e-8)
    assert out["exchange_transaction_charge"] == pytest.approx(turnover * 0.0003553, abs=1e-8)
    assert out["sebi_fee"] == pytest.approx(turnover * 0.000001, abs=1e-8)
    assert out["stamp_duty"] == pytest.approx(2000.0 * 0.00003, abs=1e-8)
    gst_base = 40.0 + turnover * 0.0003553 + turnover * 0.000001
    assert out["gst"] == pytest.approx(gst_base * 0.18, abs=1e-8)
    assert out["total"] == pytest.approx(
        out["brokerage"] + out["stt"] + out["exchange_transaction_charge"]
        + out["sebi_fee"] + out["stamp_duty"] + out["gst"],
        abs=1e-8,
    )


def test_fee_model_rounds_stt_to_nearest_rupee_half_up():
    below = ex.calculate_option_charges([
        {"side": "SELL", "price": 99.86, "quantity": 50},
    ])
    half = ex.calculate_option_charges([
        {"side": "SELL", "price": 100.0, "quantity": 50},
    ])
    assert 99.86 * 50 * ex.STT_SELL_RATE == pytest.approx(7.4895, abs=1e-12)
    assert below["stt"] == 7.0
    assert half["stt"] == 8.0
