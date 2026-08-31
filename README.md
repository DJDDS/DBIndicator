# DBIndicator — V9.2 Diagnostic Reset

**Build:** `2026-08-31-INSTITUTIONAL-V9.2.9-PIPELINE-RELIABILITY-AUDIT-HARDENING`

V9.2 is a diagnostic research build. It does not promote a new production rule and does not retune the rejected Bear final sample.

Primary jobs:

- **Bull Gate Funnel:** starts from the broad point-in-time `price up + OI up / Long Buildup` population and reports cumulative survivors through VWAP acceptance, TOD RVOL, Participation, Relative Strength, Derivatives, Bull CLV, basis and final consensus. The duplicate price+OI/Long-Buildup evidence is shown once. Bull final 20% remains locked.
- **Bear FSB Regime Decomposition:** explains why the previously validated Bear Fresh Short Buildup rule failed its already-consumed final 20%. It compares validation vs final by market regime, index trend, market volatility, futures-basis direction, stock-vs-sector state, time of day, OI magnitude, OI persistence and post-signal 60-minute positioning. These cohorts are descriptive only and must not become replacement rules.
- **Historical breadth:** explicitly marked unavailable in the current point-in-time dataset rather than inferred or fabricated.

The rejected Bear FSB final test is disabled on the Backtest page. Its frozen fingerprint is preserved for audit continuity only.

Derivative Intelligence remains downstream/live-shadow: CE/PE expression, IV/RV, expected move, liquidity, DTE and Greeks are not fabricated into the historical backtest.

## Backtest

Open **Backtest → Run V9.2 Diagnostic Reset**.

The diagnostic run remains fixed to the full NSE F&O universe, 15-minute setup/execution and 180 calendar days. The streaming/checkpoint architecture from V9.1.2 is retained for Railway reliability.

## V9.2.4 live production repair
The live Dashboard/Watchlist/OI surfaces are evidence-gated: research or rejected playbooks do not generate production candidates. The dashboard reports attempted/valid/error scan counts, and the OI Screener uses a compact JSON-safe API payload so restored state cannot break browser number formatting.


## V9.2.6 live opportunity radar (retained)

This release keeps every V9 evidence gate unchanged but separates **production validation** from **live market attention**. The main Dashboard now has a **Live Opportunity Radar — RESEARCH / SHADOW** that can surface bullish and bearish stocks even while `ACTIVE_PLAYBOOKS` is empty.

The radar ranks current F&O names using price + OI structure, day/recent OI expansion, OI acceleration, RVOL/participation, relative strength or weakness, VWAP acceptance, technical structure, 4H context and current F&O breadth. The 4H read is context only, never a veto. Names extended beyond 1.25 ATR remain visible but receive a clear anti-chase penalty.

The **Opportunity Score is an attention/ranking score, not probability of profit and not a validated entry signal**. Rejected Bear Fresh Short Buildup remains rejected; Bull Institutional Accumulation/Catalyst remain shadow-only; alerts and validated TRADE/WATCH shortlists stay evidence-gated.

V9.2.5 scan-health diagnostics are retained: exact per-symbol failure stage, last successful scan, valid/attempted universe counts, current failure details, and the Live Market State OI breadth strip.


## V9.2.8 backtest-integrity + shadow-radar upgrade

V9.2.8 keeps the V9.2.7 regime and forward-validation architecture but fixes the research-integrity issues found in the production audit: correct two-sided slippage, rising-edge Bull accumulation episodes, chunked historical retrieval, completed-candle-only replay, and explicit price/OI history coverage. The Production Early Radar stays evidence-gated; a separate Shadow Early Radar now exposes Energy Building / Ignition research stages without creating TRADE/WATCH alerts. Legacy dashboard diagnostic columns are labelled explicitly so they cannot be confused with the 0–100 Live Opportunity Score.

## V9.2.7 regime + forward-validation upgrade

V9.2.7 keeps the V9.2.6 Live Opportunity Radar but fixes the three production-integrity issues found in the live deployment:

- the live NSE stock-F&O universe is cross-checked against the NSE cash instrument map, so non-stock derivative names such as `NIFTYFPI` are excluded before scanning;
- persisted breakout-extension settings above **1.25 ATR** are migrated back to 1.25 and the Settings page cannot loosen the anti-chase ceiling beyond 1.25;
- Market Bias is now a weighted, missing-data-aware regime score from **NIFTY trend (25%) + watchlist price breadth (20%) + F&O OI breadth (20%) + sector breadth (15%) + relative-strength distribution (10%) + VWAP participation (10%)**. Regime remains ranking context only, never a veto.

The build also starts honest **forward validation** for Live Opportunity Radar names. The first top-5 Bull/Bear appearance for each symbol+direction per trading day is recorded with its original score/rank/entry price. Outcomes are then measured from the first available live scan at/after **30m, 1h, 2h, 4h and next-session same-time (1D)**. Intraday horizons that do not mature before the session ends are marked unavailable rather than contaminated with an overnight move. The forward state is persisted inside the normal scanner state and can be exported from the Dashboard.

## V9.2.9 pipeline-reliability + audit-hardening upgrade

V9.2.9 fixes the Stage-2 research bottleneck that could leave the production backtest apparently stuck at cross-sectional rank 6/7. Each symbol shard is now deserialized once per Stage-2 run, compact feature frames are ranked in memory, granular progress/elapsed-time messages are emitted, and rank-level checkpoints allow Railway restarts to resume after the last completed rank. A full 210-symbol × 5,000-bar regression test protects the workload.

The build also adopts the low-risk measurement recommendations from the external validation audit: forward validation now headlines net expectancy, net profit factor and 95% Wilson confidence intervals; duplicate Price+OI/Long-Buildup evidence is collapsed in the Bull diagnostic funnel; Bear frozen thresholds are applied at one freeze boundary rather than duplicated in the compactor; historical trial count/Bonferroni alpha and current-universe survivorship bias are disclosed explicitly. No Deflated Sharpe/FDR, MWPL history, point-in-time F&O membership or new Bear strategy is fabricated without the required data/protocol.

