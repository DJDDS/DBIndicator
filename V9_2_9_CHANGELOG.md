# V9.2.9 — Pipeline Reliability + Audit Hardening

Build: `2026-08-31-INSTITUTIONAL-V9.2.9-PIPELINE-RELIABILITY-AUDIT-HARDENING`

## Backtest pipeline reliability

- Stage 2 no longer deserializes every 210-symbol checkpoint eight times. Each Stage-1 shard is loaded once; only compact feature frames are retained while the seven cross-sectional ranks are built.
- Progress now reports Stage-2 input loading, each named rank's matrix/ranking/attachment work, event counts, and elapsed time.
- A rank-progress checkpoint is written after each completed rank, so a Railway worker restart resumes after the last completed rank instead of repeating the full Stage 2.
- The research resume schema is bumped so V9.2.9 cannot silently reuse same-day V9.2.8 shards whose compaction semantics differ.
- Full-load regression coverage includes 210 symbols × 5,000 15-minute bars with realistic compact event payloads and a generous performance budget.

## Audit recommendations adopted now

- Forward validation now headlines **net expectancy and profit factor** after the fixed 0.18% research friction, with 95% Wilson confidence intervals beside net win rate. Raw directional-return statistics remain available in the API.
- Bull gate diagnostics no longer present `Price up + OI up` and `Long Buildup` as two independent confirmations; the duplicate evidence is collapsed and the underlying evidence streams are named explicitly.
- The Bear FSB compactor now keeps the broad fresh-short seed and applies frozen extension/basis/CLV thresholds only once at the immutable freeze boundary.
- V9.2 protocol output now declares 12 historical trials, family-wise alpha 0.05, Bonferroni alpha 0.004167, and the fact that point-in-time F&O membership is unavailable. This is disclosure/guardrail metadata, not a fabricated p-value or Deflated Sharpe statistic.
- Historical current-universe replay is explicitly labelled survivorship-biased until a point-in-time NSE F&O membership dataset is supplied.

## Deliberately not added in this build

- No new short-squeeze term or sign-flipped Bear replacement: the rejected Bear FSB remains retired.
- No MWPL historical exclusion is fabricated without point-in-time MWPL data.
- No point-in-time F&O membership is invented from today's list.
- No Deflated Sharpe/FDR statistic is claimed until a calibrated trial-return protocol is implemented.
- No live opportunity, ATR, OI, regime, or production-threshold tuning.
