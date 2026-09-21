"""Artifact generation for Stage-2 index option confirmation research."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .index_option_history import LOCKED_SPLITS, _complete_regular_sessions
from .index_option_stage2 import (
    evaluate_confirmation_grid,
    summarize_by_geopolitical_regime,
    summarize_confirmation_grid,
)


def _split(trades: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    if trades is None or trades.empty:
        return pd.DataFrame(columns=[] if trades is None else trades.columns)
    d = pd.to_datetime(trades["session"]).dt.date
    lo = pd.Timestamp(start).date()
    hi = pd.Timestamp(end).date()
    return trades[(d >= lo) & (d <= hi)].copy()


def run_stage2_from_bars(
    bars_1m: pd.DataFrame,
    *,
    output_dir,
    range_pct_bounds=None,
) -> dict:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    complete_bars, session_quality = _complete_regular_sessions(bars_1m)
    if complete_bars.empty:
        raise RuntimeError("No complete regular NIFTY sessions available for Stage-2 research")

    trades = evaluate_confirmation_grid(
        complete_bars,
        range_pct_bounds=range_pct_bounds,
    )
    trades_path = output / "stage2_trades.csv"
    trades.to_csv(trades_path, index=False)

    artifacts = {"trades": trades_path.name}
    split_coverage = {}

    for horizon in (60, 120):
        overall = summarize_confirmation_grid(trades, horizon_minutes=horizon)
        path = output / f"stage2_summary_{horizon}m.csv"
        overall.to_csv(path, index=False)
        artifacts[f"summary_{horizon}m"] = path.name

    for split_name, (start, end) in LOCKED_SPLITS.items():
        split = _split(trades, start, end)
        split_coverage[split_name] = {
            "start": start,
            "end": end,
            "signal_rows": int(len(split)),
            "sessions": int(split["session"].nunique()) if not split.empty else 0,
        }
        for horizon in (60, 120):
            summary = summarize_confirmation_grid(split, horizon_minutes=horizon)
            path = output / f"stage2_{split_name}_summary_{horizon}m.csv"
            summary.to_csv(path, index=False)
            artifacts[f"{split_name}_summary_{horizon}m"] = path.name

    holdout = _split(trades, "2026-01-01", "2026-08-31")
    for horizon in (60, 120):
        regime = summarize_by_geopolitical_regime(holdout, horizon_minutes=horizon)
        path = output / f"stage2_2026_geopolitical_regime_{horizon}m.csv"
        regime.to_csv(path, index=False)
        artifacts[f"geopolitical_regime_{horizon}m"] = path.name

    manifest = {
        "research_stage": "Index Option Buying V1 / Stage 2",
        "instrument": "NIFTY 50",
        "source_bars_raw": int(len(bars_1m)),
        "source_bars_complete_sessions": int(len(complete_bars)),
        "session_quality": session_quality,
        "signal_rows": int(len(trades)),
        "range_pct_bounds": list(range_pct_bounds) if range_pct_bounds is not None else None,
        "locked_splits": {
            name: {"start": start, "end": end}
            for name, (start, end) in LOCKED_SPLITS.items()
        },
        "split_coverage": split_coverage,
        "confirmation_modes": [
            "immediate",
            "double_close",
            "acceptance_5m",
            "retest_10m",
        ],
        "geopolitical_diagnostic": {
            "war_start": "2026-02-28",
            "purpose": "diagnostic_only_not_signal_filter_or_tuning_input",
        },
        "artifacts": artifacts,
        "production_deployed": False,
        "v12_recorder_used_for_tuning": False,
    }
    manifest_path = output / "stage2_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {
        "manifest": manifest,
        "manifest_path": manifest_path,
        "trades_path": trades_path,
    }
