#!/usr/bin/env python3
"""Run audited Stage 3 on frozen NIFTY OR90/3m/double-close logic.

The runner is research-only and fail-closed.  It never downloads or fabricates
historical option quotes.  It can:
  1) verify the range gate in index points from the Stage-2 primary ledger;
  2) consume V12.1 recorder micro JSONL for forward executable ATM/ITM1 P&L;
  3) apply the frozen rule unchanged to optional Bank Nifty / Sensex 1m bars;
  4) write a SHA-256 manifest and Stage-3 gate report.

No Railway deployment or production playbook change is performed.
"""
from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path

import pandas as pd

from app import config
from app.index_option_stage3 import (
    FROZEN_SPEC_SHA256,
    build_forward_paper_ledger,
    cross_index_replication,
    extract_frozen_candidate_from_stage2,
    gate_points_check,
    load_v121_micro_jsonl,
    map_signal_frame_to_option_pnl,
    stage3_gate_report,
    summarize_option_pnl,
    write_stage3_artifacts,
)


def _args():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--stage2-primary-trades",
        default="/data/index_option_research/stage2/primary/stage2_trades.csv",
    )
    p.add_argument(
        "--stage2-gated-trades",
        default="/data/index_option_research/stage2/control_pdf_range_gate/stage2_trades.csv",
    )
    p.add_argument(
        "--historical-option-quotes",
        default=None,
        help="Optional normalized executable historical option quote CSV for 2024-2026 validation/holdout.",
    )
    p.add_argument(
        "--historical-option-refs",
        default=None,
        help="Optional snapshot reference CSV with snapshot_ts/spot/nifty_future. If absent, derive refs from quote CSV.",
    )
    p.add_argument(
        "--micro-glob",
        default=None,
        help="V12.1 micro JSONL glob. Default resolves from config.V121_INDEX_VOL_ROOT.",
    )
    p.add_argument("--banknifty-bars", default=None)
    p.add_argument("--sensex-bars", default=None)
    p.add_argument(
        "--output",
        default="/data/index_option_research/stage3",
    )
    p.add_argument(
        "--drawdown-budget",
        type=float,
        default=None,
        help="Predefined forward-paper drawdown budget in rupees. If omitted, the 40-trade gate cannot pass.",
    )
    return p.parse_args()


def _load_bars(path):
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"1-minute bars not found: {p}")
    frame = pd.read_csv(p, parse_dates=["timestamp"]).set_index("timestamp")
    return frame


def main():
    args = _args()
    print(f"STAGE3_FROZEN_SPEC_SHA256={FROZEN_SPEC_SHA256}", flush=True)

    primary_path = Path(args.stage2_primary_trades)
    if not primary_path.exists():
        raise SystemExit(f"Stage-2 primary trade ledger not found: {primary_path}")
    primary = pd.read_csv(
        primary_path,
        parse_dates=["initial_break_time", "signal_time"],
    )
    points_gate = gate_points_check(primary)
    print("STAGE3_GATE_POINTS=" + json.dumps(points_gate, sort_keys=True), flush=True)

    micro_glob = args.micro_glob or str(Path(config.V121_INDEX_VOL_ROOT) / "*_micro.jsonl")
    micro_paths = [Path(p) for p in sorted(glob.glob(micro_glob))]
    if micro_paths:
        quotes, refs = load_v121_micro_jsonl(micro_paths)
        signals, option_ledger = build_forward_paper_ledger(quotes, refs)
        option_summary = summarize_option_pnl(option_ledger)
        print(
            f"STAGE3_FORWARD_RECORDER files={len(micro_paths)} "
            f"signals={len(signals)} pnl_rows={len(option_ledger)}",
            flush=True,
        )
    else:
        quotes = pd.DataFrame()
        refs = pd.DataFrame()
        signals = pd.DataFrame()
        option_ledger = pd.DataFrame()
        option_summary = pd.DataFrame()
        print("STAGE3_FORWARD_RECORDER WAITING_DATA no micro JSONL files matched", flush=True)

    historical_signals = pd.DataFrame()
    historical_option_ledger = pd.DataFrame()
    historical_option_summary = pd.DataFrame()
    gated_path = Path(args.stage2_gated_trades)
    if args.historical_option_quotes:
        if not gated_path.exists():
            raise SystemExit(f"Stage-2 gated trade ledger not found: {gated_path}")
        quote_path = Path(args.historical_option_quotes)
        if not quote_path.exists():
            raise SystemExit(f"Historical option quotes not found: {quote_path}")
        gated = pd.read_csv(
            gated_path,
            parse_dates=["initial_break_time", "signal_time"],
        )
        historical_signals = extract_frozen_candidate_from_stage2(
            gated,
            enforce_gate=True,
            start="2024-01-01",
            end="2026-08-31",
        )
        historical_quotes = pd.read_csv(quote_path, parse_dates=["snapshot_ts"])
        if args.historical_option_refs:
            ref_path = Path(args.historical_option_refs)
            if not ref_path.exists():
                raise SystemExit(f"Historical reference snapshots not found: {ref_path}")
            historical_refs = pd.read_csv(ref_path, parse_dates=["snapshot_ts"])
        else:
            ref_cols = [
                col for col in ("snapshot_ts", "spot", "nifty_future", "india_vix")
                if col in historical_quotes.columns
            ]
            historical_refs = (
                historical_quotes[ref_cols]
                .drop_duplicates(subset=["snapshot_ts"], keep="last")
                .copy()
                if "snapshot_ts" in ref_cols
                else pd.DataFrame()
            )
        historical_option_ledger = map_signal_frame_to_option_pnl(
            historical_signals,
            historical_quotes,
            historical_refs,
        )
        historical_option_summary = summarize_option_pnl(historical_option_ledger)
        print(
            f"STAGE3_HISTORICAL_OPTION signals={len(historical_signals)} "
            f"pnl_rows={len(historical_option_ledger)}",
            flush=True,
        )
    else:
        print(
            "STAGE3_HISTORICAL_OPTION WAITING_DATA "
            "no normalized 1-minute executable bid/ask archive supplied",
            flush=True,
        )

    bars_map = {}
    bank = _load_bars(args.banknifty_bars)
    if bank is not None:
        bars_map["BANK NIFTY"] = bank
    sensex = _load_bars(args.sensex_bars)
    if sensex is not None:
        bars_map["SENSEX"] = sensex
    cross = cross_index_replication(bars_map)
    if cross.empty:
        print("STAGE3_CROSS_INDEX WAITING_DATA", flush=True)
    else:
        print("STAGE3_CROSS_INDEX " + cross.to_json(orient="records"), flush=True)

    report = stage3_gate_report(
        historical_option_ledger=historical_option_ledger,
        forward_option_ledger=option_ledger,
        gate_points=points_gate,
        cross_index=cross,
        drawdown_budget=args.drawdown_budget,
    )

    sources = [primary_path, *micro_paths]
    if gated_path.exists():
        sources.append(gated_path)
    if args.historical_option_quotes:
        sources.append(Path(args.historical_option_quotes))
    if args.historical_option_refs:
        sources.append(Path(args.historical_option_refs))
    if args.banknifty_bars:
        sources.append(Path(args.banknifty_bars))
    if args.sensex_bars:
        sources.append(Path(args.sensex_bars))

    result = write_stage3_artifacts(
        args.output,
        signals=signals,
        option_ledger=option_ledger,
        option_summary=option_summary,
        cross_index=cross,
        gate_report=report,
        historical_option_ledger=historical_option_ledger,
        historical_option_summary=historical_option_summary,
        source_files=sources,
    )
    print("STAGE3_GATE_REPORT=" + json.dumps(report, sort_keys=True, default=str), flush=True)
    print(f"STAGE3_OUTPUT={result['manifest_path'].parent}", flush=True)
    print("STAGE3_RESEARCH_ONLY production_deployed=false", flush=True)


if __name__ == "__main__":
    main()
