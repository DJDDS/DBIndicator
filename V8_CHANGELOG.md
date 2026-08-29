# V8 Dual Alpha Changelog

Build: `2026-08-29-INSTITUTIONAL-V8-DUAL-ALPHA`

## What changed

- Added independent **Bull Alpha** and **Bear Alpha** engines. Bearish logic is direction-aware rather than a sign-reversed bullish formula.
- Added cross-sectional **Structure**, **Participation**, **Relative strength/weakness**, and **Derivatives** evidence families with median consensus rather than fitted weights.
- Added direction-aware OI states: Long Buildup, Short Covering, Fresh Short Buildup, and Long Unwinding. OI remains supporting evidence, never a universal veto.
- Added fixed live states: **TRADE CANDIDATE**, **WATCH**, and **NO EDGE** with a 1.25 ATR chase guard.
- Added a separate late-session 1–2D swing decision surface. A weak intraday setup cannot be upgraded into a swing trade.
- Added point-in-time full-universe percentile enrichment to historical V8 research.
- Added fixed V8 ablations: Raw Recent Range, Structure only, Participation only, Relative only, Derivatives only, and Full Consensus. There is no parameter grid.
- Added independent bullish/bearish validation benchmarks and locked final 20% reporting.
- Added one-click **Run V8 Dual Alpha Backtest**, fixed to the 15-minute signal path and full live NSE stock-F&O universe.
- Added `/api/v8-dashboard` and a dynamic professional decision console with Bull/Bear leaderboards, Intraday/Swing tabs, evidence bars, OI state, chase state, reasons, and no full-page reload.
- Preserved V6/V7 research panels as legacy diagnostics/audit history.

## Fixed V8 live thresholds

- TRADE Alpha: `>= 85`
- WATCH Alpha: `>= 70`
- Participation for TRADE: `>= 70`
- Maximum breakout extension for TRADE: `1.25 ATR`
- Signal source: first 15-minute **Recent Range** escape

These values are pre-declared for V8 and are not selected by a backtest grid.
