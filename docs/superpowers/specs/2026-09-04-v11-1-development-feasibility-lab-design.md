# V11.1 Development & Feasibility Lab — Design

## Purpose

V11.1 is not Trial 25. It is a development-only research laboratory whose sole purpose is to determine whether either of two tightly defined, practically executable NSE F&O momentum candidates is strong enough, stable enough, and sufficiently powered to justify spending an untouched validation window.

The build must not rescue Trial 24, reinterpret its preregistered verdict, read its final 31 months, or activate production signals.

## Research objective

Find one candidate, if any, that can realistically clear an independent validation at at least 80% joint power after measured implementation costs.

The two candidate families are fixed before development analysis:

1. **Residual momentum + volatility de-risking**
   - Cross-sectional residual momentum formed 12-1 months.
   - Residualisation uses India FF3 factors with a 36-month regression, matching Trial 24.
   - Long top decile, short bottom decile, monthly rebalance, equal-weight within each side.
   - Primary risk-management rule: de-risk only when lagged forecast portfolio volatility exceeds a fixed external target; no leverage above 1.0x.

2. **Conventional price momentum + identical volatility de-risking**
   - Cross-sectional 12-1 total-return momentum.
   - Same point-in-time FUTSTK membership, monthly rebalance, deciles, equal weights, and risk-management rule.

No third alpha family, no lookback sweep, no decile sweep, no RSI/EMA/OI/volume rescue, and no post-result threshold search are allowed in V11.1.

## Data classification

### Development data

The already-read Trial-24 pre-final period, 2010-01 through 2023-05, is reclassified for future work as **development only**. It may be used to estimate nuisance parameters, compare the two fixed candidate families, measure turnover, measure realized volatility, test scaling mechanics, and estimate a development effect for pre-trial power calculations.

It can never again be called independent validation for a new momentum variant.

### Locked data

The 31 Trial-24 final months remain unread. V11.1 must physically prevent candidate-return reads from this block.

No V11.1 result may consume or infer final-period portfolio P&L, Sharpe, t-statistic, drawdown, or candidate ranking.

## Trial 24 record

Trial 24 remains `FAIL_REPLICATION_PRE_FINAL` under its preregistered gate.

V11.1 adds a non-rescuing research note using values already read:

- observed net mean: 0.188%/month under the registered stress cost;
- implied gross mean: 0.548%/month;
- realized annualized volatility: approximately 24.3%;
- canonical external residual-momentum annual volatility: 12.49%;
- the external volatility input is recorded as a source-market prior that did not transport to the Indian FUTSTK implementation;
- top-3 removal and block signs are described as diagnostics, not independent evidence of three separate failures.

This restatement does not alter Trial 24's verdict, gate, or holdout status.

## Feasibility gate V2

V11.1 replaces the current `effect >= MDE` registration condition with an explicit minimum-power contract.

### Required inputs

Every proposed confirmatory trial must declare:

- expected gross effect;
- effect provenance: external prior or development estimate;
- expected implementation cost;
- cost provenance;
- target-market volatility or standard error;
- volatility provenance;
- independent validation sample size;
- one-sided critical threshold;
- minimum required power, default 0.80;
- any additional hard gates that affect pass/fail.

### Power decision

For a simple one-sided mean test, compute normal-approximation power from the declared effect, standard error, sample size, and critical threshold.

Registration is refused when `power < 0.80`, even when expected effect exceeds the old MDE.

### Joint battery power

If the confirmatory battery contains more than the primary statistical gate, V11.1 must estimate the probability of passing the **whole battery jointly**, preserving dependence among diagnostics. It must not multiply marginal powers as though gates were independent.

The production decision field is:

- `GO_REGISTER_PREREGISTERED_TRIAL` only when all integrity/economic requirements pass and joint power is at least 0.80;
- `DO_NOT_RUN_UNDERPOWERED` otherwise.

## Development volatility estimation

External published volatility may be shown for context but cannot override target-market development volatility when the latter is available.

For each candidate, V11.1 measures volatility directly from development-period gross and measured-cost net portfolio returns.

The primary volatility-management input is **portfolio-level lagged realized volatility**, not the stock-level next-session HAR forecast from Trial 20.

Rationale: Trial 20 forecasts individual next-session variances; the monthly long-short portfolio requires portfolio risk including covariance. A simple point-in-time portfolio-risk estimator is more defensible as the first implementation.

## Volatility-management rule

The primary scaler is de-risk only:

`exposure_t = min(1.0, target_vol / forecast_vol_t)`

where `forecast_vol_t` uses only information available before the rebalance.

No leverage above 1.0x is allowed in the primary candidate.

A leverage-cap variant may be displayed only as a development diagnostic if implemented later; it cannot select or rescue the primary candidate in V11.1.

The volatility target must be externally justified and frozen before comparing candidate outcomes. For residual momentum, the canonical 12.49% annual volatility may serve as the external reference; the implementation rounds only if documented and does not search across targets.

## Portfolio turnover and costs

V11.1 must stop treating a fixed stress charge as the main economic estimate.

For every rebalance, store prior and new portfolio weights and calculate traded notional under one explicit convention.

Report:

1. gross return;
2. measured-turnover net return using the build's frozen per-turnover execution cost;
3. stress-cost net return as a robustness scenario.

Stress cost remains visible but cannot replace measured turnover in the headline development estimate.

## Tradable FUTSTK economics

Signal construction may continue to use adjusted cash returns and India factors.

A separate execution layer must evaluate whether the strategy is implementable in stock futures using official NSE historical contracts.

Before any futures outcome is used for candidate ranking, freeze and record:

- contract selection rule;
- roll rule;
- expiry handling;
- lot-size source;
- missing-contract policy;
- price field used for entry/exit;
- cost/slippage convention.

If historical contract coverage is insufficient for a clean execution replay, V11.1 reports `FUTSTK_EXECUTION_COVERAGE_INSUFFICIENT` rather than fabricating prices or silently falling back to cash P&L as if it were futures P&L.

## Candidate comparison

Both candidates use the same development window, membership history, cost method, risk-management rule, and diagnostics.

The candidate comparison reports, at minimum:

- gross CAGR/annualized mean;
- measured-cost net annualized mean;
- annualized volatility;
- Sharpe;
- downside deviation / Sortino diagnostic;
- maximum drawdown;
- worst month;
- skewness;
- kurtosis;
- 5% CVaR;
- average and median monthly turnover;
- months with complete portfolio;
- chronological subperiod diagnostics;
- estimated confirmatory joint power for a declared untouched validation window.

The development winner is selected by a frozen hierarchy, not a weighted score search:

1. data integrity and implementation coverage must pass;
2. measured-cost expected return must be positive;
3. estimated joint power must be at least 0.80;
4. if both pass, prefer the higher measured-cost Sharpe;
5. if Sharpe is practically tied, prefer the lower turnover candidate;
6. if neither passes the power gate, select no winner.

A development winner is only **eligible for Trial 25 preregistration**. It cannot activate TRADE/WATCH.

## Independent validation discovery

V11.1 may investigate data **coverage only** for possible older or later independent windows.

Coverage investigation may inspect file existence, membership availability, contract availability, factor availability, lot-size availability, and date completeness.

It must not compute candidate returns in a proposed independent window until a candidate is frozen and its Trial-25 specification has been preregistered.

## Hard gates vs diagnostics

### Hard gates

- required data integrity;
- point-in-time construction;
- no holdout read;
- positive measured-cost expected economics;
- minimum 80% joint power;
- sufficient execution coverage for any claim of FUTSTK tradability.

### Diagnostics

- top-3-month removal;
- four-block signs;
- max single-month contribution;
- skewness;
- kurtosis;
- drawdown;
- CVaR;
- stress-cost scenario.

Diagnostics can warn, but they do not create multiple independent statistical failures unless separately powered and preregistered as hard gates.

## UI / reporting

The Backtest page gains a V11.1 Development & Feasibility Lab section above the legacy research panels.

It must clearly display:

- `DEVELOPMENT ONLY — NO TRIAL 25 YET`;
- exact development date range;
- `FINAL 31 MONTHS UNREAD`;
- candidate A and candidate B side-by-side development metrics;
- power-gate inputs and provenance;
- measured turnover and measured-cost net results;
- futures execution coverage status;
- `ELIGIBLE FOR TRIAL 25`, `DO_NOT_RUN_UNDERPOWERED`, or `NO DEVELOPMENT WINNER`;
- `production_activation = NO` in all cases.

## Non-goals

V11.1 does not:

- run Trial 25;
- read the final 31 months;
- optimize momentum lookbacks, factor models, decile counts, volatility targets, leverage caps, or cost assumptions after seeing outcomes;
- activate a live strategy;
- use options P&L as a historical backtest without point-in-time option-chain data;
- claim a cash-return backtest is a futures execution backtest.

## Testing and release requirements

The build must add regression tests proving:

- old `effect >= MDE` logic cannot authorize a trial below 80% power;
- external volatility is superseded by development volatility when available;
- joint battery power is calculated jointly rather than by multiplying marginal powers;
- final 31 months cannot be loaded by V11.1 candidate evaluation;
- both candidate score calculations are point-in-time;
- the primary scaler never exceeds 1.0x and uses only lagged information;
- turnover is computed from portfolio weight changes;
- measured-cost and stress-cost returns are separate fields;
- FUTSTK execution coverage fails closed when required contracts are absent;
- no development result can set production activation true.

The release must pass the full existing regression suite, Python compilation, rendered Backtest JavaScript syntax, clean-package checks, and ZIP integrity before being labelled final.
