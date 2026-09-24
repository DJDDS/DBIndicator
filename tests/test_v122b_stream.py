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



def test_same_closed_trigger_cannot_recycle_without_fresh_evidence():
    t0 = dt.datetime(2026, 9, 24, 12, 0)
    svc = _service(t0)
    life = {
        "last_closed_signature": {
            "direction": "Bullish",
            "setup": "OPENING_DRIVE",
            "trigger": 457.0,
            "event_family": "OPENING_DRIVE",
        },
        "last_closed_at": t0,
        "last_closed_price": 470.0,
        "last_closed_relative_5m": 0.18,
        "last_closed_ret_5m": 0.22,
    }
    setup = {"direction": "Bullish", "setup": "OPENING_DRIVE", "trigger": 457.0}
    candidate = {
        "direction": "Bullish", "focus_event_family": "OPENING_DRIVE",
        "atr": 10.0, "ret_5m_pct": 0.22, "relative_5m_vs_nifty_pct": 0.18,
    }
    allowed, reason = svc._fresh_episode_allowed(
        life, setup, candidate, 470.2, t0 + dt.timedelta(minutes=9)
    )
    assert allowed is False
    assert reason == "WAITING_FOR_FRESH_STRUCTURE_AFTER_PRIOR_EPISODE"


def test_fresh_entry_reuses_existing_v123_rearm_math():
    t0 = dt.datetime(2026, 9, 24, 12, 0)
    svc = _service(t0)
    base_life = {
        "last_closed_signature": {
            "direction": "Bullish", "setup": "MICRO_BREAKOUT",
            "trigger": 100.0, "event_family": "RANGE_EXPANSION",
        },
        "last_closed_at": t0,
        "last_closed_price": 100.0,
        "last_closed_relative_5m": 0.10,
        "last_closed_ret_5m": 0.10,
    }
    setup = {"direction": "Bullish", "setup": "MICRO_BREAKOUT", "trigger": 100.0}
    candidate = {
        "direction": "Bullish", "focus_event_family": "RANGE_EXPANSION",
        "atr": 5.0, "ret_5m_pct": 0.10, "relative_5m_vs_nifty_pct": 0.10,
    }

    allowed, reason = svc._fresh_episode_allowed(
        dict(base_life), setup, candidate, 101.0, t0 + dt.timedelta(minutes=4)
    )
    assert allowed is True
    assert reason == "RENEWED_MOVE_GE_0_20_ATR"

    shifted = dict(setup)
    shifted["trigger"] = 100.75  # 0.15 ATR
    allowed, reason = svc._fresh_episode_allowed(
        dict(base_life), shifted, candidate, 100.1, t0 + dt.timedelta(minutes=4)
    )
    assert allowed is True
    assert reason == "NEW_STRUCTURAL_TRIGGER_GE_0_15_ATR"

    rearmed = dict(candidate)
    rearmed["focus_rearmed_at"] = (t0 + dt.timedelta(minutes=1)).isoformat()
    rearmed["focus_rearm_reason"] = "fresh pullback-reclaim"
    allowed, reason = svc._fresh_episode_allowed(
        dict(base_life), setup, rearmed, 100.1, t0 + dt.timedelta(minutes=4)
    )
    assert allowed is True
    assert reason == "fresh pullback-reclaim"


def test_continuation_math_tracks_path_not_just_final_price():
    t0 = dt.datetime(2026, 9, 24, 12, 0)
    svc = _service(t0)
    setup = {
        "direction": "Bullish", "setup": "MICRO_BREAKOUT",
        "speed_class": None, "invalidation": 95.0, "expected_move_abs": 10.0,
    }
    state = {"state": "TRADEABLE", "tradeable": True}
    row = {"direction": "Bullish", "live_price": 100.0, "atr": 10.0}
    _, life = svc._manage_lifecycle("ABC", row, setup, state, t0)

    row1 = {"direction": "Bullish", "live_price": 101.0, "atr": 10.0}
    svc._manage_lifecycle("ABC", row1, setup, state, t0 + dt.timedelta(minutes=1))
    m1 = svc._continuation_math(life, row1, 101.0)
    assert m1["progress_atr"] == 0.1
    assert m1["mfe_atr"] == 0.1
    assert m1["mae_atr"] == 0.0
    assert m1["path_efficiency"] == 1.0
    assert m1["pullback_ratio"] == 0.0

    row2 = {"direction": "Bullish", "live_price": 100.5, "atr": 10.0}
    svc._manage_lifecycle("ABC", row2, setup, state, t0 + dt.timedelta(minutes=2))
    m2 = svc._continuation_math(life, row2, 100.5)
    assert m2["progress_atr"] == 0.05
    assert m2["mfe_atr"] == 0.1
    assert m2["mae_atr"] == 0.0
    assert m2["path_efficiency"] == 0.3333
    assert m2["pullback_ratio"] == 0.5


def test_shadow_recorder_writes_milestones_without_controlling_trade(tmp_path):
    now = dt.datetime(2026, 9, 24, 12, 0)
    event_file = tmp_path / "v122b_events.jsonl"
    svc = v122b_stream.TacticalStockStreamService(
        candidate_provider=lambda: [],
        publish_callback=lambda payload: None,
        access_token_getter=lambda: None,
        kite_client_getter=lambda: None,
        api_key="test",
        earnings_state_file=None,
        state_file=None,
        event_file=str(event_file),
        ticker_factory=lambda *args, **kwargs: None,
        now_provider=lambda: now,
        sleep_fn=lambda seconds: None,
        reactor_getter=lambda: None,
    )
    life = {
        "episode_no": 2,
        "episode_started_at": now,
        "triggered_at": now,
        "entry_underlying": 100.0,
        "best_favourable": 1.0,
        "worst_adverse": 0.2,
        "path_length_abs": 1.5,
        "last_path_price": 100.8,
        "shadow_recorded_milestones": [],
    }
    candidate = {
        "direction": "Bullish", "atr": 10.0,
        "focus_event_family": "RANGE_EXPANSION",
        "ret_5m_pct": 0.35, "relative_5m_vs_nifty_pct": 0.20,
    }
    setup = {
        "direction": "Bullish", "setup": "MICRO_BREAKOUT",
        "trigger": 100.0, "invalidation": 99.0,
    }
    metrics = svc._record_continuation_shadow(
        now=now + dt.timedelta(minutes=3), symbol="ABC",
        candidate=candidate, setup=setup,
        state={"state": "TRADEABLE"}, life=life, live_price=100.8,
        route_health={"state": "HEALTHY"}, route={"contract": {
            "symbol": "ABC26SEP100CE", "mid": 5.0, "spread_pct": 1.2,
            "friction_to_expected_move": 0.21, "delta_abs": 0.55,
        }},
        persistence={"support_fraction": 0.7, "oppose_fraction": 0.3},
        fast={"veto": False}, stale=False, rvol3=1.8, rvol3_accel=0.4,
        relative3=0.12, five_minute={"state": "SUPPORTIVE"},
        cash_age=1.0, fut_age=1.5,
    )
    assert metrics["progress_atr"] == 0.08
    shadow = tmp_path / "v123_continuation_shadow.jsonl"
    assert shadow.exists()
    text = shadow.read_text(encoding="utf-8")
    assert '"horizon_min":3' in text
    assert '"shadow_only":true' in text
    assert '"controls_trading"' not in text
