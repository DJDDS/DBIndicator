# DBIndicator — V9.7 Trial 19 Nonlinear Extreme-OI Validation

**Build:** `2026-09-02-INSTITUTIONAL-V9.7.0-TRIAL19-NONLINEAR-EXTREME-OI`

V9.7 preserves all prior audit history and tests a new preregistered hypothesis without retuning Trial 17. The event is frozen at total share-equivalent FUTSTK OI z >= 1.5 and is evaluated only on 2018-09-01 through 2021-08-31.

The primary comparison is event stocks versus eligible non-event F&O stocks on the same trading day and same DTE bucket. Inference uses the binary `extreme_oi_event` coefficient with two-way clustering by date and symbol. Historical F&O membership, official NSE historical cash, OI normalization and MWPL/ban are mandatory. Earnings +/-5 sessions is a promotion-only control after a Trial-19 pass.

Trial 18 remains locked and never auto-runs. `ACTIVE_PLAYBOOKS = ()` remains unchanged. Prior finals stay unread/locked. V9.6.2 remains visible on Backtest as the closed predecessor.

Open **Backtest → Run V9.7 Trial 19**.
