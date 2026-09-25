# V12.3 recorder repair + forward shadow preregistration
Date: 2026-09-25

## Scope
Research-only. No production deployment, no trading-control change, no modification of the frozen V12 feasibility result, Trial-25, or V12.1 index-volatility research.

Production/main anchor before this branch:
`e5a536b82975895587b871e72c19f92010249796`

Research branch:
`research/v123-recorder-repair-20260925`

## Frozen source evidence
The 25 Sep forensic package and its component files are preserved unchanged.

- forensic archive SHA-256: `0099f2a5d5f3152d076f59748652446aac8b805cb3ff02857967e69de4507400`
- `v123_continuation_shadow.jsonl`: `b6eaaba1bf322d687588c3bdf0ffb865e02831b3434880f229022842297cc958`
- `v122b_tactical_events.jsonl`: `a2a2f8c9e97fd2eb68221971f5ba02685729de70f39e97de118b32d10f0eff7b`
- `v12_option_snapshots.jsonl`: `4f975f73fef6c3ca4915b2d2bc29dbcb001ad3e10462ba0bf60421df95f44057`
- `v12_option_state.json`: `8f0c31aeb02393b9ad3a28094607bd0df3a502fc436b60b70b490e6692171dac`
- `v123_focus_state.json`: `e38c62a9359e23bb8c4ed78ff5a891000bcf97af2237c5b114da2bfd763ba388`
- `v123_market_checkpoint.json`: `ef388bddb9ea9b0ae06c810d8f8e53ccc2de036c665ae0b0f1252fa311f7c28d`

These hashes define the in-sample evidence. Future thresholds must not be selected by re-optimizing on this package.

## Deterministic measurement repairs
### Continuation Math shadow recorder
1. Lock shadow episode direction at trigger; retain the current candidate direction separately.
2. Freeze entry ATR and entry 3-minute ATR for cumulative MFE/MAE/progress normalization.
3. Preserve current ATR/ATR3 as separate dynamic context.
4. Do not back-fill multiple missed milestone horizons with one current observation.
5. Mark late observations explicitly and record their lag.
6. Add deterministic observation IDs and suppress duplicate writes.
7. Keep these repairs shadow-only; do not change the live trade-state machine.

### V12 Option Recorder
1. Serialize fixed-slot collection and use an atomic slot-claim file to prevent duplicate slot writes.
2. Separate quote freshness from last-trade inactivity.
3. Count quote staleness separately from inactive last trade.
4. Break coarse liquidity-score ties using actual executable ATM straddle spread before symbol-name tie breaking.
5. Keep the fixed capture schedule unchanged: 09:30, 13:00, 15:10, 15:37.

## Forward shadow hypotheses
These hypotheses are locked for forward observation. They are not trade filters.

### H1 — movement prior
Opening ATM IV / term-structure level may enrich for stocks with larger later absolute underlying movement.
It does not predict direction and must not independently trigger an option trade.

### H2 — campaign vs execution
Underlying campaign state and immediate execution state are distinct.
A tactical pause, route degradation, fast veto, or depth conflict does not by itself invalidate the broader thesis.

### H3 — continuation-quality profile
The exploratory 25 Sep shape:
- progress >= +0.20 entry ATR
- path efficiency >= +0.03
- pullback ratio <= 0.65

is recorded as an in-sample hypothesis only. It must not be tuned or deployed from the 25 Sep evidence. Forward sessions will report its precision/recall for later +0.50 and +0.80 entry-ATR excursion.

### H4 — depth conflict
Futures-depth opposition is contextual confirmation, not a standalone campaign-kill condition.
Forward analysis will compare later excursion conditional on price-path quality, witness, route health, and depth opposition.

### H5 — executable route
Option route quality is independent of the underlying thesis.
Front and next expiry are compared on actual spread, delta, DTE, quote freshness and friction. No blanket “DTE <= 5 means next month” rule is allowed.

### H6 — first-hit semantics
`MINUS_0_15_ATR_FIRST` means only that -0.15 entry ATR occurred before +0.30 entry ATR.
It is not a final campaign-failure label. Forward records must retain first hit, later MFE/MAE and episode close result.

## Locked forward evaluation
For each new clean episode, record:
- episode direction, entry price, entry ATR, entry ATR3
- progress, MFE, MAE, path efficiency, pullback ratio
- first-hit state and time
- 5m witness, route health, fast veto, depth support/opposition
- option contract, DTE, bid/ask spread, delta and quote age
- later +0.30, +0.50 and +0.80 entry-ATR attainment
- episode close reason and final retained excursion

Primary forward questions:
1. Does the 3m/6m continuation-quality profile separate later >=0.50 / >=0.80 ATR movers from churn out of sample?
2. Does depth opposition add incremental information after price-path and witness variables?
3. Does the option volatility prior improve mover recall without becoming a direction signal?
4. Does route-quality separation improve executable option outcomes while preserving valid underlying campaigns?
5. How often does an initially negative first hit later recover to >=0.50 / >=0.80 ATR?

No threshold changes are permitted from a single additional session. Results should be accumulated across multiple independent trading days before any proposal to production.
