# V9.6.2 — Trial 17 Promotion Controls

Build: `2026-09-02-INSTITUTIONAL-V9.6.2-TRIAL17-PROMOTION-CONTROLS`

## Frozen evidence remains unchanged
- Trial 17 stays exactly `total FUTSTK OI z >= 1.5`.
- Fixed independent window remains `2021-09-01` through `2023-09-01`.
- 1D remains primary; 2D cannot rescue 1D.
- No DTE bucket is removed or optimized.
- Prior Trial 13/15 final holdouts remain untouched.

## Promotion-only controls added
- Official NSE financial-result filing calendar, excluding ±5 trading sessions around earnings for a separate promotion check.
- Same-day cross-sectional matched baseline using non-event F&O stocks on Trial-17 event dates.
- Official NSE India VIX plus lagged NIFTY 50 realized volatility regime controls.
- Two-way clustered OLS covariance by trading date and symbol.
- DTE-matched baseline preserving the observed event DTE distribution.
- ATM IV remains unavailable/not fabricated for historical single-stock option surfaces.

Trial 18 becomes only `ELIGIBLE FOR PREREGISTRATION` when every declared promotion control passes. It never auto-runs and cannot activate live signals.

`ACTIVE_PLAYBOOKS = ()`
