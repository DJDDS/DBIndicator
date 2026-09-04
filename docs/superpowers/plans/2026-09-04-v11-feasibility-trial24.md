# V11 Feasibility Competition & Trial 24 Preregistration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a no-outcome feasibility competition and, only when feasible and data-ready, expose a preregistered Trial 24 residual-momentum replication without reading the locked final holdout.

**Architecture:** V11 adds a standalone feasibility/preregistration module plus pinned IIMA factor and NSE corporate-action data loaders. Candidate A is exact residual-momentum replication; Candidate B is fixed-count cross-sectional basis and must remain blocked without an external 10–21 day prior magnitude. Trial 24 uses official point-in-time FUTSTK membership, official NSE cash month-end prices adjusted with official corporate actions, and pinned Indian FF monthly factors; data-readiness failure is fail-closed.

**Tech Stack:** Python 3, pandas, numpy, requests, Flask, existing NSE archive clients, pytest.

**Spec:** Approved V11 feasibility competition from the conversation and V10.2 audit/verification recommendations.

## Global Constraints
- No Trial 21/22 rerun and no read of their locked final 20%.
- V10.2.2 live reliability behavior must remain unchanged.
- Candidate selection is based on external prior + cost + power + data readiness only, never observed alpha outcomes.
- Trial 24 primary estimand is a monthly, fixed-gross-capital top-minus-bottom decile portfolio.
- Trial 24 factor model uses 36 completed monthly returns and the Indian market, SMB, HML factors; 12-1 residual momentum excludes the most recent month and excludes fitted alpha from the signal.
- Final 20% of Trial 24 calendar months stays unread.
- Production activation remains impossible.

---

### Task 1: V11 feasibility and preregistration core
**Files:** Create `app/v11_research.py`; Test `tests/test_v110_feasibility.py`.
- [ ] Write failing tests for candidate decisions, frozen constants, one-sided confirmatory t-bar, basis prior refusal, and no outcome dependence.
- [ ] Run focused tests and confirm RED.
- [ ] Implement minimal feasibility/preregistration core.
- [ ] Run focused tests and confirm GREEN.

### Task 2: Pinned factor and corporate-action loaders
**Files:** Create `app/iima_factors.py`, `app/nse_corporate_actions.py`; Test `tests/test_v110_data_sources.py`.
- [ ] Write parser/cache/fail-closed tests using local fixtures.
- [ ] Run focused tests and confirm RED.
- [ ] Implement pinned IIMA monthly factor loader and NSE corporate-action parser/adjuster.
- [ ] Run focused tests and confirm GREEN.

### Task 3: Trial 24 monthly replication engine
**Files:** Extend `app/v11_research.py`; Test `tests/test_v110_trial24.py`.
- [ ] Write no-lookahead residual-score, decile, month-end total-return, and final-holdout-lock tests.
- [ ] Run focused tests and confirm RED.
- [ ] Implement the monthly residual-momentum engine and confirmatory report.
- [ ] Run focused tests and confirm GREEN.

### Task 4: Background state, routes, and UI
**Files:** Modify `app/backtest.py`, `app/web.py`, `app/templates/backtest.html`; Test `tests/test_v110_release.py`.
- [ ] Write failing release/API/UI tests.
- [ ] Run and confirm RED.
- [ ] Add V11 state, feasibility endpoint, Trial 24 start/status endpoint, progress, and fail-closed button behavior.
- [ ] Run and confirm GREEN.

### Task 5: Release records and full verification
**Files:** Create `V11_CHANGELOG.md`; modify build markers/README/research ledger; package ZIP.
- [ ] Update release metadata without changing V10.2.2 live runtime marker.
- [ ] Run all regression batches, Python compile, JS syntax.
- [ ] Package with symlinks dereferenced and no runtime/cache state.
- [ ] Extract exact ZIP and rerun V11 smoke tests.
- [ ] Compute SHA-256 and hand off verified ZIP.
