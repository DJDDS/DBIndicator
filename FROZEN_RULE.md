# V9 Professional Playbook Research Lock

**Build:** `2026-08-30-INSTITUTIONAL-V9-PROFESSIONAL-PLAYBOOKS`

V9 is designed for the project's fixed objective: **both bullish and bearish NSE stock-F&O opportunities, intraday and 1–2 day swing, with derivative intelligence downstream for option expression.**

## Production selection architecture

V9 does not use one universal Bull/Bear score as the trade thesis. It classifies explicit playbooks:

### Bullish
1. **Bull Opening Drive** — early-session Opening-Range escape with abnormal participation, relative leadership, high-close acceptance and anti-chase.
2. **Bull Pullback/Reclaim** — bullish Recent-Range breakout followed by a confirmed retest/reclaim with relative strength and acceptable VWAP/location.
3. **Bull Catalyst Continuation** — real live event/news catalyst plus bullish price/participation confirmation. This is **LIVE/SHADOW only** until a point-in-time historical event archive exists.

### Bearish
4. **Bear Fresh Short Buildup** — bearish fresh price break with price-down/OI-up positioning, relative weakness, selling participation and non-improving basis.
5. **Bear Failed Breakout** — prior bullish breakout fails back through the decision level and rejects VWAP with bearish acceptance.
6. **Bear VWAP Retest Failure** — bearish breakdown retest fails and relative weakness persists.

## Fixed controls
- NSE stock-F&O universe.
- Signal timeframe: **15 minute**.
- 4H: context only, never a universal veto.
- Maximum extension/chase guard: **1.25 ATR**.
- OI/basis: supporting evidence; Bear Fresh Short Buildup explicitly requires fresh short positioning because that is the playbook's economic premise.
- Live focus: maximum three Bull and three Bear TRADE candidates per mode; WATCH candidates remain visible.
- Intraday and 1–2D Swing are independent states.

## Historical validation
Primary V9 backtest is locked to **15minute / 180 calendar days / current stock-F&O universe** and reports each backtestable playbook independently at 30m / 1h / 2h / EOD / 1D / 2D.

Each playbook uses chronological **60% development / 20% validation / 20% locked final**, plus four validation blocks. A weak Bear playbook cannot be averaged with a strong Bull playbook.

No final 20% is exposed until an individual playbook is frozen after adequate validation.

## Derivative Intelligence
The existing derivative-intelligence layer remains downstream. It can label a qualified underlying playbook as OPTION BUYER EDGE, OPTION EXPENSIVE, PREMIUM RICH / DEFINED-RISK SELLING BIAS, UNDERLYING ONLY / WAIT, or OPTION DATA INSUFFICIENT. It cannot manufacture an underlying trade.

Kite does not supply a complete point-in-time historical stock-option chain suitable for an honest option-P&L replay. Live option evidence is therefore forward-marked rather than retrospectively fabricated.
