# V9.2.6 Live Opportunity Radar

Build: `2026-08-31-INSTITUTIONAL-V9.2.6-LIVE-OPPORTUNITY-RADAR`

## Why this build exists

V9.2.5 correctly kept the production shortlist empty because no V9 playbook has passed the evidence gate, but that made the main dashboard look inactive even while the OI engine was detecting strong live bullish/bearish positioning.

## What changed

- Added a separate **Live Opportunity Radar — RESEARCH / SHADOW** on the main dashboard.
- It ranks bullish and bearish F&O attention names from live price/OI structure, day and recent OI change, OI acceleration, participation/RVOL, relative strength/weakness, VWAP agreement and technical structure.
- Fresh **Long Buildup / Short Buildup** receives the strongest directional prior; short covering/long unwinding can appear with lower structural weight.
- Market OI breadth alignment adds context, but never creates a production trade.
- The existing **1.25 ATR anti-chase guard** is applied as a score penalty and visibly marks extended names instead of silently hiding them.
- Dashboard toolbar now separates **Live opportunities** from **Validated Intraday / Validated Swing** counts.
- The old production early radar is relabelled so an empty validated path is not confused with an inactive live scanner.

## What did NOT change

- `ACTIVE_PLAYBOOKS` remains empty.
- Bear Fresh Short Buildup remains rejected and cannot be resurrected by the live radar.
- Bull Institutional Accumulation and Bull Catalyst Continuation remain shadow/research only.
- No backtest threshold, frozen rule, final-test result, alert permission or production evidence gate was loosened.
- Opportunity Score is an attention/ranking score, **not probability of profit** and not a validated entry signal.
