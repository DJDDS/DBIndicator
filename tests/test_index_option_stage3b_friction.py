import pandas as pd
import pytest

import app.index_option_stage3b_friction as friction


def _paper_ledger():
    return pd.DataFrame(
        [
            {
                "status": "OK",
                "session": "2026-09-22",
                "direction": "Bullish",
                "signal_time": "2026-09-22T11:00:00+05:30",
                "exit_target": "2026-09-22T13:00:00+05:30",
                "moneyness": "ITM1",
                "tradingsymbol": "NIFTY26SEP25000CE",
                "instrument_token": 12345,
                "expiry": "2026-09-24",
                "expiry_day": False,
                "strike": 25000.0,
                "lot_size": 65,
                "entry_snapshot_ts": "2026-09-22T11:00:05+05:30",
                "exit_snapshot_ts": "2026-09-22T13:00:05+05:30",
                "entry_ask": 102.0,
                "exit_bid": 110.0,
                "gross_pnl": 520.0,
                "charges": 48.0,
                "net_pnl": 472.0,
                "fee_model_version": "INDEX_NSE_EQ_OPT_2026_04_V1",
            }
        ]
    )


def _quotes():
    return pd.DataFrame(
        [
            {
                "snapshot_ts": pd.Timestamp("2026-09-22T11:00:05+05:30"),
                "instrument_token": 12345,
                "tradingsymbol": "NIFTY26SEP25000CE",
                "best_bid": 100.0,
                "best_ask": 102.0,
                "bid_qty": 130,
                "ask_qty": 130,
                "quote_age_seconds": 1.0,
            },
            {
                "snapshot_ts": pd.Timestamp("2026-09-22T13:00:05+05:30"),
                "instrument_token": 12345,
                "tradingsymbol": "NIFTY26SEP25000CE",
                "best_bid": 110.0,
                "best_ask": 112.0,
                "bid_qty": 130,
                "ask_qty": 130,
                "quote_age_seconds": 2.0,
            },
        ]
    )


def _stub_forward(monkeypatch):
    monkeypatch.setattr(
        friction,
        "build_forward_paper_ledger",
        lambda *args, **kwargs: (pd.DataFrame([{"session": "2026-09-22"}]), _paper_ledger()),
    )


def test_live_top_book_friction_uses_half_spreads_plus_charges(monkeypatch):
    _stub_forward(monkeypatch)
    out = friction.build_live_itm1_friction_ledger(_quotes(), pd.DataFrame())
    assert len(out) == 1
    row = out.iloc[0]
    assert row["friction_status"] == "OK"
    assert row["evidence_class"] == friction.TOP_BOOK_CLASS
    assert bool(row["decision_eligible"]) is True
    assert bool(row["actual_slippage_measured"]) is False
    assert row["entry_half_spread_cost_points"] == 1.0
    assert row["exit_half_spread_cost_points"] == 1.0
    assert row["round_trip_friction_points"] == pytest.approx(
        2.0 + row["charge_points"]
    )


def test_actual_fill_upgrades_friction_evidence(monkeypatch):
    _stub_forward(monkeypatch)
    fills = pd.DataFrame(
        [
            {
                "session": "2026-09-22",
                "tradingsymbol": "NIFTY26SEP25000CE",
                "entry_fill_price": 102.5,
                "exit_fill_price": 109.5,
            }
        ]
    )
    out = friction.build_live_itm1_friction_ledger(
        _quotes(),
        pd.DataFrame(),
        actual_fills=fills,
    )
    row = out.iloc[0]
    assert row["evidence_class"] == friction.ACTUAL_FILL_CLASS
    assert bool(row["actual_slippage_measured"]) is True
    assert row["entry_slippage_points"] == pytest.approx(0.5)
    assert row["exit_slippage_points"] == pytest.approx(0.5)
    expected = (102.5 - 101.0) + (111.0 - 109.5) + row["actual_fill_charge_points"]
    assert row["round_trip_friction_points"] == pytest.approx(expected)


def test_crossed_book_is_not_decision_eligible(monkeypatch):
    _stub_forward(monkeypatch)
    quotes = _quotes()
    quotes.loc[0, "best_bid"] = 103.0
    out = friction.build_live_itm1_friction_ledger(quotes, pd.DataFrame())
    row = out.iloc[0]
    assert row["friction_status"] == "NOT_MEASURABLE"
    assert row["friction_reason"] == "CROSSED_BOOK"
    assert bool(row["decision_eligible"]) is False


def test_summary_requires_twenty_eligible_rows():
    base = {
        "friction_status": "OK",
        "decision_eligible": True,
        "evidence_class": friction.TOP_BOOK_CLASS,
        "round_trip_friction_points": 1.25,
    }
    waiting = friction.summarize_live_friction(pd.DataFrame([base] * 19))
    ready = friction.summarize_live_friction(pd.DataFrame([base] * 20))
    assert waiting["status"] == "WAITING_FRICTION"
    assert waiting["eligible_count"] == 19
    assert ready["status"] == "READY"
    assert ready["eligible_count"] == 20
    assert ready["mean_round_trip_friction_points"] == 1.25


def test_duplicate_actual_fill_rows_fail_closed(monkeypatch):
    _stub_forward(monkeypatch)
    fills = pd.DataFrame(
        [
            {
                "session": "2026-09-22",
                "tradingsymbol": "NIFTY26SEP25000CE",
                "entry_fill_price": 102.5,
                "exit_fill_price": 109.5,
            },
            {
                "session": "2026-09-22",
                "tradingsymbol": "NIFTY26SEP25000CE",
                "entry_fill_price": 102.6,
                "exit_fill_price": 109.4,
            },
        ]
    )
    with pytest.raises(ValueError, match="multiple actual-fill rows"):
        friction.build_live_itm1_friction_ledger(
            _quotes(),
            pd.DataFrame(),
            actual_fills=fills,
        )
