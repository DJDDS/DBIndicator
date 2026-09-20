#!/usr/bin/env python3
"""One-shot Stage-1 NIFTY timing research runner.

Designed for an authenticated environment that already has the DBIndicator Kite token
cache available (for example the live Railway container via `railway ssh`). It writes
only to the requested output directory and never touches V12/V12.1 recorder paths.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app import kite_auth
from app.index_option_history import fetch_and_run_stage1


def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--end", default="2026-09-01")
    parser.add_argument("--output", default="/data/index_option_research/stage1")
    parser.add_argument(
        "--pdf-range-gate",
        action="store_true",
        help="Apply the PDF control gate 0.15%% <= OR width <= 0.60%%.",
    )
    return parser.parse_args()


def main():
    args = _parse_args()
    kite = kite_auth.get_kite_client()
    if kite is None:
        raise SystemExit(
            "No valid Kite session for today. Log in through the DBIndicator dashboard first."
        )

    output = Path(args.output)
    bounds = (0.0015, 0.0060) if args.pdf_range_gate else None
    result = fetch_and_run_stage1(
        kite,
        start=args.start,
        end=args.end,
        output_dir=output,
        range_pct_bounds=bounds,
    )

    print("INDEX_OPTION_STAGE1_COMPLETE")
    print(json.dumps(result["manifest"], indent=2))
    print(f"OUTPUT_DIR={output}")


if __name__ == "__main__":
    main()
