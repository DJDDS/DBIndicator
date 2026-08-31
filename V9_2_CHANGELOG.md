# V9.2 Diagnostic Reset

Build: `2026-08-30-INSTITUTIONAL-V9.2-DIAGNOSTIC-RESET`

- Adds a cumulative Bull Institutional Accumulation gate funnel beginning at the broad point-in-time `price up + OI up` seed.
- Adds diagnostic-only Bear FSB validation-vs-consumed-final decomposition by market regime, index trend, market volatility, basis direction, sector-relative state, signal time, OI magnitude, OI persistence and post-signal 60-minute positioning.
- Explicitly reports that point-in-time breadth history is unavailable rather than fabricating a breadth diagnostic.
- Keeps the Bull final 20% locked.
- Marks Bear FSB as rejected after its consumed final test and disables its final-test button in the UI.
- Does not alter the rejected Bear rule, Bull thresholds, costs, execution timing, 180-day protocol or derivative-intelligence layer.
