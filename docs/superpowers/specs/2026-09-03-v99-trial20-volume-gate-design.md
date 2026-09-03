# V9.9 / Trial 20 Abnormal FUTSTK Volume Gate — Design

## Purpose
Replace OI as the magnitude-qualification research feature with a preregistered abnormal total FUTSTK participation/volume feature while preserving OI as dashboard diagnostic context. V9.9 is research/shadow only and cannot activate TRADE/WATCH or unlock Trial 18.

## Frozen hypothesis
Today’s abnormal total FUTSTK turnover adds incremental information about next-session variance beyond HAR daily/weekly/monthly variance state.

## Feature
For each stock-day, aggregate total FUTSTK notional turnover across expiries. Use log1p(turnover). Build a point-in-time abnormal-turnover residual using only information through t: regress log-turnover on its own lagged 20-day and 60-day means, weekday dummies, and a deterministic time trend; standardize the residual by the trailing 60-day residual SD computed from prior residuals only. No threshold is optimized on the independent outcome window.

## Outcomes and controls
Primary target: next-session Yang–Zhang variance. Robustness target: next-session Garman–Klass variance. Descriptive event diagnostics include same-day/same-DTE controls, earnings +/-5-session split, two-way date + symbol clustered regression, chronological stability, and top-day sensitivity.

## Decisive gate
Compare rolling/expanding OOS HAR against HAR + abnormal volume. Evaluate only MSE and QLIKE. Use Clark-West MSPE-adjusted one-sided test for the nested MSE comparison, critical t > 1.645. PASS requires lower OOS MSE and QLIKE for HAR+Volume, Clark-West t > 1.645, positive OOS R2, and robustness/concentration checks not indicating that the result is driven by a few dates/symbols. Otherwise FAIL or INCONCLUSIVE; no retuning/rescue sweep.

## State locks
Trial 19 is closed as association-not-incremental. Trial 18 remains locked. ACTIVE_PLAYBOOKS remains unchanged. OI fields and UI remain present as diagnostics but do not enter Trial-20 magnitude qualification.
