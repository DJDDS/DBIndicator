# V8.1 Evidence-Locked Changelog

Build: `2026-08-29-INSTITUTIONAL-V8.1-EVIDENCE-LOCKED`

## Why this build exists

The 180-day V8 validation showed that fixed-cutoff Bull Structure weakened materially, Bull Full Consensus stayed interesting but was too sparse, and Bear Full Consensus failed. V8.1 changes architecture only where that evidence justified it; it does not search new thresholds.

## Production changes

- Retired V7 final-test execution from normal research and removed its UI button/panel. Its final sample has already been seen and is not reusable for tuning.
- Removed V6 Intraday/Swing production cards from the main dashboard.
- Replaced the live V6 shortlist/alert bridge with V8.1 operational shortlist fields so dashboard ranks and alerts use the same engine.
- Bull operational selection: Recent-Range bullish escape + Participation >= 70 + decision score >= 70 + <=1.25 ATR extension, then current cross-sectional Top 3 by Bull Alpha.
- Bear operational selection: any bearish breakout source + Participation >= 70 + Bear Pressure >= 70 + <=1.25 ATR extension, then current cross-sectional Top 3 by Bear Pressure.
- Bear Pressure is the median of Participation, Relative Weakness, direction-aware Derivatives/OI, and bearish close-location value. Mirrored bullish Structure is excluded.
- Backtest primary breadth is fixed to Top 1 / Top 3 / Top 5; Top 3 is predeclared as the operational breadth. This is a portfolio-breadth test, not a score-threshold grid.
- Every primary 2H and 1D variant now exposes all four chronological validation blocks with N, average net return and profit factor.
- Final 20% remains locked for V8.1.
- 4H remains context only; no 4H breakout is required.

## Intentionally unchanged

- 15-minute signal/entry architecture.
- Full NSE stock-F&O universe.
- Transaction cost/slippage assumptions already used by the research engine.
- 1.25 ATR anti-chase guard.
- OI is evidence, never a universal veto.
