# V9 Reliability / Memory Patch

This patch changes **research-job reliability only**. It does not alter V9 playbook rules, thresholds, costs, horizons, Bull/Bear logic, or Derivative Intelligence.

## Memory controls
- V9 cross-sectional feature history keeps only the eight required columns and stores them as `float32`.
- The fast path no longer retains the legacy turnover-history matrix.
- Fresh-breakout events are shared by reference between ignition and V9 playbook families instead of copying the full event dictionary.
- Duplicate event references are deduplicated before cross-sectional scoring.
- Full-universe feature/index history is explicitly released before Stage 3.
- Replay collections are released after report aggregation.
- Periodic garbage collection limits retained pandas objects during 211-stock history replay.

## Restart-safe job state
- Research status/progress is atomically checkpointed to disk.
- A completed report is checkpointed together with `status=done` and can be restored after a normal process restart when the checkpoint path survives the restart.
- A checkpoint left in `running` state is surfaced after restart as an explicit `error`: `Research job interrupted by server restart before completion. Run it again.`
- Set `EARLY_RESEARCH_STATE_PATH` to a Railway-mounted persistent volume path if recovery must survive container replacement/redeploys. The default is `/tmp/dbindicator-early-research-state.json`.

## Verification
- Reliability regression tests cover completed-result round trip, interrupted-job recovery, float32 compaction, and fast-path event de-duplication.
