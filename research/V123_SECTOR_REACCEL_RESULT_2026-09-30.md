# V12.3 sector-relative 1-minute campaign result — 30 Sep 2026

**Status:** RESEARCH ONLY / PRODUCTION UNCHANGED

## Data
- 210 NSE F&O stocks.
- Uploaded Dhan 1-minute underlying panel: 2 Mar–25 Sep 2026.
- NIFTY + 21 sector indices.
- Calibration: Mar–Apr 2026.
- Validation: May–Jun 2026.
- OOS: 1 Jul–25 Sep 2026.
- Development q99 threshold for 3-minute market+sector residual impulse: |Z| = 4.4914.

## General sector-relative pullback/reclaim

Validation base reclaim:
- +5m +0.235 bps
- +10m +0.272 bps
- +15m -0.527 bps

OOS base reclaim:
- +5m -0.173 bps
- +10m -0.158 bps
- +15m -0.462 bps

Conclusion: generic sector-relative reclaim does not create a reliable continuation edge.

## Bearish FAST_RECLAIM research subset

Definition:
- bearish 3-minute market+sector residual impulse;
- at least one adverse 1-minute bar;
- reclaim within 5 minutes;
- close breaks below the prior two 1-minute closes;
- current factor residual is bearish;
- current bearish residual is stronger than the preceding two-bar pullback residual average.

Validation May–Jun:
- 2,904 events / 40 days
- +5m +1.293 bps
- +10m +2.991 bps; day-bootstrap 95% CI [0.336, 4.874]
- +15m +1.990 bps
- 20-bps target-first 51.31%

OOS Jul–25 Sep:
- 3,390 events / 62 days
- +5m +0.787 bps
- +10m +1.502 bps; day-bootstrap 95% CI [-0.044, 3.245]
- +15m +0.953 bps
- 20-bps target-first 50.85%

The effect weakens materially OOS and does not clear a strict statistical/economic deployment bar.

## Broad-market diagnostic

For OOS bearish FAST_RECLAIM:
- NIFTY-BEAR days: +10m -1.025 bps.
- Other NIFTY regimes: +10m +0.546 bps.

The narrow effect is associated with the stock's own downside campaign, not simply a broadly bearish NIFTY session.

## Uploaded stock-option translation

Same physical strike required at entry and exit. OOS bearish FAST_RECLAIM ATM PUT:
- 149 matched signal rows.
- 112 usable same-strike observations at +10m.
- Raw +10m premium return +0.281%.
- Recorder-median spread sensitivity (1.8059%): -1.513%.
- Recorder-P75 spread sensitivity (2.5532%): -2.247%.

OOS +10m by PUT strike offset:
- ATM-1: raw +0.910%; median-spread adjusted -0.896% (54).
- ATM: raw +0.281%; median-spread adjusted -1.513% (112).
- ATM+1: raw +1.078%; median-spread adjusted -0.731% (51).

No tested strike offset survives recorder-median spread sensitivity OOS.

## Frozen conclusion

1. Generic sector-relative campaign/reclaim: negative; do not deploy.
2. Bearish FAST_RECLAIM: retain only as a research/ranking feature, not an Actionable trigger.
3. Option translation is not economically positive after realistic spread sensitivity.
4. Do not change production logic from this result.
