# Historical NIFTY Option Data Acquisition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Add a research-only historical option-data acquisition layer that can ingest Dhan/Breeze expired-option OHLC as non-executable proxy evidence and official/normalized bid-ask archives as executable evidence, without weakening the frozen Stage-3 gates.

**Architecture:** Keep `app/index_option_stage3.py` as the only executable-P&L gate. New provider adapters live in `app/index_option_historical_sources.py`. Broker OHLC sources are explicitly tagged `PROXY_ONLY` and cannot satisfy the historical executable P&L gate. A normalized bid/ask schema is validated separately so NSE historical order/trade-derived quotes can be supplied later without changing the frozen signal.

**Tech Stack:** Python 3.11, pandas, requests, pytest.

**Spec:** `docs/index_option_stage3_preregistration.md`

## Global Constraints

- Index-only research.
- Frozen NIFTY signal parameters are unchanged.
- No Railway deployment.
- No production BUY/SELL output.
- No synthetic bid/ask derived from OHLC.
- Historical proxy evidence cannot satisfy the executable option-P&L gate.
- Genuine bid/ask archives may satisfy the executable gate only after schema validation.

## Review Focus

- Dhan/Breeze response missing expected fields must fail closed.
- Timestamp parsing must preserve IST semantics.
- ATM/ITM rolling labels must remain explicit and must not be inferred from OHLC alone.
- Proxy OHLC must never be renamed to best_bid/best_ask.
- Executable normalized archive must reject crossed/zero/negative quotes and stale schema.

---

### Task 1: Historical source normalizers

**Files:**
- Create: `app/index_option_historical_sources.py`
- Test: `tests/test_index_option_historical_sources.py`

**Interfaces:**
- Produces `normalize_dhan_rolling_response(payload, expression, option_type)`
- Produces `normalize_breeze_historical_response(payload, expression, option_type)`
- Produces `validate_executable_quote_archive(frame)`

- [ ] Write failing tests for Dhan/Breeze normalization and executable-schema rejection.
- [ ] Run tests and verify RED.
- [ ] Implement minimal normalizers and validator.
- [ ] Run tests and verify GREEN.

### Task 2: Dhan expired-option fetch client

**Files:**
- Modify: `app/index_option_historical_sources.py`
- Test: `tests/test_index_option_historical_sources.py`

**Interfaces:**
- Produces `fetch_dhan_expired_options(...) -> pd.DataFrame`

- [ ] Write failing test using injected transport.
- [ ] Verify RED.
- [ ] Implement authenticated POST to `/v2/charts/rollingoption`.
- [ ] Verify GREEN.

### Task 3: Historical proxy runner

**Files:**
- Create: `scripts/fetch_index_option_historical_proxy.py`
- Modify: `.github/workflows/index-option-research.yml`

**Interfaces:**
- Reads credentials from environment only when the user provides them.
- Writes normalized proxy CSV + manifest with `executable=false`.

- [ ] Write parser/config tests where practical.
- [ ] Compile and run full index-research suite.
- [ ] Confirm no production deploy changes.
