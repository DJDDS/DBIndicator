# V11.1 — Development & Feasibility Lab

Research build: `2026-09-04-INSTITUTIONAL-V11.1-DEVELOPMENT-FEASIBILITY-LAB`

V11.1 is **not Trial 25**. It converts the already-read Trial-24 pre-final period (2010-01 through 2023-05) into development-only data for future momentum research while keeping the final 31 Trial-24 months unread.

## Locked research contract

- Exactly two candidates: residual 12-1 momentum and conventional liquid 12-1 price momentum.
- Same point-in-time FUTSTK membership, monthly top/bottom deciles, equal-weight long/short construction and volatility de-risking rule.
- Primary exposure rule is `min(1.0, target_vol / lagged_12m_portfolio_vol)`; no leverage above 1x and no volatility-target sweep.
- Residual-momentum reference target is frozen at 12.49% annual volatility; conventional price momentum uses the externally anchored 19% annual target. Both use the same de-risk-only rule and there is no volatility-target sweep.
- Measured portfolio weight-change turnover drives the headline implementation cost; 0.36%/month remains a separate stress scenario.
- FUTSTK execution coverage is audited using nearest non-expired stock futures at signal month-end, settle if positive else close, monthly reselection, official/inferred NSE lot size, and fail-closed missing-contract handling. V11.1 does not fabricate futures P&L or relabel cash P&L as futures P&L.
- The old `effect >= MDE` gate is retained only for historical reproducibility. New registration uses Feasibility Gate V2 with an explicit minimum 80% prospective power requirement and target-market development volatility when available.
- Any conjunctive hard battery is powered jointly by Monte Carlo on the same simulated path; marginal powers are diagnostics and are never multiplied as if independent.
- Top-3 removal, 4-block signs, skew, kurtosis, drawdown and CVaR are diagnostics, not duplicate hard rejection gates.
- A development winner is only `ELIGIBLE_FOR_TRIAL_25`; V11.1 never runs or registers Trial 25 automatically.
- `final_read = false` and `production_activation = false` are enforced in both pure research output and durable runner state.

## Trial 24 permanent record note

The registered Trial-24 verdict remains `FAIL_REPLICATION_PRE_FINAL`. V11.1 records, without rereading anything, the already-observed 0.188%/month stress-net mean, implied 0.548%/month gross mean, approximately 24.3% annualized realized volatility, and 12.49% external source-market volatility. The note states that source-market volatility did not transport to the Indian FUTSTK implementation; it does not rescue or alter the preregistered verdict.

## Release discipline

The deployment package must exclude `.dbindicator-research`, pytest caches, Python bytecode, local secrets, and other runtime state. Historical Trial-24 UI is read-only in V11.1.
