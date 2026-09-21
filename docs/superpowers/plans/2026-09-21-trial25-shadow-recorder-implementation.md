# Trial 25 Shadow Recorder v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy a forward-only, no-peeking Trial-25 Stage-D recorder for earnings-volatility events, with executable four-leg iron-butterfly quotes, current NSE F&O-universe reconciliation, versioned Indian option charges, deterministic event persistence, and a dashboard that exposes only operational/sample-count information before calibration.

**Architecture:** Add focused Trial-25 calendar/universe, execution, state-machine, and Stage-D modules beside the existing V12 recorder. The existing V12 fixed-slot recorder remains authoritative and runs first; Trial 25 runs sequentially after it only for due earnings events and fails closed without interrupting the scanner. Raw entry/exit quotes are persisted under `/data/v12/trial25`, while a separate Stage-D module enforces the first-40-event no-peeking boundary and freezes only sigma_D and the future Stage-C sample size.

**Tech Stack:** Python 3.11, Flask, pandas where already used, Kite Connect REST quotes/instrument master, JSON/JSONL atomic persistence, pytest, existing Railway persistent volume.

**Spec:** `docs/superpowers/specs/2026-09-21-trial25-shadow-recorder-design.md`

## Global Constraints

- Trial-25 primary cohort remains exactly the frozen 2026-09-21 Cohort-A `tradeable_symbol_list`; post-freeze additions never enter Cohort A.
- A Cohort-A symbol may generate a new event only if current live NFO contracts exist through the planned exit; otherwise fail closed.
- Entry is PRE_CAS 15:10 IST on the last verified NSE F&O trading session strictly before the earnings meeting date.
- Exit is OPEN_STABLE 09:30 IST on the first verified NSE F&O trading session strictly after the meeting date.
- Expiry must be strictly after exit and have at least 5 calendar DTE at entry.
- Structure is one-lot ATM iron butterfly with protective wings at or beyond +/-2.0x executable ATM implied-move points.
- Entry/exit use best executable bid/ask and one-lot top-level quantity; no midpoint, last-price, or theoretical-price fallback.
- Primary execution freshness uses live REST-request latency <=15 seconds plus executable book/quantity; old `last_trade_time` alone does not reject a live executable book.
- Fee model version is `ZERODHA_NSE_EQ_OPT_2026_04_V1`; STT uses the published nearest-rupee rounding rule.
- No Trial-25 P&L, event return, mean, win rate, PF, or efficacy verdict may be exposed before the first 40 eligible completed Stage-D events are frozen.
- Stage-D calibration always uses exactly the deterministic first 40 completed events ordered by exit capture timestamp then event ID.
- Existing V12/V12.1 recorders, Trial 24, frozen feasibility artifacts, and live scanner ranking logic remain unchanged.
- 2026 F&O holidays are fixed from NSE/FAOP/71777 plus its official 15-Jan-2026 Maharashtra-election supplement NSE/FAOP/72262: 15-Jan, 26-Jan, 03-Mar, 26-Mar, 31-Mar, 03-Apr, 14-Apr, 01-May, 28-May, 26-Jun, 14-Sep, 02-Oct, 20-Oct, 10-Nov, 24-Nov, 25-Dec.

## Review Focus

1. **Same-day event burst around the Stage-D boundary:** if count jumps from 38 to 42, calibration must use exactly first 40 by exit timestamp + event ID.
2. **Post-freeze F&O membership churn:** a newly-added F&O symbol must never enter Cohort A, and a removed Cohort-A symbol must fail closed once no valid contract survives the event window.
3. **Old last trade but live order book:** primary execution must remain eligible when transport is fresh and the book/quantity are executable, while the legacy stale diagnostic is still recorded.
4. **Expiry/holiday edge cases:** earnings dates around 14-Sep, 02-Oct, 20-Oct and weekends must resolve to the correct previous/next F&O trading session and never use an expiry that dies before exit.
5. **Restart/idempotency:** redeploy after entry capture must not duplicate the event or recenter contracts; redeploy after 40-event calibration must verify the existing hash-backed freeze rather than rewrite it.

---

### Task 1: Lock calendar/session rules and post-freeze F&O universe reconciliation

**Files:**
- Create: `app/trial25_calendar.py`
- Create: `app/trial25_universe.py`
- Test: `tests/test_trial25_calendar.py`
- Test: `tests/test_trial25_universe.py`

**Interfaces:**
- Produces: `is_fno_trading_day(day: date) -> bool`
- Produces: `previous_fno_session(day: date) -> date | None`
- Produces: `next_fno_session(day: date) -> date | None`
- Produces: `resolve_event_sessions(event: dict, observed_before: datetime) -> dict`
- Produces: `current_stock_option_underlyings(contracts_map: dict, today: date) -> set[str]`
- Produces: `reconcile_universe(frozen_symbols: set[str], contracts_map: dict, prior: dict, now: datetime) -> dict`

- [ ] **Step 1: Write failing calendar tests**

```python
# tests/test_trial25_calendar.py
import datetime as dt
from app import trial25_calendar as cal

def test_fno_holiday_and_weekend_resolution():
    assert cal.is_fno_trading_day(dt.date(2026, 9, 14)) is False
    assert cal.previous_fno_session(dt.date(2026, 9, 14)) == dt.date(2026, 9, 11)
    assert cal.next_fno_session(dt.date(2026, 9, 14)) == dt.date(2026, 9, 15)

def test_october_holidays_are_not_guessed_as_sessions():
    assert cal.is_fno_trading_day(dt.date(2026, 10, 2)) is False
    assert cal.previous_fno_session(dt.date(2026, 10, 2)) == dt.date(2026, 10, 1)
    assert cal.next_fno_session(dt.date(2026, 10, 2)) == dt.date(2026, 10, 5)

def test_event_revision_after_entry_cannot_rewrite_event():
    observed_before = dt.datetime(2026, 10, 8, 15, 10)
    event = {
        "symbol": "INFY",
        "meeting_date": "2026-10-09",
        "state": "REVISED",
        "first_seen_at": "2026-10-01T09:20:00",
        "last_changed_at": "2026-10-09T08:00:00",
        "previous_meeting_date": "2026-10-09",
    }
    out = cal.resolve_event_sessions(event, observed_before)
    assert out["status"] == "KNOWN_BEFORE_ENTRY"
    assert out["meeting_date"] == "2026-10-09"
    assert out["entry_date"] == "2026-10-08"
    assert out["exit_date"] == "2026-10-12"
```

- [ ] **Step 2: Run calendar tests and confirm RED**

Run: `pytest -q tests/test_trial25_calendar.py`

Expected: import/attribute failures because `trial25_calendar.py` does not exist.

- [ ] **Step 3: Implement the fixed 2026 F&O calendar**

```python
# app/trial25_calendar.py
from __future__ import annotations
import datetime as dt

FNO_HOLIDAYS = {
    2026: {
        dt.date(2026,1,26), dt.date(2026,3,3), dt.date(2026,3,26),
        dt.date(2026,3,31), dt.date(2026,4,3), dt.date(2026,4,14),
        dt.date(2026,5,1), dt.date(2026,5,28), dt.date(2026,6,26),
        dt.date(2026,9,14), dt.date(2026,10,2), dt.date(2026,10,20),
        dt.date(2026,11,10), dt.date(2026,11,24), dt.date(2026,12,25),
    }
}

def is_fno_trading_day(day: dt.date) -> bool:
    if day.year not in FNO_HOLIDAYS:
        return False
    return day.weekday() < 5 and day not in FNO_HOLIDAYS[day.year]

def previous_fno_session(day: dt.date) -> dt.date | None:
    if day.year not in FNO_HOLIDAYS:
        return None
    cur = day - dt.timedelta(days=1)
    for _ in range(14):
        if is_fno_trading_day(cur):
            return cur
        cur -= dt.timedelta(days=1)
    return None

def next_fno_session(day: dt.date) -> dt.date | None:
    if day.year not in FNO_HOLIDAYS:
        return None
    cur = day + dt.timedelta(days=1)
    for _ in range(14):
        if is_fno_trading_day(cur):
            return cur
        cur += dt.timedelta(days=1)
    return None
```

Implement `resolve_event_sessions()` so it accepts only ACTIVE/REVISED events known before entry, freezes the meeting date used at entry, and returns `UNVERIFIED_TRADING_CALENDAR` for unsupported years.

- [ ] **Step 4: Write failing F&O-universe tests**

```python
# tests/test_trial25_universe.py
import datetime as dt
from app import trial25_universe as u

def _c(symbol, expiry="2026-10-27"):
    return {"name": symbol, "underlying": symbol, "instrument_type": "CE",
            "expiry": dt.date.fromisoformat(expiry), "tradingsymbol": symbol+"26OCTCE"}

def test_new_fno_name_is_onboarding_not_primary():
    contracts = {"OLD": [_c("OLD")], "NEWCO": [_c("NEWCO")]}
    out = u.reconcile_universe({"OLD"}, contracts, {}, dt.datetime(2026,9,30,9,20))
    assert out["cohort_a_live"] == ["OLD"]
    assert out["post_freeze_new_fno_excluded"] == ["NEWCO"]
    assert "NEWCO" not in out["cohort_a_live"]

def test_removed_frozen_name_is_recorded_not_replaced():
    out = u.reconcile_universe({"OLD","REMOVED"}, {"OLD":[_c("OLD")]}, {}, dt.datetime(2026,10,1,9,20))
    assert out["cohort_a_live"] == ["OLD"]
    assert out["cohort_a_missing_contracts"] == ["REMOVED"]
```

- [ ] **Step 5: Implement reconciliation**

```python
# app/trial25_universe.py
def current_stock_option_underlyings(contracts_map, today):
    out = set()
    for symbol, rows in (contracts_map or {}).items():
        if any(r.get("instrument_type") in ("CE","PE") and r.get("expiry") and r["expiry"] >= today for r in rows or []):
            out.add(str(symbol))
    return out

def reconcile_universe(frozen_symbols, contracts_map, prior, now):
    frozen = {str(x) for x in frozen_symbols}
    live = current_stock_option_underlyings(contracts_map, now.date())
    added = sorted(live - frozen)
    present = sorted(live & frozen)
    missing = sorted(frozen - live)
    return {
        "asof": now.isoformat(timespec="seconds"),
        "cohort_a_live": present,
        "cohort_a_missing_contracts": missing,
        "post_freeze_new_fno_excluded": added,
        "first_seen": _merge_first_seen(prior.get("first_seen") or {}, live, now),
        "last_seen": _merge_last_seen(prior.get("last_seen") or {}, live, now),
    }
```

- [ ] **Step 6: Run Task-1 tests**

Run: `pytest -q tests/test_trial25_calendar.py tests/test_trial25_universe.py`

Expected: PASS.

- [ ] **Step 7: Commit Task 1**

```bash
git add app/trial25_calendar.py app/trial25_universe.py tests/test_trial25_calendar.py tests/test_trial25_universe.py
git commit -m "feat: lock Trial 25 calendar and F&O universe reconciliation"
```

---

### Task 2: Build pure execution, freshness and fee model

**Files:**
- Create: `app/trial25_execution.py`
- Test: `tests/test_trial25_execution.py`
- Modify: `TRIAL25_PREREGISTRATION.md` to make the already-approved >=5-DTE and book-freshness clarification explicit before any event is admitted.

**Interfaces:**
- Produces: `select_structure(contracts, spot, entry_date, exit_date) -> dict`
- Produces: `normalize_live_quote(contract, quote, requested_at, received_at) -> dict`
- Produces: `validate_leg(side, snap, lot_size) -> tuple[bool, str | None]`
- Produces: `calculate_option_charges(fills: list[dict]) -> dict`
- Produces: constant `FEE_MODEL_VERSION = "ZERODHA_NSE_EQ_OPT_2026_04_V1"`

- [ ] **Step 1: Write failing contract-selection tests**

```python
def test_nearest_expiry_requires_five_dte_and_survives_exit():
    entry = dt.date(2026,10,8)
    exit_day = dt.date(2026,10,12)
    rows = [
        c("CE", 100, "2026-10-09"), c("PE",100,"2026-10-09"),
        c("CE", 100, "2026-10-27"), c("PE",100,"2026-10-27"),
        c("CE", 80, "2026-10-27"), c("PE",80,"2026-10-27"),
        c("CE", 120, "2026-10-27"), c("PE",120,"2026-10-27"),
    ]
    out = ex.select_structure(rows, 101.0, entry, exit_day, implied_move_points=10.0)
    assert out["expiry"] == "2026-10-27"
    assert out["atm_strike"] == 100.0
    assert out["lower_put"]["strike"] == 80.0
    assert out["upper_call"]["strike"] == 120.0
```

- [ ] **Step 2: Write failing freshness tests**

```python
def test_old_last_trade_does_not_reject_live_executable_book():
    req = dt.datetime(2026,10,8,15,10,0)
    recv = dt.datetime(2026,10,8,15,10,1)
    q = {
        "timestamp": recv,
        "last_trade_time": dt.datetime(2026,10,8,14,30,0),
        "depth": {"buy":[{"price":10.0,"quantity":500}], "sell":[{"price":10.1,"quantity":500}]},
    }
    snap = ex.normalize_live_quote({"lot_size":250}, q, req, recv)
    ok, reason = ex.validate_leg("SELL", snap, 250)
    assert ok is True
    assert reason is None
    assert snap["last_trade_stale_600s"] is True

def test_latency_over_15_seconds_fails_closed():
    req = dt.datetime(2026,10,8,15,10,0)
    recv = dt.datetime(2026,10,8,15,10,16)
    snap = ex.normalize_live_quote({"lot_size":1}, {
        "depth":{"buy":[{"price":10,"quantity":1}],"sell":[{"price":11,"quantity":1}]}
    }, req, recv)
    assert ex.validate_leg("SELL", snap, 1) == (False, "QUOTE_LATENCY")
```

- [ ] **Step 3: Write fixed-value fee tests**

Use one deterministic fill basket and assert components independently.

```python
def test_fee_model_components():
    fills = [
        {"side":"SELL","price":100.0,"quantity":50},
        {"side":"BUY","price":40.0,"quantity":50},
    ]
    out = ex.calculate_option_charges(fills)
    assert out["model_version"] == "ZERODHA_NSE_EQ_OPT_2026_04_V1"
    assert out["brokerage"] == 40.0
    assert out["stt"] == 8.0
    assert out["stamp_duty"] == pytest.approx(0.06, abs=1e-6)
    assert out["total"] > out["brokerage"]
```

- [ ] **Step 4: Run execution tests and confirm RED**

Run: `pytest -q tests/test_trial25_execution.py`

Expected: module/functions missing.

- [ ] **Step 5: Implement structure selection and freshness**

```python
FEE_MODEL_VERSION = "ZERODHA_NSE_EQ_OPT_2026_04_V1"
MAX_QUOTE_LATENCY_SECONDS = 15.0

def validate_leg(side, snap, lot_size):
    if snap["api_latency_ms"] > MAX_QUOTE_LATENCY_SECONDS * 1000:
        return False, "QUOTE_LATENCY"
    if not snap["two_sided"]:
        return False, "ONE_SIDED_BOOK"
    qty = snap["best_bid_qty"] if side == "SELL" else snap["best_ask_qty"]
    if int(qty or 0) < int(lot_size or 0):
        return False, "INSUFFICIENT_TOP_QTY"
    return True, None
```

`select_structure()` must refuse missing wings rather than choose a closer strike inside the 2.0x target.

- [ ] **Step 6: Implement frozen fee formula**

```python
from decimal import Decimal, ROUND_HALF_UP

BROKERAGE_PER_ORDER = 20.0
STT_SELL_RATE = 0.0015
NSE_TXN_RATE = 0.0003553
SEBI_RATE = 10.0 / 10_000_000.0
STAMP_BUY_RATE = 0.00003
GST_RATE = 0.18

def _round_rupee_half_up(value):
    return float(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

def calculate_option_charges(fills):
    turnover = sum(float(f["price"]) * int(f["quantity"]) for f in fills)
    sell_turnover = sum(float(f["price"]) * int(f["quantity"]) for f in fills if f["side"] == "SELL")
    buy_turnover = sum(float(f["price"]) * int(f["quantity"]) for f in fills if f["side"] == "BUY")
    brokerage = BROKERAGE_PER_ORDER * len(fills)
    stt = _round_rupee_half_up(sell_turnover * STT_SELL_RATE)
    exchange = turnover * NSE_TXN_RATE
    sebi = turnover * SEBI_RATE
    stamp = buy_turnover * STAMP_BUY_RATE
    gst = (brokerage + exchange + sebi) * GST_RATE
    total = brokerage + stt + exchange + sebi + stamp + gst
    return {
        "model_version": FEE_MODEL_VERSION,
        "brokerage": round(brokerage, 8),
        "stt": round(stt, 8),
        "exchange_transaction_charge": round(exchange, 8),
        "sebi_fee": round(sebi, 8),
        "stamp_duty": round(stamp, 8),
        "gst": round(gst, 8),
        "total": round(total, 8),
    }
```

- [ ] **Step 7: Update preregistration wording before first event**

Add the exact >=5-DTE, transport-freshness and current-F&O-intersection clarifications already approved in the design. Do not change timing, 2.0x wings, effect size, Stage-D count or Stage-C hypothesis.

- [ ] **Step 8: Run Task-2 tests**

Run: `pytest -q tests/test_trial25_execution.py`

Expected: PASS.

- [ ] **Step 9: Commit Task 2**

```bash
git add app/trial25_execution.py tests/test_trial25_execution.py TRIAL25_PREREGISTRATION.md
git commit -m "feat: add Trial 25 executable structure and charge model"
```

---

### Task 3: Add persistent event state machine and idempotent raw capture

**Files:**
- Create: `app/trial25_shadow.py`
- Test: `tests/test_trial25_shadow.py`
- Create: `tests/test_trial25_storage.py`
- Modify: `app/v12_storage.py`
- Modify: `app/config.py`

**Interfaces:**
- Produces: `load_state(path) -> dict`
- Produces: `discover_events(earnings_state: dict, frozen_symbols: set[str], contracts_map: dict, now: datetime) -> list[dict]`
- Produces: `record_entry(*, state_file, ledger_file, raw_file, event: dict, structure: dict, quotes: dict, captured_at: datetime) -> dict`
- Produces: `record_exit(*, state_file, ledger_file, raw_file, event_id: str, quotes: dict, captured_at: datetime) -> dict`
- Produces: `process_due_events(kite, *, now: datetime, contracts_map: dict, earnings_state: dict, frozen_symbols: set[str], state_file, ledger_file, raw_file, sleep_fn=None) -> dict`
- Produces: `public_summary(state: dict) -> dict`
- Storage config keys: `TRIAL25_ROOT`, `TRIAL25_STATE_FILE`, `TRIAL25_LEDGER_FILE`, `TRIAL25_RAW_QUOTES_FILE`, `TRIAL25_STAGE_D_FILE`, `TRIAL25_STAGE_D_HASH_FILE`

- [ ] **Step 1: Write failing event-state tests**

```python
def test_entry_capture_freezes_exact_four_contracts_and_is_idempotent(tmp_path):
    state_file = tmp_path/"state.json"
    first = sh.record_entry(
        state_file=state_file,
        event=event_fixture(),
        structure=structure_fixture(),
        quotes=quote_fixture(),
        captured_at=dt.datetime(2026,10,8,15,10,4),
    )
    second = sh.record_entry(
        state_file=state_file,
        event=event_fixture(),
        structure=different_structure_fixture(),
        quotes=quote_fixture(),
        captured_at=dt.datetime(2026,10,8,15,10,5),
    )
    assert first["event_id"] == second["event_id"]
    assert second["contracts"] == first["contracts"]
    assert sh.load_state(state_file)["entry_capture_count"] == 1

def test_exit_requires_same_contract_ids(tmp_path):
    state_file = tmp_path/"state.json"
    ledger_file = tmp_path/"ledger.jsonl"
    raw_file = tmp_path/"raw.jsonl"
    event = {
        "event_id":"evt1","symbol":"INFY","registered_meeting_date":"2026-10-09",
        "entry_date":"2026-10-08","exit_date":"2026-10-12","status":"ENTRY_DUE",
    }
    structure = {
        "expiry":"2026-10-27",
        "contracts":[
            {"role":"ATM_CE","tradingsymbol":"INFY26OCT1000CE","instrument_token":1,"lot_size":400},
            {"role":"ATM_PE","tradingsymbol":"INFY26OCT1000PE","instrument_token":2,"lot_size":400},
            {"role":"WING_CE","tradingsymbol":"INFY26OCT1100CE","instrument_token":3,"lot_size":400},
            {"role":"WING_PE","tradingsymbol":"INFY26OCT900PE","instrument_token":4,"lot_size":400},
        ],
    }
    entry_quotes = {str(i):{"best_bid":10+i,"best_ask":11+i,"best_bid_qty":400,"best_ask_qty":400} for i in range(1,5)}
    sh.record_entry(
        state_file=state_file, ledger_file=ledger_file, raw_file=raw_file,
        event=event, structure=structure, quotes=entry_quotes,
        captured_at=dt.datetime(2026,10,8,15,10,3),
    )
    wrong_exit_quotes = {
        "999":{"instrument_token":999,"tradingsymbol":"INFY_WRONG","best_bid":5,"best_ask":6,
               "best_bid_qty":400,"best_ask_qty":400}
    }
    out = sh.record_exit(
        state_file=state_file, ledger_file=ledger_file, raw_file=raw_file,
        event_id="evt1", quotes=wrong_exit_quotes,
        captured_at=dt.datetime(2026,10,12,9,30,3),
    )
    assert out["status"] == "UNAVAILABLE_CONTRACT_CHANGED"
```

- [ ] **Step 2: Write failing no-rewrite persistence test**

```python
def test_completed_raw_event_is_immutable(tmp_path):
    state_file = tmp_path/"state.json"
    payload = {
        "version":1,
        "events":{"evt1":{"event_id":"evt1","status":"COMPLETED_RAW",
                          "exit_captured_at":"2026-10-12T09:30:03"}},
        "entry_capture_count":1,
    }
    state_file.write_text(json.dumps(payload, sort_keys=True, separators=(",",":")), encoding="utf-8")
    before = state_file.read_bytes()
    out = sh.record_exit(
        state_file=state_file, ledger_file=tmp_path/"ledger.jsonl",
        raw_file=tmp_path/"raw.jsonl", event_id="evt1", quotes={},
        captured_at=dt.datetime(2026,10,12,9,30,4),
    )
    assert out["status"] == "COMPLETED_RAW"
    assert state_file.read_bytes() == before
```

- [ ] **Step 3: Run state-machine tests and confirm RED**

Run: `pytest -q tests/test_trial25_shadow.py`

- [ ] **Step 4: Extend V12 storage paths**

In `app/v12_storage.py`, add a `trial25_root` under the same persistent V12 root and the five exact filenames from the spec. In `app/config.py`, expose them as constants without changing existing paths.

- [ ] **Step 5: Implement deterministic IDs and atomic persistence**

```python
def event_id(symbol, meeting_date, entry_date):
    raw = f"{symbol}|{meeting_date}|{entry_date}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

def _atomic_json(path, payload):
    p = Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True, separators=(",",":"), default=str), encoding="utf-8")
    tmp.replace(p)
```

The ID intentionally excludes expiry because expiry is not known until entry; expiry becomes immutable payload after entry.

- [ ] **Step 6: Implement state transitions**

Only legal transitions are:

```python
LEGAL = {
    "DISCOVERED": {"ENTRY_DUE", "UNAVAILABLE_CALENDAR", "UNAVAILABLE_NOT_FNO_AT_ENTRY"},
    "ENTRY_DUE": {"ENTRY_CAPTURED", "UNAVAILABLE_ENTRY_SNAPSHOT", "UNAVAILABLE_EXPIRY",
                  "UNAVAILABLE_ATM_BOOK", "UNAVAILABLE_WING_BOOK", "UNAVAILABLE_QUANTITY"},
    "ENTRY_CAPTURED": {"EXIT_DUE"},
    "EXIT_DUE": {"COMPLETED_RAW", "UNAVAILABLE_EXIT_BOOK", "UNAVAILABLE_CONTRACT_CHANGED"},
}
```

Every transition appends one JSON line containing event ID, old/new state, timestamp and reason.

- [ ] **Step 7: Run Task-3 tests**

Run: `pytest -q tests/test_trial25_shadow.py tests/test_trial25_storage.py`

Expected: PASS.

- [ ] **Step 8: Commit Task 3**

```bash
git add app/trial25_shadow.py app/v12_storage.py app/config.py tests/test_trial25_shadow.py tests/test_trial25_storage.py
git commit -m "feat: persist Trial 25 event state safely"
```

---

### Task 4: Enforce the Stage-D no-peeking boundary and immutable calibration

**Files:**
- Create: `app/trial25_stage_d.py`
- Test: `tests/test_trial25_stage_d.py`

**Interfaces:**
- Produces: `safe_stage_d_summary(state: dict) -> dict`
- Produces: `maybe_freeze_calibration(state, raw_quote_file, calibration_file, hash_file) -> dict`
- Private only: `_event_return(event, raw_quotes) -> float`
- Produces: `stage_c_required_n(sigma_d: float) -> int`

- [ ] **Step 1: Write no-peeking tests**

```python
FORBIDDEN = {"pnl","return","mean","median","win_rate","profit_factor","t_stat"}

def test_summary_before_40_contains_no_efficacy_fields():
    out = sd.safe_stage_d_summary(state_with_completed(39))
    flat = json.dumps(out).lower()
    for key in FORBIDDEN:
        assert key not in flat
    assert out["completed"] == 39
    assert out["target"] == 40
```

- [ ] **Step 2: Write 38->42 boundary test**

```python
def test_calibration_uses_exact_first_40_when_four_finish_together(tmp_path):
    state = state_with_completed(42)
    out = sd.maybe_freeze_calibration(state, raw_path(tmp_path), cal_path(tmp_path), hash_path(tmp_path))
    assert out["status"] == "FROZEN"
    assert len(out["event_ids"]) == 40
    expected = sorted(state["completed"], key=lambda e:(e["exit_captured_at"], e["event_id"]))[:40]
    assert out["event_ids"] == [e["event_id"] for e in expected]
```

- [ ] **Step 3: Write immutable-freeze test**

```python
def test_existing_calibration_is_verified_not_rewritten(tmp_path):
    first = freeze_40(tmp_path)
    before = Path(first["path"]).read_bytes()
    second = sd.maybe_freeze_calibration(
        state_with_completed(45), tmp_path/"raw.jsonl",
        tmp_path/"calibration.json", tmp_path/"calibration.sha256",
    )
    assert second["status"] == "EXISTING_VALID_FREEZE"
    assert Path(first["path"]).read_bytes() == before
```

- [ ] **Step 4: Run Stage-D tests and confirm RED**

Run: `pytest -q tests/test_trial25_stage_d.py`

- [ ] **Step 5: Implement the public-safe summary**

```python
def safe_stage_d_summary(state):
    events = list((state or {}).get("events", {}).values())
    completed = sum(e.get("status") == "COMPLETED_RAW" for e in events)
    reasons = Counter(e.get("status") for e in events if str(e.get("status","")).startswith("UNAVAILABLE_"))
    return {
        "status": "STAGE_D_COLLECTING" if completed < 40 else "STAGE_D_CALIBRATION_READY",
        "completed": completed,
        "target": 40,
        "unavailable_reasons": dict(sorted(reasons.items())),
        "stale_audit": _stale_audit(events),
    }
```

No calls to `_event_return()` are permitted inside this function.

- [ ] **Step 6: Implement calibration-only P&L path**

`_event_return()` may exist only in `trial25_stage_d.py`. It reconstructs the four-leg entry/exit fills, applies `trial25_execution.calculate_option_charges()`, computes return in the preregistered percent-of-ATM-short-straddle-premium unit, and returns the scalar. `maybe_freeze_calibration()` may use those scalars only to compute `statistics.stdev`; it must not persist per-event values or their mean.

```python
def stage_c_required_n(sigma_d):
    z = 1.644854 + 0.841621
    return max(40, math.ceil(((z * float(sigma_d)) / 4.0) ** 2))
```

- [ ] **Step 7: Freeze code/fee provenance**

Calibration JSON contains:

```python
{
  "status":"FROZEN_STAGE_D",
  "event_ids":["evt000","evt001","evt002","evt003","evt004","evt005","evt006","evt007","evt008","evt009",
               "evt010","evt011","evt012","evt013","evt014","evt015","evt016","evt017","evt018","evt019",
               "evt020","evt021","evt022","evt023","evt024","evt025","evt026","evt027","evt028","evt029",
               "evt030","evt031","evt032","evt033","evt034","evt035","evt036","evt037","evt038","evt039"],
  "sigma_d": 12.5,
  "stage_c_required_n": 61,
  "fee_model_version": ex.FEE_MODEL_VERSION,
  "trial25_stage_d_code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
  "trial25_execution_code_sha256": sha256(Path(ex.__file__).read_bytes()).hexdigest(),
  "frozen_at_utc":"2026-10-20T10:00:00+00:00"
}
```

Hash the complete calibration JSON and verify it on every subsequent call.

- [ ] **Step 8: Run Task-4 tests**

Run: `pytest -q tests/test_trial25_stage_d.py`

Expected: PASS.

- [ ] **Step 9: Commit Task 4**

```bash
git add app/trial25_stage_d.py tests/test_trial25_stage_d.py
git commit -m "feat: enforce Trial 25 Stage-D no-peeking calibration"
```

---

### Task 5: Integrate dedicated event quotes after the existing V12 recorder

**Files:**
- Modify: `app/v12_live.py`
- Modify: `app/background.py`
- Test: `tests/test_trial25_live_integration.py`
- Test: `tests/test_v120_live_integration.py`

**Interfaces:**
- Consumes Task-1 universe/calendar helpers.
- Consumes Task-2 execution helpers.
- Consumes Task-3 `process_due_events()`.
- Produces scanner-state field `trial25_shadow`.

- [ ] **Step 1: Write sequencing test**

```python
def test_trial25_capture_runs_after_v12_and_not_in_parallel(monkeypatch):
    calls = []
    monkeypatch.setattr(v12_option_recorder, "record_snapshot", lambda *a, **k: calls.append("v12") or {"status":"CAPTURED"})
    monkeypatch.setattr(trial25_shadow, "process_due_events", lambda *a, **k: calls.append("trial25") or {"status":"OK"})
    out = v12_live.process_live_scan(
        object(), [], {}, {},
        now=dt.datetime(2026,10,8,15,10),
        option_snapshot_file=tmp_path/"v12.jsonl",
        option_state_file=tmp_path/"v12_state.json",
        earnings_state_file=tmp_path/"earnings.json",
        deep_symbol_limit=40, grace_minutes=7,
    )
    assert calls == ["v12", "trial25"]
    assert out["trial25_shadow"]["status"] == "OK"
```

- [ ] **Step 2: Write fail-soft test**

```python
def test_trial25_error_never_breaks_existing_v12(monkeypatch):
    monkeypatch.setattr(trial25_shadow, "process_due_events", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = v12_live.process_live_scan(
        object(), [], {}, {},
        now=dt.datetime(2026,10,8,15,10),
        option_snapshot_file=tmp_path/"v12.jsonl",
        option_state_file=tmp_path/"v12_state.json",
        earnings_state_file=tmp_path/"earnings.json",
        deep_symbol_limit=40, grace_minutes=7,
    )
    assert out["recorder"]["status"] in {"CAPTURED","NOT_DUE"}
    assert out["trial25_shadow"]["status"] == "ERROR"
```

- [ ] **Step 3: Write no-extra-thread regression test**

Assert `background.py` does not create a new `threading.Thread` for Trial 25 and that Trial-25 state is returned only from the existing scan path.

- [ ] **Step 4: Run integration tests and confirm RED**

Run: `pytest -q tests/test_trial25_live_integration.py tests/test_v120_live_integration.py`

- [ ] **Step 5: Update `v12_live.process_live_scan()`**

Import `trial25_shadow`, `trial25_stage_d`, `trial25_universe`. After normal V12 recording, obtain the existing contracts map once when possible and pass it to Trial 25. Any extra REST quote request must use the existing conservative 1 r/s pacing and only due event symbols.

Return:

```python
"trial25_shadow": trial25_stage_d.safe_stage_d_summary(trial25_state)
```

Before feasibility freeze verification succeeds, return `{"status":"LOCKED_FEASIBILITY"}`.

- [ ] **Step 6: Wire background scanner state**

Where `v12_live.process_live_scan()` output is assigned today, add:

```python
state["trial25_shadow"] = v12_payload.get("trial25_shadow") or {}
```

Do not change scan rank/shortlist logic.

- [ ] **Step 7: Run Task-5 tests**

Run: `pytest -q tests/test_trial25_live_integration.py tests/test_v120_live_integration.py tests/test_v120_option_recorder.py`

Expected: PASS.

- [ ] **Step 8: Commit Task 5**

```bash
git add app/v12_live.py app/background.py tests/test_trial25_live_integration.py tests/test_v120_live_integration.py
git commit -m "feat: integrate Trial 25 sequential shadow capture"
```

---

### Task 6: Add the no-peeking Stage-D dashboard and API surface

**Files:**
- Modify: `app/web.py`
- Modify: `app/templates/index.html`
- Create: `tests/test_trial25_dashboard.py`

**Interfaces:**
- Consumes: `state["trial25_shadow"]`
- Produces: dashboard context key and API field `trial25_shadow`

- [ ] **Step 1: Write dashboard leakage test**

```python
def test_trial25_dashboard_payload_never_exposes_pnl_before_freeze(client, monkeypatch):
    seed_state({"trial25_shadow":{
        "status":"STAGE_D_COLLECTING","completed":7,"target":40,
        "unavailable_reasons":{"UNAVAILABLE_QUANTITY":2},
        "stale_audit":{"live_book_old_trade":11},
    }})
    payload = client.get("/api/dashboard-state", headers=auth()).get_json()["trial25_shadow"]
    text = json.dumps(payload).lower()
    for forbidden in ("pnl","return","mean","median","win_rate","profit_factor","t_stat"):
        assert forbidden not in text
```

- [ ] **Step 2: Write template content test**

Assert rendered HTML includes:
- `TRIAL 25 — EARNINGS VOLATILITY`
- `STAGE D`
- `7 / 40`
- unavailable reason count
- no `P&L`, `profit factor`, or `win rate` labels in the Trial-25 panel.

- [ ] **Step 3: Run dashboard tests and confirm RED**

Run: `pytest -q tests/test_trial25_dashboard.py`

- [ ] **Step 4: Wire web context/API**

In both `dashboard()` and `api_dashboard_state()`:

```python
trial25_shadow = state.get("trial25_shadow") or {
    "status":"PREREGISTERED_WAITING_EVENTS","completed":0,"target":40
}
```

Pass it through unchanged. Do not add raw quote/P&L export endpoints.

- [ ] **Step 5: Add compact dashboard panel**

Use only:
- status;
- frozen Cohort-A count;
- post-freeze new-F&O names excluded from Cohort A;
- next/upcoming event identifiers/dates;
- entry/exit state;
- completed/40;
- unavailable reason counts;
- stale-audit counts;
- calibration status.

No efficacy metric.

- [ ] **Step 6: Run Task-6 tests**

Run: `pytest -q tests/test_trial25_dashboard.py tests/test_v120_web_api.py tests/test_v120_ui.py`

Expected: PASS.

- [ ] **Step 7: Commit Task 6**

```bash
git add app/web.py app/templates/index.html tests/test_trial25_dashboard.py
git commit -m "feat: add no-peeking Trial 25 Stage-D dashboard"
```

---

### Task 7: Full regression, provenance checks and deployment verification

**Files:**
- Create: `.github/workflows/trial25-shadow-tests.yml`
- Create or modify: `tests/test_trial25_release.py`
- No production logic unless a failing regression proves a defect.

**Interfaces:**
- Release gate only.

- [ ] **Step 1: Write release-provenance tests**

```python
def test_trial25_does_not_mutate_frozen_feasibility_files():
    assert EXPECTED_STATE_SHA == "4cb686ec834627c83103611229bf0924526655c36a44d9376abda131f32c33f4"
    assert EXPECTED_FEASIBILITY_SHA == "d14361328e8ed09a5ecb81071e55ceff9d890f76dac3c5154e38e5dc32871260"
    assert EXPECTED_CODE_SHA == "949f7c08c1ecfea9ff130468ef50a3687ae5241c94043513efd3809120f352d7"

def test_no_trial25_order_or_alert_surface_exists():
    source = combined_trial25_source().lower()
    assert "place_order" not in source
    assert "telegram" not in source
```

- [ ] **Step 2: Add CI workflow**

Workflow runs:
```bash
python -m compileall -q app tests run.py
pytest -q tests/test_trial25_calendar.py           tests/test_trial25_universe.py           tests/test_trial25_execution.py           tests/test_trial25_shadow.py           tests/test_trial25_stage_d.py           tests/test_trial25_live_integration.py           tests/test_trial25_dashboard.py           tests/test_trial25_release.py
pytest -q tests/test_v120_option_recorder.py tests/test_v120_live_integration.py           tests/test_v120_feasibility.py tests/test_v121_release.py tests/test_v1212_release.py
pytest -q
```

- [ ] **Step 3: Run focused tests locally/CI**

Expected: all Trial-25 tests PASS.

- [ ] **Step 4: Run V12/V12.1 regression**

Expected: all existing recorder tests PASS.

- [ ] **Step 5: Run complete repository suite**

Expected: zero failures. Existing unrelated warnings may remain but must be listed.

- [ ] **Step 6: Whole-branch review**

Review specifically for:
- any hidden P&L leak before 40;
- any new thread making Kite requests;
- any path that adds post-freeze F&O symbols into Cohort A;
- any fallback to midpoint/last price;
- any mutation of feasibility freeze;
- any Trial-25 exception that can escape into the main scanner.

- [ ] **Step 7: Merge/deploy only after review passes**

Deploy the verified branch to Railway production using the existing single-worker configuration and attached `/data` volume.

- [ ] **Step 8: Post-deploy Railway verification**

Verify:
- latest deployment SUCCESS;
- `/data` remains the 2 GB persistent volume;
- V12 and V12.1 recorder state still healthy;
- `/data/v12/trial25/` exists and is writable;
- Trial-25 status is `PREREGISTERED_WAITING_EVENTS` or `STAGE_D_COLLECTING`;
- Stage-D completed count is 0 unless an actually eligible forward event has already occurred;
- no P&L fields appear in dashboard/API;
- no Twisted/KiteTicker/Trial-25 traceback/write errors;
- frozen feasibility hashes remain exactly unchanged.

- [ ] **Step 9: Final commit/release note**

```bash
git add .github/workflows/trial25-shadow-tests.yml tests/test_trial25_release.py
git commit -m "test: lock Trial 25 shadow-recorder release invariants"
```

## Self-review result

- **Spec coverage:** calendar, F&O churn, Cohort-A intersection, contract selection, 2.0x wings, >=5 DTE, dedicated sequential quote path, freshness audit, top-level quantity, charge model, persistence, no-peeking Stage D, dashboard, error handling, regressions and Railway verification are each assigned to a task.
- **Placeholder scan:** no TBD/TODO/ellipsis placeholders remain; every code step names concrete arguments and files.
- **Type consistency:** later tasks consume the exact function/module names defined in earlier tasks.
- **Review-focus coverage:** all five review-focus risks have explicit tests in Tasks 1, 2, 3 and 4.
- **Scope:** Stage-C efficacy evaluation remains intentionally excluded; this plan ships only Stage-D collection/calibration infrastructure as specified.
