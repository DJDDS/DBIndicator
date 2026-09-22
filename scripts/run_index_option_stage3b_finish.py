#!/usr/bin/env python3
"""Finish the current Stage-3B evidence tasks in one idempotent run.

Actions:
1) Five-session Dhan-vs-Kite NIFTY timestamp alignment (cached once complete).
2) Checkpointed 2021-09-22..2026-08-31 BANKNIFTY 1m history + zero-retune replication.
3) Rebuild live ITM1 friction from the existing V12.1 micro recorder.
4) Apply the preregistered Stage-3B decision using all currently available evidence.

Research-only. No order placement, no production deployment, no recorder changes.
"""
from __future__ import annotations

import glob
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from app import config
from app.index_option_historical_sources import fetch_dhan_expired_options
from app.index_option_stage3b import alignment_check, decision_report, validate_proxy_ledger
from app.index_option_stage3b_banknifty import (
    DEFAULT_END,
    DEFAULT_START,
    fetch_banknifty_history,
    run_banknifty_replication,
    write_banknifty_artifacts,
)
from app.index_option_stage3b_friction import (
    build_from_v121_files,
    write_friction_artifacts,
)


ROOT = Path("/data/index_option_research/stage3b_killtest")
ALIGNMENT_DIR = ROOT / "alignment"
BANK_DIR = ROOT / "banknifty_replication"
FRICTION_DIR = ROOT / "live_friction"
HIST_PROXY = Path("/data/index_option_research/stage3_historical_proxy/dhan_proxy_ledger.csv")
KITE_NIFTY = Path("/data/index_option_research/stage1/primary/nifty_1m.csv")
CONSOLIDATED = ROOT / "stage3b_consolidated_status.json"


def _choose_alignment_sessions(kite: pd.DataFrame, n=5):
    frame = kite.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame = frame.dropna(subset=["timestamp"])
    sessions = sorted(
        pd.Timestamp(x)
        for x in frame["timestamp"].dt.normalize().unique()
        if pd.Timestamp("2021-09-22") <= pd.Timestamp(x).tz_localize(None) <= pd.Timestamp("2026-08-31")
    )
    if len(sessions) < n:
        raise SystemExit(f"Need at least {n} NIFTY sessions for alignment; found {len(sessions)}")
    idx = np.linspace(0, len(sessions) - 1, n).round().astype(int)
    return [sessions[i] for i in idx]


def run_alignment(token: str):
    ALIGNMENT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = ALIGNMENT_DIR / "alignment_report.json"
    rows_path = ALIGNMENT_DIR / "dhan_atm_alignment_rows.csv"
    sessions_path = ALIGNMENT_DIR / "selected_sessions.json"

    # Alignment is a one-time data-quality audit. Reuse a completed report.
    if report_path.exists() and rows_path.exists() and sessions_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("status") == "COMPLETE":
            print("STAGE3B_ALIGNMENT RESUME COMPLETE", flush=True)
            return report

    if not KITE_NIFTY.exists():
        raise SystemExit(f"Kite NIFTY bars not found: {KITE_NIFTY}")
    kite = pd.read_csv(KITE_NIFTY)
    selected = _choose_alignment_sessions(kite, 5)

    parts = []
    for i, session in enumerate(selected, start=1):
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
            f"STAGE3B_ALIGNMENT DONE session {i}/5 date={start} rows={0 if frame is None else len(frame)}",
            flush=True,
        )

    if not parts:
        raise SystemExit("No Dhan rows returned for alignment audit")
    dhan = pd.concat(parts, ignore_index=True)
    report = alignment_check(dhan, kite, sessions=5)

    dhan.to_csv(rows_path, index=False)
    sessions_path.write_text(
        json.dumps([str(pd.Timestamp(x).date()) for x in selected], indent=2),
        encoding="utf-8",
    )
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    print("STAGE3B_ALIGNMENT_REPORT=" + json.dumps(report, sort_keys=True, default=str), flush=True)
    return report


def _bank_progress(info):
    state = "RESUME" if info["resumed"] else "DONE"
    print(
        "STAGE3B_BANKNIFTY "
        f"{state} chunk {info['chunk_number']}/{info['chunks_total']} "
        f"rows={info['rows']}",
        flush=True,
    )


def run_bank(token: str):
    bars = fetch_banknifty_history(
        access_token=token,
        output_dir=BANK_DIR,
        start=DEFAULT_START,
        end=DEFAULT_END,
        progress_callback=_bank_progress,
    )
    ledger, report = run_banknifty_replication(bars)
    write_banknifty_artifacts(BANK_DIR, bars=bars, ledger=ledger, report=report)
    print("STAGE3B_BANKNIFTY_REPORT=" + json.dumps(report, sort_keys=True, default=str), flush=True)
    return report


def run_friction():
    micro_glob = str(Path(config.V121_INDEX_VOL_ROOT) / "*_micro.jsonl")
    micro_paths = [Path(p) for p in sorted(glob.glob(micro_glob))]
    if not micro_paths:
        return pd.DataFrame(), {
            "status": "WAITING_FRICTION",
            "eligible_count": 0,
            "minimum_samples": 20,
            "reason": f"No V12.1 micro files matched {micro_glob}",
        }

    ledger, summary = build_from_v121_files(micro_paths)
    write_friction_artifacts(
        FRICTION_DIR,
        ledger=ledger,
        summary=summary,
        source_files=micro_paths,
    )
    print("STAGE3B_FRICTION_SUMMARY=" + json.dumps(summary, sort_keys=True, default=str), flush=True)
    return ledger, summary


def main():
    token = os.environ.get("DHAN_ACCESS_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "DHAN_ACCESS_TOKEN is not set in this Railway shell. Do not paste it into chat."
        )
    if not HIST_PROXY.exists():
        raise SystemExit(f"Historical Stage-3 proxy ledger not found: {HIST_PROXY}")

    ROOT.mkdir(parents=True, exist_ok=True)

    alignment = run_alignment(token)
    bank_report = run_bank(token)
    friction_ledger, friction_summary = run_friction()

    historical = validate_proxy_ledger(pd.read_csv(HIST_PROXY))
    if friction_ledger is not None and not friction_ledger.empty:
        eligible = friction_ledger[
            friction_ledger.get("decision_eligible", False).fillna(False).astype(bool)
            & friction_ledger["friction_status"].astype(str).eq("OK")
        ].copy()
    else:
        eligible = pd.DataFrame()

    decision = decision_report(
        historical,
        friction_log=eligible,
        banknifty_replication=bank_report,
    )

    payload = {
        "alignment": alignment,
        "banknifty_replication": bank_report,
        "live_friction": friction_summary,
        "decision": decision,
        "research_only": True,
        "production_deployed": False,
        "v12_v121_recorder_modified": False,
        "trial25_modified": False,
    }
    CONSOLIDATED.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )

    print("STAGE3B_CONSOLIDATED=" + json.dumps(payload, sort_keys=True, default=str), flush=True)
    print(f"STAGE3B_CONSOLIDATED_FILE={CONSOLIDATED}", flush=True)
    print("STAGE3B_FINISH_CURRENT_EVIDENCE_COMPLETE production_deployed=false", flush=True)


if __name__ == "__main__":
    main()
