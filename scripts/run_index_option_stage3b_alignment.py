#!/usr/bin/env python3
"""Run the preregistered five-session Dhan/Kite timestamp alignment audit.

Research-only. This script does not place orders, change production, modify
V12/V12.1, or alter the frozen Stage-3 signal.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from app.index_option_historical_sources import fetch_dhan_expired_options
from app.index_option_stage3b import WINDOW_END, WINDOW_START, alignment_check


def _args():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--kite-nifty-bars",
        default="/data/index_option_research/stage1/primary/nifty_1m.csv",
    )
    p.add_argument(
        "--output",
        default="/data/index_option_research/stage3b_killtest/alignment",
    )
    p.add_argument("--sessions", type=int, default=5)
    return p.parse_args()


def _choose_sessions(kite: pd.DataFrame, n: int) -> list[pd.Timestamp]:
    work = kite.copy()
    if "timestamp" not in work.columns:
        raise SystemExit("Kite NIFTY bars require timestamp column")
    work["timestamp"] = pd.to_datetime(work["timestamp"], errors="coerce")
    work = work.dropna(subset=["timestamp"])
    sessions = sorted(
        x for x in work["timestamp"].dt.normalize().unique()
        if WINDOW_START <= pd.Timestamp(x).tz_localize(None) <= WINDOW_END
    )
    if len(sessions) < n:
        raise SystemExit(f"Need at least {n} Kite sessions inside Stage3B window; found {len(sessions)}")
    idx = np.linspace(0, len(sessions) - 1, n).round().astype(int)
    return [pd.Timestamp(sessions[i]) for i in idx]


def main():
    args = _args()
    token = os.environ.get("DHAN_ACCESS_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "DHAN_ACCESS_TOKEN is not set. Do not paste it into chat; set it in the Railway shell/environment."
        )

    bars_path = Path(args.kite_nifty_bars)
    if not bars_path.exists():
        raise SystemExit(f"Kite NIFTY bars not found: {bars_path}")

    kite = pd.read_csv(bars_path)
    chosen = _choose_sessions(kite, int(args.sessions))

    parts = []
    for session in chosen:
        start = session.date().isoformat()
        end = (session + pd.Timedelta(days=1)).date().isoformat()
        frame = fetch_dhan_expired_options(
            access_token=token,
            from_date=start,
            to_date=end,
            option_type="CALL",
            expression="ATM",
            expiry_flag="WEEK",
            expiry_code=1,
        )
        if frame is not None and not frame.empty:
            parts.append(frame)
        print(
            f"STAGE3B_ALIGNMENT_FETCH session={start} rows={0 if frame is None else len(frame)}",
            flush=True,
        )

    if not parts:
        raise SystemExit("No Dhan alignment rows returned for selected sessions.")

    dhan = pd.concat(parts, ignore_index=True)
    result = alignment_check(dhan, kite, sessions=int(args.sessions))

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    dhan_path = output / "dhan_atm_alignment_rows.csv"
    result_path = output / "alignment_report.json"
    chosen_path = output / "selected_sessions.json"

    dhan.to_csv(dhan_path, index=False)
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True, default=str), encoding="utf-8")
    chosen_path.write_text(
        json.dumps([str(x.date()) for x in chosen], indent=2),
        encoding="utf-8",
    )

    print("STAGE3B_ALIGNMENT=" + json.dumps(result, sort_keys=True, default=str), flush=True)
    print(f"STAGE3B_ALIGNMENT_OUTPUT={output}", flush=True)
    print("STAGE3B_RESEARCH_ONLY production_deployed=false", flush=True)


if __name__ == "__main__":
    main()
