# V9.7.2 Trial 19 Confound & Integrity Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add pre-registered confound/integrity controls around frozen Trial 19 while preserving every existing trial rule and lock.

**Architecture:** Extend the existing daily-frame builder with pre-signal realized-volatility fields, add confound-only evaluators to `v97_trial19`, finish monthly MWPL reconstruction in `nse_mwpl`, add a recent-window ban bound in the runner, and surface all results through the existing Backtest V9.7 card. Original Trial-19 efficacy remains a separate immutable result.

**Tech Stack:** Python 3, pandas, numpy, Flask/Jinja/inline JS, pytest, official NSE archive clients already in the repo.

**Spec:** `docs/superpowers/specs/2026-09-02-v972-trial19-confound-integrity-design.md`

## Global Constraints
- Trial 19 event stays `total FUTSTK OI z >= 1.5`.
- Trial 19 window stays 2018-09-01 through 2021-08-31.
- Frozen same-day + same-DTE baseline and binary-event inference stay unchanged.
- Trial 18 stays locked unless promotion controls pass; pass only means eligible for preregistration.
- `ACTIVE_PLAYBOOKS = ()`; no TRADE/WATCH activation.
- Prior locked finals remain untouched.
- This workspace is an isolated copied release tree without `.git`; verification checkpoints replace commit steps.

---

### Task 1: Pre-signal volatility features
**Files:** Modify `app/v95_daily_evidence.py`; test `tests/test_v974_confounds.py`.
**Produces:** `realized_vol5_prev`, `movement_prev1_atr`, `movement_prev2_atr` columns.
- [ ] Write failing tests asserting all three fields use only information available before the event date.
- [ ] Run focused tests and confirm RED.
- [ ] Implement 5-day annualized realized volatility shifted one session, and prior-session movement fields derived from the existing horizon-scaled movement series without look-ahead.
- [ ] Run focused tests and confirm GREEN.

### Task 2: Prior-volatility matching and persistence diagnostics
**Files:** Modify `app/v97_trial19.py`; test `tests/test_v974_confounds.py`.
**Produces:** `evaluate_volatility_confound(...)` and persistence diagnostics.
- [ ] Write failing tests for same-date + same-DTE + realized-vol quintile controls and exclusion of event rows from controls.
- [ ] Write failing test that frozen `same_day_dte_matched_report` output is unchanged.
- [ ] Implement cross-sectional prior-vol quintiles and confound-only matched report.
- [ ] Implement t-1/t-2 matched diagnostics and warning flag.
- [ ] Verify focused tests GREEN.

### Task 3: Earnings confound independent of MWPL status
**Files:** Modify `app/v97_trial19.py`, `app/backtest.py`; test `tests/test_v974_confounds.py`, `tests/test_v974_runner.py`.
**Produces:** earnings diagnostic that runs when frozen efficacy gates pass even if integrity is unresolved.
- [ ] Write RED tests for efficacy-pass/integrity-inconclusive earnings evaluation.
- [ ] Implement efficacy-only eligibility helper and earnings confound evaluator.
- [ ] Keep official coverage fail-closed at 90% and preserve +/-5 observed trading sessions.
- [ ] Verify focused tests GREEN.

### Task 4: Monthly MWPL integrity path
**Files:** Modify `app/nse_mwpl.py`, `app/backtest.py`; tests `tests/test_v973_monthly_mwpl.py`, `tests/test_v972_mwpl_performance.py`.
**Produces:** monthly MWPL utilisation + targeted secban with visible month progress.
- [ ] Update stale performance tests from daily progress to `MWPL months x/36` and confirm RED where needed.
- [ ] Finish parser/client/builder edge cases: month caching, date/observation coverage, 95/80 state, targeted secban.
- [ ] Wire monthly builder into Trial-19 runner only; preserve older builders for V9.5/V9.6 compatibility.
- [ ] Verify MWPL-focused tests GREEN.

### Task 5: Recent-window ban-overlap bound
**Files:** Modify `app/v97_trial19.py`, `app/backtest.py`; test `tests/test_v974_mwpl_bound.py`.
**Produces:** recent event overlap, all-event matched lift, clean matched lift, absolute lift delta, pre-registered non-load-bearing flag.
- [ ] Write RED unit tests for overlap/delta logic and 5%/0.02x thresholds.
- [ ] Add pure `evaluate_mwpl_bound(...)` function.
- [ ] Add runner helper to load/reuse 2021-09-01..2023-09-01 NSE histories only when historical MWPL is unavailable.
- [ ] Verify recent-bound tests GREEN and no calls into closed Trial-17 evaluator.

### Task 6: Promotion gate and UI
**Files:** Modify `app/v97_trial19.py`, `app/backtest.py`, `app/web.py`; tests `tests/test_v974_ui_release.py`.
**Produces:** final Trial-18 eligibility gate plus transparent reporting.
- [ ] Write RED tests for final gate combinations and preserved frozen Trial-19 headline.
- [ ] Implement final promotion gate from frozen efficacy + vol confound + earnings + MWPL applied/bounded.
- [ ] Update build ID/copy to V9.7.2, show ~1.13x replicated planning effect and retire 1.22x from projections.
- [ ] Display monthly MWPL coverage, recent bound, vol-matched result, t-1/t-2 diagnostics and earnings result.
- [ ] Verify UI/release tests GREEN and `/backtest` route regression.

### Task 7: Full release verification and packaging
**Files:** Update `README.md`, create `V9_7_2_CHANGELOG.md`; package final ZIP.
- [ ] Run every pytest file in isolated batches and account for the complete count.
- [ ] Run Python compilation.
- [ ] Validate rendered Backtest and Dashboard JavaScript syntax.
- [ ] Confirm `ACTIVE_PLAYBOOKS = ()`, Trial-19 constants/window unchanged, Trial 18 locked by default.
- [ ] Build cache-free ZIP excluding `.env`, `.pytest_cache`, `.dbindicator-research`, `__pycache__`, `.pyc/.pyo`.
- [ ] Extract exact ZIP into a fresh directory and repeat full tests + compile + JS + release-safety checks.
- [ ] Compute SHA-256 and hand off only the verified archive.
