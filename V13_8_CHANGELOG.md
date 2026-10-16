# V13.8 - Movement predictor trade plan (two-sided bracket), ATM straddle removed

## Evidence (Dhan 5-minute data, 121 sessions 20 Apr - 15 Oct 2026, futures, 10 bps cost + 2 bps slippage)
- Replayed picks match live closely: top pick hit 70%, high-confidence top pick 84%. The model was trained up to
  2026-10-01, so these days are in-sample for it.
- 09:45 lean trade (continue the first-30-minute move): 70% won Apr-Jul, 28% won Aug-Oct. Not robust; it stays
  as information only.
- Bracket on the high-confidence 09:16 top pick: settings chosen on Apr-Jul, checked on Aug-Oct.
  Result: 44 trades, 89% won, +50 bps average (90% range +22 to +79). Apr-Jul 91%, Aug-Oct 86%.
  - Nearby settings (trigger 0.4x) give 83% won, +45 bps.
  - Normal-confidence days were not reliably profitable.
- Live 09:45 ATM straddle paper record was -4.4% per trade; the straddle lane is removed.

## Rule (paper, high-confidence days only)
- Reference = the stock's 09:20 price (close of the first 5-minute candle).
- Buy above ref +0.5x normal day move. Sell below ref -0.5x. The first level touched wins; cancel the other.
  No new entry after 12:00.
- Target: 0.3x normal beyond the entry. Stop: the other level. Otherwise exit at 15:15.
- A candle that touches both levels is skipped (as in research).

## Changes
- `app/v132_movers.py`:
  - `plan_levels`, `plan_walk` (pure, same code path as the research replay) and `run_plan`.
    `run_plan` runs every minute 09:21-15:20 from completed Kite 5-minute candles.
  - Telegram messages for the levels, the trigger and the exit.
  - New scorecard `plan`: n, win, avg bps, targets, stops, days without a trigger. A drift flag is raised when
    15 or more trades run more than 15 points below the research win rate.
  - Removed `straddle_entry`, `straddle_outcome`, `run_straddle`, the straddle scheduler steps, outcome
    pricing and scorecard.
  - Fixed `peer_groups` on newer pandas (read-only array from `to_numpy`).
- `/admin/v130` movement card: trade-plan box (levels, live status, record vs research); straddle table and
  record removed. Old `straddle` fields in picks.jsonl are ignored.
- Tests: `tests/test_v132_plan.py` replaces `tests/test_v132_straddle.py`.
