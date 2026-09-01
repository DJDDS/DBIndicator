# V9.3 Anticipation Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a research-only Component Edge Laboratory and pre-registered Silent-OI-to-Ignition Trial 13, repair 4H diagnostics, retire the dedicated overnight workflow, and add auditable 1D/2D swing research.

**Architecture:** Extend the existing 15-minute feature/replay pipeline with compact component events, aggregate them in a separate V9.3 research module, and render results beside the frozen V9.2 diagnostics. Daily continuous OI is mapped only after session completion; production playbook activation remains unchanged. 4H remains a dedicated completed-candle diagnostic with 15m execution.

**Tech Stack:** Python, pandas, NumPy, Flask/Jinja, vanilla JavaScript, pytest.

**Spec:** `docs/superpowers/specs/2026-08-31-v93-anticipation-research-design.md`

## Global Constraints
- Keep `ACTIVE_PLAYBOOKS = ()`.
- Keep 1.25 ATR anti-chase and 0.18% round-trip research friction unchanged.
- Do not retune rejected Bear FSB or unlock any final 20% sample.
- Never fabricate missing intraday OI, historical MWPL, option-chain history, or point-in-time F&O membership.

---

### Task 1: Component statistics and Trial 13
**Files:** Create `app/v93_component_lab.py`; Test `tests/test_v930_component_edge_lab.py`.
- [x] Write failing tests for fixed Trial-13 specification, direction statistics, movement lift and locked final sample.
- [x] Implement point-in-time daily OI mapping, directional/movement statistics and report builder.
- [x] Add day-cluster return confidence interval and whole-session 60/20/20 split.
- [x] Add independent OI acceleration, TOD RVOL, Coil, relative direction, VWAP, scaled ATR and anti-chase component rows.

### Task 2: Feature/replay integration
**Files:** Modify `app/early_research.py`; Test `tests/test_v930_component_edge_lab.py`.
- [x] Add 60-minute displacement in ATR and point-in-time daily OI features.
- [x] Emit independent Long/Short buildup, silent-OI, compression, daily-OI and baseline events.
- [x] Link first ignition within four bars to Silent OI and absolute NIFTY regime.
- [x] Preserve next-bar execution and existing friction.

### Task 3: Streaming/restart integrity
**Files:** Modify `app/backtest.py`; Test `tests/test_v930_component_edge_lab.py`, `tests/test_v929_pipeline_audit.py`.
- [x] Preserve V9.3 compact event payloads, including ATR%.
- [x] Fetch daily continuous OI for V9.3 research.
- [x] Persist historical coverage through Stage-2 checkpoint.
- [x] Retain full-universe single-load Stage-2 ranking architecture.

### Task 4: API and research UI
**Files:** Modify `app/web.py`, `app/templates/backtest.html`; Test `tests/test_v930_component_edge_lab.py`.
- [x] Add `v93_lab` mode fixed to full F&O 15-minute/180-day research.
- [x] Add Run V9.3 Anticipation Lab control.
- [x] Render Trial 13, independent directional components and directionless precursor lift.
- [x] Keep explicit research/shadow and final-lock labels.

### Task 5: 4H repair and legacy research cleanup
**Files:** Modify `app/web.py`, `app/templates/backtest.html`; Test `tests/test_v61_timeframe_research.py`, `tests/test_v930_product_scope.py`.
- [x] Give 4H Diagnostic a dedicated fixed mode, 4H setup and 15m execution.
- [x] Remove the old gate-sweep UI and replace it with Component Edge Laboratory workflow.
- [x] Remove dedicated overnight-test UI/API and user-facing BTST/STBT terminology.

### Task 6: 1D/2D Swing Research + forward validation
**Files:** Modify `app/oi_view.py`, `app/opportunity_forward.py`, `app/background.py`, `app/web.py`, `app/templates/index.html`; Test `tests/test_v930_product_scope.py`.
- [x] Add one-horizon-per-symbol 1D/2D Swing Research / Shadow console.
- [x] Add 2D live maturation on the second later trading session.
- [x] Persist research_horizon into first-seen forward events.
- [x] Summarize and render routed 1D/2D net expectancy and PF.

### Task 7: Build metadata, docs and full verification
**Files:** Modify build markers/README; Create `V9_3_CHANGELOG.md`.
- [x] Update build IDs and historical trial count to 13.
- [x] Update design/plan/changelog/README.
- [x] Run complete pytest suite, 210×5000 Stage-2 stress test, Python compile, browser-script syntax check and archive integrity check.
- [x] Package and re-verify one deployable V9.3.0 ZIP.
