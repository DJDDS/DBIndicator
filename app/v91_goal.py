"""V9.1 goal-focused research and frozen Bear FSB final protocol.

The Bear Fresh Short Buildup rule is frozen exactly as validated in V9.
Bull Institutional Accumulation is a new development/validation-only playbook;
its final 20% remains locked.
"""
from __future__ import annotations

import hashlib
import json
from statistics import median
from typing import Iterable

import numpy as np

BUILD_ID = "2026-08-30-INSTITUTIONAL-V9.1.2-STREAMING-BACKTEST"
BEAR_RULE_ID = "BEAR_FSB_15M_NEXTBAR_1D_V91"
BULL_PLAYBOOK = "Bull Institutional Accumulation"

_FROZEN_BEAR_RULE = {
    "rule_id": BEAR_RULE_ID,
    "setup_timeframe": "15minute",
    "execution_timeframe": "15minute",
    "direction": "Bearish",
    "fresh_breakout": True,
    "oi_state": "Fresh Short Buildup",
    "max_extension_atr": 1.25,
    "participation_min": 70.0,
    "relative_weakness_min": 60.0,
    "derivatives_min": 65.0,
    "bear_clv_min": 65.0,
    "basis_acceleration_max": 0.02,
    "score_min": 70.0,
    "score": "median(participation, relative weakness, derivatives, bear CLV)",
    "entry": "next executable 15-minute bar after the fresh bearish breakout",
    "evaluation_horizon": "1D",
    "research_days": 180,
    "cost_pct": 0.08,
    "slippage_pct_per_side": 0.05,
    "universe": "full NSE stock-F&O universe",
    "split": "60% development / 20% validation / 20% final",
    "acceptance": {
        "min_final_trades": 60,
        "min_avg_return_pct": 0.15,
        "min_profit_factor": 1.25,
        "chronological_blocks": 4,
        "required_positive_blocks": 3,
    },
}


def _finite(value) -> bool:
    try:
        return value is not None and bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def _f(value, default=None):
    return float(value) if _finite(value) else default


def _fingerprint() -> str:
    raw = json.dumps(_FROZEN_BEAR_RULE, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def frozen_bear_fsb_spec() -> dict:
    spec = json.loads(json.dumps(_FROZEN_BEAR_RULE))
    spec["fingerprint"] = _fingerprint()
    spec["build_id"] = BUILD_ID
    return spec


def _bear_clv(row: dict) -> float | None:
    cp = _f(row.get("close_position_pct"))
    if cp is not None:
        return round(100.0 - cp, 2)
    hi, lo, close = (_f(row.get(k)) for k in ("high", "low", "close"))
    if None in (hi, lo, close) or hi <= lo:
        return None
    return round(float(np.clip((hi - close) / (hi - lo) * 100.0, 0.0, 100.0)), 2)


def _consensus(values) -> float | None:
    clean = [float(v) for v in values if _finite(v)]
    return round(float(median(clean)), 2) if clean else None


def is_frozen_bear_fsb(row: dict) -> bool:
    """Exact V9 Bear Fresh Short Buildup rule, copied into the freeze boundary."""
    if (row.get("direction") or row.get("v8_direction")) != "Bearish":
        return False
    if row.get("fresh_breakout") is not True:
        return False
    if row.get("v8_oi_state") != "Fresh Short Buildup":
        return False
    ext = _f(row.get("breakout_extension_atr"))
    if ext is not None and ext > _FROZEN_BEAR_RULE["max_extension_atr"]:
        return False
    basis = _f(row.get("basis_acceleration"))
    if basis is not None and basis > _FROZEN_BEAR_RULE["basis_acceleration_max"]:
        return False
    part = _f(row.get("v8_participation"))
    relative = _f(row.get("v8_relative"))
    deriv = _f(row.get("v8_derivatives"))
    clv = _bear_clv(row)
    if part is None or part < _FROZEN_BEAR_RULE["participation_min"]:
        return False
    if relative is None or relative < _FROZEN_BEAR_RULE["relative_weakness_min"]:
        return False
    if deriv is None or deriv < _FROZEN_BEAR_RULE["derivatives_min"]:
        return False
    if clv is None or clv < _FROZEN_BEAR_RULE["bear_clv_min"]:
        return False
    score = _consensus([part, relative, deriv, clv])
    return score is not None and score >= _FROZEN_BEAR_RULE["score_min"]


def select_frozen_bear_fsb(events: Iterable[dict]) -> list[dict]:
    return sorted([e for e in (events or []) if is_frozen_bear_fsb(e)], key=lambda e: e.get("entry_time", ""))


def _stats(events: Iterable[dict], *, field="swing_returns", key="1D") -> dict:
    vals = []
    for event in events or []:
        value = (event.get(field) or {}).get(key)
        if _finite(value):
            vals.append(float(value))
    if not vals:
        return {"trade_count": 0, "win_rate_pct": None, "avg_return_pct": None, "median_return_pct": None, "profit_factor": None}
    wins = [v for v in vals if v > 0]
    losses = [v for v in vals if v < 0]
    gp, gl = float(sum(wins)), abs(float(sum(losses)))
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else None)
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


def _blocks(events: Iterable[dict], count=4):
    rows = sorted(list(events or []), key=lambda e: e.get("entry_time", ""))
    if not rows:
        return []
    out = []
    for idx, chunk in enumerate(np.array_split(np.asarray(rows, dtype=object), count), start=1):
        s = _stats(list(chunk))
        s["block"] = idx
        out.append(s)
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
        mismatches.append("universe must be full NSE stock-F&O")
    return {"valid": not mismatches, "mismatches": mismatches}


def _verdict(stats: dict, blocks: list[dict]) -> dict:
    acc = _FROZEN_BEAR_RULE["acceptance"]
    positive = sum(1 for b in (blocks or []) if _finite(b.get("avg_return_pct")) and float(b["avg_return_pct"]) > 0)
    checks = {
        "sample": int(stats.get("trade_count") or 0) >= acc["min_final_trades"],
        "expectancy": _finite(stats.get("avg_return_pct")) and float(stats["avg_return_pct"]) >= acc["min_avg_return_pct"],
        "profit_factor": _finite(stats.get("profit_factor")) and float(stats["profit_factor"]) >= acc["min_profit_factor"],
        "chronological_stability": len(blocks or []) == acc["chronological_blocks"] and positive >= acc["required_positive_blocks"],
    }
    return {
        "verdict": "PASS" if all(checks.values()) else "REJECT",
        "checks": checks,
        "positive_blocks": positive,
        "required_positive_blocks": acc["required_positive_blocks"],
    }


def bear_fsb_final_report(events: Iterable[dict], run_context: dict | None, *, reveal_final=True) -> dict:
    candidates = select_frozen_bear_fsb(events)
    dev, validation, final = _split_60_20_20(candidates)
    protocol = validate_protocol(run_context)
    report = {
        "rule": frozen_bear_fsb_spec(),
        "protocol": protocol,
        "qualifying_events": len(candidates),
        "development": _stats(dev),
        "validation": _stats(validation),
    }
    if not reveal_final or not protocol["valid"]:
        reason = "Final 20% remains locked on the V9.1 research path." if not reveal_final else "Final 20% withheld because protocol does not match the frozen rule."
        report["final_test"] = {"locked": True, "message": reason}
        report["chronological_blocks"] = []
        report["verdict"] = {"verdict": "NOT_RUN", "checks": {}}
        return report
    final_stats = _stats(final)
    blocks = _blocks(final, _FROZEN_BEAR_RULE["acceptance"]["chronological_blocks"])
    report["final_test"] = {"locked": False, **final_stats}
    report["chronological_blocks"] = blocks
    report["verdict"] = _verdict(final_stats, blocks)
    return report
