# DBIndicator — V9.2 Diagnostic Reset

**Build:** `2026-08-31-INSTITUTIONAL-V9.2.1-STAGE3-NOCOPY`

V9.2 is a diagnostic research build. It does not promote a new production rule and does not retune the rejected Bear final sample.

Primary jobs:

- **Bull Gate Funnel:** starts from the broad point-in-time `price up + OI up` population and reports cumulative survivors through the unchanged Bull Institutional Accumulation gates: Long Buildup state, VWAP acceptance, TOD RVOL, Participation, Relative Strength, Derivatives, Bull CLV, basis and final consensus. Bull final 20% remains locked.
- **Bear FSB Regime Decomposition:** explains why the previously validated Bear Fresh Short Buildup rule failed its already-consumed final 20%. It compares validation vs final by market regime, index trend, market volatility, futures-basis direction, stock-vs-sector state, time of day, OI magnitude, OI persistence and post-signal 60-minute positioning. These cohorts are descriptive only and must not become replacement rules.
- **Historical breadth:** explicitly marked unavailable in the current point-in-time dataset rather than inferred or fabricated.

The rejected Bear FSB final test is disabled on the Backtest page. Its frozen fingerprint is preserved for audit continuity only.

Derivative Intelligence remains downstream/live-shadow: CE/PE expression, IV/RV, expected move, liquidity, DTE and Greeks are not fabricated into the historical backtest.

## Backtest

Open **Backtest → Run V9.2 Diagnostic Reset**.

The diagnostic run remains fixed to the full NSE F&O universe, 15-minute setup/execution and 180 calendar days. The streaming/checkpoint architecture from V9.1.2 is retained for Railway reliability.
