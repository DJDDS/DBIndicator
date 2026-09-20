# Index Option Buying Research V1 — Stage 1 Preregistration

Status: **RESEARCH / SHADOW ONLY**  
Index: **NIFTY 50 first**  
Production/V12 recorder changes: **NONE**

## Objective

Determine whether intraday opening-range duration and minute-level confirmation timing contain
a robust, repeatable directional follow-through effect in NIFTY before introducing option-chain,
volatility, strike, DTE, or exit optimization.

This stage intentionally studies the underlying index. It must not emit a live CE/PE recommendation.

## Locked timing grid

Opening-range duration:

- 15 minutes
- 30 minutes
- 45 minutes
- 60 minutes
- 90 minutes
- 120 minutes

Break confirmation clock:

- completed 1-minute close
- completed 3-minute close
- completed 5-minute close

This is an 18-cell experiment. Trigger bars are aligned from the end of each opening range rather
than from an arbitrary wall-clock multiple.

## Signal definition

1. Use one-minute NIFTY OHLC bars timestamped at bar start.
2. Opening session begins at 09:15 IST.
3. The range high/low are computed only from completed one-minute bars inside the selected OR window.
4. Baseline breakout buffer is 0.04% of the 09:15 session open.
5. Bullish signal: first completed trigger bar whose close is at/above OR high + buffer.
6. Bearish signal: first completed trigger bar whose close is at/below OR low - buffer.
7. Last permissible signal close is 13:00 IST.
8. At most one first-break signal per session per timing cell.
9. No re-entry is studied in Stage 1.

## Range-width treatment

Primary timing-discovery analysis: **no range-width filter**. This isolates timing from regime filtering.

Predeclared secondary control: apply the strategy document's 0.15%-0.60% opening-range-width gate.
This is a sensitivity/control view, not permission to optimize the bounds.

The PDF's gap and India-VIX rules are not part of Stage 1 because this experiment is deliberately
isolating timing. They will return as separately tested research features later.

## Forward measurements

Every signal records post-signal directional return, MFE and MAE in opening-range units (R) at:

- +5m
- +10m
- +15m
- +30m
- +60m
- +90m
- +120m
- EOD

Also record time-to-+0.5R and time-to-+1.0R.

Forward measurement starts with the first one-minute bar AFTER the completed trigger bar. The trigger
bar itself is never reused as future path evidence.

## Anti-lookahead rules

- Range bars exclude the trigger bar.
- Trigger decisions use bar close only after that trigger bar is complete.
- Missing one-minute bars invalidate that OR/trigger block rather than being forward-filled.
- Future bars may affect MFE/MAE/outcomes but may never alter signal time, range, direction or trigger.
- The live V12/V12.1 ten-session dataset is excluded from model discovery and tuning.

## Locked historical chronology

The preferred historical request is **2019-01-01 through 2026-08-31**. If the connected historical
provider cannot supply the complete period, the engine must report the actual first/last timestamp
and the split coverage; it must not silently substitute another date range.

The research partitions are locked before observing Stage-1 results:

- **Development:** 2019-01-01 through 2023-12-31.
- **Validation:** 2024-01-01 through 2025-12-31.
- **Historical holdout:** 2026-01-01 through 2026-08-31.
- **Forward evidence:** the ongoing September 2026 V12/V12.1 recording period remains untouched and is
  not used to choose timing, thresholds or filters.

The 2026 historical holdout is not used to choose a timing family. It is opened only after a timing
family or stable neighbourhood has been selected from development and checked in validation.

## Research sequence after Stage 1

Stage 2: acceptance/retest confirmation (immediate close vs double-close vs acceptance vs retest).  
Stage 3: futures lead, futures RVOL, VWAP, basis and market-state context.  
Stage 4: volatility regime and option-chain context as confirmation, not prediction by assumption.  
Stage 5: DTE and contract selection (ATM vs modest ITM vs premium-based selection).  
Stage 6: exit architecture using actual option bid/ask where available.  
Stage 7: untouched OOS plus forward shadow operation.

No later-stage feature may be back-fitted into Stage 1 results.

## Validation discipline

Parameter families are discovered only in the development segment. Validation is used to reject
unstable families, not to retune them indefinitely. The historical holdout is not used to choose OR
duration, trigger clock, filters or thresholds.

The Stage-1 summary is descriptive by design and does not auto-rank a "winner". Advancement requires
robustness across adjacent timing cells and time segments, not the single highest historical mean.

## Relationship to the supplied strategy

The supplied ORB strategy remains **Control V0**. Its 09:15-09:45 range, 1-minute close trigger,
0.04% buffer and 0.15%-0.60% range-width gate are preserved as a named control. Stage 1 expands only
the timing dimension so that 30m/1m does not receive an unearned privileged status.

## Production isolation

This branch must not:

- modify V12/V12.1 recorder sampling,
- alter current production routes or live signal rules,
- use the ongoing ten-session recorder dataset for tuning,
- place orders,
- emit live BUY CE / BUY PE instructions,
- deploy to Railway production.

