# Screener Quality Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make DBIndicator surface fewer, timelier F&O entries and research them with honest OI coverage, expectancy, profit factor, and untouched holdout data.

**Architecture:** Keep the existing scanner/backtest structure, but make OI mandatory evidence instead of a permissive decoration, separate mature state alignment from fresh entry timing, and rank research by out-of-sample expectancy. Avoid brute-force optimization; test individual gates plus only targeted OI interactions.

**Tech Stack:** Python, pandas, NumPy, Flask/Jinja, Zerodha Kite Connect, vanilla JavaScript.

**Spec:** User-approved in-chat design on 2026-08-28: fix OI coverage, improve best-entry timing/anti-chase logic, compare continuation vs reversal BTST, add multi-horizon and gate-interaction research, and optimize for expectancy rather than raw win rate.

## Global Constraints

- Preserve the existing Railway/Flask deployment shape and public routes.
- No live-order execution is added.
- Missing OI must never count as OI confirmation.
- Primary positional research horizon is 3 bars; still report 1/2/3/5/10 bars.
- Research comparisons must include realistic cost/slippage and a chronological holdout.
- Do not brute-force all gate combinations.

---

### Task 1: Correct OI data semantics

**Files:** `app/backtest.py`, `app/scanner.py`, `app/background.py`, `tests/test_quality_upgrade.py`

- [x] Write failing tests for missing OI and 365-day OI coverage.
- [x] Exclude missing/neutral OI when OI agreement is required.
- [x] Let backtest OI history span the requested research window plus warm-up.
- [x] Add pass/fail/missing diagnostics.
- [x] Verify regression tests.

### Task 2: Make Best Entries timely and anti-chase

**Files:** `app/indicators.py`, `app/early_signal.py`, `app/background.py`, `app/alerts.py`, `app/templates/index.html`, `tests/test_quality_upgrade.py`

- [x] Separate fresh crossover trigger from ongoing alignment state.
- [x] Require trigger in the last two bars.
- [x] Make close-location scoring direction-aware.
- [x] Remove automatic big-candle reward and score early ATR location instead.
- [x] Require measurable, positive recent OI and non-fading acceleration.
- [x] Make alerts use the same Best Entries shortlist.
- [x] Verify regression tests.

### Task 3: Improve research quality

**Files:** `app/backtest.py`, `app/web.py`, `app/templates/backtest.html`, `tests/test_quality_upgrade.py`

- [x] Report 1/2/3/5/10-bar outcomes and use 3 bars as primary reference.
- [x] Add profit factor, payoff, average winner/loss metrics.
- [x] Reuse one price/OI snapshot across gate runs.
- [x] Add targeted OI pair interactions.
- [x] Add 30% chronological holdout and rank gates by holdout net expectancy.
- [x] Show OI pass/fail/missing diagnostics in the UI.
- [x] Verify regression tests.

### Task 4: Correct BTST/STBT research

**Files:** `app/backtest.py`, `app/templates/backtest.html`, `app/config.py`, `tests/test_quality_upgrade.py`

- [x] Compare continuation and exact reversal at next open and next close.
- [x] Keep live continuation alerts off by default until research supports them.
- [x] Verify regression tests.

### Task 5: Final verification and packaging

**Files:** all modified files

- [x] Run the full pytest suite.
- [x] Run Python compile checks.
- [x] Run JavaScript/template syntax sanity checks.
- [x] Inspect git diff for accidental/unrelated edits and secrets.
- [x] Package a clean deployable ZIP.
