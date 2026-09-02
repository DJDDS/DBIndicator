# V9.6.2 Trial 17 Promotion Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add promotion-only earnings, matched-baseline, market-regime, and two-way-cluster controls to frozen Trial 17 without changing its signal or evidence window.

**Architecture:** Add two small NSE data clients (financial-result filing dates and market-regime history), extend Trial-17 statistics with deterministic matched-baseline and two-way clustered covariance, and expose a separate promotion report that can unlock Trial 18 only when every declared control passes. The frozen Trial-17 result remains unchanged and separately visible.

**Tech Stack:** Python 3.11, pandas, numpy, requests, Flask/Jinja, pytest.

**Spec:** `docs/superpowers/specs/2026-09-02-v962-promotion-controls-design.md`

## Global Constraints
- Keep `total OI z >= 1.5` unchanged.
- Keep evidence dates 2021-09-01 through 2023-09-01 unchanged.
- Keep primary endpoint 1D horizon ATR and secondary 2D non-rescuing.
- Do not read prior locked finals.
- Do not activate any production playbook.
- Missing promotion data must fail closed; never fabricate ATM IV, earnings dates, or market regime values.

---

### Task 1: Promotion statistics
**Files:**
- Modify: `app/v96_trial17.py`
- Test: `tests/test_v962_promotion_stats.py`

**Interfaces:**
- Produces `same_day_matched_report(events, baseline, field, reps=...)`, `dte_matched_report(...)`, `two_way_cluster_robust_ols(...)`, and `evaluate_promotion_controls(...)`.

- [ ] Write tests showing same-day matching removes cross-day regime contamination, DTE matching preserves bucket composition, and two-way clustering returns the total_z coefficient/t-stat.
- [ ] Run the new test file and verify RED.
- [ ] Implement the minimal deterministic statistics.
- [ ] Run the new test file and verify GREEN.

### Task 2: NSE earnings calendar
**Files:**
- Create: `app/nse_earnings_history.py`
- Test: `tests/test_v962_earnings_history.py`

**Interfaces:**
- Produces `NSEEarningsHistoryClient.fetch_symbol(symbol, start, end)` and `build_earnings_map(symbols, start, end, client)`.

- [ ] Write parser/client tests for NSE `corporates-financial-results` rows using `broadcastDate`/`filingDate` and symbol filtering.
- [ ] Run tests and verify RED.
- [ ] Implement cached NSE fetch with fail-closed coverage metadata.
- [ ] Run tests and verify GREEN.

### Task 3: NSE market regime history
**Files:**
- Create: `app/nse_market_regime.py`
- Test: `tests/test_v962_market_regime.py`

**Interfaces:**
- Produces `NSEMarketRegimeClient.fetch(start, end)` returning date-indexed `india_vix`, `nifty_close`, `nifty_rv20_prev` and coverage metadata.

- [ ] Write parser/client tests for `/api/historical/vixhistory` and `/api/historical/indicesHistory` payload shapes.
- [ ] Run tests and verify RED.
- [ ] Implement chunked cached official NSE fetch and lagged NIFTY realized-vol calculation.
- [ ] Run tests and verify GREEN.

### Task 4: Runner integration and promotion gate
**Files:**
- Modify: `app/backtest.py`
- Modify: `app/v96_trial17.py`
- Test: `tests/test_v962_runner_promotion.py`

**Interfaces:**
- Trial-17 frames gain earnings-distance and market-regime columns only for promotion analysis.
- Result gains `promotion_controls`, `promotion_status`, and `trial18_eligible`.

- [ ] Write runner tests with injected earnings/regime fixtures and integrity controls.
- [ ] Verify RED.
- [ ] Wire data loading, +/-5 trading-session exclusion, matched reports, two-way regression, and fail-closed promotion rule.
- [ ] Verify GREEN.

### Task 5: Backtest UI and release markers
**Files:**
- Modify: `app/templates/backtest.html`
- Modify: `README.md`
- Modify: `PRODUCTION_BUILD.txt`
- Modify: `RESEARCH_BUILD.txt`
- Create: `V9_6_2_CHANGELOG.md`
- Test: `tests/test_v962_ui_release.py`

**Interfaces:**
- UI shows frozen Trial-17 verdict separately from `Trial 18 promotion gate`.

- [ ] Write UI/release tests requiring the new build marker and all promotion-control labels.
- [ ] Verify RED.
- [ ] Implement the card/output and release metadata.
- [ ] Verify GREEN.

### Task 6: Full verification and packaging
**Files:** release tree only.

- [ ] Run complete pytest set in isolated batches if the known teardown leak appears.
- [ ] Compile all Python modules.
- [ ] Render and syntax-check dashboard/backtest JavaScript.
- [ ] Build cache-free ZIP excluding secrets/runtime artifacts.
- [ ] Extract ZIP to a fresh directory and repeat full tests/compile/JS checks.
