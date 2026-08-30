# V8.2.2 Stage-3 Fast Validation Patch

Build: `2026-08-30-INSTITUTIONAL-V8.2.2-STAGE3-FAST`

## Why
The V8 fast backtest still spent unnecessary CPU time in Stage 3 because it:
- rescanned and regrouped the same event universe separately for Top-1, Top-3, Top-5 and operational Top-3;
- computed audit-only V8 ablation tables on the primary fast path;
- repeated chronological sorting/statistics across those audit variants.

## Fix
- Added one-pass `select_top_k_breadths()` selection for K=1/3/5.
- Primary fast path now calls `v8_dual_report_fast()`.
- Bull and Bear event sets are filtered/grouped/sorted once per side.
- Operational Top-3 reuses the already-built Top-3 set.
- V8 legacy ablations remain available only in the Legacy / 4H Diagnostic path.
- Trading logic, score floors, participation floor, Top-K breadth, costs and final-lock policy are unchanged.
