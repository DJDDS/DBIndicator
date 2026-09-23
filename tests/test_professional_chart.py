import datetime as dt
from pathlib import Path

import pandas as pd

from app import web


def test_professional_chart_is_decoupled_from_legacy_indicator_presets():
    html = Path("app/templates/chart.html").read_text()
    source = Path("app/web.py").read_text()

    assert "MACD params:" not in html
    assert "Price · candlesticks · 9 EMA" not in html
    assert "Set AVWAP anchor" in html
    assert "Fullscreen" in html
    assert "EMA 20" in html and "EMA 200" in html
    assert "RSI" in html and "MACD" in html and "Volume" in html

    assert '"3minute"' in source
    assert '"5minute"' in source
    assert '"30minute"' in source
    assert '"75minute"' in source
    assert '"week"' in source
    assert 'timeframe=config.WATCHLIST_TIMEFRAME' not in source[source.index('def chart_page'):source.index('def api_insights')]


def test_chart_candles_expose_volume():
    idx = pd.date_range("2026-09-23 09:15", periods=2, freq="15min", tz="Asia/Kolkata")
    df = pd.DataFrame(
        {
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "volume": [1000, 1200],
        },
        index=idx,
    )
    rows = web._candles(df)
    assert rows[0]["volume"] == 1000
    assert rows[1]["close"] == 102.0


def test_75minute_nse_chart_uses_five_equal_session_bars(monkeypatch):
    idx = pd.date_range("2026-09-23 09:15", periods=25, freq="15min", tz="Asia/Kolkata")
    rows = []
    for i, ts in enumerate(idx):
        rows.append(
            {
                "date": ts,
                "open": 100 + i,
                "high": 101 + i,
                "low": 99 + i,
                "close": 100.5 + i,
                "volume": 1000 + i,
            }
        )

    monkeypatch.setattr(web.scanner, "_fetch_historical_chunked", lambda *a, **k: rows)
    monkeypatch.setattr(
        web.scanner,
        "now_ist",
        lambda: dt.datetime(2026, 9, 23, 15, 30, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30))),
    )

    out = web._chart_fetch_candles(object(), 123, "75minute")
    assert len(out) == 5
    assert list(out["volume"]) == [
        sum(1000 + i for i in range(0, 5)),
        sum(1000 + i for i in range(5, 10)),
        sum(1000 + i for i in range(10, 15)),
        sum(1000 + i for i in range(15, 20)),
        sum(1000 + i for i in range(20, 25)),
    ]
