DBIndicator post-deploy UI/runtime fix

Fixes:
- Backtest research JS controller restored (page no longer breaks on load).
- 15-minute research can run up to 365 calendar days; timeframe-specific day bounds update dynamically.
- Settings shows current live F&O universe, latest scanned-stock count, and research-watchlist count.
- Dashboard shows live Radar / Intraday / Swing counts and polls scan state every 8 seconds.
- Dashboard re-renders Early Radar, Intraday, Swing, and watchlist only when a new scan arrives.

Copy the contents of this patch over the existing cloned DBIndicator repo, preserving paths, then commit/push.
