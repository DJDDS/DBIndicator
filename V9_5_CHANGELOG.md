# V9.5 Daily OI Evidence Lab

**Build:** `2026-09-01-INSTITUTIONAL-V9.5.0-DAILY-OI-EVIDENCE`

## Purpose

V9.5 stops adding directional indicator combinations and tests the only V9.4 feature that survived measurement repair: the exploratory daily-OI magnitude effect. It is feature research, not a production signal.

## Trial governance

- Trial 13: **closed**; final 20% remains unread.
- Trial 14: **failed as pre-registered**; no compression deletion/retuning.
- Trial 15: pre-registered `Unexpected Daily OI -> Next-session Magnitude`, 1D primary, 2D secondary and non-rescuing. Development/validation/final split is 60/20/20 by trading date; final 20% has no unlock path in V9.5.
- Trial 16 **LOCKED** until Trial 15 passes validation.
- `ACTIVE_PLAYBOOKS = ()` remains unchanged.

## Evidence engine

- Daily cash price + continuous daily stock-futures OI; default 1,095 calendar days.
- Raw OI-change z-score retained only as an audit comparator.
- Expected OI change fitted on development only using OI lags, day-of-week, days-to-expiry, days-to-expiry squared, the Sep-2025 Thursday-to-Tuesday expiry regime break and previous OI level z-score.
- `unexpected_oi_z` is the frozen-model residual z-score used by Trial 15.
- 1D and 2D future movement are horizon-scaled by point-in-time ATR.
- Realized-volatility control, volatility quartiles, day-cluster bootstrap, one-way day-cluster-robust OLS, top-3-day sensitivity and chronological block stability.
- MWPL/ban populations are split into normal, high-MWPL/pre-ban and ban/95%+ when genuine point-in-time controls are provided.
- Historical F&O membership, lot-size normalization and ATM-IV inputs are accepted only as point-in-time data; missing controls are disclosed and never invented.

## Validation bar

Trial 15 can pass only on validation data with at least 250 anomaly events and 60 distinct trading days, 1D lift >1.0 with day-cluster 95% lower bound >1.0, cluster-robust unexpected-OI t-stat >=3.0, top-3-day-removed lift >1.0 and a majority of chronological blocks above baseline. Missing load-bearing data controls force `INCONCLUSIVE`. 2D cannot rescue 1D.

## Runtime

V9.5 is isolated from the V9.4 intraday Stage-2 ranking pipeline. It checkpoints each completed symbol frame under the research volume so a worker restart can resume without refetching completed symbols. Research continues to use the single heavy-work slot and pauses the live scanner during the historical job.

## Production safety

No V9.5 module is imported by the live scanner/background production decision path. No Trial 15 result creates TRADE/WATCH alerts. `ACTIVE_PLAYBOOKS = ()`.
