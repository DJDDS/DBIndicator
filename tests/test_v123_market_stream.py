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
    event = svc._event_for("FLAT", svc._samples["FLAT"][-1], {"prev_close": 100.0, "atr": 1.5}, {"5m": 0.0}, now)
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
    )
    assert event is None
    diag = svc._event_diagnostic(
        "MISS", svc._samples["MISS"][-1],
        {"symbol":"MISS","prev_close":100.0,"atr":2.0},
        nifty, now, event=event,
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
