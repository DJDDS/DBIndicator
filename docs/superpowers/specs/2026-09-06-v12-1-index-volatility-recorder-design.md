# V12.1 Index Volatility Recorder & Feasibility Lab — Design

## Objective

Extend V12.0.1 so the system can research liquid NIFTY near-expiry volatility risk transfer for intraday / 1–2 day options without changing the existing stock-option recorder, live trade console, historical research verdicts, or Trial-25 lock.

## Non-negotiable research constraints

- V12.1 is development / feasibility only. It does not create an option-selling production signal and cannot unlock Trial 25.
- The existing V12.0.1 stock-option recorder and earnings lane stay intact and continue to collect data.
- No naked short-option production recommendation is allowed. Any future short-volatility candidate must be defined-risk and sized from a declared loss budget rather than broker margin capacity.
- India VIX is a feature / regime input, not an assumed 22–32% option edge. Premium richness must ultimately be measured from the actual near-expiry option chain.
- The remaining-session volatility model is a new target and must not inherit a “validated” label from prior HAR work.
- Recorder failures, backup failures, or development-lab failures must never interrupt the existing stock scanner or dashboard.

## Architecture

### 1. NIFTY index-option WebSocket recorder

Create a separate daemon service around KiteTicker. It starts only after the day's Kite access token exists and only during market hours. It subscribes to:

- NIFTY 50 index reference,
- INDIA VIX,
- nearest-expiry NIFTY future,
- nearest-expiry NIFTY options around ATM, default ATM ±12 strike steps, both CE and PE.

All option tokens use WebSocket FULL mode so five-level depth, OI, volume and timestamps are available. The service automatically rebuilds the subscription when the ATM strike or nearest expiry changes materially.

### 2. Two-tier persistence

To control Railway storage while preserving execution evidence:

- **Micro snapshots every 5 seconds:** top-of-book bid/ask, top quantities, LTP, volume, OI, strike/type/expiry, index spot, NIFTY future, India VIX and timestamp.
- **Depth snapshots every 60 seconds:** the same active ladder with full five-level bid/ask depth.

Files are date-partitioned below the existing V12 persistent root:

`<V12_STORAGE_ROOT>/index_vol/YYYY-MM-DD_micro.jsonl`

`<V12_STORAGE_ROOT>/index_vol/YYYY-MM-DD_depth.jsonl`

State lives at:

`<V12_STORAGE_ROOT>/v121_index_vol_state.json`

The current day remains appendable JSONL. Completed days are gzip-compressed after market close.

### 3. Off-box backup

Add optional S3-compatible backup using boto3. When bucket credentials/configuration are present, completed `.jsonl.gz` files are uploaded under a configurable prefix. Backup failure is visible and retried but never stops recording. If backup is not configured, the dashboard must say `OFF-BOX BACKUP NOT CONFIGURED` rather than implying that Railway Volume is a backup.

### 4. Historical remaining-session RV development lab

Create a pure research module that consumes historical 5-minute NIFTY bars and prior-day India VIX data. It builds one fixed development target:

- feature window: 09:15–10:30 IST,
- target window: 10:30–15:10 IST (pre-CAS),
- target: sum of squared 5-minute log returns in the target window,
- features: morning realized variance and prior-day VIX converted to daily variance,
- model: log-linear OLS with intercept,
- evaluation: expanding/rolling out-of-sample MSE and QLIKE against simple baselines.

The same module separately produces a time-of-day variance-share U-curve diagnostic. It must not optimize entry/exit times or create a trade rule.

### 5. Index-volatility health/readiness UI

Add a dashboard panel and API that expose:

- recorder status: WAITING / CONNECTING / RECORDING / ERROR / MARKET_CLOSED,
- persistent-storage status,
- WebSocket connection/reconnect count,
- active expiry and ATM strike,
- option/reference token count,
- last tick time,
- last micro write and depth write,
- current-day micro/depth row counts and file sizes,
- backup status and last successful backup,
- development-lab status and sample count,
- Trial 25 still locked.

No `EXECUTABLE` or `VALIDATED` option-volatility signal is added in V12.1.

## Error handling

- Access-token missing: WAITING_LOGIN; no exception loop.
- Market closed/weekend: MARKET_CLOSED; not an error.
- KiteTicker disconnect: mark disconnected and reconnect with backoff.
- Partial/malformed ticks: ignore only malformed tick; do not stop stream.
- Filesystem write failure: record explicit write error and keep service alive for retry.
- S3 upload failure: record backup error and retry next backup cycle.
- Instrument-resolution failure: ERROR/WAITING_INSTRUMENTS and retry; never fall back to fabricated strikes or expiries.

## Testing

Test-first coverage must include:

- nearest-expiry / ATM±12 contract selection,
- top-of-book and full-depth normalization,
- 5-second and 60-second cadence decisions,
- date-partitioned persistent paths,
- state/health reporting,
- market-closed and login-waiting behavior,
- gzip rotation,
- optional S3 backup success/failure isolation,
- historical dataset construction and no look-ahead in prior-day VIX,
- OLS/rolling OOS metrics on synthetic data,
- dashboard/API integration,
- release marker and Trial-25 lock preservation,
- complete regression and clean extracted-package regression.

## Release identity

Research build marker:

`2026-09-06-INSTITUTIONAL-V12.1-INDEX-VOLATILITY-RECORDER-FEASIBILITY-LAB`

Production playbooks remain unchanged. Trial 25 remains locked.
