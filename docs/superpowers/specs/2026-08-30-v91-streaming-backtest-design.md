# V9.1 Streaming Backtest Design

## Goal
Make the 180-day, full-F&O V9.1 backtest complete reliably on constrained Railway workers without changing any Bull Institutional Accumulation or frozen Bear Fresh Short Buildup trading logic.

## Architecture
The current resumable path saves one full replay payload per symbol, then reloads all symbol replays into RAM before ranking. The streaming path will instead persist only (1) compact float32 feature frames required for cross-sectional ranks, (2) compact V9.1 candidate events required by the goal-focused report, and (3) compact confirmation counters. Stage 2 will compute one rank feature at a time, attach ranks to compact candidate rows, score them, and atomically persist a ranked-events checkpoint. Stage 3 will read only that compact ranked-events checkpoint and generate the V9.1 report.

## Invariants
- 15-minute signal/execution protocol remains unchanged.
- 180 calendar days, full NSE stock-F&O universe, 0.08% cost and 0.05% per-side slippage remain unchanged.
- Bull Institutional Accumulation thresholds and logic remain unchanged.
- Frozen Bear FSB rule and fingerprint remain unchanged.
- Final 20% lock/reveal behavior remains unchanged.
- Legacy / 4H diagnostics retain their current replay-based path.
- V8/V9 legacy modes remain backward-compatible; streaming is specific to `v91_fast` and `v91_bear_final`.

## Persistence
Per-symbol shards contain compact feature data plus compact V9.1 events, not full replay objects. A Stage-2 `ranked-events.pkl` checkpoint is written atomically only after all rank features and V8 scores are attached. A restart after Stage 2 can therefore skip historical fetching and cross-sectional ranking.

## Progress UX
The research state will expose a resume summary. Examples:
- `170/211 symbols saved`
- `211/211 symbols saved · Stage 2 checkpoint available`
A restarted job resumes from the highest durable checkpoint instead of silently starting from scratch.

## Testing
Tests must prove: compact-event extraction preserves the fields needed for Bull/Bear decisions and returns; V9.1 streaming never calls the all-replay loader; ranked-event checkpoints are reusable after restart; streaming output matches replay output on a deterministic synthetic fixture; and the frozen Bear fingerprint is unchanged.
