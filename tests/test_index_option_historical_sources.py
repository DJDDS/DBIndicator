import pandas as pd
import pytest

from app.index_option_historical_sources import (
    build_dhan_proxy_ledger,
    dhan_proxy_expressions,
    fetch_dhan_expired_options,
    map_signal_to_dhan_proxy_pnl,
    normalize_dhan_rolling_response,
    validate_executable_quote_archive,
)


def _dhan_payload():
    return {
        "data": {
            "ce": {
                "open": [100.0, 102.0],
                "high": [105.0, 106.0],
                "low": [98.0, 101.0],
                "close": [103.0, 104.0],
                "iv": [12.1, 12.4],
                "volume": [1000, 1200],
                "strike": [25000.0, 25000.0],
                "oi": [50000, 51000],
                "spot": [25010.0, 25020.0],
                "timestamp": [1756698300, 1756698360],
            },
            "pe": None,
        }
    }


def test_dhan_rolling_normalizer_is_explicitly_proxy_only():
    frame = normalize_dhan_rolling_response(
        _dhan_payload(),
        expression="ATM",
        option_type="CALL",
    )
    assert len(frame) == 2
    assert frame.iloc[0]["data_source"] == "DHAN_ROLLING_EXPIRED_OPTIONS"
    assert frame.iloc[0]["data_quality"] == "PROXY_OHLC"
    assert bool(frame.iloc[0]["executable_quote"]) is False
    assert frame.iloc[0]["expression"] == "ATM"
    assert frame.iloc[0]["option_type"] == "CALL"
    assert "best_bid" not in frame.columns
    assert "best_ask" not in frame.columns
    assert pd.api.types.is_datetime64_any_dtype(frame["timestamp"])


def test_dhan_rolling_normalizer_rejects_mismatched_arrays():
    payload = _dhan_payload()
    payload["data"]["ce"]["close"] = [103.0]
    with pytest.raises(ValueError, match="array lengths"):
        normalize_dhan_rolling_response(payload, expression="ATM", option_type="CALL")


def test_dhan_fetch_uses_official_rolling_endpoint_and_near_weekly_expiry():
    captured = {}

    def transport(url, *, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return _dhan_payload()

    frame = fetch_dhan_expired_options(
        access_token="secret",
        from_date="2024-01-01",
        to_date="2024-01-31",
        option_type="CALL",
        expression="ATM",
        transport=transport,
    )

    assert not frame.empty
    assert captured["url"].endswith("/v2/charts/rollingoption")
    assert captured["headers"]["access-token"] == "secret"
    assert captured["json"]["securityId"] == 13
    assert captured["json"]["exchangeSegment"] == "NSE_FNO"
    assert captured["json"]["instrument"] == "OPTIDX"
    assert captured["json"]["expiryFlag"] == "WEEK"
    assert captured["json"]["expiryCode"] == 0
    assert captured["json"]["strike"] == "ATM"
    assert captured["json"]["drvOptionType"] == "CALL"
    assert captured["json"]["interval"] == "1"


def test_dhan_fetch_rejects_more_than_30_calendar_days():
    with pytest.raises(ValueError, match="30 days"):
        fetch_dhan_expired_options(
            access_token="secret",
            from_date="2024-01-01",
            to_date="2024-02-15",
            option_type="PUT",
            expression="ATM+1",
            transport=lambda *args, **kwargs: _dhan_payload(),
        )


def test_executable_archive_validator_accepts_real_bid_ask_schema():
    frame = pd.DataFrame(
        [
            {
                "snapshot_ts": "2025-01-02 10:00:00+05:30",
                "tradingsymbol": "NIFTY25JAN25000CE",
                "instrument_token": 1,
                "expiry": "2025-01-02",
                "strike": 25000.0,
                "type": "CE",
                "lot_size": 75,
                "best_bid": 99.0,
                "best_ask": 100.0,
                "bid_qty": 150,
                "ask_qty": 225,
                "spot": 25010.0,
            }
        ]
    )
    out = validate_executable_quote_archive(frame)
    assert len(out) == 1
    assert bool(out.iloc[0]["executable_quote"]) is True
    assert out.iloc[0]["data_quality"] == "EXECUTABLE_BID_ASK"


def test_executable_archive_validator_rejects_crossed_or_missing_quotes():
    crossed = pd.DataFrame(
        [
            {
                "snapshot_ts": "2025-01-02 10:00:00+05:30",
                "tradingsymbol": "NIFTY25JAN25000CE",
                "instrument_token": 1,
                "expiry": "2025-01-02",
                "strike": 25000.0,
                "type": "CE",
                "lot_size": 75,
                "best_bid": 101.0,
                "best_ask": 100.0,
                "bid_qty": 150,
                "ask_qty": 225,
                "spot": 25010.0,
            }
        ]
    )
    with pytest.raises(ValueError, match="crossed"):
        validate_executable_quote_archive(crossed)

    with pytest.raises(ValueError, match="missing columns"):
        validate_executable_quote_archive(crossed.drop(columns=["best_ask"]))


def _rolling_frames_for_signal():
    entry_ts = pd.Timestamp("2025-01-02 10:51:00+05:30")
    exit_ts = pd.Timestamp("2025-01-02 12:51:00+05:30")
    rows = []
    # Entry ATM is 25000. At exit, spot moved enough that the SAME 25000 strike
    # is now represented by ATM-2. This pins the exact-contract requirement.
    rows.extend(
        [
            {
                "timestamp": entry_ts,
                "open": 100.0,
                "high": 105.0,
                "low": 98.0,
                "close": 103.0,
                "strike": 25000.0,
                "spot": 25010.0,
                "expression": "ATM",
                "option_type": "CALL",
                "data_quality": "PROXY_OHLC",
                "executable_quote": False,
            },
            {
                "timestamp": exit_ts,
                "open": 160.0,
                "high": 165.0,
                "low": 158.0,
                "close": 162.0,
                "strike": 25100.0,
                "spot": 25110.0,
                "expression": "ATM",
                "option_type": "CALL",
                "data_quality": "PROXY_OHLC",
                "executable_quote": False,
            },
            {
                "timestamp": exit_ts,
                "open": 205.0,
                "high": 210.0,
                "low": 200.0,
                "close": 208.0,
                "strike": 25000.0,
                "spot": 25110.0,
                "expression": "ATM-2",
                "option_type": "CALL",
                "data_quality": "PROXY_OHLC",
                "executable_quote": False,
            },
        ]
    )
    return pd.DataFrame(rows)


def test_dhan_proxy_expressions_cover_same_contract_migration():
    expressions = dhan_proxy_expressions()
    assert expressions[0] == "ATM-10"
    assert "ATM" in expressions
    assert expressions[-1] == "ATM+10"
    assert len(expressions) == 21


def test_dhan_proxy_mapper_tracks_same_absolute_strike_across_rolling_bucket():
    signal = {
        "session": "2025-01-02",
        "direction": "Bullish",
        "signal_time": pd.Timestamp("2025-01-02 10:51:00+05:30"),
    }
    out = map_signal_to_dhan_proxy_pnl(
        signal,
        _rolling_frames_for_signal(),
        moneyness="ATM",
    )
    assert out["status"] == "OK_PROXY"
    assert out["entry_expression"] == "ATM"
    assert out["exit_expression"] == "ATM-2"
    assert out["strike"] == 25000.0
    assert out["entry_proxy_price"] == 100.0
    assert out["exit_proxy_price"] == 205.0
    assert out["premium_points"] == 105.0
    assert out["executable"] is False
    assert out["data_quality"] == "PROXY_OHLC_NO_BID_ASK"


def test_dhan_proxy_mapper_uses_one_strike_itm_for_calls():
    frame = _rolling_frames_for_signal()
    extra = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2025-01-02 10:51:00+05:30"),
                "open": 135.0,
                "high": 140.0,
                "low": 132.0,
                "close": 138.0,
                "strike": 24950.0,
                "spot": 25010.0,
                "expression": "ATM-1",
                "option_type": "CALL",
                "data_quality": "PROXY_OHLC",
                "executable_quote": False,
            },
            {
                "timestamp": pd.Timestamp("2025-01-02 12:51:00+05:30"),
                "open": 245.0,
                "high": 250.0,
                "low": 240.0,
                "close": 248.0,
                "strike": 24950.0,
                "spot": 25110.0,
                "expression": "ATM-3",
                "option_type": "CALL",
                "data_quality": "PROXY_OHLC",
                "executable_quote": False,
            },
        ]
    )
    signal = {
        "session": "2025-01-02",
        "direction": "Bullish",
        "signal_time": pd.Timestamp("2025-01-02 10:51:00+05:30"),
    }
    out = map_signal_to_dhan_proxy_pnl(
        signal,
        pd.concat([frame, extra], ignore_index=True),
        moneyness="ITM1",
    )
    assert out["status"] == "OK_PROXY"
    assert out["strike"] == 24950.0
    assert out["entry_expression"] == "ATM-1"
    assert out["exit_expression"] == "ATM-3"


def test_dhan_proxy_mapper_fails_closed_if_same_contract_not_found_at_exit():
    signal = {
        "session": "2025-01-02",
        "direction": "Bullish",
        "signal_time": pd.Timestamp("2025-01-02 10:51:00+05:30"),
    }
    frame = _rolling_frames_for_signal()
    frame = frame[frame["expression"] != "ATM-2"].copy()
    out = map_signal_to_dhan_proxy_pnl(signal, frame, moneyness="ATM")
    assert out["status"] == "SAME_STRIKE_NOT_FOUND_AT_EXIT"


def test_build_dhan_proxy_ledger_fetches_full_grid_and_scores_atm_itm():
    calls = []

    def fake_fetcher(**kwargs):
        calls.append((kwargs["expression"], kwargs["option_type"]))
        expr = kwargs["expression"]
        side = kwargs["option_type"]
        if side != "CALL":
            return pd.DataFrame()
        rows = {
            "ATM": [
                ("2025-01-02 10:51:00+05:30", 100.0, 25000.0, 25010.0),
                ("2025-01-02 12:51:00+05:30", 160.0, 25100.0, 25110.0),
            ],
            "ATM-1": [
                ("2025-01-02 10:51:00+05:30", 135.0, 24950.0, 25010.0),
            ],
            "ATM-2": [
                ("2025-01-02 12:51:00+05:30", 205.0, 25000.0, 25110.0),
            ],
            "ATM-3": [
                ("2025-01-02 12:51:00+05:30", 245.0, 24950.0, 25110.0),
            ],
        }.get(expr, [])
        return pd.DataFrame(
            [
                {
                    "timestamp": pd.Timestamp(ts),
                    "open": px,
                    "high": px,
                    "low": px,
                    "close": px,
                    "strike": strike,
                    "spot": spot,
                    "expression": expr,
                    "option_type": side,
                    "data_quality": "PROXY_OHLC",
                    "executable_quote": False,
                }
                for ts, px, strike, spot in rows
            ]
        )

    signals = pd.DataFrame(
        [
            {
                "session": "2025-01-02",
                "direction": "Bullish",
                "signal_time": pd.Timestamp("2025-01-02 10:51:00+05:30"),
            }
        ]
    )
    ledger = build_dhan_proxy_ledger(
        signals,
        access_token="secret",
        fetcher=fake_fetcher,
        sleep_fn=lambda *_: None,
        throttle_seconds=0,
    )

    assert len(calls) == 21
    assert set(ledger["moneyness"]) == {"ATM", "ITM1"}
    assert set(ledger["status"]) == {"OK_PROXY"}
    assert ledger["can_satisfy_stage3_executable_gate"].eq(False).all()
