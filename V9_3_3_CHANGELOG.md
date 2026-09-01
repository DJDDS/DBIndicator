# V9.3.3 — V9.3 Input Progress / Heartbeat

Build: `2026-09-01-INSTITUTIONAL-V9.3.3-V93-INPUT-PROGRESS`

- Fixed V9.3 Stage 1 appearing frozen at `0/210`: the daily continuous-OI sweep now reports cached count, current symbol, and completed-symbol count throughout the 210-symbol serial Kite sweep.
- Added a pre-request heartbeat, so a slow Kite call shows the exact symbol being requested instead of looking dead.
- V9.3 Stage 1 owns 1–8% of the progress bar; ordinary 15-minute history then advances 8–70%, preventing the progress bar from moving backwards after the OI baseline completes.
- Progress state updates in memory on every heartbeat but is persisted only every five completed symbols, avoiding the synchronous checkpoint-I/O problem fixed earlier.
- No research thresholds, Trial 13 rules, evidence gates, costs, final locks, 1D/2D logic, V9.2 isolation, or 4H logic changed.
