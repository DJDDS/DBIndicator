# V9.9.2 — Trial 20 Log-RV Integrity Closure

Build: `2026-09-03-INSTITUTIONAL-V9.9.2-TRIAL20-LOG-RV-INTEGRITY-CLOSURE`

## V9.9.2 integrity closure
- Keeps Trial 20, the 2015-09-01 through 2018-08-31 OOS window, abnormal FUTSTK turnover feature, refit cadence, MSE/QLIKE, Clark-West hurdle, earnings/DTE/chronological/top-day diagnostics and all state locks frozen.
- Fits HAR and HAR+Volume in `log(realised variance)` space for both Yang-Zhang and Garman-Klass outcomes.
- Back-transforms each OOS forecast with a training-only lognormal smearing factor estimated from that fit's residuals; no future information enters the correction.
- Adds forecast positivity/floor-hit diagnostics so QLIKE pathologies are visible rather than silently clipped.
- Labels the same-day clustered coefficient as `variance×1e6` units; the earlier `335.51` display was numerically scaled for covariance conditioning, not a raw-variance coefficient.
- Interprets a corrected FAIL as `CLOSED_REJECTED_LOG_RV_CONFIRMED`.
- Interprets a corrected statistical PASS as `SPECIFICATION_SENSITIVE_NOT_PROMOTED`; the already-observed Trial-20 window cannot be used to rescue/promote volume.
- Corrects chronological stability display to show positive blocks out of four, with the frozen requirement of at least three positives.
- Trial 18 remains locked, Trial 19 remains closed, OI remains diagnostic, and live Opportunity Radar scoring is unchanged.

## V9.9.1 performance hotfix
- Replaced only Trial-20's final same-day/same-DTE two-way clustered covariance calculation with an algebraically equivalent grouped-score implementation to remove the quadratic date-symbol intersection bottleneck.
- No Trial-20 feature, window, threshold, forecast loss, gate, or production logic changed; the frozen V9.6/V9.8 implementations remain untouched.
