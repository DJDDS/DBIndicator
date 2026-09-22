import pandas as pd
import pytest

import app.index_option_stage3b_banknifty as bank


def test_normalize_dhan_intraday_to_ist_one_minute():
    # 2024-01-02 03:45 UTC == 09:15 IST
    payload = {
        "timestamp": [1704167100, 1704167160],
        "open": [48000.0, 48010.0],
        "high": [48020.0, 48030.0],
        "low": [47990.0, 48000.0],
        "close": [48010.0, 48020.0],
        "volume": [0, 0],
    }
    out = bank.normalize_dhan_intraday(payload)
    assert len(out) == 2
    assert str(out.index.tz) == "Asia/Kolkata"
    assert out.index[0].hour == 9
    assert out.index[0].minute == 15


def test_fetch_banknifty_intraday_request_is_exact_and_no_secret_leak():
    captured = {}

    def transport(url, *, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return {
            "timestamp": [],
            "open": [],
            "high": [],
            "low": [],
            "close": [],
            "volume": [],
        }

    out = bank.fetch_banknifty_intraday_chunk(
        access_token="secret",
        from_ts="2024-01-01 09:15:00",
        to_ts="2024-03-01 15:30:00",
        transport=transport,
    )
    assert out.empty
    assert captured["url"].endswith("/v2/charts/intraday")
    assert captured["json"]["securityId"] == "25"
    assert captured["json"]["exchangeSegment"] == "IDX_I"
    assert captured["json"]["instrument"] == "INDEX"
    assert captured["json"]["interval"] == "1"
    assert captured["headers"]["access-token"] == "secret"


def test_fetch_banknifty_intraday_rejects_more_than_90_days():
    with pytest.raises(ValueError, match="at most 90 days"):
        bank.fetch_banknifty_intraday_chunk(
            access_token="secret",
            from_ts="2024-01-01 09:15:00",
            to_ts="2024-04-15 15:30:00",
            transport=lambda *a, **k: {},
        )


def test_banknifty_replication_uses_frozen_spec_and_splits_regime(monkeypatch):
    fake = pd.DataFrame(
        [
            {
                "session": "2024-11-13",
                "return_120m_points": 20.0,
                "direction": "Bullish",
            },
            {
                "session": "2024-11-14",
                "return_120m_points": -5.0,
                "direction": "Bearish",
            },
            {
                "session": "2025-01-10",
                "return_120m_points": 10.0,
                "direction": "Bullish",
            },
        ]
    )

    def evaluate(bars, *, instrument):
        assert instrument == "BANK NIFTY"
        return fake.copy()

    monkeypatch.setattr(bank, "evaluate_frozen_spec", evaluate)
    bars = pd.DataFrame(
        {"open": [1], "high": [1], "low": [1], "close": [1]},
        index=pd.DatetimeIndex(["2024-01-01 09:15:00+05:30"]),
    )
    ledger, report = bank.run_banknifty_replication(bars)
    assert len(ledger) == 3
    assert report["retuned"] is False
    assert report["weekly_contract_regime_through"] == "2024-11-13"
    assert report["monthly_only_regime_from"] == "2024-11-14"
    assert report["weekly_regime"]["trade_count"] == 1
    assert report["monthly_only_regime"]["trade_count"] == 2
    assert report["mean_120m_points"] == pytest.approx((20 - 5 + 10) / 3)
    assert report["positive_gross"] is True


def test_chunk_bounds_cover_window_without_more_than_90_days():
    bounds = list(bank._chunk_bounds(pd.Timestamp("2021-09-22"), pd.Timestamp("2022-04-01")))
    assert bounds[0][0].date().isoformat() == "2021-09-22"
    assert bounds[-1][1].date().isoformat() == "2022-04-01"
    for start, end in bounds:
        assert end - start < pd.Timedelta(days=90)
