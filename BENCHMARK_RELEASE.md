# DBIndicator Benchmark Release

This build focuses on NSE stock F&O only and separates two decision horizons:

- Intraday: 15-minute stock-in-play / compression breakout with 30m, 1h, 2h, 4h and EOD research outcomes.
- Swing: the same 15-minute execution signal with retention/context checks and 1D / 2D research outcomes.

Core live sequence:

1. Stock in Play / Energy Building
2. Actual opening-range, recent-range or compression breakout/breakdown
3. Time-of-day participation and VWAP acceptance
4. Futures OI 30m/60m sponsorship and acceleration when available
5. Anti-chase entry quality
6. Intraday Best Entry or Swing 1-2D Candidate classification

Research discipline:

- 30% chronological holdout
- profit factor and net expectancy after costs
- MFE / MAE quality
- chronological block stability
- component interactions tested without brute-force parameter grids
- Benchmark / Promising / Research promotion status

Order-book depth imbalance is shadow/forward-research only and does not control Best Entries until it earns the research benchmark.
