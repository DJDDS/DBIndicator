# V9.1 Streaming Backtest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Replace V9.1's replay-heavy 180-day research aggregation with a restart-safe compact streaming path that preserves identical trading logic and final-test semantics.

**Architecture:** V9.1 symbol shards will store compact feature frames and compact candidate events instead of whole replay dictionaries. Stage 2 will rank those compact rows one feature at a time and atomically persist a ranked-events checkpoint; Stage 3 will aggregate directly from that checkpoint. Legacy modes keep the existing replay path.

**Tech Stack:** Python 3.13, pandas, numpy, pickle, Flask, pytest.

**Spec:** `docs/superpowers/specs/2026-08-30-v91-streaming-backtest-design.md`

## Global Constraints
- Do not change any V9.1 Bull Institutional Accumulation threshold or rule.
- Do not change the frozen Bear FSB rule or fingerprint.
- Keep the protocol fixed to 15minute / 180 days / full NSE stock-F&O for V9.1 modes.
- Keep the final 20% lock/reveal semantics unchanged.
- Preserve legacy and 4H diagnostic behavior.

---

### Task 1: Compact V9.1 Symbol Shards

**Files:**
- Modify: `app/backtest.py`
- Test: `tests/test_v91_streaming.py`

**Interfaces:**
- Consumes: one symbol replay returned by `early_research.replay_feature_frame`.
- Produces: `_compact_v91_events(replay) -> list[dict]`, `_v91_confirmation_summary(replay) -> dict`, and V9.1 shard payload fields `v91_events` / `v91_confirmation`.

- [x] **Step 1: Write failing tests** that assert compact extraction retains signal/entry times, direction/price/OI/basis/VWAP/rank inputs, intraday and swing returns, while the V9.1 shard contains no full `replay` object.
- [x] **Step 2: Run** `python -m pytest tests/test_v91_streaming.py -q` and verify failure.
- [x] **Step 3: Implement compact extraction and V9.1 shard schema** in `app/backtest.py`; preserve the old shard schema for non-V9.1 fast modes.
- [x] **Step 4: Re-run the focused tests** and verify pass.
- [x] **Step 5: Commit** `feat: store compact v91 research shards`.

### Task 2: Streaming Cross-Sectional Ranking + Stage-2 Checkpoint

**Files:**
- Modify: `app/backtest.py`
- Test: `tests/test_v91_streaming.py`

**Interfaces:**
- Consumes: completed V9.1 symbol shards.
- Produces: `_build_v91_ranked_events_checkpoint(run_dir, shard_map, stage_cb=None) -> Path` and `_load_v91_ranked_events_checkpoint(path) -> dict`.

- [x] **Step 1: Write failing tests** for rank parity against `_attach_v8_full_universe_scores_from_shards` on a deterministic two-symbol fixture and for reusing an existing ranked-events checkpoint without rebuilding ranks.
- [x] **Step 2: Run the focused tests** and verify failure.
- [x] **Step 3: Implement one-feature-at-a-time ranking** using compact float32 frames, attach percentile ranks to only the compact V9.1 events, calculate Recent-Range breakout ranks, call `v8_dual.score_preranked_row`, and atomically write `ranked-events.pkl`.
- [x] **Step 4: Re-run the focused tests** and verify pass.
- [x] **Step 5: Commit** `feat: add restart-safe v91 ranked event checkpoint`.

### Task 3: V9.1 Streaming Runner and Resume Status

**Files:**
- Modify: `app/backtest.py`
- Modify: `app/early_research.py`
- Modify: `app/templates/backtest.html`
- Test: `tests/test_v91_streaming.py`
- Test: `tests/test_v9_reliability.py`

**Interfaces:**
- Consumes: ranked-events checkpoint + merged confirmation summary.
- Produces: `early_research.aggregate_v91_compact_events(...) -> dict`, V9.1 result payload matching the existing `research.v91_goal` contract, and resume-status text.

- [x] **Step 1: Write failing tests** proving `v91_fast` and `v91_bear_final` never call `_load_research_replays_from_shards`, Stage 3 aggregates directly from compact events, Stage-2 checkpoints survive restart, and the UI/status exposes the durable resume state.
- [x] **Step 2: Run focused tests** and verify failure.
- [x] **Step 3: Implement the streaming V9.1 branch** in `run_early_movement_research`, add `aggregate_v91_compact_events`, and expose resume summary in research progress/error UI.
- [x] **Step 4: Run focused tests** and verify pass.
- [x] **Step 5: Run `python -m pytest -q`** and verify the full suite passes.
- [x] **Step 6: Run Python compile and rendered-JS syntax checks.**
- [x] **Step 7: Commit** `fix: stream v91 backtest across railway restarts`.

### Task 4: Build Markers, Documentation, Package Verification

**Files:**
- Modify: `app/v91_goal.py`
- Modify: `app/v9_playbooks.py`
- Modify: `RESEARCH_BUILD.txt`
- Modify: `PRODUCTION_BUILD.txt`
- Create: `V9_1_2_CHANGELOG.md`
- Modify: `README.md`

**Interfaces:**
- Produces deployable ZIP `DBIndicator-institutional-v9.1.2-streaming-backtest.zip`.

- [x] **Step 1: Update build marker** to `2026-08-30-INSTITUTIONAL-V9.1.2-STREAMING-BACKTEST` without changing the frozen Bear rule body.
- [x] **Step 2: Add changelog/README notes** describing compact event shards, Stage-2 checkpointing and Railway persistent-volume recommendations.
- [x] **Step 3: Re-run `python -m pytest -q` and compile checks.**
- [x] **Step 4: Package the exact branch tree**, extract to a fresh directory, rerun the full suite and ZIP integrity check.
- [x] **Step 5: Verify the Bear FSB fingerprint remains `b98d2885117c`.**
- [x] **Step 6: Commit** `release: v9.1.2 streaming backtest`.
