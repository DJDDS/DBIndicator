"""V9 Professional Playbook Scanner.

V9 replaces one universal Bull/Bear score with explicit market playbooks.  The
module is intentionally deterministic: every play has a specific structural
story, a transparent evidence consensus, and a fixed anti-chase guard.  Live
news/catalyst evidence is kept separate from historical proxy research.
"""
from __future__ import annotations

import datetime as dt
from statistics import median
from typing import Iterable

import numpy as np

V9_BUILD_ID = "2026-09-01-INSTITUTIONAL-V9.3.5-MEMORY-SAFE-STAGE2"

BULL_INSTITUTIONAL_ACCUMULATION = "Bull Institutional Accumulation"
BULL_OPENING_DRIVE = "Bull Opening Drive"
BULL_PULLBACK_RECLAIM = "Bull Pullback/Reclaim"
BULL_CATALYST_CONTINUATION = "Bull Catalyst Continuation"
BEAR_FRESH_SHORT_BUILDUP = "Bear Fresh Short Buildup"
BEAR_FAILED_BREAKOUT = "Bear Failed Breakout"
BEAR_VWAP_RETEST_FAILURE = "Bear VWAP Retest Failure"

PLAYBOOKS = (
    BULL_INSTITUTIONAL_ACCUMULATION,
    BULL_OPENING_DRIVE,
    BULL_PULLBACK_RECLAIM,
    BULL_CATALYST_CONTINUATION,
    BEAR_FRESH_SHORT_BUILDUP,
    BEAR_FAILED_BREAKOUT,
    BEAR_VWAP_RETEST_FAILURE,
)

# Production eligibility is evidence-gated.  At V9.3.0 the bullish
# accumulation hypothesis is still research/shadow only and the frozen Bear
# Fresh Short Buildup rule has already failed its untouched final sample.
# Therefore no V9 playbook is allowed to drive live TRADE/WATCH shortlists yet.
ACTIVE_PLAYBOOKS = ()
SHADOW_PLAYBOOKS = (
    BULL_INSTITUTIONAL_ACCUMULATION,
    BULL_CATALYST_CONTINUATION,
)
REJECTED_PLAYBOOKS = (
    BEAR_FRESH_SHORT_BUILDUP,
    BULL_OPENING_DRIVE,
    BULL_PULLBACK_RECLAIM,
    BEAR_FAILED_BREAKOUT,
    BEAR_VWAP_RETEST_FAILURE,
)
RETIRED_PLAYBOOKS = REJECTED_PLAYBOOKS

MAX_EXTENSION_ATR = 1.25
TRADE_SCORE_MIN = 70.0
WATCH_SCORE_MIN = 60.0
OPENING_DRIVE_END = dt.time(10, 45)


def _finite(value) -> bool:
    try:
        return value is not None and bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def _f(value, default=None):
    return float(value) if _finite(value) else default


def _consensus(values: Iterable) -> float | None:
    clean = [float(v) for v in values if _finite(v)]
    return round(float(median(clean)), 2) if clean else None


def _clock(now) -> dt.time | None:
    if isinstance(now, dt.datetime):
        return now.time()
    if isinstance(now, dt.time):
        return now
    if isinstance(now, str):
        try:
            return dt.time.fromisoformat(now.split("T")[-1].split("+")[0])
        except ValueError:
            return None
    return None


def _directional_clv(row: dict, side: str) -> float | None:
    cp = _f(row.get("close_position_pct"))
    if cp is None:
        hi, lo, close = (_f(row.get(k)) for k in ("high", "low", "close"))
        if None in (hi, lo, close) or hi <= lo:
            return None
        cp = np.clip((close - lo) / (hi - lo) * 100.0, 0.0, 100.0)
    return round(cp if side == "Bullish" else 100.0 - cp, 2)


def _extension(row: dict) -> float | None:
    for key in ("breakout_extension_atr", "retained_breakout_extension_atr", "failed_breakout_extension_atr"):
        value = _f(row.get(key))
        if value is not None:
            return value
    return None


def _not_chased(row: dict) -> bool:
    ext = _extension(row)
    return ext is None or ext <= MAX_EXTENSION_ATR


def _play(playbook, side, score, reasons, *, modes=("intraday",), historical_status="BACKTESTABLE", eligible=True):
    score = round(float(score), 2) if _finite(score) else None
    if eligible and score is not None and score >= TRADE_SCORE_MIN:
        state = "TRADE CANDIDATE"
    elif score is not None and score >= WATCH_SCORE_MIN:
        state = "WATCH"
    else:
        state = "NO EDGE"
    return {
        "playbook": playbook,
        "side": side,
        "score": score,
        "state": state,
        "eligible": state == "TRADE CANDIDATE",
        "modes": list(modes),
        "historical_status": historical_status,
        "reasons": [str(x) for x in reasons if x][:5],
    }


_CATALYST_GROUPS = {
    "Results/Guidance": ("results", "earnings", "guidance", "profit", "revenue", "ebitda", "margin"),
    "Order/Contract": ("wins order", "order worth", "contract", "purchase order", "letter of award", "loa"),
    "Regulatory": ("approval", "approved", "fda", "usfda", "dgca", "sebi", "rbi", "regulatory"),
    "M&A/Strategic": ("acquisition", "merger", "stake sale", "strategic investment", "joint venture"),
    "Capital/Block": ("qip", "placement", "block deal", "preferential", "fund raise", "fundraise"),
    "Rating": ("rating upgrade", "upgraded", "rating action", "outlook revised"),
}


def score_real_catalyst(articles, *, now=None) -> dict:
    """Grade genuinely matched live headlines; never fabricate historical news.

    The score only classifies event importance/freshness.  Direction still comes
    from price/participation.  Unknown generic headlines remain low confidence.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    best = {"score": 0.0, "category": None, "headline": None, "published_at": None}
    for article in articles or []:
        title = str(article.get("title") or "").strip()
        low = title.lower()
        category = None
        base = 45.0
        for name, words in _CATALYST_GROUPS.items():
            if any(w in low for w in words):
                category = name
                base = 85.0
                break
        sent = _f(article.get("sentiment_score"))
        if sent is not None:
            base += max(-10.0, min(10.0, sent * 10.0))
        published = article.get("published_at")
        age_hours = None
        if published:
            try:
                pdt = dt.datetime.fromisoformat(str(published).replace("Z", "+00:00"))
                ref = now
                if pdt.tzinfo is not None and getattr(ref, "tzinfo", None) is None:
                    ref = ref.replace(tzinfo=pdt.tzinfo)
                elif pdt.tzinfo is None and getattr(ref, "tzinfo", None) is not None:
                    pdt = pdt.replace(tzinfo=ref.tzinfo)
                age_hours = max(0.0, (ref - pdt).total_seconds() / 3600.0)
                if age_hours <= 6:
                    base += 10.0
                elif age_hours > 24:
                    base -= 20.0
            except (ValueError, TypeError):
                pass
        score = float(np.clip(base, 0.0, 100.0))
        if score > best["score"]:
            best = {
                "score": round(score, 2), "category": category or "General News",
                "headline": title or None, "published_at": published, "age_hours": age_hours,
            }
    return best


def evaluate_row(row: dict, *, now=None, news_articles=None) -> list[dict]:
    """Return every V9 playbook currently matched by one point-in-time row."""
    r = dict(row or {})
    plays = []
    clock = _clock(now)
    direction = r.get("breakout_direction") or r.get("retained_breakout_direction") or r.get("v8_direction")
    source = r.get("breakout_source") or r.get("retained_breakout_source")
    part = _f(r.get("v8_participation"))
    relative = _f(r.get("v8_relative"))
    deriv = _f(r.get("v8_derivatives"))
    structure = _f(r.get("v8_structure"))
    bull_clv = _directional_clv(r, "Bullish")
    bear_clv = _directional_clv(r, "Bearish")
    not_chased = _not_chased(r)

    # V9.1 Bull Institutional Accumulation: new committed long positioning,
    # not a generic upside breakout. The historical replay creates a probe only
    # after price + OI are both positive and price is accepted above VWAP; the
    # cross-sectional ranks below decide whether that activity is exceptional.
    basis = _f(r.get("basis_acceleration"))
    accumulation_basis_ok = basis is None or basis >= -0.02
    price_60 = _f(r.get("price_chg_60m_pct"))
    oi_60 = _f(r.get("oi_chg_60m_pct"))
    tod = _f(r.get("tod_rvol"))
    accumulation_seed = bool(r.get("v91_accumulation_probe")) or bool(
        price_60 is not None and price_60 > 0
        and oi_60 is not None and oi_60 > 0
        and tod is not None and tod >= 1.0
    )
    bull_above_vwap = r.get("bull_above_vwap")
    if bull_above_vwap is None:
        bull_above_vwap = r.get("vwap_side_agrees")
    if (accumulation_seed and r.get("v8_oi_state") == "Long Buildup"
            and bull_above_vwap is True and accumulation_basis_ok):
        score = _consensus([part, relative, deriv, bull_clv])
        quality = bool(
            part is not None and part >= 70
            and relative is not None and relative >= 70
            and deriv is not None and deriv >= 65
            and bull_clv is not None and bull_clv >= 60
        )
        plays.append(_play(
            BULL_INSTITUTIONAL_ACCUMULATION, "Bullish", score,
            ["Price up + OI up", "Long buildup", "Above VWAP",
             "Relative leadership", "Abnormal participation"],
            modes=("intraday", "swing"), eligible=quality,
        ))

    # 1) Early-session bullish opening drive.
    if direction == "Bullish" and source == "Opening Range" and bool(r.get("fresh_breakout")):
        in_window = clock is None or clock <= OPENING_DRIVE_END
        score = _consensus([part, relative, bull_clv, structure])
        quality = bool(in_window and not_chased and part is not None and part >= 70 and relative is not None and relative >= 60 and bull_clv is not None and bull_clv >= 70)
        if in_window:
            plays.append(_play(BULL_OPENING_DRIVE, "Bullish", score,
                               ["Opening-range escape", "Abnormal opening participation", "Relative leadership", "Close accepted near high"],
                               modes=("intraday",), eligible=quality))

    # 2) Bull breakout pullback/reclaim; confirmation must already be known.
    ret_dir = r.get("retained_breakout_direction")
    ret_source = r.get("retained_breakout_source") or source
    retest = bool(r.get("breakout_retest_confirmed") or r.get("retest_confirmed"))
    if ret_dir == "Bullish" and ret_source == "Recent Range" and retest:
        score = _consensus([part, relative, bull_clv, deriv])
        quality = bool(not_chased and r.get("vwap_side_agrees") is not False and part is not None and part >= 55 and relative is not None and relative >= 60 and bull_clv is not None and bull_clv >= 55)
        plays.append(_play(BULL_PULLBACK_RECLAIM, "Bullish", score,
                           ["Confirmed pullback/reclaim", "Held breakout level", "Above/near VWAP", "Relative strength retained"],
                           modes=("intraday", "swing"), eligible=quality))

    # 3) Real catalyst continuation is live/shadow only until event history exists.
    catalyst = score_real_catalyst(news_articles, now=now) if news_articles else {"score": 0.0}
    if direction == "Bullish" and catalyst.get("score", 0) >= 70:
        score = _consensus([catalyst.get("score"), part, relative, bull_clv])
        quality = bool(not_chased and part is not None and part >= 70 and relative is not None and relative >= 60)
        plays.append(_play(BULL_CATALYST_CONTINUATION, "Bullish", score,
                           [catalyst.get("category"), catalyst.get("headline"), "Real catalyst + price confirmation", "Institutional participation"],
                           modes=("intraday", "swing"), historical_status="LIVE_SHADOW", eligible=quality))

    # 4) Bear fresh short buildup: explicitly new short positioning, not long unwinding.
    if direction == "Bearish" and bool(r.get("fresh_breakout")) and r.get("v8_oi_state") == "Fresh Short Buildup":
        basis = _f(r.get("basis_acceleration"))
        score = _consensus([part, relative, deriv, bear_clv])
        basis_ok = basis is None or basis <= 0.02
        quality = bool(not_chased and basis_ok and part is not None and part >= 70 and relative is not None and relative >= 60 and deriv is not None and deriv >= 65 and bear_clv is not None and bear_clv >= 65)
        plays.append(_play(BEAR_FRESH_SHORT_BUILDUP, "Bearish", score,
                           ["Price down + OI up", "Fresh short buildup", "Relative weakness", "Selling participation", "Basis not improving"],
                           modes=("intraday", "swing"), eligible=quality))

    # 5) A bullish breakout that fails back through its level becomes a bearish play only after failure confirmation.
    if r.get("failed_breakout_direction") == "Bearish":
        score = _consensus([part, relative, bear_clv, 100.0 if r.get("failed_breakout_vwap_reject") else 50.0])
        quality = bool(not_chased and bool(r.get("failed_breakout_vwap_reject")) and part is not None and part >= 60 and relative is not None and relative >= 60 and bear_clv is not None and bear_clv >= 60)
        plays.append(_play(BEAR_FAILED_BREAKOUT, "Bearish", score,
                           ["Prior bullish breakout failed", "Back inside decision range", "VWAP rejection", "Bearish close acceptance"],
                           modes=("intraday", "swing"), eligible=quality))

    # 6) Bear retest failure: probe of breakdown level/VWAP followed by close back below.
    if ret_dir == "Bearish" and retest:
        score = _consensus([part, relative, deriv, bear_clv])
        quality = bool(not_chased and r.get("vwap_side_agrees") is not False and part is not None and part >= 60 and relative is not None and relative >= 60 and bear_clv is not None and bear_clv >= 60)
        plays.append(_play(BEAR_VWAP_RETEST_FAILURE, "Bearish", score,
                           ["Bearish retest failed", "Breakdown level rejected", "VWAP/price acceptance bearish", "Relative weakness retained"],
                           modes=("intraday", "swing"), eligible=quality))

    if not not_chased:
        for p in plays:
            if p["state"] == "TRADE CANDIDATE":
                p["state"] = "WATCH"
                p["eligible"] = False
            p["reasons"] = (p.get("reasons") or [])[:4] + [f"Chase risk {_extension(r):.2f} ATR"]
    return plays


def best_play(rows: Iterable[dict], *, side: str, mode: str) -> list[dict]:
    """Rank live rows by their best eligible V9 playbook for one side/mode."""
    out = []
    for row in rows or []:
        matches = [p for p in (row.get("v9_playbooks") or []) if p.get("playbook") in ACTIVE_PLAYBOOKS and p.get("side") == side and mode in (p.get("modes") or []) and p.get("state") in ("TRADE CANDIDATE", "WATCH")]
        if not matches:
            continue
        matches.sort(key=lambda p: (1 if p.get("state") == "TRADE CANDIDATE" else 0, float(p.get("score") or 0)), reverse=True)
        item = dict(row)
        item["v9_best_playbook"] = matches[0]["playbook"]
        item["v9_score"] = matches[0].get("score")
        item["v9_state"] = matches[0].get("state")
        item["v9_reasons"] = matches[0].get("reasons") or []
        out.append(item)
    out.sort(key=lambda r: (1 if r.get("v9_state") == "TRADE CANDIDATE" else 0, float(r.get("v9_score") or 0)), reverse=True)
    return out


def _json_number(value):
    return round(float(value), 3) if _finite(value) else None


def _compact_dashboard_row(row: dict, *, mode: str) -> dict:
    prefix = f"v9_{mode}_"
    option_prefix = "option_swing_" if mode == "swing" else "option_"
    side = row.get("v8_direction") or row.get("failed_breakout_direction")
    def opt(name):
        return row.get(option_prefix + name)
    return {
        "symbol": row.get("symbol"),
        "direction": side,
        "playbook": row.get(prefix + "playbook"),
        "score": _json_number(row.get(prefix + "score")),
        "state": row.get(prefix + "state") or "NO EDGE",
        "reasons": [str(x) for x in (row.get(prefix + "reasons") or [])[:5]],
        "participation": _json_number(row.get("v8_participation")),
        "relative": _json_number(row.get("v8_relative")),
        "derivatives": _json_number(row.get("v8_derivatives")),
        "structure": _json_number(row.get("v8_structure")),
        "oi_state": row.get("v8_oi_state") or "OI Neutral/Unavailable",
        "extension_atr": _json_number(_extension(row)),
        "close": _json_number(row.get("close")),
        "tod_rvol": _json_number(row.get("tod_rvol")),
        "option_action": opt("action"),
        "option_edge": opt("edge"),
        "option_buyer_score": _json_number(opt("buyer_score")),
        "option_contract": opt("contract"),
        "option_iv_rv_ratio": _json_number(opt("iv_rv_ratio")),
        "option_spread_pct": _json_number(opt("spread_pct")),
        "option_dte": opt("dte"),
        "option_straddle_move_pct": _json_number(opt("straddle_move_pct")),
    }



def update_symbol_scan_health(previous: dict | None, results: Iterable[dict], scan_ts: str) -> dict:
    """Track per-symbol scan health without losing the last known good scan.

    A current error updates error metadata but deliberately preserves the prior
    ``last_success`` timestamp. A successful row clears current error metadata
    and advances ``last_success``. The structure is JSON-safe so it can be
    persisted alongside the normal Railway scan state.
    """
    health = {str(k): dict(v or {}) for k, v in (previous or {}).items()}
    for row in results or []:
        symbol = row.get("symbol")
        if not symbol:
            continue
        symbol = str(symbol)
        item = dict(health.get(symbol) or {})
        if row.get("error"):
            item.update({
                "last_error": scan_ts,
                "error_stage": row.get("error_stage") or "scan",
                "error": str(row.get("error")),
            })
            item.setdefault("last_success", None)
        else:
            item.update({
                "last_success": scan_ts,
                "last_error": None,
                "error_stage": None,
                "error": None,
            })
        health[symbol] = item
    return health


def scan_failure_details(results: Iterable[dict], symbol_health: dict | None = None) -> list[dict]:
    """Return compact diagnostics for symbols that failed the current scan."""
    health = symbol_health or {}
    failures = []
    for row in results or []:
        if not row.get("error"):
            continue
        symbol = str(row.get("symbol") or "UNKNOWN")
        item = health.get(symbol) or {}
        failures.append({
            "symbol": symbol,
            "stage": row.get("error_stage") or item.get("error_stage") or "scan",
            "error": str(row.get("error") or item.get("error") or "unknown error"),
            "last_success": item.get("last_success"),
        })
    failures.sort(key=lambda x: x["symbol"])
    return failures

def scan_health_counts(results: Iterable[dict]) -> dict:
    """Return attempted/valid/error counts for the live scan surface."""
    all_rows = list(results or [])
    errors = sum(bool(r.get("error")) for r in all_rows)
    return {"attempted": len(all_rows), "valid": len(all_rows) - errors, "errors": errors}


def dashboard_payload(state: dict, *, limit: int = 6) -> dict:
    """JSON-safe live V9 professional-playbook decision console payload."""
    all_rows = list(state.get("results") or [])
    health = scan_health_counts(all_rows)
    rows = [r for r in all_rows if not r.get("error")]
    priority = {"TRADE CANDIDATE": 2, "WATCH": 1, "NO EDGE": 0}

    def side(mode, direction):
        prefix = f"v9_{mode}_"
        candidates = [r for r in rows if (r.get("v8_direction") or r.get("failed_breakout_direction")) == direction
                      and r.get(prefix + "playbook") in ACTIVE_PLAYBOOKS]
        candidates.sort(key=lambda r: (priority.get(r.get(prefix + "state"), 0), float(r.get(prefix + "score") or 0)), reverse=True)
        return [_compact_dashboard_row(r, mode=mode) for r in candidates[:max(0, int(limit))]]

    return {
        "build_id": V9_BUILD_ID,
        "last_scan": state.get("last_scan"),
        "last_error": state.get("last_error"),
        "market_open": bool(state.get("market_open")),
        "market": {
            "index_direction": state.get("index_direction"),
            "index_chg_pct": _json_number(state.get("index_chg_pct")),
        },
        "production_status": "NO VALIDATED PRODUCTION PLAYBOOK" if not ACTIVE_PLAYBOOKS else "PRODUCTION ACTIVE",
        "shadow_playbooks": list(SHADOW_PLAYBOOKS),
        "rejected_playbooks": list(REJECTED_PLAYBOOKS),
        "scan_failures": scan_failure_details(all_rows, state.get("scan_symbol_health") or {}),
        "counts": {
            "attempted": health["attempted"],
            "universe": health["valid"],
            "errors": health["errors"],
            "intraday_trade": sum(r.get("v9_intraday_playbook") in ACTIVE_PLAYBOOKS and r.get("v9_intraday_state") == "TRADE CANDIDATE" for r in rows),
            "intraday_watch": sum(r.get("v9_intraday_playbook") in ACTIVE_PLAYBOOKS and r.get("v9_intraday_state") == "WATCH" for r in rows),
            "swing_trade": sum(r.get("v9_swing_playbook") in ACTIVE_PLAYBOOKS and r.get("v9_swing_state") == "TRADE CANDIDATE" for r in rows),
            "swing_watch": sum(r.get("v9_swing_playbook") in ACTIVE_PLAYBOOKS and r.get("v9_swing_state") == "WATCH" for r in rows),
        },
        "intraday": {"bullish": side("intraday", "Bullish"), "bearish": side("intraday", "Bearish")},
        "swing": {"bullish": side("swing", "Bullish"), "bearish": side("swing", "Bearish")},
    }
