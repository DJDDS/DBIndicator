"""Confluence Watch (card kept at its usual dashboard slot; internal logic rebuilt 14 Oct 2026) - a sector
leaderboard with stock-level OI bias layered on top: today's F&O industries/sectors are ranked by relative
strength (same median-vs-market logic the hierarchy desk uses), the strongest and weakest are kept, and every
F&O stock in one of those sectors that currently carries a directional OI read (/oi-screener's oi_structure
field) gets a row - whether or not its own OI agrees with its sector's trend. Agreement is marked, not
filtered out, so a stock bucking a hot-or-cold sector stays visible (worth knowing, even if lower conviction).

v1 (June 2026) tried to anchor on active chart Patterns and was mostly empty most of the day, since patterns
only scan once, at 15:45 IST (chart_patterns.AUTO_SLOTS). v2 (14 Oct 2026, same day) anchored on single-stock
OI confirmed by its own sector, which fixed the emptiness but only surfaced "agreeing" names and dropped
sector context the member actually wanted to see directly - which sectors are leading/lagging today, not just
whether one stock's OI happens to confirm its sector. v3 (also 14 Oct 2026, same day, member asked for this
directly) flips to the sector-leaderboard framing: show today's best- and worst-performing sectors, then every
OI-biased stock inside them, agreement flagged rather than required. Chart-pattern agreement is still a bonus
third signal when /patterns' last (once-daily) run has one, same as v2.

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

MAX_ROWS = 30
ACTIVE_PATTERN_STATUS = {"BREAKOUT", "RETEST_HOLD", "EXTENDED"}
_OI_BULL = {"Long Buildup", "Short Covering"}
_OI_BEAR = {"Short Buildup", "Long Unwinding"}
MIN_INDUSTRY_GROUP = 4   # same floor v135_hierarchy uses before trusting an industry median
TOP_N_SECTORS = 4        # how many of today's strongest AND weakest sectors to keep (up to 2x this many)


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


def _leaderboard_sectors(industry_reads, market_med):
    """Rank sectors by today's median return and keep the top/bottom TOP_N_SECTORS (deduped if the universe
    is small enough that they'd overlap). Returns {industry: (trend, edge_pct)} where trend is GAINER/LOSER
    and edge_pct is that industry's median minus the whole market's median, signed."""
    if market_med is None or not industry_reads:
        return {}
    ranked = sorted(industry_reads.items(), key=lambda kv: kv[1], reverse=True)
    k = min(TOP_N_SECTORS, len(ranked))
    gainers = ranked[:k]
    gainer_names = {name for name, _ in gainers}
    losers = [pair for pair in ranked[-k:] if pair[0] not in gainer_names]
    selected = {}
    for name, med in gainers:
        selected[name] = ("GAINER", round(med - market_med, 3))
    for name, med in losers:
        selected[name] = ("LOSER", round(med - market_med, 3))
    return selected


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
    sectors = _leaderboard_sectors(industry_reads, market_med)
    if not sectors:
        return []

    out = []
    for row in results or []:
        sym = row.get("symbol")
        structure = row.get("oi_structure")
        if not sym or structure not in _OI_BULL | _OI_BEAR:
            continue   # only stocks carrying a directional OI read get a row

        industry = ind_map.get(sym)
        if not industry or industry not in sectors:
            continue   # only stocks inside today's top/bottom sectors

        trend, sector_edge = sectors[industry]
        pdir = 1 if structure in _OI_BULL else -1
        sector_dir = 1 if trend == "GAINER" else -1
        agree = pdir == sector_dir

        score = 2 if agree else 1
        pattern = None
        if agree:
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
            "industry": industry, "sector_trend": trend, "industry_edge_pct": sector_edge,
            "agree": agree, "pattern": pattern, "score": score,
        })

    out.sort(key=lambda r: (-r["score"], -abs(r["industry_edge_pct"]),
                             -(abs(r["price_chg_today_pct"]) if r["price_chg_today_pct"] is not None else 0)))
    return out[:MAX_ROWS]


def watch(results) -> list:
    """Member-facing read: today's strongest and weakest F&O sectors, with every directionally-OI-biased
    stock inside them, flagged for whether that OI agrees with its sector's trend (and bonus-scored higher
    when an active chart pattern also agrees). Cached (see CACHE_TTL). On a transient failure, keeps serving
    the last good value rather than flashing empty."""
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
