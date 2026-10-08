# V3 Option Executability Gate — Result (30 Sep 2026)

**Status:** RESEARCH ONLY. Production unchanged.

## Objective
Keep the V2 underlying signal fixed and use option-state information only to decide whether the selected stock signal is economically executable as a stock-option buy.

Historical option fields available:
- 1-minute option OHLC,
- IV,
- OI,
- volume,
- spot,
- strike,
- expiry code.

Historical bid/ask/depth are not available at every event minute, so the frozen 15-day option recorder is used only as a friction sensitivity reference:
- median ATM-straddle spread: 1.8059%,
- P75 spread: 2.5532%,
- 195/210 symbols below the 4% practical spread gate,
- 95.1% term-structure coverage.

## Model chronology
Independent option candidates:
- Apr-May 2026 train,
- Jun 2026 gate selection,
- Jul-Sep 2026 untouched OOS.

The execution model uses only information known at signal time:
1/3/5-minute premium returns, premium acceleration, IV changes, OI changes, volume ratio, directional spot returns/acceleration, moneyness, estimated absolute delta, DTE, premium/spot, elasticity, required underlying bps to cover spread, time of day, strike offset.

Entry is the next minute after the signal. Exit requires the same physical strike.

## Independent candidate OOS
The June-selected contract rule chooses the highest predicted-executability contract among ATM-1 / ATM / ATM+1 and keeps the top 30% probability gate.

Jul-Sep OOS:
- selected contracts: 51,
- +10m mean after recorder-median spread sensitivity: +0.51%,
- +10m mean after recorder-P75 spread sensitivity: -0.24%,
- +10m median after recorder-median spread: -0.63%,
- day-bootstrap confidence interval crosses zero.

Direction split at +10m:
- CALL: +2.02% after median spread, +1.26% after P75 spread,
- PUT: -1.06% after median spread, -1.80% after P75 spread.

A PUT-only execution model also failed OOS (AUC about 0.45; all June-selected gates remained negative in Jul-Sep OOS).

## V2 bearish underlying + V3 option gate
Applying the frozen mixed V3 gate to untouched V2 bearish OOS:
- V2 option-overlap signals available: 70.
- V3-passing signals: 29.
- Contract selection is causal: highest V3 probability among available ATM-1 / ATM / ATM+1 above the June-frozen threshold.

Spread-adjusted option performance:
- +5m: 22 usable, mean -1.65%.
- +10m: 23 usable, mean +0.55%; P75-spread mean -0.20%.
- +15m: 21 usable, mean +2.56%; P75-spread mean +1.80%.

But:
- +15m median is slightly negative (~-0.21%),
- hit rate 47.6%,
- only 14 trading days at +15m,
- day-bootstrap 95% CI crosses zero widely,
- mean is right-tail driven,
- independent PUT OOS is negative.

## Feature ranking
Strongest validation-period execution features:
1. absolute delta,
2. directional 5-minute spot return,
3. directional 3-minute spot return,
4. DTE,
5. 1-minute premium return,
6. 3-minute IV change,
7. time of day,
8. premium/spot ratio,
9. required underlying bps to cover spread.

## Verdict
- V3 mixed option-execution ranking contains useful information.
- It is asymmetric and materially stronger for CALLs than PUTs.
- V2 bearish + V3 is interesting but too small and insufficiently robust to deploy.
- Do not change Actionable Desk logic from V3.
- Preserve V3 probability/features as research-only.
- Next defensible step: study the CALL side separately with the same chronology instead of forcing one symmetric execution model.
