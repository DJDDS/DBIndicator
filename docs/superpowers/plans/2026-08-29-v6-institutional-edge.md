# V6 Institutional Edge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade DBIndicator V5 into a V6 NSE F&O intraday + 1–2D swing engine using regime, stock-in-play/catalyst proxies, turnover/leadership, price location, soft OI/basis sponsorship, 5-minute execution and path-aware exit research.

**Architecture:** Keep V5 recent-range detection and research infrastructure. Add a focused `v6_edge.py` module for pure feature/scoring/exit functions, enrich live rows cross-sectionally in `background.py`, extend futures quote/history plumbing in `scanner.py`/`backtest.py`, and add V6 research surfaces in `early_research.py` + Backtest UI. Do not make partial-coverage features hard gates.

**Tech Stack:** Python 3, pandas, NumPy, FastAPI/Jinja, Kite Connect, pytest.

**Spec:** `docs/superpowers/specs/2026-08-29-v6-institutional-edge-design.md`

## Global Constraints
- NSE stock-F&O only for live Best Entries.
- 15-minute setup, optional 5-minute execution, 4H/Daily-style context only.
- OI is sponsorship, not a universal hard gate.
- Final-test split remains hidden unless explicitly unlocked.
- No look-ahead: range, HTF, price-location, 5-minute execution and exits use only information available at decision time.
- Conservative same-bar target/stop ambiguity: stop wins.

---

### Task 1: V6 pure feature and scoring engine
**Files:** Create `app/v6_edge.py`; Test `tests/test_v6_edge.py`.
- [ ] Write failing tests for turnover percentile, catalyst proxy, market regime, price location, soft sponsorship, long/short asymmetry and 5-minute execution quality.
- [ ] Run `pytest tests/test_v6_edge.py -q` and confirm RED.
- [ ] Implement minimal pure functions.
- [ ] Run targeted tests and confirm GREEN.

### Task 2: Path-aware exits and 60/20/20 research split
**Files:** Modify `app/v6_edge.py`, `app/early_research.py`; Test `tests/test_v6_edge.py`.
- [ ] Write failing tests for conservative first-touch exits, trail/breakeven variants, three-way chronological split and locked final test.
- [ ] Verify RED.
- [ ] Implement exit simulator and split/report helpers.
- [ ] Verify GREEN.

### Task 3: Historical V6 features
**Files:** Modify `app/early_research.py`, `app/backtest.py`, `app/scanner.py`; Test `tests/test_v6_research.py`.
- [ ] Write failing tests for point-in-time price location, catalyst proxy fields, market regime, sector-relative context and partial futures-basis coverage.
- [ ] Verify RED.
- [ ] Add feature plumbing and optional near-futures history.
- [ ] Verify GREEN.

### Task 4: V6 recent-range edge lab
**Files:** Modify `app/early_research.py`; Test `tests/test_v6_research.py`.
- [ ] Write failing tests for V6 variants: recent-range long, high-turnover, catalyst, leadership/location, volume, OI, basis, sponsored stack, retained/retest, and path exits.
- [ ] Verify RED.
- [ ] Implement validation-only reports plus locked final-test payload.
- [ ] Verify GREEN.

### Task 5: Live cross-sectional V6 ranking
**Files:** Modify `app/scanner.py`, `app/background.py`, `app/stock_in_play.py`, `app/config.py`; Test `tests/test_v6_live.py`.
- [ ] Write failing tests proving OI disagreement no longer universally blocks, top-turnover/catalyst/leadership gets ranked, shorts remain research-only for swing, and depth remains shadow-only.
- [ ] Verify RED.
- [ ] Add futures last price/basis fields, basis history, cross-sectional ranks/regime, and V6 classifier.
- [ ] Verify GREEN.

### Task 6: 5-minute finalist execution
**Files:** Modify `app/background.py`, `app/scanner.py`, `app/v6_edge.py`; Test `tests/test_v6_live.py`.
- [ ] Write failing tests that only a bounded finalist set triggers 5-minute fetches and that unknown 5-minute state is neutral, not an automatic pass/fail.
- [ ] Verify RED.
- [ ] Implement top-finalist 5-minute enrichment and re-ranking.
- [ ] Verify GREEN.

### Task 7: Dashboard / Backtest UI and settings cleanup
**Files:** Modify `app/templates/index.html`, `app/templates/backtest.html`, `app/templates/settings.html`, `app/web.py`, `app/config.py`; Test `tests/test_v6_ui.py`.
- [ ] Write failing template/API tests for V6 stage labels, V6 Edge Lab, final-test lock, and removal of OI-as-hard-gate language.
- [ ] Verify RED.
- [ ] Implement UI and settings.
- [ ] Verify GREEN.

### Task 8: Full verification and release package
**Files:** Update `README.md`, `BENCHMARK_RELEASE.md`, `RESEARCH_BUILD.txt`.
- [ ] Run complete pytest suite.
- [ ] Run Python compile checks.
- [ ] Render templates and validate JavaScript syntax.
- [ ] Scan for secrets/caches.
- [ ] Package clean ZIP, extract it fresh, rerun tests/compile/syntax checks on the extracted copy.
