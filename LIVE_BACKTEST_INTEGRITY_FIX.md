# Live Scanner + Backtest Integrity Fix

This release repairs two data-integrity faults without changing the strategy thresholds.

1. **Live scan:** Kite may return timezone-aware intraday indexes while `now_ist()` is a naive IST wall-clock. TOD-RVOL now normalizes only for the forming-bar comparison, so Watchlist and OI rows are no longer replaced by datetime error rows.
2. **V9.2 research:** Bull accumulation now stores direction-independent session-VWAP availability/acceptance, and the consumed Bear final summary is immutable rather than recomputed from a later rolling research window.
