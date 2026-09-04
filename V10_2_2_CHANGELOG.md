# V10.2.2 — Live Reliability Hotfix

Live runtime: `2026-09-04-INSTITUTIONAL-V10.2.2-LIVE-RELIABILITY-HOTFIX`

Research architecture remains V10.2.1; no alpha hypothesis, threshold, validation window, final holdout, or production playbook is changed.

- Settings GET is offline/cache-only and never calls Kite instrument masters.
- Dashboard separates last scan attempt from last successful scan and exposes RUNNING / RETRYING / RESEARCH-PAUSED / MARKET-CLOSED / WAITING-LOGIN state.
- Failed cycles use bounded 10/20/40/60-second retry backoff rather than waiting a full normal scan interval.
- The live F&O universe is persisted and falls back to the last-known-good list if Kite's master request times out.
- F&O cash instrument tokens are persisted/seeded so a restart can keep resolving the prior live universe through a transient master outage.
- OI/NFO master failure no longer aborts the underlying price scan; OI degrades to unavailable for that cycle.
- An empty Kite candle payload gets one bounded retry before a symbol is marked failed.
- Research V10.2.1 provenance/final-holdout locks and live Opportunity Radar logic are unchanged.
