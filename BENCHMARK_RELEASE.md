# DBIndicator Institutional V6 Release

V6 is built for **NSE stock F&O only**, with two objectives: early intraday entries and 1–2 trading-day swing continuation. It replaces OI-heavy eligibility with an evidence stack and deliberately keeps the final 20% research sample locked while tuning.

## Live hierarchy

1. **Stock in Play / Energy Building** — abnormal participation or range energy; no forced direction.
2. **Recent-Range Setup** — price reveals direction by escaping a recent decision range.
3. **Sponsored Recent-Range** — TOD volume plus either OI confirmation or expanding futures basis; a strong catalyst proxy can substitute when sponsorship data is incomplete.
4. **V6 Intraday Entry** — Recent-Range + Stock-in-Play/turnover + leadership/location + sponsorship + anti-chase, with a bounded 5-minute execution check for top finalists.
5. **V6 Swing 1–2D** — long-only until the short model independently clears benchmark; requires retention/retest, 4H context and non-opposing sector context.

## V6 evidence axes

- cross-sectional turnover percentile across the current F&O universe
- catalyst proxy: gap/ATR, opening RVOL, TOD RVOL, range shock and turnover rank
- sector rank and stock-vs-sector leadership
- 20/50-session price location
- futures basis and ~30-minute basis acceleration
- OI as a soft sponsorship input, not a universal hard gate
- 5-minute execution quality only for a bounded finalist set
- order-book depth remains shadow/forward-test only

## Research discipline

- chronological **60% development / 20% validation / 20% locked final test**
- final test is hidden unless `V6_UNLOCK_FINAL_TEST=true`
- no look-ahead; next-executable-bar entries
- historical cross-sectional turnover ranks and sector ranks
- partial futures-basis coverage is reported honestly around rolls
- long and short models are evaluated separately
- promotion requires positive validation expectancy after costs, PF >= 1.25, adequate sample, MFE/MAE quality and chronological stability

## Exit research

V6 includes a conservative first-touch target/stop grid plus breakeven variants. If target and stop are both inside the same OHLC bar, the **stop wins**. This directly tests whether the prior fixed-horizon exits were giving favorable movement back.
