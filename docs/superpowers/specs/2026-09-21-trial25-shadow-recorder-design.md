# Trial 25 Shadow Recorder v1 — Design

**Date:** 2026-09-21  
**Repository:** DJDDS/DBIndicator  
**Design branch:** `trial25-shadow-recorder-design-20260921`  
**Status:** Design approved in chat; implementation not started  
**Purpose:** Build a forward, options-native earnings-event shadow recorder that can collect Stage-D evidence without exposing or optimizing efficacy outcomes.

## 1. Objective

The V12 ten-day feasibility sample has already answered the first question: Indian stock-option data are sufficiently executable to support a real forward study. The next question is narrower:

> Does a preregistered, defined-risk earnings-volatility structure retain positive executable P&L after real bid/ask crossing and actual trading charges?

This build must therefore create a **Trial-25 Stage-D data-acquisition system**, not another scanner and not a production trading strategy.

Success means:

1. Every primary eligible earnings event is determined from the frozen V12 Cohort-A universe and the point-in-time NSE earnings ledger; later NSE F&O additions are observed separately and cannot silently enter the primary trial.
2. The system captures one fixed, defined-risk ATM iron-butterfly plan before the event and the same four contracts after the event.
3. Execution evidence uses executable top-of-book bid/ask and top-level quantity, never midpoint substitution.
4. The historical V12 `stale` flag is audited and replaced for Trial 25 by an execution-freshness definition that distinguishes an old last trade from a live resting order book.
5. Trading charges are calculated under one frozen, versioned Zerodha/NSE equity-options fee model.
6. No Trial-25 P&L, win rate, profit factor, mean return, or PASS/FAIL result is calculated or displayed before exactly 40 eligible completed Stage-D events exist.
7. Existing V12/V12.1 recorders, the live directional scanner, Trial 24, and the frozen ten-day feasibility files remain unchanged.

## 2. Non-goals

This build will **not**:

- activate a live option-selling strategy;
- create trade alerts or broker orders;
- optimize the 2.0x wing rule, entry time, exit time, expiry rule, symbol universe, or spread threshold;
- use RSI, EMA, MACD, OI, volume, directional scanner scores, news sentiment, or HAR forecasts to select events;
- read Trial-24 final-holdout outcomes;
- expose individual Stage-D P&L before the 40-event calibration point;
- build the Stage-C confirmatory evaluator yet.

Stage C remains specified in `TRIAL25_PREREGISTRATION.md`, but its evaluator is intentionally deferred until Stage-D variance is frozen.

## 3. Existing invariants that remain locked

The implementation must use the existing frozen feasibility artifacts under:

- `/data/v12/research_freezes/v12_option_state_10d_2026-09-21.json`
- `/data/v12/research_freezes/v12_feasibility_code_10d_2026-09-21.py`
- `/data/v12/research_freezes/v12_feasibility_10d_2026-09-21.json`
- `/data/v12/research_freezes/v12_feasibility_10d_2026-09-21.sha256`

The research-eligible universe is exactly the frozen `tradeable_symbol_list`. No later symbol may be added because it later becomes more liquid.

NSE F&O membership can change after the freeze. Trial 25 therefore uses a prospective **intersection rule** at each event entry:

`Trial25 event-eligible = frozen tradeable_symbol_list AND a currently listed option contract set that satisfies the locked expiry/exit rules`.

If a frozen symbol has been removed/phased out of F&O and no valid contract set exists at entry, the event is recorded as `UNAVAILABLE_NOT_FNO_AT_ENTRY`. A symbol newly introduced to F&O after the freeze is **not admitted to Trial 25**, because it did not pass the frozen ten-day feasibility sample. The ordinary V12 recorder may continue recording such new F&O symbols for a future separately preregistered study; those observations cannot change the Trial-25 universe.

The existing V12 feasibility thresholds remain unchanged:

- at least 70% two-sided ATM coverage;
- median executable ATM-straddle spread no greater than 4%;
- minimum 20 qualifying symbols;
- first 10 distinct recorded trading days only.

The genuine missing 2026-09-11 09:30 snapshot remains missing.


## 3A. NSE F&O universe changes after the frozen feasibility window

The live NSE single-stock F&O universe is allowed to change after the
2026-09-21 feasibility freeze.  This must **not** silently rewrite the Trial-25
research cohort.

Two universes are therefore maintained:

### Cohort A — frozen Trial-25 primary universe

The primary Trial-25 universe remains exactly the 195 symbols in the immutable
2026-09-21 `tradeable_symbol_list`.

- A later F&O addition is **not** inserted into Cohort A.
- A later F&O exclusion does not rewrite historical eligibility.
- For a Cohort-A symbol that is being phased out by NSE, a new Trial-25 event is
  allowed only when the required option expiry and all four contracts are
  actually listed and remain valid through the planned exit.  Otherwise the
  event is marked `UNAVAILABLE_NSE_FNO_PHASEOUT`.
- Once NSE has no valid stock-option contract for the planned event window, the
  symbol cannot generate new Trial-25 observations even though it remains in
  the historical frozen Cohort-A list.

This preserves the interpretation of the original feasibility gate.

### New-F&O onboarding pool — operational observation only

Stocks introduced into NSE F&O after the freeze are automatically discovered
from the live Kite/NSE contract master and placed in a separate
`NEW_FNO_ONBOARDING` pool.

For each new underlying the system records, from its first live F&O session:

- first F&O trading date;
- ATM two-sided coverage by fixed V12 slot;
- executable ATM-straddle spread;
- term-structure availability;
- quote-freshness/book-quality diagnostics;
- lot size and listed expiry continuity.

The onboarding pool is **not part of Trial-25 Stage D or Stage C** and its
outcomes cannot affect Cohort-A thresholds.

After 10 distinct live F&O trading sessions, the same frozen V12 feasibility
formula may be applied prospectively to that new stock.  Passing this check
only makes it eligible for a separately labelled future/secondary cohort (for
example Trial-25B or a later trial); it does not retroactively join Cohort A.

This rule is particularly important for securities introduced immediately
before earnings season: brand-new option chains must establish actual Indian
bid/ask liquidity before they can influence an efficacy study.

### Daily exchange-universe reconciliation

At process start and before each fixed option snapshot, the current stock-option
underlyings are derived from the live NFO contract master rather than from a
hard-coded F&O list.

The reconciliation records:

- newly present underlyings;
- underlyings in NSE phase-out that still have live contracts;
- underlyings with no remaining live contracts;
- first/last contract dates seen.

The reconciliation is operational metadata only.  It cannot alter the frozen
Cohort-A feasibility artifact.


## 4. Architecture

The feature is split into five small units with explicit interfaces.

### 4.1 `app/trial25_calendar.py`

Purpose: convert the existing point-in-time earnings ledger into deterministic event dates and trading-session dates.

Responsibilities:

- read only the current V12 earnings state/ledger;
- accept only `ACTIVE` or `REVISED` financial-results events whose `first_seen_at` / current revision was recorded before the fixed entry capture;
- preserve revisions using the latest meeting date known before entry; a revision first observed after entry cannot rewrite the event;
- calculate the last NSE F&O trading session before the registered meeting date;
- calculate the first NSE F&O trading session after the registered meeting date.

For 2026, the trading calendar is versioned from the official NSE F&O holiday circular and is stored in code/tests as an immutable 2026 holiday set. Weekends plus those holidays are non-trading days. If the event belongs to a year for which the application has no verified F&O holiday set, Trial 25 fails closed for that event with `UNVERIFIED_TRADING_CALENDAR`; it does not guess.

This deliberately avoids adding a fragile live holiday-page parser to the critical event path.

### 4.2 `app/trial25_execution.py`

Purpose: pure contract selection, quote-quality validation, charge calculation, and synthetic-testable execution maths.

Responsibilities:

- select the event expiry and ATM strike;
- select the two protective wings;
- validate top-of-book execution;
- record quote-freshness diagnostics;
- calculate the versioned fee schedule when Stage-D calibration is eventually permitted.

No file I/O and no dashboard logic belongs here.

### 4.3 `app/trial25_shadow.py`

Purpose: Trial-25 event state machine and persistence.

States:

`DISCOVERED -> ENTRY_DUE -> ENTRY_CAPTURED -> EXIT_DUE -> COMPLETED_RAW`

or fail-closed terminal states such as:

- `UNAVAILABLE_NOT_IN_FROZEN_UNIVERSE`
- `UNAVAILABLE_NOT_FNO_AT_ENTRY`
- `UNAVAILABLE_CALENDAR`
- `UNAVAILABLE_ENTRY_SNAPSHOT`
- `UNAVAILABLE_EXPIRY`
- `UNAVAILABLE_ATM_BOOK`
- `UNAVAILABLE_WING_BOOK`
- `UNAVAILABLE_QUANTITY`
- `UNAVAILABLE_EXIT_BOOK`
- `UNAVAILABLE_CONTRACT_CHANGED`

Each transition is append-only in an event ledger and summarized in an atomic JSON state file.

### 4.4 `app/trial25_stage_d.py`

Purpose: no-peeking Stage-D gate.

Before 40 eligible `COMPLETED_RAW` events:

- it may return only counts, missingness reasons, quote-quality summaries, and stale-audit summaries;
- it must not calculate individual event return, mean return, median return, win rate, profit factor, or a direction-of-effect statistic.

At exactly 40 eligible completed events:

- it computes only the primary-return sample standard deviation needed by the preregistered power formula;
- freezes `sigma_D`, the exact 40 event IDs, the fee-model version, the code hash, and the resulting Stage-C required N;
- does not publish the Stage-D mean or event-level returns;
- writes an immutable hash-backed Stage-D calibration artifact.

### 4.5 Dashboard integration

The existing dashboard receives a compact `trial25_shadow` surface. It shows:

- `PREREGISTERED / STAGE D`;
- frozen eligible-universe count;
- upcoming point-in-time earnings events;
- current event states;
- entry/exit capture status;
- execution-unavailable reasons;
- `Stage-D completed: X / 40`;
- stale-audit counts;
- Stage-D calibration status.

It must not show P&L fields before the calibration gate.

No Trial-25 raw-quote export endpoint is added in v1.

## 5. Exact event timing

For an accepted earnings meeting date:

- **Entry session:** last verified NSE F&O trading session strictly before the meeting date.
- **Entry slot:** existing V12 `PRE_CAS` time, 15:10 IST, using the existing 7-minute capture grace window.
- **Exit session:** first verified NSE F&O trading session strictly after the meeting date.
- **Exit slot:** existing V12 `OPEN_STABLE` time, 09:30 IST, using the same 7-minute grace window.

If the required slot is missed, the event becomes unavailable. A later quote is not substituted.

The system does not use the 13:00 or 15:37 snapshots to rescue an event.

## 6. Contract-selection contract

At entry, for each eligible symbol:

1. Verify the symbol is in the frozen eligible universe.
2. Verify the current Kite/NSE instrument master exposes a valid live stock-option contract set for the symbol. This is the operational source of truth for post-freeze F&O additions/exclusions; no manually maintained membership list may override the actual listed contracts.
3. Use the exact underlying spot captured for the event request.
4. Consider listed stock-option expiries that:
   - expire strictly after the planned exit session; and
   - have at least **5 calendar DTE at entry**.
5. Select the nearest such expiry.
6. Select the strike nearest spot as ATM.
7. Fetch the ATM CE and PE executable quotes.
8. Define executable implied-move points as:
   `ATM_CE_ask + ATM_PE_ask`.
9. Protective put target:
   `ATM - 2.0 * implied_move_points`.
10. Protective call target:
   `ATM + 2.0 * implied_move_points`.
11. Select the nearest listed put strike at or below the put target and the nearest listed call strike at or above the call target.
12. Fetch those exact protective contracts.
13. Freeze the four trading symbols and instrument tokens in the event record.

The exact same four contracts are used at exit. There is no re-centering after the event.

The 5-DTE rule is a pre-outcome clarification to keep the event away from immediate stock-option physical-settlement/expiry mechanics. The implementation must update the preregistration text before any Trial-25 event is admitted.

## 7. Dedicated quote path and rate-limit safety

Trial 25 will not depend on the current V12 deep-ladder limit of 40 symbols or +/-6 strikes. Earnings symbols remain prioritized in the general recorder, but Trial 25 performs a small **dedicated event quote pass** so the required 2.0x wings are guaranteed to be requested when they exist.

The call sequence is serialized inside the existing V12 live-processing path:

1. normal V12 fixed-slot recorder;
2. conservative wait respecting Kite's quote-rate limit;
3. Trial-25 event quote capture for only due event symbols.

No new thread performs Kite quote requests.

The event quote pass is fail-soft: any Trial-25 error is recorded in Trial-25 state and must never stop the normal scanner or V12/V12.1 recorders.

## 8. Quote freshness and the stale-quote audit

The frozen ten-day sample reported a high legacy stale-quote rate because the current V12 helper marks a contract stale from `last_trade_time` (falling back to quote timestamp) when that value is older than 600 seconds.

That measure is useful as an **activity diagnostic**, but it is not sufficient to decide whether a currently returned resting bid/ask can execute. Trial 25 therefore separates three concepts.

For every leg record:

- `quote_request_at`
- `quote_received_at`
- `api_latency_ms`
- raw `quote_timestamp`, if supplied by Kite
- raw `last_trade_time`, if supplied
- `quote_timestamp_age_s`
- `last_trade_age_s`
- best bid price and quantity
- best ask price and quantity
- whether the book is two-sided
- legacy `last_trade_stale_600s`

### Primary Trial-25 execution-freshness gate

A leg is execution-fresh only when:

- its quote was returned by the dedicated live REST request;
- request-to-response latency is <= 15 seconds;
- best bid > 0;
- best ask >= best bid;
- the required side's top-level quantity is at least one recorded lot.

An old `last_trade_time` alone does **not** reject the leg when the current REST snapshot contains an executable two-sided book. This rule is frozen before any Trial-25 event outcome is read.

The Stage-D dashboard reports the disagreement categories:

- live executable book + last trade older than 10 minutes;
- non-executable book + recent last trade;
- both executable and recent;
- neither.

This audit tells us whether the old 50% stale rate was primarily a last-trade-age artefact without using P&L to decide the answer.

## 9. Executable iron-butterfly accounting

One event is one lot, four legs.

### Entry

- sell ATM CE at best bid;
- sell ATM PE at best bid;
- buy upper protective CE at best ask;
- buy lower protective PE at best ask.

Entry net credit per unit:

`ATM_CE_bid + ATM_PE_bid - upper_CE_ask - lower_PE_ask`.

### Exit

- buy ATM CE at best ask;
- buy ATM PE at best ask;
- sell upper protective CE at best bid;
- sell lower protective PE at best bid.

Exit net debit per unit:

`ATM_CE_ask + ATM_PE_ask - upper_CE_bid - lower_PE_bid`.

If top-level quantity is below the recorded lot size on any required side, the event is unavailable in the primary executable sample.

No midpoint fallback, last-price fallback, theoretical price, or later quote is allowed.

## 10. Frozen charge model

Trial 25 uses a versioned model:

`ZERODHA_NSE_EQ_OPT_2026_04_V1`

The rates are frozen from the current Zerodha charges schedule and NSE 2026 statutory rates:

- brokerage: Rs 20 per executed option order;
- STT: 0.15% of option premium on sell-side transactions;
- NSE equity-option transaction charge: 0.03553% of premium turnover on both sides;
- SEBI turnover fee: Rs 10 per crore of premium turnover;
- stamp duty: 0.003% of premium turnover on buy-side transactions;
- GST: 18% of brokerage + SEBI fee + exchange transaction charge;
- NSE IPFT is treated as included in the published NSE exchange transaction charge;
- no additional slippage percentage is added because executable bid/ask crossing is already used directly.

The iron butterfly normally creates four executed orders at entry and four at exit. The model assumes the normal resident-retail Rs 20/order schedule; exceptional higher brokerage caused by account debit/collateral shortfall is outside the research contract.

A pure fee function receives a list of side/price/quantity fills and returns each component plus total charges. It is covered by fixed-value unit tests.

The fee-model version is stored on every Trial-25 event and in the Stage-D freeze.

## 11. Persistence model

New paths under the existing persistent V12 root:

- `/data/v12/trial25/trial25_state.json`
- `/data/v12/trial25/trial25_event_ledger.jsonl`
- `/data/v12/trial25/trial25_raw_quotes.jsonl`
- `/data/v12/trial25/trial25_stage_d_calibration.json`
- `/data/v12/trial25/trial25_stage_d_calibration.sha256`

Rules:

- state JSON uses temp-file + atomic rename;
- ledgers are append-only;
- every raw event record has a deterministic event ID composed from symbol, registered meeting date, entry date and fixed expiry;
- duplicate scanner loops cannot create duplicate entry or exit captures;
- completed raw events are immutable;
- Stage-D calibration is write-once and hash-verified on every boot;
- the live ten-day V12 freeze files are never modified by this subsystem.

## 12. No-peeking enforcement

Before 40 eligible completed events:

- no function exposed to the dashboard returns event P&L or return;
- no dashboard route computes event P&L;
- no aggregate direction-of-effect statistic is calculated;
- no raw Trial-25 export route is provided;
- internal state stores the raw executable quotes required for later calculation, but not derived event P&L.

At the first transition where at least 40 eligible events are complete:

1. deterministically select the first 40 by exit-capture timestamp then event ID and verify those exact event IDs;
2. compute charge-adjusted event returns internally;
3. use them only to calculate sample standard deviation `sigma_D`;
4. calculate:
   `N = ceil(((1.644854 + 0.841621) * sigma_D / 4.0)^2)`;
5. set Stage-C N to at least 40;
6. freeze the event IDs, sigma, required N, code hash and fee-model hash;
7. do not save individual Stage-D P&L or the Stage-D mean in the public state/dashboard.

If the required Stage-C sample is operationally impractical, the later status is `INFEASIBLE_TO_CONFIRM`; thresholds are not loosened.

## 13. Integration points

### `app/v12_live.py`

After the normal V12 recorder attempt, call Trial-25 processing only when:

- the ten-day feasibility freeze exists and verifies;
- its status is `STOCK OPTIONS PRACTICALLY TESTABLE`;
- the current date/time is relevant to a due event transition.

Normal V12 processing remains authoritative and unaffected by Trial-25 failures.

### `app/background.py`

Expose Trial-25 state in the scanner state object only. Do not add a second scheduling thread.

### `app/web.py`

Add read-only Trial-25 summary data to the normal dashboard state/API. No POST action is needed for v1.

### dashboard template

Add one compact “TRIAL 25 — EARNINGS VOLATILITY / STAGE D” panel. The panel contains only operational and sample-count information before 40 events.

## 14. Error handling

Trial 25 always fails closed.

Examples:

- earnings feed unavailable -> preserve prior point-in-time event knowledge, do not invent an event;
- unknown holiday calendar year -> event unavailable;
- Kite login unavailable -> due capture becomes unavailable after the existing grace window;
- partial quote request -> event unavailable for the affected capture;
- missing ATM side -> event unavailable;
- wing outside listed strikes -> event unavailable;
- insufficient top-level quantity -> event unavailable;
- contract token/trading symbol differs at exit -> event unavailable;
- state write failure -> log error and preserve prior state; never silently mark completed;
- Trial-25 exception -> never propagate into the main scanner loop.

## 15. Testing strategy

Implementation is test-first.

### Unit tests

- 2026 NSE F&O holiday/session resolver, including weekends and 14-Sep-2026;
- point-in-time ACTIVE/REVISED/REMOVED earnings handling;
- frozen-universe rejection;
- post-freeze NSE F&O addition goes to NEW_FNO_ONBOARDING, not Cohort A;
- NSE phase-out/exclusion blocks a new event when no valid contracts remain through planned exit;
- entry and exit date determination;
- nearest expiry with > exit and >=5 DTE;
- ATM selection;
- 2.0x wing selection;
- same-contract exit enforcement;
- two-sided book validation;
- one-lot top-level quantity validation;
- transport-fresh vs old-last-trade distinction;
- quote-latency boundary at 15 seconds;
- Zerodha/NSE fee components;
- idempotent event-state transitions;
- no-peeking summary omits P&L fields;
- Stage-D transition when count first reaches >=40, always using exactly the deterministic first 40 events;
- Stage-D freeze immutability/hash validation.

### Integration/regression tests

- Trial-25 quote capture runs sequentially after normal V12 capture;
- Trial-25 quote errors do not break V12 recorder;
- V12/V12.1 existing regression tests remain green;
- existing frozen feasibility hashes remain unchanged;
- dashboard renders without P&L leakage;
- restart/redeploy resumes event state without duplicate capture.

## 16. Deployment sequence

1. Implement on an isolated feature branch.
2. Run focused Trial-25 tests.
3. Run existing V12/V12.1 regression tests.
4. Run full repository suite.
5. Deploy only after test verification.
6. Verify Railway persistent paths and dashboard after deployment.
7. Confirm no Trial-25 efficacy fields are visible.
8. Let the first real eligible earnings events accumulate without tuning.

## 17. Acceptance criteria

The build is accepted only if all of the following are true:

- existing V12/V12.1 recorders remain healthy;
- frozen 10-day feasibility files retain their existing hashes;
- Trial-25 primary research reads exactly the frozen Cohort-A eligible universe;
- live NSE/Kite universe reconciliation detects additions and phase-outs without rewriting Cohort A;
- post-freeze additions are recorded only in NEW_FNO_ONBOARDING until a separate prospective cohort is defined;
- earnings events are point-in-time and session dates are deterministic;
- dedicated entry/exit quotes use the exact four frozen contracts;
- transport freshness, last-trade age and book executability are recorded separately;
- one-lot top-level quantity is enforced;
- the fee model is versioned and tested;
- no P&L/return/efficacy statistic is exposed before 40 completed eligible events;
- Stage-D calibration can freeze sigma and required Stage-C N without exposing Stage-D mean;
- no production trade/order/alert behavior exists.

## 18. Design decision summary

The chosen design deliberately favors **research integrity over speed**:

- dedicated event quotes rather than assuming the +/-6-strike general recorder always contains the wings;
- static verified 2026 F&O holidays rather than guessing trading days;
- book executability rather than last-trade recency as the primary freshness gate;
- actual bid/ask and lot quantity rather than midpoint backtest prices;
- actual versioned Indian option charges rather than a generic cost percentage;
- a hard no-peeking Stage-D boundary rather than watching early P&L and adjusting the model.

This is the smallest architecture that protects the Trial-25 objective without changing the live scanner or contaminating the future efficacy sample.
