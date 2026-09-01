# V9.3.4 — Research Worker Stability + Slim App

Build: `2026-09-01-INSTITUTIONAL-V9.3.4-RESEARCH-WORKER-STABILITY-SLIM`

- Historical research and the live scanner now share one exclusive Kite-heavy runtime slot; research has priority and the live scanner yields until the lab ends.
- V9.3 daily continuous OI is acquired inside each symbol batch, so completed symbols become resumable units instead of depending on a separate 210-symbol pre-sweep.
- Research checkpoints live under configurable `RESEARCH_STATE_DIR`; restart messages promise resume only when durable files are actually present.
- Research status exposes worker PID/RSS telemetry for diagnosing host pressure.
- Signal Journal public UI, logging, confidence badges and per-cycle resolver are removed; existing journal data files are not deleted.
- Dead Custom Backtest UI and obsolete public gate-ablation routes are removed.
- OI acceleration remains available to the screener, Opportunity Radar and Component Edge Lab, but OI-acceleration-only desktop/in-app alert events and their API polling surface are removed.
- Duplicate `/api/early-research/*` route definitions are removed.
- Strategy rules, Trial 13, 0.18% friction, 1.25 ATR guard, 1D/2D horizons and final locks are unchanged.
