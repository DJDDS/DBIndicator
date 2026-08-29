# V6 Institutional Edge Design

## Goal
Build a research-led NSE stock-F&O screener for intraday and 1–2 day swing trades that detects genuine "stocks in play" early, lets price reveal direction, uses sponsorship/context as supporting evidence, and validates entry *and exit* logic on chronological out-of-sample data.

## Core design
V6 replaces the V5 OI-heavy eligibility rule with a layered evidence model:

1. **Market regime** — classify trend-up, trend-down, rotation, chop/high-volatility from NIFTY, breadth/dispersion live and equivalent point-in-time index/sector context in research.
2. **Catalyst / stock-in-play proxy** — gap/ATR, opening participation, bar-range shock, time-of-day RVOL, and cross-sectional turnover rank. This estimates information-driven activity without pretending to know news causality.
3. **Leadership / price location** — stock-vs-sector relative return, sector rank when available, and price location versus previous 20/50-session highs/lows. Long momentum receives more credit near highs with high participation; short logic remains research-only until it proves itself.
4. **Recent-range setup quality** — V5 recent-range breakout/retention/retest remains the primary directional setup because it is the strongest validated subgroup so far.
5. **Sponsorship** — volume, OI, futures basis and context. OI is a 10–15% supporting feature, never a universal hard gate. Volume + OI or volume + expanding basis can sponsor a breakout.
6. **5-minute execution** — 15-minute data selects setups; only finalists are checked on 5-minute data for micro-retention/retest, VWAP side, volume burst and extension. Historical 5-minute execution is replayed separately where data is available.
7. **Path-aware exits** — fixed-horizon returns remain visible, but V6 adds conservative first-touch target/stop research and simple trail/breakeven variants, because the prior research showed large MFE but poor fixed-horizon expectancy.

## Live hierarchy
- Stock in Play
- Recent-Range Setup
- Sponsored Recent-Range
- Intraday V6 Entry
- Swing V6 1–2D

Generic opening-range/compression breakouts remain radar/research only.

## Research discipline
- 60% development / 20% validation / 20% locked final-test split.
- Final-test metrics are hidden by default; `V6_UNLOCK_FINAL_TEST=true` is required to display them after the model is frozen.
- Promotion requires positive validation expectancy after costs, PF >= 1.25, adequate sample size, excursion quality, and chronological stability.
- Long and short models are evaluated separately. Live Swing V6 is long-only until the short model independently clears promotion.
- Features with partial historical coverage (intraday futures basis, 5-minute execution) report coverage explicitly and cannot silently become hard gates.

## Non-goals
- No proprietary institutional formula claims.
- No brute-force optimization over thousands of combinations.
- No use of live order-book depth as an eligibility gate; depth stays shadow-only until forward evidence clears promotion.
- No guarantee of >50% win rate. Expectancy, PF and drawdown matter more than raw hit rate.
