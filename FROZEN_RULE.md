# V7 Frozen Production Candidate

**Build:** `2026-08-29-INSTITUTIONAL-V7-FROZEN`  
**Rule ID:** `RR_LONG_CATALYST60_15M_NEXTBAR_1D`

This build does not search for a better combination. It spends the previously locked final 20% on one pre-declared rule only.

## Frozen trade rule

- Universe: full live NSE stock-F&O universe returned by Kite.
- Setup timeframe: 15 minute.
- Direction: Bullish only.
- Structural trigger: actual Recent-Range escape.
- Participation trigger: Catalyst Score **>= 60**.
- Catalyst Score inputs are unchanged from V6: gap/ATR, opening RVOL, time-of-day RVOL, bar-range/ATR shock, and cross-sectional turnover percentile.
- Entry: next executable 15-minute bar after the confirmed escape.
- Evaluation horizon: 1 trading day.
- Research window: exactly 180 calendar days.
- Costs: 0.08% round-trip cost assumption plus 0.05% slippage per side, unchanged from the validated V6 research.
- Split: 60% development / 20% validation / 20% final.

OI, futures basis, VWAP, 4H context, sector leadership, price location, retention/retest and high turnover are **not eligibility gates** for this frozen final test. They remain diagnostics/context only.

## One-shot acceptance rule

The final 20% receives exactly one verdict.

**PASS** requires all of the following:

- Final sample N >= 80.
- Final average net return >= +0.15%.
- Final profit factor >= 1.20.
- At least 3 of 4 chronological final-sample blocks have positive average net return.

If any check fails, the verdict is **REJECT**. The threshold is not moved afterward to rescue the result.

## Anti-fishing safeguards

The final sample is only revealed when the run matches the frozen protocol: full F&O universe, 15-minute setup/execution, 180 days, and fixed cost/slippage assumptions. All legacy V6 final-test surfaces stay locked, even if the old `V6_UNLOCK_FINAL_TEST` environment variable is set.

The Backtest page includes a dedicated **Run Frozen V7 Final Test** button that automatically launches the correct protocol. The normal diagnostic research button remains available, but non-protocol runs cannot reveal the V7 final sample.
