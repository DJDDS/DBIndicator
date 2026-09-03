# V9.8 Incremental OI Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a non-retuned V9.8 layer that tests whether Trial-19 extreme OI adds next-session variance information beyond HAR volatility, abnormal futures volume and earnings.

**Architecture:** Extend existing daily NSE frames with variance/HAR/volume fields, evaluate them in a separate `v98_incremental_oi` module, repair earnings auditability, and expose the result in the existing V9.7 runner/UI. V9.8 is promotion-only research and never changes Trial-19 event formation or production playbooks.

**Tech Stack:** Python 3, pandas, NumPy, Flask/Jinja, pytest, official NSE daily FO/CM archives.

**Spec:** `docs/superpowers/specs/2026-09-03-v98-incremental-oi-validation-design.md`

## Global Constraints
- Freeze `total_z >= 1.5` and 2018-09-01 through 2021-08-31.
- Preserve existing V9.7 Trial-19 output and all prior locked finals.
- Trial 18 remains LOCKED; no TRADE/WATCH activation.
- Use only point-in-time fields available by event-date close for HAR/volume regressors.
- Missing earnings/volume/variance coverage fails closed; no fabricated data.

---

### Task 1: Daily variance, HAR and futures-volume features
**Files:** modify `app/nse_futures_history.py`, `app/v95_daily_evidence.py`; create `tests/test_v980_features.py`.
**Produces:** `total_volume`, `near_volume`; `next_yz_var`, `next_gk_var`, `har_daily_var`, `har_weekly_var`, `har_monthly_var`, `futures_volume_z`.
- [ ] Write failing parser/history tests proving FUTSTK volume aggregates point-in-time.
- [ ] Write failing daily-frame tests proving no future leakage in HAR/volume covariates and correct next-session variance formulas.
- [ ] Implement minimal history/frame changes.
- [ ] Run focused tests to green.

### Task 2: Incremental V9.8 evaluator
**Files:** create `app/v98_incremental_oi.py`; create `tests/test_v980_incremental.py`.
**Consumes:** Trial-19 prepared symbol frames and frozen Trial-19 result.
**Produces:** `evaluate_v98_incremental(symbol_frames, frozen_result, earnings_map)` with variance coverage, HAR-only and HAR+volume+OI two-way clustered regressions, incremental R² and matched variance diagnostics.
- [ ] Write failing tests for frozen-rule lock, HAR construction, volume horse race and t>=3 gate.
- [ ] Implement deterministic two-way clustered regressions using existing V9.6 robust OLS.
- [ ] Run focused tests to green.

### Task 3: Earnings audit repair and split
**Files:** modify `app/nse_earnings_history.py`, `app/v97_trial19.py`; create `tests/test_v980_earnings_audit.py`.
**Produces:** earnings metadata with record/date counts, symbol match counts, overlap counts and examples; inside/outside earnings reports.
- [ ] Write failing tests proving 100% file/symbol coverage with zero matched dates cannot be called a completed earnings control.
- [ ] Add normalized symbol aliases and explicit matched-date audit counters.
- [ ] Add inside/outside +/-5 trading-session split.
- [ ] Run focused tests to green.

### Task 4: Runner, state, API and UI integration
**Files:** modify `app/backtest.py`, `app/templates/backtest.html`, `app/web.py`; create `tests/test_v980_runner.py`, `tests/test_v980_ui.py`, `V9_8_CHANGELOG.md`.
**Produces:** V9.8 current build marker and result card within the existing `/api/v97` job, avoiding a second heavy archive pass.
- [ ] Write failing release/UI tests for V9.8 marker, four high-risk test rows and Trial-18 lock.
- [ ] Bump V97 run schema/build so old V9.7.2 shards cannot silently skip new volume fields.
- [ ] Invoke V9.8 evaluator after earnings map is available; persist JSON-safe scalar diagnostics.
- [ ] Render variance/HAR/volume/earnings result without hiding the frozen Trial-19 result.
- [ ] Run focused runner/UI tests to green.

### Task 5: Regression and packaging
**Files:** all tests/release docs.
- [ ] Run complete pytest suite in isolated batches if required by known teardown behavior.
- [ ] Run Python compilation and rendered Backtest/Dashboard JavaScript syntax checks.
- [ ] Verify `ACTIVE_PLAYBOOKS = ()`, Trial-18 lock, frozen threshold/window and no prior-final reads.
- [ ] Create cache-free ZIP, extract into a fresh directory and repeat the complete verification against the exact archive.
