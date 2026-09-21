"""Artifact generation for Stage-2 index option confirmation research."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .index_option_history import LOCKED_SPLITS, _complete_regular_sessions
from .index_option_stage2 import (
    CONFIRMATION_MODES,
    Stage2Spec,
    analyze_session_confirmation,
    summarize_by_geopolitical_regime,
    summarize_confirmation_grid,
)


OPENING_RANGES = (15, 30, 45, 60, 90, 120)
TRIGGER_WINDOWS = (1, 3, 5)


def _split(trades: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    if trades is None or trades.empty:
        return pd.DataFrame(columns=[] if trades is None else trades.columns)
    d = pd.to_datetime(trades["session"]).dt.date
    lo = pd.Timestamp(start).date()
    hi = pd.Timestamp(end).date()
    return trades[(d >= lo) & (d <= hi)].copy()


def _load_family_checkpoint(path: Path) -> pd.DataFrame | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    frame = pd.read_csv(
        path,
        parse_dates=["initial_break_time", "signal_time"],
    )
    return frame


def _run_family(
    sessions: list[pd.DataFrame],
    *,
    opening_range: int,
    trigger_window: int,
    range_pct_bounds,
) -> pd.DataFrame:
    records = []
    for session in sessions:
        for confirmation in CONFIRMATION_MODES:
            row = analyze_session_confirmation(
                session,
                Stage2Spec(
                    opening_range_minutes=int(opening_range),
                    trigger_minutes=int(trigger_window),
                    confirmation=confirmation,
                ),
                range_pct_bounds=range_pct_bounds,
            )
            if row is not None:
                records.append(row)
    if not records:
        return pd.DataFrame()
    return pd.DataFrame.from_records(records).sort_values(
        ["session", "confirmation"]
    ).reset_index(drop=True)


def _finalize_stage2(
    trades: pd.DataFrame,
    *,
    output: Path,
    source_bars_raw: int,
    source_bars_complete_sessions: int,
    session_quality: dict,
    range_pct_bounds,
    checkpoint_dir: Path,
) -> dict:
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
        "source_bars_raw": int(source_bars_raw),
        "source_bars_complete_sessions": int(source_bars_complete_sessions),
        "session_quality": session_quality,
        "signal_rows": int(len(trades)),
        "range_pct_bounds": list(range_pct_bounds) if range_pct_bounds is not None else None,
        "locked_splits": {
            name: {"start": start, "end": end}
            for name, (start, end) in LOCKED_SPLITS.items()
        },
        "split_coverage": split_coverage,
        "confirmation_modes": list(CONFIRMATION_MODES),
        "geopolitical_diagnostic": {
            "war_start": "2026-02-28",
            "purpose": "diagnostic_only_not_signal_filter_or_tuning_input",
        },
        "checkpointing": {
            "enabled": True,
            "checkpoint_dir": checkpoint_dir.name,
            "families_expected": len(OPENING_RANGES) * len(TRIGGER_WINDOWS),
            "families_completed": len(list(checkpoint_dir.glob("or*_trig*.csv"))),
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


def run_stage2_from_bars(
    bars_1m: pd.DataFrame,
    *,
    output_dir,
    range_pct_bounds=None,
    progress_callback=None,
) -> dict:
    """Run Stage 2 with family-level persistent checkpoints and automatic resume.

    Each of the 18 OR/trigger families is written immediately after completion. If the
    process is interrupted by a container restart, a rerun loads existing family files
    and resumes only the missing families.
    """
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    checkpoint_dir = output / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    complete_bars, session_quality = _complete_regular_sessions(bars_1m)
    if complete_bars.empty:
        raise RuntimeError("No complete regular NIFTY sessions available for Stage-2 research")

    sessions = [
        group.copy()
        for _, group in complete_bars.groupby(complete_bars.index.normalize(), sort=True)
    ]

    family_frames = []
    completed = 0
    total = len(OPENING_RANGES) * len(TRIGGER_WINDOWS)

    for opening_range in OPENING_RANGES:
        for trigger_window in TRIGGER_WINDOWS:
            checkpoint = checkpoint_dir / f"or{opening_range}_trig{trigger_window}.csv"
            family = _load_family_checkpoint(checkpoint)
            resumed = family is not None
            if family is None:
                family = _run_family(
                    sessions,
                    opening_range=opening_range,
                    trigger_window=trigger_window,
                    range_pct_bounds=range_pct_bounds,
                )
                family.to_csv(checkpoint, index=False)

            family_frames.append(family)
            completed += 1
            if progress_callback is not None:
                progress_callback(
                    {
                        "opening_range_minutes": opening_range,
                        "trigger_minutes": trigger_window,
                        "family_number": completed,
                        "families_total": total,
                        "signal_rows": int(len(family)),
                        "resumed": resumed,
                        "checkpoint": str(checkpoint),
                    }
                )

    nonempty = [frame for frame in family_frames if frame is not None and not frame.empty]
    if not nonempty:
        raise RuntimeError("Stage-2 produced no confirmation signals")
    trades = pd.concat(nonempty, ignore_index=True).sort_values(
        ["session", "opening_range_minutes", "trigger_minutes", "confirmation"]
    ).reset_index(drop=True)

    return _finalize_stage2(
        trades,
        output=output,
        source_bars_raw=len(bars_1m),
        source_bars_complete_sessions=len(complete_bars),
        session_quality=session_quality,
        range_pct_bounds=range_pct_bounds,
        checkpoint_dir=checkpoint_dir,
    )
