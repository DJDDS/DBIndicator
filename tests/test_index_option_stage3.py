import datetime as dt

import pandas as pd

from app.index_option_stage3 import (
    FROZEN_SPEC,
    FROZEN_SPEC_SHA256,
    build_spot_1m_from_refs,
    evaluate_frozen_spec,
    gate_points_check,
    map_signal_to_option_pnl,
    session_bootstrap_mean_ci,
    stage3_gate_report,
)


def _frozen_signal_session(day="2026-09-22"):
    idx = pd.date_range(f"{day} 09:15", periods=375, freq="min", tz="Asia/Kolkata")
    frame = pd.DataFrame(
        {
            "open": 25000.0,
            "high": 25020.0,
            "low": 24980.0,
            "close": 25000.0,
        },
        index=idx,
    )
    # Make the 90-minute opening range exactly 100 points = 0.40% of session open.
    frame.loc[pd.Timestamp(f"{day} 09:20", tz="Asia/Kolkata"), "high"] = 25050.0
    frame.loc[pd.Timestamp(f"{day} 09:30", tz="Asia/Kolkata"), "low"] = 24950.0

    # 3-minute bar 10:45-10:48 closes beyond 25050 + 10 point buffer.
    for minute in ("10:45", "10:46", "10:47"):
        ts = pd.Timestamp(f"{day} {minute}", tz="Asia/Kolkata")
        frame.loc[ts, ["open", "high", "low", "close"]] = [25055, 25075, 25050, 25070]
    # Second consecutive completed 3-minute close confirms at 10:51.
    for minute in ("10:48", "10:49", "10:50"):
        ts = pd.Timestamp(f"{day} {minute}", tz="Asia/Kolkata")
        frame.loc[ts, ["open", "high", "low", "close"]] = [25068, 25085, 25060, 25080]
    frame.loc[frame.index >= pd.Timestamp(f"{day} 10:51", tz="Asia/Kolkata"), "close"] = 25100.0
    frame.loc[frame.index >= pd.Timestamp(f"{day} 10:51", tz="Asia/Kolkata"), "high"] = 25110.0
    frame.loc[frame.index >= pd.Timestamp(f"{day} 10:51", tz="Asia/Kolkata"), "low"] = 25085.0
    return frame


def test_frozen_stage3_spec_is_exact_audited_candidate():
    assert FROZEN_SPEC.opening_range_minutes == 90
    assert FROZEN_SPEC.trigger_minutes == 3
    assert FROZEN_SPEC.confirmation == "double_close"
    assert FROZEN_SPEC.range_pct_low == 0.0015
    assert FROZEN_SPEC.range_pct_high == 0.0060
    assert FROZEN_SPEC.hold_minutes == 120
    assert FROZEN_SPEC.stop_rule == "NONE_TIME_EXIT_ONLY"
    assert len(FROZEN_SPEC_SHA256) == 64


def test_frozen_evaluator_emits_points_and_no_grid():
    ledger = evaluate_frozen_spec(_frozen_signal_session())
    assert len(ledger) == 1
    row = ledger.iloc[0]
    assert row["opening_range_minutes"] == 90
    assert row["trigger_minutes"] == 3
    assert row["confirmation"] == "double_close"
    assert row["signal_time"] == pd.Timestamp("2026-09-22 10:51", tz="Asia/Kolkata")
    assert row["return_120m_points"] > 0
    assert row["frozen_spec_sha256"] == FROZEN_SPEC_SHA256


def test_gate_is_checked_in_points_not_only_r():
    primary = pd.DataFrame(
        [
            {
                "opening_range_minutes": 90,
                "trigger_minutes": 3,
                "confirmation": "double_close",
                "range_pct": 0.004,
                "range_width": 100.0,
                "signal_close": 25000.0,
                "return_120m_r": 0.10,
            },
            {
                "opening_range_minutes": 90,
                "trigger_minutes": 3,
                "confirmation": "double_close",
                "range_pct": 0.008,
                "range_width": 100.0,
                "signal_close": 25000.0,
                "return_120m_r": -0.05,
            },
        ]
    )
    out = gate_points_check(primary)
    assert out["status"] == "PASS"
    assert out["inside_mean_points"] == 10.0
    assert out["removed_mean_points"] == -5.0


def _quote_rows(day="2026-09-22"):
    entry_ts = pd.Timestamp(f"{day} 10:51:00", tz="Asia/Kolkata")
    exit_ts = pd.Timestamp(f"{day} 12:51:00", tz="Asia/Kolkata")
    rows = []
    contracts = [
        ("NIFTY24950CE", 101, 24950.0, "CE", 130.0, 132.0, 165.0, 167.0),
        ("NIFTY25000CE", 102, 25000.0, "CE", 105.0, 107.0, 139.0, 141.0),
        ("NIFTY25050CE", 103, 25050.0, "CE", 82.0, 84.0, 110.0, 112.0),
        ("NIFTY24950PE", 201, 24950.0, "PE", 70.0, 72.0, 50.0, 52.0),
        ("NIFTY25000PE", 202, 25000.0, "PE", 90.0, 92.0, 65.0, 67.0),
        ("NIFTY25050PE", 203, 25050.0, "PE", 115.0, 117.0, 86.0, 88.0),
    ]
    for symbol, token, strike, typ, ebid, eask, xbid, xask in contracts:
        common = {
            "tradingsymbol": symbol,
            "instrument_token": token,
            "strike": strike,
            "type": typ,
            "lot_size": 75,
            "expiry": day,
            "spot": 25010.0,
            "quote_age_seconds": 1.0,
            "bid_qty": 300,
            "ask_qty": 300,
        }
        rows.append({**common, "snapshot_ts": entry_ts, "best_bid": ebid, "best_ask": eask})
        rows.append({**common, "snapshot_ts": exit_ts, "best_bid": xbid, "best_ask": xask, "spot": 25080.0})
    quotes = pd.DataFrame(rows)
    refs = pd.DataFrame(
        [
            {"snapshot_ts": entry_ts, "spot": 25010.0, "nifty_future": 25025.0, "india_vix": 13.0},
            {"snapshot_ts": exit_ts, "spot": 25080.0, "nifty_future": 25100.0, "india_vix": 12.8},
        ]
    )
    return quotes, refs


def test_option_mapping_uses_ask_entry_bid_exit_and_real_charges():
    quotes, refs = _quote_rows()
    signal = {
        "session": "2026-09-22",
        "direction": "Bullish",
        "signal_time": pd.Timestamp("2026-09-22 10:51", tz="Asia/Kolkata"),
    }
    out = map_signal_to_option_pnl(signal, quotes, refs, moneyness="ATM")
    assert out["status"] == "OK"
    assert out["strike"] == 25000.0
    assert out["entry_ask"] == 107.0
    assert out["exit_bid"] == 139.0
    assert out["gross_pnl"] == (139.0 - 107.0) * 75
    assert out["charges"] > 0
    assert out["net_pnl"] < out["gross_pnl"]
    assert out["futures_directional_points"] == 75.0

    itm = map_signal_to_option_pnl(signal, quotes, refs, moneyness="ITM1")
    assert itm["status"] == "OK"
    assert itm["strike"] == 24950.0


def test_spot_reconstruction_preserves_observed_minute_ohlc():
    refs = pd.DataFrame(
        [
            {"snapshot_ts": pd.Timestamp("2026-09-22 09:15:05", tz="Asia/Kolkata"), "spot": 25000},
            {"snapshot_ts": pd.Timestamp("2026-09-22 09:15:25", tz="Asia/Kolkata"), "spot": 25010},
            {"snapshot_ts": pd.Timestamp("2026-09-22 09:15:55", tz="Asia/Kolkata"), "spot": 24995},
        ]
    )
    bars = build_spot_1m_from_refs(refs)
    row = bars.iloc[0]
    assert row["open"] == 25000
    assert row["high"] == 25010
    assert row["low"] == 24995
    assert row["close"] == 24995


def test_session_bootstrap_is_deterministic():
    frame = pd.DataFrame(
        {
            "session": ["a", "b", "c", "d"],
            "net_pnl": [10.0, -5.0, 20.0, 0.0],
        }
    )
    a = session_bootstrap_mean_ci(frame, "net_pnl", samples=200, seed=7)
    b = session_bootstrap_mean_ci(frame, "net_pnl", samples=200, seed=7)
    assert a == b
    assert a["n_sessions"] == 4


def _positive_option_ledger(n_per_expression=40):
    rows = []
    for moneyness in ("ATM", "ITM1"):
        for i in range(n_per_expression):
            rows.append(
                {
                    "status": "OK",
                    "session": f"2026-10-{(i % 20) + 1:02d}",
                    "signal_time": f"2026-10-{(i % 20) + 1:02d}T10:00:00+05:30",
                    "moneyness": moneyness,
                    "net_pnl": 10.0,
                    "expiry_day": bool(i % 2 == 0),
                }
            )
    return pd.DataFrame(rows)


def test_forward_data_cannot_substitute_for_missing_historical_option_gate():
    forward = _positive_option_ledger()
    cross = pd.DataFrame([{"instrument": "BANK NIFTY", "mean_120m_points": 10.0}])
    report = stage3_gate_report(
        historical_option_ledger=None,
        forward_option_ledger=forward,
        gate_points={"status": "PASS"},
        cross_index=cross,
        drawdown_budget=1000.0,
    )
    assert report["historical_option_pnl_validation_holdout"]["status"] == "WAITING_DATA"
    assert report["forward_paper"]["status"] == "PASS"
    assert report["all_stage3_gates_pass"] is False
    assert report["production_ready"] is False


def test_even_all_stage3_gates_only_enable_separate_review_not_production():
    historical = _positive_option_ledger(n_per_expression=4)
    forward = _positive_option_ledger()
    cross = pd.DataFrame([{"instrument": "SENSEX", "mean_120m_points": 5.0}])
    report = stage3_gate_report(
        historical_option_ledger=historical,
        forward_option_ledger=forward,
        gate_points={"status": "PASS"},
        cross_index=cross,
        drawdown_budget=1000.0,
    )
    assert report["historical_option_pnl_validation_holdout"]["status"] == "PASS"
    assert report["forward_paper"]["status"] == "PASS"
    assert report["cross_index_replication"]["status"] == "PASS"
    assert report["all_stage3_gates_pass"] is True
    assert report["eligible_for_separate_production_review"] is True
    assert report["production_ready"] is False
