# F&O Early Movement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the mature daily confluence/BTST surfaces with a selective 15-minute NSE F&O early-movement shortlist driven by fresh positioning, time-of-day participation, relative strength and entry timing.

**Architecture:** Keep the existing Flask/Kite scanner and indicator pipeline, but add focused early-movement helpers and make the background loop use the live F&O universe on 15-minute bars. Best Entries is ranked from independent evidence groups; legacy late/duplicative gates remain research/display-only. BTST/STBT is research-only.

**Tech Stack:** Python, pandas, NumPy, Flask/Jinja, Zerodha Kite Connect, vanilla JavaScript.

**Spec:** `docs/superpowers/specs/2026-08-28-fno-early-movement-design.md`

## Global Constraints
- NSE stock F&O only; derive the live universe from Kite NFO futures.
- Primary live timeframe is 15 minutes; primary research horizon is 3 bars with 1/2/3/5/10 reported.
- Missing OI/participation data never counts as confirmation.
- Do not brute-force parameter combinations; only targeted comparisons with chronological holdout.
- Retire live BTST/STBT alerts and candidates until positive holdout edge exists.
- Preserve existing Flask routes, Railway startup shape and Kite login flow.

---

### Task 1: F&O-only 15-minute execution surface
**Files:** Modify `app/config.py`, `app/background.py`; Test `tests/test_early_movement_upgrade.py`.
**Interfaces:** `scanner.get_fno_stock_list(kite) -> list[str]`; `WATCHLIST_TIMEFRAME == "15minute"`.
- [ ] Write failing tests proving the live timeframe is 15-minute, shortlist cap is 5, and the background scan refreshes the NFO stock-futures universe instead of trusting a stale manual watchlist.
- [ ] Run the tests and verify they fail for the intended reason.
- [ ] Implement the minimal configuration/universe refresh.
- [ ] Run the tests and verify green.

### Task 2: Aggregate live near/next/far futures OI
**Files:** Modify `app/scanner.py`; Test `tests/test_early_movement_upgrade.py`.
**Interfaces:** `fetch_oi_map(kite, symbols)` returns `oi`, `near_oi`, `next_oi`, `far_oi`, contract metadata, and summed day-high/day-low where available.
- [ ] Write a fake-Kite regression test with three expiries and prove current code only returns near-month OI.
- [ ] Run RED.
- [ ] Implement expiry-sorted three-contract mapping and batched quote aggregation.
- [ ] Run GREEN.

### Task 3: Time-of-day relative volume
**Files:** Modify `app/indicators.py`; Test `tests/test_early_movement_upgrade.py`.
**Interfaces:** `time_of_day_rvol(df, lookback_sessions=20) -> Series` and `tod_rvol`/`tod_rvol_accel` fields from `compute_signal`.
- [ ] Write tests using synthetic 15-minute sessions where the same raw volume is normal at the open but abnormal at midday.
- [ ] Run RED.
- [ ] Implement same-clock-slot median baseline over prior sessions and a short acceleration read.
- [ ] Run GREEN.

### Task 4: Fresh trigger and entry-quality semantics
**Files:** Modify `app/indicators.py`, `app/config.py`; Test `tests/test_early_movement_upgrade.py`.
**Interfaces:** RSI defaults 14; MACD 8/17/9 for 15-minute; `entry_trigger`, `entry_trigger_bars_ago`, `vwap_side_agrees`, `entry_is_extended`.
- [ ] Write tests proving mature alignment without a recent RSI/MACD trigger cannot qualify, wrong-side VWAP fails, and >1.25 ATR extension fails.
- [ ] Run RED.
- [ ] Implement defaults and fields without adding extra indicator votes.
- [ ] Run GREEN.

### Task 5: Independent early-movement score
**Files:** Create `app/early_movement.py`; Modify `app/background.py`; Test `tests/test_early_movement_upgrade.py`.
**Interfaces:** `score_candidate(row) -> dict(score, coverage, eligible, parts, blockers)`.
- [ ] Write tests for long/short symmetry, missing-data penalty, OI mismatch rejection, fading 60m OI rejection, and a high-quality synthetic candidate ranking above an extended/late one.
- [ ] Run RED.
- [ ] Implement weights 35/20/20/15/10 and hard blockers from the design.
- [ ] Run GREEN.

### Task 6: Remove legacy parameters from Best Entries and Settings emphasis
**Files:** Modify `app/background.py`, `app/templates/index.html`, `app/templates/settings.html`, `app/web.py`.
**Interfaces:** Best Entries consumes only `early_movement` score/blockers; legacy fields remain chart/research-only.
- [ ] Write template/route tests proving Best Entries no longer references BTST, big-candle, strong-close, delivery or candlestick gates.
- [ ] Run RED.
- [ ] Simplify settings UI to the parameters that affect live early movement; move legacy gates to a collapsed research/legacy section or remove controls that no longer affect Best Entries.
- [ ] Run GREEN.

### Task 7: Retire live BTST/STBT and keep research-only edge status
**Files:** Modify `app/background.py`, `app/alerts.py`, `app/templates/index.html`, `app/templates/backtest.html`, `app/web.py`; Test `tests/test_early_movement_upgrade.py`.
**Interfaces:** No live `btst_side` shortlist/Telegram publish; Backtest overnight endpoint remains available.
- [ ] Write tests proving BTST candidates are not published live and the dashboard states research-only/no edge.
- [ ] Run RED.
- [ ] Remove live candidate generation/alert call and replace dashboard panel with research status/link.
- [ ] Run GREEN.

### Task 8: Research ranking and holdout promotion
**Files:** Modify `app/backtest.py`, `app/templates/backtest.html`; Test `tests/test_early_movement_upgrade.py`.
**Interfaces:** 3-bar primary result, 1/2/3/5/10 secondary, chronological 70/30 split, `promotable` requires positive holdout net expectancy and PF > 1.1 with sample floor.
- [ ] Write tests for chronological split and promotion rules.
- [ ] Run RED.
- [ ] Implement the focused research comparison without brute-force combination search.
- [ ] Run GREEN.

### Task 9: Verification and clean package
**Files:** All changed files; Create deployment ZIP in `/mnt/data`.
- [ ] Run full pytest suite.
- [ ] Run `python -m compileall app run.py`.
- [ ] Validate inline JavaScript syntax from changed templates.
- [ ] Inspect package for `.env`, tokens, settings, caches and compiled artifacts.
- [ ] Build `DBIndicator-early-movement.zip` and re-run tests from a fresh extracted copy.
