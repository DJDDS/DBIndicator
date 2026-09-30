# V2 Executable Move Gate — Result (30 Sep 2026)

**Status:** RESEARCH ONLY. Production unchanged.

## Objective

Predict whether an underlying pullback/re-acceleration will produce a large enough move, quickly enough, to be economically usable for stock-option buying.

## Chronology

- March-April 2026: underlying abnormal-move calibration.
- May 2026: model training.
- June 2026: gate/coverage selection.
- 1 July-25 September 2026: untouched OOS.

## Primary target

Bearish underlying event reaches +30 bps in the trade direction before a -15 bps adverse excursion, within 10 minutes.

The model uses only information known at the reclaim:
- market+sector residual impulse magnitude;
- volume participation;
- pullback duration/depth;
- pullback/reclaim volume ratios;
- residual re-acceleration;
- campaign retention;
- market and sector alignment.

Options are not used to discover the stock signal.

## OOS result

Primary bearish model:
- OOS AUC: 0.5689.
- Baseline target-first rate: 25.04%.
- June-selected gate: top 30% of model probability.
- OOS gated events: 1,590 over 62 trading days.
- OOS target-first rate: 30.25%.
- OOS stop-first rate: 59.94%.
- OOS unresolved: 9.81%.
- Mean +5m return: +1.10 bps.
- Mean +10m return: +2.64 bps.
- Mean +15m return: +2.84 bps.
- Mean 10m MFE: 38.75 bps.
- Mean 10m MAE: -34.87 bps.
- Day-bootstrap 95% CI for mean +10m return: about [-2.05, +5.02] bps.

For the 30/15 first-hit bracket, gross expectancy is approximately +0.08 bps/event before transaction costs if unresolved events are treated as zero. This is economically flat.

Bullish ranking remained weak and did not produce a positive executable result.

## Ranking diagnostics

Strongest development-period ranking variables:
1. absolute market+sector residual impulse magnitude;
2. sector alignment at reclaim;
3. market alignment at reclaim;
4. sector alignment of the original impulse;
5. residual-sign persistence / re-acceleration.

Bearish OOS probability ranking:
- bottom quintile target-first rate: about 19.1%;
- top quintile target-first rate: about 31.2%.

This is useful ranking information, not a tradeable edge.

## Historical option translation

The OOS V2 gate was matched to uploaded Dhan one-minute stock-option history.
Entry is no earlier than the next minute after the underlying reclaim and exit requires the same physical strike.

ATM-preferred matched signals: 78.

ATM-preferred:
- +10m raw mean premium return: +0.34%.
- +10m after 1.8059% recorder-median spread sensitivity: -1.45%.
- +10m after 2.5532% recorder-P75 spread sensitivity: -2.19%.

All three tested strike offsets remained negative after recorder-median spread sensitivity at +10m:
- ATM-1: -0.87%.
- ATM: -1.22% across all ATM matches.
- ATM+1: -0.18%.

The estimated median underlying move required to cover recorder-median spread was about 12.5 bps in matched contracts. About 78% of matched events achieved 10m MFE above that hurdle, yet realized spread-adjusted option returns remained negative. MFE alone is therefore insufficient; timing/path, IV, strike behavior and friction remain material.

## Verdict

- V2 improves bearish move-magnitude ranking.
- The improvement is too small to survive realistic stock-option friction.
- The 30/15 target-stop economics are approximately flat before option costs.
- Bullish V2 is not useful.
- Do not deploy V2 as an Actionable option-buy trigger.
- Preserve the bearish V2 probability only as a research/ranking variable.
- Do not adopt stricter probability thresholds chosen from OOS.
