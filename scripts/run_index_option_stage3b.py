#!/usr/bin/env python3
"""Run preregistered Stage 3B kill tests without changing Stage 3.

Research-only. Historical Dhan option data remain PROXY_OHLC_NO_BID_ASK and
cannot satisfy executable bid/ask gates.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from app.index_option_stage3b import (
    FROZEN_SPEC_SHA256,
    banknifty_gross_replication,
    validate_proxy_ledger,
    write_stage3b_artifacts,
)


def _args():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--stage3-proxy-ledger",
        default="/data/index_option_research/stage3_historical_proxy/dhan_proxy_ledger.csv",
        help="Completed frozen Stage-3 Dhan proxy ledger (2022-01-01..2026-08-31).",
    )
    p.add_argument(
        "--stage3b-2021-extension-ledger",
        default=None,
        help="Optional separate Sep-Dec 2021 Dhan proxy ledger; Stage 3 artifacts are never overwritten.",
    )
    p.add_argument(
        "--friction-log",
        default=None,
        help="Optional forward ITM1 friction CSV with round_trip_friction_points.",
    )
    p.add_argument(
        "--banknifty-bars",
        default=None,
        help="Optional BANK NIFTY 1-minute OHLC CSV for zero-retune frozen-spec replication.",
    )
    p.add_argument(
        "--expiry-calendar",
        default=None,
        help="Optional CSV with session,is_expiry_day for the single preregistered refinement.",
    )
    p.add_argument(
        "--output",
        default="/data/index_option_research/stage3b_killtest",
    )
    return p.parse_args()


def _read_csv(path, *, required=False, parse_dates=None):
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        if required:
            raise SystemExit(f"Required file not found: {p}")
        raise SystemExit(f"Optional file path supplied but not found: {p}")
    return pd.read_csv(p, parse_dates=parse_dates)


def _read_bars(path):
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"BANK NIFTY bars not found: {p}")
    frame = pd.read_csv(p, parse_dates=["timestamp"])
    if "timestamp" not in frame.columns:
        raise SystemExit("BANK NIFTY bars require timestamp column")
    return frame.set_index("timestamp")


def main():
    args = _args()

    base_path = Path(args.stage3_proxy_ledger)
    if not base_path.exists():
        raise SystemExit(f"Stage-3 proxy ledger not found: {base_path}")

    ledgers = [pd.read_csv(base_path)]
    sources = [base_path]

    if args.stage3b_2021_extension_ledger:
        ext_path = Path(args.stage3b_2021_extension_ledger)
        if not ext_path.exists():
            raise SystemExit(f"2021 extension ledger not found: {ext_path}")
        ledgers.append(pd.read_csv(ext_path))
        sources.append(ext_path)

    proxy = pd.concat(ledgers, ignore_index=True)
    proxy = validate_proxy_ledger(proxy)

    friction = _read_csv(args.friction_log)
    if args.friction_log:
        sources.append(Path(args.friction_log))

    expiry_calendar = _read_csv(args.expiry_calendar)
    if args.expiry_calendar:
        sources.append(Path(args.expiry_calendar))

    bank_bars = _read_bars(args.banknifty_bars)
    bank = banknifty_gross_replication(bank_bars)
    if args.banknifty_bars:
        sources.append(Path(args.banknifty_bars))

    result = write_stage3b_artifacts(
        args.output,
        proxy_ledger=proxy,
        friction_log=friction,
        banknifty_replication=bank,
        expiry_calendar=expiry_calendar,
        source_files=sources,
    )

    print(f"STAGE3B_FROZEN_SPEC_SHA256={FROZEN_SPEC_SHA256}", flush=True)
    print("STAGE3B_RESEARCH_ONLY production_deployed=false", flush=True)
    print("STAGE3B_HISTORICAL_CLASS=PROXY_OHLC_NO_BID_ASK", flush=True)
    print("STAGE3B_EXECUTABLE_GATE=false", flush=True)
    print("STAGE3B_DECISION=" + json.dumps(result["decision"], sort_keys=True, default=str), flush=True)
    print("STAGE3B_REFINEMENT=" + json.dumps(result["refinement"], sort_keys=True, default=str), flush=True)
    print(f"STAGE3B_OUTPUT={Path(args.output)}", flush=True)


if __name__ == "__main__":
    main()
