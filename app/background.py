"""
A small background thread that re-runs the scan every SCAN_INTERVAL_SECONDS
during market hours, so the web page always has a recent result to show
without the visitor having to trigger anything themselves.
"""
import datetime as dt
import json
import logging
import os
import threading
import time

from . import alerts, delivery, early_signal, early_movement, stock_in_play, v6_edge, v8_dual, v9_playbooks, derivative_intelligence, kite_auth, scanner, news, oi_view, opportunity_forward, research_runtime, v94_magnitude, v12_live, config
from .config import (
    settings, SCAN_RESULTS_FILE, PARAM_WEIGHTS_FILE, WATCHLIST_TIMEFRAME,
)
from .scanner import (
    scan_watchlist, is_market_open, now_ist, compute_oi_acceleration,
    classify_oi_structure, fetch_index_direction, fetch_sector_directions, fetch_sector_contexts,
    SYMBOL_SECTOR_MAP,
)

log = logging.getLogger(__name__)

LIVE_RELIABILITY_BUILD_ID = "2026-09-04-INSTITUTIONAL-V10.2.2-LIVE-RELIABILITY-HOTFIX"

# How far back (in minutes) to keep OI samples per symbol.
# compute_oi_acceleration needs up to 120 minutes of history (its
# "prior 60-minute" window looks 60-120 minutes back), so this keeps a
# safety margin beyond that regardless of the configured scan interval
# - unlike a fixed sample-count cap, this stays correct whether scans
# run every 60s or every 5 minutes.
OI_HISTORY_MAX_MINUTES = 150

# Rolling near-futures basis samples for V6 sponsorship acceleration.
_v6_basis_history = {}

# --------------------------------------------------------------------------
# The screener's fixed 4-parameter confluence check: RSI (vs its
# smoothing line), MACD (vs signal line), EMA9 (vs Bollinger mid), and
# Relative Volume (vs its own 20-bar average, threshold configurable on
# the Settings page). indicators.compute_signal already does the real
# work of counting how many of these 4 agree with a row's direction -
# that count comes back as `aligned` (0-4). SCREEN_PARAM_DEFS below is
# kept purely as display labels (footnotes, tooltips) - it's not used
# for any matching logic anymore, so there's a single source of truth
# for "how many parameters agree" instead of two systems that could
# quietly disagree with each other.
# --------------------------------------------------------------------------

SCREEN_PARAM_DEFS = [
    {"id": "rsi_state", "label": "RSI (vs its smoothing line)"},
    {"id": "macd_state", "label": "MACD (vs signal line)"},
    {"id": "cmf", "label": "Chaikin Money Flow (directional volume)"},
    {"id": "rel_volume", "label": "Relative Volume (vs 20-bar avg, threshold on Settings page)"},
]


# --------------------------------------------------------------------------
# The early-signal layer.
#
# This is the change the whole rewrite turns on. Previously the technical
# screen and the OI panel were two separate surfaces that never met: the
# screen decided signal_confirmed from four price/volume indicators, and OI
# was computed afterwards, displayed in its own table, and consumed by
# nothing. The single field that combined them - positional_qualified - was
# assigned once and read zero times anywhere in the codebase.
#
# Now OI runs BEFORE the gates and can veto a row. A name reaches the
# shortlist only when the price read and the positioning read agree, which
# is what "link OI with the parameter-pass stocks" actually means in code.
# It is also the main reason the shortlist is short: two independent
# witnesses have to say the same thing, and most days most stocks cannot
# manage that.
# --------------------------------------------------------------------------

def _apply_early_signal(results, oi_history, index_ret_20=None, index_ret_10=None,
                        intraday=False):
    """Attach the early-signal score and its OI reading to every row.

    Everything here degrades to None rather than to a guess. A symbol with
    no OI baseline gets oi_z=None, which makes its OI component unmeasured,
    which lowers its coverage - and if coverage falls below the floor the
    row is ineligible rather than being ranked on the components that
    happen to be present. Missing data can disqualify a row here. It can
    never flatter one."""
    for r in results:
        r["oi_z"] = None
        r["oi_chg_pct_daily"] = None
        r["oi_accel_ratio"] = None
        r["oi_structure_early"] = None
        r["oi_agrees"] = None
        r["rs_pct"] = None
        r["rs_improving"] = None
        r["rs_acceleration"] = None
        r["early_score"] = None
        r["early_band"] = None
        r["early_band_note"] = None
        r["early_parts"] = None
        r["early_coverage"] = None
        r["early_eligible"] = False
        if r.get("error"):
            continue

        direction = r.get("direction")
        hist = (oi_history or {}).get(r.get("symbol"))
        # r["oi"] is the LIVE reading from this scan's batched quote() call
        # (see scanner.fetch_oi_map). The history is a once-a-day fetch, so
        # without splicing the live value in, every scan would re-score the
        # morning's frozen snapshot - see early_signal._with_live.
        live_oi = r.get("oi")
        oi_z, oi_chg, _sigma = early_signal.oi_zscore(hist, intraday=intraday, latest_oi=live_oi)
        r["oi_z"] = oi_z
        r["oi_chg_pct_daily"] = oi_chg
        r["oi_accel_ratio"] = early_signal.oi_acceleration_ratio(
            hist, intraday=intraday, latest_oi=live_oi)

        # Price change for the quadrant is close-vs-previous-close on the
        # SAME daily bar the OI figure belongs to - not an intraday
        # since-first-scan drift, which is what made the old quadrant flip
        # every time price crossed its own baseline.
        price_chg = None
        close_v, prev_v = r.get("close"), r.get("prev_close")
        if close_v and prev_v:
            price_chg = (close_v / prev_v - 1.0) * 100.0
        structure = early_signal.classify_oi_structure(price_chg, oi_chg, oi_z=oi_z)
        r["oi_structure_early"] = structure

        oi_dir = early_signal.oi_direction(structure)
        r["oi_agrees"] = None if oi_dir is None else (oi_dir == direction)

        # Relative strength: this stock's return minus the index's over the
        # same window. The old four-vote screen had no market-relative axis
        # at all, which is why it lit up across the board on a day the whole
        # market rallied - every stock looks strong when measured only
        # against itself.
        if index_ret_20 is not None and r.get("ret_20") is not None:
            rs20 = r["ret_20"] - index_ret_20
            r["rs_pct"] = round(rs20, 2)
            if index_ret_10 is not None and r.get("ret_10") is not None:
                rs10 = r["ret_10"] - index_ret_10
                r["rs_improving"] = bool(rs10 > 0)
                r["rs_acceleration"] = round(rs10 - rs20, 2)

        scored = early_signal.early_signal_score(
            direction,
            oi_z=oi_z, oi_structure=structure,
            rvol=r.get("vol_multiple"), rvol_accel=r.get("rvol_accel"),
            vol_rising=r.get("vol_rising"),
            rsi_cross=r.get("rsi_cross"), rsi_above=r.get("rsi_above"),
            macd_agrees=r.get("macd_agrees"),
            close_pos=r.get("close_position_pct"),
            big_candle_agrees=r.get("big_candle_agrees"),
            coiling=r.get("vol_contracting"), nr7=r.get("nr7"),
            entry_extension_atr=r.get("entry_extension_atr"),
            rs_pct=r.get("rs_pct"), rs_improving=r.get("rs_improving"),
        )
        r["early_score"] = scored["score"]
        band = early_signal.score_band(scored["score"], scored.get("coverage"))
        r["early_band"] = band[0] if band else None
        r["early_band_note"] = band[1] if band else None
        r["early_parts"] = scored["parts"]
        r["early_coverage"] = scored["coverage"]
        r["early_eligible"] = scored["eligible"]


def _apply_oi_gate(results):
    """When REQUIRE_OI_AGREEMENT is on, a row whose OI positioning does not
    back its direction loses signal_confirmed.

    When OI is configured as mandatory, only an explicit True counts as
    confirmation. False is active disagreement and None is unmeasured or
    neutral; neither is strong enough evidence for a Best Entry."""
    if not settings.REQUIRE_OI_AGREEMENT:
        return
    for r in results:
        if r.get("error"):
            continue
        if r.get("signal_confirmed") and r.get("oi_agrees") is not True:
            # If OI is configured as mandatory, unknown/neutral OI cannot be
            # treated as confirmation. The previous asymmetry made the live
            # gate looser than its name and made research coverage misleading.
            r["signal_confirmed"] = False


def _apply_early_movement_shortlist(results):
    """Rank only fresh 15-minute F&O movement candidates.

    This is intentionally independent of the legacy signal_confirmed/4-vote
    state.  It consumes positioning, time-of-day participation, relative
    strength, fresh trigger and entry location directly.
    """
    eligible = []
    radar = []
    for r in results:
        r["shortlist_rank"] = None
        r["radar_rank"] = None
        r["movement_stage"] = None
        r["movement_score"] = None
        r["movement_coverage"] = None
        r["movement_parts"] = None
        r["movement_blockers"] = []
        if r.get("error"):
            continue
        # Live recent OI direction: total OI increasing while the latest
        # 15-minute price bar moves in the candidate's direction.  This is
        # the early, rollover-resistant confirmation; the near-contract
        # z-score remains a separate anomaly-strength input.
        oi60 = r.get("oi_chg_60m_pct")
        close_v, prev_v = r.get("close"), r.get("prev_close")
        recent_agrees = None
        if oi60 is not None and close_v and prev_v:
            px = (close_v / prev_v - 1.0) * 100.0
            if r.get("direction") == "Bullish":
                recent_agrees = bool(oi60 > 0 and px > 0)
            elif r.get("direction") == "Bearish":
                recent_agrees = bool(oi60 > 0 and px < 0)
        r["oi_recent_agrees"] = recent_agrees

        scored = early_movement.score_candidate(r)
        r["movement_score"] = scored["score"]
        r["movement_coverage"] = scored["coverage"]
        r["movement_parts"] = scored["parts"]
        r["movement_blockers"] = scored["blockers"]
        r["movement_stage"] = scored.get("stage")
        if scored.get("stage") in ("Energy Building", "Ignition"):
            radar.append(r)
        if scored["eligible"]:
            eligible.append(r)

    radar.sort(key=lambda r: (
        1 if r.get("movement_stage") == "Ignition" else 0,
        r.get("movement_score") or -1,
        r.get("compression_score") or -1,
        r.get("oi_chg_60m_pct") if r.get("oi_chg_60m_pct") is not None else -999,
    ), reverse=True)
    for n, r in enumerate(radar[:8], start=1):
        r["radar_rank"] = n

    eligible.sort(key=lambda r: (
        r.get("movement_score") or -1,
        -(r.get("entry_trigger_bars_ago") if r.get("entry_trigger_bars_ago") is not None else 99),
        r.get("oi_chg_60m_pct") if r.get("oi_chg_60m_pct") is not None else -999,
        r.get("tod_rvol") if r.get("tod_rvol") is not None else -999,
    ), reverse=True)
    for n, r in enumerate(eligible[: settings.SHORTLIST_MAX], start=1):
        r["shortlist_rank"] = n
    return eligible[: settings.SHORTLIST_MAX]


def _apply_stock_in_play_shortlists(results):
    """Rank actual 15-minute breakout candidates for intraday and 1–2D swing.

    Legacy indicator alignment is intentionally ignored here.  Direction comes
    from ``breakout_direction`` and sponsorship from TOD volume/OI/context.
    """
    intraday, swing, radar = [], [], []
    for r in results:
        r["shortlist_rank"] = None
        r["swing_rank"] = None
        r["radar_rank"] = None
        r["movement_stage"] = None
        r["movement_score"] = None
        r["movement_blockers"] = []
        r["oi_status"] = None
        if r.get("error"):
            continue

        bdir = r.get("breakout_direction") or r.get("retained_breakout_direction")
        if not r.get("breakout_direction") and r.get("retained_breakout_direction"):
            if r.get("retained_breakout_source") is not None:
                r["breakout_source"] = r.get("retained_breakout_source")
            if r.get("retained_breakout_level") is not None:
                r["breakout_level"] = r.get("retained_breakout_level")
            if r.get("retained_breakout_extension_atr") is not None:
                r["breakout_extension_atr"] = r.get("retained_breakout_extension_atr")
        # Re-evaluate contextual agreement against the breakout direction, not
        # the old RSI/MACD/CMF majority direction.
        if bdir:
            r["trade_direction"] = bdir
            sector_dir = r.get("sector_direction")
            r["sector_agrees"] = None if sector_dir is None else (sector_dir == bdir)
            htf_dir = r.get("htf_direction")
            r["htf_agrees"] = None if htf_dir is None else (htf_dir == bdir)
            r["vwap_side_agrees"] = r.get("breakout_vwap_agrees")
            r["vwap_distance_atr"] = r.get("breakout_vwap_distance_atr")
            r["entry_is_extended"] = r.get("breakout_entry_extended")

            oi60 = r.get("oi_chg_60m_pct")
            px_now, px_prev = r.get("close"), r.get("prev_close")
            if oi60 is not None and px_now and px_prev:
                px = (px_now / px_prev - 1.0) * 100.0
                r["oi_recent_agrees"] = bool(oi60 > 0 and ((bdir == "Bullish" and px > 0) or (bdir == "Bearish" and px < 0)))
            else:
                r["oi_recent_agrees"] = None

        if not bdir:
            r["trade_direction"] = None
        classified = stock_in_play.classify_live_candidate(r)
        r["movement_stage"] = classified.get("stage")
        r["movement_score"] = classified.get("score")
        r["movement_blockers"] = classified.get("blockers", [])
        r["oi_status"] = classified.get("oi_status")
        r["intraday_eligible"] = classified.get("intraday_eligible", False)
        r["swing_eligible"] = classified.get("swing_eligible", False)
        r["edge_priority"] = classified.get("edge_priority", 0)
        r["retest_confirmed"] = classified.get("retest_confirmed", False)
        if classified.get("stage") in (
            "Energy Building", "Stock in Play", "Ignition",
            "Recent-Range Breakout", "Sponsored Recent-Range",
        ):
            radar.append(r)
        if r["intraday_eligible"]:
            intraday.append(r)
        if r["swing_eligible"]:
            swing.append(r)

    radar.sort(key=lambda r: (
        r.get("edge_priority") if r.get("edge_priority") is not None else 0,
        r.get("movement_score") if r.get("movement_score") is not None else -1,
        r.get("tod_rvol") if r.get("tod_rvol") is not None else -1,
        r.get("compression_score") if r.get("compression_score") is not None else -1,
    ), reverse=True)
    for n, r in enumerate(radar[:10], 1):
        r["radar_rank"] = n

    intraday.sort(key=lambda r: (
        r.get("edge_priority") if r.get("edge_priority") is not None else 0,
        r.get("movement_score") if r.get("movement_score") is not None else -1,
        r.get("tod_rvol") if r.get("tod_rvol") is not None else -1,
        r.get("oi_chg_60m_pct") if r.get("oi_chg_60m_pct") is not None else -999,
    ), reverse=True)
    intraday = intraday[: settings.SHORTLIST_MAX]
    for n, r in enumerate(intraday, 1):
        r["shortlist_rank"] = n

    swing.sort(key=lambda r: (
        r.get("edge_priority") if r.get("edge_priority") is not None else 0,
        r.get("movement_score") if r.get("movement_score") is not None else -1,
        r.get("oi_chg_60m_pct") if r.get("oi_chg_60m_pct") is not None else -999,
        r.get("tod_rvol") if r.get("tod_rvol") is not None else -1,
    ), reverse=True)
    swing = swing[: settings.SHORTLIST_MAX]
    for n, r in enumerate(swing, 1):
        r["swing_rank"] = n
    return intraday, swing




def _apply_v6_cross_sectional_context(results, *, index_chg_pct=None, breadth=None, sector_contexts=None):
    """Attach V6 cross-sectional participation, leadership and regime fields.

    These are *ranking* features, not universal vetoes.  The important
    difference from the legacy screener is that a stock is compared with the
    rest of the current F&O universe and with its own sector rather than only
    with fixed absolute thresholds.
    """
    import pandas as pd

    rows = [r for r in (results or []) if not r.get("error")]
    if not rows:
        return results

    turnover = pd.Series({
        r.get("symbol"): (
            float(r.get("close")) * float(r.get("volume"))
            if r.get("close") not in (None, 0) and r.get("volume") is not None
            else float("nan")
        )
        for r in rows
    }, dtype="float64")
    turn_rank = v6_edge.percentile_rank(turnover)

    sector_contexts = sector_contexts or {}
    sector_changes = pd.Series({
        sector: ctx.get("chg_pct") if isinstance(ctx, dict) else None
        for sector, ctx in sector_contexts.items()
    }, dtype="float64")
    sector_ranks = v6_edge.percentile_rank(sector_changes) if not sector_changes.empty else pd.Series(dtype="float64")

    chgs = []
    for r in rows:
        c, p = r.get("close"), r.get("prev_close")
        if c not in (None, 0) and p not in (None, 0):
            try:
                chgs.append((float(c) / float(p) - 1.0) * 100.0)
            except (TypeError, ValueError, ZeroDivisionError):
                pass
    dispersion = float(pd.Series(chgs, dtype="float64").std(ddof=0)) if chgs else 0.0
    breadth = breadth or {}
    regime = v6_edge.classify_market_regime(
        index_chg_pct=index_chg_pct,
        bullish_pct=breadth.get("bullish_pct"),
        bearish_pct=breadth.get("bearish_pct"),
        dispersion_pct=dispersion,
    )

    for r in rows:
        symbol = r.get("symbol")
        tp = turn_rank.get(symbol) if symbol in turn_rank.index else None
        r["turnover_percentile"] = round(float(tp), 1) if tp is not None and pd.notna(tp) else None
        r["market_regime"] = regime
        r["market_dispersion_pct"] = round(dispersion, 3)

        sector = r.get("sector") or SYMBOL_SECTOR_MAP.get(symbol)
        r["sector"] = sector
        ctx = sector_contexts.get(sector) if sector else None
        if isinstance(ctx, dict):
            if ctx.get("direction") is not None:
                r["sector_direction"] = ctx.get("direction")
            sr = sector_ranks.get(sector) if sector in sector_ranks.index else None
            r["sector_rank_percentile"] = round(float(sr), 1) if sr is not None and pd.notna(sr) else None
            c, p = r.get("close"), r.get("prev_close")
            try:
                stock_chg = (float(c) / float(p) - 1.0) * 100.0 if c not in (None, 0) and p not in (None, 0) else None
            except (TypeError, ValueError, ZeroDivisionError):
                stock_chg = None
            sec_chg = ctx.get("chg_pct")
            r["stock_sector_lead_pct"] = (
                round(float(stock_chg) - float(sec_chg), 3)
                if stock_chg is not None and sec_chg is not None else None
            )
        else:
            r["sector_rank_percentile"] = None
            r["stock_sector_lead_pct"] = None

        direction = r.get("breakout_direction") or r.get("retained_breakout_direction") or r.get("direction")
        loc = v6_edge.price_location_score(
            direction=direction if direction in ("Bullish", "Bearish") else "Bullish",
            close=r.get("close"), high20=r.get("prior_high_20d"), low20=r.get("prior_low_20d"),
            high50=r.get("prior_high_50d"), low50=r.get("prior_low_50d"),
        )
        r["price_location_score"] = loc.get("score")
        r["price_position_20d_pct"] = loc.get("position_20d_pct")
        r["price_position_50d_pct"] = loc.get("position_50d_pct")
        r["near_20d_high"] = loc.get("near_20d_high")
        r["near_20d_low"] = loc.get("near_20d_low")
        r["catalyst_score"] = v6_edge.catalyst_proxy_score(
            gap_atr=r.get("gap_atr"), opening_rvol=r.get("opening_rvol"),
            tod_rvol=r.get("tod_rvol"), bar_range_atr=r.get("bar_range_atr"),
            turnover_percentile=r.get("turnover_percentile"),
        )
    return results


def _apply_v8_dual_alpha(results, now=None):
    """Attach V8 Bull/Bear cross-sectional alpha fields in place.

    V8 reads the same live NSE F&O cross-section for both sides, but interprets
    relative performance and derivatives directionally.  OI is evidence, never
    a veto.  `ret_4` is the exact 60-minute price return on the 15-minute live
    engine and is exposed explicitly for the four-quadrant OI state.
    """
    rows = [r for r in (results or []) if not r.get("error")]
    if not rows:
        return results
    for r in rows:
        if r.get("price_chg_60m_pct") is None:
            if r.get("ret_4") is not None:
                r["price_chg_60m_pct"] = r.get("ret_4")
            else:
                # Conservative fallback for old/scarce rows.  It is labelled as
                # unavailable to V8 if even the session return cannot be formed.
                c, p = r.get("close"), r.get("prev_close")
                try:
                    r["price_chg_60m_pct"] = round((float(c) / float(p) - 1.0) * 100.0, 3) if c and p else None
                except (TypeError, ValueError, ZeroDivisionError):
                    r["price_chg_60m_pct"] = None
        # V9.1 accumulation does not require a breakout. Seed Bullish direction
        # only when new long positioning is already visible in raw point-in-time
        # facts; the later cross-sectional ranks still decide whether it is good.
        try:
            p60 = float(r.get("price_chg_60m_pct")) if r.get("price_chg_60m_pct") is not None else None
            oi60 = float(r.get("oi_chg_60m_pct")) if r.get("oi_chg_60m_pct") is not None else None
            tod = float(r.get("tod_rvol")) if r.get("tod_rvol") is not None else None
        except (TypeError, ValueError):
            p60 = oi60 = tod = None
        if (not (r.get("breakout_direction") or r.get("retained_breakout_direction") or r.get("direction"))
                and p60 is not None and p60 > 0 and oi60 is not None and oi60 > 0
                and tod is not None and tod >= 1.0
                and (r.get("bull_above_vwap") if r.get("bull_above_vwap") is not None else r.get("vwap_side_agrees")) is True):
            r["v91_accumulation_seed_direction"] = "Bullish"
    ranked = v8_dual.rank_cross_section(rows)
    clock = now if now is not None else now_ist()
    for original, scored in zip(rows, ranked):
        for key, value in scored.items():
            if key.startswith("v8_") or key.startswith("v81_"):
                original[key] = value
        direction = original.get("v8_direction")
        if direction in ("Bullish", "Bearish"):
            decision_score = original.get("v8_alpha") if direction == "Bullish" else original.get("v81_bear_pressure")
            swing = v8_dual.classify_swing_opportunity(
                original, direction=direction, alpha=decision_score,
                participation=original.get("v8_participation"),
                derivatives=original.get("v8_derivatives"), now_time=clock,
            )
            original["v8_swing_alpha"] = swing.get("alpha")
            original["v8_swing_state"] = swing.get("state")
            original["v8_swing_eligible"] = swing.get("eligible")
            original["v8_swing_day_location"] = swing.get("day_location")
            original["v8_swing_persistence"] = swing.get("persistence")
            original["v8_swing_late_session"] = swing.get("late_session")
    return results





def _refresh_v9_catalyst_news(results):
    """Spend the throttled Marketaux budget only on bullish stocks already in play."""
    candidates = [
        r for r in (results or [])
        if not r.get("error") and r.get("v8_direction") == "Bullish"
        and (r.get("v8_participation") is not None and float(r.get("v8_participation")) >= 70.0)
    ]
    candidates.sort(key=lambda r: float(r.get("v8_participation") or 0), reverse=True)
    symbols = [r.get("symbol") for r in candidates[:10] if r.get("symbol")]
    if symbols:
        try:
            news.fetch_news_for_symbols(symbols)
        except Exception:  # noqa: BLE001 - catalyst data can never stop scanning
            log.exception("V9 catalyst-news refresh failed")
    return symbols


def _apply_v9_playbooks(results, now=None):
    """Attach V9 professional playbooks and cap live focus to Top-3 per side.

    V8 component ranks remain useful evidence inputs, but V9—not V8.1 Top-K—
    decides the operational setup. Real catalyst evidence comes only from the
    live news cache; no price-volume proxy is relabelled as a real catalyst.
    """
    clock = now or now_ist()
    rows = [r for r in (results or []) if not r.get("error")]
    for r in rows:
        articles = news.get_news_for_symbol(r.get("symbol"), limit=3) if r.get("symbol") else []
        plays = v9_playbooks.evaluate_row(r, now=clock, news_articles=articles)
        r["v9_playbooks"] = plays
        for mode in ("intraday", "swing"):
            matches = [p for p in plays if p.get("playbook") in v9_playbooks.ACTIVE_PLAYBOOKS and mode in (p.get("modes") or []) and p.get("state") in ("TRADE CANDIDATE", "WATCH")]
            matches.sort(key=lambda p: (1 if p.get("state") == "TRADE CANDIDATE" else 0, float(p.get("score") or 0)), reverse=True)
            best = matches[0] if matches else {}
            prefix = f"v9_{mode}_"
            r[prefix + "playbook"] = best.get("playbook")
            r[prefix + "score"] = best.get("score")
            r[prefix + "state"] = best.get("state") or "NO EDGE"
            r[prefix + "reasons"] = best.get("reasons") or []

    # Operational focus is at most three Bull and three Bear names per horizon.
    # This is a display/risk-cap, not a searched score threshold.
    for mode in ("intraday", "swing"):
        for side in ("Bullish", "Bearish"):
            candidates = [r for r in rows if (r.get("v8_direction") == side or r.get("failed_breakout_direction") == side)
                          and r.get(f"v9_{mode}_state") == "TRADE CANDIDATE"]
            candidates.sort(key=lambda r: float(r.get(f"v9_{mode}_score") or -1), reverse=True)
            keep = {id(r) for r in candidates[:3]}
            for r in candidates[3:]:
                r[f"v9_{mode}_state"] = "WATCH"
                reasons = list(r.get(f"v9_{mode}_reasons") or [])
                reasons.append("Outside current Top-3 focus")
                r[f"v9_{mode}_reasons"] = reasons[:5]
    return results


def _apply_shadow_early_radar(results):
    """Attach research-only early-stage ranks without touching production fields.

    This makes Energy Building / Ignition observable while ACTIVE_PLAYBOOKS is
    empty. It cannot create ``radar_rank``, TRADE/WATCH, alerts, or eligibility.
    """
    ranked = []
    for r in results or []:
        r["shadow_radar_rank"] = None
        r["shadow_movement_stage"] = None
        r["shadow_movement_score"] = None
        r["shadow_movement_blockers"] = []
        if r.get("error"):
            continue
        probe = dict(r)
        direction = (
            probe.get("breakout_direction") or probe.get("retained_breakout_direction")
            or probe.get("trade_direction") or probe.get("direction")
        )
        if direction in ("Bullish", "Bearish"):
            probe["direction"] = direction
            probe["trade_direction"] = direction
            if probe.get("oi_recent_agrees") is None:
                oi60 = probe.get("oi_chg_60m_pct")
                px_now, px_prev = probe.get("close"), probe.get("prev_close")
                if oi60 is not None and px_now and px_prev:
                    px_move = (float(px_now) / float(px_prev) - 1.0) * 100.0
                    probe["oi_recent_agrees"] = bool(
                        float(oi60) > 0 and ((direction == "Bullish" and px_move > 0) or (direction == "Bearish" and px_move < 0))
                    )
        scored = early_movement.score_candidate(probe)
        stage = scored.get("stage")
        if stage not in ("Energy Building", "Ignition", "Best Entry"):
            continue
        r["shadow_movement_stage"] = stage
        r["shadow_movement_score"] = scored.get("score")
        r["shadow_movement_blockers"] = scored.get("blockers") or []
        ranked.append(r)
    priority = {"Best Entry": 3, "Ignition": 2, "Energy Building": 1}
    ranked.sort(key=lambda r: (
        priority.get(r.get("shadow_movement_stage"), 0),
        r.get("shadow_movement_score") if r.get("shadow_movement_score") is not None else -1,
        r.get("compression_score") if r.get("compression_score") is not None else -1,
        r.get("tod_rvol") if r.get("tod_rvol") is not None else -1,
    ), reverse=True)
    for i, r in enumerate(ranked[:8], 1):
        r["shadow_radar_rank"] = i
    return ranked[:8]


def _apply_v9_operational_shortlists(results):
    """Project V9 playbook decisions onto shortlist fields used by UI/alerts."""
    intraday, swing, radar = [], [], []
    for r in results or []:
        r["shortlist_rank"] = None
        r["swing_rank"] = None
        r["radar_rank"] = None
        r["intraday_eligible"] = False
        r["swing_eligible"] = False
        if r.get("error"):
            continue
        side = r.get("v8_direction") or r.get("failed_breakout_direction")
        if side not in ("Bullish", "Bearish"):
            continue
        r["trade_direction"] = side
        istate = r.get("v9_intraday_state") or "NO EDGE"
        sstate = r.get("v9_swing_state") or "NO EDGE"
        score = r.get("v9_intraday_score")
        r["movement_score"] = score
        r["movement_stage"] = f"V9.1 {r.get('v9_intraday_playbook')}" if r.get("v9_intraday_playbook") else "V9.1 No Playbook"
        if istate in ("TRADE CANDIDATE", "WATCH"):
            radar.append(r)
        if istate == "TRADE CANDIDATE":
            r["intraday_eligible"] = True
            intraday.append(r)
        if sstate == "TRADE CANDIDATE":
            r["swing_eligible"] = True
            swing.append(r)

    intraday.sort(key=lambda r: float(r.get("v9_intraday_score") or -1), reverse=True)
    swing.sort(key=lambda r: float(r.get("v9_swing_score") or -1), reverse=True)
    radar.sort(key=lambda r: (1 if r.get("v9_intraday_state") == "TRADE CANDIDATE" else 0,
                              float(r.get("v9_intraday_score") or -1)), reverse=True)
    for i, r in enumerate(intraday, 1):
        r["shortlist_rank"] = i
    for i, r in enumerate(swing, 1):
        r["swing_rank"] = i
    for i, r in enumerate(radar[:10], 1):
        r["radar_rank"] = i
    return intraday, swing


def _apply_derivative_intelligence(kite, results, now=None):
    """Attach live option-expression evidence to the strongest V8.1 names.

    Option data never changes the underlying Bull/Bear rank.  It answers the
    second question: whether the shortlisted underlying is sensibly expressed
    through a liquid option at current IV/spread, or should be left as an
    underlying-only/watch idea.
    """
    clock = now or now_ist()
    try:
        derivative_intelligence.resolve_shadow_outcomes(kite, now=clock)
    except Exception:  # noqa: BLE001 - forward validation must never stop stock scan
        log.exception("Derivative shadow outcome resolution failed")
    try:
        return derivative_intelligence.enrich_shortlisted_options(
            kite, results, now=clock, max_candidates=6
        )
    except Exception:  # noqa: BLE001 - option layer must never stop stock scan
        log.exception("Derivative intelligence enrichment failed")
        return results


def _apply_v81_operational_shortlists(results):
    """Project V8.1 decisions onto the legacy shortlist fields used by alerts/UI.

    This is the production bridge: no V6 classification is consulted. Bull and
    Bear TRADE CANDIDATE states come from the V8.1 point-in-time Top-3 engine.
    """
    intraday, swing, radar = [], [], []
    for r in results or []:
        r["shortlist_rank"] = None
        r["swing_rank"] = None
        r["radar_rank"] = None
        r["intraday_eligible"] = False
        r["swing_eligible"] = False
        if r.get("error"):
            continue
        direction = r.get("v8_direction")
        if direction not in ("Bullish", "Bearish"):
            continue
        r["trade_direction"] = direction
        decision = r.get("v8_decision_score")
        r["movement_score"] = decision
        r["movement_stage"] = (
            "V8.1 Bull Top-3" if direction == "Bullish"
            else "V8.1 Bear Pressure Top-3"
        ) if r.get("v8_state") == "TRADE CANDIDATE" else "V8.1 Watch"
        if r.get("v8_state") in ("TRADE CANDIDATE", "WATCH"):
            radar.append(r)
        if r.get("v8_state") == "TRADE CANDIDATE":
            r["intraday_eligible"] = True
            intraday.append(r)
        if r.get("v8_swing_state") == "TRADE CANDIDATE":
            r["swing_eligible"] = True
            swing.append(r)

    intraday.sort(key=lambda r: float(r.get("v8_decision_score") or -1), reverse=True)
    swing.sort(key=lambda r: float(r.get("v8_swing_alpha") or -1), reverse=True)
    radar.sort(key=lambda r: (
        1 if r.get("v8_state") == "TRADE CANDIDATE" else 0,
        float(r.get("v8_decision_score") or -1),
    ), reverse=True)
    for i, r in enumerate(intraday, 1):
        r["shortlist_rank"] = i
    for i, r in enumerate(swing, 1):
        r["swing_rank"] = i
    for i, r in enumerate(radar[:10], 1):
        r["radar_rank"] = i
    return intraday, swing

def _apply_v6_basis(results, *, history=None, now=None):
    """Attach live near-futures basis and ~30-minute basis acceleration.

    Basis is deliberately independent of OI.  V6 allows an expanding futures
    premium/discount plus real volume to sponsor a breakout even when OI is
    unavailable or disagrees, subject to the other quality checks.
    """
    import pandas as pd

    if history is None:
        history = {}
    now = pd.Timestamp(now if now is not None else now_ist())
    for r in results or []:
        r["basis_pct"] = None
        r["basis_acceleration"] = None
        if r.get("error"):
            continue
        spot, fut = r.get("close"), r.get("fut_price_near")
        try:
            if spot is None or fut is None or float(spot) == 0:
                continue
            basis = (float(fut) / float(spot) - 1.0) * 100.0
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        r["basis_pct"] = round(basis, 4)
        sym = r.get("symbol")
        samples = history.setdefault(sym, []) if sym else []
        cutoff = now - pd.Timedelta(minutes=25)
        prior = None
        for sample in reversed(samples):
            try:
                ts = pd.Timestamp(sample.get("ts"))
                if ts.tzinfo is not None and now.tzinfo is None:
                    ts = ts.tz_localize(None)
                elif ts.tzinfo is None and now.tzinfo is not None:
                    ts = ts.tz_localize(now.tzinfo)
                if ts <= cutoff:
                    prior = sample.get("basis_pct")
                    break
            except Exception:
                continue
        if prior is not None:
            try:
                r["basis_acceleration"] = round(basis - float(prior), 4)
            except (TypeError, ValueError):
                pass
        if sym:
            samples.append({"ts": now.isoformat(), "basis_pct": basis})
            trim_before = now - pd.Timedelta(hours=3)
            kept = []
            for x in samples:
                try:
                    ts = pd.Timestamp(x.get("ts"))
                    if ts.tzinfo is not None and now.tzinfo is None:
                        ts = ts.tz_localize(None)
                    elif ts.tzinfo is None and now.tzinfo is not None:
                        ts = ts.tz_localize(now.tzinfo)
                    if ts >= trim_before:
                        kept.append(x)
                except Exception:
                    continue
            history[sym] = kept
    return results


def _enrich_v6_execution_5m(kite, results, *, max_candidates=5, signal_time=None):
    """Fetch 5-minute data only for the best bounded Recent-Range finalists."""
    candidates = [r for r in (results or []) if not r.get("error")
                  and (r.get("breakout_source") or r.get("retained_breakout_source")) == "Recent Range"
                  and (r.get("breakout_direction") or r.get("retained_breakout_direction")) in ("Bullish", "Bearish")]
    candidates.sort(key=lambda r: (
        r.get("v6_score") if r.get("v6_score") is not None else (r.get("movement_score") or -1),
        r.get("catalyst_score") if r.get("catalyst_score") is not None else -1,
        r.get("turnover_percentile") if r.get("turnover_percentile") is not None else -1,
    ), reverse=True)
    finalists = candidates[:max(0, int(max_candidates))]
    if not finalists:
        return results
    instruments = scanner._load_instrument_map(kite)
    for r in finalists:
        r["execution_5m_quality"] = None
        r["execution_5m_available"] = False
        token = instruments.get(r.get("symbol"))
        if not token:
            continue
        try:
            df = scanner.fetch_candles(kite, token, "5minute")
            q = v6_edge.five_minute_execution_quality(
                df,
                direction=r.get("breakout_direction") or r.get("retained_breakout_direction"),
                breakout_level=r.get("breakout_level") or r.get("retained_breakout_level"),
                atr=r.get("atr"),
                signal_time=signal_time or r.get("timestamp") or now_ist(),
            )
            r["execution_5m_available"] = q.get("available", False)
            r["execution_5m_quality"] = q.get("quality")
            r["execution_5m_retained"] = q.get("retained")
            r["execution_5m_retest"] = q.get("retest")
            r["execution_5m_volume_burst"] = q.get("volume_burst")
            r["execution_5m_extended"] = q.get("extended")
            r["execution_5m_extension_atr"] = q.get("extension_atr")
        except Exception as exc:  # noqa: BLE001 - one finalist cannot break the cycle
            log.debug("V6 5-minute enrichment failed for %s: %s", r.get("symbol"), exc)
    return results


def _apply_v6_shortlists(results):
    """Apply the V6 evidence model and return ranked intraday/swing lists.

    ``radar_rank`` remains broader than an executable entry: it keeps Stock in
    Play / Recent-Range setups visible while only evidence-rich names graduate
    to Intraday or Swing.
    """
    intraday, swing, radar = [], [], []
    for r in results or []:
        if r.get("error"):
            continue
        bdir = r.get("breakout_direction") or r.get("retained_breakout_direction")
        if bdir:
            r["direction"] = bdir
            r["trade_direction"] = bdir
            r["vwap_side_agrees"] = r.get("breakout_vwap_agrees", r.get("vwap_side_agrees"))
            r["entry_is_extended"] = r.get("breakout_entry_extended", r.get("entry_is_extended"))
            htf = r.get("htf_direction")
            r["htf_agrees"] = None if htf is None else (htf == bdir)
            sec = r.get("sector_direction")
            r["sector_agrees"] = None if sec is None else (sec == bdir)
        result = v6_edge.classify_v6_candidate(r)
        r["v6_score"] = result.get("score")
        r["movement_score"] = result.get("score")
        r["movement_stage"] = result.get("stage")
        r["movement_blockers"] = result.get("blockers", [])
        r["intraday_eligible"] = result.get("intraday_eligible", False)
        r["swing_eligible"] = result.get("swing_eligible", False)
        r["short_research_only"] = result.get("short_research_only", False)
        r["v6_sponsorship"] = result.get("sponsorship")
        r["edge_priority"] = result.get("edge_priority", 0)
        if r.get("movement_stage") in (
            "Energy Building", "Stock in Play", "Recent-Range Setup",
            "Sponsored Recent-Range", "V6 Intraday Entry", "V6 Swing 1-2D",
        ):
            radar.append(r)
        if r["intraday_eligible"]:
            intraday.append(r)
        if r["swing_eligible"]:
            swing.append(r)

    key = lambda r: (
        r.get("edge_priority") or 0,
        r.get("v6_score") if r.get("v6_score") is not None else -1,
        r.get("execution_5m_quality") if r.get("execution_5m_quality") is not None else -1,
        r.get("turnover_percentile") if r.get("turnover_percentile") is not None else -1,
    )
    radar.sort(key=key, reverse=True)
    for i, r in enumerate(radar[:10], 1):
        r["radar_rank"] = i
    intraday.sort(key=key, reverse=True)
    swing.sort(key=key, reverse=True)
    intraday = intraday[: settings.SHORTLIST_MAX]
    swing = swing[: settings.SHORTLIST_MAX]
    for i, r in enumerate(intraday, 1):
        r["shortlist_rank"] = i
    for i, r in enumerate(swing, 1):
        r["swing_rank"] = i
    return intraday, swing

def _apply_shortlist(results):
    """The single ranked output: `shortlist_rank`, 1 = best, None = not on it.

    This replaces the 2-of-4 / 3-of-4 / 4-of-4 tier sections, which were
    never a filter. `dir_match_count = max(n, 3 - n)` is never below 2 for
    n in 0..3, so EVERY symbol scored at least 2 and landed in some tier -
    the three lists between them partitioned the entire watchlist while
    looking like a funnel. That is the direct cause of "lots of options in
    2-to-3 and 3-to-4": there was no screen there to pass.

    A row reaches the shortlist only if it is signal_confirmed, has enough
    measured evidence to be scored at all, and clears the score floor. All
    three are real conditions, so on a quiet day this list is SHORT, and on
    a genuinely quiet day it is EMPTY - which is a finding, not a failure.
    A screener that always returns five names is not selecting; it is
    sorting."""
    floor = settings.MIN_EARLY_SCORE
    eligible = []
    for r in results:
        r["shortlist_rank"] = None
        if r.get("error") or not r.get("signal_confirmed"):
            continue
        if not r.get("early_eligible") or r.get("early_score") is None:
            continue
        # OI is the point. A row we have no OI baseline for can still
        # clear the coverage floor on volume, momentum and structure
        # alone - but those are the readings the old screen already had,
        # and the whole reason the old screen returned half the universe.
        # Without the independent witness, this is not a shortlist
        # candidate; it is just a stock that looks busy.
        if r.get("oi_z") is None:
            continue
        if r["early_score"] < floor:
            continue
        # Coverage is a SEPARATE bar from score, deliberately. The two fail
        # differently: a low score means the evidence disagrees, while low
        # coverage means there was not much evidence to disagree. A row can
        # score 82 on 60% coverage - confident about a smaller thing - and
        # the score alone cannot catch that.
        if (r.get("early_coverage") or 0) < settings.MIN_SHORTLIST_COVERAGE:
            continue
        # Best Entries must be timely, not merely a mature aligned state.
        # Require at least one RSI/MACD/CMF crossover in the current trade
        # direction within the last two bars.
        if r.get("entry_trigger") != r.get("direction"):
            continue
        bars_ago = r.get("entry_trigger_bars_ago")
        if bars_ago is None or bars_ago > 2:
            continue
        if r.get("entry_is_extended") is True:
            continue
        # Best Entries needs a measured latest-hour OI read. A fresh deploy
        # can therefore produce an empty list until the live history is long
        # enough; that is safer than treating unknown positioning as fresh.
        recent_60 = r.get("oi_chg_60m_pct")
        accel = r.get("oi_acceleration")
        # Best means verified now. If the service just restarted or has not
        # collected enough timestamped OI yet, wait instead of ranking a
        # candidate on stale/unknown positioning.
        if recent_60 is None or accel is None:
            continue
        if recent_60 <= 0:
            continue
        if accel < -0.30:
            continue
        eligible.append(r)

    # Ties broken by coverage: between two rows on the same score, prefer
    # the one backed by more measured evidence.
    eligible.sort(
        key=lambda r: (
            r["early_score"],
            -(r.get("entry_trigger_bars_ago") if r.get("entry_trigger_bars_ago") is not None else 99),
            r.get("oi_chg_60m_pct") if r.get("oi_chg_60m_pct") is not None else -999,
            abs(r.get("oi_z") or 0),
            r.get("early_coverage") or 0,
        ),
        reverse=True,
    )
    for n, r in enumerate(eligible[: settings.SHORTLIST_MAX], start=1):
        r["shortlist_rank"] = n
    return eligible[: settings.SHORTLIST_MAX]


def _apply_param_tier(results):
    """Mutates each result dict in place, attaching param_tier (2, 3,
    or 4 - the bucket this row belongs to, straight from
    indicators.compute_signal's `aligned`; None if it matched fewer
    than 2, has no signal at all, or is still inside the opening
    window). Each row lands in exactly ONE tier (its exact match
    count), not every tier it clears, so the dashboard's three tier
    sections never show the same stock twice."""
    for r in results:
        aligned = r.get("aligned")
        if r.get("error") or not r.get("direction") or r.get("in_opening_window") or aligned is None:
            r["param_tier"] = None
            continue
        r["param_tier"] = aligned if aligned >= 2 else None


def _apply_index_filter(results, index_direction):
    """Mutates each result dict in place, attaching index_agrees - does
    this row's own direction match NIFTY 50's current confluence
    direction on the same timeframe (see scanner.fetch_index_direction)?
    None means "no index reading available this scan" (a fetch hiccup,
    or the token hasn't resolved yet) - treated as agreeing, same
    convention as indicators.py's htf_agrees, so a transient index
    fetch failure can never silently filter out every row.

    When settings.REQUIRE_INDEX_AGREEMENT is on, a row that disagrees
    with the index also loses its signal_confirmed status - counter-
    trend trades have historically had a lower win rate, so this is an
    optional stricter gate layered on top, not a replacement for
    anything else. Off by default; index_agrees is always attached
    either way purely for display."""
    require = settings.REQUIRE_INDEX_AGREEMENT
    for r in results:
        if r.get("error") or not r.get("direction"):
            r["index_agrees"] = None
            continue
        r["index_agrees"] = True if index_direction is None else (r["direction"] == index_direction)
        if require and r.get("signal_confirmed") and not r["index_agrees"]:
            r["signal_confirmed"] = False


def _apply_candle_pattern_filter(results):
    """Mutates each result dict in place: when settings.
    REQUIRE_CANDLE_PATTERN_AGREEMENT is on, a row that already has
    candle_agrees=False (set by indicators.compute_signal via
    _compute_candle_pattern) also loses its signal_confirmed status -
    same shape as _apply_volume_flow_filter just above. Off by default;
    candle_pattern/candle_direction/candle_agrees are always attached by
    compute_signal either way, purely for display (the small candle
    badge next to the Signal column)."""
    if not settings.REQUIRE_CANDLE_PATTERN_AGREEMENT:
        return
    for r in results:
        if r.get("error"):
            continue
        if r.get("signal_confirmed") and r.get("candle_agrees") is False:
            r["signal_confirmed"] = False


def _apply_macd_hist_filter(results):
    """Mutates each result dict in place: when settings.
    REQUIRE_MACD_HIST_AGREEMENT is on, a row that already has
    macd_hist_agrees=False (set by indicators.compute_signal - is the
    MACD histogram growing in this row's own direction, i.e. momentum
    accelerating rather than fading) also loses its signal_confirmed
    status - same shape as _apply_volume_flow_filter/_apply_candle_
    pattern_filter above. Off by default; macd_hist/macd_hist_rising/
    macd_hist_agrees are always attached by compute_signal either way,
    purely for display."""
    if not settings.REQUIRE_MACD_HIST_AGREEMENT:
        return
    for r in results:
        if r.get("error"):
            continue
        if r.get("signal_confirmed") and r.get("macd_hist_agrees") is False:
            r["signal_confirmed"] = False


def _apply_big_candle_filter(results):
    """Mutates each result dict in place: when settings.
    REQUIRE_BIG_CANDLE_AGREEMENT is on, a row that already has
    big_candle_agrees=False (set by indicators.compute_signal - does the
    most recent qualifying range-expansion "big candle" within
    BIG_CANDLE_LOOKBACK bars agree with this row's own direction) also
    loses its signal_confirmed status - same shape as _apply_volume_flow_
    filter/_apply_candle_pattern_filter/_apply_macd_hist_filter above.
    Off by default; big_candle/big_candle_direction/big_candle_level/
    big_candle_recent_*/big_candle_continuation/big_candle_agrees are
    always attached by compute_signal either way, purely for display."""
    if not settings.REQUIRE_BIG_CANDLE_AGREEMENT:
        return
    for r in results:
        if r.get("error"):
            continue
        if r.get("signal_confirmed") and r.get("big_candle_agrees") is False:
            r["signal_confirmed"] = False


def _apply_strong_close_filter(results):
    """Mutates each result dict in place: when settings.
    REQUIRE_STRONG_CLOSE_AGREEMENT is on, a row that already has
    strong_close_agrees=False (set by indicators.compute_signal - did
    this bar's own close land in the extreme top/bottom
    STRONG_CLOSE_THRESHOLD_PCT% of its own high-low range, in this row's
    own direction) also loses its signal_confirmed status - a BTST-
    oriented "closed with real conviction" gate, same shape as every
    other filter here. Off by default; close_position_pct/strong_close_
    agrees are always attached by compute_signal either way, purely for
    display."""
    if not settings.REQUIRE_STRONG_CLOSE_AGREEMENT:
        return
    for r in results:
        if r.get("error"):
            continue
        if r.get("signal_confirmed") and r.get("strong_close_agrees") is False:
            r["signal_confirmed"] = False


def _apply_entry_location_filter(results):
    """Mutates each result dict in place: when settings.
    REQUIRE_ENTRY_LOCATION_AGREEMENT is on, a row that already has
    entry_location_agrees=False (set by indicators.compute_signal - price
    is already more than MAX_ENTRY_EXTENSION_ATR ATRs past its own VWAP
    in this row's own direction, i.e. the move is being CHASED rather
    than caught early) also loses its signal_confirmed status. Off by
    default; entry_extension_atr/entry_is_extended/entry_reference/
    entry_location_agrees are always attached by compute_signal either
    way, purely for display."""
    if not settings.REQUIRE_ENTRY_LOCATION_AGREEMENT:
        return
    for r in results:
        if r.get("error"):
            continue
        if r.get("signal_confirmed") and r.get("entry_location_agrees") is False:
            r["signal_confirmed"] = False


def _apply_atr_floor_filter(results):
    """Mutates each result dict in place: when settings.REQUIRE_ATR_FLOOR
    is on, a row that already has atr_floor_agrees=False (set by
    indicators.compute_signal - this stock's ATR as a % of its own price
    is below settings.MIN_ATR_PCT, i.e. it isn't currently moving enough
    to plausibly deliver a big move regardless of how many parameters
    agree) also loses its signal_confirmed status. Off by default;
    atr_pct/atr_floor_agrees are always attached by compute_signal either
    way, purely for display."""
    if not settings.REQUIRE_ATR_FLOOR:
        return
    for r in results:
        if r.get("error"):
            continue
        if r.get("signal_confirmed") and r.get("atr_floor_agrees") is False:
            r["signal_confirmed"] = False


def _apply_delivery_filter(results):
    """Mutates each result dict in place, attaching delivery_pct/
    delivery_date/delivery_agrees from app/delivery.py's cache (see that
    module's docstring for the timing/reliability caveats - this is
    NEVER a same-day-live number, and the fetch can be blocked entirely
    depending on where this app is hosted). None (no delivery data
    available for this symbol yet) always reads delivery_agrees=True,
    same "never block on missing data" convention as every other gate.

    When settings.REQUIRE_DELIVERY_AGREEMENT is on, a row whose delivery
    reading is below settings.DELIVERY_THRESHOLD_PCT also loses its
    signal_confirmed status. Off by default; delivery_pct/delivery_date/
    delivery_agrees are always attached either way, purely for display.
    Does NOT call delivery.refresh_if_stale() itself - see _run_loop,
    which triggers that at most once per cycle so the multi-tf loop's own
    calls to this function never trigger a
    second, redundant network attempt."""
    require = settings.REQUIRE_DELIVERY_AGREEMENT
    threshold = settings.DELIVERY_THRESHOLD_PCT
    for r in results:
        symbol = r.get("symbol")
        if r.get("error") or not symbol:
            r["delivery_pct"] = None
            r["delivery_date"] = None
            r["delivery_agrees"] = None
            continue
        pct, date = delivery.get_delivery_pct(symbol)
        r["delivery_pct"] = pct
        r["delivery_date"] = date
        r["delivery_agrees"] = True if pct is None else (pct >= threshold)
        if require and r.get("signal_confirmed") and not r["delivery_agrees"]:
            r["signal_confirmed"] = False


def _apply_sector_filter(results, sector_directions):
    """Mutates each result dict in place, attaching sector (the NSE
    sectoral index this symbol maps to, or None if it isn't in
    scanner.SYMBOL_SECTOR_MAP), sector_direction (that index's own
    current confluence direction, from sector_directions - see
    scanner.fetch_sector_directions), and sector_agrees - does this
    row's own direction match its sector's? Same "None means agree"
    convention used everywhere else: a symbol with no sector mapping,
    or a sector whose fetch didn't resolve this cycle, always reads
    sector_agrees=True, never blocking anything on its own.

    When settings.REQUIRE_SECTOR_AGREEMENT is on, a row that disagrees
    with its own sector also loses its signal_confirmed status - same
    shape as _apply_index_filter, just keyed per-symbol by sector
    instead of one shared index-wide value. Off by default; sector/
    sector_direction/sector_agrees are always attached either way,
    purely for display (the small sector badge next to Signal)."""
    require = settings.REQUIRE_SECTOR_AGREEMENT
    for r in results:
        if r.get("error") or not r.get("direction"):
            r["sector"] = None
            r["sector_direction"] = None
            r["sector_agrees"] = None
            continue
        sector = SYMBOL_SECTOR_MAP.get(r.get("symbol"))
        r["sector"] = sector
        sector_direction = sector_directions.get(sector) if sector else None
        r["sector_direction"] = sector_direction
        r["sector_agrees"] = True if sector_direction is None else (r["direction"] == sector_direction)
        if require and r.get("signal_confirmed") and not r["sector_agrees"]:
            r["signal_confirmed"] = False


def _compute_breadth(results):
    """Advances/declines across the CURRENT watchlist's own scan results
    (not full-NSE breadth - Kite has no cheap all-market advance/decline
    endpoint, so this is a watchlist-scoped proxy computed for free from
    data this cycle already fetched, labelled as such wherever it's
    shown). Only rows with a clear, error-free direction count toward
    the total; rows with no signal at all are excluded rather than
    counted as neutral. Returns {"bullish": int, "bearish": int,
    "total": int, "bullish_pct": float|None, "bearish_pct": float|None}
    - the two _pct fields are None when total is 0 (e.g. every row
    errored), so callers never divide by zero."""
    bullish = sum(1 for r in results if not r.get("error") and r.get("direction") == "Bullish")
    bearish = sum(1 for r in results if not r.get("error") and r.get("direction") == "Bearish")
    total = bullish + bearish
    return {
        "bullish": bullish,
        "bearish": bearish,
        "total": total,
        "bullish_pct": round(bullish / total * 100, 1) if total else None,
        "bearish_pct": round(bearish / total * 100, 1) if total else None,
    }


def _apply_breadth_filter(results, breadth):
    """Mutates each result dict in place, attaching breadth_agrees - is
    at least settings.BREADTH_THRESHOLD_PCT of the CURRENT watchlist's
    resolved rows also pointing this row's own direction? None/empty
    breadth (no resolved rows this cycle) always reads breadth_agrees
    =True, same "never block on missing data" convention as every other
    gate here.

    When settings.REQUIRE_BREADTH_AGREEMENT is on, a row whose own
    direction is decisively against the watchlist's current advance/
    decline split also loses its signal_confirmed status - operationalizes
    NEXT_HORIZON_RESEARCH.md Finding 5's "don't fully trust a bullish
    breakout on a day the broader market is mostly declining" as a
    watchlist-scoped proxy. Off by default; breadth_agrees is always
    attached either way, purely for display."""
    require = settings.REQUIRE_BREADTH_AGREEMENT
    threshold = settings.BREADTH_THRESHOLD_PCT
    total = breadth.get("total") or 0
    for r in results:
        if r.get("error") or not r.get("direction"):
            r["breadth_agrees"] = None
            continue
        if total == 0:
            r["breadth_agrees"] = True
        elif r["direction"] == "Bullish":
            r["breadth_agrees"] = (breadth.get("bullish_pct") or 0) >= threshold
        else:
            r["breadth_agrees"] = (breadth.get("bearish_pct") or 0) >= threshold
        if require and r.get("signal_confirmed") and not r["breadth_agrees"]:
            r["signal_confirmed"] = False


# Equal-weight fallback for weighted_score below, until you've run
# "Auto-Weight Parameters" on the Backtest page at least once - matches
# the plain aligned/4 count in spirit (every parameter counts the same).
_DEFAULT_PARAM_WEIGHTS = {
    "rsi_cross": 0.25, "macd_cross": 0.25, "cmf_flow": 0.25, "rel_volume": 0.25,
}
_param_weights_cache = {"mtime": None, "weights": None}


def _load_param_weights():
    """Re-reads PARAM_WEIGHTS_FILE only when its mtime has changed since
    the last call (cheap: one stat() per scan cycle in the common case
    of nothing new). Falls back to equal weighting if the file doesn't
    exist yet (no "Auto-Weight Parameters" run so far) or is corrupt."""
    try:
        mtime = os.path.getmtime(PARAM_WEIGHTS_FILE)
    except OSError:
        return _DEFAULT_PARAM_WEIGHTS
    if _param_weights_cache["mtime"] != mtime:
        try:
            with open(PARAM_WEIGHTS_FILE) as f:
                data = json.load(f)
            weights = data.get("weights") or {}
            if weights:
                _param_weights_cache["weights"] = weights
                _param_weights_cache["mtime"] = mtime
        except (json.JSONDecodeError, OSError):
            pass
    return _param_weights_cache["weights"] or _DEFAULT_PARAM_WEIGHTS


def _apply_weighted_score(results):
    """Mutates each result dict in place, attaching weighted_score (0-100) -
    a backtest-informed alternative to the plain aligned/4 count. Rather
    than treating RSI/MACD/CMF/Relative Volume as equally weighted,
    this multiplies each one's current agreement with the row's
    direction by that parameter's own recent historical win rate (see
    backtest.compute_param_weights, run manually from the Backtest
    page's "Auto-Weight Parameters" panel and persisted to
    PARAM_WEIGHTS_FILE) - a parameter that's actually been predictive
    lately counts for more than one that hasn't. Purely an additional,
    informational sort/display field - doesn't replace aligned/
    param_tier/signal_confirmed anywhere, and falls back to equal
    25%-each weighting (identical in spirit to aligned/4) until you've
    run a weight computation at least once."""
    weights = _load_param_weights()
    for r in results:
        if r.get("error") or not r.get("direction"):
            r["weighted_score"] = None
            continue
        direction = r["direction"]
        score = 0.0
        if r.get("rsi_state") == direction:
            score += weights.get("rsi_cross", 0)
        if r.get("macd_state") == direction:
            score += weights.get("macd_cross", 0)
        if r.get("vol_flow_direction") == direction:
            score += weights.get("cmf_flow", 0)
        if r.get("vol_confirmed"):
            score += weights.get("rel_volume", 0)
        r["weighted_score"] = round(score * 100, 1)

# --------------------------------------------------------------------------


_state_lock = threading.Lock()
_state = {
    "results": [],
    "last_scan": None,
    "last_scan_attempt": None,
    "last_scan_attempt_status": None,
    "last_scan_attempt_error": None,
    "scan_status": "WAITING",
    "next_scan_due": None,
    "consecutive_scan_failures": 0,
    "last_fno_symbols": [],
    "last_fno_cash_tokens": {},
    "fno_universe_source": None,
    "last_error": None,
    "oi_history": {},
    "basis_history": {},
    "oi_day_baseline": {},
    "oi_structure_prev": {},
    "index_direction": None,
    "index_close": None,
    "index_chg_pct": None,
    "breadth": None,
    "market_regime": None,
    "scan_symbol_health": {},
    "opportunity_forward": opportunity_forward.empty_state(),
    "v12_trade_console": {"label": "V12 LIVE TRADE OPPORTUNITY CONSOLE", "validation_label": "NOT VALIDATED", "intraday": [], "swing": {"1D": [], "2D": []}, "counts": {}},
    "v12_option_recorder": {"status": "WAITING"},
    "v12_feasibility": {"status": "RECORDING — NO FEASIBILITY VERDICT", "trial25_locked": True},
    "v12_earnings": {"status": "EMPTY", "active_count": 0, "upcoming_7d": []},
    "v12_trial25_status": v12_live.TRIAL25_LOCKED_STATUS,
}

# Set by web.py whenever a Quick Settings / Settings change is applied
# (timeframe, indicator lengths, watchlist, etc.) so the very next scan
# picks up the new settings within a second or two, instead of the
# dashboard silently showing stale results for up to
# SCAN_INTERVAL_SECONDS (default 3 minutes) - which read as "the
# timeframe switch isn't working" even though it was actually just
# waiting for the next scheduled cycle.
_rescan_event = threading.Event()


def trigger_rescan():
    _rescan_event.set()


def _apply_oi_trend(results):
    """Mutates each result dict in place, attaching the rolling-window
    OI acceleration fields (oi_chg_15m_pct, oi_chg_30m_pct,
    oi_chg_60m_pct, oi_acceleration, oi_accel_label - see
    scanner.compute_oi_acceleration) based on this symbol's timestamped
    OI history across scans. Must be called while holding _state_lock -
    it reads and appends to _state["oi_history"].

    oi_trend_label is kept as an alias for oi_accel_label (falling back
    to "New" when there's not enough history yet) purely so existing
    call sites/templates that already read oi_trend_label keep working
    without having to touch every one of them."""
    history = _state["oi_history"]
    now = now_ist()
    cutoff = now - dt.timedelta(minutes=OI_HISTORY_MAX_MINUTES)
    for r in results:
        symbol, oi = r.get("symbol"), (r.get("oi_total") if r.get("oi_total") is not None else r.get("oi"))
        if not symbol or oi is None:
            continue
        hist = history.setdefault(symbol, [])
        # Migration guard: older persisted state stored plain numbers
        # instead of {"ts", "oi"} dicts - those can't be time-windowed,
        # so drop them rather than let a stale format crash the scan.
        hist[:] = [e for e in hist if isinstance(e, dict) and e.get("ts")]
        hist.append({"ts": now.isoformat(), "oi": oi})
        cutoff_iso = cutoff.isoformat()
        hist[:] = [e for e in hist if e["ts"] >= cutoff_iso]

        accel = compute_oi_acceleration(hist, now)
        r["oi_chg_15m_pct"] = accel["chg_15m"]
        r["oi_chg_30m_pct"] = accel["chg_30m"]
        r["oi_chg_60m_pct"] = accel["chg_60m"]
        r["oi_chg_prior_30m_pct"] = accel["chg_prior_30m"]
        r["oi_chg_prior_60m_pct"] = accel["chg_prior_60m"]
        r["oi_acceleration"] = accel["acceleration"]
        r["oi_accel_label"] = accel["accel_label"]
        r["oi_trend_label"] = accel["accel_label"] or "New"


def _apply_oi_screener_fields(results):
    """Attaches the dashboard's OI-driven fields to each result: works
    out today's price/OI move since session open (distinct from
    _apply_oi_trend's rolling-window numbers above) plus today's OI
    move vs. YESTERDAY's closing OI ("Day OI Change %"), classifies the
    4-quadrant OI Structure from the since-open move, flags whether
    that structure just changed this scan ("stage": "New"), derives a
    decisive "oi_break_signal" (Break Up / Break Down) when OI is
    building in a direction AND accelerating faster than its own recent
    pace, and cross-references the existing confluence signal (this
    symbol's own aligned/direction) to mark "positional_qualified" - a
    stock that isn't just showing an OI structure, but one that agrees
    with your confluence signal too. Must be called after
    _apply_oi_trend (needs oi_accel_label already set) and while
    holding _state_lock."""
    today = now_ist().date().isoformat()
    baseline = _state["oi_day_baseline"]
    prev_structure = _state["oi_structure_prev"]

    for r in results:
        symbol, oi, close = r.get("symbol"), (r.get("oi_total") if r.get("oi_total") is not None else r.get("oi")), r.get("close")
        if not symbol or r.get("error"):
            continue

        base = baseline.get(symbol)
        if base is None or base.get("date") != today:
            # First scan of a new trading day (or first time we've ever
            # seen this symbol) - carry forward whatever OI we last saw
            # yesterday as "previous day close" for Day OI Change %,
            # then reset today's open/close baseline to right now.
            prev_close_oi = base.get("oi_last") if base else None
            if oi is not None and close is not None:
                baseline[symbol] = {
                    "date": today, "oi": oi, "close": close,
                    "prev_close_oi": prev_close_oi, "oi_last": oi,
                }
            base = baseline.get(symbol)
        elif oi is not None:
            # Same trading day - keep today's open snapshot fixed, but
            # track the latest OI seen so it's ready to become
            # TOMORROW's "previous close" on the next day's rollover.
            base["oi_last"] = oi

        price_chg_pct = oi_chg_pct = day_oi_chg_pct = None
        if base and base.get("date") == today and oi is not None and close is not None:
            if base["close"]:
                price_chg_pct = (close - base["close"]) / base["close"] * 100
            if base["oi"]:
                oi_chg_pct = (oi - base["oi"]) / base["oi"] * 100
            prev_close_oi = base.get("prev_close_oi")
            if prev_close_oi:
                day_oi_chg_pct = (oi - prev_close_oi) / prev_close_oi * 100

        structure = classify_oi_structure(price_chg_pct, oi_chg_pct)
        r["price_chg_today_pct"] = price_chg_pct
        r["oi_chg_today_pct"] = oi_chg_pct
        r["oi_day_chg_pct"] = day_oi_chg_pct
        r["oi_structure"] = structure

        r["stage"] = "New" if structure and prev_structure.get(symbol) not in (None, structure) else None
        if structure:
            prev_structure[symbol] = structure

        # A decisive OI signal: not just "OI is building", but "OI is
        # building AND doing so faster than its own recent pace right
        # now" - that combination is what actually suggests fresh
        # conviction rather than routine drift. Long Buildup + strong/
        # moderate acceleration reads as a bullish break-up; Short
        # Buildup + strong/moderate acceleration reads as a bearish
        # break-down. Relies on oi_accel_label already being set by
        # _apply_oi_trend, which always runs first in the scan loop.
        accel_strong = r.get("oi_accel_label") in ("Strong acceleration", "Moderate acceleration")
        oi_break_signal = None
        if structure == "Long Buildup" and accel_strong:
            oi_break_signal = "Break Up"
        elif structure == "Short Buildup" and accel_strong:
            oi_break_signal = "Break Down"
        r["oi_break_signal"] = oi_break_signal

        direction = r.get("direction")
        aligned = r.get("aligned") or 0
        structure_agrees = (
            (direction == "Bullish" and structure == "Long Buildup")
            or (direction == "Bearish" and structure == "Short Buildup")
        )
        # index_ok: the Index/Market-trend filter (see _apply_index_filter,
        # which must run before this function so r["index_agrees"] is
        # already set) - only actually gates anything when
        # REQUIRE_INDEX_AGREEMENT is on; otherwise every row passes this
        # check regardless of what index_agrees says, same as before that
        # setting existed.
        index_ok = (not settings.REQUIRE_INDEX_AGREEMENT) or bool(r.get("index_agrees"))
        r["positional_qualified"] = bool(
            aligned >= settings.MIN_REQUIRED and structure_agrees and not r.get("in_opening_window") and index_ok
        )



def _run_v12_live(kite, results, radar_snapshot, swing_snapshot, fno_symbols, *, now):
    """Run V12 downstream evidence collection without risking scan uptime."""
    refresh = None
    try:
        refresh = v12_live.refresh_earnings_calendar(
            set(fno_symbols or []),
            now=now,
            state_file=config.V12_EARNINGS_STATE_FILE,
            ledger_file=config.V12_EARNINGS_LEDGER_FILE,
        )
    except Exception as exc:  # noqa: BLE001 - auxiliary calendar cannot stop live scanning
        log.exception("V12 earnings calendar refresh failed")
        refresh = {"status": "ERROR", "error": str(exc)}

    try:
        out = v12_live.process_live_scan(
            kite,
            results,
            radar_snapshot,
            swing_snapshot,
            now=now,
            option_snapshot_file=config.V12_OPTION_SNAPSHOT_FILE,
            option_state_file=config.V12_OPTION_STATE_FILE,
            earnings_state_file=config.V12_EARNINGS_STATE_FILE,
            deep_symbol_limit=config.V12_DEEP_SYMBOL_LIMIT,
            grace_minutes=config.V12_SNAPSHOT_GRACE_MINUTES,
        )
    except Exception as exc:  # noqa: BLE001 - V12 is downstream and must fail soft
        log.exception("V12 live orchestration failed")
        out = {
            "trade_console": v12_live.v12_trade_console.build_trade_console(
                radar_snapshot, swing_snapshot, results, limit=5
            ),
            "recorder": {"status": "ERROR", "error": str(exc)},
            "feasibility": {"status": "UNAVAILABLE", "trial25_locked": True},
            "earnings": {"status": "UNAVAILABLE", "active_count": 0, "upcoming_7d": []},
            "trial25_status": v12_live.TRIAL25_LOCKED_STATUS,
        }
    out.setdefault("earnings", {})["refresh_status"] = (refresh or {}).get("status") or "UNKNOWN"
    if (refresh or {}).get("error"):
        out["earnings"]["refresh_error"] = refresh.get("error")
    return out


def _load_persisted_state():
    """Restores the last scan from disk on startup, so a restart (a
    redeploy, the host restarting the container, etc.) doesn't wipe the
    day's data - after-hours, this is the only thing keeping results on
    screen for you to still analyse."""
    if not os.path.exists(SCAN_RESULTS_FILE):
        return
    try:
        with open(SCAN_RESULTS_FILE) as f:
            saved = json.load(f)
        if isinstance(saved, dict) and "results" in saved:
            with _state_lock:
                _state["results"] = saved.get("results", [])
                _state["last_scan"] = saved.get("last_scan")
                _state["last_scan_attempt"] = saved.get("last_scan_attempt")
                _state["last_scan_attempt_status"] = saved.get("last_scan_attempt_status")
                _state["last_scan_attempt_error"] = saved.get("last_scan_attempt_error")
                _state["scan_status"] = saved.get("scan_status") or "WAITING"
                _state["next_scan_due"] = saved.get("next_scan_due")
                _state["consecutive_scan_failures"] = int(saved.get("consecutive_scan_failures") or 0)
                restored_results = saved.get("results", []) or []
                restored_symbols = [r.get("symbol") for r in restored_results if isinstance(r, dict) and r.get("symbol")]
                _state["last_fno_symbols"] = list(saved.get("last_fno_symbols") or restored_symbols)
                _state["last_fno_cash_tokens"] = dict(saved.get("last_fno_cash_tokens") or {})
                _state["fno_universe_source"] = saved.get("fno_universe_source")
                _state["oi_history"] = saved.get("oi_history", {})
                _state["basis_history"] = saved.get("basis_history", {})
                _state["oi_day_baseline"] = saved.get("oi_day_baseline", {})
                _state["oi_structure_prev"] = saved.get("oi_structure_prev", {})
                _state["scan_symbol_health"] = saved.get("scan_symbol_health", {})
                _state["opportunity_forward"] = saved.get("opportunity_forward") or opportunity_forward.empty_state()
                _state["v12_trade_console"] = saved.get("v12_trade_console") or _state["v12_trade_console"]
                _state["v12_option_recorder"] = saved.get("v12_option_recorder") or _state["v12_option_recorder"]
                _state["v12_feasibility"] = saved.get("v12_feasibility") or _state["v12_feasibility"]
                _state["v12_earnings"] = saved.get("v12_earnings") or _state["v12_earnings"]
                _state["v12_trial25_status"] = saved.get("v12_trial25_status") or v12_live.TRIAL25_LOCKED_STATUS
                _state["last_error"] = None
        # Seed only the persisted F&O cash tokens. If Kite's NSE instrument
        # master is temporarily unavailable after restart, the price scan can
        # still resolve the last-known-good universe rather than going dark.
        scanner.seed_nse_instrument_cache(saved.get("last_fno_cash_tokens") or {})
    except (json.JSONDecodeError, OSError):
        pass


def _save_persisted_state():
    with _state_lock:
        snapshot = {
            "results": _state["results"],
            "last_scan": _state["last_scan"],
            "last_scan_attempt": _state.get("last_scan_attempt"),
            "last_scan_attempt_status": _state.get("last_scan_attempt_status"),
            "last_scan_attempt_error": _state.get("last_scan_attempt_error"),
            "scan_status": _state.get("scan_status"),
            "next_scan_due": _state.get("next_scan_due"),
            "consecutive_scan_failures": _state.get("consecutive_scan_failures", 0),
            "last_fno_symbols": _state.get("last_fno_symbols") or [],
            "last_fno_cash_tokens": _state.get("last_fno_cash_tokens") or {},
            "fno_universe_source": _state.get("fno_universe_source"),
            "oi_history": _state["oi_history"],
            "basis_history": _state["basis_history"],
            "oi_day_baseline": _state["oi_day_baseline"],
            "oi_structure_prev": _state["oi_structure_prev"],
            "scan_symbol_health": _state["scan_symbol_health"],
            "opportunity_forward": _state.get("opportunity_forward") or opportunity_forward.empty_state(),
            "v12_trade_console": _state.get("v12_trade_console") or {},
            "v12_option_recorder": _state.get("v12_option_recorder") or {},
            "v12_feasibility": _state.get("v12_feasibility") or {},
            "v12_earnings": _state.get("v12_earnings") or {},
            "v12_trial25_status": _state.get("v12_trial25_status") or v12_live.TRIAL25_LOCKED_STATUS,
        }
    try:
        # default=str is a safety net: if any result field ever ends up
        # holding a non-JSON-native type again (pandas Timestamp, numpy
        # int64, etc.) this coerces it to a string instead of raising -
        # a persistence hiccup should never be able to kill the whole
        # scan loop the way an uncaught TypeError here once did.
        with open(SCAN_RESULTS_FILE, "w") as f:
            json.dump(snapshot, f, default=str)
    except Exception:  # noqa: BLE001 - persistence must never crash the scan loop
        log.exception("Failed to persist scan results")


_load_persisted_state()


def get_state():
    with _state_lock:
        return dict(_state)


_SCAN_RETRY_MIN_SECONDS = 10
_SCAN_RETRY_MAX_SECONDS = 60


def _retry_delay_seconds(consecutive_failures: int) -> int:
    failures = max(1, int(consecutive_failures or 1))
    return min(_SCAN_RETRY_MAX_SECONDS, _SCAN_RETRY_MIN_SECONDS * (2 ** min(failures - 1, 3)))


def _iso_after(seconds: int) -> str:
    return (now_ist() + dt.timedelta(seconds=max(0, int(seconds)))).isoformat(timespec="seconds")


def _record_scan_attempt_start() -> str:
    ts = now_ist().isoformat(timespec="seconds")
    with _state_lock:
        _state["last_scan_attempt"] = ts
        _state["last_scan_attempt_status"] = "RUNNING"
        _state["last_scan_attempt_error"] = None
        _state["scan_status"] = "RUNNING"
        _state["next_scan_due"] = None
    return ts


def _record_scan_attempt_failure(error) -> int:
    message = str(error)
    with _state_lock:
        failures = int(_state.get("consecutive_scan_failures") or 0) + 1
        _state["consecutive_scan_failures"] = failures
        _state["last_scan_attempt_status"] = "FAILED"
        _state["last_scan_attempt_error"] = message
        _state["last_error"] = message
        _state["scan_status"] = "RETRYING"
    delay = _retry_delay_seconds(failures)
    with _state_lock:
        _state["next_scan_due"] = _iso_after(delay)
    return delay


def _record_scan_attempt_success(scan_ts: str) -> int:
    delay = max(1, int(settings.SCAN_INTERVAL_SECONDS))
    with _state_lock:
        _state["last_scan"] = scan_ts
        _state["last_scan_attempt_status"] = "SUCCESS"
        _state["last_scan_attempt_error"] = None
        _state["last_error"] = None
        _state["scan_status"] = "RUNNING"
        _state["consecutive_scan_failures"] = 0
        _state["next_scan_due"] = _iso_after(delay)
    return delay


def _set_scan_status(status: str, *, next_scan_due=None) -> None:
    with _state_lock:
        _state["scan_status"] = status
        _state["next_scan_due"] = next_scan_due


def _resolve_live_fno_symbols(kite):
    """Resolve today's F&O universe with a persisted last-known-good fallback.

    One immediate retry covers a transient instrument-master timeout. If both
    attempts fail, use the last successful live universe (and seed its cached
    NSE cash tokens) so scanning can continue without silently switching the
    research watchlist into the live universe.
    """
    last_exc = None
    for attempt in range(2):
        try:
            symbols = list(scanner.get_fno_stock_list(kite) or [])
            if symbols:
                tokens = scanner.cached_nse_instrument_tokens(symbols)
                with _state_lock:
                    _state["last_fno_symbols"] = symbols
                    if tokens:
                        _state["last_fno_cash_tokens"] = tokens
                    _state["fno_universe_source"] = "LIVE_KITE"
                return symbols, "LIVE_KITE"
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt == 0:
                time.sleep(1)
    with _state_lock:
        fallback = list(_state.get("last_fno_symbols") or [])
        tokens = dict(_state.get("last_fno_cash_tokens") or {})
    if fallback:
        scanner.seed_nse_instrument_cache(tokens)
        log.warning("Using last-known-good F&O universe after Kite master failure: %s", last_exc)
        with _state_lock:
            _state["fno_universe_source"] = "LAST_KNOWN_GOOD"
        return fallback, "LAST_KNOWN_GOOD"
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Kite returned an empty F&O universe and no last-known-good universe is available")


def _run_loop():
    while True:
        # The entire iteration body is wrapped in this try/except as a
        # last-resort safety net. Previously, a single uncaught exception
        # anywhere in this loop (e.g. trying to JSON-persist a pandas
        # Timestamp) would permanently kill this daemon thread - the web
        # page kept loading fine, so nothing looked "down", but scanning
        # silently stopped forever until the next redeploy. Now, no
        # matter what goes wrong on a given cycle, the loop logs it and
        # tries again next cycle instead of dying.
        try:
            kite = kite_auth.get_kite_client()
            if kite is not None and is_market_open():
                # Full historical research has priority over the expensive live scan.
                # Dashboard/API serving remains available; only Kite-heavy scanning yields.
                if research_runtime.is_research_active() or not research_runtime.live_scan_slot():
                    _set_scan_status("RESEARCH-PAUSED", next_scan_due=_iso_after(5))
                    _rescan_event.wait(timeout=5)
                    _rescan_event.clear()
                    continue
                wait_seconds = max(1, int(settings.SCAN_INTERVAL_SECONDS))
                _record_scan_attempt_start()
                try:
                    try:
                        fno_symbols, _universe_source = _resolve_live_fno_symbols(kite)
                        results = scan_watchlist(kite, timeframe=WATCHLIST_TIMEFRAME, symbols=fno_symbols)
                        # One extra Kite call per cycle for the Index/Market-
                        # trend filter - fetch_index_direction swallows its
                        # own exceptions and returns (None, None, None) on
                        # any failure, so a bad index fetch can never cost
                        # this cycle's actual stock results.
                        index_direction, index_close, index_chg_pct = fetch_index_direction(kite, WATCHLIST_TIMEFRAME)
                        # One more Kite call PER DISTINCT SECTOR actually
                        # present in this cycle's results (typically well
                        # under a dozen, not one per watchlist symbol) for
                        # the sector relative-strength filter - see
                        # scanner.fetch_sector_directions, same swallow-all-
                        # failures contract as the index fetch above.
                        sectors_needed = {SYMBOL_SECTOR_MAP[r["symbol"]] for r in results
                                           if r.get("symbol") in SYMBOL_SECTOR_MAP}
                        sector_contexts = fetch_sector_contexts(kite, sectors_needed, WATCHLIST_TIMEFRAME) \
                            if sectors_needed else {}
                        sector_directions = {k: (v or {}).get("direction") for k, v in sector_contexts.items()}
                        # OI history and index returns feed the early-signal
                        # layer. Both are fetched OUTSIDE the state lock - the
                        # OI sweep is throttled and can take a minute on a full
                        # F&O universe, and holding the lock through it would
                        # stall every dashboard request for that whole time.
                        try:
                            oi_history = scanner.fetch_oi_history(
                                kite, fno_symbols, timeframe=WATCHLIST_TIMEFRAME)
                        except Exception:  # noqa: BLE001 - a missing baseline must not stop the scan
                            log.exception("OI history fetch failed")
                            oi_history = {}
                        index_returns = scanner.fetch_index_returns(kite)

                        # Build V6 evidence outside the state lock so 5-minute finalist
                        # fetches never freeze dashboard requests. Legacy research fields
                        # remain attached for diagnostics, but live ranking is V6-only.
                        _apply_early_signal(results, oi_history,
                                            index_ret_20=index_returns.get(20),
                                            index_ret_10=index_returns.get(10),
                                            intraday=scanner.oi_is_intraday(WATCHLIST_TIMEFRAME))
                        _apply_sector_filter(results, sector_directions)
                        breadth = _compute_breadth(results)
                        _apply_oi_trend(results)
                        _apply_oi_screener_fields(results)
                        _apply_v6_cross_sectional_context(
                            results, index_chg_pct=index_chg_pct, breadth=breadth,
                            sector_contexts=sector_contexts,
                        )
                        _apply_v6_basis(results, history=_v6_basis_history, now=now_ist())
                        _apply_v8_dual_alpha(results, now=now_ist())
                        # Refresh real event headlines only for already-strong bullish
                        # attention names, then classify the explicit V9 playbooks.
                        _refresh_v9_catalyst_news(results)
                        # V9 converts cross-sectional evidence into explicit Bull/Bear
                        # playbooks. It is now the single production shortlist source.
                        _apply_v9_playbooks(results, now=now_ist())
                        _apply_shadow_early_radar(results)
                        _apply_v9_operational_shortlists(results)
                        # V8.2 Derivative Intelligence remains downstream: it decides
                        # option expression and cannot create an underlying playbook.
                        _apply_derivative_intelligence(kite, results, now=now_ist())
                        # V9.4 magnitude research is deliberately separate from
                        # Bull/Bear production logic. A cached completed-session
                        # daily-OI anomaly plus a *fresh* compression onset may
                        # register an executable ATM-straddle shadow observation,
                        # but it cannot create alerts or TRADE/WATCH states.
                        try:
                            v94_magnitude.register_live_trial14_straddles(
                                kite, results, now=now_ist()
                            )
                        except Exception:  # noqa: BLE001 - research shadow cannot stop live scan
                            log.exception("V9.4 magnitude shadow registration failed")
                        scan_now = now_ist()
                        scan_ts = scan_now.isoformat(timespec="seconds")
                        radar_snapshot = oi_view.live_opportunity_radar(
                            results, index_direction=index_direction, index_chg_pct=index_chg_pct,
                            market_breadth=breadth,
                        )
                        swing_snapshot = oi_view.swing_research_console(radar_snapshot)
                        v12_snapshot = _run_v12_live(
                            kite, results, radar_snapshot, swing_snapshot, fno_symbols, now=scan_now
                        )
                        with _state_lock:
                            _state["results"] = results
                            _state["index_direction"] = index_direction
                            _state["index_close"] = index_close
                            _state["index_chg_pct"] = index_chg_pct
                            _state["breadth"] = breadth
                            _state["scan_symbol_health"] = v9_playbooks.update_symbol_scan_health(
                                _state.get("scan_symbol_health") or {}, results, scan_ts
                            )
                            _state["opportunity_forward"] = opportunity_forward.process_scan(
                                _state.get("opportunity_forward"), radar_snapshot, results, now=scan_now,
                                swing_research=swing_snapshot,
                            )
                            _state["v12_trade_console"] = v12_snapshot.get("trade_console") or {}
                            _state["v12_option_recorder"] = v12_snapshot.get("recorder") or {}
                            _state["v12_feasibility"] = v12_snapshot.get("feasibility") or {}
                            _state["v12_earnings"] = v12_snapshot.get("earnings") or {}
                            _state["v12_trial25_status"] = v12_snapshot.get("trial25_status") or v12_live.TRIAL25_LOCKED_STATUS
                        wait_seconds = _record_scan_attempt_success(scan_ts)
                        try:
                            alerts.process_scan_results(results, WATCHLIST_TIMEFRAME)
                        except Exception:  # noqa: BLE001 - alerting must never break scanning
                            log.exception("Alert processing failed")
                    except Exception as exc:  # noqa: BLE001
                        log.exception("Background scan failed")
                        wait_seconds = _record_scan_attempt_failure(exc)

                    _save_persisted_state()
                finally:
                    # Release the Kite-heavy slot immediately after the scan. Do not
                    # hold it during the normal scan-interval sleep, or a research job
                    # could wait minutes even though the live scan itself already ended.
                    research_runtime.exit_live_scan()
                # wait() instead of a plain sleep() so a Quick Settings / Settings
                # change can wake the loop immediately. This wait deliberately happens
                # outside the heavy-work slot so historical research can start now.
                _rescan_event.wait(timeout=wait_seconds)
                _rescan_event.clear()
            elif kite is not None and v12_live.post_cash_derivative_window(now_ist()):
                # NSE equity derivatives now remain open after the legacy
                # 15:30 continuous cash-session boundary. Do NOT extend the
                # entire scanner: old candle/session logic is intentionally
                # frozen. Run only the lightweight V12 option recorder so the
                # fixed 15:37 POST_CAS snapshot is not silently impossible.
                post_now = now_ist()
                with _state_lock:
                    post_results = list(_state.get("results") or [])
                    post_symbols = list(_state.get("last_fno_symbols") or [])
                    post_index_direction = _state.get("index_direction")
                    post_index_chg_pct = _state.get("index_chg_pct")
                    post_breadth = _state.get("breadth")
                post_radar = oi_view.live_opportunity_radar(
                    post_results,
                    index_direction=post_index_direction,
                    index_chg_pct=post_index_chg_pct,
                    market_breadth=post_breadth,
                )
                post_swing = oi_view.swing_research_console(post_radar)
                post_v12 = _run_v12_live(
                    kite, post_results, post_radar, post_swing, post_symbols, now=post_now
                )
                with _state_lock:
                    _state["v12_trade_console"] = post_v12.get("trade_console") or _state.get("v12_trade_console") or {}
                    _state["v12_option_recorder"] = post_v12.get("recorder") or {}
                    _state["v12_feasibility"] = post_v12.get("feasibility") or {}
                    _state["v12_earnings"] = post_v12.get("earnings") or {}
                    _state["v12_trial25_status"] = post_v12.get("trial25_status") or v12_live.TRIAL25_LOCKED_STATUS
                _save_persisted_state()
                _set_scan_status("V12-POST-CAS", next_scan_due=_iso_after(20))
                _rescan_event.wait(timeout=20)
                _rescan_event.clear()
            else:
                # Not logged in yet today, or outside market hours - the
                # last scan's results (loaded from disk on startup, or still
                # in memory from earlier today) are left untouched so
                # there's always something on screen to analyse. Check back
                # periodically without hammering anything.
                status = "MARKET-CLOSED" if kite is not None else "WAITING-LOGIN"
                _set_scan_status(status, next_scan_due=_iso_after(30))
                _rescan_event.wait(timeout=30)
                _rescan_event.clear()
        except Exception as exc:  # noqa: BLE001 - never let this thread die
            log.exception("Background scan loop hit an unexpected error - retrying")
            delay = _record_scan_attempt_failure(
                "Background loop hit an unexpected error - see server logs: %s" % exc
            )
            time.sleep(delay)


def start_background_scanner():
    thread = threading.Thread(target=_run_loop, daemon=True)
    thread.start()
