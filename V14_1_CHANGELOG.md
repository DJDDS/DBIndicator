# V14.1 - "Trade now" panel on the intraday desk, hierarchy baseline fills itself

## Why
- Live desk on 16-Oct-2026: all 3 pool stocks showed "watch" with no explanation of what to trade.
  - The entry check was WEAK 0/4 on each, so no pool trade.
  - The best confidence was 0.104 against 0.14 needed for grade A.
- The hierarchy desk showed nothing: its 09:30-09:45 volume baseline had 4 of the 10 required days, and the lab
  that seeds it had never been run.

## Changes
- `app/v141_trade_now.py` (pure) + `_trade_now.html`: one panel at the top of /admin/intraday.
  - **TAKE**: the pool's traded picks, the movement-predictor bracket and the NIFTY ACD short. Each shows
    entry, stop, target, exit time and live state.
  - **Paper only**: hierarchy PICK (research desk).
  - **Considered but not traded**: each skipped stock with the reason in plain words (e.g. "entry check WEAK
    0/4 - already stretched").
  - The grade-A rule, with today's best confidence.
  - One status line per desk: ACD watch levels, hierarchy reasons, movement predictor.
  - "No trade right now" when nothing passed.
- `v136_hierarchy_lab.seed_recent_baseline` + a baseline keeper thread.
  - Weekdays 08:00-09:40: if the baseline is shorter than `min_vol_days`, fetch the last 40 days of 5-minute
    candles per F&O stock from Kite (one request each) and fill it.
  - Days recorded live are kept.
- Print: the sticky desk header no longer floats over the page in PDFs.
