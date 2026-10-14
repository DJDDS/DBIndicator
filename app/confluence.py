"""Confluence Watch - a new, synthesized view correlating three things that already exist as separate
pages: chart Patterns (/patterns), the OI Screener's live positioning read (/oi-screener, the oi_structure
field on every scan row), and Sector Analysis's industry relative strength (built from the same industry
map the V4.7 hierarchy desk already uses). None of the three is new data; what's new is checking whether
they agree.

Anchored on active chart patterns (status BREAKOUT / RETEST_HOLD / EXTENDED - a pattern still in FORMING
has nothing yet to confirm or contradict). For each one, two independent reads are checked against the
pattern's own direction:
  - OI positioning: Long Buildup / Short Covering agree with a bullish pattern; Short Buildup / Long
    Unwinding agree with a bearish one (classic F&O positioning labels, already computed once per stock
    in the live scan).
  - Industry relative strength: is the stock's industry (>=4 F&O members needed, same floor the hierarchy
    desk uses) beating or trailing the whole F&O market's today-return by more than a small threshold, in
    the pattern's direction.
A row only surfaces here when at least one of those two agrees with the pattern (agree >= 2, counting the
pattern itself) - a technical setup nobody else is confirming stays on /patterns, not here.

Paper / spotting only. Stock + direction only - no option instruction, no execution advice.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

_CACHE = {'at': 0.0, 'value': []}
CACHE_TTL = 20.0   # seconds - this reads the whole patterns results file (with NSE-evidence enrichment)
                    # plus the live scan's whole results list on every refresh; the dashboard polls every
                    # 8s, so, learning directly from the research-ticker slowdown, this is cached from day one.

ACTIVE_STATUS = {"BREAKOUT", "RETEST_HOLD", "EXTENDED"}
_OI_BULL = {"Long Buildup", "Short Covering"}
_OI_BEAR = {"Short Buildup", "Long Unwinding"}
INDUSTRY_THRESHOLD_PCT = 0.15   # an industry must beat/trail the F&O market median today-return by more
                                 # than this many points, in the pattern's direction, to count as agreeing
MIN_INDUSTRY_GROUP = 4          # same floor v135_hierarchy uses before trusting an industry median


def _median(vals):
    vals = sorted(vals)
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _oi_lookup(results):
    """symbol -> its current oi_structure label, straight off the live scan row."""
    out = {}
    for row in results or []:
        sym = row.get("symbol")
        if sym:
            out[sym] = row.get("oi_structure")
    return out


def _industry_reads(results, ind_map):
    """industry -> median today-return of its F&O members (>=MIN_INDUSTRY_GROUP needed), and the whole
    F&O market's median today-return, both from price_chg_today_pct on the live scan rows."""
    by_ind, market_vals = {}, []
    for row in results or []:
        sym = row.get("symbol")
        chg = row.get("price_chg_today_pct")
        if sym is None or chg is None:
            continue
        try:
            chg = float(chg)
        except (TypeError, ValueError):
            continue
        market_vals.append(chg)
        industry = ind_map.get(sym)
        if industry:
            by_ind.setdefault(industry, []).append(chg)
    market_med = _median(market_vals)
    reads = {ind: _median(vals) for ind, vals in by_ind.items() if len(vals) >= MIN_INDUSTRY_GROUP}
    return reads, market_med


def _refresh(results) -> list:
    from . import chart_patterns, v135_hierarchy

    ind_map = v135_hierarchy.industry_map()
    pattern_rows = chart_patterns.public_rows(chart_patterns.load_results())
    oi_by_symbol = _oi_lookup(results)
    industry_reads, market_med = _industry_reads(results, ind_map)

    out = []
    for row in pattern_rows:
        status = row.get("status")
        direction = row.get("direction")           # "BULL" / "BEAR"
        if status not in ACTIVE_STATUS or direction not in ("BULL", "BEAR"):
            continue
        pdir = 1 if direction == "BULL" else -1
        sym = row.get("symbol")

        signals = [f"Pattern: {row.get('pattern')} ({status.replace('_', ' ').title()})"]
        agree = 1

        structure = oi_by_symbol.get(sym)
        if (pdir > 0 and structure in _OI_BULL) or (pdir < 0 and structure in _OI_BEAR):
            agree += 1
            signals.append(f"OI: {structure}")

        industry = ind_map.get(sym)
        industry_edge = None
        if industry and market_med is not None and industry in industry_reads:
            industry_edge = round(industry_reads[industry] - market_med, 3)
            if pdir * industry_edge > INDUSTRY_THRESHOLD_PCT:
                agree += 1
                signals.append(f"Sector: {industry} {'leading' if pdir > 0 else 'lagging'} market "
                                f"({industry_edge:+.2f}pp)")

        if agree < 2:
            continue

        out.append({
            "symbol": sym, "direction": "UP" if pdir > 0 else "DOWN",
            "pattern": row.get("pattern"), "status": status,
            "tier": row.get("tier"), "tier_rank": row.get("tier_rank", 3),
            "industry": industry, "oi_structure": structure, "industry_edge_pct": industry_edge,
            "agree": agree, "signals": signals,
        })

    out.sort(key=lambda r: (-r["agree"], r.get("tier_rank", 3)))
    return out


def watch(results) -> list:
    """Member-facing read: patterns confirmed by OI positioning and/or sector strength. Cached (see
    CACHE_TTL). On a transient failure, keeps serving the last good value rather than flashing empty."""
    now = time.time()
    if now - _CACHE['at'] < CACHE_TTL:
        return _CACHE['value']
    try:
        value = _refresh(results)
    except Exception:  # noqa: BLE001
        log.exception("confluence watch failed")
        value = _CACHE['value']
    _CACHE.update(at=now, value=value)
    return value
