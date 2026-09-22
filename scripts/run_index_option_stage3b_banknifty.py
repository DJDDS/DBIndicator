#!/usr/bin/env python3
"""Fetch and run zero-retune BANK NIFTY Stage-3B replication.

Research-only. Uses Dhan's five-year 1-minute BANKNIFTY index OHLC API.
No production deployment, no V12/V12.1 modification, no Trial-25 change.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app.index_option_stage3b_banknifty import (
    DEFAULT_END,
    DEFAULT_START,
    fetch_banknifty_history,
    run_banknifty_replication,
    write_banknifty_artifacts,
)


def _args():
    p = argparse.ArgumentParser()
    p.add_argument("--start", default=str(DEFAULT_START.date()))
    p.add_argument("--end", default=str(DEFAULT_END.date()))
    p.add_argument(
        "--output",
        default="/data/index_option_research/stage3b_killtest/banknifty_replication",
    )
    return p.parse_args()


def _progress(info):
    state = "RESUME" if info["resumed"] else "DONE"
    print(
        "STAGE3B_BANKNIFTY "
        f"{state} chunk {info['chunk_number']}/{info['chunks_total']} "
        f"{info['chunk_start']} -> {info['chunk_end']} rows={info['rows']}",
        flush=True,
    )


def main():
    args = _args()
    token = os.environ.get("DHAN_ACCESS_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "DHAN_ACCESS_TOKEN is not set. Do not paste it into chat; set it in the Railway shell/environment."
        )

    output = Path(args.output)
    bars = fetch_banknifty_history(
        access_token=token,
        output_dir=output,
        start=args.start,
        end=args.end,
        progress_callback=_progress,
    )
    print(f"STAGE3B_BANKNIFTY_BARS rows={len(bars)}", flush=True)

    ledger, report = run_banknifty_replication(bars)
    result = write_banknifty_artifacts(
        output,
        bars=bars,
        ledger=ledger,
        report=report,
    )

    print("STAGE3B_BANKNIFTY_REPORT=" + json.dumps(report, sort_keys=True, default=str), flush=True)
    print(f"STAGE3B_BANKNIFTY_SIGNAL_LEDGER={result['ledger_path']}", flush=True)
    print(f"STAGE3B_BANKNIFTY_REPORT_FILE={result['report_path']}", flush=True)
    print("STAGE3B_RESEARCH_ONLY production_deployed=false", flush=True)


if __name__ == "__main__":
    main()
