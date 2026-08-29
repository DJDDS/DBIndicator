# F&O Stock-in-Play Breakout Design

## Goal
Build an NSE stock-F&O screener that finds movement early for two distinct use cases: intraday trades and 1–2 trading-day swings. Replace pre-breakout directional indicator voting with a staged engine that first identifies stored energy or abnormal participation, then lets price reveal direction through an actual range breakout/breakdown, and finally uses volume/OI/context as sponsorship and risk controls.

## Evidence from current research
- Compression/coil has shown a high frequency of subsequent absolute ATR expansion, but the existing directional ignition model has negative holdout expectancy and low profit factor.
- RSI/MACD/relative-strength acceleration/TOD RVOL individually have not produced a directional edge in the current replay.
- Historical OI velocity is currently unmeasured because `_session_pct_change` compares timezone-aware session dates with timezone-stripped shifted dates, causing every same-session comparison to fail.
- 4-hour should be context, not an entry timeframe.

## Live architecture
### Stage 1 — Radar
A symbol can enter Radar in either of two ways:
1. **Compression setup:** compression score >= 60, or
2. **Stock-in-play setup:** abnormal time-of-day participation/range/gap without requiring a coil.

Radar is directionless. It answers: “is movement becoming likely?”

### Stage 2 — Ignition
Direction is assigned only when a completed/current 15-minute bar closes outside a prior decision range. Breakout sources:
- recent 90-minute range (previous 6 bars),
- 30-minute opening range after it is fully formed,
- compression range where applicable.

A fresh breakout must be a rising edge: the previous close was not already beyond the same range.

### Stage 3 — Intraday Best Entry
Require:
- fresh breakout/breakdown,
- price on the correct side of session VWAP,
- TOD RVOL >= 1.3 or equivalent strong stock-in-play participation,
- entry <= 1.25 ATR beyond the breakout reference,
- at least one sponsorship axis: positive recent futures OI in the breakout direction, or strong market/sector relative participation when historical/live OI is unavailable.

OI unavailable is explicit; it never silently passes as “OI confirmed.”

### Stage 4 — Swing 1–2 Day Candidate
Require the intraday ignition plus:
- 4-hour context not strongly opposed,
- no major extension,
- stronger participation/retention,
- late-session persistence when the signal is being considered for overnight holding.

Swing classification is separate from intraday classification; the same event can be valid intraday but not for overnight risk.

## Parameters to remove from primary live eligibility
- RSI crossover gate
- MACD crossover gate
- generic candlestick-pattern gate
- delivery percentage as directional evidence
- strong-close gate
- big-candle gate
- ADX gate
- 4-of-4 indicator voting
- RS acceleration as a mandatory condition

RSI/MACD may remain display diagnostics only.

## Backtest architecture
Primary timeframe: 15-minute NSE stock-F&O universe.

### Intraday horizons
- 30 minutes (2 bars)
- 1 hour (4 bars)
- 2 hours (8 bars)
- 4 hours (16 bars)
- same-session close

### Swing horizons
- next trading-session close (1D)
- second trading-session close (2D)

### Research outputs
- net expectancy after cost/slippage
- win rate and profit factor
- MFE/MAE in percent and ATR units
- time to +0.5 ATR and +1 ATR
- long and short splits
- setup-source splits (compression breakout / opening-range breakout / recent-range breakout / stock-in-play)
- chronological 30% holdout
- component interaction tests limited to economically motivated combinations
- OI coverage and explicit unavailable counts

### Compression research
Compression is directionless. Compare event expansion rates against an unconditional F&O-bar baseline and report lift for >=0.5/0.75/1.0/1.5 ATR expansion over 1h/2h/4h/1D.

### OI parity
Fix timezone-safe same-session OI change calculation. Historical intraday OI uses available futures contract history and is labelled approximate around rollover; live ranking continues to aggregate near/next/far OI.

## Promotion rule
No live parameter becomes mandatory because it sounds plausible. A candidate rule is promoted only if chronological holdout has positive net expectancy, profit factor >= 1.15, adequate sample size, and no obvious collapse in the opposite direction or market regime.
