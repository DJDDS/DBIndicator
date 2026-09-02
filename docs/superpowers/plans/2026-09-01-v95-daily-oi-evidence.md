# V9.5 Daily OI Evidence Lab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a research-only 3-year daily-OI evidence engine that validates whether unexpected daily OI contains independent 1D/2D movement information before any new directional strategy is allowed.

**Architecture:** A new `app/v95_daily_evidence.py` owns point-in-time daily feature construction, development-fitted expected-OI residuals, clustered inference, integrity gates, and Trial 15 reporting. `app/backtest.py` gets a separate memory-bounded daily runner and background state. `app/templates/backtest.html` renders V9.5 independently of V9.4, which remains unchanged as the audit trail.

**Tech Stack:** Python 3, pandas, NumPy, Flask, existing KiteConnect fetch helpers, pytest, vanilla HTML/JS.

**Spec:** `docs/superpowers/specs/2026-09-01-v95-daily-oi-evidence-design.md`

## Global Constraints
- `ACTIVE_PLAYBOOKS = ()` must remain unchanged.
- Trial 13 final 20% remains unread.
- Trial 14 remains failed as preregistered and is not retuned.
- Trial 15 primary horizon is 1D; 2D cannot rescue 1D.
- No fabricated historical ATM IV, MWPL, ban, membership, lot-size, or corporate-action data.
- Missing load-bearing controls force an explicit INCONCLUSIVE status.
- V9.5 uses daily history and bypasses V9.4 15-minute cross-sectional ranking.

---

### Task 1: Trial 15 evidence math

**Files:**
- Create: `app/v95_daily_evidence.py`
- Test: `tests/test_v950_daily_evidence.py`

**Interfaces:**
- Produces `trial15_spec() -> dict`
- Produces `build_symbol_daily_frame(price_df, oi_series, expiry_dates=None, ban_series=None, mwpl_series=None) -> pd.DataFrame`
- Produces `fit_expected_oi_model(dev_frame) -> dict`
- Produces `apply_expected_oi_model(frame, model) -> pd.DataFrame`
- Produces `evaluate_trial15(symbol_frames, controls) -> dict`

- [ ] Write failing tests proving the build ID/spec, no look-ahead features, Sep-2025 expiry-regime handling, development-only expected-OI model, final-mask behavior, 2D non-rescue, and fail-closed missing-control status.
- [ ] Run `PYTHONPATH=. pytest tests/test_v950_daily_evidence.py -q` and confirm RED.
- [ ] Implement the smallest daily feature/model/report helpers needed by those tests using NumPy/pandas only.
- [ ] Run the focused test and confirm GREEN.

### Task 2: Clustered statistics and robustness

**Files:**
- Modify: `app/v95_daily_evidence.py`
- Modify: `tests/test_v950_daily_evidence.py`

**Interfaces:**
- Produces `day_cluster_bootstrap_lift(events, baseline, horizon, reps=1000, seed=950) -> dict`
- Produces `cluster_robust_ols(y, x, clusters) -> dict`

- [ ] Add failing tests for deterministic day-cluster bootstrap, clustered OLS/t-stat, volatility-quartile reporting, top-3-day removal and chronological-block stability.
- [ ] Run focused tests and confirm RED.
- [ ] Implement deterministic cluster resampling and one-way cluster-robust OLS covariance without adding dependencies.
- [ ] Run focused tests and confirm GREEN.

### Task 3: Memory-bounded 3-year daily runner

**Files:**
- Modify: `app/backtest.py`
- Test: `tests/test_v950_daily_runner.py`

**Interfaces:**
- Produces `run_v95_daily_oi_evidence(kite, symbols=None, days=1095, progress_cb=None, integrity_data=None) -> dict`
- Uses existing `scanner_mod.fetch_oi_history(... timeframe='day', days_override=days)` and daily cash-history fetches.

- [ ] Add failing tests with a fake Kite client proving daily-only fetches, 1095-day default, per-symbol progress, no V9.4 rank builder call, bounded symbol-frame aggregation, and honest missing-control metadata.
- [ ] Run focused tests and confirm RED.
- [ ] Implement daily price/OI fetching, per-symbol transformation, cleanup via `research_runtime.release_memory_pressure()`, and Trial 15 aggregation.
- [ ] Run focused tests and confirm GREEN.

### Task 4: Background API and isolated UI

**Files:**
- Modify: `app/backtest.py`
- Modify: `app/web.py`
- Modify: `app/templates/backtest.html`
- Test: `tests/test_v950_ui.py`

**Interfaces:**
- `POST /api/v95/start`
- `GET /api/v95/status`
- separate in-memory/background state from V9.4 jobs

- [ ] Add failing UI/API tests for V9.5 card text, Trial 15/16 lock copy, start/status endpoints, research-only copy, and V9.4 preservation.
- [ ] Run focused tests and confirm RED.
- [ ] Implement isolated background state/endpoints and report rendering.
- [ ] Run focused tests and confirm GREEN.

### Task 5: Build markers, documentation, and regression safety

**Files:**
- Modify: `RESEARCH_BUILD.txt`
- Modify: `PRODUCTION_BUILD.txt`
- Modify: `README.md`
- Create: `V9_5_CHANGELOG.md`
- Test: `tests/test_v950_release_integrity.py`

**Interfaces:**
- Research build: `2026-09-01-INSTITUTIONAL-V9.5.0-DAILY-OI-EVIDENCE`
- Production playbook state remains unchanged.

- [ ] Add release-integrity tests proving build marker consistency, `ACTIVE_PLAYBOOKS = ()`, Trial 13/14 preservation, and no V9.5 production signal path.
- [ ] Run focused tests and confirm RED.
- [ ] Update markers/docs only after the behavior is implemented.
- [ ] Run focused tests and confirm GREEN.

### Task 6: Full verification and clean package

**Files:**
- Package all deployment files except runtime/cache/secrets.

- [ ] Run `PYTHONPATH=. pytest -q` and require zero failures.
- [ ] Run `python -m compileall -q app tests` and require exit 0.
- [ ] Run JavaScript syntax extraction/check for inline Backtest JS.
- [ ] Build a ZIP excluding `.env`, `.dbindicator-research`, `.pytest_cache`, `__pycache__`, `.pyc`, and local work/docs caches not needed at runtime.
- [ ] Extract ZIP into a fresh directory and rerun full pytest + compile checks there.
- [ ] Provide the verified ZIP and checksum.
