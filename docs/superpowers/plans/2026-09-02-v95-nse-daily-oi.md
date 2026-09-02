# V9.5 NSE Daily OI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make official NSE daily stock-futures archives the primary 3–5 year OI source for V9.5 Trial 15 while keeping the research hypothesis frozen.

**Architecture:** Add a focused `nse_futures_history` module that downloads/caches legacy and UDiFF bhavcopies, normalizes both schemas, and streams per-day FUTSTK observations into per-symbol aggregate series. Integrate those series into the existing V9.5 runner so membership, actual expiries, near/next/far OI and the primary total-OI series come from NSE; keep Kite daily cash candles only for underlying price outcomes.

**Tech Stack:** Python 3, pandas, numpy, requests, zipfile, Flask existing app stack, pytest.

**Spec:** `docs/superpowers/specs/2026-09-02-v95-nse-daily-oi-design.md`

## Global Constraints
- Trial 15 thresholds and 60/20/20 partition remain unchanged.
- Trial 16 remains locked.
- Final 20% remains unread.
- `ACTIVE_PLAYBOOKS = ()` remains unchanged.
- Missing NSE coverage fails closed; no synthetic OI or membership data.
- Archive downloads are cached on the durable research volume.

---

### Task 1: NSE Bhavcopy parser and archive client

**Files:**
- Create: `app/nse_futures_history.py`
- Test: `tests/test_v952_nse_futures_history.py`

**Interfaces:**
- Produces: `parse_legacy_fo_bhavcopy(content, trade_date) -> DataFrame`
- Produces: `parse_udiff_fo_bhavcopy(content, trade_date) -> DataFrame`
- Produces: `NSEFuturesArchiveClient.fetch_day(day) -> DataFrame`

- [ ] Write failing tests for legacy FUTSTK parsing, UDiFF stock-futures parsing, old/new URL routing, zip extraction, and fail-closed malformed archives.
- [ ] Run the focused tests and confirm RED failures.
- [ ] Implement the schema-normalizing parser and cached archive client.
- [ ] Run the focused tests and confirm GREEN.

### Task 2: Build point-in-time symbol history from daily archives

**Files:**
- Modify: `app/nse_futures_history.py`
- Test: `tests/test_v952_nse_futures_history.py`

**Interfaces:**
- Produces: `build_symbol_histories(days, symbols, client, progress_cb=None) -> dict`
- Each symbol payload contains `total_oi`, `near_oi`, `next_oi`, `far_oi`, `membership`, `near_expiry`, `near_dte`, `lot_size`, `coverage`.

- [ ] Add failing tests proving aggregation across expiries, historical membership from contract presence, near/next/far ordering, and no forward-fill of membership across missing dates.
- [ ] Run RED.
- [ ] Implement the streaming day-wise aggregator.
- [ ] Run GREEN.

### Task 3: Wire NSE OI into frozen Trial 15

**Files:**
- Modify: `app/backtest.py`
- Modify: `app/v95_daily_evidence.py` only if a source-metadata column is required; no hypothesis changes.
- Test: `tests/test_v952_nse_runner.py`
- Modify: `tests/test_v950_daily_runner.py` expectations where the primary OI source changes.

**Interfaces:**
- `run_v95_daily_oi_evidence(..., nse_history_client=None)` uses NSE OI first.
- Fallback to Kite OI is explicitly tagged `KITE_CROSSCHECK_FALLBACK` and cannot satisfy the NSE integrity gate.

- [ ] Add failing runner tests showing NSE OI is used while Kite is called only for cash price, membership/expiry come from NSE, and insufficient NSE coverage forces INCONCLUSIVE.
- [ ] Run RED.
- [ ] Implement minimal integration.
- [ ] Run focused runner tests GREEN.

### Task 4: Complete MWPL validation integration

**Files:**
- Modify: `app/backtest.py`
- Test: `tests/test_v951_mwpl_control.py`

**Interfaces:**
- Validation dates are derived from the pre-final 80% only.
- `nse_mwpl.build_validation_mwpl_controls(...)` returns coverage metadata and maps used only for validation.

- [ ] Reproduce the two currently failing MWPL tests.
- [ ] Integrate the loader after Trial-15 partitions are known, without touching final dates.
- [ ] Confirm both MWPL tests pass.

### Task 5: UI/release metadata

**Files:**
- Modify: `app/templates/backtest.html`
- Modify: `README.md`
- Create: `V9_5_2_CHANGELOG.md`
- Modify: `RESEARCH_BUILD.txt`
- Modify: `PRODUCTION_BUILD.txt` only if the research build marker is surfaced there without enabling production.
- Test: `tests/test_v952_ui.py`

**Interfaces:**
- UI shows `NSE historical FUTSTK OI` as primary source, date coverage, archive format coverage, and whether fallback occurred.

- [ ] Add failing UI tests.
- [ ] Implement concise data-source/integrity labels and progress text.
- [ ] Run UI tests GREEN.

### Task 6: Full verification and clean packaging

**Files:**
- No production logic changes.

- [ ] Run `PYTHONPATH=. pytest -q` and require zero failures.
- [ ] Run `python -m compileall -q app tests`.
- [ ] Run JavaScript syntax extraction/check used by the existing release tests.
- [ ] Build a cache-free ZIP excluding `.env`, `.pytest_cache`, `.dbindicator-research`, `__pycache__`, and `.pyc`.
- [ ] Extract the ZIP into a new directory and rerun the complete test/compile checks there.
- [ ] Compute SHA-256 and hand off the verified ZIP.
