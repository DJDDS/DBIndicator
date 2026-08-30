# V9 Professional Playbook Scanner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and verify the V9 six-playbook Bull/Bear F&O screener, live dashboard, alerts, and evidence-locked historical playbook backtest while retaining V8.2 derivative intelligence as the option-expression layer.

**Architecture:** Add a focused `app/v9_playbooks.py` domain module. Extend historical price features only with confirmation/failure facts needed by V9, generate V9 events during replay, aggregate them into a separate playbook report, and bridge live V9 decisions onto existing shortlist/alert fields. Keep legacy V8/V6 diagnostics callable but outside the primary V9 path.

**Tech Stack:** Python 3, pandas/numpy, Flask/Jinja, pytest, existing Kite/Marketaux integration.

**Spec:** `docs/superpowers/specs/2026-08-30-v9-professional-playbook-design.md`

## Global Constraints
- Primary protocol: NSE stock F&O, 15-minute, 180 calendar days.
- Bullish and bearish playbooks are independent.
- 4H context only; no 4H hard entry gate.
- V8.2 Derivative Intelligence cannot change the underlying V9 rank.
- No fabricated historical catalyst/news backtest.
- Final 20% remains locked.

---

### Task 1: V9 playbook classification module
**Files:** Create `app/v9_playbooks.py`; Test `tests/test_v9_playbooks.py`.
- [ ] Write failing tests for Opening Drive, Pullback/Reclaim, Fresh Short Buildup, Failed Breakout, VWAP Retest Failure, live real-catalyst classification, anti-chase, and independent Bull/Bear directions.
- [ ] Run tests and verify RED.
- [ ] Implement minimal deterministic playbook scoring/classification.
- [ ] Run tests and verify GREEN.

### Task 2: Historical confirmation/failure facts and V9 replay events
**Files:** Modify `app/stock_in_play.py`, `app/early_research.py`; Test `tests/test_v9_research.py`.
- [ ] Write failing tests proving failed bullish breakout is known only on the next bar; retest/reclaim entries occur only after confirmation; fast V9 replay emits playbook events without legacy V6 work.
- [ ] Verify RED.
- [ ] Implement point-in-time failure fields and dedicated V9 event generation.
- [ ] Verify GREEN.

### Task 3: Evidence-locked V9 report
**Files:** Modify `app/early_research.py`; Test `tests/test_v9_research.py`.
- [ ] Write failing tests for per-playbook horizons, validation blocks, locked final, and catalyst LIVE/SHADOW status.
- [ ] Verify RED.
- [ ] Implement `v9_playbook_report` and integrate into fast aggregation.
- [ ] Verify GREEN.

### Task 4: Live V9 shortlists, catalyst enrichment, and option expression
**Files:** Modify `app/background.py`, `app/news.py`; Test `tests/test_v9_live.py`.
- [ ] Write failing tests showing V9—not V8.1—populates shortlist ranks; live catalyst uses cached/matched news; derivative intelligence runs after V9 shortlist.
- [ ] Verify RED.
- [ ] Implement V9 live application and shortlist bridge.
- [ ] Verify GREEN.

### Task 5: Dynamic professional V9 dashboard and backtest UI
**Files:** Modify `app/web.py`, `app/templates/index.html`, `app/templates/backtest.html`; Test `tests/test_v9_ui.py`.
- [ ] Write failing tests for V9 labels, playbook names, V9 endpoint payload, Run V9 button, and absence of V8.1 operational copy on the main console.
- [ ] Verify RED.
- [ ] Implement UI/API changes while retaining legacy diagnostic navigation.
- [ ] Verify GREEN.

### Task 6: Build markers, docs, full verification and package
**Files:** Modify `PRODUCTION_BUILD.txt`, `RESEARCH_BUILD.txt`, `README.md`; Create `V9_CHANGELOG.md`.
- [ ] Run full test suite with `PYTHONPATH=. pytest -q`.
- [ ] Run Python compile checks and Jinja parse checks.
- [ ] Package ZIP, extract it fresh, rerun full suite against extracted ZIP, and run ZIP integrity check.
