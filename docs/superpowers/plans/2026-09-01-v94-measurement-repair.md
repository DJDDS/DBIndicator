# V9.4 Measurement Repair + Trial Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve Trial 13 without touching its final 20%, repair V9.3 measurement faults, centralize research cost accounting, add executable ATM-straddle forward accounting, and pre-register Trial 14 as a magnitude hypothesis.

**Architecture:** Keep the existing V9.3 research pipeline and resume schema, but upgrade the report layer to V9.4. Daily point-in-time OI and compression become the primary magnitude-research inputs. Option forward validation records executable ask-entry/bid-exit straddle prices; historical option data is not fabricated.

**Tech Stack:** Python, pandas, NumPy, Flask/Jinja templates, pytest.

**Spec:** `/mnt/data/DBIndicator-Model-Validation-Audit_2.pdf`

## Global Constraints

- Trial 13 final 20% remains locked and outcome-inaccessible.
- Trial 14 primary horizon is 1D; 2D is secondary/exploratory.
- Intraday OI coverage limitation remains explicit; daily OI is preferred for Trial 14.
- Research/shadow only; no production playbook activation.
- OI Screener remains intact.
- Existing V9.3.4 resume-shard schema remains reusable.

---

### Task 1: Trial 13 pre-final resolution
**Files:** `app/v93_component_lab.py`, `tests/test_v940_measurement_repair.py`
- [ ] Add failing tests for pre-final dev+validation decomposition and final lock.
- [ ] Add payoff decomposition, top-trade sensitivity, and day-cluster bootstrap CI.
- [ ] Verify final outcomes are never returned.

### Task 2: Repair VWAP and horizon-scaled movement measurement
**Files:** `app/v93_component_lab.py`, `app/early_research.py`, `tests/test_v940_measurement_repair.py`
- [ ] Add failing tests for numeric/NumPy VWAP truth normalization.
- [ ] Add daily-ATR-scaled movement and historical percentile metrics; remove meaningless fixed 1-ATR hit interpretation from 1D/2D report.
- [ ] Preserve raw 15m-ATR movement only as legacy diagnostic field.

### Task 3: Central research cost function
**Files:** `app/costs.py`, `app/stock_in_play.py`, `app/v6_edge.py`, `tests/test_v940_measurement_repair.py`
- [ ] Add failing test proving gross +1.00% becomes +0.82% under 0.08% cost + 0.05% slippage/side.
- [ ] Route primary research net-return helpers through one implementation.

### Task 4: Executable long-vol forward validator
**Files:** `app/derivative_intelligence.py`, `app/web.py`, dashboard template/tests.
- [ ] Add failing tests for ATM CE+PE ask entry and bid exit.
- [ ] Store entry/exit executable prices, IV, DTE, implied move, underlying realised move, and net straddle return.
- [ ] Report count, expectancy, PF and win rate by 1D/2D.

### Task 5: Pre-register Trial 14 and UI/report
**Files:** `app/v93_component_lab.py`, `app/templates/backtest.html`, build/changelog files, tests.
- [ ] Add fixed Trial 14 spec: daily OI anomaly + compression onset -> excess 1D movement.
- [ ] Report full-data coverage and magnitude lift with day-cluster bootstrap CI.
- [ ] Keep Trial 14 research/shadow only.

### Task 6: Full verification and package
- [ ] Run focused V9.4 tests.
- [ ] Run full pytest suite.
- [ ] Compile Python and validate rendered JS.
- [ ] Package ZIP and retest the packaged contents.
