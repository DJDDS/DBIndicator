# V11.1 Development & Feasibility Lab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a development-only V11.1 laboratory that compares exactly two momentum candidates, measures realistic risk/cost inputs, and refuses Trial 25 unless the frozen design has at least 80% prospective joint power.

**Architecture:** Keep historical V11.0/Trial-24 code and verdict intact. Add a new V11.1 development module for point-in-time scores, portfolio construction, lagged-volatility de-risking, turnover/cost accounting, diagnostics, execution-coverage auditing, and candidate selection. Extend the feasibility module with a V2 explicit-power contract while preserving the old historical gate for ledger reproducibility. Add a separate V11.1 durable runner/API/UI so Trial 24 cannot be rerun and the final 31 months remain physically inaccessible.

**Tech Stack:** Python 3.13, pandas, numpy, Flask/Jinja, pytest, existing NSE archive clients.

**Spec:** `docs/superpowers/specs/2026-09-04-v11-1-development-feasibility-lab-design.md`

## Global Constraints

- Development window is fixed at 2010-01 through 2023-05.
- Trial-24 final 31 months remain unread and inaccessible to V11.1 candidate evaluation.
- Exactly two alpha families: residual 12-1 momentum and conventional 12-1 price momentum.
- Primary scaler is de-risk-only: `min(1.0, target_vol / lagged_forecast_vol)`; never lever above 1x.
- Residual target annual volatility is fixed at 12.49%; no target search.
- Measured turnover drives headline cost; Trial-24 0.36% stress cost remains a separate diagnostic.
- Trial 25 is not run or registered automatically.
- Production activation is always false.
- Registration eligibility requires at least 80% prospective joint power.

---

### Task 1: Feasibility Gate V2

**Files:**
- Modify: `app/research_feasibility.py`
- Create: `tests/test_v111_feasibility_gate.py`

**Interfaces:**
- Produces `assess_pretrial_feasibility_v2(...) -> dict` with explicit `minimum_power`, `primary_power`, `joint_power`, provenance fields, and `GO_REGISTER_PREREGISTERED_TRIAL` / `DO_NOT_RUN_UNDERPOWERED`.
- Produces `select_target_sigma(...)` so measured development volatility supersedes external volatility.
- Produces `estimate_joint_battery_power(...)` using one joint Monte-Carlo path per simulated sample and a caller-supplied battery function; it never multiplies marginal powers.

- [ ] Write failing tests for old-MDE false authorization, volatility provenance precedence, and dependent joint-power simulation.
- [ ] Run those tests and verify expected failures.
- [ ] Implement the minimal V2 functions without changing historical `assess_pretrial_feasibility` semantics.
- [ ] Run tests to green.

### Task 2: Point-in-Time Candidate Scores and Holdout Firewall

**Files:**
- Create: `app/v111_development.py`
- Create: `tests/test_v111_candidate_scores.py`

**Interfaces:**
- `development_only_inputs(...)` validates every frame ends no later than 2023-05 and rejects later candidate-return data.
- `compute_residual_momentum_scores(...)` delegates the frozen Trial-24 score construction.
- `compute_price_momentum_scores(...)` forms 12-1 cumulative price momentum using only t-12..t-2 information and point-in-time membership.
- Both return the same `date,symbol,score,decile` schema.

- [ ] Write failing tests proving future mutations cannot change earlier scores and any frame extending into the locked period is rejected.
- [ ] Run red tests.
- [ ] Implement minimal score/firewall functions.
- [ ] Run green tests.

### Task 3: Portfolio Weights, Lagged Volatility Scaling, Turnover, and Costs

**Files:**
- Modify: `app/v111_development.py`
- Create: `tests/test_v111_portfolio_economics.py`

**Interfaces:**
- `scores_to_weights(scores)` returns monthly 200%-gross equal-weight long/short weights.
- `portfolio_gross_returns(weights, monthly_returns)` uses next month only.
- `lagged_realized_vol_forecast(gross_returns, lookback_months=12)` uses strictly prior monthly returns and annualizes by sqrt(12).
- `derisk_exposure(forecast_vol, target_vol=0.1249)` returns [0,1].
- `apply_exposure(...)` uses exposure known before the outcome month.
- `portfolio_turnover(weights)` computes traded notional from changes in scaled weights.
- `apply_measured_costs(gross, turnover, per_turnover_cost)` returns separate gross, measured-cost net, and stress-net series.

- [ ] Write red tests for <=1x scaler, lag-only information, exact weight-change turnover, and separate cost fields.
- [ ] Implement minimal economics functions.
- [ ] Run green tests.

### Task 4: Development Metrics, Power Projection, and Winner Hierarchy

**Files:**
- Modify: `app/v111_development.py`
- Create: `tests/test_v111_candidate_selection.py`

**Interfaces:**
- `candidate_metrics(...)` reports annualized mean, vol, Sharpe, Sortino, max drawdown, worst month, skew, kurtosis, CVaR5, turnover mean/median, complete months, block diagnostics.
- `project_confirmatory_power(...)` calls Feasibility Gate V2 with development-measured volatility/effect and a declared untouched validation-month count.
- `select_development_winner(a,b)` implements the frozen hierarchy: integrity -> positive measured-cost return -> >=80% joint power -> higher Sharpe -> lower turnover -> no winner.

- [ ] Write red tests for no-winner when both are underpowered, higher-Sharpe selection when both pass, and lower-turnover tie-break.
- [ ] Implement minimal metrics/power/selection.
- [ ] Run green tests.

### Task 5: FUTSTK Execution Coverage Contract

**Files:**
- Modify: `app/v11_monthly_data.py`
- Modify: `app/v111_development.py`
- Create: `tests/test_v111_futstk_coverage.py`

**Interfaces:**
- Month-end snapshot builder preserves compact contract metadata needed for coverage only: symbol, expiry, lot size, settle/close availability.
- Frozen coverage rule: nearest non-expired FUTSTK contract at the month-end trading date; price field `settle` if positive else `close`; monthly roll; missing lot/contract/price fails coverage.
- `audit_futstk_execution_coverage(...)` reports coverage and `FUTSTK_EXECUTION_COVERAGE_INSUFFICIENT` without computing futures P&L or silently substituting cash returns.

- [ ] Write red tests for missing-contract fail-closed and frozen rule metadata.
- [ ] Implement coverage-only contract metadata path.
- [ ] Run green tests.

### Task 6: V11.1 Development Runner and Trial-24 Record Restatement

**Files:**
- Create: `app/v111_lab.py`
- Modify: `app/backtest.py`
- Create: `tests/test_v111_runner.py`

**Interfaces:**
- `run_development_lab(inputs, ...)` evaluates exactly the two candidates and returns `DEVELOPMENT_ONLY_NO_TRIAL25_YET` plus candidate metrics, power provenance, coverage, winner eligibility, Trial-24 restatement, `final_read=False`, `production_activation=False`.
- `start_v111_development_lab()` creates a durable background job separate from the historical Trial-24 state.
- `get_v111_development_state()` exposes progress/result.

- [ ] Write red tests proving final read false, production false, exactly two candidates, Trial-24 verdict unchanged, and no Trial-25 auto-run.
- [ ] Implement lab orchestration and durable state.
- [ ] Run green tests.

### Task 7: API and Backtest UI

**Files:**
- Modify: `app/web.py`
- Modify: `app/templates/backtest.html`
- Create: `tests/test_v111_ui_release.py`

**Interfaces:**
- Add `/api/v111/development/start` and `/api/v111/development/status`.
- Display `DEVELOPMENT ONLY — NO TRIAL 25 YET`, exact development window, `FINAL 31 MONTHS UNREAD`, side-by-side candidates, power/provenance, turnover/cost, execution coverage, and final eligibility status.
- Historical Trial-24 card stays visible read-only; its run button is removed/disabled in V11.1.

- [ ] Write red UI/API tests.
- [ ] Implement routes and rendering.
- [ ] Run green tests and syntax-check rendered JavaScript.

### Task 8: Release Markers, Changelog, and Full Verification

**Files:**
- Modify: `RESEARCH_BUILD.txt`
- Create: `V11_1_CHANGELOG.md`
- Add/modify release tests as needed.

- [ ] Set build marker to `2026-09-04-INSTITUTIONAL-V11.1-DEVELOPMENT-FEASIBILITY-LAB`.
- [ ] Document non-goals, locked final, two-candidate-only rule, power gate, and no production activation.
- [ ] Run all V11/V111 tests.
- [ ] Run the complete regression suite in deterministic batches with `python -m pytest` and verify zero failures.
- [ ] Compile every Python file.
- [ ] Render Backtest template and run `node --check` on extracted JavaScript.
- [ ] Build a clean ZIP excluding runtime research state, cache files, bytecode, test caches, and local secrets.
- [ ] Extract ZIP into a clean directory and rerun packaged tests/compile/JS syntax/ZIP integrity.
- [ ] Compute SHA-256 and provide the deployment artifact.
