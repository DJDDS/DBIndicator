# Trial 25 Shadow Recorder v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a forward-only, no-peeking Trial-25 earnings-volatility shadow recorder that captures one defined-risk executable iron-butterfly event per eligible earnings announcement without changing the existing V12/V12.1 research recorders.

**Architecture:** Add five focused Trial-25 units: a point-in-time F&O membership/probation ledger, an event/session resolver, a pure execution/fees kernel, a persistent event state machine, and a Stage-D calibration gate. Wire them sequentially into the existing V12 live scan after the normal option recorder, expose only operational/sample-count fields on the dashboard, and keep the first 40 completed eligible events hidden from efficacy inspection.

**Tech Stack:** Python 3.11, Flask, pytest, Kite Connect REST quotes/instrument master, JSON/JSONL persistent files on Railway Volume, existing V12/V12.1 orchestration and dashboard.

**Spec:** `docs/superpowers/specs/2026-09-21-trial25-shadow-recorder-design.md`

## Global Constraints

- Trial 25 is research/shadow only; no broker orders, production alerts, or live strategy activation.
- Frozen eligible universe is exactly the 2026-09-21 V12 `tradeable_symbol_list`; newly added F&O names after the freeze cannot enter the original Trial 25.
- Post-freeze F&O additions are recorded in a separate `NEW_FNO_PROBATION` lane. They need 10 distinct forward trading sessions under the same >=70% two-sided ATM coverage and <=4% median ATM-straddle-spread gate before they can be marked `QUALIFIED_FOR_FUTURE_TRIAL`; this never amends Trial 25.
- A frozen symbol may enter an event only if it is still live-listed for the required entry/exit structure; otherwise use `UNAVAILABLE_NOT_FNO_AT_ENTRY`.
- Entry is PRE_CAS 15:10 IST on the last verified NSE F&O trading session strictly before the registered earnings date.
- Exit is OPEN_STABLE 09:30 IST on the first verified NSE F&O trading session strictly after the registered earnings date.
- Entry/exit use the existing 7-minute grace; missed slots are never backfilled.
- Expiry must expire strictly after planned exit and have at least 5 calendar DTE at entry.
- Structure is one-lot ATM iron butterfly with protective wings at or beyond +/-2.0x executable ATM implied-move points.
- No midpoint, last-price, theoretical-price, re-centering, or later-quote fallback.
- Trial-25 execution freshness requires a dedicated live REST quote, request-to-response latency <=15 seconds, valid two-sided book, and at least one lot on the required top-of-book side.
- Old `last_trade_time` is diagnostic only; it cannot by itself reject a current executable resting book.
- Charge model is `ZERODHA_NSE_EQ_OPT_2026_04_V1`, with the exact rates frozen in the spec.
- Stage D uses exactly the deterministic first 40 completed eligible events, ordered by exit-capture timestamp then event ID.
- Before the Stage-D boundary, no P&L, return, mean, median, win rate, profit factor, or direction-of-effect statistic is returned by dashboard-facing code.
- Existing frozen ten-day feasibility files and their hashes must never be modified.
- Trial 24 final holdout remains unread.
- Existing V12/V12.1 recorders must remain fail-soft and unaffected by Trial-25 errors.

## Review Focus

1. **F&O membership churn after 29-Sep:** a newly introduced symbol must stay excluded from Trial 25 but accumulate an independent probation record; a frozen symbol with no valid live contract must fail closed as `UNAVAILABLE_NOT_FNO_AT_ENTRY`; an incomplete instrument-master refresh must never mass-delete symbols.
2. **Holiday / weekend adjacency:** earnings on or around weekends/holidays must resolve to the correct last-before and first-after F&O sessions; unsupported calendar years must fail closed.
3. **Old last trade but live book:** an executable quote returned now with an old `last_trade_time` must remain execution-fresh while the stale diagnostic records the age.
4. **Scanner retries / redeploys:** repeated calls inside the same grace window must not duplicate event discovery, entry capture, exit capture, or Stage-D membership.
5. **Several events reaching the 40-event boundary together:** Stage D must freeze exactly the deterministic first 40 and leave later completions outside the variance-calibration sample.

---

### Task 0: Point-in-Time F&O Membership Ledger and New-Entrant Probation

**Files:**
- Create: `app/trial25_membership.py`
- Create: `tests/test_trial25_membership.py`
- Modify: `app/config.py`
- Modify: `app/v12_storage.py`
- Modify: `app/v12_live.py` only to pass the current complete F&O universe and completed V12 slot summary into this module.

**Interfaces:**
- Consumes: current complete F&O symbol set from the live scanner, immutable frozen Trial-25 symbols, and the symbol-level ATM summaries already produced by a completed V12 fixed-slot capture.
- Produces:
  - `observe_membership(path, *, observed_at, symbols, source_complete) -> dict`
  - `membership_status(state, symbol, on_date, frozen_symbols) -> str`
  - `record_probation_slot(path, *, date, slot, symbol_summaries, current_symbols, frozen_symbols) -> dict`
  - `probation_summary(state) -> dict`
  - storage path `TRIAL25_MEMBERSHIP_STATE_FILE`.

- [ ] **Step 1: Write failing complete/incomplete-universe tests**

```python
import datetime as dt

from app import trial25_membership as membership


def test_complete_observation_records_add_and_remove(tmp_path):
    path = tmp_path / "membership.json"
    membership.observe_membership(
        path,
        observed_at=dt.datetime(2026, 9, 28, 9, 20),
        symbols={"AAA", "BBB"},
        source_complete=True,
    )
    state = membership.observe_membership(
        path,
        observed_at=dt.datetime(2026, 9, 30, 9, 20),
        symbols={"BBB", "CCC"},
        source_complete=True,
    )
    assert {"symbol":"AAA", "change":"REMOVED", "effective_date":"2026-09-30"} in state["events"]
    assert {"symbol":"CCC", "change":"ADDED", "effective_date":"2026-09-30"} in state["events"]


def test_incomplete_observation_cannot_remove_existing_symbol(tmp_path):
    path = tmp_path / "membership.json"
    membership.observe_membership(
        path,
        observed_at=dt.datetime(2026, 9, 28, 9, 20),
        symbols={"AAA", "BBB"},
        source_complete=True,
    )
    state = membership.observe_membership(
        path,
        observed_at=dt.datetime(2026, 9, 29, 9, 20),
        symbols={"BBB"},
        source_complete=False,
    )
    assert membership.membership_status(
        state, "AAA", dt.date(2026, 9, 29), {"AAA"}
    ) == "ORIGINAL_ACTIVE"
```

- [ ] **Step 2: Run and confirm RED**

Run:

```bash
pytest -q tests/test_trial25_membership.py -k "complete_observation or incomplete_observation"
```

Expected: import failure because `app.trial25_membership` does not exist.

- [ ] **Step 3: Implement atomic point-in-time membership state**

The persisted schema contains:

```python
{
    "version": 1,
    "current_symbols": [],
    "last_complete_observation": None,
    "last_incomplete_observation": None,
    "events": [],
    "probation": {},
}
```

Use temp-file + atomic rename. Only a `source_complete=True` observation may change `current_symbols` or create `ADDED` / `REMOVED` events.

`membership_status(...)` returns exactly one of:

```text
ORIGINAL_ACTIVE
ORIGINAL_INACTIVE
NEW_FNO_PROBATION
OUTSIDE_TRIAL25
```

- [ ] **Step 4: Write failing probation tests**

```python
def test_new_fno_needs_ten_distinct_days_and_same_v12_gate(tmp_path):
    path = tmp_path / "membership.json"
    frozen = {"AAA"}
    for i in range(10):
        day = dt.date(2026, 9, 30) + dt.timedelta(days=i)
        if day.weekday() >= 5:
            continue
        membership.record_probation_slot(
            path,
            date=day,
            slot="OPEN_STABLE",
            symbol_summaries={
                "NEWFNO": {
                    "primary": {
                        "two_sided": True,
                        "straddle_spread_pct": 2.0,
                    }
                }
            },
            current_symbols={"AAA", "NEWFNO"},
            frozen_symbols=frozen,
        )
    summary = membership.probation_summary(membership.load_state(path))["NEWFNO"]
    assert summary["trial25_eligible"] is False
    if summary["trading_days"] >= 10:
        assert summary["status"] == "QUALIFIED_FOR_FUTURE_TRIAL"


def test_wide_spread_new_fno_never_qualifies_future_trial(tmp_path):
    path = tmp_path / "membership.json"
    for n in range(10):
        membership.record_probation_slot(
            path,
            date=dt.date(2026, 10, 5) + dt.timedelta(days=n),
            slot="OPEN_STABLE",
            symbol_summaries={
                "NEWFNO": {
                    "primary": {
                        "two_sided": True,
                        "straddle_spread_pct": 6.0,
                    }
                }
            },
            current_symbols={"NEWFNO"},
            frozen_symbols=set(),
        )
    out = membership.probation_summary(membership.load_state(path))["NEWFNO"]
    assert out["status"] != "QUALIFIED_FOR_FUTURE_TRIAL"
    assert out["trial25_eligible"] is False
```

- [ ] **Step 5: Implement probation accounting with the locked V12 thresholds**

Per new symbol persist:

```python
{
    "first_seen_date": "2026-09-30",
    "trading_days": [],
    "broad_snapshots": 0,
    "two_sided_snapshots": 0,
    "spread_values": [],
}
```

Compute:

```python
coverage_pct = 100.0 * two_sided_snapshots / broad_snapshots
median_spread_pct = statistics.median(spread_values)
qualified = (
    len(set(trading_days)) >= 10
    and coverage_pct >= 70.0
    and median_spread_pct <= 4.0
)
```

Even when `qualified` is true, return `trial25_eligible=False`; the status is evidence for a later preregistered trial only.

- [ ] **Step 6: Add persistent path**

Extend `app/v12_storage.py` / `app/config.py`:

```python
TRIAL25_MEMBERSHIP_STATE_FILE = str(
    Path(TRIAL25_ROOT) / "trial25_membership_state.json"
)
```

- [ ] **Step 7: Add the non-bloating V12 hook**

Do **not** put all `broad_summaries` into the long-lived scanner state. Instead add an optional callback to `v12_option_recorder.record_snapshot(..., broad_capture_hook=None)` invoked only after the normal V12 snapshot/state write succeeds:

```python
if broad_capture_hook is not None:
    broad_capture_hook(
        date=now.date(),
        slot=slot,
        broad_summaries=broad_summaries,
    )
```

Wrap the hook separately so a probation bookkeeping failure cannot turn a successful V12 capture into a failed capture. A dedicated regression test must assert V12 still returns `CAPTURED` when the hook raises.

- [ ] **Step 8: Run tests**

Run:

```bash
pytest -q tests/test_trial25_membership.py tests/test_v120_option_recorder.py
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add app/trial25_membership.py app/config.py app/v12_storage.py app/v12_option_recorder.py app/v12_live.py tests/test_trial25_membership.py tests/test_v120_option_recorder.py
git commit -m "feat: track Trial 25 F&O membership and new-entrant probation"
```

---

### Task 1: Lock Trial-25 Research Contract, Storage Paths, and Trading-Session Resolver

**Files:**
- Modify: `TRIAL25_PREREGISTRATION.md`
- Modify: `app/v12_storage.py`
- Modify: `app/config.py`
- Create: `app/trial25_calendar.py`
- Create: `tests/test_trial25_calendar.py`
- Modify: `tests/test_v120_earnings_calendar.py`

**Interfaces:**
- Consumes: frozen `tradeable_symbol_list` from the immutable feasibility report; V12 earnings state rows with `meeting_date`, `state`, `first_seen_at`, and `last_changed_at`.
- Produces:
  - `verified_trading_day(day: date) -> bool | None`
  - `last_trading_day_before(day: date) -> date | None`
  - `first_trading_day_after(day: date) -> date | None`
  - `event_known_before_entry(event: dict, entry_capture_at: datetime) -> bool`
  - storage paths `TRIAL25_STATE_FILE`, `TRIAL25_LEDGER_FILE`, `TRIAL25_RAW_QUOTES_FILE`, `TRIAL25_STAGE_D_FILE`, `TRIAL25_STAGE_D_HASH_FILE`.

- [ ] **Step 1: Write failing trading-calendar tests**

Create `tests/test_trial25_calendar.py`:

```python
import datetime as dt

from app import trial25_calendar as cal


def test_weekend_resolves_to_last_and_next_verified_session():
    meeting = dt.date(2026, 10, 11)  # Sunday
    assert cal.last_trading_day_before(meeting) == dt.date(2026, 10, 9)
    assert cal.first_trading_day_after(meeting) == dt.date(2026, 10, 12)


def test_known_2026_fo_holiday_is_not_a_trading_session():
    holiday = dt.date(2026, 9, 14)
    assert cal.verified_trading_day(holiday) is False


def test_unsupported_calendar_year_fails_closed():
    assert cal.verified_trading_day(dt.date(2027, 1, 4)) is None
    assert cal.last_trading_day_before(dt.date(2027, 1, 5)) is None


def test_revision_observed_after_entry_cannot_rewrite_event():
    event = {
        "state": "REVISED",
        "meeting_date": "2026-10-12",
        "first_seen_at": "2026-09-25T10:00:00+05:30",
        "last_changed_at": "2026-10-09T15:12:00+05:30",
    }
    entry_capture = dt.datetime(
        2026, 10, 9, 15, 10,
        tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)),
    )
    assert cal.event_known_before_entry(event, entry_capture) is False
```

- [ ] **Step 2: Run the tests and confirm RED**

Run:

```bash
pytest -q tests/test_trial25_calendar.py
```

Expected: collection/import failure because `app.trial25_calendar` does not exist.

- [ ] **Step 3: Implement the 2026 verified session resolver**

Create `app/trial25_calendar.py` with a frozen `NSE_FO_HOLIDAYS_2026` set and fail-closed year handling:

```python
from __future__ import annotations

import datetime as dt

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SUPPORTED_YEARS = {2026}

# Source: NSE/FAOP/71777 dated 2025-12-12, modified by
# NSE/FAOP/72262 dated 2026-01-12 (adds 2026-01-15).
NSE_FO_HOLIDAYS_2026 = frozenset({
    dt.date(2026, 1, 15),
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
})


def verified_trading_day(day: dt.date) -> bool | None:
    if day.year not in SUPPORTED_YEARS:
        return None
    if day.weekday() >= 5:
        return False
    return day not in NSE_FO_HOLIDAYS_2026


def last_trading_day_before(day: dt.date) -> dt.date | None:
    probe = day - dt.timedelta(days=1)
    for _ in range(10):
        status = verified_trading_day(probe)
        if status is None:
            return None
        if status:
            return probe
        probe -= dt.timedelta(days=1)
    return None


def first_trading_day_after(day: dt.date) -> dt.date | None:
    probe = day + dt.timedelta(days=1)
    for _ in range(10):
        status = verified_trading_day(probe)
        if status is None:
            return None
        if status:
            return probe
        probe += dt.timedelta(days=1)
    return None


def event_known_before_entry(event: dict, entry_capture_at: dt.datetime) -> bool:
    if event.get("state") not in {"ACTIVE", "REVISED"}:
        return False
    raw = event.get("last_changed_at") or event.get("first_seen_at")
    if not raw:
        return False
    observed = dt.datetime.fromisoformat(str(raw))
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=IST)
    if entry_capture_at.tzinfo is None:
        entry_capture_at = entry_capture_at.replace(tzinfo=IST)
    return observed <= entry_capture_at
```

Use the explicit holiday set above. Do not substitute the NSE Clearing settlement-holiday calendar: Trial 25 needs **trading** days, not settlement days. The two authoritative inputs are NSE/FAOP/71777 and its January-15 modification NSE/FAOP/72262.

- [ ] **Step 4: Add Trial-25 persistent paths**

Extend `app/v12_storage.py`:

```python
out["trial25_root"] = str(Path(root) / "trial25") if mount else "trial25"
out["trial25_state"] = str(Path(out["trial25_root"]) / "trial25_state.json")
out["trial25_ledger"] = str(Path(out["trial25_root"]) / "trial25_event_ledger.jsonl")
out["trial25_raw_quotes"] = str(Path(out["trial25_root"]) / "trial25_raw_quotes.jsonl")
out["trial25_stage_d"] = str(Path(out["trial25_root"]) / "trial25_stage_d_calibration.json")
out["trial25_stage_d_hash"] = str(Path(out["trial25_root"]) / "trial25_stage_d_calibration.sha256")
```

Expose those paths in `app/config.py`.

- [ ] **Step 5: Update the preregistration for F&O churn and >=5-DTE clarification**

Add the exact prospective intersection rule:

```text
Trial25 event-eligible = frozen tradeable_symbol_list
                         AND currently listed contracts satisfying the locked expiry/exit rules.

Post-freeze F&O additions are excluded from Trial 25.
Frozen symbols with no valid live contracts at entry are
UNAVAILABLE_NOT_FNO_AT_ENTRY.
```

Also make the >=5-calendar-DTE rule explicit in the preregistration before any event is admitted.

- [ ] **Step 6: Run focused tests**

Run:

```bash
pytest -q tests/test_trial25_calendar.py tests/test_v120_earnings_calendar.py
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add TRIAL25_PREREGISTRATION.md app/trial25_calendar.py app/v12_storage.py app/config.py tests/test_trial25_calendar.py tests/test_v120_earnings_calendar.py
git commit -m "feat: lock Trial 25 event calendar and storage"
```

---

### Task 2: Build the Pure Execution, Freshness, Contract-Selection, and Charge Kernel

**Files:**
- Create: `app/trial25_execution.py`
- Create: `tests/test_trial25_execution.py`

**Interfaces:**
- Consumes: current option instrument rows, spot, planned exit date, raw Kite quote payloads, one-lot quantity.
- Produces:
  - `select_event_contracts(contracts, spot, entry_date, exit_date) -> dict`
  - `normalize_live_quote(contract, quote, requested_at, received_at) -> dict`
  - `execution_fresh(leg, required_side, lot_size) -> tuple[bool, str | None]`
  - `calculate_option_charges(fills) -> dict`
  - constant `FEE_MODEL_VERSION = "ZERODHA_NSE_EQ_OPT_2026_04_V1"`.

- [ ] **Step 1: Write failing contract-selection tests**

At the top of `tests/test_trial25_execution.py`, define:

```python
import datetime as dt

from app import trial25_execution as execution


def contract(symbol="AAA", expiry=dt.date(2026, 10, 27), strike=100.0, typ="CE", lot_size=100):
    return {
        "tradingsymbol": f"{symbol}-{expiry.isoformat()}-{strike:g}-{typ}",
        "instrument_token": hash((symbol, expiry, strike, typ)) & 0xFFFF,
        "instrument_type": typ,
        "strike": float(strike),
        "expiry": expiry,
        "lot_size": int(lot_size),
    }


def contract_ladder(expiries, strikes, symbol="AAA"):
    rows = []
    for expiry in expiries:
        for strike in strikes:
            rows.append(contract(symbol, expiry, strike, "CE"))
            rows.append(contract(symbol, expiry, strike, "PE"))
    return rows


def raw_quote(*, bid=10.0, bid_qty=200, ask=10.2, ask_qty=200, last_trade_time=None, timestamp=None):
    return {
        "last_price": (bid + ask) / 2.0,
        "last_trade_time": last_trade_time,
        "timestamp": timestamp,
        "depth": {
            "buy": [{"price": bid, "quantity": bid_qty, "orders": 1}],
            "sell": [{"price": ask, "quantity": ask_qty, "orders": 1}],
        },
    }


def aware(text):
    return dt.datetime.fromisoformat(text)
```

Then add:

```python
def test_post_freeze_added_symbol_cannot_be_made_eligible_by_contracts():
    frozen = {"AAA"}
    assert execution.symbol_event_eligibility("NEWFNO", frozen, [{"expiry": "2026-10-27"}]) == (
        False, "UNAVAILABLE_NOT_IN_FROZEN_UNIVERSE"
    )


def test_frozen_symbol_without_live_contracts_is_not_fno_at_entry():
    assert execution.symbol_event_eligibility("AAA", {"AAA"}, []) == (
        False, "UNAVAILABLE_NOT_FNO_AT_ENTRY"
    )


def test_nearest_expiry_requires_five_dte_and_survives_exit():
    rows = contract_ladder(
        [dt.date(2026,10,13), dt.date(2026,10,27)],
        [80,90,100,110,120],
    )
    chosen = execution.select_event_contracts(
        rows,
        spot=100.0,
        entry_date=dt.date(2026,10,8),
        exit_date=dt.date(2026,10,12),
    )
    assert chosen["expiry"] == "2026-10-27"
```

- [ ] **Step 2: Write failing wing-selection tests**

```python
def test_wings_are_at_or_beyond_two_times_executable_implied_move():
    rows = contract_ladder(
        [dt.date(2026,10,27)],
        [60,70,80,90,100,110,120,130,140],
    )
    quotes = {
        "NFO:AAA-2026-10-27-100-CE": raw_quote(bid=5.8, ask=6.0),
        "NFO:AAA-2026-10-27-100-PE": raw_quote(bid=4.8, ask=5.0),
    }
    out = execution.freeze_structure(rows, quotes, spot=101.0, expiry=dt.date(2026,10,27))
    assert out["atm_strike"] == 100.0
    assert out["lower_put"]["strike"] <= 78.0
    assert out["upper_call"]["strike"] >= 122.0
```

- [ ] **Step 3: Write failing freshness tests**

```python
def test_old_last_trade_with_current_executable_book_is_fresh():
    requested = aware("2026-10-08T15:10:01+05:30")
    received = aware("2026-10-08T15:10:02+05:30")
    leg = execution.normalize_live_quote(
        contract(lot_size=100),
        raw_quote(
            last_trade_time=aware("2026-10-08T14:40:00+05:30"),
            timestamp=aware("2026-10-08T15:10:01+05:30"),
            bid=10, bid_qty=200, ask=10.2, ask_qty=200,
        ),
        requested,
        received,
    )
    assert leg["last_trade_stale_600s"] is True
    assert execution.execution_fresh(leg, "SELL", 100) == (True, None)


def test_quote_transport_over_15_seconds_fails_closed():
    leg = {
        "api_latency_ms": 15001,
        "best_bid": 10.0, "best_bid_quantity": 200,
        "best_ask": 10.2, "best_ask_quantity": 200,
    }
    assert execution.execution_fresh(leg, "SELL", 100)[0] is False


def test_insufficient_top_level_quantity_fails_primary_execution():
    leg = {
        "api_latency_ms": 300,
        "best_bid": 10.0, "best_bid_quantity": 50,
        "best_ask": 10.2, "best_ask_quantity": 200,
    }
    assert execution.execution_fresh(leg, "SELL", 100) == (False, "UNAVAILABLE_QUANTITY")
```

- [ ] **Step 4: Write failing fee-model test**

Use a fixed synthetic eight-fill round trip and assert every component separately:

```python
def test_fee_model_is_versioned_and_componentized():
    fills = [
        {"side":"SELL","price":100.0,"quantity":100},
        {"side":"SELL","price":90.0,"quantity":100},
        {"side":"BUY","price":10.0,"quantity":100},
        {"side":"BUY","price":8.0,"quantity":100},
        {"side":"BUY","price":60.0,"quantity":100},
        {"side":"BUY","price":55.0,"quantity":100},
        {"side":"SELL","price":4.0,"quantity":100},
        {"side":"SELL","price":3.0,"quantity":100},
    ]
    out = execution.calculate_option_charges(fills)
    assert out["model_version"] == "ZERODHA_NSE_EQ_OPT_2026_04_V1"
    assert out["brokerage"] == 160.0
    assert out["stt"] > 0
    assert out["exchange_transaction_charge"] > 0
    assert out["sebi_fee"] > 0
    assert out["stamp_duty"] > 0
    assert out["gst"] > 0
    assert out["total"] == round(
        out["brokerage"] + out["stt"] + out["exchange_transaction_charge"]
        + out["sebi_fee"] + out["stamp_duty"] + out["gst"], 6
    )
```

- [ ] **Step 5: Run tests and confirm RED**

```bash
pytest -q tests/test_trial25_execution.py
```

Expected: module/function failures.

- [ ] **Step 6: Implement `trial25_execution.py` minimally**

Key constants:

```python
FEE_MODEL_VERSION = "ZERODHA_NSE_EQ_OPT_2026_04_V1"
BROKERAGE_PER_ORDER = 20.0
STT_SELL_RATE = 0.0015
NSE_OPTION_TXN_RATE = 0.0003553
SEBI_RATE = 10.0 / 100_000_000.0
STAMP_BUY_RATE = 0.00003
GST_RATE = 0.18
MAX_API_LATENCY_MS = 15_000
MIN_DTE = 5
WING_IMPLIED_MOVE_MULTIPLIER = 2.0
```

Implementation rules:

```python
def execution_fresh(leg, required_side, lot_size):
    if float(leg.get("api_latency_ms") or 1e99) > MAX_API_LATENCY_MS:
        return False, "UNAVAILABLE_STALE_TRANSPORT"
    bid, ask = leg.get("best_bid"), leg.get("best_ask")
    if bid is None or ask is None or float(bid) <= 0 or float(ask) < float(bid):
        return False, "UNAVAILABLE_ONE_SIDED_BOOK"
    qty_key = "best_bid_quantity" if required_side == "SELL" else "best_ask_quantity"
    if int(leg.get(qty_key) or 0) < int(lot_size):
        return False, "UNAVAILABLE_QUANTITY"
    return True, None
```

The fee function must round only the final reported components, not intermediate turnover.

- [ ] **Step 7: Run the execution tests**

```bash
pytest -q tests/test_trial25_execution.py
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/trial25_execution.py tests/test_trial25_execution.py
git commit -m "feat: add Trial 25 execution kernel"
```

---

### Task 3: Build the Persistent Trial-25 Event State Machine

**Files:**
- Create: `app/trial25_shadow.py`
- Create: `tests/test_trial25_shadow.py`

**Interfaces:**
- Consumes:
  - frozen feasibility report path;
  - current earnings state;
  - live contract map;
  - entry/exit capture payloads from Task 4.
- Produces:
  - `load_state(path) -> dict`
  - `discover_events(...) -> dict`
  - `due_transitions(state, now) -> list[dict]`
  - `record_entry(state, event_id, capture) -> dict`
  - `record_exit(state, event_id, capture) -> dict`
  - `public_summary(state) -> dict`
  - append-only transition ledger and raw-quote ledger.

- [ ] **Step 1: Write failing state-machine tests**

At the top of `tests/test_trial25_shadow.py`, define explicit fixture helpers `earnings_state(symbol)`, `seeded_entry_due_state()`, `entry_capture()`, `completed_state()`, `different_exit_capture()`, and `state_with_completed_raw_events(n)` as plain dictionaries. Each helper must use only fields defined by the state-machine schema in this task; do not import production persistence helpers to construct expected inputs.

Then add:

```python
def test_new_event_id_is_deterministic():
    event = shadow.make_event(
        symbol="AAA",
        meeting_date=date(2026,10,9),
        entry_date=date(2026,10,8),
    )
    again = shadow.make_event(
        symbol="AAA",
        meeting_date=date(2026,10,9),
        entry_date=date(2026,10,8),
    )
    assert event["event_id"] == again["event_id"]


def test_discovery_rejects_new_post_freeze_fno_symbol():
    out = shadow.discover_events(
        earnings_state=earnings_state("NEWFNO"),
        frozen_symbols={"AAA"},
        now=aware("2026-10-08T10:00:00+05:30"),
    )
    assert out["events"][0]["state"] == "UNAVAILABLE_NOT_IN_FROZEN_UNIVERSE"


def test_retry_inside_same_slot_does_not_duplicate_entry(tmp_path):
    state = seeded_entry_due_state()
    first = shadow.record_entry(state, "evt1", entry_capture())
    second = shadow.record_entry(first, "evt1", entry_capture())
    assert second["events"]["evt1"]["entry_capture_count"] == 1
```

- [ ] **Step 2: Write failing persistence/immutability tests**

```python
def test_completed_event_cannot_be_rewritten(tmp_path):
    state = completed_state()
    with pytest.raises(shadow.Trial25IntegrityError):
        shadow.record_exit(state, "evt1", different_exit_capture())


def test_atomic_state_write_survives_reload(tmp_path):
    path = tmp_path / "trial25_state.json"
    shadow.save_state(path, seeded_state())
    assert shadow.load_state(path)["version"] == 1
```

- [ ] **Step 3: Write failing no-peeking public-summary test**

```python
def test_public_summary_contains_no_efficacy_fields_before_stage_d():
    state = state_with_completed_raw_events(12)
    summary = shadow.public_summary(state)
    encoded = json.dumps(summary).lower()
    for forbidden in ("pnl", "return_pct", "win_rate", "profit_factor", "mean_return", "median_return"):
        assert forbidden not in encoded
    assert summary["stage_d_completed"] == 12
    assert summary["stage_d_target"] == 40
```

- [ ] **Step 4: Run tests and confirm RED**

```bash
pytest -q tests/test_trial25_shadow.py
```

- [ ] **Step 5: Implement the event state machine**

Use explicit terminal states and append-only transition records:

```python
TERMINAL_UNAVAILABLE = {
    "UNAVAILABLE_NOT_IN_FROZEN_UNIVERSE",
    "UNAVAILABLE_NOT_FNO_AT_ENTRY",
    "UNAVAILABLE_CALENDAR",
    "UNAVAILABLE_ENTRY_SNAPSHOT",
    "UNAVAILABLE_EXPIRY",
    "UNAVAILABLE_ATM_BOOK",
    "UNAVAILABLE_WING_BOOK",
    "UNAVAILABLE_QUANTITY",
    "UNAVAILABLE_EXIT_BOOK",
    "UNAVAILABLE_CONTRACT_CHANGED",
}

ACTIVE_STATES = {"DISCOVERED", "ENTRY_DUE", "ENTRY_CAPTURED", "EXIT_DUE"}
COMPLETED_STATE = "COMPLETED_RAW"
```

Persist raw entry/exit legs but do not derive P&L in this module.

- [ ] **Step 6: Run tests**

```bash
pytest -q tests/test_trial25_shadow.py
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/trial25_shadow.py tests/test_trial25_shadow.py
git commit -m "feat: add Trial 25 event state machine"
```

---

### Task 4: Add Dedicated Event Quote Capture and Integrate It Sequentially After V12

**Files:**
- Modify: `app/v12_live.py`
- Modify: `app/v12_option_recorder.py` only if a small reusable quote helper is required; do not alter existing recorder semantics
- Modify: `app/background.py`
- Create: `tests/test_trial25_live_integration.py`
- Modify: `tests/test_v120_live_integration.py`
- Modify: `tests/test_v120_background_integration.py`

**Interfaces:**
- Consumes Task-2 execution functions and Task-3 state transitions.
- Produces:
  - `trial25_process_due_events(kite, ..., now, ...) -> dict`
  - background state key `trial25_shadow`.

- [ ] **Step 1: Write failing integration test proving call order**

```python
def test_trial25_quote_pass_runs_after_normal_v12_recorder(monkeypatch, tmp_path):
    calls = []

    monkeypatch.setattr(
        v12_live.v12_option_recorder,
        "record_snapshot",
        lambda *a, **k: calls.append("v12") or {"status":"CAPTURED"},
    )
    monkeypatch.setattr(
        v12_live.trial25_shadow,
        "process_due_events",
        lambda *a, **k: calls.append("trial25") or {"status":"WAITING","stage_d_completed":0},
    )

    v12_live.process_live_scan(
        object(), [], {"bullish":[],"bearish":[]}, {"1D":{},"2D":{}},
        now=aware("2026-10-08T15:10:30+05:30"),
        option_snapshot_file=tmp_path/"snap.jsonl",
        option_state_file=tmp_path/"state.json",
        earnings_state_file=tmp_path/"earn.json",
    )

    assert calls == ["v12", "trial25"]
```

- [ ] **Step 2: Write failing fail-soft test**

```python
def test_trial25_failure_does_not_break_v12_result(monkeypatch, tmp_path):
    monkeypatch.setattr(v12_live.v12_option_recorder, "record_snapshot", lambda *a, **k: {"status":"CAPTURED"})
    monkeypatch.setattr(v12_live.trial25_shadow, "process_due_events", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("trial25 down")))
    out = call_process_live_scan(tmp_path)
    assert out["recorder"]["status"] == "CAPTURED"
    assert out["trial25_shadow"]["status"] == "ERROR"
```

- [ ] **Step 3: Write failing dedicated-wing quote test**

Add a concrete fake Kite client to `tests/test_trial25_live_integration.py`:

```python
class FakeKite:
    def __init__(self, quotes):
        self.quotes = quotes
        self.quote_calls = []

    def quote(self, keys):
        keys = list(keys)
        self.quote_calls.append(keys)
        return {key: self.quotes[key] for key in keys if key in self.quotes}


def test_trial25_requests_far_wings_directly(monkeypatch, tmp_path):
    far_put = "NFO:AAA-2026-10-27-70-PE"
    far_call = "NFO:AAA-2026-10-27-130-CE"
    kite = FakeKite({
        far_put: live_book(2.0, 2.1, 100),
        far_call: live_book(2.2, 2.3, 100),
    })

    # Seed the event with ATM quotes implying wings beyond +/-6 normal ladder steps.
    state = seeded_entry_due_event(
        symbol="AAA",
        expiry="2026-10-27",
        atm_strike=100.0,
        lower_wing_symbol=far_put,
        upper_wing_symbol=far_call,
    )
    out = trial25_shadow.capture_due_entry(
        kite,
        state,
        event_id="AAA:2026-10-09:2026-10-08",
        now=aware("2026-10-08T15:10:30+05:30"),
        sleep_fn=lambda _seconds: None,
    )

    flattened = [key for call in kite.quote_calls for key in call]
    assert far_put in flattened
    assert far_call in flattened
    assert out["events"]["AAA:2026-10-09:2026-10-08"]["state"] == "ENTRY_CAPTURED"
```

In the same test file define `live_book()`, `aware()`, and `seeded_entry_due_event()` directly above this test so it is self-contained.

- [ ] **Step 4: Run focused tests and confirm RED**

```bash
pytest -q tests/test_trial25_live_integration.py tests/test_v120_live_integration.py tests/test_v120_background_integration.py
```

- [ ] **Step 5: Implement sequential Trial-25 processing**

In `app/v12_live.py`:

```python
try:
    trial25 = trial25_shadow.process_due_events(
        kite,
        now=now,
        results=results,
        earnings_state=earnings_state,
        frozen_feasibility_file=str(Path(config.V12_STORAGE_ROOT) / "research_freezes" / "v12_feasibility_10d_2026-09-21.json"),
        state_file=config.TRIAL25_STATE_FILE,
        ledger_file=config.TRIAL25_LEDGER_FILE,
        raw_quotes_file=config.TRIAL25_RAW_QUOTES_FILE,
        sleep_fn=time.sleep,
    )
except Exception as exc:
    trial25 = {"status":"ERROR", "error":str(exc), "stage_d_completed":0, "stage_d_target":40}
```

Before any dedicated `kite.quote` call, sleep at least the existing quote-pacing boundary so Trial 25 cannot cause rate-limit bursts behind the normal V12 snapshot.

- [ ] **Step 6: Add background state without a new thread**

Extend default/background state:

```python
"trial25_shadow": {
    "status": "PREREGISTERED_WAITING_EVENTS",
    "stage_d_completed": 0,
    "stage_d_target": 40,
}
```

Update the existing `_run_v12_live` projection; do not create a new scheduler thread.

- [ ] **Step 7: Run integration tests**

```bash
pytest -q tests/test_trial25_live_integration.py tests/test_v120_live_integration.py tests/test_v120_background_integration.py tests/test_v120_option_recorder.py
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/v12_live.py app/background.py tests/test_trial25_live_integration.py tests/test_v120_live_integration.py tests/test_v120_background_integration.py
git commit -m "feat: wire Trial 25 shadow capture after V12 recorder"
```

---

### Task 5: Implement the No-Peeking Stage-D Calibration Gate

**Files:**
- Create: `app/trial25_stage_d.py`
- Create: `tests/test_trial25_stage_d.py`
- Modify: `app/trial25_shadow.py`

**Interfaces:**
- Consumes: completed raw event quotes, Task-2 fee function.
- Produces:
  - `maybe_freeze_stage_d(state, calibration_file, hash_file, code_files) -> dict`
  - immutable calibration object containing only sample IDs, `sigma_D`, Stage-C N, hashes, fee-model version, and metadata.

- [ ] **Step 1: Write failing pre-40 no-peeking test**

```python
# tests/test_trial25_stage_d.py defines local helpers
# state_with_completed_raw_events(n, shuffled=False), deterministic_first_40(state),
# and freeze_40(tmp_path) as plain synthetic event dictionaries.
def test_stage_d_does_not_calculate_returns_before_40(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(stage_d, "_event_return_pct", lambda *a, **k: called.append(True) or 1.0)
    out = stage_d.maybe_freeze_stage_d(state_with_completed_raw_events(39), tmp_path/"cal.json", tmp_path/"cal.sha256", [])
    assert out["status"] == "ACCUMULATING"
    assert out["completed"] == 39
    assert called == []
```

- [ ] **Step 2: Write failing deterministic-first-40 test**

```python
def test_stage_d_freezes_exact_first_40_when_43_complete(tmp_path):
    state = state_with_completed_raw_events(43, shuffled=True)
    out = stage_d.maybe_freeze_stage_d(state, tmp_path/"cal.json", tmp_path/"cal.sha256", [])
    assert out["status"] == "FROZEN_STAGE_D"
    assert len(out["event_ids"]) == 40
    expected = deterministic_first_40(state)
    assert out["event_ids"] == expected
```

- [ ] **Step 3: Write failing calibration-content test**

```python
def test_calibration_contains_sigma_and_required_n_but_no_mean_or_event_returns(tmp_path):
    out = freeze_40(tmp_path)
    encoded = json.dumps(out).lower()
    assert out["sigma_d"] > 0
    assert out["stage_c_required_n"] >= 40
    assert "mean_return" not in encoded
    assert "event_returns" not in encoded
    assert "win_rate" not in encoded
```

- [ ] **Step 4: Write failing tamper test**

```python
def test_existing_stage_d_freeze_detects_tampering(tmp_path):
    out = freeze_40(tmp_path)
    Path(out["calibration_file"]).write_text("{}", encoding="utf-8")
    with pytest.raises(stage_d.StageDIntegrityError):
        freeze_40(tmp_path)
```

- [ ] **Step 5: Run and confirm RED**

```bash
pytest -q tests/test_trial25_stage_d.py
```

- [ ] **Step 6: Implement internal return calculation and sigma-only freeze**

The internal helper may calculate event returns only once 40 events are available:

```python
def _event_return_pct(event):
    entry_short_premium = (
        event["entry"]["atm_ce"]["best_bid"]
        + event["entry"]["atm_pe"]["best_bid"]
    ) * event["lot_size"]
    gross = (event["entry"]["net_credit_per_unit"] - event["exit"]["net_debit_per_unit"]) * event["lot_size"]
    charges = calculate_option_charges(event_fills(event))["total"]
    return 100.0 * (gross - charges) / entry_short_premium
```

Then:

```python
sigma_d = statistics.stdev(returns)
required_n = max(
    40,
    math.ceil((((1.644854 + 0.841621) * sigma_d) / 4.0) ** 2),
)
```

Persist only `sigma_d`, `stage_c_required_n`, selected event IDs, hashes, fee model, timestamp, and code hashes. Do not persist event returns or mean.

- [ ] **Step 7: Run tests**

```bash
pytest -q tests/test_trial25_stage_d.py tests/test_trial25_shadow.py
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/trial25_stage_d.py app/trial25_shadow.py tests/test_trial25_stage_d.py tests/test_trial25_shadow.py
git commit -m "feat: add no-peeking Trial 25 Stage-D calibration"
```

---

### Task 6: Add the Read-Only Trial-25 Dashboard Surface Without Efficacy Leakage

**Files:**
- Modify: `app/web.py`
- Modify: `app/templates/index.html`
- Modify: `tests/test_v120_web_api.py`
- Modify: `tests/test_v120_ui.py`
- Create: `tests/test_trial25_ui.py`

**Interfaces:**
- Consumes: `background.get_state()["trial25_shadow"]`.
- Produces: dashboard/API field `trial25_shadow`; no Trial-25 POST endpoints and no raw export endpoint.

- [ ] **Step 1: Write failing web/API surface test**

At the top of `tests/test_trial25_ui.py` define:

```python
from pathlib import Path

WEB = Path(__file__).parents[1] / "app" / "web.py"
TEMPLATE = Path(__file__).parents[1] / "app" / "templates" / "index.html"
```

Then add:

```python
def test_dashboard_state_exposes_trial25_shadow_only_as_read_only_summary():
    source = WEB.read_text(encoding="utf-8")
    assert '"trial25_shadow"' in source
    assert '/api/trial25' not in source
```

- [ ] **Step 2: Write failing no-peeking UI source test**

```python
def test_trial25_panel_has_operational_fields_and_no_efficacy_labels():
    html = TEMPLATE.read_text(encoding="utf-8")
    for required in (
        'id="trial25-shadow"',
        'STAGE D',
        'stage_d_completed',
        'execution_unavailable',
        'stale_audit',
    ):
        assert required in html
    for forbidden in (
        'Trial 25 P&L',
        'Trial 25 Win Rate',
        'Trial 25 Profit Factor',
        'Trial 25 Mean Return',
    ):
        assert forbidden not in html
```

- [ ] **Step 3: Run and confirm RED**

```bash
pytest -q tests/test_trial25_ui.py tests/test_v120_ui.py tests/test_v120_web_api.py
```

- [ ] **Step 4: Add dashboard projection**

In `app/web.py`, pass:

```python
trial25_shadow=state.get("trial25_shadow") or {
    "status":"PREREGISTERED_WAITING_EVENTS",
    "stage_d_completed":0,
    "stage_d_target":40,
}
```

and include it in `/api/dashboard-state`.

- [ ] **Step 5: Add compact panel renderer**

The panel displays only:

```text
TRIAL 25 — EARNINGS VOLATILITY / STAGE D
Status
Frozen universe count
Upcoming eligible events
Current event states
Stage-D completed X / 40
Unavailable reason counts
Fresh-book / old-last-trade audit
Calibration status
```

Do not render prices that allow manual P&L reconstruction from the dashboard.

- [ ] **Step 6: Run UI/API tests**

```bash
pytest -q tests/test_trial25_ui.py tests/test_v120_ui.py tests/test_v120_web_api.py
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/web.py app/templates/index.html tests/test_trial25_ui.py tests/test_v120_ui.py tests/test_v120_web_api.py
git commit -m "feat: add no-peeking Trial 25 Stage-D dashboard"
```

---

### Task 7: Add Release-Integrity Guards and Run the Complete Regression

**Files:**
- Create: `tests/test_trial25_release.py`
- Modify: `TRIAL25_PREREGISTRATION.md` only if exact implemented names differ from the approved spec; no behavioral relaxation
- No changes to frozen runtime artifacts.

**Interfaces:**
- Consumes all prior Trial-25 components.
- Produces one release gate that proves old research is untouched and new research cannot leak efficacy.

- [ ] **Step 1: Add release-integrity tests**

```python
def test_frozen_v12_hash_constants_are_not_rewritten_by_trial25_source():
    source = Path("app/trial25_shadow.py").read_text(encoding="utf-8")
    assert "v12_option_state_10d_2026-09-21.json" not in source or "read" in source.lower()
    assert "write_text" not in source or "research_freezes" not in source


def test_trial25_has_no_order_placement_calls():
    combined = "\n".join(
        Path(p).read_text(encoding="utf-8")
        for p in (
            "app/trial25_calendar.py",
            "app/trial25_execution.py",
            "app/trial25_shadow.py",
            "app/trial25_stage_d.py",
        )
    )
    for forbidden in (".place_order(", ".modify_order(", ".cancel_order("):
        assert forbidden not in combined


def test_no_peeking_tokens_absent_from_dashboard_projection():
    source = Path("app/web.py").read_text(encoding="utf-8")
    assert "trial25_event_return" not in source
    assert "trial25_pnl" not in source
```

Also add a runtime fixture asserting a post-freeze new F&O symbol remains excluded and a removed frozen symbol becomes `UNAVAILABLE_NOT_FNO_AT_ENTRY`.

- [ ] **Step 2: Run focused Trial-25 suite**

```bash
pytest -q   tests/test_trial25_calendar.py   tests/test_trial25_execution.py   tests/test_trial25_shadow.py   tests/test_trial25_live_integration.py   tests/test_trial25_stage_d.py   tests/test_trial25_ui.py   tests/test_trial25_release.py
```

Expected: PASS.

- [ ] **Step 3: Run V12/V12.1 compatibility suite**

```bash
pytest -q   tests/test_v120_option_recorder.py   tests/test_v120_earnings_calendar.py   tests/test_v120_live_integration.py   tests/test_v120_background_integration.py   tests/test_v120_web_api.py   tests/test_v120_ui.py   tests/test_v12_feasibility_freeze.py   tests/test_v121_index_recorder.py   tests/test_v121_stream_service.py   tests/test_v1211_release.py   tests/test_v1212_release.py
```

Expected: PASS.

- [ ] **Step 4: Compile all Python**

```bash
python -m compileall -q app tests run.py
```

Expected: exit code 0.

- [ ] **Step 5: Run the complete repository suite**

```bash
pytest -q
```

Expected: all collected tests PASS. Do not report success from a timed-out partial run; split into deterministic non-overlapping batches if the harness timeout is reached.

- [ ] **Step 6: Verify the frozen feasibility hashes on Railway before deployment**

Read-only compare the existing production freeze manifest against the previously verified values:

```text
v12_option_state_10d_2026-09-21.json
  4cb686ec834627c83103611229bf0924526655c36a44d9376abda131f32c33f4

v12_feasibility_code_10d_2026-09-21.py
  949f7c08c1ecfea9ff130468ef50a3687ae5241c94043513efd3809120f352d7

v12_feasibility_10d_2026-09-21.json
  d14361328e8ed09a5ecb81071e55ceff9d890f76dac3c5154e38e5dc32871260
```

Abort deployment if any differ.

- [ ] **Step 7: Commit release gate**

```bash
git add tests/test_trial25_release.py TRIAL25_PREREGISTRATION.md
git commit -m "test: lock Trial 25 release integrity"
```

---

### Task 8: Deploy in ARMED / No-Peeking Mode and Verify Production

**Files:**
- No new feature code unless production validation reveals a defect.
- Use Railway deployment and read-only validation tooling.

**Interfaces:**
- Consumes the verified feature branch.
- Produces a production deployment that is ready to collect earnings events beginning in October without efficacy leakage.

- [ ] **Step 1: Final branch review**

Compare the feature branch against the production base and confirm the diff contains only:

```text
Trial-25 modules/tests/docs
V12 live/background integration
storage/config paths
read-only dashboard surface
preregistration clarification
```

Reject unrelated scanner/research changes.

- [ ] **Step 2: Merge only after verification**

Merge the implementation branch to `main` with the verified commit history.

- [ ] **Step 3: Watch Railway deployment to SUCCESS**

Verify:
- one `kite-scanner` service;
- 2 GB `/data` volume still mounted;
- no new helper service;
- no restart loop;
- Gunicorn worker boots successfully.

- [ ] **Step 4: Verify Trial-25 ARMED state**

Expected public summary before the earnings season:

```json
{
  "status": "PREREGISTERED_WAITING_EVENTS",
  "stage_d_completed": 0,
  "stage_d_target": 40,
  "calibration_status": "NOT_READY"
}
```

The exact `stage_d_completed` may be greater than zero only if a genuinely eligible post-registration earnings event has already completed; no P&L fields may appear.

- [ ] **Step 5: Verify both legacy recorders remain healthy**

Check:
- Forward Option Edge Recorder continues scheduled captures;
- NIFTY Index Volatility Recorder has fresh ticks and growing 5s/60s files;
- no new quote/write/Twisted/watchdog errors.

- [ ] **Step 6: Verify post-29-Sep F&O churn behavior on the first session after the change**

For each NSE F&O addition/exclusion visible in the live instrument master:
- new post-freeze addition: ordinary V12 may record it, Trial 25 must show it as outside frozen universe;
- removed frozen symbol: Trial 25 must not create an entry unless valid contracts satisfying expiry/exit rules still exist;
- no manual list edit is needed.

- [ ] **Step 7: Stop changing Trial-25 rules**

After production acceptance, any change to event timing, wings, expiry, freshness, fees, universe, or no-peeking logic requires a new preregistration/version before outcomes are read.
