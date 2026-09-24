import datetime as dt
from collections import deque

from app import v122b_stream, v122b_tactical


def _service(now):
    return v122b_stream.TacticalStockStreamService(
        candidate_provider=lambda: [],
        publish_callback=lambda payload: None,
        access_token_getter=lambda: None,
        kite_client_getter=lambda: None,
        api_key="test",
        earnings_state_file=None,
        state_file=None,
        event_file=None,
        ticker_factory=lambda *args, **kwargs: None,
        now_provider=lambda: now,
        sleep_fn=lambda seconds: None,
        reactor_getter=lambda: None,
    )


def test_trigger_clock_can_restart_for_fresh_entry_episode():
    t0 = dt.datetime(2026, 9, 24, 10, 0)
    svc = _service(t0)
    setup = {
        "direction": "Bullish",
        "setup": "MICRO_BREAKOUT",
        "speed_class": "IMPULSE",
        "invalidation": 99.0,
        "expected_move_abs": 2.0,
    }
    row = {"direction": "Bullish", "live_price": 100.0}
    state = {"state": "TRADEABLE", "tradeable": True}

    _, life = svc._manage_lifecycle("ABC", row, setup, state, t0)
    assert life["triggered_at"] == t0
    assert life["entry_underlying"] == 100.0

    svc._reset_trigger(life)
    assert "triggered_at" not in life
    assert "entry_underlying" not in life
    assert "best_favourable" not in life

    t1 = t0 + dt.timedelta(minutes=30)
    row2 = {"direction": "Bullish", "live_price": 103.0}
    _, life = svc._manage_lifecycle("ABC", row2, setup, state, t1)
    assert life["triggered_at"] == t1
    assert life["entry_underlying"] == 103.0


def test_new_trading_day_clears_deep_tactical_state():
    t0 = dt.datetime(2026, 9, 23, 15, 20)
    svc = _service(t0)
    svc._session_date = t0.date()
    svc._lifecycle["ABC"] = {"triggered_at": t0, "entry_underlying": 100.0}
    svc._bars["ABC"].append({"ts": "2026-09-23T15:18:00", "close": 100.0})
    svc._bar_builders["ABC"] = v122b_tactical.ThreeMinuteBarBuilder()
    svc._seeded.add("ABC")
    svc._depth_samples["ABC"].append({"ts": t0.isoformat(), "l5_imbalance": 0.2})
    svc._basis_samples["ABC"].append({"ts": t0, "basis_pct": 0.1})
    svc._universe_signature = (("ABC", "Bullish", 100.0, ""),)

    changed = svc._maybe_reset_session(dt.datetime(2026, 9, 24, 9, 10))
    assert changed is True
    assert svc._lifecycle == {}
    assert list(svc._bars.get("ABC") or []) == []
    assert "ABC" not in svc._seeded
    assert svc._universe_signature is None


class _HistoryKite:
    def historical_data(self, token, start, end, interval):
        return [
            {
                "date": dt.datetime(2026, 9, 24, 10, 0),
                "open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5,
                "volume": 1000,
            },
            {
                "date": dt.datetime(2026, 9, 24, 10, 3),
                "open": 100.5, "high": 102.0, "low": 100.4, "close": 101.5,
                "volume": 600,
            },
        ]


def test_seed_excludes_still_forming_current_three_minute_bucket():
    now = dt.datetime(2026, 9, 24, 10, 4, 20)
    svc = _service(now)
    svc._seed_three_minute(_HistoryKite(), "ABC", 1, now)
    bars = list(svc._bars["ABC"])
    assert len(bars) == 1
    assert bars[0]["ts"] == "2026-09-24T10:00:00"


def test_removed_symbol_is_reseedable_on_later_reentry():
    now = dt.datetime(2026, 9, 24, 11, 0)
    svc = _service(now)
    svc._seeded.add("ABC")
    svc._bars["ABC"].append({"ts": "2026-09-24T10:57:00", "close": 100.0})
    svc._bar_builders["ABC"] = v122b_tactical.ThreeMinuteBarBuilder(
        current={"bucket": dt.datetime(2026, 9, 24, 10, 57)}
    )
    svc._drop_symbol_market_state("ABC")
    assert "ABC" not in svc._seeded
    assert not list(svc._bars.get("ABC") or [])
    assert "ABC" not in svc._bar_builders
