# V9.3.5 — Memory-Safe Stage 2

Build: `2026-09-01-INSTITUTIONAL-V9.3.5-MEMORY-SAFE-STAGE2`

## Why

V9.3.4 could finish and persist all 210 Stage-1 symbol shards, then lose the Railway worker as Stage 2 loaded every compact feature frame into RAM at once. The restart pattern occurred after `210/210` with durable checkpoints intact.

## Changes

- Preserves the V9.3.4 resume schema so the already-saved 210/210 symbol shards can be reused on the same research day.
- Stage 2 now deserializes each heavy Stage-1 shard only once and writes a lean rank-only feature checkpoint per symbol.
- The lean-input preparation boundary is itself checkpointed. A restart after that point resumes without reopening heavy Stage-1 payloads.
- Cross-sectional ranking is performed one rank at a time from lean shards; the full universe of compact frames is never held in RAM simultaneously.
- Every completed rank remains checkpointed and resumable.
- Progress now identifies memory-safe input preparation and per-rank streamed loading.
- No strategy thresholds, Trial 13 rules, OI Screener calculations, production eligibility, 0.18% friction, or 1.25 ATR chase guard changed.

## Verification target

The 210-symbol × 5,000-bar Stage-2 stress test must remain below the CI time budget and materially below the previous all-frames peak memory design.
