# V9.1.1 Resumable Backtest Reliability Fix

- Trading logic, frozen Bear FSB fingerprint, Bull Institutional Accumulation rules, costs and validation thresholds are unchanged.
- Fast 180-day research now checkpoints each completed F&O symbol as an atomic on-disk shard.
- After a Railway/Python worker restart, rerunning the same job resumes from saved symbol batches instead of fetching all 211 symbols again.
- Stage 1 no longer retains all compact feature frames/replays in RAM; each completed symbol is released after checkpointing.
- Cross-sectional V8/V9 ranks are built from disk-backed shards one feature at a time, materially reducing the Stage 2 peak memory footprint.
- Interrupted-state copy now explicitly tells the user that rerunning resumes from saved batches.
- Successful jobs atomically persist the final report before cleaning their temporary resume shards, so same-day manual reruns fetch fresh history.
- Fast-result `symbols_completed` is preserved before replay memory is released.
