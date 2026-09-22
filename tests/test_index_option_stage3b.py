import pandas as pd
import pytest

from app.index_option_stage3b import (
    EARLY_KILL_GROSS_FLOOR_POINTS,
    PRIMARY_EXPRESSION,
    alignment_check,
    apply_expiry_day_refinement,
    decision_report,
    paired_atm_itm1,
    split_summary,
    summarize_friction,
    validate_proxy_ledger,
)


def _row(session, moneyness, points, direction="Bullish"):
    return {
        "session": session,
        "moneyness": moneyness,
        "status": "OK_PROXY",
        "premium_points": points,
        "direction": direction,
        "data_quality": "PROXY_OHLC_NO_BID_ASK",
        "executable": False,
        "can_satisfy_stage3_executable_gate": False,
    }


def _ledger(val=4.0, hold=4.0):
    rows = [
        _row("2022-01-05", "ATM", 1.0),
        _row("2022-01-05", "ITM1", 2.0),
        _row("2024-01-05", "ATM", val - 1.0),
        _row("2024-01-05", "ITM1", val),
        _row("2025-06-05", "ATM", val - 2.0, "Bearish"),
        _row("2025-06-05", "ITM1", val, "Bearish"),
        _row("2026-02-05", "ATM", hold - 1.0),
        _row("2026-02-05", "ITM1", hold),
        _row("2026-07-05", "ATM", hold - 2.0, "Bearish"),
        _row("2026-07-05", "ITM1", hold, "Bearish"),
    ]
    return validate_proxy_ledger(pd.DataFrame(rows))


def _friction(value=2.0, n=20):
    return pd.DataFrame(
        {
            "moneyness": [PRIMARY_EXPRESSION] * n,
            "round_trip_friction_points": [value] * n,
        }
    )


def test_stage3b_proxy_validation_fails_closed_on_executable_upgrade():
    bad = pd.DataFrame([_row("2024-01-05", "ITM1", 3.0)])
    bad.loc[0, "can_satisfy_stage3_executable_gate"] = True
    with pytest.raises(ValueError, match="must never satisfy"):
        validate_proxy_ledger(bad)


def test_stage3b_split_dates_are_frozen():
    out = split_summary(_ledger())
    val = out[
        out["moneyness"].eq("ITM1")
        & out["split"].eq("VALIDATION_2024_2025")
    ].iloc[0]
    hold = out[
        out["moneyness"].eq("ITM1")
        & out["split"].eq("HOLDOUT_2026_TO_AUG31")
    ].iloc[0]
    assert val["trade_count"] == 2
    assert hold["trade_count"] == 2
    assert val["mean_gross_points"] == 4.0
    assert hold["mean_gross_points"] == 4.0


def test_stage3b_paired_itm1_minus_atm_is_session_paired():
    out = paired_atm_itm1(_ledger())
    assert out["paired_sessions"] == 5
    assert out["mean_itm1_minus_atm_points"] > 0
    assert "selection_warning" in out


def test_stage3b_friction_requires_20_primary_observations():
    waiting = summarize_friction(_friction(n=19))
    ready = summarize_friction(_friction(n=20))
    assert waiting["status"] == "WAITING_FRICTION"
    assert ready["status"] == "READY"
    assert ready["mean_friction_points"] == 2.0


def test_stage3b_early_kill_happens_before_friction_requirement():
    frame = _ledger(
        val=EARLY_KILL_GROSS_FLOOR_POINTS,
        hold=EARLY_KILL_GROSS_FLOOR_POINTS,
    )
    out = decision_report(frame, friction_log=None, banknifty_replication=None)
    assert out["decision"] == "KILL"
    assert "1.50-point" in out["reason"]


def test_stage3b_pilot_requires_net_above_one_positive_splits_and_bank():
    out = decision_report(
        _ledger(val=4.0, hold=4.0),
        friction_log=_friction(value=2.0),
        banknifty_replication={
            "status": "READY",
            "mean_120m_points": 3.0,
        },
    )
    assert out["decision"] == "PILOT"
    assert out["net_2024_to_2026_points"] == 2.0
    assert out["net_validation_points"] == 2.0
    assert out["net_holdout_points"] == 2.0


def test_stage3b_park_if_bank_replication_non_positive():
    out = decision_report(
        _ledger(val=4.0, hold=4.0),
        friction_log=_friction(value=2.0),
        banknifty_replication={
            "status": "READY",
            "mean_120m_points": -0.1,
        },
    )
    assert out["decision"] == "PARK"


def test_stage3b_kill_if_measured_net_is_non_positive():
    out = decision_report(
        _ledger(val=2.0, hold=2.0),
        friction_log=_friction(value=3.0),
        banknifty_replication={
            "status": "READY",
            "mean_120m_points": 2.0,
        },
    )
    assert out["decision"] == "KILL"
    assert out["net_2024_to_2026_points"] == -1.0


def test_stage3b_expiry_refinement_is_the_only_separate_round():
    frame = _ledger(val=4.0, hold=4.0)
    cal = pd.DataFrame(
        {
            "session": frame["session"].drop_duplicates().astype(str),
            "is_expiry_day": [False] * frame["session"].nunique(),
        }
    )
    out = apply_expiry_day_refinement(
        frame,
        cal,
        friction_log=_friction(value=2.0),
        banknifty_replication={
            "status": "READY",
            "mean_120m_points": 1.0,
        },
    )
    assert out["status"] == "COMPLETE"
    assert out["refinement"] == "EXCLUDE_EXPIRY_DAY"
    assert out["refinement_round"] == 1
    assert out["cannot_overwrite_base_decision"] is True



def test_stage3b_expiry_calendar_parses_string_false_safely():
    frame = _ledger(val=4.0, hold=4.0)
    sessions = frame["session"].drop_duplicates().astype(str).tolist()
    cal = pd.DataFrame(
        {
            "session": sessions,
            "is_expiry_day": ["false"] * len(sessions),
        }
    )
    out = apply_expiry_day_refinement(
        frame,
        cal,
        friction_log=_friction(value=2.0),
        banknifty_replication={
            "status": "READY",
            "mean_120m_points": 1.0,
        },
    )
    assert out["status"] == "COMPLETE"



def test_stage3b_alignment_uses_five_deterministic_sessions():
    sessions = pd.date_range("2024-01-01", periods=10, freq="D")
    dhan_rows = []
    kite_rows = []
    for day in sessions:
        for minute in (15, 16):
            ts = pd.Timestamp(day.date().isoformat() + f" 09:{minute}:00+05:30")
            px = 22000.0 + float(day.day)
            dhan_rows.append({"timestamp": ts, "spot": px})
            kite_rows.append({"timestamp": ts, "close": px})

    out = alignment_check(
        pd.DataFrame(dhan_rows),
        pd.DataFrame(kite_rows),
        sessions=5,
    )
    assert out["status"] == "COMPLETE"
    assert len(out["sessions"]) == 5
    assert out["matched_minutes"] == 10
    assert out["mean_abs_spot_difference_points"] == 0.0
    assert out["selection_method"] == "5 evenly spaced common sessions; no cherry-picking"
