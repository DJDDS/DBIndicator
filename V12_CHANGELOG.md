# V12.1 — Index Volatility Recorder & Feasibility Lab

Research build: `2026-09-06-INSTITUTIONAL-V12.1-INDEX-VOLATILITY-RECORDER-FEASIBILITY-LAB`

V12.0 realigns the live product to the stated objective: Indian F&O stocks, intraday to 1–2 day opportunities, with derivative expression only when executable.

## Locked contract

- The live trade console uses `OBSERVE -> WATCH -> SETUP -> EXECUTABLE`, and every state remains **NOT VALIDATED**.
- Four unconditional forward option snapshots are scheduled at 09:30, 13:00, 15:10 and 15:37 IST with a seven-minute grace window and no later backfill.
- Broad ATM liquidity is measured across the live OPTSTK universe; deeper near/next strike ladders are recorded only for the most liquid/event-relevant names.
- Point-in-time NSE earnings-board-meeting observations are append-only; revisions are preserved and missing calendars fail closed.
- Option feasibility is measured from executable bid/ask, depth and recorded spread. Midpoint prices are descriptive only.
- **Trial 25 LOCKED** — no efficacy direction or threshold is registered in V12.0.
- The final 31 Trial-24 months unread remain untouched.
- No Trial-25 runner exists in this build and production activation remains NO.

## V12.0.1 persistence + recorder-health repair
- Railway Volume-aware V12 runtime storage via `RAILWAY_VOLUME_MOUNT_PATH`; defaults to `<mount>/v12`.
- Explicit `EPHEMERAL WARNING` when no persistent Volume backs the V12 files.
- Recorder health proves successful JSONL writes with last-write timestamp, file existence/size, slot record count, option-contract count, two-sided ATM straddle count, quote errors and write errors.
- Dashboard exposes each fixed slot (09:30, 13:00, 15:10, 15:37) and a protected `/api/v12-recorder-health` endpoint.
- Strategy logic, feasibility thresholds, Trial-25 lock and all prior holdouts are unchanged.


## V12.1 — Index Volatility Recorder & Feasibility Lab
- Adds a separate NIFTY near-expiry WebSocket research recorder; the V12.0.1 stock-option recorder remains unchanged.
- India VIX is an input / regime feature, not an assumed 22–32% premium edge.
- Adds 5-second top-of-book and 60-second full-depth index-option research persistence plus optional S3-compatible off-box backup.
- Adds a development-only remaining-session realised-variance lab; it does not inherit a validated label from prior HAR work.
- Trial 25 LOCKED. No V12.1 production option-volatility activation.
