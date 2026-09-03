# V9.9.0 — Trial 20 OOS Volume Gate

Build: `2026-09-03-INSTITUTIONAL-V9.9.0-TRIAL20-OOS-VOLUME-GATE`

## Research decision

- Trial 19 is closed as **association, not incremental**: V9.8 showed the OI magnitude relationship disappears after HAR + abnormal volume.
- Trial 18 remains **LOCKED**.
- OI remains available as descriptive/diagnostic market intelligence, but it is not a Trial-20 magnitude eligibility gate.
- No live TRADE/WATCH playbook is activated and the Opportunity Radar logic is unchanged.

## Trial 20 preregistration

- Candidate feature: total FUTSTK notional turnover.
- Frozen transformation: `log(turnover)`, point-in-time detrending using prior 20/60-session turnover means + weekday dummies + deterministic trend, then standardisation by prior trailing 60-session residual SD.
- Independent outcome window: 2015-09-01 through 2018-08-31; earlier data are used only for warm-up/training.
- Benchmark: HAR daily + weekly + monthly realised-variance terms.
- Challenger: HAR + abnormal FUTSTK volume.
- Primary target: next-session Yang–Zhang variance.
- Robustness target: next-session Garman–Klass variance.
- OOS loss functions: MSE and QLIKE only.
- Nested-model test: Clark–West MSPE-adjusted, one-sided, hurdle `t > 1.645`.
- Additional controls: same-day/same-DTE diagnostic, two-way date + symbol clustering, earnings ±5-session split, four chronological blocks, top-3 favourable-day sensitivity and concentration diagnostics.
- No volume threshold optimization or post-outcome retuning.

## Data integrity

- Historical FUTSTK archive parsing now retains rupee notional turnover and aggregates it across contracts by symbol/date.
- V9.9 uses official NSE historical futures and cash archives and fails closed when archive coverage or the earnings join is insufficient.
