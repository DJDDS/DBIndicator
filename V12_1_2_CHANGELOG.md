# V12.1.2 — WebSocket Session-Rollover / Health Hotfix

This is a bounded infrastructure/reliability hotfix on top of V12.1.1. The
V12.1 research identity remains unchanged.

## Fixed

- The NIFTY WebSocket lifecycle now retires the prior trading-day ticker at
  market close and on a detected session rollover, so an old lifecycle cannot
  block the next trading day's expiry/universe rebuild.
- When Twisted's reactor is already running, later KiteTicker lifecycle starts
  are marshalled onto the reactor thread with `callFromThread` rather than
  invoking Twisted connection APIs directly from the recorder service thread.
- A market-hours watchdog retires a ticker that has delivered no live ticks for
  the configured stale interval, allowing the outer service loop to reconnect
  cleanly instead of remaining stuck indefinitely.
- Recorder health is freshness-aware: an old `last_tick_at` now reports
  `STALE` during market hours instead of falsely reporting `RECORDING`.
- Retired ticker callbacks are ignored so a late callback from an old lifecycle
  cannot overwrite the state of a replacement session.

## Explicit non-changes

- No strategy thresholds changed.
- No option-selection, strike-ladder, feasibility, RV-model, stock-option
  recorder, opportunity-console, execution or risk rule changed.
- Existing `/data` research files are not rewritten.
- Trial 25 remains LOCKED.
- The V12.1 research build marker remains
  `2026-09-06-INSTITUTIONAL-V12.1-INDEX-VOLATILITY-RECORDER-FEASIBILITY-LAB`.
