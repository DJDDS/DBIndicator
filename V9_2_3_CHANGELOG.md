# V9.2.3 Live + Backtest Integrity Fix

Build: `2026-08-31-INSTITUTIONAL-V9.2.3-LIVE-BACKTEST-INTEGRITY`

## Live production fix
- Normalizes the naive IST scanner clock to the timezone shape of Kite's intraday candle index inside TOD-RVOL before comparisons.
- Prevents the per-symbol `can't compare offset-naive and offset-aware datetimes` crash that emptied Dashboard/Watchlist/OI Screener rows.
- Adds direction-independent Bull VWAP facts (`bull_vwap_available`, `bull_above_vwap`) to the live signal so V9.1 accumulation is not tied to the legacy majority direction.

## Backtest integrity fix
- 15-minute primary research now defaults visibly to 180 calendar days; V9.2 API remains hard-locked to 180 days.
- Historical V9.2 rows preserve Bull VWAP availability and `close > session VWAP` independently of breakout direction.
- Bull Gate Funnel now reports `VWAP data available` before `Above-VWAP acceptance`.
- The consumed Bear FSB final result is immutable: 68 trades, -0.208% average net, PF 0.68, REJECT.
- V9.2 no longer creates a replacement final sample from a later rolling window. Exact final cohort diagnostics are explicitly unavailable because the original final event IDs were not persisted.

No trading thresholds, costs, Bull/Bear ranking thresholds, or rejected Bear rule fingerprint are changed.
