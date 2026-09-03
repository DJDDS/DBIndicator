# V9.9.2 Trial 20 Log-RV Integrity Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Repair Trial 20's variance-forecast specification by fitting HAR and HAR+Volume in log-RV space, rerun the same frozen OOS gate once, and encode a non-promotable closure verdict.

**Architecture:** Modify only the V9.9 Trial-20 forecasting layer and its UI/release metadata. Fit benchmark/challenger OLS in log variance space, back-transform with training-only smearing, preserve all existing frozen inputs and robustness checks, and add forecast-positivity diagnostics plus a closure-state interpretation.

**Tech Stack:** Python 3, pandas, NumPy, Flask/Jinja/vanilla JavaScript, pytest.

**Spec:** `docs/superpowers/specs/2026-09-03-v992-log-rv-integrity-closure-design.md`

## Global Constraints
- Trial number stays 20; no new feature or threshold search.
- Independent outcome window stays 2015-09-01 through 2018-08-31.
- Abnormal FUTSTK volume construction is unchanged.
- MSE, QLIKE, OOS R2 and Clark-West are evaluated in original variance units.
- Clark-West one-sided hurdle remains 1.645.
- Trial 18 stays LOCKED; Trial 19 stays closed; no TRADE/WATCH activation.
- A corrected PASS is specification-sensitive and cannot be promoted from the already-observed outcome window.
- Live Opportunity Radar and OI dashboard diagnostics remain unchanged.

---

### Task 1: Log-RV forecast core and smearing back-transform

**Files:**
- Modify: `app/v99_volume_gate.py`
- Create: `tests/test_v992_log_rv_closure.py`

**Interfaces:**
- Produces: `_fit_log_variance_model(y, X) -> (beta, smear)` and positive original-unit forecasts from `_oos_prediction_rows`.

- [x] **Step 1:** Add failing tests proving log-RV fits never emit non-positive forecasts and smearing uses training residuals only.
- [x] **Step 2:** Run `python -m pytest tests/test_v992_log_rv_closure.py -q` and confirm RED.
- [x] **Step 3:** Implement log transforms with `VAR_FLOOR`, OLS in log space, training-only `mean(exp(residual))` smearing, and original-unit back-transform for HAR and HAR+Volume.
- [x] **Step 4:** Re-run focused tests and confirm GREEN.

### Task 2: Frozen gate semantics and closure state

**Files:**
- Modify: `app/v99_volume_gate.py`
- Test: `tests/test_v992_log_rv_closure.py`

**Interfaces:**
- Produces: `forecast_integrity` diagnostics and closure states `CLOSED_REJECTED_LOG_RV_CONFIRMED` / `SPECIFICATION_SENSITIVE_NOT_PROMOTED`.

- [x] **Step 1:** Add failing tests for positive forecast diagnostics, frozen Trial-20 spec metadata, and non-promotable corrected PASS behavior.
- [x] **Step 2:** Run focused tests and confirm RED.
- [x] **Step 3:** Add forecast min/max/floor-hit diagnostics and map the frozen gate result to closure interpretation without changing the underlying statistical gate booleans.
- [x] **Step 4:** Re-run focused tests and confirm GREEN.

### Task 3: UI integrity and release metadata

**Files:**
- Modify: `app/templates/backtest.html`
- Modify: `RESEARCH_BUILD.txt`
- Modify: `PRODUCTION_BUILD.txt`
- Modify: `V9_9_CHANGELOG.md`
- Modify: current-build assertions in release/UI tests
- Test: `tests/test_v992_log_rv_closure.py`

**Interfaces:**
- Produces: V9.9.2 build marker, log-RV closure copy, forecast-integrity row, and chronological display `positive/4 (need >=3)`.

- [x] **Step 1:** Add failing UI assertions for V9.9.2 marker, log-RV wording, forecast integrity, and corrected block denominator.
- [x] **Step 2:** Run focused UI/release tests and confirm RED.
- [x] **Step 3:** Update UI rendering and build/changelog markers without changing live scanner copy or routes.
- [x] **Step 4:** Re-run focused tests and confirm GREEN.

### Task 4: Full regression and deployable package

**Files:** all changed files; output ZIP.

- [x] **Step 1:** Run `python -m pytest -q` and require zero failures.
- [x] **Step 2:** Run `python -m compileall -q app`.
- [x] **Step 3:** Verify `ACTIVE_PLAYBOOKS = ()`, Trial-18 lock, Trial-19 closed state, frozen window, and unchanged volume-feature construction.
- [x] **Step 4:** Create cache-free `DBIndicator-institutional-v9.9.2-trial20-LOG-RV-INTEGRITY-CLOSURE-FINAL.zip`.
- [x] **Step 5:** Extract the exact ZIP into a fresh directory; rerun the full suite and compile check there.
- [x] **Step 6:** Run ZIP integrity and SHA-256 checks before handoff.
