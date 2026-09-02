# V9.6.2 Trial 17 Promotion Controls Design

## Goal
Preserve frozen Trial 17 exactly while adding promotion-only controls that decide whether Trial 18 may be unlocked.

## Frozen Trial 17
- Signal: total share-equivalent FUTSTK OI z >= 1.5.
- Evidence window: 2021-09-01 through 2023-09-01.
- Primary endpoint: next-session 1D movement in horizon ATR.
- Secondary 2D cannot rescue failed 1D.
- No DTE exclusions, threshold tuning, date changes, or use of prior locked finals.

## Promotion controls
1. Historical F&O membership, cash-price coverage, OI normalization, and MWPL/ban must be APPLIED.
2. Earnings exclusion: remove event and matched-baseline observations within +/-5 trading sessions of a symbol's published NSE financial-result filing date. This is a promotion control only; it does not rewrite the frozen Trial 17 result.
3. Same-day matched baseline: for each Trial-17 event date, compare event stocks with eligible non-event F&O stocks from that same date. Report matched lift and day-cluster bootstrap CI.
4. Market regime controls: add India VIX close and NIFTY 50 trailing realized volatility as same-day/past-information covariates, together with existing stock trailing realized vol, ATR%, and DTE.
5. Two-way clustered inference: estimate the total-OI-z coefficient with date + symbol clustered covariance.
6. DTE-matched baseline: compute lift using non-event observations sampled/weighted to the event DTE-bucket distribution without excluding any DTE bucket.

## Promotion rule
Trial 18 remains locked unless all of the following are true:
- Frozen Trial 17 status is PASS_INDEPENDENT_VALIDATION.
- Historical membership, historical cash, lot-size OI normalization, and MWPL/ban controls are APPLIED.
- Earnings calendar coverage >= 90% of event symbols and earnings-excluded 1D lift > 1.0 with CI lower bound > 1.0.
- Same-day matched 1D lift > 1.0 with CI lower bound > 1.0.
- Two-way clustered total-OI-z coefficient is positive with t >= 3.0.
- Market-regime coverage >= 90% of event days.
- DTE-matched 1D lift > 1.0.

## Safety
- `ACTIVE_PLAYBOOKS = ()` remains unchanged.
- V9.6.2 cannot emit TRADE/WATCH.
- ATM IV remains unavailable/not fabricated for the 2021-2023 stock-option surface.
- Participant-wise OI and directional modeling are explicitly deferred until Trial 18 becomes eligible.
