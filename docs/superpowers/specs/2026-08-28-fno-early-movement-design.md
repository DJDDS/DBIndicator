# F&O Early Movement Screener Design

## Goal
Surface a very small list of NSE stock-futures names where a move appears to be starting now, not names whose daily indicators are already mature. The live engine is F&O-only and optimises for 1-3 day follow-through after costs.

## Evidence model
Best Entries stops counting RSI/MACD/EMA/volume as four equal votes. It uses five evidence groups:

1. **Fresh trigger** — RSI(14) crossing its own SMA plus MACD momentum as confirmation. A state that has been bullish for many bars is not an entry trigger.
2. **F&O positioning** — aggregated near/next/far stock-futures OI, recent 30m/60m OI growth, OI acceleration, and direction-consistent fresh buildup. Missing or neutral OI cannot qualify a Best Entry.
3. **Participation** — intraday time-of-day relative volume, so the 09:30 bar is compared with prior 09:30 bars rather than with midday volume. Static raw RVOL remains informational only.
4. **Relative strength** — stock versus NIFTY and, when available, sector direction. Continuation candidates must lead in their trade direction rather than merely move with the market.
5. **Entry location / structure** — price must be on the correct side of VWAP, not more than 1.25 ATR beyond it, and preferably emerge from recent volatility compression/range breakout rather than an already-expanded candle.

## Live timeframe
The primary live Best Entries engine runs on 15-minute data, with higher-timeframe trend confirmation from the existing higher-timeframe machinery. The scanner may evaluate a still-forming 15-minute candle, but a fresh trigger must be recent and the first 15 minutes remain excluded.

## Parameters removed from live Best Entries
The following stay available for charts/research if already computed, but no longer gate or add points to Best Entries: candlestick-pattern agreement, big-candle agreement, strong-close agreement, delivery percentage, breadth agreement, generic ADX regime, and BTST/STBT score. These either duplicated existing information, were directionally ambiguous, arrived too late, or failed the user's own ablation/overnight tests.

## BTST/STBT
The live BTST/STBT candidate panel and alerts are retired. The Backtest page retains overnight continuation/reversal research and explicitly reports that the feature remains disabled until a chronological holdout has positive net expectancy and profit factor > 1.1 with a meaningful sample.

## F&O universe
The live universe is refreshed from Kite NFO stock futures each trading day. Index futures and non-stock F&O names remain excluded.

## Ranking
A Best Entry must pass all hard integrity/timing gates, then is ranked 0-100 by independent evidence:
- OI positioning/recent acceleration: 35
- time-of-day participation: 20
- fresh momentum trigger: 20
- relative strength/market context: 15
- entry structure/location: 10

No missing component receives neutral points. Coverage is shown separately. The shortlist defaults to at most 5 names.

## Validation
Backtest/research reports 1/2/3/5/10-bar net outcomes and uses 3 bars as the primary 1-3 day objective. Gate/parameter promotion must use chronological holdout results, not the same sample used to discover the rule. Win rate alone never promotes a rule; net expectancy and profit factor are primary.
