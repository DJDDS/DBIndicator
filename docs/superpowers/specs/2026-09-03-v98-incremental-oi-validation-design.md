# V9.8 Incremental OI Validation Design

## Goal
Determine whether the already-frozen Trial-19 event `total FUTSTK OI z >= 1.5` adds next-session variance information beyond standard volatility persistence, abnormal futures volume, and the earnings calendar, without retuning Trial 19 or unlocking Trial 18.

## Frozen constraints
- Trial-19 event definition remains `total_z >= 1.5`.
- Trial-19 evidence window remains 2018-09-01 through 2021-08-31.
- Historical membership, official NSE cash prices, OI normalization and prior locked finals remain untouched.
- Existing Trial-19 ATR result remains visible as legacy replicated evidence; V9.8 is an incremental-validation layer, not a rewrite.
- Trial 18 remains LOCKED. Passing V9.8 may only make a future directional trial eligible for preregistration after explicit review.
- `ACTIVE_PLAYBOOKS = ()` remains unchanged.

## Architecture
V9.8 adds a dedicated `app/v98_incremental_oi.py` evaluator. The existing V9.7 runner continues to build the point-in-time symbol frames, but the daily frame is enriched with next-session OHLC variance proxies, HAR state variables and futures-volume surprise. Earnings history is repaired to expose auditable match counts and actual matched examples. V9.8 runs only after frozen Trial-19 efficacy gates pass and reports four high-risk tests before any downstream Clark-West/OOS evaluation.

## Four high-risk tests
1. **Variance target repair**: primary next-session Yang-Zhang-style daily variance proxy (overnight + open-to-close + Rogers-Satchell component); Garman-Klass as robustness; ATR result remains legacy-only.
2. **Full HAR control**: regress next-session variance on daily, weekly and monthly lagged realized-variance state plus the binary extreme-OI event, using two-way date+symbol clustered inference. Report OI coefficient, t-stat and incremental in-sample R².
3. **Volume horse race**: add abnormal total FUTSTK volume z-score to HAR + OI. Report OI and volume coefficients/t-stats and whether OI retains `t >= 3.0`.
4. **Earnings split**: repair symbol/date join, show downloaded records, symbols matched, result dates, overlap counts and examples; report the OI effect inside and outside +/-5 trading sessions around earnings.

## Outcome and gate
V9.8 status is `PASS_INCREMENTAL_OI`, `FAIL_*`, or `INCONCLUSIVE_*`. The decisive pass condition is: adequate variance-target coverage, positive OI coefficient with two-way clustered `t >= 3.0` after HAR + abnormal volume, positive incremental R², and a valid earnings join with an outside-earnings effect that remains positive with confidence interval above 1.0 on the variance-scale matched diagnostic. No production activation follows automatically.

## Deferred methods
Clark-West MSPE-adjusted, Campbell-Thompson OOS R², MSE/QLIKE forecast comparison, reverse-direction diagnostic and OI level/change/surprise decomposition are deferred until the four high-risk tests pass. They are not used to rescue a failed V9.8 result.
