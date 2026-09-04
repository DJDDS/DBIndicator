# V10.0 Directional Edge Laboratory

Build: `2026-09-03-INSTITUTIONAL-V10.0.1-DIRECTIONAL-EDGE-LAB-PROGRESS-HOTFIX`

## Why V10 exists
V9.9.2 cleanly closed Trial 20: abnormal FUTSTK volume did not provide stable, broad incremental OOS magnitude forecasting value beyond the HAR benchmark. V10 therefore stops searching the OI/volume magnitude family and begins a new directional research programme.

## Trial 21 — Hierarchical Residual Strength
- Rolling trailing-60-session regression: stock return on NIFTY return + mapped NSE sector-index return.
- Betas are fit strictly on prior observations; current-day residual is scored only after close t.
- Five-session cumulative residual strength, point-in-time cross-sectional residual percentile, unique-sector strength percentile, and 20-session absolute trend.
- Frozen Bull rule: residual percentile >=90, sector percentile >=70, 20-session return >0.
- Frozen Bear rule: residual percentile <=10, sector percentile <=30, 20-session return <0.
- Missing sector mapping/history fails closed for Trial 21.

## Trial 22 — Carry-Normalized Futures Basis Innovation
- Official NSE FUTSTK archive now retains near/next settlement and expiry point-in-time.
- Near annualized log basis and next-vs-near curve slope are computed with actual DTE.
- Basis expectation is estimated from prior history only; innovation is standardized by prior residual volatility.
- Frozen Bull rule: basis innovation z >=1.5 and curve slope >=0.
- Frozen Bear rule: basis innovation z <=-1.5 and curve slope <=0.
- OI and volume are explicitly not eligibility gates.

## Validation
- Fixed evidence range 2018-09-01 through 2026-08-31 with 60/20/20 chronological split.
- Final 20% remains unread in V10.0.
- Signal close t; entry next session open; primary exit next session close; 2D secondary cannot rescue 1D.
- 0.18% round-trip cost charged from the first research run.
- Promotion bars per side: >=250 events, >=120 event days, positive net expectancy, day-cluster t >=3.0, PF >=1.25, >=3/4 positive validation blocks, positive result after top-3 favorable days removed, top-5 positive-P&L symbol share <=40%, top-3 positive-P&L sector share <=65% when sector data exist.
- Bull and Bear pass/fail independently.

## Research locks
- Trial 23: `LOCKED_PENDING_TRIAL21_AND_22`; no combined V10 score is evaluated.
- Trial 18 remains LOCKED.
- Trial 19 remains CLOSED — association, not incremental.
- Trial 20 remains CLOSED — rejected after log-RV integrity closure.
- `ACTIVE_PLAYBOOKS = ()` unchanged.
- Live Opportunity Radar scoring unchanged; V10 is research/shadow only.


## V10.0.1 progress hotfix
- No research feature, threshold, cost, evidence window, split, gate, or Trial-23 lock changed.
- Wired the existing NSE futures/cash archive date-level callbacks into V10 state.
- Stage 1 now reports `FUTSTK archive done/total · YYYY-MM-DD`; Stage 2 reports cash archive progress.
- Stage 3 remains symbol-level Trial-21/22 feature construction.
