# DBIndicator Institutional V7 Frozen Release

V7 is a **decision release**, not another parameter-search release. V6 found one subgroup that remained positive as the 15-minute history expanded: Bullish Recent-Range escapes with Catalyst Score >= 60. V7 freezes that exact rule and exposes only its previously locked final 20%.

## Frozen candidate

Rule ID: `RR_LONG_CATALYST60_15M_NEXTBAR_1D`

- NSE stock-F&O universe only
- 15-minute setup and execution
- Bullish Recent-Range escape
- Catalyst Score >= 60
- next executable 15-minute-bar entry
- 1D evaluation horizon
- 180 calendar days
- fixed 0.08% cost + 0.05% slippage per side

The Catalyst Score formula is unchanged from V6 and uses observable participation/shock variables only: gap/ATR, opening RVOL, TOD RVOL, bar-range/ATR shock, and cross-sectional turnover percentile.

## Final acceptance gate

PASS requires all four:

1. N >= 80
2. average net return >= +0.15%
3. profit factor >= 1.20
4. at least 3 of 4 chronological final blocks positive

Otherwise the verdict is REJECT.

## What is deliberately not optimized

No final-test threshold controls are exposed. OI, futures basis, VWAP, 4H context, sector leadership, price location, retention/retest and high turnover are diagnostic/context fields, not extra hard gates for the frozen candidate. Legacy V6 final-test variants remain permanently locked in this build so the user cannot inspect many final answers and choose the prettiest one afterward.

See `FROZEN_RULE.md` for the exact protocol and anti-fishing safeguards.
