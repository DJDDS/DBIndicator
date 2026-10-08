#!/usr/bin/env python3
"""One-shot Stage-1 NIFTY timing research runner.

Designed for an authenticated environment that already has the DBIndicator Kite token
cache available (for example the live Railway container via `railway ssh`). It writes
only to the requested output directory and never touches V12/V12.1 recorder paths.

By default it fetches historical data ONCE, then runs both:
1) the primary timing-discovery study without a range-width filter, and
2) the supplied PDF control with the 0.15%-0.60% range-width gate.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app import kite_auth
from app.index_option_history import fetch_nifty_1m_history, run_stage1_from_bars


def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--end", default="2026-09-01")
    parser.add_argument("--output", default="/data/index_option_research/stage1")
    parser.add_argument(
        "--primary-only",
        action="store_true",
        help="Skip the separate PDF range-gate control run.",
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
    output.mkdir(parents=True, exist_ok=True)

    bars, fetch_report = fetch_nifty_1m_history(
        kite,
        args.start,
        args.end,
        return_fetch_report=True,
    )
    if bars.empty:
        raise SystemExit("No NIFTY 50 one-minute candles were returned.")

    primary = run_stage1_from_bars(
        bars,
        output_dir=output / "primary",
        range_pct_bounds=None,
        fetch_report=fetch_report,
    )

    payload = {"primary": primary["manifest"]}
    if not args.primary_only:
        control = run_stage1_from_bars(
            bars,
            output_dir=output / "control_pdf_range_gate",
            range_pct_bounds=(0.0015, 0.0060),
            fetch_report=fetch_report,
        )
        payload["control_pdf_range_gate"] = control["manifest"]

    run_manifest = output / "run_manifest.json"
    run_manifest.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print("INDEX_OPTION_STAGE1_COMPLETE")
    print(json.dumps(payload, indent=2))
    print(f"OUTPUT_DIR={output}")


if __name__ == "__main__":
    main()
