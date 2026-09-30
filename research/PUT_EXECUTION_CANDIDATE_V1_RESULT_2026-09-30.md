# PUT_EXECUTION_CANDIDATE_V1 — Historical Result (30 Sep 2026)

**Status:** RESEARCH FAILED / DO NOT DEPLOY.

## Methodology
- Bearish/PUT only.
- Historical strict option candidates from the uploaded Dhan 1-minute option pack.
- ATM-1 / ATM / ATM+1 contracts.
- Symbols must pass the frozen recorder's <=4% practical spread gate.
- Features available at signal time only: 1/3/5m premium return, premium acceleration,
  IV changes, OI changes, option volume ratio, bearish-direction spot momentum,
  moneyness, absolute delta, DTE, premium/spot, elasticity, spread burden,
  time-of-day, strike offset and expiry code.
- Model: regularized logistic regression (C=0.2, balanced classes).
- Target during development: positive 5-minute return after each symbol's frozen
  recorder median spread sensitivity.
- Apr-May: training.
- June: contract/gate validation.
- Frozen June gate: best ATM±1 contract plus probability >= 0.613783893455
  (top 50% of June signal scores).
- Jul-Sep: historical OOS diagnostic. No OOS retuning.

## June validation
- Selected signals: 8
- Mean 5m spread-adjusted return: +3.54%
- Median: +2.34%
- Winner rate: 75.0%

## Jul-Sep historical OOS
- Selected signals: 26
- Trading days: 18
- Unique symbols: 26
- Mean 5m spread-adjusted return: -0.86%
- Median: -1.17%
- Winner rate: 46.2%
- Raw 5m mean before spread: +0.83%
- Global-median-spread 5m mean: -0.98%
- Global-P75-spread 5m mean: -1.72%
- OOS row-level AUC: 0.497

Month-by-month symbol-specific 5m means:
- July: +0.29%
- August: -4.29%
- September: +1.05%

## Verdict
The June validation effect did not survive Jul-Sep. The historical OOS mean and
median are both negative after realistic symbol-level friction, and the probability
ranking has no useful OOS discrimination. This PUT model is rejected.

Do not invert CALL V1, do not deploy this PUT gate, and do not retune on the same
Jul-Sep sample. Bearish option buying should remain disabled until genuinely new
forward bid/ask data supports a separate PE model.
