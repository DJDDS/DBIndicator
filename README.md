# DBIndicator — V9.6 Trial 17 Independent Total-OI Validation

**Build:** `2026-09-02-INSTITUTIONAL-V9.6.0-TRIAL17-INDEPENDENT-TOTAL-OI`

## V9.6 Trial 17

V9.6 freezes the V9.5.3 exploratory **fresh total FUTSTK OI expansion** definition at `total OI z >= 1.5` and validates it once on older, non-overlapping official NSE history from **2021-09-01 through 2023-09-01**. The V9.5 discovery window and every previously locked final remain untouched.

Trial 17 is directionless and research/shadow only. Its primary outcome is 1D horizon-scaled ATR movement; 2D is secondary and cannot rescue a failed 1D result. A PASS requires at least 250 event rows, at least 100 distinct event days, 1D lift >= 1.10x, day-cluster 95% CI lower bound > 1.00x, cluster-robust total-OI-z t-stat >= 3.0 after realized-volatility/ATR/DTE controls, top-3-day-removed lift > 1.00x, and at least 3/4 positive chronological blocks. Historical membership and OI normalization are mandatory; MWPL/ban is mandatory before a PASS can be declared.

**Trial 18 remains LOCKED** until Trial 17 passes independent validation. `ACTIVE_PLAYBOOKS = ()` remains unchanged.

Open **Backtest → Run V9.6 Trial 17**. V9.5.3 remains visible below it as the completed discovery/audit path.

---

# DBIndicator — V9.5.3 Trial 15 Closure + Contract Structure Research

**Build:** `2026-09-02-INSTITUTIONAL-V9.5.3-TRIAL15-CLOSED-CONTRACT-STRUCTURE`

## V9.5.3 Daily OI Evidence + Contract Structure Lab


V9.5.3 changes the verdict hierarchy after the NSE-native validation run: a primary efficacy failure is now terminal **before** missing secondary integrity controls are considered. Trial 15 therefore closes when 1D lift/CI, volatility-control, tail or time-stability gates fail; missing MWPL remains visible for audit but cannot mask or rescue that failure. The final 20% remains permanently locked and Trial 16 remains LOCKED.

The build also adds a separate **Contract Structure Feature Research** layer over point-in-time NSE near/next/far OI. It measures fresh near creation, rollover-dominant transfer, fresh total expansion and abnormal unwind as magnitude features only. This layer has no trial number, cannot rescue Trial 15, cannot unlock Trial 16 and cannot create production TRADE/WATCH signals.

MWPL ingestion is hardened for zipped Combined-OI payloads and legacy `NSE Open Interest` column naming, with NCL Open Interest fallback parsing where the historical report generation differs.

V9.5 adds a separate **research-only 3-year daily-OI evidence path**. It does not alter the live production gate or the completed V9.4 audit. `ACTIVE_PLAYBOOKS = ()` remains unchanged.

- **Trial 13 is closed**; its final 20% remains permanently unread in this build.
- **Trial 14 remains failed as pre-registered**; V9.5 never rescues it by deleting compression.
- **Trial 15** asks whether a positive *unexpected* daily futures-OI shock predicts abnormal next-session magnitude after development-fitted OI expectations, realized-volatility controls, expiry-cycle controls, day-cluster inference, tail sensitivity and chronological stability. The final 20% is locked.
- **Trial 16 LOCKED**: Trial 15 is closed in V9.5.3, so the conditional-direction path remains locked and is not auto-run.
- Official NSE MWPL/ban data is loaded only for the frozen validation dates; historical F&O membership and lot-size normalization come from the NSE derivatives archive itself. Any incomplete load-bearing source blocks an otherwise passing feature and remains disclosed; it does **not** mask or rescue an already-failed primary efficacy gate. Historical ATM IV is used only if an honest point-in-time series is supplied.
- V9.5.2 uses official NSE daily stock-futures history as the primary OI source: the compact F&O Market Activity contract-wise futures report is preferred, with legacy/UDiFF F&O bhavcopies as date-appropriate fallbacks. It reconstructs actual near/next/far expiries, keeps UDiFF `OpnIntrst` in its published underlying-quantity units while deriving a contracts diagnostic from `NewBrdLotQty`, derives point-in-time FUTSTK membership from contract presence, and uses Kite only for daily cash prices/live cross-checks. Per-symbol daily evidence frames and raw NSE archives are cached for restart recovery.

Open **Backtest → Run V9.5 Daily OI Evidence Lab**. V9.4 remains visible below it as the completed measurement/audit path.

V9.3 keeps the live V9 evidence gate intact while changing the research question from **“which extra confirmation gate rescues a weak setup?”** to **“which independent evidence stream predicts movement before the move, and at what holding horizon?”**

## What to run first

Open **Backtest → Run V9.5 Daily OI Evidence Lab** for the current primary research path. V9.4 remains below it as the completed measurement/audit path and should not be rerun to retune Trial 14.

The V9.3 research run is fixed to the current NSE stock-F&O universe, 15-minute setup/execution and 180 calendar days. Primary evidence is **1D / 2D**; 2H / 4H remain diagnostics. Historical price fetching is chunked. Stage 2 converts the completed symbol shards once into lean rank-only checkpoints, then streams one cross-sectional rank at a time so the full universe is never retained in RAM.

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


### V9.3.5 alert scope
OI acceleration remains a research/ranking feature. OI-acceleration-only popups are disabled; alerts are reserved for the remaining configured signal/news channels.
