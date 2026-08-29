# V8 Dual Alpha Scanner Design

## Goal
Build one production-oriented NSE F&O scanner and backtest suite that treats bullish and bearish opportunities as independent first-class engines, uses cross-sectional evidence rather than indicator voting, keeps 4H contextual, and presents a dynamic professional dashboard.

## Non-negotiable product requirements
- Universe: live NSE stock-F&O universe; backtests use the same universe when Kite is available.
- Signal timeframe: 15-minute Recent-Range first escape; 4H is context only, never the primary trigger.
- Both directions: independent Bull Alpha and Bear Alpha. Bear is not a sign-reversed Bull formula.
- No RSI/MACD/ADX/EMA voting as hard eligibility gates.
- OI is directional sponsorship, not a universal veto.
- Intraday and 1-2D swing evidence are reported separately.
- No giant parameter grid. Predeclared thresholds only.
- Historical order-book depth is not fabricated; live five-level depth remains shadow research data.
- Dashboard must be dynamic, professional, and update through JSON state polling without full-page refresh.

## Core setup
Recent Range uses the prior 6 completed 15-minute bars, excluding the current bar and resetting each NSE session. Bullish event: close > prior Recent High. Bearish event: close < prior Recent Low. Only the first escape in a run is a signal. Breakout extension is distance from the escaped level divided by ATR(14); extension > 1.25 ATR is a chase warning, not an alpha input.

## Evidence families
V8 computes independent 0-100 cross-sectional percentile evidence families. Missing data is neutral/omitted rather than an automatic failure.

### Structure
- breakout_strength: directional breakout distance / ATR(14), cross-sectional percentile.
- directional_clv: Bull = (close-low)/(high-low)*100; Bear = (high-close)/(high-low)*100.
- Structure Rank = median of available breakout-strength percentile and directional CLV.

### Participation
- TOD RVOL percentile.
- opening RVOL percentile.
- bar range / ATR percentile.
- absolute gap / ATR percentile.
- turnover percentile.
- Participation Rank = median of available components.

### Relative performance
For live rows, use stock daily/available return residual versus sector and NIFTY where available, plus existing stock-sector lead fields. For historical research, use event fields currently available and add cross-sectional ranks at each signal timestamp. Bull rewards positive residual; Bear rewards negative residual.

### Derivatives
Classify 60-minute price/OI state directionally:
- Long Buildup: price up, OI up.
- Short Covering: price up, OI down.
- Fresh Short Buildup: price down, OI up.
- Long Unwinding: price down, OI down.
Directional base sponsorship: Bull 100/70/20/0 for Long Buildup/Short Covering/Long Unwinding/Fresh Short Buildup. Bear 100/70/20/0 for Fresh Short Buildup/Long Unwinding/Short Covering/Long Buildup. Scale toward neutral when absolute OI change is small via cross-sectional |OI change| percentile. Futures basis impulse is supportive only: increasing premium helps Bull; increasing discount helps Bear.

### Consensus
- Bull Alpha = median(Structure, Participation, Bull Relative Strength, Bull Derivatives).
- Bear Alpha = median(Structure, Participation, Bear Relative Weakness, Bear Derivatives).
- TRADE CANDIDATE: alpha >= 85 AND participation >= 70 AND source == Recent Range AND extension <= 1.25 ATR.
- WATCH: alpha >= 70 OR a Recent-Range event with meaningful evidence.
- NO EDGE: otherwise.
No weight fitting is permitted in V8.

## Swing layer
Intraday alpha remains the base signal. Swing evidence is evaluated separately using 1D/2D outcomes. Live swing state may add persistence/day-location/OI persistence only when those fields are already available without look-ahead; it must not convert a weak intraday event into a TRADE candidate.

## 4H context
4H may display Trend Up / Trend Down / Neutral / Compression / Extended based on existing closed-bucket context. It is informational/contextual and never a hard eligibility gate in V8.

## Backtest protocol
- Primary V8 research: 15-minute setup, next executable 15-minute entry.
- Up to 365 calendar days where Kite history permits.
- Bull and Bear results separate.
- Outcomes: 30m, 1h, 2h, EOD, 1D, 2D.
- Include configured transaction costs and slippage.
- Chronological 60/20/20 development/validation/final split; final stays locked by default.
- V8 report compares raw Recent-Range, Structure-only, Participation-only, Relative-only, Derivatives-only, and Full Consensus. This is a fixed ablation list, not a parameter search.
- Promotion benchmark: validation N >= 100 per direction, avg net > 0, PF >= 1.25, MFE/MAE >= 1.4 where available, and at least 3/4 positive chronological blocks. Final is revealed only after model freeze.

## Dashboard
Top area is a decision console, not a legacy indicator table.
- Bullish Leaders and Bearish Leaders side-by-side.
- Each card: rank, symbol, TRADE/WATCH/NO EDGE, alpha, Structure, Participation, Relative, Derivatives, OI state, breakout extension, catalyst/context tags, and concise reasons.
- Intraday / Swing view selector.
- Dynamic state endpoint returns V8 rows; browser polls and re-renders without full reload.
- Show last scan, market state, universe count, and stale/error state.
- Keep legacy diagnostics available below for investigation but visually secondary.

## Acceptance
The build is acceptable when unit tests prove directional asymmetry, percentile/median behavior, OI-state mapping, thresholds, backtest split/report separation, dashboard JSON contract, and dynamic UI hooks; the full existing regression suite and Python compile check must pass.
