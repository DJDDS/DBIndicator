# V9.7.0 — Trial 19 Nonlinear Extreme-OI Validation

Build: `2026-09-02-INSTITUTIONAL-V9.7.0-TRIAL19-NONLINEAR-EXTREME-OI`

- Closes Trial 17 under its frozen continuous-z t-stat rule; does not retune or reopen it.
- Pre-registers Trial 19 on a third untouched NSE window: 2018-09-01 through 2021-08-31.
- Freezes the event at total share-equivalent FUTSTK OI z >= 1.5.
- Primary baseline is non-event F&O stocks on the same trading day and same DTE bucket.
- Primary inference is a binary `extreme_oi_event` coefficient with date + symbol two-way clustered errors.
- Pass gates: >=250 events, >=250 event days, matched 1D lift >=1.10x, day-cluster CI lower bound >1.00x, event t>=3.0, top-3-day matched lift >1.00x and >=3/4 positive chronological matched blocks.
- Historical membership, official NSE cash history, OI normalization and MWPL/ban are mandatory.
- Earnings +/-5 sessions is a post-pass promotion control; Trial 18 never auto-runs.
- Trial 18 remains locked unless Trial 19 and the earnings promotion control both pass.
- `ACTIVE_PLAYBOOKS = ()` remains unchanged; no TRADE/WATCH activation.
