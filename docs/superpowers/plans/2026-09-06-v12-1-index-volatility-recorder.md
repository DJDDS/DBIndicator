# V12.1 Index Volatility Recorder & Feasibility Lab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fail-soft NIFTY near-expiry WebSocket recorder, persistent/off-box archival, and a development-only remaining-session realized-variance lab without changing V12.0.1 stock-option research or unlocking Trial 25.

**Architecture:** A new `v121_index_recorder` module owns instrument selection, stream state and two-tier persistence; a separate `v121_rv_lab` module owns historical development math. `background.py` starts the independent WebSocket service beside the existing scanner, while `web.py` and `index.html` expose health/readiness only.

**Tech Stack:** Python 3.11, Flask, kiteconnect/KiteTicker 5.0.1, pandas 2.2.2, numpy 1.26.4, boto3 1.43.18, Railway persistent Volume.

**Spec:** `docs/superpowers/specs/2026-09-06-v12-1-index-volatility-recorder-design.md`

## Global Constraints

- Trial 25 stays locked and no V12.1 option-volatility signal may be labeled EXECUTABLE or VALIDATED.
- Existing V12.0.1 stock-option recorder behavior and paths remain unchanged.
- India VIX is an input, never a hard-coded 22–32% premium assumption.
- V12.1 stream/backup/lab errors must not interrupt the existing scanner.
- Current-day option data must use actual bid/ask/depth; no midpoint fill assumptions.
- Completed-day backup is optional S3-compatible and must fail soft.

---

### Task 1: Release and storage contract

**Files:**
- Modify: `app/v12_storage.py`
- Modify: `app/config.py`
- Modify: `.env.example`
- Modify: `requirements.txt`
- Modify: `RESEARCH_BUILD.txt`
- Modify: `V12_CHANGELOG.md`
- Test: `tests/test_v121_release.py`

**Interfaces:**
- Produces `resolve_v12_storage(...)["index_vol_root"]`, `index_vol_state`, `index_vol_backup_state`, `rv_lab_state`.
- Produces V12.1 configuration constants for cadence, strike steps and optional S3 backup.

- [ ] Write failing release/storage tests asserting the V12.1 build marker, new persistent paths under Railway Volume, default strike/cadence values, boto3 dependency and Trial-25 lock text.
- [ ] Run `python -m pytest tests/test_v121_release.py -q` and confirm failure.
- [ ] Implement storage/config/release metadata and add `boto3==1.43.18`.
- [ ] Re-run the test and confirm pass.

### Task 2: Instrument selection and tick normalization

**Files:**
- Create: `app/v121_index_recorder.py`
- Test: `tests/test_v121_index_recorder.py`

**Interfaces:**
- `select_index_universe(nfo_rows, nse_rows, spot, today, strike_steps=12) -> dict`
- `normalize_micro_tick(meta, tick, refs, ts) -> dict`
- `normalize_depth_tick(meta, tick, refs, ts) -> dict`

- [ ] Write failing tests with synthetic NFO/NSE instrument dumps proving nearest NIFTY expiry selection, ATM±12 strikes, CE+PE pairing, nearest NIFTY FUT, NIFTY 50 and INDIA VIX references.
- [ ] Run targeted tests and confirm failure.
- [ ] Implement deterministic selection with no fabricated fallback contracts.
- [ ] Add failing normalization tests for best bid/ask, quantities, OI/volume and five-level depth.
- [ ] Implement normalization and re-run targeted tests.

### Task 3: Two-tier writer, state and health

**Files:**
- Modify: `app/v121_index_recorder.py`
- Test: `tests/test_v121_index_recorder.py`

**Interfaces:**
- `IndexVolWriter(root, state_file, micro_seconds=5, depth_seconds=60)`
- `writer.ingest(ticks, metadata, refs, now) -> dict`
- `index_recorder_health(root, state_file, now, storage_mode) -> dict`

- [ ] Write failing cadence/path tests for 5-second micro writes, 60-second depth writes and date partitioning.
- [ ] Implement append-safe JSONL writer and atomic state updates.
- [ ] Write failing health tests for row counts, file sizes, last-write times, active expiry/ATM, storage warning and weekend MARKET_CLOSED.
- [ ] Implement health reporting and re-run tests.

### Task 4: KiteTicker service lifecycle

**Files:**
- Modify: `app/kite_auth.py`
- Modify: `app/v121_index_recorder.py`
- Modify: `app/background.py`
- Test: `tests/test_v121_stream_service.py`

**Interfaces:**
- `kite_auth.get_access_token() -> str | None`
- `IndexVolStreamService(...).run_forever()`
- `start_v121_index_stream_once()` in `background.py`

- [ ] Write failing tests using a fake ticker factory for WAITING_LOGIN, connect, subscribe FULL mode, tick ingestion, disconnect/backoff and market-closed states.
- [ ] Implement public access-token accessor and service lifecycle with dependency-injected ticker factory.
- [ ] Add scanner integration test proving stream exceptions cannot stop/alter existing V12 stock scanner state.
- [ ] Wire one daemon stream thread from `start_background_scanner()` and re-run tests.

### Task 5: Daily gzip rotation and off-box S3-compatible backup

**Files:**
- Create: `app/v121_backup.py`
- Modify: `app/v121_index_recorder.py`
- Modify: `app/background.py`
- Test: `tests/test_v121_backup.py`

**Interfaces:**
- `compress_completed_day(root, day) -> list[pathlib.Path]`
- `backup_files(paths, *, bucket, prefix, endpoint_url, region, client_factory=None) -> dict`
- `run_daily_backup_cycle(...) -> dict`

- [ ] Write failing gzip tests proving only completed-day JSONL files are compressed and originals are removed only after successful gzip creation.
- [ ] Implement deterministic gzip rotation.
- [ ] Write failing fake-S3 tests for successful upload and isolated failure state.
- [ ] Implement lazy boto3 S3 client and atomic backup state.
- [ ] Integrate a post-close backup cycle that never blocks the scanner and re-run tests.

### Task 6: Remaining-session RV development lab

**Files:**
- Create: `app/v121_rv_lab.py`
- Test: `tests/test_v121_rv_lab.py`

**Interfaces:**
- `build_remaining_session_dataset(nifty_5m, vix_daily, feature_end="10:30", target_end="15:10") -> pandas.DataFrame`
- `fit_loglinear_model(train_df) -> dict`
- `rolling_oos_forecast(dataset, min_train=120) -> pandas.DataFrame`
- `forecast_metrics(predictions) -> dict`
- `intraday_variance_curve(nifty_5m) -> pandas.DataFrame`

- [ ] Write failing synthetic-data tests proving target-window variance, morning variance, strict prior-day VIX lag and exclusion of incomplete sessions.
- [ ] Implement dataset builder.
- [ ] Write failing OLS/rolling OOS tests and implement model/metrics with MSE and QLIKE.
- [ ] Write failing U-curve test and implement time-of-day variance-share diagnostic without optimizing a window.

### Task 7: Development runner/readiness state

**Files:**
- Create: `app/v121_development.py`
- Modify: `app/web.py`
- Test: `tests/test_v121_development.py`

**Interfaces:**
- `run_development_lab(kite, state_file, start, end) -> dict`
- Protected endpoint `/api/v121-development-status`

- [ ] Write failing tests that mock historical NIFTY/VIX input and assert DEVELOPMENT ONLY, sample count, OOS metrics and no Trial-25 promotion.
- [ ] Implement token/history resolution using existing scanner chunked historical helpers where possible.
- [ ] Persist development state atomically and add read-only status API.
- [ ] Re-run tests.

### Task 8: Dashboard/API recorder health

**Files:**
- Modify: `app/web.py`
- Modify: `app/templates/index.html`
- Test: `tests/test_v121_ui.py`

**Interfaces:**
- Protected endpoint `/api/v121-index-vol-health`
- Dashboard panel `V12.1 · INDEX VOLATILITY RECORDER & FEASIBILITY LAB`

- [ ] Write failing HTML/API tests for recorder status, expiry, ATM, token count, last tick, micro/depth rows and bytes, backup status, development status and Trial-25 lock.
- [ ] Implement API payload and dashboard rendering.
- [ ] Re-run tests and validate rendered inline JavaScript syntax with Node.

### Task 9: Release verification and package

**Files:**
- Modify: `V12_CHANGELOG.md`
- Package: `/mnt/data/DBIndicator-institutional-v12.1-INDEX-VOLATILITY-RECORDER-FEASIBILITY-LAB-FINAL.zip`

**Interfaces:** none.

- [ ] Run all V12.1 targeted tests.
- [ ] Run the complete repository regression in deterministic batches.
- [ ] Compile every Python file.
- [ ] Validate rendered Dashboard and Backtest JavaScript syntax with Node.
- [ ] Remove runtime/cache files and create the deployment ZIP.
- [ ] Extract the exact ZIP into a clean directory.
- [ ] Re-run the complete deterministic regression from that extracted package.
- [ ] Re-run compilation, JavaScript and ZIP-integrity checks from the extracted package.
- [ ] Compute SHA-256 and hand off only if every check passes.
