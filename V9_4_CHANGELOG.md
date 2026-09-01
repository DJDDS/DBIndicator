# V9.4 Measurement Repair + Trial Resolution

Build: `2026-09-01-INSTITUTIONAL-V9.4.0-MEASUREMENT-TRIAL14`

- Settles Trial 13 on development + validation only; locked final 20% remains unread.
- Adds payoff decomposition, top-winner sensitivity, and trading-day bootstrap intervals.
- Pre-registers Trial 14: point-in-time Daily OI Anomaly + fresh Compression Onset -> Expansion; directionless, 1D primary, 2D secondary, final 20% locked.
- Fixes the V9.3 VWAP truth-value bug that could report zero aligned events.
- Replaces the trivial one-day-vs-15m-ATR hit rate with horizon-scaled daily-ATR movement measurements.
- Centralizes directional research friction through `app.costs` and validates the computed 0.18% drag.
- Long-vol forward validation now enters ATM CE+PE at executable asks and exits at executable bids; expectancy and profit factor are reported.
- Adds 2D option-forward maturation.
- Persists a tiny point-in-time daily-OI cache from the V9.4 research shards so live Trial-14 shadow registration does not refetch 210 histories during market hours.
- Live Trial 14 registers only a fresh compression onset with Daily OI z >= 1.5, and only as RESEARCH/SHADOW. It cannot create production TRADE/WATCH signals or alerts.
- OI Screener and OI acceleration calculations remain intact.
