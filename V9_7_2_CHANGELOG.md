# V9.7.2 — Trial 19 Confound & Integrity Closure

- Preserves frozen Trial 19 unchanged.
- Adds look-ahead-safe prior 5-day realised-volatility quintile matching.
- Adds t-1/t-2 pre-signal movement diagnostics.
- Runs official NSE earnings/board-meeting ±5-session control once frozen efficacy passes, even if MWPL is unresolved.
- Uses monthly NSE MWPL masters + reconstructed daily total FUTSTK OI utilisation + targeted secban checks.
- Adds 2021-2023 MWPL/ban overlap and lift-delta bounding fallback.
- Carries ~1.13x replicated planning effect; retires 1.22x discovery estimate for projections.
- Trial 18 remains locked unless all promotion controls pass; no auto-run or production activation.
- `ACTIVE_PLAYBOOKS = ()`.

### Runtime hotfix — earnings DatetimeIndex iteration
- Fixed `evaluate_earnings_promotion()` rejecting non-empty `pd.DatetimeIndex` earnings calendars via ambiguous boolean coercion (`dates or []`).
- Iteration now uses an explicit `None` check; Trial 19 research math, thresholds, evidence window, MWPL/volatility/earnings controls, and Trial 18 lock are unchanged.
- Added an exact regression test using a non-empty earnings `DatetimeIndex`.
