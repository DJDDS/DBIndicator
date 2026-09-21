# Index Option Buying V1 — Stage 3 preregistration

Status: **RESEARCH / SHADOW ONLY — NOT VALIDATED — NOT DEPLOYED**  
Audit source: **Stage 2 Audit & Recommendations, 21 September 2026**  
DBIndicator linkage: **V12 / V12.1 recorder-audit research stream**  
Forward evidence begins: **22 September 2026**

## Why Stage 3 exists

Stage 2 established that the ungated NIFTY opening-range breakout should not be carried forward. The audited pocket worth testing is narrow, and the 2026 historical holdout has already been viewed. Therefore Stage 3 is not another parameter search. It is an attempt to falsify one frozen rule with executable option prices and genuinely new evidence.

The existing DBIndicator V12.1 NIFTY index-volatility recorder is the forward market-data source for this work. Stage 3 may **read** recorder artifacts but must not alter recorder sampling, thresholds, universe selection, persistence, Trial 25, V12 feasibility, or any production playbook.

## Frozen signal specification

The only primary Stage-3 signal is:

- NIFTY 50
- opening range: **90 minutes**
- range-width gate: **0.15% to 0.60% of the 09:15 session open**
- breakout buffer: **0.04% of the 09:15 session open**
- trigger clock: **completed 3-minute bars**
- confirmation: **two consecutive closes beyond the same breakout boundary**
- entry deadline: **13:00 IST**
- position count: **maximum one signal per session**
- stop: **none in the research definition**
- exit: **120 minutes after the confirmed signal**
- direction: Bullish -> long call; Bearish -> long put

The code writes a canonical SHA-256 of this specification. No OR length, trigger clock, confirmation mode, range gate, buffer, stop, or holding period may be retuned on NIFTY 2019-Aug-2026.

The audit's OR90/1-minute/double-close alternative is retained only as a named historical backup in the audit record. It is **not** run in the primary Stage-3 pipeline.

## What Stage 3 measures

### 1. Points check of the range gate

The exact frozen candidate is extracted from the Stage-2 **ungated primary** trade ledger. Returns are converted from R back to NIFTY points using: directional points = return_120m_r × opening_range_width.

The 0.15%-0.60% rows are compared with rows removed by that gate. This directly tests the audit concern that the apparent improvement might be caused by the R denominator.

### 2. Executable option P&L

For every forward signal, both expressions are evaluated:

- ATM weekly option
- one-strike-ITM weekly option

A bullish signal buys CE; a bearish signal buys PE. Entry is at the recorded **best ask** and exit is at the recorded **best bid**. The top-of-book quantity must cover one lot. Quotes older than the locked freshness limit are rejected. No midpoint fills and no synthetic Black-Scholes prices are substituted for missing quotes.

The existing version-stamped Zerodha/NSE equity-option charge model from app/trial25_execution.py is used so brokerage, STT, exchange charges, SEBI fees, stamp duty and GST are deducted from every round trip.

### 3. Futures control

The NIFTY-future reference recorded by V12.1 at the same entry and exit timestamps is scored in directional points. This is a control for the underlying signal. If the future retains directional follow-through while the long option loses after spread, theta and charges, that points to the instrument expression rather than the ORB signal as the failure source.

### 4. Forward paper evidence

The V12.1 recorder's 5-second micro files provide NIFTY spot reference, NIFTY future reference, CE/PE bid/ask, top quantities, quote age, strike, expiry and lot size.

Observed spot references are resampled to one-minute OHLC. Missing minutes stay missing; they are never forward-filled. The Stage-2 signal engine therefore fails closed when the complete opening-range or trigger block is unavailable.

Only sessions dated **22 September 2026 or later** count as new Stage-3 forward evidence. The already-viewed Jan-Aug-2026 holdout cannot be reused as confirmation.

### 5. Cross-index replication

If independent one-minute BANK NIFTY and/or SENSEX data are supplied, the exact same frozen NIFTY specification is applied unchanged. There is no index-specific retuning. Results are reported in index points with a session-level bootstrap confidence interval.

## Preregistered Stage-3 gates

All of the following must pass before a later production decision can even be considered:

1. **Executable option P&L:** mean net P&L after all modeled costs is positive, with both expiry-day and non-expiry-day observations represented and the session-level bootstrap CI reported.
2. **Cross-index replication:** positive mean 120-minute directional points on at least one independently supplied BANK NIFTY or SENSEX dataset, with no retuning.
3. **Gate reality check:** the frozen 0.15%-0.60% subset beats the removed observations in NIFTY points, not only in R.
4. **Forward paper:** at least 40 executable paper trades, non-negative cumulative net P&L, and maximum drawdown within a drawdown budget fixed before reading the 40-trade result.

If the drawdown budget is not supplied in advance, the forward gate reports WAITING_DRAWDOWN_BUDGET; it must not guess a budget afterward.

## Evidence that is still genuinely unavailable

The repository does not contain a complete 2019-Aug-2026 archive of historical one-minute NIFTY option bid/ask quotes. Stage 3 therefore does **not** fabricate historical option P&L from delta/theta assumptions. Historical option validation remains WAITING_DATA unless genuine executable quote history is supplied.

This is intentional. The audit specifically warned that a small spot edge can disappear after premium decay, spread and charges.

## Reproducibility

The Stage-3 runner writes frozen_signals.csv, option_pnl_ledger.csv, option_pnl_summary.csv, cross_index_replication.csv, stage3_gate_report.json and stage3_manifest.json.

The manifest records the frozen-spec SHA-256, fee-model version, input paths, artifact SHA-256 hashes, audit date, forward start date and explicit production_deployed=false.

## Production isolation

Stage 3 must not deploy to Railway production, change live scanner behavior, change V12/V12.1 recorder logic, change Trial 25, read the frozen Trial-25 efficacy period for tuning, create BUY/SELL recommendations, or unlock any production playbook.

A future production change requires a separate, explicit decision after the predeclared Stage-3 gates are complete.