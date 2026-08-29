# F&O Stock-in-Play Breakout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace pre-breakout indicator voting with a backtestable 15-minute F&O stock-in-play/range-breakout engine for intraday and 1–2 day swing trades.

**Architecture:** Add a focused `stock_in_play` feature/scoring module, feed it from the existing 15-minute scanner, and rework Early Movement Research to replay the same breakout semantics. Keep 4H/Daily as context only, preserve live three-expiry OI aggregation, and make historical OI availability explicit.

**Tech Stack:** Python, pandas, NumPy, Flask/Jinja, Zerodha Kite Connect, vanilla JavaScript, pytest.

**Spec:** `docs/superpowers/specs/2026-08-28-stock-in-play-breakout-design.md`

## Global Constraints
- NSE stock F&O only.
- 15-minute is the execution/research timeframe.
- Intraday and 1–2 day swing results are reported separately.
- Missing OI is unavailable evidence, never confirmation.
- All research is net of configured costs/slippage and includes a chronological holdout.
- No brute-force Cartesian parameter search.

---

### Task 1: Repair intraday OI replay
**Files:** Modify `app/early_research.py`; Test `tests/test_stock_in_play_breakout.py`.
- [ ] Add a timezone-aware regression test for `_session_pct_change` showing 30m/60m changes remain measurable within one NSE session and reset across sessions.
- [ ] Run the test and confirm it fails with the current implementation.
- [ ] Implement timezone-safe same-session masking without `.values` timezone loss.
- [ ] Run targeted and full tests.

### Task 2: Add range/stock-in-play features
**Files:** Create `app/stock_in_play.py`; Test `tests/test_stock_in_play_breakout.py`.
- [ ] Test recent 6-bar range, 30-minute opening range, fresh breakout rising-edge detection, gap-in-ATR, range expansion, and stock-in-play participation.
- [ ] Implement vectorized historical features and a live-row scorer with direction assigned only by actual breakout/breakdown.
- [ ] Ensure compression is directionless and RSI/MACD are not eligibility gates.

### Task 3: Create separate intraday and swing qualification
**Files:** Modify `app/stock_in_play.py`, `app/background.py`, `app/alerts.py`; Test `tests/test_stock_in_play_breakout.py`.
- [ ] Test Intraday Best Entry requires breakout + VWAP + participation + anti-chase + sponsorship.
- [ ] Test OI unavailable can be rescued only by strong market/sector sponsorship and is labelled unavailable, not confirmed.
- [ ] Test Swing 1–2D adds 4H/late-session persistence and does not make intraday eligibility stricter.
- [ ] Replace `_apply_early_movement_shortlist` with the new staged ranking and keep maximum shortlist small.

### Task 4: Rebuild research horizons and excursion statistics
**Files:** Modify `app/early_research.py`, `app/backtest.py`; Test `tests/test_stock_in_play_breakout.py`.
- [ ] Test 30m/1h/2h/4h/EOD and next-session/second-session exits.
- [ ] Test MFE, MAE, and time-to-0.5ATR/1ATR calculations.
- [ ] Test long/short and setup-source splits.
- [ ] Implement separate intraday and swing summaries.

### Task 5: Add compression baseline lift and motivated interaction studies
**Files:** Modify `app/early_research.py`; Test `tests/test_stock_in_play_breakout.py`.
- [ ] Test compression hit-rate versus unconditional-bar baseline for 0.5/0.75/1/1.5 ATR and 1h/2h/4h/1D.
- [ ] Add lift calculation.
- [ ] Compare breakout-only, breakout+TOD RVOL, breakout+OI, breakout+TOD RVOL+OI, and breakout+4H context on holdout; do not brute-force other combinations.

### Task 6: Align dashboard and backtest UI
**Files:** Modify `app/templates/index.html`, `app/templates/backtest.html`, `app/web.py`, `app/config.py` if needed; Test template assertions.
- [ ] Show Radar, Ignition, Intraday Best Entry, Swing 1–2D Candidate.
- [ ] Remove legacy “Aligned / RSI/MACD vote” emphasis from the main shortlist table.
- [ ] Show breakout source, TOD RVOL, OI 30/60m, OI status, VWAP, extension ATR, and stage.
- [ ] Backtest UI labels actual horizons rather than “3 bars = 1–3 day.”

### Task 7: Verification and clean package
**Files:** all changed files.
- [ ] Run `PYTHONPATH=. pytest -q`.
- [ ] Run Python compilation across `app/` and top-level Python files.
- [ ] Validate embedded JavaScript syntax from changed templates.
- [ ] Scan package for secrets, `.env`, caches, and credentials.
- [ ] Create a clean ZIP and rerun tests from a fresh extraction.
