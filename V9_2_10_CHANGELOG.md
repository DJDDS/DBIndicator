# V9.2.10 — Bull Population Integrity

This release fixes the V9.2.9 contradiction where the Bull Gate Funnel could report hundreds of qualified Institutional Accumulation events while the Bull development/validation table reported zero.

## Changes

- The cumulative Bull Gate Funnel is now the single source of truth for the historical Bull research population.
- The funnel returns stable qualified event keys and the exact survivor rows feed the 60/20/20 development/validation/final split.
- A hard population-integrity invariant raises `DATA/LOGIC ERROR` if funnel-qualified count/identity ever diverges from the Bull backtest population.
- Historical Data Coverage is rendered directly in the V9.2 goal-focused result area even during the fast V9.2 run.
- No Bull/Bear threshold, ATR chase guard, cost/slippage assumption, or locked-final policy is changed.
