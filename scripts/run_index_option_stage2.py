#!/usr/bin/env python3
"""Run Stage-2 confirmation research from the already-saved Stage-1 NIFTY bars.

No broker API calls are made. The script reads the persistent research CSV created by
Stage 1 and writes Stage-2 artifacts under a separate directory.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from app.index_option_stage2_history import run_stage2_from_bars


def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bars",
        default="/data/index_option_research/stage1/primary/nifty_1m.csv",
    )
    parser.add_argument(
        "--output",
        default="/data/index_option_research/stage2",
    )
    return parser.parse_args()


def main():
    args = _parse_args()
    bars_path = Path(args.bars)
    if not bars_path.exists():
        raise SystemExit(f"Stage-1 bars not found: {bars_path}")

    bars = pd.read_csv(bars_path, parse_dates=["timestamp"]).set_index("timestamp")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    primary = run_stage2_from_bars(
        bars,
        output_dir=output / "primary",
        range_pct_bounds=None,
    )
    control = run_stage2_from_bars(
        bars,
        output_dir=output / "control_pdf_range_gate",
        range_pct_bounds=(0.0015, 0.0060),
    )
    payload = {
        "primary": primary["manifest"],
        "control_pdf_range_gate": control["manifest"],
    }
    (output / "run_manifest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print("INDEX_OPTION_STAGE2_COMPLETE")
    print(json.dumps(payload, indent=2))
    print(f"OUTPUT_DIR={output}")


if __name__ == "__main__":
    main()
