# V11.0 — Feasibility Competition + Trial 24 Preregistration

Research build: `2026-09-04-INSTITUTIONAL-V11.0-FEASIBILITY-TRIAL24-PREREGISTRATION`

- No outcome data are used to select the next trial.
- Candidate A is the exact published-style residual-momentum family: 36-month India FF3 regression, 12-1M residual formation, monthly top-minus-bottom deciles, one-month hold.
- The pinned published prior is 11.20% annual return / 12.49% annual volatility. The 200%-gross long-short spread is stress-costed at 0.36% round trip.
- Exact published replication uses a named one-sided confirmatory t-bar of 1.645; no discovery threshold search is permitted.
- Candidate B, fixed-count cross-sectional basis, is blocked with `DO_NOT_RUN_PRIOR_EFFECT_REQUIRED` because no independent 10–21 day effect magnitude is registered.
- Trial 24 is runnable only because Candidate A clears the binding cost/MDE feasibility gate before outcomes are read.
- Trial 24 uses official NSE point-in-time FUTSTK month-end membership, NSE cash month-end prices, pinned IIM Ahmedabad survivorship-adjusted monthly factors and NSE corporate actions. Unsupported corporate actions fail closed for the affected symbol-month.
- The production loader stops at the frozen pre-final outcome month. The final 20% is not fetched/evaluated by the Trial-24 runner.
- A completed Trial 24 cannot be rerun. If an error occurs after the alpha-read stage begins, automatic reread is refused.
- No live Opportunity Radar, production playbook, OI logic, or V10.2.2 live-reliability behavior is changed.
