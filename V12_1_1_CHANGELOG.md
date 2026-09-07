# V12.1.1 — Recorder Integrity Hotfix

This is a bounded infrastructure/reliability hotfix on top of the frozen
V12.1 research build. The V12.1 research identity remains unchanged.

## Fixed

- `quote_age_seconds` now normalizes source clocks before subtraction:
  recorder-side naive timestamps are interpreted as IST and Kite exchange
  naive timestamps as UTC, then both are converted to UTC. This removes the
  observed ~19,800-second (5.5-hour) false quote-age offset while preserving
  the raw exchange and last-trade timestamps already recorded.
- The NIFTY `KiteTicker` now uses the supported `threaded=True` connection
  path so Twisted does not try to install process signal handlers from the
  background recorder thread.
- A service-level ticker lifecycle guard prevents the polling `run_forever()`
  loop from creating duplicate concurrent KiteTicker instances while the
  active ticker owns connection/reconnection.
- Native KiteTicker reconnect callbacks are observed rather than recursively
  calling `connect()` from `on_close`; terminal `on_noreconnect` releases the
  lifecycle so a later outer retry can create a fresh ticker.

## Explicit non-changes

- No strategy thresholds changed.
- No option-selection, strike-ladder, persistence, feasibility, RV-model or
  opportunity-console rule changed.
- Historical `/data` files are not rewritten; preserved raw timestamps make
  prior quote-age diagnostics recomputable if needed.
- Trial 25 remains LOCKED.
- The V12.1 research build marker remains
  `2026-09-06-INSTITUTIONAL-V12.1-INDEX-VOLATILITY-RECORDER-FEASIBILITY-LAB`.
