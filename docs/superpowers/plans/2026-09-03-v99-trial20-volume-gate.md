# V9.9 Trial 20 Volume Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a preregistered, fail-closed V9.9/Trial-20 abnormal FUTSTK volume OOS forecasting gate to the existing V9.8 research app.

**Architecture:** Extend official NSE futures history with notional turnover, build a dedicated `app/v99_volume_gate.py` research module, integrate a separate resumable V9.9 runner/state/API path, and add a backtest UI block. Preserve V9.8/Trial-19 outputs and all live/OI diagnostics unchanged.

**Tech Stack:** Python, pandas, NumPy, pytest, Flask/Jinja/vanilla JavaScript.

**Spec:** `docs/superpowers/specs/2026-09-03-v99-trial20-volume-gate-design.md`

## Global Constraints
- Research/shadow only; no TRADE/WATCH activation.
- Trial 18 remains LOCKED.
- Trial 19 remains closed; do not retune OI.
- No threshold optimization on the independent outcome period.
- OOS metrics limited to MSE and QLIKE; Clark-West one-sided hurdle 1.645.
- OI remains visible as diagnostic intelligence but does not qualify Trial-20 magnitude events.

---

### Task 1: Official futures notional-turnover history
**Files:** modify `app/nse_futures_history.py`; test `tests/test_v990_turnover_history.py`.
**Interfaces:** Produce contract column `turnover_notional` and per-symbol series `total_turnover_notional` / `near_turnover_notional`.
- [ ] Write failing parser/aggregation tests for legacy `VAL_INLAKH`, UDiFF `TtlTrfVal`, and Market Activity `Traded Value`.
- [ ] Run tests and verify RED.
- [ ] Implement normalized notional-turnover extraction and aggregation.
- [ ] Run tests and existing NSE history tests GREEN.

### Task 2: Point-in-time abnormal-volume feature
**Files:** create `app/v99_volume_gate.py`; test `tests/test_v990_volume_gate.py`.
**Interfaces:** `build_abnormal_turnover(frame) -> DataFrame` with `abnormal_futstk_volume`; `trial20_spec() -> dict`.
- [ ] Write leakage and construction tests first.
- [ ] Verify RED.
- [ ] Implement lagged 20/60 means, weekday/trend residualization, prior-60 residual SD standardization.
- [ ] Verify GREEN.

### Task 3: Rolling OOS HAR gate and robustness
**Files:** modify `app/v99_volume_gate.py`; test `tests/test_v990_volume_gate.py`.
**Interfaces:** `evaluate_trial20(symbol_frames, earnings_map=None, min_train_dates=252, refit_every=20) -> dict`.
- [ ] Write synthetic PASS/FAIL tests for MSE, QLIKE, Clark-West, OOS R2, chronological and top-day checks.
- [ ] Verify RED.
- [ ] Implement expanding-date OOS HAR and HAR+Volume forecasts, QLIKE/MSE, Clark-West, pooled two-way clustered diagnostics, same-day/same-DTE event diagnostics, earnings split, and concentration checks.
- [ ] Verify GREEN.

### Task 4: V9.9 runner/state/API integration
**Files:** modify `app/backtest.py`, `app/web.py`; test `tests/test_v990_runner.py`.
**Interfaces:** `run_v99_trial20`, `start_v99_trial20`, `get_v99_trial20_state`, `/api/v99/start`, `/api/v99/status`.
- [ ] Write failing integration tests proving separate V9.9 state and Trial-18 lock.
- [ ] Verify RED.
- [ ] Reuse official archive/cash/earnings loaders without MWPL dependency; build frames with total notional turnover.
- [ ] Verify GREEN.

### Task 5: Backtest UI and release markers
**Files:** modify `app/templates/backtest.html`, `RESEARCH_BUILD.txt`, `PRODUCTION_BUILD.txt`; create `V9_9_CHANGELOG.md`; test `tests/test_v990_ui_release.py`.
**Interfaces:** Show preregistration, PASS/FAIL/INCONCLUSIVE, OOS MSE/QLIKE, Clark-West, OOS R2, stability, top-day sensitivity, earnings/same-DTE diagnostics; leave OI sections visible.
- [ ] Write failing UI/release-marker tests.
- [ ] Verify RED.
- [ ] Implement V9.9 panel and JS polling/rendering.
- [ ] Verify GREEN.

### Task 6: Regression verification and package
**Files:** all changed files; output ZIP.
- [ ] Run `python -m pytest -q`.
- [ ] Run `python -m compileall -q app`.
- [ ] Validate build markers and ZIP contents.
- [ ] Package deployable source ZIP.
