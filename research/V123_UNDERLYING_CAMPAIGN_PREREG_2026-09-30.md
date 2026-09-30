# Underlying Campaign Research — preregistration

**Date:** 30 Sep 2026  
**Status:** RESEARCH ONLY / DO NOT DEPLOY / PRODUCTION UNCHANGED  
**Branch:** research/underlying-campaign-20260930

## Objective

Test the user's actual trading objective:

> detect an F&O stock early, keep the underlying thesis alive through a normal
> pullback/retrace, enter/re-enter when directional momentum resumes, and use
> options only as the execution vehicle.

This is not an option-first prediction study.

## Data

1. Dhan 5-minute NSE cash history, 1 Oct 2021–25 Sep 2026, for the frozen
   pre-30-Sep-2026 210-stock F&O universe where history exists.
2. Dhan daily cash history, 1 Oct 2016–25 Sep 2026, for long-regime context.
3. NIFTY 50 matching history.
4. Dhan expired rolling stock-option minute history for the final OOS period,
   used only after underlying event selection.
5. Frozen V12 executable-option recorder reference:
   10 trading days / 39 captured slots, median ATM-straddle spread 1.8059%,
   P75 2.5532%, 195 symbols below the 4% practical spread gate,
   term-structure coverage 95.1%. This is used only as a friction sensitivity
   reference; it never creates the underlying event.

## Chronology

- Development: through 30 Sep 2024.
- Validation: 1 Oct 2024–30 Sep 2025.
- Final OOS: 1 Oct 2025–25 Sep 2026.

No OOS value may tune a signal threshold.

## Underlying-only event definition

### Abnormal impulse

For each stock 5-minute bar:

1. compute stock and NIFTY log returns;
2. fit a no-intercept market beta from the prior 60 completed bars;
3. residual = stock return - beta * NIFTY return;
4. estimate causal robust residual sigma from the prior 60 bars;
5. sum residuals over three bars (15 minutes);
6. standardise by sigma * sqrt(3).

The impulse threshold is the pooled development-only q99 of absolute 3-bar
standardised residual movement. An event begins only on a threshold crossing.

### Pullback

After an impulse, at least one adverse close-to-close bar must occur.
The original impulse is invalidated if the adverse excursion retraces 100% or
more of the impulse price movement.

### Reclaim

Within eight 5-minute bars after the impulse, after an adverse bar has occurred:

- bullish: close exceeds the prior two closes;
- bearish: close is below the prior two closes;
- the current market-adjusted residual return must again have the original sign.

The reclaim trigger is causal: it does not use the future pullback minimum or
future return.

## Primary comparison

Compare forward underlying movement from:

1. immediate abnormal-impulse entry;
2. causal pullback -> reclaim entry;
3. reclaim where pullback median volume is <= impulse median volume;
4. reclaim where pullback volume contracts and reclaim volume re-expands.

Report +5m, +10m, +15m, +30m signed return, hit rate, whole-day bootstrap
confidence interval, and symmetric 10/20/30-bps target-first rate.

## Pullback depth

Predeclared bins:

- 0–20%
- 20–35%
- 35–50%
- 50–65%
- 65–80%
- 80–100%

All bins are reported in validation and OOS. If a single depth bin is selected,
selection occurs on validation only and is frozen before its OOS result is read.

## Ten-year daily context

Attach prior-completed-day context only:

- 20-day realised volatility relative to its prior 252-day median;
- 5-day stock-minus-NIFTY relative direction.

These are diagnostics first. They do not create the event in this run.

## Historical option overlay

For final-OOS underlying events only:

- direction maps Bullish -> CALL, Bearish -> PUT;
- request Dhan rolling stock-option ATM minute history for near monthly expiry;
- underlying event time is the close of its 5-minute bar, so option entry is not
  allowed before timestamp + 5 minutes;
- option exit is evaluated at +5/+10/+15/+30 minutes;
- entry and exit must report the same physical strike; otherwise the hold is
  discarded because a rolling ATM series can change strike;
- raw premium-return results are reported;
- bid/ask sensitivity is then applied using the frozen recorder's 1.8059% median,
  2.5532% P75 and 4% practical-liquidity gate.

Dhan rolling history does not provide historical bid/ask, so spread-adjusted
figures remain sensitivity estimates, not exact executable P&L.

## Decision logic

The reclaim hypothesis earns further consideration only if final OOS:

1. reclaim +15m mean is positive with a whole-day bootstrap CI excluding zero;
2. reclaim materially improves over immediate-impulse entry;
3. the result is not confined to one narrow pullback-depth bin;
4. historical option premium response remains positive after the recorder median
   spread sensitivity and is not destroyed at the recorder P75 spread;
5. event count is large enough that the result is not a handful of anecdotes.

No production logic changes from this run alone.
