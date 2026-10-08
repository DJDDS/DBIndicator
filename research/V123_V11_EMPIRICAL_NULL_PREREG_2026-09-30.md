# V1.1 Research Follow-up — Empirical Calibration & Continuation Test

**Date:** 30 Sep 2026  
**Status:** RESEARCH ONLY / NO PRODUCTION CONTROL / DO NOT DEPLOY  
**Parent production formula:** DBI-AQ-PIPELINE-V1.0  
**Motivation:** independent auditor replay of 210 F&O stocks over 141 trading days found V1.0-1m over-fired (6.8% vs nominal 1%) and ACTIONABLE continuation was ~+2 bps at 15m, statistically indistinguishable from zero.

## Locked objective

Test whether any causal, price-first statistical state has enough forward continuation to justify an option-buying execution layer after costs.

No technical indicators, named chart patterns, hand-picked “trading” timeframes or post-hoc threshold selection are allowed.

## Data contract

The runner expects a normalized one-minute table with one row per stock-minute and columns:

- `timestamp`
- `trade_date`
- `symbol`
- `close`
- `nifty_close`
- `sector_close`
- `sector_id`

Input may be CSV or Parquet. Every feature at time t must use information available at or before t.

## OOS chronology

- Sort sessions chronologically.
- First 40 complete sessions: calibration only.
- Every later session D is evaluated OOS using completed sessions strictly earlier than D.
- Calibration uses an expanding prior-session window capped at 60 sessions.
- Nothing from D may change D’s thresholds or model parameters.

## Experiment A — empirical max-|Z| calibration

For each stock-minute and each candidate horizon H = {1,2,3,5,10,15} minutes:

1. calculate stock log returns;
2. calculate NIFTY and sector log returns;
3. fit prior-window no-intercept ridge betas;
4. residualize stock return;
5. robustly scale residuals by MAD;
6. compute horizon Z;
7. define M_t = max_h |Z_h|.

For evaluation day D set:

    q_D = empirical 99th percentile of M_t from completed calibration sessions only.

Primary questions:

- Does the OOS trigger rate move near 1%?
- Does q_D materially differ from fixed |Z|=3.188815?
- What is the distribution of q_D through time?

No use of the auditor’s post-hoc 5.66 threshold as a fixed production rule.

## Experiment B — detector ablation

Run the same OOS chronology for:

1. raw stock returns / fixed horizon;
2. market residual only;
3. market + sector residual;
4. multiscale max-|Z|;
5. each fixed horizon separately;
6. empirical threshold with CUSUM off;
7. empirical threshold with CUSUM on.

CUSUM is causal and must use only information available by t.

Primary outcomes at +5m, +15m, +30m:

    signed_forward_return = direction * (P_future / P_t - 1)

Report mean, median, day-cluster bootstrap 95% CI, hit rate, MFE/MAE and matched same-stock/day random-minute placebo.

## Experiment C — conditional continuation model

At each OOS detection time save only causal features:

- Z at every horizon;
- selected h*;
- selected Z and empirical tail percentile;
- stock CUSUM up/down;
- market/sector residual CUSUM;
- robust volatility;
- residual coherence;
- time-of-day;
- recent volatility ratio.

Outcome labels are defined later and may not feed the same-day model.

Primary binary labels:

    Y_5  = signed return at +5m  > 0
    Y_15 = signed return at +15m > 0
    Y_30 = signed return at +30m > 0

Economic labels:

    Y_cost(c,h) = 1 if signed return at horizon h exceeds c bps

where c is evaluated as a sensitivity grid {5, 10, 15} bps, not optimized.

Fit L2-regularized logistic regression on completed prior sessions only and score D.
Report AUC, Brier score, reliability/calibration, and net expectancy by predicted-probability decile.

## Experiment D — economic hurdle

A detector is not considered useful merely because mean forward return > 0.

For each OOS event and horizon h evaluate:

    edge_bps = 10,000 * signed_forward_return

and report:

- P(edge > 5 bps)
- P(edge > 10 bps)
- P(edge > 15 bps)
- mean excess return after subtracting each hurdle.

No option P&L is claimed without historical bid/ask option data.

## Multiple-testing discipline

- All variants above are preregistered before seeing their OOS results.
- No new threshold may be chosen from the 141-day final test window.
- If a new threshold/model is proposed after results, it becomes V1.2 and requires a new untouched OOS window.
- Confidence intervals are resampled by whole trading day, not individual minute.

## Decision rule

V1.1 is considered promising only if, on OOS sessions:

1. empirical calibration keeps trigger rate close to its declared rate;
2. at least one causal variant beats its matched placebo with a day-bootstrap CI excluding zero at the primary horizon;
3. the edge is not concentrated in one early subperiod;
4. the measured underlying edge is large enough to plausibly clear the 5/10/15 bps hurdle sensitivity;
5. results survive fixed-horizon and CUSUM ablations.

Otherwise the correct conclusion is that the price-only continuation edge is not demonstrated.

## Production isolation

This research branch must not be merged or deployed without a separate explicit owner approval.
