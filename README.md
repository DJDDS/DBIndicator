# DBIndicator — V9.3 Anticipation Research

**Build:** `2026-09-01-INSTITUTIONAL-V9.3.1-V93-ISOLATION`

V9.3 keeps the live V9 evidence gate intact while changing the research question from **“which extra confirmation gate rescues a weak setup?”** to **“which independent evidence stream predicts movement before the move, and at what holding horizon?”**

## What to run first

Open **Backtest → Run V9.3 Anticipation Lab**.

The V9.3 research run is fixed to the current NSE stock-F&O universe, 15-minute setup/execution and 180 calendar days. Primary evidence is **1D / 2D**; 2H / 4H remain diagnostics. Historical price fetching is chunked and the Stage-2 pipeline retains the V9.2.9 single-load/checkpoint design.

## Component Edge Laboratory

V9.3 measures independent streams separately before any new combination is considered:

- OI acceleration (fixed reference ≥ +0.5 percentage points)
- Long Buildup / Short Buildup episode onset
- time-of-day RVOL (fixed reference ≥ 1.3x)
- compression / Coil (fixed reference ≥ 60)
- relative direction alignment
- VWAP direction alignment
- scaled minimum-ATR regime
- 1.25 ATR anti-chase / not-extended state
- fresh breakout with and without an absolute NIFTY 8-bar regime gate
- Silent OI → Ignition without chasing
- directionless Silent-OI onset, compression onset, point-in-time daily-OI anomaly and a fixed-time baseline

Directional rows report N, distinct trading days, net expectancy after the existing **0.18% research friction**, profit factor, win-rate Wilson interval, day-cluster average-return interval, MFE and MAE at 2H / 4H / 1D / 2D. Directionless precursor rows measure future expansion in ATR and lift versus baseline.

## Pre-registered Trial 13 — Silent OI Build → Ignition

Trial 13 is declared before viewing its result:

1. intraday OI z-score ≥ 1.5;
2. absolute 60-minute price displacement ≤ 0.5 ATR;
3. first fresh breakout within four completed 15-minute bars;
4. completed NIFTY 8-bar return sign must agree with breakout direction;
5. entry must remain inside the existing 1.25 ATR chase ceiling;
6. primary horizon 1D, secondary horizon 2D;
7. chronological 60/20/20 split is done by **whole trading days** so one session cannot leak across development and validation;
8. final 20% remains locked.

Trial 13 is historical model trial 13. Family-wise alpha remains 0.05, making the Bonferroni reference alpha 0.003846. The trial is **RESEARCH / SHADOW only** and cannot activate a production playbook.

## OI evidence integrity

V9.3 does not invent expired-contract intraday OI. Intraday OI coverage is displayed explicitly and may cover only the current near-expiry era. To obtain a longer point-in-time OI stream for swing research, V9.3 separately loads Kite continuous **daily** futures OI and maps a daily observation to intraday rows only after the corresponding session has completed.

The historical stock-F&O universe is still today's membership replayed backward until a true point-in-time F&O membership dataset is supplied; the UI continues to disclose that survivorship limitation.

## 4H Diagnostic repaired

**Run 4H Diagnostic** is a dedicated path. It cannot inherit a stale 15-minute scope value. It is fixed to 180 days, forms signals only from completed 4H candles and maps execution/outcomes to the first available 15-minute bar after the setup candle closes.

## BTST/STBT retired

The dedicated overnight-test workflow and its API/UI surface are removed. Session-aware 1D and 2D outcome machinery remains because it is required for genuine swing research; it is not an overnight-gap strategy.

## Dashboard 1D / 2D Swing Research

The Dashboard now contains a separate **Swing Research / Shadow** console:

- active Ignition-type attention is routed to a 1D research horizon;
- quiet abnormal-OI / compression positioning can be routed to 2D;
- a symbol is routed to one research horizon only;
- live forward validation matures 30m / 1h / 2h / 4h / 1D / 2D and records the routed 1D/2D horizon so its actual net expectancy and PF can be measured separately.

This routing is a research hypothesis, not a production signal. The validated production Swing tab remains empty until a playbook survives validation and its untouched final test.

## Production safety

- `ACTIVE_PLAYBOOKS = ()` remains unchanged.
- Bear Fresh Short Buildup remains rejected and is not retuned.
- Bull Institutional Accumulation remains research-only; its final sample is not unlocked.
- 1.25 ATR anti-chase remains active.
- 0.18% historical research friction remains active.
- Live Opportunity Radar and Shadow Early Radar remain attention/research layers, not probability-of-profit labels.
