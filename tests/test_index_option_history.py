import datetime as dt
import json

import pandas as pd

from app import index_option_history


class FakeKite:
    def __init__(self, rows):
        self.rows = rows
        self.history_calls = []

    def instruments(self, exchange):
        assert exchange == "NSE"
        return [{"tradingsymbol": "NIFTY 50", "instrument_token": 256265, "segment": "INDICES"}]

    def historical_data(self, instrument_token, start, end, interval, continuous=False, oi=False):
        self.history_calls.append((instrument_token, start, end, interval, continuous, oi))
        return [
            row for row in self.rows
            if pd.Timestamp(start) <= pd.Timestamp(row["date"]).tz_localize(None)
            <= pd.Timestamp(end)
        ]


def _rows_for_days(days):
    rows = []
    for day in days:
        idx = pd.date_range(f"{day} 09:15", periods=375, freq="min")
        for ts in idx:
            close = 100.0
            if ts.time() >= dt.time(10, 0):
                close = 100.5
            rows.append({
                "date": ts.to_pydatetime(),
                "open": close,
                "high": close + 0.1,
                "low": close - 0.1,
                "close": close,
                "volume": 0,
            })
    return rows


def _rows_for_two_days():
    return _rows_for_days(("2026-09-01", "2026-09-02"))


def test_fetch_nifty_history_uses_existing_chunker_and_returns_sorted_unique(monkeypatch):
    rows = _rows_for_two_days()
    kite = FakeKite(rows)

    monkeypatch.setattr(index_option_history.scanner, "_index_token_cache", {})

    frame = index_option_history.fetch_nifty_1m_history(
        kite,
        "2026-09-01 09:15",
        "2026-09-02 15:31",
    )

    assert len(frame) == 750
    assert frame.index.is_monotonic_increasing
    assert not frame.index.duplicated().any()
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert kite.history_calls
    assert all(call[0] == 256265 for call in kite.history_calls)
    assert all(call[3] == "minute" for call in kite.history_calls)


def test_stage1_artifacts_include_raw_bars_trades_summaries_and_manifest(tmp_path):
    rows = _rows_for_two_days()
    bars = pd.DataFrame(rows).rename(columns={"date": "timestamp"}).set_index("timestamp")

    result = index_option_history.run_stage1_from_bars(bars, output_dir=tmp_path)

    for key in (
        "bars_path",
        "trades_path",
        "summary_60m_path",
        "summary_120m_path",
        "manifest_path",
    ):
        assert result[key].exists()

    manifest = json.loads(result["manifest_path"].read_text(encoding="utf-8"))
    assert manifest["instrument"] == "NIFTY 50"
    assert manifest["source_bars"] == 750
    assert manifest["production_deployed"] is False
    assert manifest["v12_recorder_used_for_tuning"] is False
    assert manifest["locked_splits"]["development"] == {
        "start": "2019-01-01",
        "end": "2023-12-31",
    }

    summary = pd.read_csv(result["summary_60m_path"])
    assert set(summary["opening_range_minutes"]) == {15, 30, 45}
    assert set(summary["trigger_minutes"]) == {1, 3, 5}


def test_locked_split_summaries_are_written_without_pooling_periods(tmp_path):
    rows = _rows_for_days(("2023-12-29", "2024-01-02", "2026-08-31"))
    bars = pd.DataFrame(rows).rename(columns={"date": "timestamp"}).set_index("timestamp")

    result = index_option_history.run_stage1_from_bars(bars, output_dir=tmp_path)
    manifest = json.loads(result["manifest_path"].read_text(encoding="utf-8"))

    assert manifest["split_coverage"]["development"]["sessions"] == 1
    assert manifest["split_coverage"]["validation"]["sessions"] == 1
    assert manifest["split_coverage"]["historical_holdout"]["sessions"] == 1

    for split in ("development", "validation", "historical_holdout"):
        for horizon in (60, 120):
            path = tmp_path / f"stage1_{split}_summary_{horizon}m.csv"
            assert path.exists()
            summary = pd.read_csv(path)
            assert not summary.empty


def test_pdf_control_range_gate_can_be_run_as_separate_artifact_set(tmp_path):
    rows = _rows_for_two_days()
    bars = pd.DataFrame(rows).rename(columns={"date": "timestamp"}).set_index("timestamp")

    result = index_option_history.run_stage1_from_bars(
        bars,
        output_dir=tmp_path,
        range_pct_bounds=(0.0015, 0.0060),
    )
    manifest = json.loads(result["manifest_path"].read_text(encoding="utf-8"))
    assert manifest["range_pct_bounds"] == [0.0015, 0.006]
