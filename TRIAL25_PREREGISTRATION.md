# Trial 25 — Earnings Volatility-Repricing Study
## Forward, options-native, defined-risk preregistration

**Registration date:** 2026-09-21  
**Status:** PREREGISTERED DESIGN / NO EFFICACY OUTCOMES READ  
**Production activation:** NO  
**Prior Trial-24 final 31 months:** remain unread and irrelevant to this trial.

## 1. Why this is Trial 25

Trial 25 is **not another daily directional stock screener trial**.  It is the
earnings volatility-repricing candidate selected before any Trial-25 outcome is
observed.  The economic prior is that option-implied earnings moves tend to
exceed subsequent realised moves and that the repricing arrives around the
announcement in a one-to-two-session window.

The first ten distinct live Indian stock-option recording days are a
**feasibility sample only**.  Their only job is to decide whether executable
bid/ask data are sufficiently liquid to make this study legitimate.

## 2. Immutable feasibility dependency

Trial 25 may accrue events only after the startup freeze creates and validates:

- `research_freezes/v12_option_state_10d_2026-09-21.json`
- `research_freezes/v12_feasibility_code_10d_2026-09-21.py`
- `research_freezes/v12_feasibility_10d_2026-09-21.json`
- `research_freezes/v12_feasibility_10d_2026-09-21.sha256`

The frozen feasibility result must be
`STOCK OPTIONS PRACTICALLY TESTABLE`, with at least 20 symbols satisfying the
already-existing V12 gate:

- two-sided ATM coverage >= 70%;
- median executable ATM straddle spread <= 4%;
- at least 10 distinct recorded trading days.

The **eligible stock universe is exactly the frozen
`tradeable_symbol_list`**.  No later name may be added because its subsequent
spread happens to look attractive.  A name may be unavailable for a specific
event only for a predeclared execution/data reason below.

The genuine missing 2026-09-11 09:30 snapshot remains missing.  It is never
backfilled or replaced.

## 3. Event definition — point-in-time only

An event is eligible when:

1. the stock is in the frozen eligible universe;
2. the V12 point-in-time earnings ledger, as known before entry, identifies an
   earnings/result date;
3. the event's entry and exit fixed-clock snapshots exist;
4. the option expiry used remains alive through the planned exit;
5. all four legs required by the defined-risk structure have executable,
   non-stale bid/ask quotes at entry and exit.

The latest earnings date known **before entry** is used.  Revisions are kept in
the ledger.  No as-known-today calendar reconstruction may replace the
point-in-time record.

## 4. Fixed timing

**Entry:** PRE_CAS snapshot, 15:10 IST, on the last trading session strictly
before the registered earnings date.

**Exit:** OPEN_STABLE snapshot, 09:30 IST, on the first trading session strictly
after the registered earnings date.

This deliberately spans the earnings event regardless of whether the company
publishes before market, during market, or after close on the registered date.
There is no discretionary early exit, profit target, stop, or event-by-event
holding-period change in the research result.

If the required fixed snapshot is absent, the event is marked unavailable; a
later quote is not substituted.

## 5. Contract and structure — defined risk from the start

The research expression is a **one-lot ATM iron butterfly** (short earnings
volatility, capped tails), never a naked short straddle.

At entry:

- use the nearest listed expiry that expires strictly after the planned exit;
- short the call and put at the strike nearest spot;
- compute the executable ATM implied move from
  `(ATM call ask + ATM put ask) / spot`;
- select the protective put wing at the nearest listed strike at or below
  `ATM - 2.0 * implied_move_points`;
- select the protective call wing at the nearest listed strike at or above
  `ATM + 2.0 * implied_move_points`.

The 2.0x wing multiplier is fixed now and is not tuned on Trial-25 outcomes.
If either required wing is outside the recorded deep ladder or lacks an
executable two-sided quote, the event is unavailable rather than converted to
a naked position.

All legs remain present through exit.  No hedge leg may be removed.

## 6. Executable-price accounting

No midpoint P&L is permitted.

Entry credit for one lot is:

`ATM_CE_bid + ATM_PE_bid - lower_PE_ask - upper_CE_ask`.

Exit debit is:

`ATM_CE_ask + ATM_PE_ask - lower_PE_bid - upper_CE_bid`.

Primary event P&L is entry credit minus exit debit, multiplied by the recorded
lot size, less contemporaneous statutory/exchange/broker charges under one
version-stamped charge function.  Bid/ask crossing therefore enters the result
directly rather than as an assumed spread.

If displayed top-level quantity is insufficient for one lot on any leg, the
event is unavailable in the executable primary analysis.  A descriptive
one-lot quote-only sensitivity may be reported separately but cannot rescue the
primary result.

## 7. Predictor — no HAR jump forecast

HAR/continuous realised-volatility forecasts are **not** the Trial-25 earnings
signal.

For every eligible event, compute the firm's historical earnings-event absolute
move using only earnings events available before the current event.  The
historical estimator and minimum-history rule are fixed in the implementation
before Trial-25 development outcomes are opened.

The option-implied event move is the executable ATM straddle ask divided by
spot.  The prespecified conditioning variable is:

`implied_move_pct - historical_earnings_abs_move_pct`.

No RSI, EMA, MACD, OI, volume, market-direction, news-sentiment, or scanner
score is allowed to select Trial-25 events.

## 8. Two-stage forward protocol and power

The external prior is approximately an 8% loss for the long earnings straddle
per event.  Because Trial 25 tests a **defined-risk** structure rather than the
uncapped straddle, the confirmatory effect is conservatively pre-set to
**+4% of the executable ATM short-straddle entry premium** before any
Trial-25 outcome is read.

### Stage D — variance calibration only

The first **40 eligible earnings events** after this registration are
development observations.  They are used only to estimate the standard
deviation of the exact executable, defined-risk primary return and to audit
missingness / execution feasibility.

They produce **no PASS/FAIL efficacy verdict** and cannot change:

- event timing;
- 2.0x wing rule;
- frozen universe;
- +4% confirmatory effect;
- direction of the test;
- outcome definition.

### Stage C — independent confirmation

Before opening any Stage-C outcome, compute and freeze the required independent
sample size using:

`N = ceil(((z_alpha + z_beta) * sigma_D / 4.0)^2)`

where:

- `z_alpha = 1.644854` (one-sided 5%);
- `z_beta = 0.841621` (80% power);
- `sigma_D` is the Stage-D standard deviation in the same percentage-of-ATM-
  premium units.

Minimum Stage-C N is 40 events even if the formula gives less.  There is no
maximum-N truncation that can manufacture a positive verdict; if the required
sample is impractically large, the registered result is **INFEASIBLE TO
CONFIRM**, not a lowered threshold.

## 9. Confirmatory hypothesis and verdict

**Primary null:** mean net defined-risk earnings-event return <= 0.  
**Primary alternative:** mean net defined-risk earnings-event return > 0.

The one-sided critical value is 1.644854.  The primary standard error is
cluster-robust by underlying symbol; date clustering is reported as a
robustness check when multiple events share an exit date.

A PASS requires all of:

1. required Stage-C sample size reached;
2. mean primary net return > 0;
3. one-sided symbol-clustered t-statistic >= 1.644854;
4. executable primary event completeness >= 80%;
5. no single event contributes more than 25% of aggregate positive P&L
   (concentration hard gate).

Otherwise the confirmatory result is FAIL or INCONCLUSIVE according to the
predeclared missing-data rule.  Secondary statistics (win rate, profit factor,
median return, max loss, drawdown, historical-vs-implied slope, sector/event
subgroups) are descriptive and cannot rescue a failed primary test.

## 10. Missing data and revisions

- Missing entry or exit snapshot: event unavailable, never backfilled.
- Missing/one-sided/stale leg: event unavailable.
- Earnings date revised before entry: use the latest point-in-time revision.
- Earnings date revised after entry: keep the event under intention-to-treat
  using the date known at entry; flag the revision for robustness reporting.
- Contract expiry/physical-settlement constraint prevents the planned exit:
  no entry.
- No discretionary exclusions after P&L is known.

## 11. Position-risk interpretation

This is research/shadow evidence only.  It does not authorize live short-option
trading.  If confirmatory evidence eventually passes, any later production
activation must separately lock capital-at-risk limits, portfolio concurrency,
margin, stress loss and kill-switch rules.  The defined-risk wings cannot be
removed merely to improve backtest profitability.

## 12. What remains untouched

- Trial 24's final 31 months remain unread.
- V12/V12.1 recorder thresholds are unchanged.
- The live directional scanner is not a Trial-25 gate.
- NIFTY V12.1 volatility research remains a separate research lane.
- No Trial-25 efficacy outcome is read or scored by this registration commit.
