# V9.5 Daily OI Evidence Lab — Design

## Goal
Build a research-only daily-bar evidence engine that determines whether point-in-time daily futures OI contains independent 1D/2D movement information after controlling for volatility regime, expiry/roll structure, and data-integrity effects. Do not create or modify production TRADE/WATCH playbooks.

## Decisions

1. **Trial 13 stays closed.** Its final 20% remains permanently unread.
2. **Trial 14 stays failed as preregistered.** It is never rerun after deleting compression.
3. **Trial 15 is the next registered trial.** It validates the standalone daily-OI magnitude effect on chronological history that was not used to discover the 1.22x V9.4 result.
4. **Trial 16 is reserved and locked.** Direction conditional on a validated OI anomaly is not run automatically. It becomes eligible only after Trial 15 passes all evidence gates.
5. **No production activation.** `ACTIVE_PLAYBOOKS = ()` and all current live production gates remain unchanged.

## Trial 15 question
Does a point-in-time daily futures OI shock predict unusually large next-session movement after accounting for the volatility regime and the deterministic monthly-expiry OI cycle?

Primary horizon: 1D. Secondary horizon: 2D and cannot rescue a failed 1D test.

### Primary feature
The engine computes two related features without look-ahead:

- `raw_oi_z`: trailing z-score of daily percentage OI change, retained only as an audit comparator.
- `unexpected_oi_z`: z-score of the residual from an expected-OI-change model fitted on the development period only.

Expected OI change is estimated with ordinary least squares from:
- lagged OI changes (1 and 2 sessions),
- day-of-week indicators,
- days-to-expiry and its square,
- post-2025-09-01 expiry-regime indicator,
- previous-session OI level z-score.

The residual model never uses future observations and validation/final periods use coefficients frozen from development.

### Outcome
Daily True Range / previous completed 14-session ATR is the primary 1D movement outcome. A 2D path-range outcome normalized by the same point-in-time ATR is secondary. The baseline is the same eligible symbol-days from the same period, sampled/aggregated without the OI-anomaly condition.

## Chronological partitions
- Development: first 60% of trading dates.
- Validation: next 20% of trading dates.
- Final: last 20% of trading dates, permanently masked from report output until a separately approved unlock exists. V9.5 itself has no unlock function.

Trial 15 is evaluated on validation only. Development may fit the expected-OI model and choose no thresholds beyond the preregistered constants. Final rows are counted only as locked rows and their outcomes are never aggregated.

## Integrity controls

### Volatility regime
Every event carries previous-session trailing realized volatility (20 sessions) and previous-session ATR%. Trial 15 reports:
- raw anomaly lift,
- within-volatility-quartile lift,
- a day-clustered regression of next-day movement on `unexpected_oi_z` plus realized-vol controls.

If the OI coefficient/effect disappears after controls, the status is `FAIL_VOL_REGIME_CONTROL`.

Historical ATM IV is not invented. If a point-in-time IV dataset is unavailable, the report says `ATM_IV_CONTROL_UNAVAILABLE` rather than treating it as passed.

### F&O ban / MWPL
The engine accepts optional point-in-time ban/MWPL data. It reports normal, high-MWPL/pre-ban, and ban populations separately. If historical ban/MWPL data is unavailable, Trial 15 cannot become `ESTABLISHED`; it remains `INCONCLUSIVE_MISSING_MWPL_CONTROL` even if the raw lift is strong.

### Expiry and roll
Days-to-expiry is mandatory. For historical rows where exact stock-futures expiry calendars are unavailable, the engine derives the applicable monthly Tuesday/Thursday regime and marks the row as `derived_expiry_calendar`. The 2025-09-01 Thursday→Tuesday structural break is explicit.

### Membership and corporate-action/lot-size integrity
The runner accepts optional point-in-time F&O membership and lot-size/corporate-action normalization. Without historical membership, the report exposes survivorship bias and cannot claim institutional-grade establishment. Raw OI is transformed to a relative change/residual feature, but a missing lot-size normalization source is still disclosed.

## Statistical gates
Trial 15 is `PASS_VALIDATION` only if all required data controls are present and:
- validation anomaly events >= 250,
- validation distinct trading days >= 60,
- 1D movement lift > 1.0,
- day-cluster bootstrap 95% lower bound for 1D lift > 1.0,
- cluster-robust OI coefficient t-stat >= 3.0 in magnitude regression,
- effect does not rely on the top 3 trading days (top-3-day-removed lift remains > 1.0),
- majority of preregistered chronological validation blocks have lift > 1.0.

Otherwise it is fail/inconclusive with explicit reasons. 2D can never change a failed 1D result to pass.

## Daily runner
A separate V9.5 runner fetches only daily cash price history and continuous daily futures OI for the current F&O universe, default 1,095 calendar days. It does not invoke V9.4's 15-minute Stage-2 ranking pipeline. Each symbol is transformed and checkpointed independently so memory remains bounded.

The current-universe replay limitation is disclosed until a true historical membership file is supplied.

## UI
Backtest gets a new top card: **V9.5 Daily OI Evidence Lab**. It displays:
- registered Trial 15 specification,
- data-control coverage,
- raw vs unexpected OI evidence,
- volatility-controlled result,
- MWPL/ban result or missing-control warning,
- validation-only status,
- final 20% lock,
- explicit `Trial 16 LOCKED` state.

V9.4 remains visible below as the completed historical audit path.

## Out of scope
- no live trade alerts from Trial 15,
- no automatic Trial 16 execution,
- no option-P&L promotion,
- no fabricated historical IV,
- no claim of historical point-in-time F&O membership unless supplied.
