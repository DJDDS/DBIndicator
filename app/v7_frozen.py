"""V7 frozen production-candidate protocol.

This module intentionally contains one rule and one acceptance gate.  It is
not a parameter-search surface.  The purpose is to spend the previously locked
final 20% exactly once on the rule that survived V6 validation.
"""
from __future__ import annotations

import hashlib
import json
from typing import Iterable

import numpy as np

BUILD_ID = "2026-08-29-INSTITUTIONAL-V7-FROZEN"
RULE_ID = "RR_LONG_CATALYST60_15M_NEXTBAR_1D"

_FROZEN_RULE = {
    "rule_id": RULE_ID,
    "setup_timeframe": "15minute",
    "execution_timeframe": "15minute",
    "direction": "Bullish",
    "breakout_source": "Recent Range",
    "catalyst_score_min": 60.0,
    "entry": "next executable 15-minute bar after confirmed escape",
    "evaluation_horizon": "1D",
    "research_days": 180,
    "cost_pct": 0.08,
    "slippage_pct_per_side": 0.05,
    "universe": "full NSE stock-F&O watchlist",
    "split": "60% development / 20% validation / 20% final",
    "acceptance": {
        "min_final_trades": 80,
        "min_avg_return_pct": 0.15,
        "min_profit_factor": 1.20,
        "chronological_blocks": 4,
        "required_positive_blocks": 3,
    },
}


def _fingerprint() -> str:
    raw = json.dumps(_FROZEN_RULE, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def frozen_rule_spec() -> dict:
    spec = json.loads(json.dumps(_FROZEN_RULE))
    spec["fingerprint"] = _fingerprint()
    spec["build_id"] = BUILD_ID
    return spec


def _finite(value) -> bool:
    try:
        return value is not None and bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def select_frozen_candidates(events: Iterable[dict]) -> list[dict]:
    """The one frozen rule.  No optional gates or threshold knobs."""
    out = []
    for event in events or []:
        if event.get("direction") != "Bullish":
            continue
        if event.get("breakout_source") != "Recent Range":
            continue
        score = event.get("catalyst_score")
        if not _finite(score) or float(score) < 60.0:
            continue
        out.append(event)
    return sorted(out, key=lambda e: e.get("entry_time", ""))


def _stats(events: Iterable[dict]) -> dict:
    vals = []
    for event in events or []:
        value = (event.get("swing_returns") or {}).get("1D")
        if _finite(value):
            vals.append(float(value))
    if not vals:
        return {
            "trade_count": 0,
            "win_rate_pct": None,
            "avg_return_pct": None,
            "median_return_pct": None,
            "profit_factor": None,
        }
    wins = [v for v in vals if v > 0]
    losses = [v for v in vals if v < 0]
    gross_profit = float(sum(wins))
    gross_loss = abs(float(sum(losses)))
    pf = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else None)
    return {
        "trade_count": len(vals),
        "win_rate_pct": round(len(wins) / len(vals) * 100.0, 1),
        "avg_return_pct": round(float(np.mean(vals)), 3),
        "median_return_pct": round(float(np.median(vals)), 3),
        "profit_factor": round(float(pf), 2) if pf is not None and np.isfinite(pf) else pf,
    }


def _split_60_20_20(events: Iterable[dict]):
    rows = sorted(list(events or []), key=lambda e: e.get("entry_time", ""))
    n = len(rows)
    i = int(np.floor(n * 0.60))
    j = int(np.floor(n * 0.80))
    return rows[:i], rows[i:j], rows[j:]


def _chronological_blocks(events: Iterable[dict], block_count: int = 4) -> list[dict]:
    rows = sorted(list(events or []), key=lambda e: e.get("entry_time", ""))
    if not rows:
        return []
    chunks = np.array_split(np.asarray(rows, dtype=object), block_count)
    out = []
    for idx, chunk in enumerate(chunks, start=1):
        stats = _stats(list(chunk))
        stats["block"] = idx
        out.append(stats)
    return out


def validate_protocol(run_context: dict | None) -> dict:
    ctx = dict(run_context or {})
    mismatches = []
    if ctx.get("setup_timeframe") != "15minute":
        mismatches.append("setup timeframe must be 15minute")
    if ctx.get("execution_timeframe") != "15minute":
        mismatches.append("execution timeframe must be 15minute")
    if int(ctx.get("days") or 0) != 180:
        mismatches.append("research window must be exactly 180 calendar days")
    if not _finite(ctx.get("cost_pct")) or abs(float(ctx.get("cost_pct")) - 0.08) > 1e-9:
        mismatches.append("cost assumption must be fixed at 0.08%")
    if not _finite(ctx.get("slippage_pct")) or abs(float(ctx.get("slippage_pct")) - 0.05) > 1e-9:
        mismatches.append("slippage assumption must be fixed at 0.05% per side")
    if ctx.get("universe_is_full_fno") is not True:
        mismatches.append("universe must be the full configured NSE stock-F&O watchlist")
    return {"valid": not mismatches, "mismatches": mismatches}


def final_verdict(final_stats: dict, blocks: list[dict]) -> dict:
    acceptance = _FROZEN_RULE["acceptance"]
    positive_blocks = sum(
        1 for block in (blocks or [])
        if _finite(block.get("avg_return_pct")) and float(block["avg_return_pct"]) > 0
    )
    checks = {
        "sample": int(final_stats.get("trade_count") or 0) >= acceptance["min_final_trades"],
        "expectancy": _finite(final_stats.get("avg_return_pct")) and float(final_stats["avg_return_pct"]) >= acceptance["min_avg_return_pct"],
        "profit_factor": (
            final_stats.get("profit_factor") is not None
            and not np.isnan(float(final_stats["profit_factor"]))
            and float(final_stats["profit_factor"]) >= acceptance["min_profit_factor"]
        ),
        "chronological_stability": len(blocks or []) == acceptance["chronological_blocks"] and positive_blocks >= acceptance["required_positive_blocks"],
    }
    return {
        "verdict": "PASS" if all(checks.values()) else "REJECT",
        "checks": checks,
        "positive_blocks": positive_blocks,
        "required_positive_blocks": acceptance["required_positive_blocks"],
    }


def frozen_candidate_report(events: Iterable[dict], run_context: dict | None = None) -> dict:
    candidates = select_frozen_candidates(events)
    development, validation, final = _split_60_20_20(candidates)
    protocol = validate_protocol(run_context)
    report = {
        "rule": frozen_rule_spec(),
        "protocol": protocol,
        "qualifying_events": len(candidates),
        "development": _stats(development),
        "validation": _stats(validation),
    }
    if not protocol["valid"]:
        report["final_test"] = {
            "locked": True,
            "message": "Final 20% withheld because this run does not match the frozen protocol.",
        }
        report["chronological_blocks"] = []
        report["verdict"] = {"verdict": "NOT_RUN", "checks": {}}
        return report

    final_stats = _stats(final)
    blocks = _chronological_blocks(final, _FROZEN_RULE["acceptance"]["chronological_blocks"])
    report["final_test"] = {"locked": False, **final_stats}
    report["chronological_blocks"] = blocks
    report["verdict"] = final_verdict(final_stats, blocks)
    return report
