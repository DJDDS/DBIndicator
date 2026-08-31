# V9.2 Diagnostic / Audit Lock

**Build:** `2026-08-31-INSTITUTIONAL-V9.2.1-STAGE3-NOCOPY`

## Rejected Bear rule — audit only

`BEAR_FSB_15M_NEXTBAR_1D_V91`

Fingerprint remains unchanged for audit continuity. The rule used the full NSE stock-F&O universe, 15-minute setup/execution, 180 calendar days, 0.08% costs and 0.05% slippage per side, with:

- fresh bearish breakout;
- futures state = **Fresh Short Buildup** (price down + OI up);
- breakout extension <= 1.25 ATR;
- Participation >= 70;
- Relative Weakness >= 60;
- direction-aware Derivatives >= 65;
- bearish close-location >= 65;
- futures-basis acceleration <= +0.02 when available;
- median evidence score >= 70.

Its untouched final 20% was consumed and rejected. V9.2 must not alter this rule or create a replacement rule from final-sample cohorts. The final-test button is disabled.

## Bull diagnostic rule

**Bull Institutional Accumulation** remains research-only and its final 20% stays locked. V9.2 does not lower thresholds. It broadens only the diagnostic population to every point-in-time `price up + OI up` seed so the gate funnel can identify exactly where candidates disappear.
