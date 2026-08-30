# V9.1.2 Streaming Backtest Reliability Upgrade

Build: `2026-08-30-INSTITUTIONAL-V9.1.2-STREAMING-BACKTEST`

- Trading logic is unchanged: Bull Institutional Accumulation and the frozen Bear Fresh Short Buildup rule retain the exact V9.1 thresholds, costs, chronology and final-lock behavior.
- V9.1 symbol checkpoints now store only compact float32 rank inputs, compact goal-focused candidate rows and confirmation counters. Full replay dictionaries are no longer persisted for V9.1 modes.
- Rows that cannot possibly qualify for either active V9.1 model are discarded before checkpointing, without changing any rule outcome.
- Stage 2 builds cross-sectional ranks one feature at a time and writes an atomic `ranked-events.pkl` checkpoint.
- A restart after Stage 2 resumes directly from the ranked-events checkpoint without re-fetching price history or rebuilding ranks.
- Legacy V9.1.1 symbol shards remain same-day compatible: one old replay is compacted at a time rather than loading all replays together.
- The UI displays durable resume information such as `170/211 symbols saved` and `Stage 2 checkpoint available`.
- Legacy / 4H Diagnostic retains the replay-heavy audit path; the streaming path is limited to V9.1 goal-focused and frozen-Bear-final modes.
- For complete Railway container replacement survival, configure `EARLY_RESEARCH_STATE_PATH` and `EARLY_RESEARCH_WORK_ROOT` on a mounted persistent volume.
