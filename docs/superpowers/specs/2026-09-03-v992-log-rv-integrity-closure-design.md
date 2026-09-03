# V9.9.2 Trial 20 Log-RV Integrity Closure — Design

## Purpose
Close the only remaining technical objection to V9.9 Trial 20. The original OOS result failed, but its QLIKE magnitude, raw-variance coefficient scale, and Yang–Zhang/Garman–Klass loss gap indicate a levels-fitted variance forecast can approach the positivity floor. V9.9.2 reruns the exact same Trial-20 hypothesis once with HAR estimated in log-realized-variance space.

## Frozen research question
Does today's already-preregistered abnormal total FUTSTK notional-turnover residual add incremental information about next-session realized variance beyond HAR daily/weekly/monthly state on the untouched 2015-09-01 through 2018-08-31 Trial-20 outcome window?

## Model repair
For each target (Yang–Zhang primary and Garman–Klass robustness), fit the benchmark and challenger in log variance space using only training observations available before each OOS forecast date.

Benchmark:
`log(RV[t+1]) ~ 1 + log(HAR_daily[t]) + log(HAR_weekly[t]) + log(HAR_monthly[t])`

Challenger:
`log(RV[t+1]) ~ 1 + log(HAR_daily[t]) + log(HAR_weekly[t]) + log(HAR_monthly[t]) + abnormal_futstk_volume[t]`

All variance inputs use a small positive floor only to make the log defined. The abnormal-volume feature, OOS window, warm-up data, refit cadence, earnings split, DTE handling, chronological blocks, top-day sensitivity, and Clark–West hurdle remain unchanged.

## Back-transform and loss evaluation
Convert each log forecast back to variance units using a training-only lognormal smearing factor `mean(exp(residual))` estimated from the same training sample used for the forecast fit. This avoids look-ahead and guarantees strictly positive variance forecasts. Evaluate MSE, QLIKE, OOS R2 and Clark–West in original variance units exactly as before.

## Integrity diagnostics
Report forecast minima/maxima and counts hitting the numerical floor. A clean log-RV run should have no floor-clipped forecasts except pathological input cases. Preserve the same-day/same-DTE clustered diagnostic as a descriptive association test; it is not the promotion gate.

## Closure rule
This is an integrity closure rerun, not a new trial and not a rescue search. If HAR+Volume still fails the frozen OOS gates, mark Trial 20 `CLOSED_REJECTED_LOG_RV_CONFIRMED`. If the corrected specification passes, mark it `SPECIFICATION_SENSITIVE_NOT_PROMOTED`; do not activate a signal or unlock Trial 18 because the outcome window has already been observed and the repair was selected after seeing a numerical pathology.

## State locks
Trial 18 remains permanently locked. Trial 19 remains closed as association-not-incremental. `ACTIVE_PLAYBOOKS = ()` remains unchanged. OI remains diagnostic only. Live Opportunity Radar logic remains unchanged.

## UI correction
Chronological stability must display the actual number of blocks evaluated. The current `required=3` means the pass hurdle is three positive blocks out of four, not that only three blocks exist. The UI must render `positive/4 (need >=3)`.
