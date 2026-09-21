import pandas as pd
import pytest

from app.index_option_historical_sources import (
    fetch_dhan_expired_options,
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
