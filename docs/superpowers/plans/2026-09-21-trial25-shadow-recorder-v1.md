# Trial 25 Shadow Recorder v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a forward, no-peeking Trial-25 earnings-volatility shadow recorder that survives the 29-September F&O universe change, captures executable four-leg iron-butterfly entry/exit evidence, and freezes Stage-D variance after exactly the first 40 eligible completed events.

**Architecture:** Add focused Trial-25 modules beside the existing V12 recorder rather than modifying scanner-selection logic. The baseline 195-stock cohort remains immutable; daily NFO membership is recorded point-in-time, newly introduced F&O names must pass the same 10-session feasibility gate prospectively, and removed names fail closed when required contracts disappear. Trial-25 uses a deterministic event state machine, dedicated serialized quote pass, versioned Indian-option charge model, and a no-peeking Stage-D summary.

**Tech Stack:** Python 3.11, Flask, existing Kite Connect client, JSON/JSONL persistence on Railway Volume, pytest, SHA-256 provenance, existing V12/V12.1 runtime patterns.

**Spec:** `docs/superpowers/specs/2026-09-21-trial25-shadow-recorder-design.md`

## Global Constraints

- Baseline Trial-25 universe is the immutable 2026-09-21 frozen `tradeable_symbol_list`.
- Newly introduced F&O symbols require 10 distinct post-introduction trading sessions under the unchanged V12 gate: two-sided ATM coverage >=70% and median ATM-straddle spread <=4%.
- Supplemental eligibility begins only on the next trading day after the 10-session gate passes; never retroactively.
- Removed symbols are never silently replaced; an event is unavailable when required contracts no longer exist.
- Entry is fixed at PRE_CAS 15:10 IST on the last verified F&O trading session strictly before the registered earnings date.
- Exit is fixed at OPEN_STABLE 09:30 IST on the first verified F&O trading session strictly after the registered earnings date.
- Nearest listed expiry must expire strictly after planned exit and have at least 5 calendar DTE at entry.
- Structure is one-lot ATM iron butterfly with protective wings at or beyond +/-2.0x executable ATM-straddle ask implied-move points.
- Entry sells ATM CE/PE at best bid and buys wings at best ask; exit reverses on best ask/bid.
- Primary execution requires live REST response latency <=15 seconds, two-sided positive book, and required-side top-level quantity >= one recorded lot on all four legs.
- Old `last_trade_time` alone does not make an otherwise current executable REST book ineligible; it remains a separately reported stale-activity diagnostic.
- Fee model version is `ZERODHA_NSE_EQ_OPT_2026_04_V1`.
- No Trial-25 event P&L, return, mean, median, win rate, profit factor, or efficacy direction may be returned by dashboard/API before Stage-D calibration.
- Stage-D calibration uses exactly the deterministic first 40 eligible completed events, ordered by exit-capture timestamp then event ID, even if more than one event completes in the scanner cycle that crosses 40.
- Stage-D can freeze only sample standard deviation, exact event IDs, fee-model/code hashes, and required independent Stage-C N.
- Trial 24 final holdout, V12/V12.1 recorder rules, and the live directional scanner remain unchanged.
- Trial-25 failures are fail-soft and must never stop the scanner or existing recorders.
- Persistent runtime files live below the existing Railway V12 root.
- No live broker order, alert, or production strategy activation is added.

## Review Focus

1. **Monthly F&O membership transition around 29/30 September:** a symbol newly appearing in the NFO master must start at session 1/10 and remain Trial-25-ineligible until the next trading day after a valid 10-session gate; a disappeared symbol must not be silently replaced.
2. **Earnings-date revision at the entry boundary:** a revision first observed before the 15:10 entry uses the revised date; a revision first observed after entry must not rewrite the captured event.
3. **REST book vs last-trade age disagreement:** a live <=15-second REST snapshot with valid one-lot two-sided depth must remain execution-fresh even when `last_trade_time` is >600 seconds old, while the diagnostic flags that disagreement.
4. **Several events complete together at the Stage-D boundary:** if completed count jumps from 38 to 43, the freeze must select exactly the first 40 by `(exit_captured_at, event_id)`.
5. **Restart during a due capture window:** a persisted `ENTRY_CAPTURED` or `COMPLETED_RAW` event must not be quoted or appended a second time after process restart.

---

## File Structure

### New files

- `app/trial25_universe.py` — point-in-time F&O membership ledger and supplemental 10-session feasibility.
- `app/trial25_calendar.py` — verified 2026 NSE F&O trading sessions and point-in-time earnings-event date resolution.
- `app/trial25_execution.py` — pure contract selection, book validation, freshness diagnostics, and versioned fee model.
- `app/trial25_shadow.py` — event state machine, append-only ledger/raw quote persistence, idempotent capture.
- `app/trial25_stage_d.py` — no-peeking operational summary and deterministic 40-event calibration freeze.
- `tests/test_trial25_universe.py`
- `tests/test_trial25_calendar.py`
- `tests/test_trial25_execution.py`
- `tests/test_trial25_shadow.py`
- `tests/test_trial25_stage_d.py`
- `tests/test_trial25_live_integration.py`

### Modified files

- `TRIAL25_PREREGISTRATION.md` — incorporate rolling F&O membership and the >=5-DTE clarification before any event is admitted.
- `app/v12_storage.py` — add Trial-25 persistent paths.
- `app/config.py` — expose resolved Trial-25 paths/constants.
- `app/v12_live.py` — call Trial-25 processing after the normal V12 recorder, serially and fail-soft.
- `app/background.py` — pass current F&O universe source/symbols into V12/Trial-25 orchestration and persist read-only Trial-25 summary in scanner state.
- `app/web.py` — expose no-peeking Trial-25 summary in dashboard JSON/template context.
- `app/templates/index.html` — compact Stage-D operational panel only.
- existing V12/V12.1 regression tests — extend only where integration contracts require it.

---

### Task 1: Lock research contract and persistent paths

**Files:**
- Modify: `TRIAL25_PREREGISTRATION.md`
- Modify: `app/v12_storage.py`
- Modify: `app/config.py`
- Test: `tests/test_v12_storage.py`

**Interfaces:**
- Consumes: existing `resolve_v12_storage(environ) -> dict`
- Produces:
  - `V12_TRIAL25_ROOT`
  - `V12_TRIAL25_STATE_FILE`
  - `V12_TRIAL25_LEDGER_FILE`
  - `V12_TRIAL25_RAW_QUOTES_FILE`
  - `V12_TRIAL25_UNIVERSE_STATE_FILE`
  - `V12_TRIAL25_UNIVERSE_LEDGER_FILE`
  - `V12_TRIAL25_STAGE_D_FILE`
  - `V12_TRIAL25_STAGE_D_HASH_FILE`

- [ ] **Step 1: Update the preregistration before any Trial-25 event can be admitted**

Add the approved rolling F&O membership rule in substance:

```markdown
### Point-in-time F&O membership

The 2026-09-21 frozen tradeable_symbol_list is the immutable baseline cohort.
A later F&O addition is not eligible immediately. From its first observed NFO
instrument-master date it must complete 10 distinct trading sessions under the
same >=70% two-sided ATM coverage and <=4% median ATM-straddle spread gate.
Eligibility starts only on the next trading day after that gate passes.

A baseline or supplemental name whose required contracts no longer exist is
operationally unavailable for that event; it is not replaced.

Daily NFO membership is persisted point-in-time so later Exchange changes
cannot rewrite event-date eligibility.
```

Also change the expiry rule to explicitly require both conditions:

```markdown
Use the nearest listed expiry that expires strictly after planned exit and has
at least 5 calendar DTE at entry.
```

- [ ] **Step 2: Write failing storage-path tests**

Add to `tests/test_v12_storage.py`:

```python
def test_trial25_paths_live_under_persistent_v12_root():
    out = resolve_v12_storage({"RAILWAY_VOLUME_MOUNT_PATH": "/data"})
    assert out["trial25_root"] == "/data/v12/trial25"
    assert out["trial25_state"] == "/data/v12/trial25/trial25_state.json"
    assert out["trial25_ledger"] == "/data/v12/trial25/trial25_event_ledger.jsonl"
    assert out["trial25_raw_quotes"] == "/data/v12/trial25/trial25_raw_quotes.jsonl"
    assert out["trial25_universe_state"] == "/data/v12/trial25/trial25_universe_state.json"
    assert out["trial25_universe_ledger"] == "/data/v12/trial25/trial25_universe_ledger.jsonl"
    assert out["trial25_stage_d"] == "/data/v12/trial25/trial25_stage_d_calibration.json"
    assert out["trial25_stage_d_hash"] == "/data/v12/trial25/trial25_stage_d_calibration.sha256"
    assert out["persistent"] is True
```

- [ ] **Step 3: Run test and verify it fails**

Run:

```bash
pytest -q tests/test_v12_storage.py -k trial25
```

Expected: FAIL because the Trial-25 keys do not exist.

- [ ] **Step 4: Extend storage resolution minimally**

In `app/v12_storage.py`, derive `trial25_root = Path(root) / "trial25"` and return the eight exact paths above. Include them in persistence-under-volume validation.

In `app/config.py`, expose those values without introducing new environment tunables.

- [ ] **Step 5: Run storage tests**

```bash
pytest -q tests/test_v12_storage.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add TRIAL25_PREREGISTRATION.md app/v12_storage.py app/config.py tests/test_v12_storage.py
git commit -m "trial25: lock membership contract and storage paths"
```

---

### Task 2: Point-in-time F&O membership and supplemental feasibility

**Files:**
- Create: `app/trial25_universe.py`
- Create: `tests/test_trial25_universe.py`

**Interfaces:**
- Consumes:
  - frozen feasibility JSON path
  - current list of NFO instrument rows
  - current fixed-slot broad summary: `dict[symbol, summary]`
  - `now: datetime`
- Produces:
  - `stock_fno_symbols(instruments: list[dict]) -> set[str]`
  - `observe_membership(state, symbols, now) -> tuple[state, ledger_records]`
  - `record_supplemental_session(state, symbol, day, two_sided, straddle_spread_pct) -> dict`
  - `eligible_on(state, symbol, trading_date) -> tuple[bool, str]`
  - `load_universe_state(path) -> dict`
  - `save_universe_state(path, state) -> None`

- [ ] **Step 1: Write failing baseline/addition/removal tests**

Create `tests/test_trial25_universe.py`:

```python
import datetime as dt

from app import trial25_universe as u


def test_membership_detects_addition_and_removal_without_rewriting_history():
    state = u.empty_universe_state(["RELIANCE", "TCS"])
    d1 = dt.datetime(2026, 9, 29, 9, 20)
    d2 = dt.datetime(2026, 9, 30, 9, 20)

    state, first = u.observe_membership(state, {"RELIANCE", "TCS"}, d1)
    state, second = u.observe_membership(state, {"RELIANCE", "NEWFNO"}, d2)

    assert first[-1]["symbols"] == ["RELIANCE", "TCS"]
    assert second[-1]["added"] == ["NEWFNO"]
    assert second[-1]["removed"] == ["TCS"]
    assert state["symbols"]["NEWFNO"]["first_seen_fno"] == "2026-09-30"
    assert state["symbols"]["TCS"]["last_removed_fno"] == "2026-09-30"


def test_new_fno_name_is_not_eligible_until_next_day_after_ten_session_gate():
    state = u.empty_universe_state(["RELIANCE"])
    state, _ = u.observe_membership(state, {"RELIANCE", "NEWFNO"}, dt.datetime(2026, 9, 30, 9, 20))
    dates = [
        dt.date(2026, 9, 30), dt.date(2026, 10, 1), dt.date(2026, 10, 5),
        dt.date(2026, 10, 6), dt.date(2026, 10, 7), dt.date(2026, 10, 8),
        dt.date(2026, 10, 9), dt.date(2026, 10, 12), dt.date(2026, 10, 13),
        dt.date(2026, 10, 14),
    ]
    for day in dates:
        state = u.record_supplemental_session(
            state, "NEWFNO", day, two_sided=True, straddle_spread_pct=2.0
        )

    assert u.eligible_on(state, "NEWFNO", dt.date(2026, 10, 14))[0] is False
    assert u.eligible_on(state, "NEWFNO", dt.date(2026, 10, 15)) == (True, "SUPPLEMENTAL_10D_PASS")


def test_supplemental_gate_uses_same_coverage_and_spread_rule():
    state = u.empty_universe_state([])
    state, _ = u.observe_membership(state, {"NEWFNO"}, dt.datetime(2026, 9, 30, 9, 20))
    dates = [dt.date(2026, 10, d) for d in (1, 5, 6, 7, 8, 9, 12, 13, 14, 15)]
    for i, day in enumerate(dates):
        state = u.record_supplemental_session(
            state, "NEWFNO", day,
            two_sided=i < 6,
            straddle_spread_pct=3.0 if i < 6 else None,
        )
    ok, reason = u.eligible_on(state, "NEWFNO", dt.date(2026, 10, 16))
    assert ok is False
    assert reason == "SUPPLEMENTAL_GATE_NOT_PASSED"


def test_removed_baseline_name_is_unavailable_when_not_currently_in_fno():
    state = u.empty_universe_state(["TCS"])
    state, _ = u.observe_membership(state, {"TCS"}, dt.datetime(2026, 9, 29, 9, 20))
    state, _ = u.observe_membership(state, set(), dt.datetime(2026, 9, 30, 9, 20))
    assert u.eligible_on(state, "TCS", dt.date(2026, 10, 1)) == (False, "NOT_CURRENT_FNO_MEMBER")
```

- [ ] **Step 2: Run and verify red**

```bash
pytest -q tests/test_trial25_universe.py
```

Expected: import/module failure.

- [ ] **Step 3: Implement membership state**

Create `app/trial25_universe.py` with this public shape:

```python
def empty_universe_state(baseline_symbols):
    return {
        "version": 1,
        "baseline_symbols": sorted(set(map(str, baseline_symbols or []))),
        "current_symbols": [],
        "last_observed_date": None,
        "membership_hash": None,
        "symbols": {},
        "supplemental": {},
    }


def stock_fno_symbols(instruments):
    out = set()
    for row in instruments or []:
        typ = str(row.get("instrument_type") or "")
        name = str(row.get("name") or "").strip().upper()
        segment = str(row.get("segment") or "")
        if name and typ in {"FUT", "CE", "PE"} and segment.startswith("NFO"):
            out.add(name)
    return out
```

Membership observations hash the sorted symbol list with SHA-256, append at most once per date/hash, and store explicit `added`/`removed` arrays.

For supplemental qualification, aggregate one session observation per distinct trading date:

```python
coverage_pct = 100.0 * two_sided_sessions / recorded_sessions
median_spread = statistics.median(valid_spreads) if valid_spreads else None
passed = recorded_sessions >= 10 and coverage_pct >= 70.0 and median_spread is not None and median_spread <= 4.0
```

When `passed` becomes true, set `qualified_on=<decision date>`; `eligible_from` is the next verified trading session supplied by Task 3.

Do not use future snapshots to backfill an earlier date.

- [ ] **Step 4: Add idempotency/hash test**

```python
def test_same_day_same_membership_is_idempotent():
    state = u.empty_universe_state(["RELIANCE"])
    now = dt.datetime(2026, 9, 30, 9, 20)
    state, first = u.observe_membership(state, {"RELIANCE", "NEWFNO"}, now)
    first_hash = state["membership_hash"]
    state, second = u.observe_membership(state, {"RELIANCE", "NEWFNO"}, now)
    assert second == []
    assert state["membership_hash"] == first_hash
```

- [ ] **Step 5: Run green**

```bash
pytest -q tests/test_trial25_universe.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/trial25_universe.py tests/test_trial25_universe.py
git commit -m "trial25: preserve point-in-time F&O membership"
```

---

### Task 3: Verified 2026 F&O trading calendar and earnings timing

**Files:**
- Create: `app/trial25_calendar.py`
- Create: `tests/test_trial25_calendar.py`

**Interfaces:**
- Consumes: V12 earnings-state event dict, `as_known_at: datetime`
- Produces:
  - `is_fno_trading_day(day: date) -> bool`
  - `previous_fno_trading_day(day: date) -> date`
  - `next_fno_trading_day(day: date) -> date`
  - `revision_known_before_entry(event: dict, entry_at: datetime) -> bool`
  - `resolve_event_sessions(event: dict, as_known_at: datetime) -> dict`

- [ ] **Step 1: Write failing holiday/session tests**

Use official NSE F&O circular NSE/FAOP/71777:

```python
def test_2026_fno_holidays_and_weekends_are_closed():
    assert cal.is_fno_trading_day(dt.date(2026, 9, 14)) is False
    assert cal.is_fno_trading_day(dt.date(2026, 10, 2)) is False
    assert cal.is_fno_trading_day(dt.date(2026, 10, 20)) is False
    assert cal.is_fno_trading_day(dt.date(2026, 10, 3)) is False
    assert cal.is_fno_trading_day(dt.date(2026, 10, 5)) is True


def test_earnings_event_uses_strictly_previous_and_next_fno_sessions():
    event = {
        "symbol": "RELIANCE",
        "meeting_date": "2026-10-09",
        "state": "ACTIVE",
        "first_seen_at": "2026-09-21T10:00:00",
        "last_changed_at": "2026-09-21T10:00:00",
    }
    out = cal.resolve_event_sessions(event, dt.datetime(2026, 10, 8, 15, 0))
    assert out["entry_date"] == "2026-10-08"
    assert out["exit_date"] == "2026-10-12"
```

- [ ] **Step 2: Add revision-boundary test**

```python
def test_post_entry_revision_cannot_rewrite_captured_event():
    event = {
        "meeting_date": "2026-10-12",
        "first_seen_at": "2026-09-21T10:00:00",
        "last_changed_at": "2026-10-08T15:20:00",
        "state": "REVISED",
    }
    assert cal.revision_known_before_entry(
        event,
        dt.datetime(2026, 10, 8, 15, 10),
    ) is False
```

- [ ] **Step 3: Run red**

```bash
pytest -q tests/test_trial25_calendar.py
```

- [ ] **Step 4: Implement deterministic 2026 F&O calendar**

Define:

```python
FNO_HOLIDAYS_2026 = {
    dt.date(2026, 1, 26),
    dt.date(2026, 3, 3),
    dt.date(2026, 3, 26),
    dt.date(2026, 3, 31),
    dt.date(2026, 4, 3),
    dt.date(2026, 4, 14),
    dt.date(2026, 5, 1),
    dt.date(2026, 5, 28),
    dt.date(2026, 6, 26),
    dt.date(2026, 9, 14),
    dt.date(2026, 10, 2),
    dt.date(2026, 10, 20),
    dt.date(2026, 11, 10),
    dt.date(2026, 11, 24),
    dt.date(2026, 12, 25),
}
```

For any year other than 2026, fail closed with `UNVERIFIED_TRADING_CALENDAR`; do not infer weekdays-only.

- [ ] **Step 5: Run green**

```bash
pytest -q tests/test_trial25_calendar.py
```

- [ ] **Step 6: Commit**

```bash
git add app/trial25_calendar.py tests/test_trial25_calendar.py
git commit -m "trial25: add verified earnings trading calendar"
```

---

### Task 4: Execution primitives, stale audit, and Indian option charges

**Files:**
- Create: `app/trial25_execution.py`
- Create: `tests/test_trial25_execution.py`

**Interfaces:**
- Produces:
  - `select_event_contracts(contracts, spot, entry_date, exit_date, atm_ask_total) -> dict`
  - `book_snapshot(contract, quote, requested_at, received_at, required_side) -> dict`
  - `execution_book_ok(snapshot, lot_size) -> tuple[bool, str]`
  - `iron_fly_entry_credit(legs) -> float`
  - `iron_fly_exit_debit(legs) -> float`
  - `option_charges(fills) -> dict`
  - `FEE_MODEL_VERSION = "ZERODHA_NSE_EQ_OPT_2026_04_V1"`

- [ ] **Step 1: Write failing expiry/ATM/wing tests**

```python
def test_selects_nearest_expiry_after_exit_with_at_least_five_dte():
    contracts = option_chain(
        expiries=[dt.date(2026, 10, 13), dt.date(2026, 10, 27)],
        strikes=[900, 950, 1000, 1050, 1100],
    )
    out = ex.select_event_contracts(
        contracts,
        spot=1004.0,
        entry_date=dt.date(2026, 10, 8),
        exit_date=dt.date(2026, 10, 12),
        atm_ask_total=50.0,
    )
    assert out["expiry"] == "2026-10-13"
    assert out["atm_strike"] == 1000.0
    assert out["lower_put_strike"] == 900.0
    assert out["upper_call_strike"] == 1100.0
```

Also assert no eligible expiry returns `{"status":"UNAVAILABLE_EXPIRY"}`.

- [ ] **Step 2: Write freshness and one-lot depth tests**

```python
def test_old_last_trade_does_not_reject_current_executable_rest_book():
    requested = dt.datetime(2026, 10, 8, 15, 10, 0)
    received = dt.datetime(2026, 10, 8, 15, 10, 2)
    quote = {
        "last_trade_time": dt.datetime(2026, 10, 8, 14, 0, 0),
        "timestamp": dt.datetime(2026, 10, 8, 15, 10, 1),
        "depth": {
            "buy": [{"price": 100.0, "quantity": 500, "orders": 3}],
            "sell": [{"price": 101.0, "quantity": 500, "orders": 2}],
        },
    }
    snap = ex.book_snapshot({"lot_size": 250}, quote, requested, received, "SELL")
    assert snap["last_trade_stale_600s"] is True
    assert snap["transport_fresh"] is True
    assert ex.execution_book_ok(snap, 250) == (True, "OK")


def test_transport_latency_over_15_seconds_fails_closed():
    requested = dt.datetime(2026, 10, 8, 15, 10, 0)
    received = dt.datetime(2026, 10, 8, 15, 10, 16)
    snap = ex.book_snapshot(
        {"lot_size": 250},
        {"depth": {"buy": [{"price": 100, "quantity": 500}], "sell": [{"price": 101, "quantity": 500}]}},
        requested, received, "SELL",
    )
    assert ex.execution_book_ok(snap, 250) == (False, "STALE_TRANSPORT")
```

- [ ] **Step 3: Write charge-model test with exact formula**

```python
def manual_charges(fills):
    brokerage = 20.0 * len(fills)
    sell_turnover = sum(f["price"] * f["quantity"] for f in fills if f["side"] == "SELL")
    buy_turnover = sum(f["price"] * f["quantity"] for f in fills if f["side"] == "BUY")
    turnover = sell_turnover + buy_turnover
    stt = sell_turnover * 0.0015
    txn = turnover * 0.0003553
    sebi = turnover * 0.000001
    stamp = buy_turnover * 0.00003
    gst = 0.18 * (brokerage + txn + sebi)
    return brokerage + stt + txn + sebi + stamp + gst


def test_fee_model_matches_frozen_2026_formula():
    fills = [
        {"side": "SELL", "price": 100.0, "quantity": 250},
        {"side": "SELL", "price": 80.0, "quantity": 250},
        {"side": "BUY", "price": 15.0, "quantity": 250},
        {"side": "BUY", "price": 12.0, "quantity": 250},
    ]
    got = ex.option_charges(fills)
    assert got["version"] == "ZERODHA_NSE_EQ_OPT_2026_04_V1"
    assert got["total"] == pytest.approx(manual_charges(fills), rel=1e-12)
```

- [ ] **Step 4: Run red**

```bash
pytest -q tests/test_trial25_execution.py
```

- [ ] **Step 5: Implement pure execution module**

Ensure `book_snapshot` records:

```python
{
    "requested_at": ...,
    "received_at": ...,
    "api_latency_ms": ...,
    "quote_timestamp": ...,
    "quote_timestamp_age_s": ...,
    "last_trade_time": ...,
    "last_trade_age_s": ...,
    "last_trade_stale_600s": ...,
    "best_bid": ...,
    "best_bid_qty": ...,
    "best_ask": ...,
    "best_ask_qty": ...,
    "transport_fresh": api_latency_ms <= 15000,
    "required_side": "BUY" | "SELL",
}
```

Use exact executable formulas from the spec; do not import midpoint-return helpers.

- [ ] **Step 6: Run green**

```bash
pytest -q tests/test_trial25_execution.py
```

- [ ] **Step 7: Commit**

```bash
git add app/trial25_execution.py tests/test_trial25_execution.py
git commit -m "trial25: add executable iron-fly primitives"
```

---

### Task 5: Persistent Trial-25 event state machine

**Files:**
- Create: `app/trial25_shadow.py`
- Create: `tests/test_trial25_shadow.py`

**Interfaces:**
- Produces:
  - `empty_state() -> dict`
  - `make_event(...) -> dict`
  - `discover_events(...) -> dict`
  - `due_transition(event, now) -> str | None`
  - `capture_entry(...) -> dict`
  - `capture_exit(...) -> dict`
  - `public_event(event) -> dict`
  - `load_state(path) -> dict`
  - `save_state(path, state) -> None`
  - `append_ledger(path, record) -> None`
  - `append_raw_quotes(path, record) -> None`

- [ ] **Step 1: Write failing event-state tests**

```python
def test_event_id_is_deterministic_and_entry_freezes_four_contracts():
    event = shadow.make_event(
        symbol="RELIANCE",
        meeting_date=dt.date(2026, 10, 9),
        entry_date=dt.date(2026, 10, 8),
        exit_date=dt.date(2026, 10, 12),
    )
    first = shadow.capture_entry(event, executable_entry_fixture(), captured_at="2026-10-08T15:10:03")
    second = shadow.capture_entry(first, executable_entry_fixture(), captured_at="2026-10-08T15:10:04")
    assert first["event_id"] == second["event_id"]
    assert first["state"] == "ENTRY_CAPTURED"
    assert second == first
    assert len(first["contracts"]) == 4


def test_exit_rejects_contract_change():
    entered = shadow.capture_entry(base_event(), executable_entry_fixture(), captured_at="2026-10-08T15:10:03")
    changed = executable_exit_fixture()
    changed["atm_ce"]["instrument_token"] = 999999
    out = shadow.capture_exit(entered, changed, captured_at="2026-10-12T09:30:02")
    assert out["state"] == "UNAVAILABLE_CONTRACT_CHANGED"


def test_completed_event_public_shape_has_no_pnl():
    completed = completed_raw_event_fixture()
    public = shadow.public_event(completed)
    forbidden = {"pnl", "return", "return_pct", "profit", "win", "loss"}
    assert forbidden.isdisjoint(set(public))
```

- [ ] **Step 2: Add restart/idempotency test**

```python
def test_persisted_entry_is_not_appended_twice_after_restart(tmp_path):
    state_file = tmp_path / "state.json"
    ledger_file = tmp_path / "ledger.jsonl"
    raw_file = tmp_path / "raw.jsonl"
    state = shadow.empty_state()
    state = shadow.record_entry_once(
        state, base_event(), executable_entry_fixture(),
        state_file=state_file, ledger_file=ledger_file, raw_file=raw_file,
        captured_at="2026-10-08T15:10:03",
    )
    reloaded = shadow.load_state(state_file)
    state2 = shadow.record_entry_once(
        reloaded, base_event(), executable_entry_fixture(),
        state_file=state_file, ledger_file=ledger_file, raw_file=raw_file,
        captured_at="2026-10-08T15:10:05",
    )
    assert state2 == reloaded
    assert len(ledger_file.read_text().splitlines()) == 1
    assert len(raw_file.read_text().splitlines()) == 1
```

- [ ] **Step 3: Run red**

```bash
pytest -q tests/test_trial25_shadow.py
```

- [ ] **Step 4: Implement explicit fail-closed terminal states**

```python
UNAVAILABLE_STATES = {
    "UNAVAILABLE_NOT_IN_FROZEN_UNIVERSE",
    "UNAVAILABLE_SUPPLEMENTAL_NOT_QUALIFIED",
    "UNAVAILABLE_CALENDAR",
    "UNAVAILABLE_ENTRY_SNAPSHOT",
    "UNAVAILABLE_EXPIRY",
    "UNAVAILABLE_ATM_BOOK",
    "UNAVAILABLE_WING_BOOK",
    "UNAVAILABLE_QUANTITY",
    "UNAVAILABLE_EXIT_BOOK",
    "UNAVAILABLE_CONTRACT_CHANGED",
}
```

Store raw executable quotes but no derived P&L fields before calibration.

Use atomic temp+rename for state JSON and append-only JSONL for transitions/raw quotes.

- [ ] **Step 5: Run green**

```bash
pytest -q tests/test_trial25_shadow.py
```

- [ ] **Step 6: Commit**

```bash
git add app/trial25_shadow.py tests/test_trial25_shadow.py
git commit -m "trial25: add persistent earnings event state machine"
```

---

### Task 6: No-peeking Stage-D summary and deterministic 40-event freeze

**Files:**
- Create: `app/trial25_stage_d.py`
- Create: `tests/test_trial25_stage_d.py`

**Interfaces:**
- Consumes: raw completed Trial-25 events, fee model, Stage-D freeze paths
- Produces:
  - `operational_summary(state) -> dict`
  - `maybe_freeze_calibration(state, report_path, hash_path) -> dict`
  - `verify_calibration(report_path, hash_path) -> dict`

- [ ] **Step 1: Write no-peeking tests**

```python
def test_summary_before_40_exposes_counts_but_no_efficacy():
    state = state_with_completed_events(39)
    out = stage.operational_summary(state)
    assert out["completed_eligible"] == 39
    assert out["target"] == 40
    assert out["calibration_status"] == "COLLECTING"
    forbidden = {
        "mean_return", "median_return", "win_rate", "profit_factor",
        "pnl", "return_pct", "t_stat",
    }
    assert forbidden.isdisjoint(set(out))


def test_freeze_does_not_run_at_39(tmp_path):
    out = stage.maybe_freeze_calibration(
        state_with_completed_events(39),
        tmp_path / "cal.json",
        tmp_path / "cal.sha256",
    )
    assert out["status"] == "COLLECTING"
    assert not (tmp_path / "cal.json").exists()
```

- [ ] **Step 2: Write the 38->43 deterministic boundary test**

```python
def test_stage_d_uses_exactly_first_40_when_cycle_completes_five_events(tmp_path):
    state = state_with_completed_events(43, shuffled=True)
    out = stage.maybe_freeze_calibration(
        state,
        tmp_path / "cal.json",
        tmp_path / "cal.sha256",
    )
    assert out["status"] == "FROZEN"
    frozen = json.loads((tmp_path / "cal.json").read_text())
    expected = sorted(
        state["events"].values(),
        key=lambda e: (e["exit_captured_at"], e["event_id"]),
    )[:40]
    assert frozen["event_ids"] == [e["event_id"] for e in expected]
    assert len(frozen["event_ids"]) == 40
    assert "mean_return" not in frozen
    assert "event_returns" not in frozen
```

- [ ] **Step 3: Write freeze immutability test**

```python
def test_existing_calibration_is_hash_verified_not_overwritten(tmp_path):
    state = state_with_completed_events(40)
    first = stage.maybe_freeze_calibration(state, tmp_path/"cal.json", tmp_path/"cal.sha256")
    before = (tmp_path/"cal.json").read_bytes()
    later = state_with_completed_events(60)
    second = stage.maybe_freeze_calibration(later, tmp_path/"cal.json", tmp_path/"cal.sha256")
    assert second["status"] == "EXISTING_VALID_FREEZE"
    assert (tmp_path/"cal.json").read_bytes() == before
```

- [ ] **Step 4: Run red**

```bash
pytest -q tests/test_trial25_stage_d.py
```

- [ ] **Step 5: Implement internal-only return calculation**

Inside `maybe_freeze_calibration`, compute event returns only in local variables:

```python
entry_credit_rupees = entry_credit_per_unit * lot_size
exit_debit_rupees = exit_debit_per_unit * lot_size
charges = option_charges(entry_fills + exit_fills)["total"]
net_pnl = entry_credit_rupees - exit_debit_rupees - charges
return_pct = 100.0 * net_pnl / ((atm_ce_entry_bid + atm_pe_entry_bid) * lot_size)
```

Then compute only:

```python
sigma_d = statistics.stdev(returns)
required_n = max(
    40,
    math.ceil((((1.644854 + 0.841621) * sigma_d) / 4.0) ** 2),
)
```

Persist only:

```python
{
    "status": "FROZEN",
    "event_ids": [...40 ids...],
    "sigma_d": sigma_d,
    "required_stage_c_n": required_n,
    "fee_model_version": FEE_MODEL_VERSION,
    "trial25_stage_d_code_sha256": ...,
    "frozen_at": ...,
}
```

No individual returns or mean may be written to state/report/logs.

- [ ] **Step 6: Run green**

```bash
pytest -q tests/test_trial25_stage_d.py
```

- [ ] **Step 7: Commit**

```bash
git add app/trial25_stage_d.py tests/test_trial25_stage_d.py
git commit -m "trial25: enforce no-peeking Stage-D calibration"
```

---

### Task 7: Serialized live orchestration after existing V12 recorder

**Files:**
- Modify: `app/v12_live.py`
- Modify: `app/background.py`
- Create: `tests/test_trial25_live_integration.py`
- Modify: `tests/test_v12_live.py`

**Interfaces:**
- Consumes current `kite`, scanner `results`, `fno_symbols`, current universe source, NFO contracts, existing V12 recorder result
- Produces `trial25_shadow` read-only summary in `process_live_scan()` output

- [ ] **Step 1: Write ordering test**

```python
def test_trial25_quote_pass_runs_after_normal_v12_capture(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(v12_option_recorder, "record_snapshot", lambda *a, **k: calls.append("v12") or {"status":"CAPTURED"})
    monkeypatch.setattr(trial25_shadow, "process_due_events", lambda *a, **k: calls.append("trial25") or {"status":"COLLECTING"})
    out = run_live_fixture(tmp_path)
    assert calls == ["v12", "trial25"]
    assert out["trial25_shadow"]["status"] == "COLLECTING"
```

- [ ] **Step 2: Write fail-soft test**

```python
def test_trial25_failure_never_breaks_v12(monkeypatch, tmp_path):
    monkeypatch.setattr(v12_option_recorder, "record_snapshot", lambda *a, **k: {"status":"CAPTURED"})
    def boom(*a, **k):
        raise RuntimeError("trial25 synthetic failure")
    monkeypatch.setattr(trial25_shadow, "process_due_events", boom)
    out = run_live_fixture(tmp_path)
    assert out["recorder"]["status"] == "CAPTURED"
    assert out["trial25_shadow"]["status"] == "ERROR"
```

- [ ] **Step 3: Write fallback-universe test**

A fallback universe may preserve scanning but must never create F&O membership changes:

```python
def test_fallback_fno_universe_cannot_promote_or_remove_membership():
    out = trial25_universe.observe_runtime_universe(
        state=universe_state_fixture(),
        symbols={"RELIANCE"},
        source="LAST_KNOWN_GOOD",
        now=dt.datetime(2026, 9, 30, 9, 20),
    )
    assert out["membership_updated"] is False
    assert out["reason"] == "UNVERIFIED_RUNTIME_UNIVERSE"
```

- [ ] **Step 4: Run red**

```bash
pytest -q tests/test_trial25_live_integration.py tests/test_v12_live.py
```

- [ ] **Step 5: Integrate without a new thread**

Modify `v12_live.process_live_scan` to:

1. perform the existing V12 recorder call unchanged;
2. obtain/reuse the NFO contract map where possible;
3. update Trial-25 membership only from a verified `LIVE_KITE` universe observation;
4. update supplemental feasibility from the normal fixed-slot broad summary when available;
5. when an entry/exit transition is due, sleep conservatively for the Kite quote rate limit and perform only the dedicated event quote request;
6. call `trial25_stage_d.operational_summary`;
7. return `trial25_shadow`.

Wrap Trial-25 operations in a fail-soft exception boundary that cannot propagate into the main scanner.

Modify `background.py` to persist only the public Trial-25 summary.

- [ ] **Step 6: Run focused integration tests**

```bash
pytest -q tests/test_trial25_live_integration.py tests/test_v12_live.py tests/test_v12_option_recorder.py
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/v12_live.py app/background.py tests/test_trial25_live_integration.py tests/test_v12_live.py
git commit -m "trial25: integrate shadow recorder after V12 capture"
```

---

### Task 8: No-peeking dashboard surface

**Files:**
- Modify: `app/web.py`
- Modify: `app/templates/index.html`
- Modify: existing dashboard route regression test file

**Interfaces:**
- Consumes: `state["trial25_shadow"]`
- Produces: dashboard panel and `/api/dashboard-state["trial25_shadow"]`

- [ ] **Step 1: Write failing route test**

```python
def test_dashboard_api_exposes_trial25_operations_but_no_efficacy(client, monkeypatch):
    state = dashboard_state_with_trial25({
        "status": "STAGE_D_COLLECTING",
        "completed_eligible": 7,
        "target": 40,
        "upcoming_events": [{"symbol":"RELIANCE","meeting_date":"2026-10-09"}],
        "stale_audit": {"book_live_last_trade_old": 3},
    })
    monkeypatch.setattr(background, "get_state", lambda: state)
    payload = client.get("/api/dashboard-state", headers=auth()).get_json()
    t25 = payload["trial25_shadow"]
    assert t25["completed_eligible"] == 7
    forbidden = {"mean_return","win_rate","profit_factor","pnl","return_pct"}
    assert forbidden.isdisjoint(set(t25))
```

- [ ] **Step 2: Write template leakage test**

```python
def test_trial25_panel_contains_no_pnl_labels_before_stage_d(client, monkeypatch):
    html = client.get("/", headers=auth()).get_data(as_text=True)
    assert "TRIAL 25" in html
    assert "Stage-D completed" in html
    assert "Trial-25 P&L" not in html
    assert "Trial-25 Win Rate" not in html
    assert "Trial-25 Profit Factor" not in html
```

- [ ] **Step 3: Run red**

```bash
pytest -q tests -k "trial25 and dashboard"
```

- [ ] **Step 4: Add compact operational panel**

Before calibration, render only:

```text
TRIAL 25 — EARNINGS VOLATILITY / STAGE D
Status
Baseline eligible symbols
Supplemental F&O names: collecting / qualified
Upcoming eligible earnings events
Entry due / captured
Exit due / completed raw
Unavailable event counts by reason
Stage-D completed: X / 40
Book live + last trade >10m
Book invalid + recent last trade
Calibration: COLLECTING / FROZEN
Required Stage-C N (only after calibration freeze)
```

No raw-quote export and no P&L columns.

- [ ] **Step 5: Run green**

```bash
pytest -q tests -k "trial25 and dashboard"
```

- [ ] **Step 6: Commit**

```bash
git add app/web.py app/templates/index.html tests
git commit -m "trial25: add no-peeking Stage-D dashboard"
```

---

### Task 9: Full research-integrity regression and release gate

**Files:**
- Modify only release notes/tests if verification reveals a real compatibility issue.
- No strategy logic changes are allowed in this task.

**Interfaces:**
- Consumes all prior tasks
- Produces verified release candidate ready for Railway deployment

- [ ] **Step 1: Verify frozen V12 hashes are unchanged**

The production freeze must remain:

```text
v12_option_state_10d_2026-09-21.json
4cb686ec834627c83103611229bf0924526655c36a44d9376abda131f32c33f4

v12_feasibility_code_10d_2026-09-21.py
949f7c08c1ecfea9ff130468ef50a3687ae5241c94043513efd3809120f352d7

v12_feasibility_10d_2026-09-21.json
d14361328e8ed09a5ecb81071e55ceff9d890f76dac3c5154e38e5dc32871260
```

A different hash is a release failure, not a reason to update the expected constants.

- [ ] **Step 2: Run all Trial-25 tests**

```bash
pytest -q   tests/test_trial25_universe.py   tests/test_trial25_calendar.py   tests/test_trial25_execution.py   tests/test_trial25_shadow.py   tests/test_trial25_stage_d.py   tests/test_trial25_live_integration.py
```

Expected: all PASS.

- [ ] **Step 3: Run V12/V12.1 compatibility suite**

```bash
pytest -q   tests/test_v12_storage.py   tests/test_v12_option_recorder.py   tests/test_v12_live.py   tests/test_v12_feasibility.py   tests/test_v12_feasibility_freeze.py   tests/test_v121_index_recorder.py   tests/test_v121_release.py   tests/test_v1212_release.py
```

Expected: all PASS.

- [ ] **Step 4: Run full repository suite**

```bash
pytest -q
```

Expected: 100% pass; warnings may remain only if already-existing and unrelated.

- [ ] **Step 5: Compile Python**

```bash
python -m compileall -q app tests run.py
```

Expected: exit code 0.

- [ ] **Step 6: Inspect branch diff for prohibited changes**

```bash
git diff --name-only main...HEAD
git diff --stat main...HEAD
```

Confirm no changes to Trial-24 final-holdout data, scanner signal thresholds, V12 feasibility thresholds, V12.1 index-vol research rules, or live broker order/alert code.

- [ ] **Step 7: Add release note and commit**

Release note text:

```text
Trial 25 Stage-D shadow recorder
Research-only / no production activation
No-peeking P&L gate: active until first 40 eligible completed events
Baseline universe: frozen 2026-09-21 feasibility cohort
Supplemental F&O membership: same 10-session feasibility rule, prospective only
```

Commit:

```bash
git add .
git commit -m "trial25: finalize Stage-D shadow recorder"
```

- [ ] **Step 8: Request final review before merge/deploy**

Review must specifically inspect no-peeking leakage paths, F&O addition/removal eligibility, stale-book logic, fee-model arithmetic, idempotent persistence/restart behavior, first-40 calibration selection, and scanner/V12 fail-soft isolation.

- [ ] **Step 9: Railway production acceptance after merge**

After verified merge/deploy:

- deployment is `SUCCESS`;
- 2-GB `/data` volume remains mounted;
- existing V12/V12.1 state files remain intact;
- `/data/v12/trial25/` contains only Trial-25 runtime artifacts;
- dashboard shows `STAGE D — COLLECTING` and genuine `X/40`;
- no P&L/return field is present;
- current F&O membership is recorded only from `LIVE_KITE`;
- existing option recorders still capture normal slots;
- no new Twisted/KiteTicker/write/runtime errors appear.
