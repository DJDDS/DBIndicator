# V12.1.1 Recorder-Integrity Hotfix — Bounded TDD Plan

- **Service:** kite-scanner (production, EU West)
- **Current branch:** main
- **Current deployment:** v12.1 (commit `41cc00528cc4cd7a92135e8e81600c337bb4f857`)
- **Working branch:** `v1211-recorder-integrity-hotfix-tdd` (isolated — does NOT merge back to
  main, does NOT auto-deploy)
- **Status:** Planning only. No production code has been modified. No test files have been
  modified. This document exists to scope the two confirmed defects and lock in a test-first
  sequence before any implementation begins.

## Scope and Ground Rules

- This branch is isolated from `main` by design. It is not intended to be merged back
  automatically, and nothing on it should trigger an auto-deploy.
- No changes to `app/v121_index_recorder.py`, `app/background.py`, or anything under `tests/`
  happen as part of this planning commit. Those files are touched only in the implementation
  commits described below, and only after failing tests exist first.
- Historical data under `/data` is never rewritten as part of this hotfix. Defect A is a
  diagnostic-field bug, not a data-correctness bug — see below.

---

## Defect A: `quote_age_seconds` Timezone Bug

- **Location:** `app/v121_index_recorder.py`, lines 52-62, the `_age_seconds()` function.
- **Current behavior:** The function subtracts two `datetime` values that can each
  independently be naive or aware. In the failure case both `observed_at` and `quoted_at` end
  up naive — one effectively in IST local time, the other in UTC — and the subtraction proceeds
  as if they were on the same clock.
- **Root cause:** The existing `.replace(tzinfo=...)` fallback logic only fixes the mismatch
  when exactly one side is aware and the other is naive. When both sides are naive (`tzinfo is
  None` on both), there is nothing to copy from, so the naive-to-naive arithmetic silently
  proceeds on mismatched wall-clock times.
- **Observed impact:** A consistent ~5:30 hour offset in the reported `quote_age_seconds` value,
  matching the IST/UTC offset exactly. This field is diagnostic only — it is surfaced for
  observability and is not used anywhere as a filtering or gating condition, so the defect does
  not affect which rows are recorded or scanned, only what age is reported for them.
- **Required fix:**
  - Normalize both `observed_at` and `quoted_at` to timezone-aware UTC values before
    subtracting, rather than patching one side's `tzinfo` from the other.
  - All arithmetic should happen strictly in UTC.
  - The fix is proven by failing tests written first (see Test Strategy below), which must go
    red against the current implementation and green only after the fix lands.
  - Historical data already written to `/data` is left untouched — this is a forward-looking
    diagnostic fix, not a backfill/reprocessing task.

## Defect B: Twisted/KiteTicker Signal Handler Error

- **Location:** `app/background.py`, lines 273-287, `IndexVolStreamService.run_forever()`.
- **Current error:** `signal only works in main thread of the main interpreter`.
- **Root cause:** `run_forever()` executes on a background thread and calls
  `ticker.connect(threaded=False)`. With `threaded=False`, Twisted's reactor assumes it owns the
  main thread and attempts to install signal handlers, which is only legal from the actual main
  interpreter thread. Running this from a worker thread raises the error above.
- **Lifecycle issue:** `run_once()` may spin up a new `KiteTicker` instance on each invocation
  without guaranteeing the previous one has been torn down, creating a risk of duplicate,
  concurrent WebSocket connections to the same instrument feed.
- **Reconnect risk:** The `on_close` callback fires from within the WebSocket client's own event
  loop thread, not the main thread. Any reconnect logic invoked from `on_close` inherits the
  same thread-safety hazard as the initial connect if it is not handled carefully.
- **Required fix:**
  - Inspect the `kiteconnect` library's `KiteTicker` API for the supported thread-safe
    connect/reconnect pattern (e.g. `threaded=True`, or explicit reactor lifecycle management)
    rather than assuming the current call shape is correct.
  - Ensure a stable connect/reconnect flow: exactly one `KiteTicker` instance alive per
    `run_forever()` execution, no duplicate concurrent connections across restarts.
  - Eliminate the "signal only works in main thread" error entirely under normal operation and
    under simulated market-closed / reconnect conditions.
  - Preserve existing fail-soft behavior: shutdown and retry must continue to degrade
    gracefully rather than crash the process, and the stock scanner (unrelated to the index
    vol stream) must remain unaffected by any change here.

---

## Test Strategy (TDD)

Tests are written first and must fail against the current code before any fix is implemented.

### Defect A

- `test_age_seconds_nearby_quote()` — a quote observed a few seconds after it was quoted
  reports a small, correct positive age.
- `test_age_seconds_stale_10min()` — a quote observed 10 minutes after it was quoted reports
  ~600 seconds, not skewed by any timezone offset.
- `test_age_seconds_both_utc_aware()` — both timestamps already timezone-aware in UTC produce
  the correct age with no adjustment needed.
- `test_age_seconds_mixed_tzinfo()` — one timestamp aware, one naive, continues to resolve
  correctly (regression guard for the case that already worked).
- `test_age_seconds_none_values()` — either or both inputs missing/unparseable returns `None`
  rather than raising.

### Defect B

- `test_ticker_single_connect()` — repeated calls into the service's run/connect path never
  result in more than one live `KiteTicker` connection at a time.
- `test_ticker_no_signal_error_in_thread()` — invoking the connect path from a non-main thread
  does not raise `signal only works in main thread of the main interpreter`.
- `test_ticker_lifecycle_on_close()` — the `on_close` callback, exercised from a background
  thread, triggers reconnect logic without raising and without violating the single-ticker
  invariant.
- `test_ticker_market_closed_recovery()` — a simulated market-closed disconnect is followed by
  a clean, fail-soft retry cycle with no crash and no duplicate connections.

---

## Implementation Sequence

1. **Commit 1:** Add failing tests for both defects (Defect A age-calculation tests and
   Defect B ticker/signal-handling tests). Confirm they fail for the right reasons against the
   current `main`-derived code.
2. **Commit 2:** Implement the Defect A fix in `app/v121_index_recorder.py`
   (`_age_seconds()`, lines 52-62) — UTC-normalized, timezone-aware subtraction.
3. **Commit 3:** Run and pass all Defect A tests.
4. **Commit 4:** Implement the Defect B fix in `app/background.py`
   (`IndexVolStreamService.run_forever()`, lines 273-287) — stable, thread-safe
   connect/reconnect with a single ticker per run and no signal-handler error.
5. **Commit 5:** Run and pass all Defect B tests.
6. **Commit 6:** Run the full test suite to confirm no regressions elsewhere (in particular,
   the stock scanner path, which must remain unaffected by the Defect B change).

---

## Out of Scope for This Commit

- No changes to `app/v121_index_recorder.py`.
- No changes to `app/background.py`.
- No changes to anything under `tests/`.
- No rewriting of historical `/data` files.
- No merge back to `main`, no deploy.
