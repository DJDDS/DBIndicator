# V9.6.0 — Trial 17 Independent Total-OI Validation

Build: `2026-09-02-INSTITUTIONAL-V9.6.0-TRIAL17-INDEPENDENT-TOTAL-OI`

- Pre-registers Trial 17 after the V9.5.3 feature discovery.
- Frozen event threshold: total share-equivalent FUTSTK OI z >= 1.5. No threshold search or retuning.
- Independent evidence window: 2021-09-01 through 2023-09-01, ending before the V9.5 discovery window.
- Uses official NSE historical total FUTSTK OI, point-in-time contract membership, actual expiry/DTE structure, and lot-normalized/share-equivalent OI.
- Primary 1D pass bar: >=250 events, >=100 event days, lift >=1.10x, day-cluster CI low >1.00x, total-OI-z cluster t-stat >=3.0, top-3-day lift >1.00x, >=3 of 4 positive chronological blocks.
- 2D is diagnostic/secondary and cannot rescue 1D.
- MWPL/ban sensitivity is loaded for the fixed Trial-17 evidence window; missing MWPL blocks PASS but cannot hide an efficacy failure.
- Trial 18 direction research remains locked.
- Trial 15 and all earlier locked finals remain unread and untouched.
- Research/shadow only; `ACTIVE_PLAYBOOKS = ()` remains unchanged.

## Deployment hotfix
- Fixes `/backtest` HTTP 500 introduced by V9.6 worker telemetry calling a non-existent `research_runtime.worker_snapshot()` interface. V9.6 now uses the established `research_runtime.snapshot()` API.
- Adds a regression test that exercises the V9.6 state accessor used by the backtest route so this render-path failure cannot recur silently.
