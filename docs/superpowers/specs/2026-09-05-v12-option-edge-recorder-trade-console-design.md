# V12.0 Option Edge Recorder & Trade Opportunity Console — Design

## Objective

Build the first architecture aligned with the stated operating objective:

> Indian F&O stocks → intraday to 1–2 day opportunities → derivative expression where executable.

V12.0 has two parallel jobs:

1. Surface a very small number of live **trade candidates** from facts already measured by the scanner, without relabelling them as validated alpha.
2. Start the irreversible forward option dataset required to test earnings-event repricing and final-week gamma before Trial 25 can exist.

V11.1 remains an immutable historical research record. Its monthly momentum branch is closed for current development purposes and its 31 final months remain unread.

## Design principles

- No new historical alpha read is required for V12.0.
- No Trial 25 is created or run.
- No old rejected V8/V9 playbook is reactivated.
- No midpoint fills are used for executable option economics.
- Option liquidity is measured from live bid/ask and depth; it is never assumed.
- The live trade console is an **operational ranking surface**, not a claimed probability-of-profit model.
- Missing data degrade a row to a lower operational state; they never fabricate executability.
- Research recording failures must not kill the live scanner.
- Runtime/cache files remain excluded from deployment packages.

## Subsystem A — V12 Live Trade Opportunity Console

### Inputs

Reuse the existing `oi_view.live_opportunity_radar()` output plus the matching full live scan row for each symbol. This preserves the already-transparent attention score while adding operational execution gates downstream.

### Status ladder

Every displayed candidate receives exactly one state:

- `OBSERVE`: radar score 40–54.9, or an otherwise stronger row that is materially incomplete.
- `WATCH`: radar score 55–69.9, or score >=70 but the anti-chase guard is extended / execution liquidity is not yet acceptable.
- `SETUP`: score >=70, anti-chase guard OK, and the row has enough live execution context to define a trigger/invalidation.
- `EXECUTABLE`: `SETUP` plus a live executable route:
  - FUTSTK route when the near-futures spread is finite and <= 12 bps; or
  - option route when a concrete quoted contract has two-sided prices and spread <= 4% of premium.

These thresholds are operational/liquidity conventions, not alpha thresholds. The UI must label `EXECUTABLE` as `EXECUTABLE CANDIDATE · NOT VALIDATED`.

### Execution context

Each candidate shows:

- direction and live attention score;
- price displacement, recent/day OI, RVOL, participation and relative strength/weakness;
- VWAP agreement and 4H context;
- anti-chase status / ATR extension;
- FUTSTK spread bps where available;
- option contract, DTE and option spread where available;
- preferred executable route: `FUTURES`, `OPTION`, `BOTH`, or `WAIT`;
- trigger reference: breakout level if present, otherwise VWAP;
- invalidation reference: same structural level, explicitly marked as a reference rather than a guaranteed stop;
- a `NOT VALIDATED` badge on every state.

### Candidate count

Return at most five intraday candidates and at most five 1D/2D swing candidates. Never force the list to fill. Zero is valid.

## Subsystem B — V12 Forward Option Edge Recorder

### Clock

Record at four fixed IST slots on market days:

- `OPEN_STABLE` — 09:30
- `MIDDAY` — 13:00
- `PRE_CAS` — 15:10
- `POST_CAS` — 15:37

A slot is eligible only within a seven-minute grace window and is recorded at most once per trading date. Missed slots are not backfilled from later quotes.

### Universe and quote load

Use the live NSE/Kite OPTSTK universe from the daily NFO instrument dump.

Two-stage quote architecture:

1. **Broad ATM pass** across every live F&O stock with an available spot price:
   - near and next monthly expiries;
   - nearest ATM CE + PE for each expiry;
   - used to measure tradeability across the whole universe.
2. **Deep ladder pass** for the best 40 names by observed broad-pass liquidity plus any symbol with a forthcoming financial-results board meeting:
   - near and next monthly expiries;
   - ATM ±6 listed strike steps;
   - CE + PE;
   - duplicate contracts removed.

Kite quote requests are batched at 400 instruments per call, safely below the documented 500-instrument full-quote limit already used by the codebase.

### Stored option fields

For each recorded contract persist only the fields needed for research and execution:

- snapshot date/time and slot;
- underlying symbol, spot and nearest FUTSTK price if available;
- expiry, DTE, strike, CE/PE, lot size, trading symbol;
- last price, volume, OI;
- full five-level bid and ask depth normalized to price / quantity / orders;
- best bid, best ask, midpoint when two-sided;
- spread rupees and spread percent of midpoint;
- bid IV, midpoint IV and ask IV calculated locally;
- Black-Scholes delta/gamma/theta/vega at midpoint IV when valid;
- stale/two-sided flags;
- CAS regime flag.

### Snapshot summaries

For every symbol/expiry compute:

- ATM strike;
- executable straddle bid and ask;
- straddle round-trip spread as percent of premium;
- ATM bid/mid/ask IV;
- quote-depth quality;
- liquid/not-liquid classification;
- near vs next total-variance fields needed later for scheduled-event variance extraction.

The recorder does **not** infer an earnings trade direction.

## Subsystem C — Point-in-time Earnings Calendar Recorder

### Source and cadence

Once per market day, query the official NSE corporate-board-meeting feed for forthcoming meetings over the next 45 calendar days, filtered to F&O symbols where supported.

Persist an append-only point-in-time event ledger with:

- symbol;
- meeting date;
- purpose/details;
- broadcast timestamp if supplied;
- `first_seen_at`;
- `last_seen_at`;
- `last_changed_at`;
- current active/cancelled/revised state;
- raw source fingerprint.

A revision creates a new ledger observation; historical observations are never rewritten.

Only purposes explicitly referring to financial/quarterly/audited/unaudited results are treated as earnings-event candidates.

If NSE is unavailable, the calendar status is `UNAVAILABLE`; dates are never inferred from quarterly periodicity.

## Subsystem D — V12 Feasibility Dashboard

Expose a new API/dashboard section summarizing the forward recorder without pretending it is an efficacy test.

### First-stage feasibility metrics

- trading days recorded;
- scheduled slots due / captured / missed;
- live OPTSTK symbols seen;
- symbols with two-sided ATM near-expiry quotes;
- median and 75th percentile ATM straddle round-trip spread %;
- count of names below 1%, 2%, 4% and 5% spread thresholds;
- median ATM depth and stale-quote rate;
- number of forthcoming point-in-time earnings events with usable option quotes;
- near/next term-structure coverage;
- final-week (<6 DTE) sample count.

### Feasibility interpretation

Before 10 distinct trading days: `RECORDING — NO FEASIBILITY VERDICT`.

After 10 days:

- `STOCK OPTIONS PRACTICALLY TESTABLE` when at least 20 symbols have two-sided near-expiry ATM quotes on >=70% of captured broad snapshots and their median executable ATM-straddle spread is <=4% of premium.
- otherwise `STOCK OPTION LIQUIDITY GATE NOT MET`.

This is a **market-microstructure feasibility gate only**. It is not Trial 25 and cannot activate production.

## Subsystem E — Trial 25 Lock

Trial 25 remains physically unavailable in V12.0.

The UI must say:

`TRIAL 25 LOCKED — FORWARD INDIAN OPTION DATA REQUIRED.`

No earnings-event efficacy threshold, long/short direction, or implied-vs-historical-move cutoff is registered in this build.

## Persistence and deployment

New configurable files:

- `V12_OPTION_SNAPSHOT_FILE` default `v12_option_snapshots.jsonl`
- `V12_OPTION_STATE_FILE` default `v12_option_state.json`
- `V12_EARNINGS_LEDGER_FILE` default `v12_earnings_ledger.jsonl`
- `V12_EARNINGS_STATE_FILE` default `v12_earnings_state.json`

All are runtime research state and excluded from the deployment ZIP. Export endpoints must allow the user to download the accumulated snapshot and state files before any redeploy.

## Failure handling

- Kite instrument failure: recorder skips slot with audited error state; scanner continues.
- Quote batch failure: successful batches are retained, failed batch count recorded; slot marked partial, never silently complete.
- Missing bid/ask: contract retained as one-sided/untradeable; no invented spread or IV.
- NSE calendar failure: earnings layer marked unavailable; no inferred events.
- JSONL write failure: logged and surfaced in V12 status; never crashes live scan.

## Testing

Test-first coverage must include:

1. snapshot-slot timing and deduplication;
2. near/next and ATM/deep-ladder contract selection;
3. quote batching <=400;
4. bid/mid/ask IV and depth normalization;
5. one-sided quote fail-closed behavior;
6. liquidity ranking and deep-universe construction;
7. point-in-time earnings revision ledger;
8. 10-day feasibility gate;
9. operational trade states and route classification;
10. V12 API/UI release markers;
11. Trial 25 remains unavailable;
12. V11.1 / Trial-24 holdout locks remain unchanged;
13. full regression suite, Python compilation, rendered JS syntax, clean-package and ZIP-integrity verification.

## Success condition for V12.0

V12.0 is successful when it can be deployed and immediately:

- continue the existing live scanner;
- show 0–5 operational trade candidates without claiming validation;
- begin an unconditional, auditable, point-in-time stock-option panel;
- preserve forthcoming earnings-date revisions;
- quantify whether Indian stock-option liquidity is good enough for a future earnings/gamma Trial 25;
- keep every previous holdout and rejected research branch locked.
