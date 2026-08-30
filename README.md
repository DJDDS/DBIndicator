# DBIndicator — V9.1 Goal-Focused Scanner

**Build:** `2026-08-30-INSTITUTIONAL-V9.1.2-SHUTIL-FIX`

V9.1 narrows the scanner to the two evidence-backed jobs that matter now:

- **Bear Fresh Short Buildup:** frozen exactly as V9 validation qualified it. The Backtest page has a dedicated final-test button that reveals only this rule's untouched final 20%.
- **Bull Institutional Accumulation:** new development/validation-only model based on price up + OI up, VWAP acceptance, abnormal participation, relative leadership and direction-aware futures positioning. Its final 20% remains locked.

The failed V9 Opening Drive, Pullback/Reclaim, Failed Breakout and VWAP Retest Failure models are retired from the primary live shortlist instead of being retuned. Bull Catalyst Continuation remains live/shadow until point-in-time event history exists.

Derivative Intelligence remains downstream: it evaluates CE/PE expression, IV/RV, expected move, liquidity, DTE and Greeks only after the underlying model qualifies a stock.

## Backtest

Open **Backtest**:

1. **Run V9.1 Goal-Focused Backtest** — full NSE F&O, 15-minute, 180 days; validates Bull Institutional Accumulation and keeps all final samples locked.
2. **Run Frozen Bear FSB Final Test** — same fixed protocol, but reveals the final 20% only for the fingerprinted Bear Fresh Short Buildup rule.

The final test has no threshold controls.

## V9.1.2 streaming reliability

The 180-day V9.1 path is now constant-memory relative to the full replay universe: each stock is checkpointed as compact rank inputs plus only the Bull Accumulation / frozen Bear FSB candidate rows needed by the goal-focused report. Stage 2 persists a ranked-events checkpoint, so a worker restart after cross-sectional ranking resumes directly at validation instead of rebuilding history or ranks.

During a resumed run the Backtest page shows the durable checkpoint state (for example, `170/211 symbols saved` or `211/211 symbols saved · Stage 2 checkpoint available`).
