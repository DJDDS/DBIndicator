#!/usr/bin/env python3
"""Extract Stage-3B live ITM1 friction from existing V12.1 micro files.

This runner is research-only. It does not open a second market-data stream,
place orders, modify the recorder, or deploy anything.
"""
from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path

import pandas as pd

from app import config
from app.index_option_stage3b import decision_report, validate_proxy_ledger
from app.index_option_stage3b_friction import (
    build_from_v121_files,
    write_friction_artifacts,
)


def _args():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--micro-glob",
        default=None,
        help="V12.1 micro JSONL glob; defaults to config.V121_INDEX_VOL_ROOT/*_micro.jsonl",
    )
    p.add_argument(
        "--actual-fills",
        default=None,
        help="Optional CSV with session,tradingsymbol,entry_fill_price,exit_fill_price.",
    )
    p.add_argument(
        "--historical-proxy-ledger",
        default="/data/index_option_research/stage3_historical_proxy/dhan_proxy_ledger.csv",
        help="Frozen historical proxy ledger used only for the preregistered PARK/KILL decision.",
    )
    p.add_argument(
        "--output",
        default="/data/index_option_research/stage3b_killtest/live_friction",
    )
    p.add_argument(
        "--decision-output",
        default="/data/index_option_research/stage3b_killtest/stage3b_decision_live_friction.json",
    )
    return p.parse_args()


def main():
    args = _args()
    micro_glob = args.micro_glob or str(Path(config.V121_INDEX_VOL_ROOT) / "*_micro.jsonl")
    micro_paths = [Path(p) for p in sorted(glob.glob(micro_glob))]
    if not micro_paths:
        raise SystemExit(f"No V12.1 micro files matched: {micro_glob}")

    actual_fills = None
    if args.actual_fills:
        fill_path = Path(args.actual_fills)
        if not fill_path.exists():
            raise SystemExit(f"Actual fills file not found: {fill_path}")
        actual_fills = pd.read_csv(fill_path)

    ledger, summary = build_from_v121_files(
        micro_paths,
        actual_fills=actual_fills,
    )
    result = write_friction_artifacts(
        args.output,
        ledger=ledger,
        summary=summary,
        source_files=micro_paths,
    )

    hist_path = Path(args.historical_proxy_ledger)
    decision = None
    if hist_path.exists():
        historical = validate_proxy_ledger(pd.read_csv(hist_path))
        eligible = ledger[
            ledger.get("decision_eligible", False).fillna(False).astype(bool)
            & ledger["friction_status"].astype(str).eq("OK")
        ].copy() if not ledger.empty else pd.DataFrame()

        # decision_report consumes only the preregistered per-trade friction
        # column. BANK NIFTY is deliberately left unavailable here because the
        # current NIFTY validation split already caps the decision at PARK.
        decision = decision_report(
            historical,
            friction_log=eligible,
            banknifty_replication=None,
        )
        decision_path = Path(args.decision_output)
        decision_path.parent.mkdir(parents=True, exist_ok=True)
        decision_path.write_text(
            json.dumps(decision, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
    else:
        decision_path = None

    print(f"STAGE3B_FRICTION_MICRO_FILES={len(micro_paths)}", flush=True)
    print("STAGE3B_FRICTION_SUMMARY=" + json.dumps(summary, sort_keys=True, default=str), flush=True)
    if decision is not None:
        print("STAGE3B_DECISION_WITH_LIVE_FRICTION=" + json.dumps(decision, sort_keys=True, default=str), flush=True)
    print(f"STAGE3B_FRICTION_LEDGER={result['ledger_path']}", flush=True)
    print(f"STAGE3B_FRICTION_SUMMARY_FILE={result['summary_path']}", flush=True)
    if decision_path is not None:
        print(f"STAGE3B_DECISION_FILE={decision_path}", flush=True)
    print("STAGE3B_RESEARCH_ONLY production_deployed=false", flush=True)


if __name__ == "__main__":
    main()
