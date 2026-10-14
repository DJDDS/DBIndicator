"""Confluence Watch - correlates three things that already exist as separate pages: the OI Screener's live
positioning read (/oi-screener, the oi_structure field on every live scan row), Sector Analysis's industry
relative strength (built from the same industry map the V4.7 hierarchy desk already uses), and chart
Patterns (/patterns). None of the three is new data; what's new is checking whether they agree.

Rebuilt 14 Oct 2026 after member feedback that v1 (anchored on active chart patterns) was mostly empty and
the wrong synthesis. The chart-pattern scan only runs once a day, at 15:45 IST after the close
(chart_patterns.AUTO_SLOTS), so for most of the trading day that list is yesterday's and often small -
anchoring a "live" dashboard card on it meant the card sat empty for hours. OI positioning and sector/
industry relative strength are both live, computed fresh every scan, so v2 anchors there instead:

  1. OI positioning gives the directional read: Long Buildup / Short Covering = bullish, Short Buildup /
     Long Unwinding = bearish (classic F&O positioning labels, already on every live scan row).
  2. Sector confirmation: is the stock's industry (>=4 F&O members needed, same floor the hierarchy desk
     uses) beating or trailing the whole F&O market's today-return by more than a small threshold, in OI's
     direction. A row only exists once this agrees - OI alone is noisy; OI without any sector backing stays
     off this list.
  3. Chart-pattern confirmation is a bonus third signal, layered on top when /patterns' last scan has an
     active pattern (BREAKOUT / RETEST_HOLD / EXTENDED) on the same stock in the same direction. It raises
     the score from 2 to 3 but is never required, since it only refreshes once a day.

Paper / spotting only. Stock + direction only - no option instruction, no execution advice.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

_CACHE = {'at': 0.0, 'value': []}
CACHE_TTL = 20.0    # seconds - reads the live scan's whole results list plus the patterns results file on
                     # every refresh; the dashboard polls every 8s, so this is cached from day one (the
                     # lesson from the earlier research-ticker slowdown).

MAX_ROWS = 20
ACTIVE_PATTERN_STATUS = {"BREAKOUT", "RETEST_HOLD", "EXTENDED"}
_OI_BULL = {"Long Buildup", "Short Covering"}
_OI_BEAR = {"Short Buildup", "Long Unwinding"}
INDUSTRY_THRESHOLD_PCT = 0.15   # an industry must beat/trail the F&O market median today-return by more
                                 # than this many points, in OI's direction, to count as confirming
MIN_INDUSTRY_GROUP = 4          # same floor v135_hierarchy uses before trusting an industry median


def _median(vals):
    vals = sorted(vals)
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


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


def _active_pattern_lookup(pattern_rows):
    """symbol -> its best active pattern row (lowest tier_rank wins), status in ACTIVE_PATTERN_STATUS."""
    best = {}
    for row in pattern_rows:
        if row.get("status") not in ACTIVE_PATTERN_STATUS or row.get("direction") not in ("BULL", "BEAR"):
            continue
        sym = row.get("symbol")
        if not sym:
            continue
        cur = best.get(sym)
        if cur is None or row.get("tier_rank", 3) < cur.get("tier_rank", 3):
            best[sym] = row
    return best


def _refresh(results) -> list:
    from . import chart_patterns, v135_hierarchy

    ind_map = v135_hierarchy.industry_map()
    pattern_by_symbol = _active_pattern_lookup(chart_patterns.public_rows(chart_patterns.load_results()))
    industry_reads, market_med = _industry_reads(results, ind_map)

    out = []
    for row in results or []:
        sym = row.get("symbol")
        structure = row.get("oi_structure")
        if not sym or structure not in _OI_BULL | _OI_BEAR:
            continue
        pdir = 1 if structure in _OI_BULL else -1

        industry = ind_map.get(sym)
        if not industry or industry not in industry_reads or market_med is None:
            continue
        industry_edge = round(industry_reads[industry] - market_med, 3)
        if pdir * industry_edge <= INDUSTRY_THRESHOLD_PCT:
            continue   # OI alone, with no sector backing, doesn't make the list

        score = 2
        pattern = None
        p = pattern_by_symbol.get(sym)
        if p and ((pdir > 0 and p["direction"] == "BULL") or (pdir < 0 and p["direction"] == "BEAR")):
            score = 3
            pattern = {"name": p.get("pattern"), "status": p.get("status"), "tier": p.get("tier")}

        chg = row.get("price_chg_today_pct")
        try:
            chg = round(float(chg), 2) if chg is not None else None
        except (TypeError, ValueError):
            chg = None

        out.append({
            "symbol": sym, "direction": "UP" if pdir > 0 else "DOWN",
            "price_chg_today_pct": chg, "oi_structure": structure,
            "industry": industry, "industry_edge_pct": industry_edge,
            "pattern": pattern, "score": score,
        })

    out.sort(key=lambda r: (-r["score"], -abs(r["industry_edge_pct"]),
                             -(abs(r["price_chg_today_pct"]) if r["price_chg_today_pct"] is not None else 0)))
    return out[:MAX_ROWS]


def watch(results) -> list:
    """Member-facing read: live OI positioning confirmed by sector/industry strength, with chart-pattern
    confirmation layered on as a bonus third signal when available. Cached (see CACHE_TTL). On a transient
    failure, keeps serving the last good value rather than flashing empty."""
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
