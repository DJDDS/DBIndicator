# V12.0 — Option Edge Recorder & Trade Opportunity Console

Research build: `2026-09-05-INSTITUTIONAL-V12.0.1-PERSISTENT-OPTION-RECORDER-HEALTH`

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
