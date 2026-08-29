# V8 Dual Alpha Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an evidence-ranked dual bullish/bearish NSE F&O scanner, historical V8 backtest report, and dynamic professional dashboard.

**Architecture:** Add a pure `app/v8_dual.py` scoring module consumed by both live background enrichment and historical event aggregation. Extend the existing research pipeline with timestamp-level cross-sectional ranks and a fixed V8 ablation/validation report. Add a compact dashboard JSON endpoint plus client-side rendering for Bull/Bear leader cards while keeping legacy diagnostics intact below.

**Tech Stack:** Python 3, Flask, pandas/numpy, Jinja2, vanilla browser JavaScript/CSS, pytest.

**Spec:** `docs/superpowers/specs/2026-08-29-v8-dual-alpha-design.md`

## Global Constraints
- Signal generation is 15-minute Recent-Range only; 4H is context only.
- Bull and Bear are independent and must be reported separately.
- No fitted weights or parameter grid.
- TRADE threshold alpha >= 85, participation >= 70, max extension 1.25 ATR.
- Preserve all existing endpoints/features unless explicitly superseded by V8 presentation.
- Historical depth is never invented.

---

### Task 1: Pure V8 directional scoring
**Files:** Create `app/v8_dual.py`; Test `tests/test_v8_dual.py`.
**Interfaces:** Produce `classify_oi_state`, `score_directional_row`, `rank_cross_section` and `build_live_leaderboards`.
- [ ] Write failing tests for Bull/Bear directional CLV, OI-state asymmetry, median consensus, TRADE/WATCH thresholds, missing evidence neutrality, and independent leaderboards.
- [ ] Run `python -m pytest tests/test_v8_dual.py -q` and confirm RED.
- [ ] Implement minimal pure scoring/ranking functions.
- [ ] Re-run V8 tests and confirm GREEN.

### Task 2: Live scanner integration
**Files:** Modify `app/background.py`; Test `tests/test_v8_live.py`.
**Interfaces:** `background._apply_v8_dual_alpha(results, index_chg_pct, sector_contexts)` attaches `v8_*` fields and ranks.
- [ ] Write failing integration tests for live cross-sectional ranking and independent bull/bear candidates.
- [ ] Confirm RED.
- [ ] Add V8 enrichment after existing V6 context/basis enrichment, preserving V6 fields for diagnostics.
- [ ] Confirm GREEN.

### Task 3: Historical V8 report
**Files:** Modify `app/early_research.py`, `app/backtest.py`; Test `tests/test_v8_research.py`.
**Interfaces:** `early_research.v8_dual_report(events)` returns Bull/Bear development, validation, locked final, ablations, chronological blocks, and benchmark checks.
- [ ] Write failing tests using synthetic chronological events that prove Bull/Bear separation and fixed ablations.
- [ ] Confirm RED.
- [ ] Add timestamp-level cross-sectional rank enrichment to research events and aggregate V8 report.
- [ ] Confirm GREEN.

### Task 4: Dynamic professional dashboard API
**Files:** Modify `app/web.py`; Test `tests/test_v8_dashboard_api.py`.
**Interfaces:** `/api/v8-dashboard` returns last scan, market state, bull leaders, bear leaders, and compact component evidence.
- [ ] Write failing endpoint/serialization tests.
- [ ] Confirm RED.
- [ ] Add endpoint using current scanner state and V8 leaderboards.
- [ ] Confirm GREEN.

### Task 5: Dynamic professional dashboard UI
**Files:** Modify `app/templates/index.html`; Test `tests/test_v8_ui.py`.
**Interfaces:** DOM IDs `v8-bull-leaders`, `v8-bear-leaders`, `v8-dashboard-status`, `v8-view-intraday`, `v8-view-swing`; JS polls `/api/v8-dashboard`.
- [ ] Write failing markup/static-contract tests.
- [ ] Confirm RED.
- [ ] Add professional decision-console markup/CSS and client rendering with stale/error handling.
- [ ] Confirm GREEN.

### Task 6: Backtest page V8 section
**Files:** Modify `app/templates/backtest.html`; Test `tests/test_v8_ui.py`.
**Interfaces:** Render V8 Bull/Bear validation and fixed ablation tables when `research.v8_dual` exists.
- [ ] Add failing template-contract tests.
- [ ] Confirm RED.
- [ ] Add V8 section before legacy V6 diagnostics.
- [ ] Confirm GREEN.

### Task 7: Documentation, build ID, verification and package
**Files:** Modify `README.md`, `RESEARCH_BUILD.txt`; Create `V8_CHANGELOG.md`.
- [ ] Update docs to describe V8 dual-engine rules and dashboard.
- [ ] Run `python -m pytest -q`.
- [ ] Run `python -m compileall -q app tests`.
- [ ] Package ZIP excluding caches and verify `unzip -t`.
- [ ] Extract packaged ZIP to a fresh directory and run the full test suite from the packaged contents.
