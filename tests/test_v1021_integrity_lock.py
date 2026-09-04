import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app import research_feasibility as rf
from app import research_integrity as ri
from app import v10_directional_edge as v10


def _uneven_events():
    rows = []
    # Unequal day clusters intentionally create a difference between naive
    # and date-clustered inference while keeping the same event-weighted mean.
    for value in [0.012, 0.010, 0.008, 0.006, 0.004]:
        rows.append({"date": pd.Timestamp("2024-01-02"), "symbol": f"A{len(rows)}", "net": value})
    for value in [-0.010, -0.008]:
        rows.append({"date": pd.Timestamp("2024-01-03"), "symbol": f"B{len(rows)}", "net": value})
    rows.append({"date": pd.Timestamp("2024-01-04"), "symbol": "C", "net": 0.003})
    return pd.DataFrame(rows)


def test_event_weighted_cluster_t_uses_same_point_estimate_and_unequal_clusters():
    ev = _uneven_events()
    out = ri.clustered_mean_inference(ev["net"], ev["date"])

    vals = ev["net"].to_numpy(dtype=float)
    mean = float(vals.mean())
    naive_se = float(np.std(vals, ddof=1) / math.sqrt(len(vals)))
    centered = ev.assign(centered=ev["net"] - mean).groupby("date")["centered"].sum().to_numpy(dtype=float)
    g = ev["date"].nunique()
    cluster_se = math.sqrt((g / (g - 1.0)) * float(np.sum(centered ** 2)) / (len(vals) ** 2))
    counts = ev.groupby("date").size().to_numpy(dtype=float)
    m_eff = float(np.sum(counts ** 2) / np.sum(counts))
    de = (cluster_se / naive_se) ** 2
    expected_rho = (de - 1.0) / (m_eff - 1.0)

    assert math.isclose(out["mean"], mean)
    assert math.isclose(out["naive_se"], naive_se)
    assert math.isclose(out["cluster_se"], cluster_se)
    assert math.isclose(out["event_cluster_t"], mean / cluster_se)
    assert math.isclose(out["unequal_cluster_size"], m_eff)
    assert math.isclose(out["design_effect"], de)
    assert math.isclose(out["rho"], expected_rho)
    assert out["rho_status"] == "IDENTIFIED"
    assert out["effective_n"] <= len(vals)


def test_direction_report_exposes_matching_event_clustered_inference_without_changing_legacy_gate():
    ev = _uneven_events()
    report = v10._direction_report(ev, "net", bootstrap_reps=20)
    inf = ri.clustered_mean_inference(ev["net"], ev["date"])
    assert math.isclose(report["event_cluster_t"], inf["event_cluster_t"])
    assert math.isclose(report["event_cluster_se"], inf["cluster_se"])
    assert report["registered_estimand"] == "EVENT_WEIGHTED_NET_LEGACY_FROZEN"
    assert report["legacy_gate_inference"] == "DAY_WEIGHTED_T_LEGACY_FROZEN"
    assert report["future_primary_estimand"] == "DAY_WEIGHTED_FIXED_CAPITAL_NET"
    # V10.2.1 must not rewrite the historical pass/fail battery.
    assert report["gate_battery_version"] == v10.LEGACY_GATE_BATTERY_VERSION


def test_provenance_manifest_fails_closed_when_sector_panel_is_incomplete_and_hashes_inputs():
    idx = pd.to_datetime(["2024-01-02", "2024-01-03"])
    sector_map = {"AAA": "NIFTY BANK", "BBB": "NIFTY IT"}
    sector_hist = {
        "NIFTY BANK": pd.DataFrame({"close": [100.0, 101.0]}, index=idx),
        "NIFTY IT": pd.DataFrame(),
    }
    histories = {
        "AAA": {"membership": pd.Series([True, True], index=idx), "lot_size": pd.Series([10, 10], index=idx)},
        "BBB": {"membership": pd.Series([True, True], index=idx), "lot_size": pd.Series([20, 20], index=idx)},
    }
    manifest = ri.build_v10_input_manifest(
        research_symbols=["AAA", "BBB"],
        sector_map=sector_map,
        sector_history_by_symbol=sector_hist,
        histories=histories,
        gate_battery_version="battery-v1",
        cost_model={"round_trip": 0.0018},
    )
    assert manifest["sector_panel_complete"] is False
    assert manifest["sector_histories_expected"] == 2
    assert manifest["sector_histories_loaded"] == 1
    assert manifest["missing_sector_histories"] == ["NIFTY IT"]
    assert len(manifest["manifest_sha256"]) == 64
    assert len(manifest["sector_map_sha256"]) == 64
    assert set(manifest["membership_sha256_by_symbol"]) == {"AAA", "BBB"}
    assert set(manifest["lot_size_sha256_by_symbol"]) == {"AAA", "BBB"}


def test_event_artifact_is_deterministic_and_persists_hash(tmp_path):
    ev = _uneven_events()
    a = ri.persist_event_artifact(tmp_path / "events-a.csv.gz", ev)
    b = ri.persist_event_artifact(tmp_path / "events-b.csv.gz", ev.sample(frac=1.0, random_state=7))
    assert a["row_count"] == len(ev)
    assert a["content_sha256"] == b["content_sha256"]
    assert Path(a["path"]).exists()


def test_feasibility_registration_is_binding_and_names_t_bar():
    assessment = rf.assess_pretrial_feasibility(
        prior_gross_effect=0.0007,
        round_trip_cost=0.0018,
        sigma_day=0.01,
        effective_days=250,
        t_bar=3.25,
        t_bar_name="BONFERRONI_44_READS",
        source="published prior",
        horizon="1D",
    )
    assert assessment["t_bar"] == 3.25
    assert assessment["t_bar_name"] == "BONFERRONI_44_READS"
    with pytest.raises(rf.TrialRegistrationRefused):
        rf.require_feasible_registration(assessment)


def test_complete_sector_panel_is_required_before_trial21_feature_construction():
    status = ri.validate_sector_panel(
        research_symbols=["AAA", "BBB"],
        sector_map={"AAA": "S1", "BBB": "S2"},
        sector_history_by_symbol={"S1": pd.DataFrame({"close": [1.0]}), "S2": pd.DataFrame()},
    )
    assert status["complete"] is False
    assert status["missing"] == ["S2"]


def test_legacy_v102_state_migrates_read_only_without_alpha_rerun():
    raw = {
        "build": v10.PREVIOUS_BUILD_ID,
        "status": "done",
        "result": {"trial21": {"status": "FAIL"}, "trial22": {"status": "FAIL"}, "final_read": False},
        "started_at": "2026-09-04T08:00:00+05:30",
        "finished_at": "2026-09-04T08:46:00+05:30",
    }
    migrated = v10.migrate_previous_result_state(raw)
    assert migrated["build"] == v10.BUILD_ID
    assert migrated["status"] == "done"
    assert migrated["result"]["source_build"] == v10.PREVIOUS_BUILD_ID
    assert migrated["result"]["alpha_rerun_performed"] is False
    assert migrated["result"]["provenance_lock_status"] == "LEGACY_SUMMARY_LOCKED_RAW_EVENT_ROWS_UNAVAILABLE"
    assert migrated["result"]["final_read"] is False


def test_research_record_keeps_both_trial21_reads_and_confirms_runtime_sector_panel_dependency():
    history = v10.trial21_read_history()
    assert [x["build_label"] for x in history] == ["V10.0", "V10.2"]
    assert history[0]["bull"]["events"] == 586
    assert history[1]["bull"]["events"] == 564
    assert history[0]["sector_histories_loaded"] == 15
    assert history[1]["sector_histories_loaded"] == 10
    assert history[1]["cause_status"] == "CONFIRMED_RUNTIME_SECTOR_PANEL_DEPENDENCY"
    # The gate existed in V10.0; do not falsely record it as a V10.2 battery change.
    assert history[1]["sector_concentration_gate_new_in_v102"] is False


def test_v10_runner_fails_trial21_closed_when_sector_panel_is_incomplete(monkeypatch):
    from app import backtest
    idx = pd.bdate_range("2018-05-01", periods=160)
    close = pd.Series(np.linspace(100, 120, len(idx)), index=idx)
    px = pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99, "close": close}, index=idx)
    hist = {
        "membership": pd.Series(True, index=idx),
        "lot_size": pd.Series(10, index=idx),
        "near_settle": close * 1.001,
        "next_settle": close * 1.002,
        "near_expiry": pd.Series(idx + pd.Timedelta(days=20), index=idx),
        "next_expiry": pd.Series(idx + pd.Timedelta(days=50), index=idx),
    }
    integrity = {
        "nse_history_by_symbol": {"AAA": hist, "BBB": hist, "_meta": {"date_coverage": 1.0}},
        "nse_cash_by_symbol": {"AAA": px, "BBB": px, "_meta": {"date_coverage": 1.0}},
        "market_history": px,
        "sector_history_by_symbol": {"S1": px, "S2": pd.DataFrame()},
        "sector_map": {"AAA": "S1", "BBB": "S2"},
        "_explicit_internal_replay": True,
    }
    monkeypatch.setattr(backtest.v10_directional_edge, "trial21_features", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Trial21 must not be constructed")))
    result = backtest.run_v10_directional_lab(None, symbols=["AAA", "BBB"], integrity_data=integrity)
    assert result["trial21"]["status"] == "INTEGRITY_FAILURE_SECTOR_PANEL_INCOMPLETE"
    assert result["trial21"]["pass"] is False
    assert result["integrity"]["sector_panel_complete"] is False
    assert result["integrity"]["missing_sector_histories"] == ["S2"]
    assert result["input_manifest"]["sector_histories_expected"] == 2
    assert result["input_manifest"]["sector_histories_loaded"] == 1


def test_v1021_start_refuses_new_alpha_rerun_by_default(monkeypatch):
    from app import backtest
    with backtest._v10_lock:
        backtest._v10_state.clear()
        backtest._v10_state.update(backtest._default_v10_state())
    out = backtest.start_v10_directional_lab(None, symbols=["AAA"])
    assert out["started"] is False
    assert out["reason"] == "V10.2.1 is a provenance lock and refuses new alpha rereads."


def test_backtest_state_loader_migrates_completed_v102_state_without_rerun(tmp_path, monkeypatch):
    from app import backtest
    path = tmp_path / "v10-state.json"
    path.write_text(json.dumps({
        "build": v10.PREVIOUS_BUILD_ID,
        "status": "done",
        "result": {"trial21": {"status": "FAIL"}, "trial22": {"status": "FAIL"}, "final_read": False},
        "started_at": "2026-09-04T08:00:00+05:30",
        "finished_at": "2026-09-04T08:46:00+05:30",
    }), encoding="utf-8")
    monkeypatch.setattr(backtest, "_V10_STATE_PATH", path)
    out = backtest._load_v10_state()
    assert out["build"] == v10.BUILD_ID
    assert out["status"] == "done"
    assert out["migrated_from_build"] == v10.PREVIOUS_BUILD_ID
    assert out["result"]["alpha_rerun_performed"] is False


def test_freeze_validation_event_artifacts_records_bull_and_bear_hashes(tmp_path):
    bull = _uneven_events().iloc[:3].copy()
    bear = _uneven_events().iloc[3:5].copy()
    out = v10.freeze_validation_event_artifacts(21, bull, bear, tmp_path)
    assert out["trial"] == 21
    assert out["bull"]["row_count"] == 3
    assert out["bear"]["row_count"] == 2
    assert len(out["bull"]["content_sha256"]) == 64
    assert Path(out["bull"]["path"]).exists()
    assert Path(out["bear"]["path"]).exists()


def test_v1021_ui_exposes_provenance_lock_and_matching_inference():
    html = Path("app/templates/backtest.html").read_text(encoding="utf-8")
    assert "V10.2.1 Provenance &amp; Statistical Integrity Lock" in html
    assert "New alpha rereads are disabled" in html
    assert "Event-clustered t" in html
    assert "Cluster effective N" in html
    assert "ρ status" in html
    assert "Input manifest" in html
    assert "Trial-21 read history" in html


def test_provenance_manifest_hashes_market_cash_and_basis_inputs_that_feed_features():
    idx = pd.to_datetime(["2024-01-02", "2024-01-03"])
    px = pd.DataFrame({"open":[99,100],"close":[100,101]}, index=idx)
    hist = {
        "membership": pd.Series([True, True], index=idx),
        "lot_size": pd.Series([10, 10], index=idx),
        "near_settle": pd.Series([100.2, 101.2], index=idx),
        "next_settle": pd.Series([100.4, 101.4], index=idx),
        "near_expiry": pd.Series(pd.to_datetime(["2024-01-25", "2024-01-25"]), index=idx),
        "next_expiry": pd.Series(pd.to_datetime(["2024-02-29", "2024-02-29"]), index=idx),
    }
    out = ri.build_v10_input_manifest(
        research_symbols=["AAA"], sector_map={"AAA":"S1"},
        sector_history_by_symbol={"S1":px}, histories={"AAA":hist},
        market_history=px, cash_by_symbol={"AAA":px},
        gate_battery_version="b1", cost_model={"round_trip":0.0018},
    )
    assert len(out["market_history_sha256"]) == 64
    assert len(out["cash_history_sha256_by_symbol"]["AAA"]) == 64
    assert len(out["basis_inputs_sha256_by_symbol"]["AAA"]) == 64


def test_rho_at_or_above_point_95_is_reported_not_identified():
    vals = pd.Series([0.01]*5 + [-0.01]*5)
    days = pd.Series(["d1"]*5 + ["d2"]*5)
    out = ri.clustered_mean_inference(vals, days)
    assert out["rho"] is None
    assert out["rho_status"] == "NOT_IDENTIFIED_RHO_AT_OR_ABOVE_0_95"
