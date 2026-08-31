import datetime as dt

import numpy as np
import pandas as pd
import pytest

from app import indicators, scanner
from app.oi_view import select_oi_screener_rows


def _aware_intraday_frame(days=29):
    rows = []
    start = pd.Timestamp('2026-08-03', tz='Asia/Kolkata')
    for d in range(days):
        day = start + pd.Timedelta(days=d)
        if day.weekday() >= 5:
            continue
        for i in range(25):
            ts = day.replace(hour=9, minute=15) + pd.Timedelta(minutes=15 * i)
            base = 100.0 + 0.03 * len(rows)
            rows.append((ts, base, base + 0.25, base - 0.2, base + 0.05, 1000 + (i * 15)))
    idx = pd.DatetimeIndex([r[0] for r in rows])
    return pd.DataFrame(
        {
            'open': [r[1] for r in rows],
            'high': [r[2] for r in rows],
            'low': [r[3] for r in rows],
            'close': [r[4] for r in rows],
            'volume': [r[5] for r in rows],
        },
        index=idx,
    )


def test_time_of_day_rvol_accepts_timezone_aware_kite_index_with_naive_ist_now():
    idx = pd.DatetimeIndex([
        pd.Timestamp('2026-08-27 10:00', tz='Asia/Kolkata'),
        pd.Timestamp('2026-08-28 10:00', tz='Asia/Kolkata'),
        pd.Timestamp('2026-08-31 10:00', tz='Asia/Kolkata'),
    ])
    df = pd.DataFrame({'volume': [1500, 1500, 500]}, index=idx)
    now = dt.datetime(2026, 8, 31, 10, 5)  # scanner.now_ist() semantics: naive IST wall clock

    rv = indicators.time_of_day_rvol(df, lookback_sessions=2, now=now, interval_minutes=15)

    assert rv.iloc[-1] == pytest.approx(1.0, rel=0.05)


def test_live_scan_with_timezone_aware_candles_returns_valid_row_and_attaches_oi(monkeypatch):
    frame = _aware_intraday_frame()
    monkeypatch.setattr(scanner, '_load_instrument_map', lambda _k: {'AAA': 1})
    monkeypatch.setattr(scanner, 'fetch_candles', lambda *_a, **_k: frame)
    monkeypatch.setattr(scanner, 'fetch_oi_map', lambda _k, _symbols: {
        'AAA': {
            'oi': 123456, 'oi_day_high': 130000, 'oi_day_low': 110000,
            'oi_near': 123456, 'oi_next': 80000, 'oi_far': 40000,
            'oi_total': 243456, 'contracts': [],
        }
    })
    monkeypatch.setattr(scanner, 'now_ist', lambda: dt.datetime(2026, 8, 31, 10, 6, 23))

    rows = scanner.scan_watchlist(object(), timeframe='15minute', with_oi=True, symbols=['AAA'])

    assert len(rows) == 1
    assert rows[0]['symbol'] == 'AAA'
    assert 'error' not in rows[0]
    assert rows[0]['oi'] == 123456
    assert rows[0]['oi_total'] == 243456
    oi_rows = select_oi_screener_rows(rows)
    assert [r['symbol'] for r in oi_rows] == ['AAA']
