import datetime as dt

from app import v123_focus, v123_state
from app.v123_market_stream import UniverseMomentumStreamService


def _focus_state(now):
    return {
        "version": 1,
        "trade_date": now.date().isoformat(),
        "focus": {
            "ABC": {
                "symbol": "ABC",
                "direction": "Bullish",
                "lifecycle": "READY",
                "selected_at": now.isoformat(timespec="seconds"),
                "last_state_change_at": now.isoformat(timespec="seconds"),
                "event_family": "RANGE_EXPANSION",
                "trigger": 101.2,
                "invalidation": 99.8,
                "live_price": 100.9,
                "vehicles": {"cash": "ELIGIBLE", "future": "WAIT", "option": "WAIT"},
                "history": [],
            }
        },
        "recent": [],
        "missed": {},
        "last_update": now.isoformat(timespec="seconds"),
    }


def test_focus_store_survives_restart(tmp_path):
    now = dt.datetime(2026, 9, 23, 11, 45)
    path = tmp_path / "v123_focus_state.json"
    store = v123_state.FocusStateStore(str(path))
    state = _focus_state(now)

    assert store.save_if_changed(state, now=now) is True
    # New object simulates a new process after Railway redeploy.
    restored = v123_state.FocusStateStore(str(path)).load(now=now + dt.timedelta(minutes=2))
    assert restored is not None
    assert restored["focus"]["ABC"]["lifecycle"] == "READY"
    assert restored["focus"]["ABC"]["trigger"] == 101.2


def test_focus_store_ignores_yesterdays_session(tmp_path):
    t0 = dt.datetime(2026, 9, 23, 15, 0)
    path = tmp_path / "v123_focus_state.json"
    v123_state.FocusStateStore(str(path)).save_if_changed(_focus_state(t0), now=t0)

    restored = v123_state.FocusStateStore(str(path)).load(
        now=dt.datetime(2026, 9, 24, 9, 16)
    )
    assert restored is None


def test_focus_store_does_not_rewrite_for_price_only_change(tmp_path):
    now = dt.datetime(2026, 9, 23, 12, 0)
    path = tmp_path / "v123_focus_state.json"
    store = v123_state.FocusStateStore(str(path))
    state = _focus_state(now)
    assert store.save_if_changed(state, now=now) is True

    state["focus"]["ABC"]["live_price"] = 101.05
    state["focus"]["ABC"]["focus_age_min"] = 12.0
    assert store.save_if_changed(state, now=now + dt.timedelta(seconds=2)) is False

    state["focus"]["ABC"]["lifecycle"] = "ACTIVE"
    state["focus"]["ABC"]["last_state_change_at"] = (now + dt.timedelta(seconds=3)).isoformat(timespec="seconds")
    assert store.save_if_changed(state, now=now + dt.timedelta(seconds=3)) is True


def test_observer_checkpoint_restores_short_horizon_memory(tmp_path):
    now = dt.datetime(2026, 9, 23, 11, 30)
    path = tmp_path / "v123_market_checkpoint.json"

    samples = {
        "ABC": [
            {"ts": now - dt.timedelta(minutes=10), "price": 100.0, "volume": 1000, "high": 100.2, "low": 99.8, "prev_close": 99.0},
            {"ts": now - dt.timedelta(minutes=5), "price": 100.5, "volume": 1200, "high": 100.6, "low": 99.8, "prev_close": 99.0},
            {"ts": now, "price": 101.0, "volume": 1500, "high": 101.0, "low": 99.8, "prev_close": 99.0},
        ],
        "NIFTY 50": [
            {"ts": now - dt.timedelta(minutes=5), "price": 25000, "volume": 1, "high": 25010, "low": 24990, "prev_close": 24980},
            {"ts": now, "price": 25020, "volume": 2, "high": 25025, "low": 24990, "prev_close": 24980},
        ],
    }
    latest = {
        "ABC": {"last_price": 101.0, "volume_traded": 1500, "ohlc": {"open": 99.2, "high": 101.0, "low": 99.0, "close": 99.0}},
        "NIFTY 50": {"last_price": 25020, "volume_traded": 2, "ohlc": {"open": 24990, "high": 25025, "low": 24980, "close": 24980}},
    }
    v123_state.save_observer_checkpoint(str(path), samples, latest, now=now)

    svc = UniverseMomentumStreamService(
        publish_callback=lambda payload: None,
        metadata_provider=lambda: [],
        access_token_getter=lambda: None,
        kite_client_getter=lambda: None,
        api_key="x",
        checkpoint_path=str(path),
        now_provider=lambda: now + dt.timedelta(seconds=20),
        sleep_fn=lambda _: None,
    )
    svc._restore_checkpoint(now + dt.timedelta(seconds=20))

    assert len(svc._samples["ABC"]) >= 3
    assert svc._return("ABC", now + dt.timedelta(seconds=20), 300) is not None
    assert "ABC" in svc._latest


def test_restart_keeps_focus_even_if_observer_is_temporarily_empty(tmp_path):
    now = dt.datetime(2026, 9, 23, 11, 45)
    path = tmp_path / "v123_focus_state.json"
    store = v123_state.FocusStateStore(str(path))
    state = _focus_state(now)
    store.save_if_changed(state, now=now)

    restored = v123_state.FocusStateStore(str(path)).load(now=now + dt.timedelta(minutes=1))
    updated = v123_focus.update_focus(
        restored,
        {"events": [], "leaders": [], "laggards": []},
        {"rows": []},
        {"candidates": []},
        [{"symbol": "ABC", "close": 100.95, "atr": 2.0}],
        now=now + dt.timedelta(minutes=1),
    )
    assert "ABC" in updated["focus"]
    assert updated["focus"]["ABC"]["lifecycle"] == "READY"
