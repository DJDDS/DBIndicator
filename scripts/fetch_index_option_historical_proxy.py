#!/usr/bin/env python3
"""Fetch multi-year NIFTY expired-option OHLC proxy evidence from Dhan.

This runner is research-only. It never changes production and never upgrades
rolling OHLC to executable bid/ask evidence. It:
1) reads the existing Stage-2 primary ledger;
2) extracts ONLY the frozen Stage-3 OR90/3m/double-close/0.15%-0.60% signals;
3) fetches the Dhan near-weekly ATM-10..ATM+10 grid on each signal date;
4) tracks the same absolute strike from entry to the frozen +120m exit;
5) writes ATM and one-strike-ITM premium proxy results.

A Dhan Data API access token must be supplied in DHAN_ACCESS_TOKEN.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pandas as pd

from app.index_option_historical_sources import build_dhan_proxy_ledger
from app.index_option_stage3 import (
    FROZEN_SPEC_SHA256,
    extract_frozen_candidate_from_stage2,
    session_bootstrap_mean_ci,
)


def _args():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--stage2-primary-trades",
        default="/data/index_option_research/stage2/primary/stage2_trades.csv",
    )
    p.add_argument("--start", default="2022-01-01")
    p.add_argument("--end", default="2026-08-31")
    p.add_argument(
        "--output",
        default="/data/index_option_research/stage3_historical_proxy",
    )
    return p.parse_args()


def _summarize(ledger: pd.DataFrame) -> pd.DataFrame:
    if ledger is None or ledger.empty:
        return pd.DataFrame()
    rows = []
    for moneyness, group in ledger.groupby("moneyness", sort=True):
        ok = group[group["status"].eq("OK_PROXY")].copy()
        values = pd.to_numeric(ok.get("premium_points"), errors="coerce").dropna()
        pct = pd.to_numeric(ok.get("premium_return_pct"), errors="coerce").dropna()
        ci = session_bootstrap_mean_ci(
            ok.assign(session=ok["session"].astype(str)),
            "premium_points",
            samples=5000,
            seed=2503,
        ) if not ok.empty else {
            "mean": None, "ci_low": None, "ci_high": None, "n_sessions": 0
        }
        rows.append({
            "moneyness": moneyness,
            "requested_rows": int(len(group)),
            "mapped_rows": int(len(ok)),
            "mapping_rate_pct": float(len(ok) / len(group) * 100.0) if len(group) else None,
            "win_rate_pct": float((values > 0).mean() * 100.0) if len(values) else None,
            "mean_premium_points": float(values.mean()) if len(values) else None,
            "median_premium_points": float(values.median()) if len(values) else None,
            "mean_premium_return_pct": float(pct.mean()) if len(pct) else None,
            "bootstrap_ci_low_points": ci.get("ci_low"),
            "bootstrap_ci_high_points": ci.get("ci_high"),
            "bootstrap_n_sessions": ci.get("n_sessions"),
            "evidence_class": "PROXY_OHLC_NO_BID_ASK",
            "can_satisfy_stage3_executable_gate": False,
        })
    return pd.DataFrame(rows)


def main():
    args = _args()
    token = os.environ.get("DHAN_ACCESS_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "DHAN_ACCESS_TOKEN is not set. Historical proxy fetch cannot start. "
            "Do not paste the token into chat; set it in the shell/environment."
        )

    ledger_path = Path(args.stage2_primary_trades)
    if not ledger_path.exists():
        raise SystemExit(f"Stage-2 primary ledger not found: {ledger_path}")

    primary = pd.read_csv(
        ledger_path,
        parse_dates=["initial_break_time", "signal_time"],
    )
    signals = extract_frozen_candidate_from_stage2(
        primary,
        enforce_gate=True,
        start=args.start,
        end=args.end,
    )
    if signals.empty:
        raise SystemExit("No frozen Stage-3 signals in requested historical period.")

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    checkpoints = output / "checkpoints"
    checkpoints.mkdir(parents=True, exist_ok=True)

    completed_dates = {p.stem for p in checkpoints.glob("*.csv")}
    all_parts = []
    total = len(signals)

    for i, (_, signal) in enumerate(signals.iterrows(), start=1):
        session = str(signal["session"])
        cp = checkpoints / f"{session}.csv"
        if cp.exists() and cp.stat().st_size:
            part = pd.read_csv(cp)
            state = "RESUME"
        else:
            part = build_dhan_proxy_ledger(
                pd.DataFrame([signal]),
                access_token=token,
            )
            part.to_csv(cp, index=False)
            state = "DONE"
        all_parts.append(part)
        print(
            f"DHAN_PROXY {state} signal {i}/{total} session={session} rows={len(part)}",
            flush=True,
        )

    ledger = pd.concat(all_parts, ignore_index=True) if all_parts else pd.DataFrame()
    ledger_out = output / "dhan_proxy_ledger.csv"
    summary = _summarize(ledger)
    summary_out = output / "dhan_proxy_summary.csv"
    ledger.to_csv(ledger_out, index=False)
    summary.to_csv(summary_out, index=False)

    manifest = {
        "research_stage": "Index Option Buying V1 / Stage 3 historical proxy",
        "frozen_spec_sha256": FROZEN_SPEC_SHA256,
        "requested_start": args.start,
        "requested_end": args.end,
        "frozen_signals": int(len(signals)),
        "ledger_rows": int(len(ledger)),
        "source": "Dhan rolling expired options",
        "source_quality": "PROXY_OHLC_NO_BID_ASK",
        "same_absolute_strike_required_at_exit": True,
        "expressions_fetched_per_signal_side": 21,
        "historical_executable_pnl_gate": "WAITING_DATA",
        "reason": "Dhan expired-option endpoint provides OHLC/IV/OI/volume/spot but not historical best bid/ask.",
        "production_deployed": False,
        "files": {
            "ledger": ledger_out.name,
            "summary": summary_out.name,
        },
    }
    manifest_out = output / "manifest.json"
    manifest_out.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print("INDEX_OPTION_HISTORICAL_PROXY_COMPLETE", flush=True)
    print(summary.to_string(index=False), flush=True)
    print(f"OUTPUT_DIR={output}", flush=True)


if __name__ == "__main__":
    main()
