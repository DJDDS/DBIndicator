# V9.7.2 Trial 19 Confound & Integrity Closure Design

## Goal
Settle the remaining alternative explanations for the replicated extreme-total-OI magnitude effect without changing Trial 19's frozen signal, evidence window, matched baseline, thresholds, or prior trial outcomes.

## Frozen Trial 19
- Evidence: 2018-09-01 through 2021-08-31.
- Event: `total FUTSTK OI z >= 1.5`.
- Primary endpoint: next-session `movement_1d_atr`.
- Frozen primary baseline: eligible non-event F&O stocks on the same trading day and same DTE bucket.
- Frozen primary inference: binary `extreme_oi_event`, two-way clustered by trading date and symbol.
- Frozen efficacy bars: >=250 events, >=250 event days, matched lift >=1.10x, 95% CI lower bound >1.00x, event t-stat >=3.0, top-3-day robustness, >=3/4 positive chronological blocks.
- Trial 18 remains locked. No TRADE/WATCH activation. `ACTIVE_PLAYBOOKS = ()`.
- Prior locked finals remain untouched.

## New controls

### 1. Monthly MWPL + daily ban integrity
Use official monthly `mpl_monyyyy` limits as the denominator and Trial-19's already-normalized total FUTSTK OI as the daily numerator. Reconstruct daily utilisation. Use the 95/80 state machine and targeted daily `fo_secban` cross-checks only on risk dates. Do not use the previous 729-day combined-OI probe loop.

Historical MWPL remains a declared Trial-19 integrity control. If historical coverage is insufficient, do not silently remove it. Instead run the recent-window empirical bound below and disclose both results.

### 2. Recent-window MWPL/ban bound
On the already-used 2021-09-01 through 2023-09-01 independent window, measure:
- fraction of extreme-OI events occurring in banned / >=95% MWPL observations;
- same-day+DTE matched 1D lift with all events;
- same metric after ban/>=95% exclusion;
- absolute lift delta.

This is a bounding diagnostic only. It cannot alter Trial 19's frozen efficacy result and cannot by itself unlock Trial 18. Historical MWPL is considered empirically non-load-bearing only if recent event overlap <=5% and absolute matched-lift delta <=0.02x; otherwise historical MWPL must be fully applied for promotion eligibility.

### 3. Prior-volatility confound matching
Compute `realized_vol5_prev` from cash close-to-close log returns using a 5-session rolling standard deviation annualized by sqrt(252), shifted one session so the event day's close does not enter the covariate.

Within each trading date, assign eligible observations to cross-sectional realized-volatility quintiles using percentile ranks. The confound baseline matches on:
- trading date;
- DTE bucket (0-5, 6-10, 11-20, 21+);
- prior 5-day realized-volatility quintile.

The event remains `total_z >= 1.5`; no threshold changes. The confound control passes if 1D matched lift >=1.10x and its 95% CI lower bound >1.00x.

### 4. Pre-signal persistence diagnostic
Report matched event/control movement for the two complete sessions before the signal date (`t-1` and `t-2`). These are diagnostics, not extra post-hoc gates. Flag a persistence warning when either prior-session matched lift is >=1.10x with a 95% CI lower bound >1.00x.

### 5. Earnings confound
Run the existing NSE historical financial-result calendar regardless of MWPL completion, provided Trial 19's frozen efficacy gates (excluding integrity) remain satisfied. Exclude +/-5 observed trading sessions around each covered earnings/result date. Report coverage, event count removed, matched lift and 95% CI.

The earnings control passes only when symbol coverage >=90%, earnings-excluded 1D matched lift >=1.10x and CI lower bound >1.00x. If official coverage is below 90%, report `INCONCLUSIVE_EARNINGS_COVERAGE`; do not fabricate dates.

## Promotion eligibility
Trial 18 remains locked unless all are true:
1. Frozen Trial-19 efficacy gates remain satisfied.
2. Historical membership, historical cash and OI normalization are APPLIED.
3. Prior-volatility confound control passes.
4. Earnings control passes.
5. MWPL is either fully APPLIED under the declared historical rule, or the recent-window bound meets the pre-registered non-load-bearing limits (overlap <=5%, lift delta <=0.02x) while historical unavailability remains disclosed.

Passing means only `ELIGIBLE_FOR_PREREGISTRATION`, never auto-run and never production activation.

## Reporting
- Preserve the original Trial-19 headline result separately.
- Display confound controls below it; do not overwrite the frozen result.
- Carry forward ~1.13x as the replicated planning effect; label the original 1.22x discovery estimate retired for economic projection.
- Show MWPL monthly coverage, date/observation coverage, recent ban overlap/lift delta, prior-vol matched lift/CI, t-1/t-2 diagnostics, earnings coverage/excluded lift/CI, and a final Trial-18 eligibility decision.

## Non-goals
No direction research, no participant-wise OI feature, no expected/unexpected-OI decomposition, no DTE exclusion, no threshold optimization, no option-strategy activation, and no opening of any prior locked final.
