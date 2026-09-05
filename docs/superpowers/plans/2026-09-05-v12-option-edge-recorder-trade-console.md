# V12.0 Option Edge Recorder & Trade Opportunity Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a live operational trade-candidate console and start an auditable forward stock-option / point-in-time earnings dataset while keeping Trial 25 and every prior holdout locked.

**Architecture:** Keep the existing scanner and V11.1 research code intact. Add isolated V12 modules for (1) operational candidate classification, (2) option snapshot selection/normalization/persistence, (3) point-in-time earnings-calendar persistence, and (4) feasibility summaries, then wire them downstream of the existing live scan. V12 can observe and record but cannot create validated alpha or run Trial 25.

**Tech Stack:** Python 3, Flask, pandas, requests, existing Kite Connect client, JSON/JSONL persistence, pytest, vanilla HTML/JavaScript.

**Spec:** `docs/superpowers/specs/2026-09-05-v12-option-edge-recorder-trade-console-design.md`

## Global Constraints

- No new historical alpha read and no Trial 25 in V12.0.
- V11.1 conclusions and Trial-24 final 31-month holdout remain unchanged and unread.
- Existing V8/V9 playbooks remain rejected/locked exactly as before.
- V12 trade states are operational labels only; every candidate displays `NOT VALIDATED`.
- Option execution uses two-sided bid/ask where required; no fabricated spread or executable midpoint fill.
- Recorder failures never stop the live scanner.
- Runtime V12 JSON/JSONL files must not ship in the deployment ZIP.
- Quote batches are capped at 400 instruments.
- Four IST slots: 09:30, 13:00, 15:10, 15:37 with seven-minute grace and no backfill.
- Stock-option feasibility requires at least 10 distinct recorded trading days before a verdict.

---

### Task 1: V12 release identity and configuration

**Files:**
- Modify: `app/config.py`
- Modify: `.env.example`
- Create: `V12_CHANGELOG.md`
- Modify: `RESEARCH_BUILD.txt`
- Test: `tests/test_v120_release.py`

**Interfaces:**
- Produces config constants `V12_OPTION_SNAPSHOT_FILE`, `V12_OPTION_STATE_FILE`, `V12_EARNINGS_LEDGER_FILE`, `V12_EARNINGS_STATE_FILE`, `V12_SNAPSHOT_GRACE_MINUTES`, `V12_DEEP_SYMBOL_LIMIT`.

- [ ] Write `tests/test_v120_release.py` asserting the V12 build marker, changelog presence, config defaults, and absence of any Trial-25 runner marker.
- [ ] Run `python -m pytest tests/test_v120_release.py -q`; verify RED because V12 symbols do not exist.
- [ ] Add the minimal config/build/changelog values.
- [ ] Run the release test; verify GREEN.

### Task 2: Operational trade-candidate state machine

**Files:**
- Create: `app/v12_trade_console.py`
- Test: `tests/test_v120_trade_console.py`

**Interfaces:**
- Consumes `radar: dict`, `swing_research: dict`, `results: list[dict]`.
- Produces `build_trade_console(radar, swing_research, results, *, limit=5) -> dict`.
- Produces per-row fields `trade_state`, `execution_route`, `trigger_reference`, `invalidation_reference`, `not_validated=True`.

- [ ] Write tests for `OBSERVE`, `WATCH`, `SETUP`, futures `EXECUTABLE`, option `EXECUTABLE`, extended downgrade, no-liquidity downgrade, maximum-five behavior, and `NOT VALIDATED` labeling.
- [ ] Run the new tests and verify RED.
- [ ] Implement pure deterministic classification using the frozen score bands and liquidity thresholds from the spec.
- [ ] Run tests and verify GREEN.

### Task 3: Option snapshot timing, contract selection, quote normalization

**Files:**
- Create: `app/v12_option_recorder.py`
- Test: `tests/test_v120_option_recorder.py`

**Interfaces:**
- Produces `due_snapshot_slot(now, state, grace_minutes=7) -> str | None`.
- Produces `select_broad_atm_contracts(contracts_map, spot_map) -> list[dict]`.
- Produces `rank_deep_symbols(broad_summaries, earnings_symbols, limit=40) -> list[str]`.
- Produces `select_deep_contracts(...) -> list[dict]`.
- Produces `quote_in_batches(kite, keys, batch_size=400) -> (dict, list[dict])`.
- Produces `normalize_contract_snapshot(contract, quote, spot, now, slot) -> dict`.

- [ ] Write timing tests around all four slots, grace-window boundaries, duplicate-slot state and missed-slot no-backfill behavior.
- [ ] Write selection tests proving near+next only, ATM for broad pass, ATM±6 for deep pass, duplicate elimination and no expired contracts.
- [ ] Write batch tests proving no request exceeds 400 and partial failures are returned explicitly.
- [ ] Write quote tests proving five-level depth preservation, two-sided spread math, bid/mid/ask IV, and one-sided fail-closed behavior.
- [ ] Run tests and verify RED.
- [ ] Implement minimal recorder primitives reusing Black-Scholes/Greek helpers from `derivative_intelligence` where safe.
- [ ] Run tests and verify GREEN.

### Task 4: Point-in-time earnings calendar ledger

**Files:**
- Create: `app/v12_earnings_calendar.py`
- Test: `tests/test_v120_earnings_calendar.py`

**Interfaces:**
- Produces `parse_bulk_board_meetings(payload, fno_symbols=None) -> list[dict]`.
- Produces `record_calendar_observation(events, *, now, ledger_file, state_file) -> dict`.
- Produces `fetch_upcoming_earnings(session, symbols, start, end, timeout=25) -> list[dict]`.
- Produces `upcoming_earnings_symbols(state, today, days=7) -> set[str]`.

- [ ] Write parser tests proving only financial-result purposes are accepted and non-F&O names can be filtered.
- [ ] Write ledger tests for first-seen, unchanged observation, revised meeting date, disappeared/cancelled event, source fingerprint and append-only history.
- [ ] Write fetch-failure test proving `UNAVAILABLE` with no inferred quarterly date.
- [ ] Run tests and verify RED.
- [ ] Implement the bulk NSE-board-meeting client and append-only ledger/state reconciliation.
- [ ] Run tests and verify GREEN.

### Task 5: Snapshot persistence and feasibility summary

**Files:**
- Modify: `app/v12_option_recorder.py`
- Create: `app/v12_feasibility.py`
- Test: `tests/test_v120_feasibility.py`

**Interfaces:**
- Produces `record_snapshot(kite, results, earnings_symbols, *, now, snapshot_file, state_file) -> dict`.
- Produces `load_v12_state(path) -> dict` and atomic save helper.
- Produces `summarize_feasibility(state) -> dict`.

- [ ] Write snapshot test proving broad pass → liquidity rank → deep pass, append-only JSONL, slot dedupe, partial-batch audit and no scanner exception leakage.
- [ ] Write feasibility tests for <10-day `RECORDING`, 10-day pass, 10-day fail, 20-symbol threshold, >=70% two-sided coverage, median spread <=4%, stale quote rate and term-structure coverage.
- [ ] Run tests and verify RED.
- [ ] Implement persistence and summary math without efficacy statistics or trade direction.
- [ ] Run tests and verify GREEN.

### Task 6: Background integration

**Files:**
- Modify: `app/background.py`
- Modify: `app/web.py`
- Test: `tests/test_v120_background_integration.py`

**Interfaces:**
- Background calls daily earnings refresh and due option recorder downstream of the normal scan.
- `_state` gains `v12_trade_console`, `v12_option_status`, `v12_earnings_status`.
- `/api/live` payload exposes the new V12 blocks.
- Export endpoints expose accumulated V12 files.

- [ ] Write integration tests using fake Kite/NSE clients proving the normal scanner result persists when V12 recording fails.
- [ ] Write API tests proving V12 payloads are present and export endpoints return 404/empty safely when runtime files do not yet exist.
- [ ] Run tests and verify RED.
- [ ] Wire V12 after derivative intelligence and after `radar_snapshot`/`swing_snapshot`; catch/log all research failures.
- [ ] Add safe export endpoints for snapshot, recorder state, earnings ledger and earnings state.
- [ ] Run tests and verify GREEN.

### Task 7: Dashboard UI

**Files:**
- Modify: `app/templates/index.html`
- Test: `tests/test_v120_ui.py`

**Interfaces:**
- Adds `V12 Trade Opportunity Console` with intraday and 1D/2D candidate cards.
- Adds `V12 Option Edge Recorder` status/feasibility card and export links.

- [ ] Write HTML/JS release tests for all four operational states, `NOT VALIDATED`, Trial-25 lock text, recorder metrics and export links.
- [ ] Run tests and verify RED.
- [ ] Implement minimal dashboard markup/CSS/JS using existing card styles and `/api/live` payload.
- [ ] Run tests and verify GREEN.

### Task 8: Regression, packaging, and clean extracted-artifact verification

**Files:**
- Modify if needed: `.gitignore`, `README.md`
- Output: `/mnt/data/DBIndicator-institutional-v12.0-OPTION-EDGE-RECORDER-TRADE-CONSOLE-FINAL.zip`

**Interfaces:**
- Deployment ZIP must reproduce the verified source tree except excluded runtime/cache files.

- [ ] Run all new V12 tests together.
- [ ] Run the complete project suite in deterministic batches using `python -m pytest`.
- [ ] Compile every Python file with `python -m py_compile` / `compileall`.
- [ ] Extract rendered Backtest/Dashboard JavaScript and verify syntax with Node where available.
- [ ] Remove runtime state/cache files from the package and verify exclusions.
- [ ] Build ZIP, run `unzip -t`, compute SHA-256.
- [ ] Extract ZIP into a clean directory and rerun all V12 tests plus the full deterministic regression batches.
- [ ] Only after all checks pass, report V12.0 complete and provide the deployment ZIP.
