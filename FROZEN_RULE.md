# V9.1 Goal-Focused Research Lock

**Build:** `2026-08-30-INSTITUTIONAL-V9.1.2-STREAMING-BACKTEST`

## Frozen Bear rule

`BEAR_FSB_15M_NEXTBAR_1D_V91`

The final-test path is fixed to the full NSE stock-F&O universe, 15-minute setup and execution, 180 calendar days, 0.08% round-trip costs and 0.05% slippage per side.

Bear Fresh Short Buildup is frozen as validated in V9:
- fresh bearish breakout;
- futures state = **Fresh Short Buildup** (price down + OI up);
- breakout extension <= 1.25 ATR;
- Participation >= 70;
- Relative Weakness >= 60;
- direction-aware Derivatives >= 65;
- bearish close-location >= 65;
- futures-basis acceleration <= +0.02 when available;
- median evidence score >= 70.

Final acceptance is predeclared: at least 60 final trades, average net >= +0.15%, PF >= 1.25, and at least 3 of 4 chronological final blocks positive.

## Bull research rule

**Bull Institutional Accumulation** is research-only and its final 20% stays locked. The historical probe starts from price up + OI up, above-VWAP acceptance and at least normal TOD participation, then requires cross-sectional Participation >= 70, Relative Strength >= 70, Derivatives >= 65, bullish close-location >= 60, and non-deteriorating basis when available.

Bull Catalyst Continuation stays live/shadow until a point-in-time historical news archive exists.
