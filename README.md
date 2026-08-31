# DBIndicator — V9.2 Diagnostic Reset

**Build:** `2026-08-31-INSTITUTIONAL-V9.2.6-LIVE-OPPORTUNITY-RADAR`

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

## V9.2.4 live production repair
The live Dashboard/Watchlist/OI surfaces are evidence-gated: research or rejected playbooks do not generate production candidates. The dashboard reports attempted/valid/error scan counts, and the OI Screener uses a compact JSON-safe API payload so restored state cannot break browser number formatting.


## V9.2.6 live opportunity radar

This release keeps every V9 evidence gate unchanged but separates **production validation** from **live market attention**. The main Dashboard now has a **Live Opportunity Radar — RESEARCH / SHADOW** that can surface bullish and bearish stocks even while `ACTIVE_PLAYBOOKS` is empty.

The radar ranks current F&O names using price + OI structure, day/recent OI expansion, OI acceleration, RVOL/participation, relative strength or weakness, VWAP acceptance, technical structure, 4H context and current F&O breadth. The 4H read is context only, never a veto. Names extended beyond 1.25 ATR remain visible but receive a clear anti-chase penalty.

The **Opportunity Score is an attention/ranking score, not probability of profit and not a validated entry signal**. Rejected Bear Fresh Short Buildup remains rejected; Bull Institutional Accumulation/Catalyst remain shadow-only; alerts and validated TRADE/WATCH shortlists stay evidence-gated.

V9.2.5 scan-health diagnostics are retained: exact per-symbol failure stage, last successful scan, valid/attempted universe counts, current failure details, and the Live Market State OI breadth strip.
