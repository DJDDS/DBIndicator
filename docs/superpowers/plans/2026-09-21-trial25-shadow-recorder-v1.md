# Trial 25 Shadow Recorder v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a forward-only Trial-25 Stage-D earnings-volatility shadow recorder that captures one executable, defined-risk iron-butterfly event per eligible earnings announcement without exposing efficacy outcomes before the fixed 40-event calibration boundary.

**Architecture:** Add four focused modules: trading-calendar/event-date resolution, executable-option mechanics and fees, persistent Trial-25 event state, and Stage-D no-peeking calibration. Integrate them sequentially after the existing V12 fixed-slot recorder so they share the current Kite session and instrument master without adding a second quote thread. The dashboard receives a read-only operational summary only.

**Tech Stack:** Python 3.11, Flask, existing Kite Connect integration, JSON/JSONL atomic persistence, pytest, Railway persistent volume.

**Spec:** `docs/superpowers/specs/2026-09-21-trial25-shadow-recorder-design.md`

## Global Constraints

- Trial 25 is research/shadow only; it must never place broker orders or generate production trade alerts.
- Frozen Trial-25 universe is exactly the 2026-09-21 V12 `tradeable_symbol_list`; new post-freeze F&O admissions are excluded.
- A frozen symbol no longer available in the live NFO master at entry is `UNAVAILABLE_NOT_FNO_AT_ENTRY`.
- Entry is the verified trading session before the earnings meeting, `PRE_CAS` 15:10 IST; exit is the verified trading session after the meeting, `OPEN_STABLE` 09:30 IST.
- Missing fixed snapshots are never backfilled with another slot.
- Structure is one-lot ATM iron butterfly with protective wings at >= 2.0x executable implied-move distance.
- Nearest expiry must expire after planned exit and have >=5 calendar DTE at entry.
- Same four contracts are frozen at entry and used at exit; no re-centering.
- Primary execution uses bid/ask plus one-lot top-level quantity; never midpoint, last price, or theoretical fallback.
- Primary execution freshness is REST-request freshness (<=15 s round-trip) plus executable book; an old last trade alone does not invalidate a current two-sided book.
- Fee model version is `ZERODHA_NSE_EQ_OPT_2026_04_V1`.
- Before the first 40 eligible completed Stage-D events, no individual or aggregate P&L/return/win-rate/PF/direction-of-effect field may be computed for any dashboard/API response.
- When completed eligible events first reach >=40, deterministically freeze exactly the first 40 ordered by exit-capture timestamp then event ID.
- Existing V12/V12.1 recorders, Trial-24 final holdout, frozen ten-day feasibility artifacts, and live directional scanner behavior remain unchanged.
- 2026 regular-session holidays are the NSE-published weekday holidays current as of 2026-09-21; Sunday Muhurat trading is not treated as a normal F&O research session.
- Post-29-Sep F&O additions/deletions are resolved from the live NFO instrument master at event entry, not from a hardcoded future roster.

## Review Focus

- **NSE roster turnover after 29-Sep:** a newly admitted symbol that appears in Kite must still be excluded if absent from the frozen 195; a frozen deleted symbol must fail closed rather than being replaced.
- **Earnings revisions around the entry boundary:** a revision first seen after entry must not rewrite the registered event date or contracts.
- **Old last trade with live resting book:** an old `last_trade_time` must be recorded as a diagnostic but not cause a false execution rejection when the dedicated REST snapshot is fresh and top-of-book is executable.
- **Several events complete in one 09:30 cycle near event #40:** calibration must always use exactly the deterministic first 40 events and never 41+.
- **Restart/redeploy during an event:** persisted entry contracts and event IDs must survive and prevent duplicate entry/exit captures.

---

### Task 1: Trading Calendar, Earnings-Date Resolution, and Frozen/Live F&O Membership

**Files:**
- Create: `app/trial25_calendar.py`
- Create: `tests/test_trial25_calendar.py`
- Modify: `TRIAL25_PREREGISTRATION.md`

**Interfaces:**
- Consumes: V12 earnings-state rows, frozen eligible-symbol set, live `contracts_map`.
- Produces:
  - `is_trading_day(day: date) -> bool`
  - `previous_trading_day(day: date) -> date`
  - `next_trading_day(day: date) -> date`
  - `resolve_event_sessions(event: dict, known_at: datetime) -> dict`
  - `fno_membership_status(symbol: str, frozen_symbols: set[str], contracts_map: dict) -> dict`

- [ ] **Step 1: Write failing calendar/session tests**

```python
# tests/test_trial25_calendar.py
import datetime as dt
from app import trial25_calendar as cal


def test_2026_nse_holidays_and_weekends_are_not_regular_sessions():
    assert cal.is_trading_day(dt.date(2026, 9, 14)) is False
    assert cal.is_trading_day(dt.date(2026, 10, 2)) is False
    assert cal.is_trading_day(dt.date(2026, 9, 20)) is False
    assert cal.is_trading_day(dt.date(2026, 9, 21)) is True


def test_event_sessions_skip_weekend_and_gandhi_jayanti():
    event = {
        "symbol": "INFY",
        "meeting_date": "2026-10-02",
        "state": "ACTIVE",
        "first_seen_at": "2026-09-25T09:00:00",
        "last_changed_at": "2026-09-25T09:00:00",
    }
    out = cal.resolve_event_sessions(event, known_at=dt.datetime(2026, 10, 1, 15, 10))
    assert out["status"] == "OK"
    assert out["entry_date"] == "2026-10-01"
    assert out["exit_date"] == "2026-10-05"


def test_unknown_calendar_year_fails_closed():
    event = {
        "symbol": "INFY",
        "meeting_date": "2027-01-15",
        "state": "ACTIVE",
        "first_seen_at": "2026-12-01T09:00:00",
        "last_changed_at": "2026-12-01T09:00:00",
    }
    assert cal.resolve_event_sessions(event, known_at=dt.datetime(2027, 1, 14, 15, 10))["status"] == "UNVERIFIED_TRADING_CALENDAR"
```

- [ ] **Step 2: Run the calendar tests and verify RED**

Run:
```bash
pytest -q tests/test_trial25_calendar.py
```

Expected: import failure because `app.trial25_calendar` does not yet exist.

- [ ] **Step 3: Implement the verified 2026 calendar**

```python
# app/trial25_calendar.py
from __future__ import annotations
import datetime as dt

NSE_FO_HOLIDAYS_2026 = frozenset({
    dt.date(2026, 1, 15), dt.date(2026, 1, 26),
    dt.date(2026, 2, 19),
    dt.date(2026, 3, 3), dt.date(2026, 3, 19), dt.date(2026, 3, 26), dt.date(2026, 3, 31),
    dt.date(2026, 4, 1), dt.date(2026, 4, 3), dt.date(2026, 4, 14),
    dt.date(2026, 5, 1), dt.date(2026, 5, 28),
    dt.date(2026, 6, 26),
    dt.date(2026, 8, 26),
    dt.date(2026, 9, 14),
    dt.date(2026, 10, 2), dt.date(2026, 10, 20),
    dt.date(2026, 11, 10), dt.date(2026, 11, 24),
    dt.date(2026, 12, 25),
})


def is_trading_day(day: dt.date) -> bool:
    if day.year != 2026:
        return False
    return day.weekday() < 5 and day not in NSE_FO_HOLIDAYS_2026


def previous_trading_day(day: dt.date) -> dt.date:
    if day.year != 2026:
        raise ValueError("UNVERIFIED_TRADING_CALENDAR")
    cur = day - dt.timedelta(days=1)
    while not is_trading_day(cur):
        cur -= dt.timedelta(days=1)
    return cur


def next_trading_day(day: dt.date) -> dt.date:
    if day.year != 2026:
        raise ValueError("UNVERIFIED_TRADING_CALENDAR")
    cur = day + dt.timedelta(days=1)
    while not is_trading_day(cur):
        cur += dt.timedelta(days=1)
    return cur
```

Then add `resolve_event_sessions` so it accepts only `ACTIVE` or `REVISED`, requires `first_seen_at <= known_at`, and returns ISO entry/exit dates. If the meeting year is not 2026, return `{"status": "UNVERIFIED_TRADING_CALENDAR"}`.

- [ ] **Step 4: Add failing post-29-Sep F&O membership tests**

```python
def test_new_post_freeze_fno_symbol_is_excluded():
    out = cal.fno_membership_status(
        "NEWFNO",
        frozen_symbols={"INFY", "TCS"},
        contracts_map={"NEWFNO": [{"instrument_type": "CE"}, {"instrument_type": "PE"}]},
    )
    assert out["status"] == "NOT_IN_FROZEN_UNIVERSE"


def test_frozen_symbol_deleted_from_live_fno_is_unavailable():
    out = cal.fno_membership_status(
        "INFY",
        frozen_symbols={"INFY", "TCS"},
        contracts_map={"TCS": [{"instrument_type": "CE"}, {"instrument_type": "PE"}]},
    )
    assert out["status"] == "UNAVAILABLE_NOT_FNO_AT_ENTRY"


def test_frozen_symbol_with_live_ce_and_pe_is_eligible():
    out = cal.fno_membership_status(
        "INFY",
        frozen_symbols={"INFY"},
        contracts_map={"INFY": [{"instrument_type": "CE"}, {"instrument_type": "PE"}]},
    )
    assert out["status"] == "OK"
```

- [ ] **Step 5: Implement frozen/live F&O membership**

```python
def fno_membership_status(symbol: str, frozen_symbols: set[str], contracts_map: dict) -> dict:
    symbol = str(symbol)
    if symbol not in set(frozen_symbols or set()):
        return {"status": "NOT_IN_FROZEN_UNIVERSE", "symbol": symbol}
    rows = list((contracts_map or {}).get(symbol) or [])
    types = {row.get("instrument_type") for row in rows}
    if not {"CE", "PE"}.issubset(types):
        return {"status": "UNAVAILABLE_NOT_FNO_AT_ENTRY", "symbol": symbol}
    return {"status": "OK", "symbol": symbol}
```

- [ ] **Step 6: Update the preregistration text before any event is admitted**

Add explicit language to `TRIAL25_PREREGISTRATION.md`:

```markdown
### F&O roster changes after the 2026-09-21 feasibility freeze

The confirmatory cohort never expands after the freeze. Securities newly
admitted to NSE equity derivatives after the freeze are not Trial-25 symbols.
A frozen symbol that is absent from the live NFO instrument master at the fixed
entry session is UNAVAILABLE_NOT_FNO_AT_ENTRY. No replacement symbol is used.
The live instrument-master observation date is stored with every event.
```

Also clarify the expiry constraint as **nearest expiry strictly after planned exit and >=5 calendar DTE at entry**.

- [ ] **Step 7: Run Task-1 tests**

Run:
```bash
pytest -q tests/test_trial25_calendar.py
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/trial25_calendar.py tests/test_trial25_calendar.py TRIAL25_PREREGISTRATION.md
git commit -m "feat: add Trial 25 calendar and frozen F&O cohort"
```

---

### Task 2: Executable Quote Freshness, Iron-Butterfly Selection, and 2026 Charges

**Files:**
- Create: `app/trial25_execution.py`
- Create: `tests/test_trial25_execution.py`

**Interfaces:**
- Consumes: option contract rows from Kite instrument master, dedicated quote payloads, spot, planned exit date.
- Produces:
  - `select_structure(contracts, spot, entry_date, exit_date, quote_lookup) -> dict`
  - `execution_leg(snapshot, side: str, lot_size: int) -> dict`
  - `quote_freshness(quote, request_at, received_at) -> dict`
  - `option_charges(fills: list[dict]) -> dict`
  - constant `FEE_MODEL_VERSION = "ZERODHA_NSE_EQ_OPT_2026_04_V1"`

- [ ] **Step 1: Write failing expiry/ATM/wing tests**

```python
# tests/test_trial25_execution.py
import datetime as dt
from app import trial25_execution as ex


def _c(symbol, expiry, strike, typ, lot=250):
    return {
        "tradingsymbol": f"{symbol}{expiry:%y%m%d}{int(strike)}{typ}",
        "instrument_token": hash((symbol, expiry, strike, typ)) & 0x7FFFFFFF,
        "expiry": expiry,
        "strike": strike,
        "instrument_type": typ,
        "lot_size": lot,
    }


def test_structure_uses_nearest_expiry_after_exit_with_at_least_5_dte():
    entry = dt.date(2026, 10, 8)
    exit_day = dt.date(2026, 10, 12)
    e1 = dt.date(2026, 10, 13)
    e2 = dt.date(2026, 10, 27)
    contracts = []
    for expiry in (e1, e2):
        for strike in (900, 950, 1000, 1050, 1100):
            contracts += [_c("ABC", expiry, strike, "CE"), _c("ABC", expiry, strike, "PE")]
    quotes = {
        ("2026-10-13", 1000, "CE"): {"best_ask": 30.0},
        ("2026-10-13", 1000, "PE"): {"best_ask": 25.0},
    }
    out = ex.structure_targets(contracts, spot=1002.0, entry_date=entry, exit_date=exit_day, atm_quote_lookup=quotes)
    assert out["expiry"] == "2026-10-13"
    assert out["atm_strike"] == 1000.0
    assert out["put_wing_strike"] == 900.0
    assert out["call_wing_strike"] == 1150.0 or out["status"] == "UNAVAILABLE_WING_OUTSIDE_LISTED_STRIKES"
```

Use a second fixture with strikes through 1150 and assert exact 900/1150 wings. This pins the 2.0 x (30+25)=110-point wing rule and verifies no inward wing substitution.

- [ ] **Step 2: Run selection tests and verify RED**

```bash
pytest -q tests/test_trial25_execution.py -k "structure"
```

- [ ] **Step 3: Implement pure structure selection**

Implement:

```python
FEE_MODEL_VERSION = "ZERODHA_NSE_EQ_OPT_2026_04_V1"
MIN_DTE = 5
WING_MULTIPLIER = 2.0
MAX_REST_LATENCY_SECONDS = 15.0


def choose_expiry(contracts, entry_date, exit_date):
    expiries = sorted({
        row["expiry"] for row in contracts
        if row.get("instrument_type") in ("CE", "PE")
        and row.get("expiry") is not None
        and row["expiry"] > exit_date
        and (row["expiry"] - entry_date).days >= MIN_DTE
    })
    return expiries[0] if expiries else None
```

`structure_targets` must select nearest ATM strike, compute implied-move points from ATM **asks**, then select a put strike at or below target and call strike at or above target. If either does not exist, return `UNAVAILABLE_WING_OUTSIDE_LISTED_STRIKES`; never pull the wing inward.

- [ ] **Step 4: Write failing freshness/book tests**

```python
def test_old_last_trade_does_not_reject_fresh_executable_book():
    request_at = dt.datetime(2026, 10, 8, 15, 10, 0)
    received_at = dt.datetime(2026, 10, 8, 15, 10, 2)
    quote = {
        "last_trade_time": dt.datetime(2026, 10, 8, 14, 40, 0),
        "timestamp": dt.datetime(2026, 10, 8, 15, 10, 1),
        "depth": {
            "buy": [{"price": 50.0, "quantity": 500}],
            "sell": [{"price": 50.5, "quantity": 500}],
        },
    }
    f = ex.quote_freshness(quote, request_at, received_at)
    assert f["transport_fresh"] is True
    assert f["last_trade_stale_600s"] is True
    leg = ex.execution_leg(quote, side="SELL", lot_size=250, request_at=request_at, received_at=received_at)
    assert leg["status"] == "OK"
    assert leg["execution_price"] == 50.0


def test_insufficient_top_level_quantity_fails_closed():
    q = {"depth": {"buy": [{"price": 50.0, "quantity": 100}], "sell": [{"price": 50.5, "quantity": 500}]}}
    out = ex.execution_leg(
        q, side="SELL", lot_size=250,
        request_at=dt.datetime(2026, 10, 8, 15, 10),
        received_at=dt.datetime(2026, 10, 8, 15, 10, 1),
    )
    assert out["status"] == "UNAVAILABLE_QUANTITY"
```

Also test 15.0 seconds accepted, >15.0 rejected, one-sided book rejected, zero/negative prices rejected, and BUY consumes best ask while SELL consumes best bid.

- [ ] **Step 5: Implement the stale-audit/execution contract**

```python
def quote_freshness(quote, request_at, received_at):
    latency = max(0.0, (received_at - request_at).total_seconds())
    last_trade = quote.get("last_trade_time")
    quote_ts = quote.get("timestamp")
    return {
        "api_latency_ms": round(latency * 1000.0, 3),
        "transport_fresh": latency <= MAX_REST_LATENCY_SECONDS,
        "quote_timestamp_age_s": _age_seconds(quote_ts, received_at),
        "last_trade_age_s": _age_seconds(last_trade, received_at),
        "last_trade_stale_600s": (
            _age_seconds(last_trade, received_at) is not None
            and _age_seconds(last_trade, received_at) > 600.0
        ),
    }
```

`execution_leg` must require a two-sided valid book even though only one side is consumed. It records both bid/ask and quantities, then selects best bid for SELL and best ask for BUY.

- [ ] **Step 6: Write failing fee-model tests**

Use a deterministic eight-fill round trip:

```python
def test_zerodha_nse_option_charge_model_components():
    fills = [
        {"side": "SELL", "price": 50.0, "quantity": 250},
        {"side": "SELL", "price": 45.0, "quantity": 250},
        {"side": "BUY", "price": 10.0, "quantity": 250},
        {"side": "BUY", "price": 8.0, "quantity": 250},
        {"side": "BUY", "price": 25.0, "quantity": 250},
        {"side": "BUY", "price": 22.0, "quantity": 250},
        {"side": "SELL", "price": 4.0, "quantity": 250},
        {"side": "SELL", "price": 3.0, "quantity": 250},
    ]
    out = ex.option_charges(fills)
    assert out["version"] == "ZERODHA_NSE_EQ_OPT_2026_04_V1"
    assert out["brokerage"] == 160.0
    assert out["sell_turnover"] == 25500.0
    assert out["buy_turnover"] == 16250.0
    assert out["total_turnover"] == 41750.0
    assert out["stt"] == 38.0  # 0.15% sell premium, rounded per model
    assert out["total"] > 160.0
```

Add exact expected transaction-charge, SEBI-fee, stamp-duty and GST assertions after computing them once from the formulas below; use round-to-paise for service charges and the current Zerodha-documented nearest-rupee STT behavior.

- [ ] **Step 7: Implement the frozen fee model**

```python
BROKERAGE_PER_ORDER = 20.0
STT_SELL_PREMIUM_RATE = 0.0015
NSE_OPTION_TXN_RATE = 0.0003553
SEBI_RATE = 10.0 / 10_000_000.0
STAMP_BUY_RATE = 0.00003
GST_RATE = 0.18


def option_charges(fills):
    fills = list(fills or [])
    sell_turnover = sum(float(x["price"]) * int(x["quantity"]) for x in fills if x["side"] == "SELL")
    buy_turnover = sum(float(x["price"]) * int(x["quantity"]) for x in fills if x["side"] == "BUY")
    total_turnover = sell_turnover + buy_turnover
    brokerage = BROKERAGE_PER_ORDER * len(fills)
    stt = float(round(sell_turnover * STT_SELL_PREMIUM_RATE))
    transaction = total_turnover * NSE_OPTION_TXN_RATE
    sebi = total_turnover * SEBI_RATE
    stamp = buy_turnover * STAMP_BUY_RATE
    gst = GST_RATE * (brokerage + transaction + sebi)
    total = brokerage + stt + transaction + sebi + stamp + gst
    return {
        "version": FEE_MODEL_VERSION,
        "brokerage": round(brokerage, 2),
        "sell_turnover": round(sell_turnover, 2),
        "buy_turnover": round(buy_turnover, 2),
        "total_turnover": round(total_turnover, 2),
        "stt": round(stt, 2),
        "transaction_charges": round(transaction, 2),
        "sebi_charges": round(sebi, 2),
        "stamp_duty": round(stamp, 2),
        "gst": round(gst, 2),
        "total": round(total, 2),
    }
```

Do not add a generic slippage percentage; the test must assert that bid/ask crossing is already represented by the chosen fill prices.

- [ ] **Step 8: Run Task-2 tests**

```bash
pytest -q tests/test_trial25_execution.py
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add app/trial25_execution.py tests/test_trial25_execution.py
git commit -m "feat: add Trial 25 executable structure and fees"
```

---

### Task 3: Persistent Trial-25 Event State Machine and Raw Quote Ledger

**Files:**
- Create: `app/trial25_shadow.py`
- Create: `tests/test_trial25_shadow.py`
- Modify: `app/v12_storage.py`
- Modify: `app/config.py`

**Interfaces:**
- Consumes:
  - frozen feasibility report;
  - earnings state;
  - live contracts map;
  - due fixed slot;
  - dedicated quote function.
- Produces:
  - `load_state(path) -> dict`
  - `discover_events(...) -> dict`
  - `process_due_events(...) -> dict`
  - `public_summary(state) -> dict`
  - persistent state/ledger/raw-quote files under `/data/v12/trial25/`.

- [ ] **Step 1: Extend V12 storage paths with failing test**

Add to the existing storage test:

```python
def test_trial25_paths_follow_persistent_v12_root(monkeypatch):
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", "/data")
    from app.v12_storage import resolve_v12_storage
    out = resolve_v12_storage(dict(os.environ))
    assert out["trial25_root"] == "/data/v12/trial25"
    assert out["trial25_state"].endswith("/v12/trial25/trial25_state.json")
    assert out["trial25_ledger"].endswith("/v12/trial25/trial25_event_ledger.jsonl")
    assert out["trial25_raw_quotes"].endswith("/v12/trial25/trial25_raw_quotes.jsonl")
```

- [ ] **Step 2: Add paths to `v12_storage.py` and `config.py`**

The resolver must derive all Trial-25 files from the same root as existing V12 persistence. Do not add a second Railway volume.

Expose:

```python
TRIAL25_ROOT
TRIAL25_STATE_FILE
TRIAL25_EVENT_LEDGER_FILE
TRIAL25_RAW_QUOTES_FILE
TRIAL25_STAGE_D_FILE
TRIAL25_STAGE_D_HASH_FILE
```

- [ ] **Step 3: Write failing discovery/idempotency tests**

```python
def test_new_fno_admission_never_creates_trial25_event(tmp_path):
    frozen = {"INFY"}
    earnings = {
        "events": {
            "NEWFNO": {
                "symbol": "NEWFNO", "meeting_date": "2026-10-09",
                "state": "ACTIVE", "first_seen_at": "2026-09-25T10:00:00",
                "last_changed_at": "2026-09-25T10:00:00",
            }
        }
    }
    state = shadow.discover_events(
        shadow.empty_state(), earnings, frozen, {"NEWFNO": [{"instrument_type": "CE"}, {"instrument_type": "PE"}]},
        now=dt.datetime(2026, 10, 8, 14, 0),
        instrument_master_date=dt.date(2026, 10, 8),
    )
    assert state["events"] == {}


def test_restart_does_not_duplicate_entry_capture(tmp_path):
    # Seed one ENTRY_CAPTURED event with four frozen contracts.
    # Calling process_due_events again at the same slot must not append another ENTRY_CAPTURED transition.
    ...
```

Replace the ellipsis with a concrete seeded state fixture in the actual test file; the test must count ledger transition records before/after the repeated call.

- [ ] **Step 4: Implement append-only event identity and atomic state**

Event ID:

```python
def event_id(symbol, meeting_date, entry_date):
    canonical = f"{symbol}|{meeting_date}|{entry_date}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
```

State skeleton:

```python
def empty_state():
    return {
        "version": 1,
        "stage": "STAGE_D_ACCUMULATING",
        "events": {},
        "last_error": None,
        "last_write_error": None,
        "last_updated_at": None,
    }
```

Use temp-file + `os.replace` for state JSON and append-only JSONL for transitions/raw quote envelopes.

Every transition record must include:

```python
{
    "event_id": ...,
    "symbol": ...,
    "meeting_date": ...,
    "entry_date": ...,
    "exit_date": ...,
    "state_from": ...,
    "state_to": ...,
    "observed_at": ...,
    "instrument_master_date": ...,
    "reason": ...,
}
```

- [ ] **Step 5: Write failing entry-capture tests**

Tests must cover:

1. frozen symbol + live F&O + due 15:10 -> freezes exact four contracts;
2. frozen symbol deleted from NFO -> `UNAVAILABLE_NOT_FNO_AT_ENTRY`;
3. post-freeze new F&O admission -> no event;
4. expiry unavailable -> `UNAVAILABLE_EXPIRY`;
5. wing unavailable -> `UNAVAILABLE_WING_BOOK`;
6. quantity short -> `UNAVAILABLE_QUANTITY`;
7. earnings revision first seen after entry cannot rewrite meeting date.

Use a fake quote function returning a deterministic mapping and request/receive timestamps.

- [ ] **Step 6: Implement dedicated entry capture**

The implementation flow inside `process_due_events` must be:

```python
if slot == "PRE_CAS":
    for event in entry_due_events:
        membership = trial25_calendar.fno_membership_status(...)
        if membership["status"] != "OK":
            terminal_unavailable(...)
            continue
        # Resolve candidate expiry/ATM.
        # Fetch ATM pair first.
        # Compute implied move from executable ATM asks.
        # Resolve exact wing contracts.
        # Fetch only missing wing quotes.
        # Validate all four execution sides and one-lot quantity.
        # Freeze contract identifiers + entry quote envelope.
```

The function receives a `quote_fetcher(keys) -> (quotes, request_at, received_at, errors)` dependency so unit tests do not touch Kite.

- [ ] **Step 7: Write failing exit-contract tests**

```python
def test_exit_uses_same_four_contracts_and_never_recenters():
    # Entry spot 1000, exit spot 1120.
    # The event must request the four entry trading symbols, not a new ATM set.
    ...
```

Also test missing exact exit symbol/token -> `UNAVAILABLE_CONTRACT_CHANGED` and one-sided exit book -> `UNAVAILABLE_EXIT_BOOK`.

- [ ] **Step 8: Implement exact-contract exit capture**

At `OPEN_STABLE` on the registered exit session, request only the stored four trading symbols. Validate the exact symbol/token pair, record exit quote envelopes, then transition to `COMPLETED_RAW`.

Do **not** calculate P&L in this module.

- [ ] **Step 9: Implement no-peeking public summary**

```python
PUBLIC_EVENT_KEYS = {
    "event_id", "symbol", "meeting_date", "entry_date", "exit_date",
    "state", "unavailable_reason", "expiry", "atm_strike",
    "put_wing_strike", "call_wing_strike",
}


def public_summary(state):
    events = []
    for raw in (state.get("events") or {}).values():
        events.append({key: raw.get(key) for key in PUBLIC_EVENT_KEYS})
    return {
        "stage": state.get("stage"),
        "completed_stage_d": sum(e.get("state") == "COMPLETED_RAW" for e in (state.get("events") or {}).values()),
        "events": sorted(events, key=lambda x: (x.get("meeting_date") or "", x.get("symbol") or "")),
    }
```

A test must recursively assert the serialized summary contains none of:

`pnl`, `profit`, `return`, `win_rate`, `profit_factor`, `mean`, `median`, `t_stat`.

- [ ] **Step 10: Run Task-3 tests**

```bash
pytest -q tests/test_trial25_shadow.py tests/test_v12_storage.py
```

Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add app/trial25_shadow.py app/v12_storage.py app/config.py tests/test_trial25_shadow.py tests/test_v12_storage.py
git commit -m "feat: add persistent Trial 25 event recorder"
```

---

### Task 4: Stage-D 40-Event No-Peeking Calibration and Immutable Freeze

**Files:**
- Create: `app/trial25_stage_d.py`
- Create: `tests/test_trial25_stage_d.py`

**Interfaces:**
- Consumes: Trial-25 state with `COMPLETED_RAW` events and raw quote envelopes.
- Produces:
  - `maybe_freeze_stage_d(state, *, calibration_file, hash_file, fee_model_version, code_path) -> dict`
  - `stage_d_status(...) -> dict`
- Internal only:
  - `_primary_return(event) -> float`

- [ ] **Step 1: Write failing under-40 no-peeking test**

```python
def test_39_events_never_compute_or_return_efficacy(monkeypatch, tmp_path):
    state = make_completed_state(39)
    called = False
    def forbidden(_event):
        nonlocal called
        called = True
        raise AssertionError("P&L must not be opened before 40 events")
    monkeypatch.setattr(stage_d, "_primary_return", forbidden)
    out = stage_d.maybe_freeze_stage_d(
        state,
        calibration_file=tmp_path / "stage_d.json",
        hash_file=tmp_path / "stage_d.sha256",
        fee_model_version="ZERODHA_NSE_EQ_OPT_2026_04_V1",
        code_path=Path(stage_d.__file__),
    )
    assert out == {"status": "ACCUMULATING", "completed": 39, "required": 40}
    assert called is False
```

- [ ] **Step 2: Write deterministic-first-40 test**

Create 42 completed events where two share the same exit timestamp. Assert the selected IDs equal:

```python
expected = [
    e["event_id"]
    for e in sorted(events, key=lambda e: (e["exit_captured_at"], e["event_id"]))[:40]
]
```

and that the freeze contains exactly those IDs.

- [ ] **Step 3: Implement private P&L only inside Stage-D**

```python
def _primary_return(event):
    lot = int(event["lot_size"])
    entry = event["entry_execution"]
    exit_ = event["exit_execution"]

    entry_credit = (
        entry["atm_ce_sell"] + entry["atm_pe_sell"]
        - entry["upper_ce_buy"] - entry["lower_pe_buy"]
    )
    exit_debit = (
        exit_["atm_ce_buy"] + exit_["atm_pe_buy"]
        - exit_["upper_ce_sell"] - exit_["lower_pe_sell"]
    )
    fills = _fee_fills_from_event(event)
    charges = trial25_execution.option_charges(fills)["total"]
    gross_rupees = (entry_credit - exit_debit) * lot
    net_rupees = gross_rupees - charges
    denom = (entry["atm_ce_sell"] + entry["atm_pe_sell"]) * lot
    if denom <= 0:
        raise ValueError("invalid ATM entry premium")
    return 100.0 * net_rupees / denom
```

This function must not be imported by `web.py`, `v12_live.py`, or `trial25_shadow.py`.

- [ ] **Step 4: Implement sigma-only calibration**

For the deterministic first 40 returns:

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
    "status": "STAGE_D_FROZEN",
    "event_ids": [... exactly 40 ...],
    "event_count": 40,
    "sigma_d": ...,
    "stage_c_required_n": ...,
    "effect_size_pct_of_atm_premium": 4.0,
    "z_alpha": 1.644854,
    "z_beta": 0.841621,
    "fee_model_version": "ZERODHA_NSE_EQ_OPT_2026_04_V1",
    "code_sha256": ...,
    "source_state_sha256": ...,
    "created_at": ...,
}
```

Do not persist event returns, Stage-D mean, win rate, PF, t-stat, or sign counts.

- [ ] **Step 5: Add immutable/hash verification tests**

Tests must prove:

- an existing valid calibration is returned as `EXISTING_VALID_FREEZE`;
- later events 41+ cannot change sigma/N;
- a changed calibration file with stale manifest fails as `FREEZE_HASH_MISMATCH`;
- a restart produces identical event IDs and values.

- [ ] **Step 6: Run Task-4 tests**

```bash
pytest -q tests/test_trial25_stage_d.py
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/trial25_stage_d.py tests/test_trial25_stage_d.py
git commit -m "feat: add no-peeking Trial 25 Stage-D calibration"
```

---

### Task 5: Sequential V12 Integration Without a Second Quote Thread

**Files:**
- Modify: `app/v12_live.py`
- Modify: `app/background.py`
- Create: `tests/test_trial25_v12_integration.py`

**Interfaces:**
- Consumes previous-task modules.
- Produces new state keys:
  - `trial25_shadow`
  - `trial25_stage_d`
- Must preserve existing return keys from `process_live_scan`.

- [ ] **Step 1: Write failing integration-order test**

Use monkeypatches:

```python
def test_trial25_runs_after_normal_v12_recorder(monkeypatch):
    calls = []
    monkeypatch.setattr(v12_live.v12_option_recorder, "record_snapshot", lambda *a, **k: calls.append("v12") or {"status": "CAPTURED"})
    monkeypatch.setattr(v12_live.trial25_shadow, "process_due_events", lambda *a, **k: calls.append("trial25") or {"status": "OK"})
    out = v12_live.process_live_scan(...)
    assert calls.index("v12") < calls.index("trial25")
```

Build the fixture with concrete temporary paths, minimal `results`, earnings state, and frozen-feasibility fixture.

- [ ] **Step 2: Write fail-soft integration test**

```python
def test_trial25_exception_never_breaks_v12(monkeypatch):
    monkeypatch.setattr(v12_live.trial25_shadow, "process_due_events", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = v12_live.process_live_scan(...)
    assert out["recorder"]["status"] in {"CAPTURED", "NOT_DUE"}
    assert out["trial25_shadow"]["status"] == "ERROR"
    assert "boom" in out["trial25_shadow"]["error"]
```

- [ ] **Step 3: Implement lazy frozen-universe loading**

Add a helper that loads `research_freezes/v12_feasibility_10d_2026-09-21.json`, verifies status and hash via the existing `v12_feasibility_freeze` verification path, and extracts `tradeable_symbol_list`.

If verification fails, Trial 25 returns `WAITING_VALID_FREEZE` and makes no quote calls.

- [ ] **Step 4: Implement sequential Trial-25 orchestration**

After `record_snapshot` completes:

1. determine whether the current fixed slot is relevant to any Trial-25 event;
2. if not, do no Trial-25 quote call;
3. if relevant, sleep through the same conservative rate-limit boundary used by V12 before dedicated option requests;
4. call `trial25_shadow.process_due_events`;
5. call `trial25_stage_d.maybe_freeze_stage_d`;
6. attach only `trial25_shadow.public_summary(...)` and `stage_d_status(...)` to application state.

Do not create a thread, timer, cron job, or second Kite client.

- [ ] **Step 5: Integrate with `background.py` state**

Where V12 result keys are assigned, add:

```python
_state["trial25_shadow"] = v12.get("trial25_shadow") or {}
_state["trial25_stage_d"] = v12.get("trial25_stage_d") or {}
```

Keep existing `v12_trial25_status` compatibility string until Task 6 changes the visible panel.

- [ ] **Step 6: Run integration + V12 regressions**

```bash
pytest -q   tests/test_trial25_v12_integration.py   tests/test_v12_option_recorder.py   tests/test_v12_live.py   tests/test_v12_feasibility.py   tests/test_v121_release.py   tests/test_v1212_release.py
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/v12_live.py app/background.py tests/test_trial25_v12_integration.py
git commit -m "feat: integrate Trial 25 shadow recorder after V12 capture"
```

---

### Task 6: No-Peeking Trial-25 Dashboard Surface

**Files:**
- Modify: `app/web.py`
- Modify: `templates/index.html`
- Create: `tests/test_trial25_dashboard.py`

**Interfaces:**
- Consumes: `state["trial25_shadow"]`, `state["trial25_stage_d"]`.
- Produces:
  - dashboard context `trial25_shadow`, `trial25_stage_d`;
  - dashboard-state JSON keys with operational-only information.

- [ ] **Step 1: Write failing API no-peeking test**

```python
FORBIDDEN = (
    "pnl", "profit_factor", "win_rate", "mean_return", "median_return",
    "event_return", "t_stat", "gross_return", "net_return",
)


def test_dashboard_state_exposes_trial25_operations_but_no_efficacy(client, monkeypatch):
    monkeypatch.setattr(web, "get_state", lambda: {
        "results": [],
        "trial25_shadow": {
            "stage": "STAGE_D_ACCUMULATING",
            "completed_stage_d": 7,
            "events": [{"event_id": "x", "symbol": "INFY", "state": "ENTRY_CAPTURED"}],
        },
        "trial25_stage_d": {"status": "ACCUMULATING", "completed": 7, "required": 40},
    })
    payload = client.get("/api/dashboard-state", headers=auth()).get_json()
    assert payload["trial25_stage_d"]["completed"] == 7
    text = json.dumps(payload["trial25_shadow"]).lower() + json.dumps(payload["trial25_stage_d"]).lower()
    for forbidden in FORBIDDEN:
        assert forbidden not in text
```

- [ ] **Step 2: Add dashboard/API context**

Add to both `dashboard()` and `api_dashboard_state()`:

```python
trial25_shadow=state.get("trial25_shadow") or {},
trial25_stage_d=state.get("trial25_stage_d") or {},
```

No endpoint returns raw Trial-25 JSONL files in v1.

- [ ] **Step 3: Add the compact dashboard panel**

Render:

```html
<section class="card" id="trial25-stage-d">
  <h3>Trial 25 — Earnings Volatility / Stage D</h3>
  <div class="status">PREREGISTERED · RESEARCH ONLY</div>
  <div>Frozen eligible universe: {{ trial25_shadow.get("frozen_universe_count", 0) }}</div>
  <div>Stage-D completed: {{ trial25_stage_d.get("completed", 0) }} / 40</div>
  <div>Calibration: {{ trial25_stage_d.get("status", "ACCUMULATING") }}</div>
  <div>Old-last-trade / live-book audit: {{ trial25_shadow.get("stale_audit", {}) }}</div>
  <!-- Event rows may show symbol/date/state/contracts/unavailable reason only. -->
</section>
```

Do not add P&L, return, win rate, PF, mean, median, direction-of-effect, or rank-by-performance fields.

- [ ] **Step 4: Add HTML leakage test**

Render the dashboard with a 7-event state and assert the HTML contains `Stage-D completed` and not any forbidden efficacy labels.

- [ ] **Step 5: Run dashboard tests**

```bash
pytest -q tests/test_trial25_dashboard.py tests/test_web.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/web.py templates/index.html tests/test_trial25_dashboard.py
git commit -m "feat: add no-peeking Trial 25 Stage-D dashboard"
```

---

### Task 7: Release Integrity, Full Regression, and Railway Acceptance

**Files:**
- Create: `TRIAL25_STAGE_D_CHANGELOG.md`
- Modify only if needed for tests: `.github/workflows/*.yml`

**Interfaces:**
- Consumes completed implementation.
- Produces a release-ready branch/commit with verified hashes and no research-state mutation.

- [ ] **Step 1: Write a release-integrity test**

Create `tests/test_trial25_release.py` that asserts:

```python
def test_trial25_preregistration_and_no_peeking_contract():
    text = Path("TRIAL25_PREREGISTRATION.md").read_text(encoding="utf-8")
    assert "ZERODHA_NSE_EQ_OPT_2026_04_V1" in text
    assert ">=5 calendar DTE" in text or "at least 5 calendar DTE" in text
    assert "UNAVAILABLE_NOT_FNO_AT_ENTRY" in text


def test_trial25_modules_do_not_import_order_execution():
    for path in (
        "app/trial25_calendar.py",
        "app/trial25_execution.py",
        "app/trial25_shadow.py",
        "app/trial25_stage_d.py",
    ):
        text = Path(path).read_text(encoding="utf-8").lower()
        assert "place_order(" not in text
        assert "kite.place_order" not in text
```

Also assert no raw Trial-25 export route exists.

- [ ] **Step 2: Record the existing frozen feasibility hashes before tests**

Expected existing hashes from the live freeze:

```text
v12_option_state_10d_2026-09-21.json
4cb686ec834627c83103611229bf0924526655c36a44d9376abda131f32c33f4

v12_feasibility_code_10d_2026-09-21.py
949f7c08c1ecfea9ff130468ef50a3687ae5241c94043513efd3809120f352d7

v12_feasibility_10d_2026-09-21.json
d14361328e8ed09a5ecb81071e55ceff9d890f76dac3c5154e38e5dc32871260
```

The deployment verification must confirm these same files still exist and validate; implementation code must never overwrite them.

- [ ] **Step 3: Run focused Trial-25 suite**

```bash
pytest -q   tests/test_trial25_calendar.py   tests/test_trial25_execution.py   tests/test_trial25_shadow.py   tests/test_trial25_stage_d.py   tests/test_trial25_v12_integration.py   tests/test_trial25_dashboard.py   tests/test_trial25_release.py
```

Expected: all PASS.

- [ ] **Step 4: Run the existing V12/V12.1 compatibility suite**

```bash
pytest -q   tests/test_v12_option_recorder.py   tests/test_v12_feasibility.py   tests/test_v12_feasibility_freeze.py   tests/test_v12_live.py   tests/test_v12_storage.py   tests/test_v121_release.py   tests/test_v1212_release.py
```

Expected: all PASS.

- [ ] **Step 5: Run compile + complete repository tests**

```bash
python -m compileall -q app tests run.py
pytest -q
```

Expected: compilation succeeds and complete suite passes with no new failures.

- [ ] **Step 6: Add changelog**

`TRIAL25_STAGE_D_CHANGELOG.md` must record:

- frozen 195-symbol feasibility cohort;
- post-29-Sep additions excluded / frozen deletions unavailable;
- 2026 NSE verified trading-calendar behavior;
- exact 15:10/09:30 timing;
- >=5 DTE and 2.0x wings;
- executable bid/ask + lot-depth rule;
- stale-last-trade audit distinction;
- fee-model version;
- 40-event no-peeking calibration;
- no production activation.

- [ ] **Step 7: Commit release verification**

```bash
git add tests/test_trial25_release.py TRIAL25_STAGE_D_CHANGELOG.md
git commit -m "test: verify Trial 25 Stage-D release integrity"
```

- [ ] **Step 8: Whole-branch review before merge**

Because the user selected **Native** execution, complete all tasks in this session and then perform one fresh whole-branch review. Review specifically for:

1. accidental efficacy leakage before 40 events;
2. dynamic F&O roster contamination;
3. duplicate event capture across restart;
4. rate-limit interference with the V12 recorder;
5. any mutation of the frozen feasibility sample.

Do not merge until review findings are resolved and tests rerun.

- [ ] **Step 9: Merge/deploy and verify Railway**

After merge to `main`, wait for Railway deployment success, then verify read-only:

- `kite-scanner` deployment = SUCCESS;
- 2 GB `/data` volume still mounted;
- both existing option recorders remain healthy;
- `/data/v12/trial25/` exists and is writable;
- Trial-25 dashboard says `STAGE D`, completed count initially 0 (or only genuinely captured forward events);
- no P&L/return fields visible;
- frozen V12 feasibility files/hashes unchanged;
- no Twisted/KiteTicker/quote/write traceback introduced.

If any acceptance check fails, rollback the Trial-25 integration commit while preserving all recorder data.

## Self-Review Result

- **Spec coverage:** all architecture, data-flow, error, execution, persistence, no-peeking, F&O-roster, dashboard, and deployment requirements have an owning task.
- **Placeholder scan:** plan contains no implementation placeholders; test fixtures that require full setup explicitly instruct the implementer to write concrete seeded state rather than leaving ellipses in committed code.
- **Type consistency:** module/function names in later tasks match the interfaces defined by earlier tasks.
- **Review-focus coverage:** all five high-risk conditions have explicit tests in Tasks 1, 2, 3, 4, and 7.
- **Scope:** Stage C is intentionally not implemented here; this plan ends with a working Stage-D recorder and calibration gate.
