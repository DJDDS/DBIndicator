# V9.5.3 Trial 15 Closure + Contract Structure Lab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close Trial 15 correctly when its primary efficacy gates fail, complete historical MWPL ingestion, and add a new research-only contract-structure evidence lab without unlocking Trial 16 or production playbooks.

**Architecture:** Preserve the frozen V9.5 Trial 15 model and final holdout. Change only the verdict ordering so efficacy failures precede missing secondary controls; expand the NSE MWPL parser/fetcher across supported historical formats; add a separate contract-structure research module using near/next/far daily OI states already produced by the NSE archive. The new module reports feature evidence only and cannot produce TRADE/WATCH signals.

**Tech Stack:** Python 3, pandas, NumPy, Flask/Jinja, pytest, official NSE daily derivatives archives.

**Spec:** Approved in chat on 2026-09-02: V9.5.3 research-integrity patch + contract-structure research pivot.

## Global Constraints

- Trial 15 thresholds, 60/20/20 split and final 20% lock are immutable.
- Trial 16 remains locked and cannot auto-run.
- ACTIVE_PLAYBOOKS remains empty.
- Missing load-bearing controls may block a passing feature, but may not override an already-failed primary efficacy gate.
- Contract-structure work is RESEARCH / SHADOW only.

---

### Task 1: Trial 15 verdict hierarchy

**Files:** `app/v95_daily_evidence.py`, `tests/test_v953_trial15_closure.py`

- [ ] Write failing tests for failed 1D lift with MWPL unavailable and for a passing efficacy result with MWPL unavailable.
- [ ] Verify RED.
- [ ] Implement verdict ordering: sample → lift/CI → vol t-stat → tail → time stability → missing controls → PASS.
- [ ] Keep final holdout unread and Trial 16 locked.
- [ ] Verify GREEN.

### Task 2: Historical MWPL multi-format support

**Files:** `app/nse_futures_history.py`, `tests/test_v953_mwpl_formats.py`

- [ ] Add failing fixtures for legacy CSV and combined-open-interest aliases.
- [ ] Verify RED.
- [ ] Implement canonical parsing to `date,symbol,mwpl_pct,ban_flag,mwpl,market_oi` plus coverage metadata.
- [ ] Verify GREEN.

### Task 3: Contract-structure evidence module

**Files:** create `app/v953_contract_structure.py`; modify `app/backtest.py`; test `tests/test_v953_contract_structure.py`

- [ ] Add failing synthetic tests distinguishing fresh near creation, rollover, total expansion and abnormal unwind.
- [ ] Verify RED.
- [ ] Implement point-in-time state classification using near/next/far OI only.
- [ ] Implement 1D/2D day-clustered magnitude evidence report with no production side effects.
- [ ] Wire it into the completed V9.5 result.
- [ ] Verify GREEN.

### Task 4: UI and release markers

**Files:** `app/templates/backtest.html`, `README.md`, `RESEARCH_BUILD.txt`, `PRODUCTION_BUILD.txt`, create `V9_5_3_CHANGELOG.md`; tests `tests/test_v953_ui.py`, `tests/test_v953_release.py`

- [ ] Add failing UI/release tests for V9.5.3, Trial 15 closure, Trial 16 lock and research-only contract structure.
- [ ] Verify RED.
- [ ] Implement minimal rendering and marker updates.
- [ ] Verify GREEN.

### Task 5: Full verification and clean release

- [ ] Run `python -m pytest -q` with zero failures.
- [ ] Run Python compilation and rendered-JavaScript syntax checks.
- [ ] Create cache-free ZIP.
- [ ] Extract ZIP fresh and rerun the full checks.
- [ ] Publish SHA-256 and exact verified ZIP.
