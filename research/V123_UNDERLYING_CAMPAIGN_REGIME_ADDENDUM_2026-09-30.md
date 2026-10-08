# Underlying Campaign Research — regime addendum

**Date/time:** 30 Sep 2026  
**Status:** Added before regime-aware replay outcomes were available.  
**Production:** unchanged / research only.

## User-requested regime distinctions

The campaign replay must report the same underlying impulse and pullback-reclaim
metrics separately for these overlapping historical windows:

1. **COVID period:** 11 Mar 2020 through 31 Dec 2021.
   - Full period is available in Dhan daily history.
   - Dhan intraday history is limited to the last five years, so the intraday
     campaign test can include only the available late-2021 overlap.
2. **Post-Oct-2024 stress window:** 1 Oct 2024 through the dataset end
   (25 Sep 2026).
   - This is a user-requested stress bucket, not an assertion that every
     session in the interval was continuously bearish.
3. **U.S.–Iran conflict:** 28 Feb 2026 through the dataset end.
   - The conflict start is set to 28 Feb 2026 because the coordinated
     U.S./Israeli attacks began on that date. March 2026 remains an important
     subphase because the oil/Hormuz shock intensified then.

These flags may overlap and must not alter event admission.

## Independent market-state diagnostic

In addition to named periods, classify each intraday event using only the
**prior completed NIFTY daily session**:

- STRESS_DRAWDOWN: NIFTY is at least 10% below its prior 126-session peak.
- BEAR: both prior 20-session and 60-session NIFTY returns are negative.
- BULL: both are positive.
- MIXED: otherwise.

This regime label is diagnostic only and cannot create, block, or tune an
underlying signal in this run.

## Reporting

For every regime report:

- event count and trading-day count;
- immediate-impulse +5/+10/+15/+30m signed returns;
- pullback-reclaim +5/+10/+15/+30m signed returns;
- day-bootstrap 95% CI;
- hit rate;
- volume-contraction reclaim subset where available.

The final OOS remains untouched for rule selection.
