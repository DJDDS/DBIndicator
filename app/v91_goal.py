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

BUILD_ID = "2026-08-31-INSTITUTIONAL-V9.2.6-LIVE-OPPORTUNITY-RADAR"
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



# Immutable audit record from the one-shot Bear FSB final test consumed on
# 2026-08-30.  V9.2 diagnostics must never regenerate a different final
# sample from a later rolling 180-day window.  Exact final event identifiers
# were not persisted by the consumed run, so cohort-level final decomposition
# is intentionally unavailable rather than fabricated.
CONSUMED_BEAR_FSB_FINAL_SUMMARY = {
    "trade_count": 68,
    "avg_return_pct": -0.208,
    "profit_factor": 0.68,
    "verdict": "REJECT",
}

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



def _bull_clv(row: dict) -> float | None:
    cp = _f(row.get("close_position_pct"))
    if cp is not None:
        return round(float(np.clip(cp, 0.0, 100.0)), 2)
    hi, lo, close = (_f(row.get(k)) for k in ("high", "low", "close"))
    if None in (hi, lo, close) or hi <= lo:
        return None
    return round(float(np.clip((close - lo) / (hi - lo) * 100.0, 0.0, 100.0)), 2)


def bull_accumulation_gate_funnel(events: Iterable[dict]) -> dict:
    """Cumulative diagnostic funnel for the unchanged Bull Accumulation rule.

    This reports where the population disappears; it does not alter eligibility
    or propose replacement thresholds.  The input may contain the broader V9.2
    price-up/OI-up diagnostic seeds as well as the original V9.1 probes.
    """
    rows = [e for e in (events or []) if e.get("v92_accumulation_seed") or e.get("v91_accumulation_probe")]

    def finite_ge(row, key, threshold):
        v = _f(row.get(key))
        return v is not None and v >= threshold

    def seed(row):
        p = _f(row.get("price_chg_60m_pct"))
        oi = _f(row.get("oi_chg_60m_pct"))
        return p is not None and p > 0 and oi is not None and oi > 0

    def basis_ok(row):
        value = _f(row.get("basis_acceleration"))
        return value is None or value >= -0.02

    def score_ok(row):
        score = _consensus([row.get("v8_participation"), row.get("v8_relative"), row.get("v8_derivatives"), _bull_clv(row)])
        return score is not None and score >= 70.0

    def vwap_available(row):
        explicit = row.get("bull_vwap_available")
        if explicit is not None:
            return bool(explicit)
        # Compatibility for older unit fixtures/shards where the only VWAP
        # field was direction-relative. A real True/False value means VWAP was
        # available; None means it was not evaluable for this broad Bull seed.
        return row.get("vwap_side_agrees") is not None

    def above_vwap(row):
        explicit = row.get("bull_above_vwap")
        if explicit is not None:
            return bool(explicit)
        return row.get("vwap_side_agrees") is True

    gates = [
        ("price_up_oi_up", "Price up + OI up", seed),
        ("long_buildup", "Long Buildup state", lambda r: r.get("v8_oi_state") == "Long Buildup"),
        ("vwap_available", "VWAP data available", vwap_available),
        ("above_vwap", "Above-VWAP acceptance", above_vwap),
        ("tod_rvol_ge_1", "TOD RVOL >= 1.0", lambda r: finite_ge(r, "tod_rvol", 1.0)),
        ("participation_ge_70", "Participation >= 70", lambda r: finite_ge(r, "v8_participation", 70.0)),
        ("relative_strength_ge_70", "Relative Strength >= 70", lambda r: finite_ge(r, "v8_relative", 70.0)),
        ("derivatives_ge_65", "Derivatives >= 65", lambda r: finite_ge(r, "v8_derivatives", 65.0)),
        ("bull_clv_ge_60", "Bull CLV >= 60", lambda r: (_bull_clv(r) is not None and _bull_clv(r) >= 60.0)),
        ("basis_non_deteriorating", "Basis acceleration >= -0.02 or unavailable", basis_ok),
        ("consensus_ge_70", "Median evidence >= 70", score_ok),
    ]
    survivors = rows
    stages = []
    previous = len(rows)
    for gate, label, predicate in gates:
        survivors = [r for r in survivors if predicate(r)]
        current = len(survivors)
        stages.append({
            "gate": gate, "label": label, "survivors": current,
            "dropped_at_gate": max(0, previous - current),
            "survival_pct_of_seed": round(current / len(rows) * 100.0, 1) if rows else 0.0,
        })
        previous = current
    return {
        "diagnostic_only": True,
        "seed_count": len(rows),
        "qualified": len(survivors),
        "stages": stages,
        "message": "Diagnostic only: identifies the population bottleneck without changing any Bull threshold.",
    }


def _cohort_stats(events: Iterable[dict], classifier) -> dict:
    groups = {}
    for event in events or []:
        key = classifier(event)
        groups.setdefault(str(key), []).append(event)
    return {key: _stats(rows) for key, rows in sorted(groups.items())}


def _time_bucket(event: dict) -> str:
    raw = event.get("signal_time") or event.get("entry_time")
    try:
        # Research timestamps are ISO strings; the local wall-clock portion is
        # sufficient for this descriptive time-of-day cohort.
        text = str(raw).split("T")[-1][:5]
        hh, mm = (int(x) for x in text.split(":"))
        minutes = hh * 60 + mm
    except Exception:
        return "unknown"
    if minutes < 10 * 60 + 45:
        return "opening_to_10_45"
    if minutes < 13 * 60 + 30:
        return "midday_10_45_to_13_30"
    return "late_after_13_30"


def _period_regime_summary(events: Iterable[dict]) -> dict:
    rows = list(events or [])
    def basis_state(e):
        v = _f(e.get("basis_acceleration"))
        if v is None:
            return "missing"
        return "deteriorating" if v < 0 else "non_deteriorating"
    def sector_state(e):
        v = _f(e.get("stock_sector_lead_pct"))
        if v is None:
            return "missing"
        return "weaker_than_sector" if v < 0 else "not_weaker_than_sector"
    def oi_bucket(e):
        v = _f(e.get("oi_chg_60m_pct"))
        if v is None:
            return "missing"
        if v < 2.0:
            return "oi_0_to_2pct"
        if v < 5.0:
            return "oi_2_to_5pct"
        return "oi_5pct_plus"
    def index_trend(e):
        v = _f(e.get("index_ret_8_pct"))
        if v is None:
            return "missing"
        if v >= 0.35:
            return "index_up"
        if v <= -0.35:
            return "index_down"
        return "index_flat"
    def market_volatility(e):
        v = _f(e.get("index_vol_20bar_pct"))
        if v is None:
            return "missing"
        return "high_vol" if v >= 0.25 else "normal_vol"
    def oi_persistence(e):
        v = _f(e.get("oi_acceleration"))
        if v is None:
            return "missing"
        if v > 0.05:
            return "accelerating_oi"
        if v < -0.05:
            return "decelerating_oi"
        return "stable_oi"
    def post_positioning(e):
        p = _f(e.get("future_price_chg_60m_pct"))
        oi = _f(e.get("future_oi_chg_60m_pct"))
        if p is None or oi is None:
            return "missing"
        if p < 0 and oi > 0:
            return "shorts_persisting"
        if p > 0 and oi < 0:
            return "short_covering"
        if p < 0 and oi < 0:
            return "long_unwinding"
        if p > 0 and oi > 0:
            return "long_buildup_reversal"
        return "mixed_flat"
    regime = lambda e: e.get("market_regime") or "Unknown"

    def numeric_summary(key):
        vals = [_f(e.get(key)) for e in rows]
        vals = [v for v in vals if v is not None]
        if not vals:
            return {"n": 0, "mean": None, "median": None}
        return {"n": len(vals), "mean": round(float(np.mean(vals)), 4), "median": round(float(np.median(vals)), 4)}

    return {
        "overall": _stats(rows),
        "market_regime": _cohort_stats(rows, regime),
        "index_trend": _cohort_stats(rows, index_trend),
        "market_volatility": _cohort_stats(rows, market_volatility),
        "basis_direction": _cohort_stats(rows, basis_state),
        "sector_relative": _cohort_stats(rows, sector_state),
        "time_bucket": _cohort_stats(rows, _time_bucket),
        "oi_strength_bucket": _cohort_stats(rows, oi_bucket),
        "oi_persistence": _cohort_stats(rows, oi_persistence),
        "post_60m_positioning": _cohort_stats(rows, post_positioning),
        "feature_summary": {
            "oi_chg_60m_pct": numeric_summary("oi_chg_60m_pct"),
            "oi_acceleration": numeric_summary("oi_acceleration"),
            "basis_acceleration": numeric_summary("basis_acceleration"),
            "stock_sector_lead_pct": numeric_summary("stock_sector_lead_pct"),
            "index_ret_8_pct": numeric_summary("index_ret_8_pct"),
            "index_vol_20bar_pct": numeric_summary("index_vol_20bar_pct"),
            "tod_rvol": numeric_summary("tod_rvol"),
        },
    }


def bear_fsb_regime_decomposition(events: Iterable[dict]) -> dict:
    """Audit the rejected Bear FSB rule without regenerating its final sample.

    The one-shot final result is immutable.  The consumed run did not persist
    exact final event identifiers, therefore V9.2 reports the exact consumed
    final summary and refuses to invent cohort-level final diagnostics from a
    later rolling window. Validation cohorts remain descriptive only.
    """
    candidates = select_frozen_bear_fsb(events)
    _dev, validation, _rolling_final = _split_60_20_20(candidates)
    immutable_final = dict(CONSUMED_BEAR_FSB_FINAL_SUMMARY)
    return {
        "diagnostic_only": True,
        "rule_status": "REJECTED_FINAL_DO_NOT_RETUNE",
        "breadth_history": "UNAVAILABLE_IN_CURRENT_HISTORICAL_DATASET",
        "validation": _period_regime_summary(validation),
        "final": {
            "overall": immutable_final,
            "market_regime": {}, "index_trend": {}, "market_volatility": {},
            "basis_direction": {}, "sector_relative": {}, "time_bucket": {},
            "oi_strength_bucket": {}, "oi_persistence": {}, "post_60m_positioning": {},
            "feature_summary": {},
        },
        "final_sample_source": "IMMUTABLE_CONSUMED_FINAL_SUMMARY",
        "final_cohort_analysis_available": False,
        "message": "The exact consumed final summary is preserved (68 trades, -0.208%, PF 0.68). Exact final event IDs were not persisted, so V9.2 does not fabricate final regime cohorts from a later rolling window.",
    }

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
