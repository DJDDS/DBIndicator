# Index Option Buying V1 — Stage 3B kill-test preregistration

Status: **RESEARCH / SHADOW ONLY — NOT VALIDATED — NOT DEPLOYED**  
Preregistered: **22 September 2026**  
Parent: **Stage 3 frozen NIFTY OR90 / 3-minute / double-close / 0.15%-0.60% / 120-minute study**  
Parent tested head at handoff: `21b23934124980c7c2f4bf36d49c7d7a33fe7d70`

## Purpose

Stage 3B exists to try to falsify the Stage-3 idea quickly without changing the frozen signal.

The already-viewed Stage-3 historical result remains immutable evidence:
- 355 NIFTY signals from 2022-01-01 through 2026-08-31;
- Dhan historical option data are `PROXY_OHLC_NO_BID_ASK`;
- ATM and ITM1 were each mapped on all 355 sessions;
- this historical proxy can never satisfy an executable bid/ask P&L gate.

Stage 3B is not permission to retune OR length, trigger timeframe, breakout confirmation, range gate, buffer, stop, or 120-minute holding period.

## Frozen parent signal

Unchanged:
- NIFTY 50
- OR90
- completed 3-minute trigger bars
- two consecutive closes beyond the same breakout boundary
- opening-range width gate 0.15%-0.60%
- buffer 0.04%
- entry deadline 13:00 IST
- maximum one signal per session
- no research stop
- exit exactly +120 minutes

The canonical parent `FROZEN_SPEC_SHA256` is imported from Stage 3 and recorded in every Stage-3B manifest.

## Historical window and split

Dhan's rolling expired-options history is treated as a five-year rolling source. Stage 3B therefore fixes:

- requested historical start: **2021-09-22**
- requested historical end: **2026-08-31**
- development/descriptive split: **2021-09-22 through 2023-12-31**
- validation split: **2024-01-01 through 2025-12-31**
- holdout split: **2026-01-01 through 2026-08-31**

The original Stage-3 2022-2026 ledger is not rewritten. Any Sep-Dec 2021 extension is stored separately and may be combined only by the Stage-3B analysis runner.

The 2026 segment has already been viewed at aggregate level in Stage 3. Stage 3B therefore never labels it as a pristine untouched holdout; "holdout" is retained only as the audit's fixed split name.

## Instrument declaration

The external audit declared **ITM1** as the Stage-3B challenger. It is the primary expression for the kill decision.

This is **not** an untouched instrument-selection result because Stage-3 ATM/ITM1 aggregates were already seen before this declaration. The Stage-3B report must say so.

**ATM remains a mandatory comparator** and must be reported in the same diagnostic tables.

No additional moneyness level may be introduced in Stage 3B.

## Historical kill tests

The per-trade proxy ledger is the source of truth. The following are fixed before the Stage-3B split/tail results are inspected:

1. Report ITM1 and ATM by the fixed date splits.
2. Report bullish and bearish directions separately.
3. Report each calendar year separately.
4. Report mean, median, win rate, 5%-per-tail trimmed mean and session-bootstrap CI.
5. Report top 1%, 5% and 10% winner contribution to total premium points.
6. Report paired ITM1-minus-ATM results on the same sessions with a session-bootstrap CI.
7. Break-even friction for an expression equals its historical mean gross premium points per trade.
8. No historical OHLC row can be upgraded to executable evidence.

### Early kill floor

Before measured live friction is plugged in:

> **KILL** if ITM1 mean gross premium points over 2024-01-01 through 2026-08-31 is **<= +1.50 points per trade**.

This is a deliberately severe zero-friction test.

## Forward friction measurement

Forward data are used primarily to measure execution friction, not to wait for statistical proof of a 2-4 point efficacy edge.

A friction row represents one observed ITM1 round trip and must provide:
- signal/session identifier;
- entry-side spread/slippage contribution;
- exit-side spread/slippage contribution;
- statutory/broker charges converted to premium points;
- total `round_trip_friction_points`.

The Stage-3B primary friction estimator is frozen as the **arithmetic mean** of `round_trip_friction_points`.

Also report median, P75 and P90.

A minimum of **20 ITM1 friction observations** is required before a measured-friction Pilot/Park/Kill decision. Fewer observations return `WAITING_FRICTION`.

Historical net points are calculated mechanically as:

`historical gross premium points - measured mean round-trip friction points`

No friction threshold may be selected after seeing the outcome.

## BANK NIFTY replication

BANK NIFTY is the required zero-retune cross-index replication.

Apply the exact parent Stage-3 signal with no Bank-Nifty-specific parameter changes.

The Pilot rule requires BANK NIFTY mean gross 120-minute directional points to be positive.

For future BANK NIFTY option-premium replication, the weekly-to-monthly contract-regime change must be reported as separate regimes. It may not be used to search for a favorable cutoff.

SENSEX is optional and descriptive only in Stage 3B; it cannot substitute for the required BANK NIFTY replication.

## Timestamp alignment audit

Before relying on cross-source premium/spot mapping, compare Dhan's `spot` field with Kite NIFTY one-minute closes.

Five common sessions are selected deterministically as evenly spaced dates across the available overlap. No volatile/quiet day cherry-picking is allowed.

Report:
- matched minute count;
- mean absolute point difference;
- median absolute point difference;
- P95 absolute point difference;
- maximum absolute point difference.

This is a data-quality diagnostic, not a parameter-selection gate.

## Preregistered final decision

The base decision uses **ITM1** over 2024-01-01 through 2026-08-31.

### KILL
- zero-friction gross mean <= +1.50 points, **or**
- after at least 20 measured friction observations, mean historical net <= 0 points.

### PILOT
All must hold:
- mean net over 2024-2026 is **> +1.00 premium point per trade**;
- validation 2024-2025 mean net is **> 0**;
- 2026-to-Aug-31 mean net is **> 0**;
- BANK NIFTY zero-retune mean gross 120-minute directional points is **> 0**.

### PARK
If net is positive but Pilot does not pass, including:
- mean net between 0 and +1.00 points;
- validation and 2026 split have mixed signs;
- BANK NIFTY replication is non-positive.

If NIFTY survives but BANK NIFTY data are unavailable, return `WAITING_REPLICATION`, not Pilot.

## One refinement only

Exactly one refinement is preregistered:

> **REFINEMENT 1: EXCLUDE EXPIRY-DAY SESSIONS**

No other filter, DTE rule, volatility rule, day-of-week rule, direction rule, range sub-band, stop, target, or time-exit change is permitted in Stage 3B.

Expiry-day exclusion requires an explicit expiry-calendar input. The code must not guess historical expiry dates.

The refinement result is stored separately and **cannot overwrite the base decision**.

Maximum refinement rounds: **1**.

## Production isolation

Stage 3B:
- does not deploy to Railway production;
- does not change the live scanner;
- does not change V12/V12.1 recorder logic;
- does not touch Trial-25;
- does not create live BUY/SELL activation;
- does not merge the Stage-3 or Stage-3B branch;
- does not alter the frozen Stage-3 specification.

Any later pilot or production action requires a separate explicit approval.
