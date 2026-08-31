# V9.2 Diagnostic Reset Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add diagnostic-only Bull gate-funnel and rejected Bear FSB regime decomposition without changing any production trading rule or touching a locked final sample.

**Architecture:** Capture broad Bull long-buildup seed events before the current accumulation gates, then report cumulative survival counts through the exact frozen Bull research gates. For Bear FSB, reuse the already-revealed 60/20/20 candidate population and compare validation vs final across descriptive regimes only; no new eligibility rule is produced.

**Tech Stack:** Python, pandas/numpy, Flask/Jinja, pytest.

**Spec:** Approved in chat: V9.2 Diagnostic Reset.

## Global Constraints
- Full NSE stock-F&O, 15-minute, 180-day protocol unchanged.
- Bear FSB fingerprint/rule unchanged and permanently rejected after its final test.
- Bull final 20% remains locked.
- Diagnostics cannot alter live shortlist eligibility.
- No threshold search or parameter optimization.

---

### Task 1: Bull Gate Funnel
**Files:** Modify `app/early_research.py`, `app/v91_goal.py`; Test `tests/test_v92_diagnostics.py`.
- [ ] Add failing test proving price-up/OI-up seed rows survive capture even before VWAP/RVOL gates.
- [ ] Add failing test for cumulative funnel counts: seed → long buildup → VWAP → TOD → participation → relative → derivatives → CLV → basis → consensus → qualified.
- [ ] Implement broad diagnostic seed capture and report helper.
- [ ] Verify tests.

### Task 2: Bear Regime Decomposition
**Files:** Modify `app/v91_goal.py`, `app/early_research.py`; Test `tests/test_v92_diagnostics.py`.
- [ ] Add failing test comparing validation vs already-revealed final FSB by market regime, basis sign, sector-relative sign, time bucket, and OI-strength bucket.
- [ ] Implement descriptive cohort stats with N/avg/PF only; do not emit a new trade rule.
- [ ] Verify tests.

### Task 3: Backtest UI + Build Markers
**Files:** Modify `app/templates/backtest.html`, `app/v91_goal.py`, `app/early_research.py`, docs/build markers; Test `tests/test_v92_diagnostics.py`.
- [ ] Add failing rendered-page assertions for Bull Gate Funnel and Bear Validation-vs-Final Regime tables.
- [ ] Render diagnostic sections and explicit “diagnostic only / no retuning” copy.
- [ ] Update build markers to V9.2 Diagnostic Reset.
- [ ] Run full tests, compile, rendered JS syntax, ZIP integrity.
