# V9.3 Anticipation Research Design

## Objective
Replace the exhausted confirmation-stack research pattern with a research-only component laboratory that measures independent evidence streams, pre-registers one before-the-move hypothesis, repairs 4H diagnostics, removes the dedicated overnight workflow, and makes 1D/2D swing research observable without changing production eligibility.

## Evidence constraints
- Current 180-day price history has only partial intraday futures-OI coverage; no missing expired-contract 15-minute OI is reconstructed.
- Add daily continuous-futures OI as a separate point-in-time, swing-oriented stream. A daily observation becomes available only after the completed session.
- Historical F&O membership remains today's universe replayed backward; disclose survivorship bias.
- Production playbooks remain inactive; rejected Bear FSB remains rejected; final samples stay locked.
- Existing 0.18% research friction and 1.25 ATR chase ceiling are immutable in this release.

## Component Edge Laboratory
Measure independently:
- Long Buildup and Short Buildup episode onset.
- OI acceleration ≥ +0.5 percentage points.
- TOD RVOL ≥ 1.3x.
- Compression / Coil ≥ 60.
- Relative direction alignment.
- VWAP direction alignment.
- Scaled minimum-ATR state.
- Not-extended / anti-chase state.
- Fresh breakout, with and without absolute NIFTY 8-bar regime alignment.
- Silent OI to ignition without chasing.
- Directionless Silent OI onset, compression onset, daily OI anomaly, and fixed-time baseline.

Directional outcomes: 2H/4H diagnostics, 1D/2D primary, N, distinct days, net expectancy after existing 0.18% friction, PF, win-rate Wilson CI, day-cluster average-return CI, MFE and MAE.

Directionless outcomes: max absolute move in ATR, hit rates for 0.5/1/1.5 ATR, and lift versus a fixed 11:00 baseline.

## Pre-registered Trial 13
**Silent OI Build -> Ignition**
- Intraday OI z-score >= 1.5.
- Absolute 60-minute price displacement <= 0.5 ATR.
- First fresh breakout within the next four completed 15-minute bars.
- Breakout direction must agree with the sign of the completed NIFTY 8-bar return.
- Existing 1.25 ATR anti-chase ceiling remains unchanged.
- Primary horizon 1D; secondary 2D; 2H/4H diagnostics only.
- Chronological 60/20/20 split by whole trading days; final 20% remains locked.
- Count this as historical model trial 13; family-wise alpha 0.05 gives Bonferroni reference 0.003846.
- Research/shadow only. No automatic production activation.

## 4H Diagnostic
- Dedicated UI/API mode fixed to 4hour and 180 days.
- Setup signals use completed 4H candles only.
- Execution/outcomes begin on the first available 15-minute bar after setup close.
- Progress and errors use the same research job status surface; no silent fallback to 15m.

## Legacy audit replacement
- Retire the old gate-sweep UI because slicing a negative baseline by correlated confirmation gates does not answer the new research question.
- Replace it with Component Edge Laboratory results.
- Old backend compatibility helpers may remain for reproducibility but must not be the primary user workflow.

## Overnight workflow removal
- Remove dedicated overnight-test UI and web routes.
- Keep session-aware 1D/2D outcome helpers because they are required for swing research and are not an overnight-gap strategy.
- Remove user-facing BTST/STBT terminology.

## Dashboard Swing Research
- Add a separate RESEARCH / SHADOW 1D/2D console.
- Route an attention name to one horizon only: active ignition favors 1D; quiet abnormal-OI/compression buildup can favor 2D.
- Persist the selected research horizon when the live forward event is first captured.
- Mature 2D on the second later trading session, not by calendar-hour arithmetic.
- Report routed 1D/2D net expectancy and PF separately.
- Never convert this research horizon into production TRADE/WATCH eligibility.

## Reliability
- V9.3 uses the V9.2.9 single-load Stage-2 architecture and rank-level checkpoints.
- Persist price/OI coverage through Stage-2 checkpoints so restart reports remain auditable.
- Compact event payload must preserve every Component Lab field, including ATR%.
- Full release verification must include the 210-symbol × 5,000-bar Stage-2 stress test, focused 4H tests, full pytest, Python compile, browser-script syntax validation and archive-integrity test.
