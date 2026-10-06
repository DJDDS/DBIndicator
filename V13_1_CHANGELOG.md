# V13.1 - Bearish & intraday paper lanes (admin-only, spotting, no orders)

Shown on /admin/v130 under "Bearish & intraday lanes (paper)". Owner-only APIs: /api/v131/status, /api/v131/export/{shorts,gaps,preopen}.

## Lane 1 - 09:45 short detector (bearish)
- At 09:45 IST every F&O stock is scored by a frozen LightGBM (18 features from the 09:15-09:45 snapshot and recent days;
  same settings as research early_model.py, trained on F&O stock-days 2015-2026). Lowest 3 scores = paper shorts at the 09:45 price,
  covered at the 15:15 close (5-minute bar), 10 bps cost. F&O-ban stocks excluded.
- Research (walk-forward, after cost): 2019-26 +30 bps/short (58% win), 2023-26 +21 bps, 2026 +14 bps; every year 2017-26 positive;
  random shorts about -2 bps. Holding to 15:15 beat every target/stop tried.
- Parity: live feature code reproduces the research features (15 of 18 identical, market averages within 0.5%); same 3 shorts on 11 of 12
  sampled days; numpy model reader equals LightGBM to 0.0.

### Put option (paper) on each short
- ATM put on the nearest expiry with 3+ days left, bought at the 09:45 ask, sold at the 15:15 bid; skipped if the spread is above 3.5%.
- Modelled 2019-26 (stock moves + range-based IV, not real option prices): +10.9% of premium per put, 55% win, median +3%, worst 5% -57%;
  strongest in expiry week. Real 1-minute option prices covered only 36 of the 2026 picks - too few to judge. Recording real bid/ask now.

## Lane 2 - Gap-down bounce basket (bullish, cash)
- Only on days the F&O universe opens more than 0.5% lower on average. Buy up to 10 stocks that open below yesterday's low by more than one
  90-day sigma while yesterday's close is above the 20-day average; entry at the opening price (pre-open order), exits recorded at 09:45
  and 10:15, 15 bps cost. Research 2019-26: +131 bps (76%) at 10:15, ~11 days a year. Parity: identical baskets on 10 of 10 sampled days.

## Restart safety (V13.0 fixes)
- Evening plan runs once per date even after a server restart (a restart could re-plan and add extra picks).
- Call-option session count advances once per day even if outcomes run twice.

## Recorder
- 09:09-09:14 pre-open snapshot of every F&O stock (indicative price, total buy/sell quantity) for future order-flow research.

## Schedule (IST, weekdays; fail-soft, idempotent per day)
prep 08:00-09:12 (needs Kite login) · pre-open 09:09 · gap basket 09:16 · shorts 09:45 · gap outcomes 10:16 · short outcomes 15:16.
Storage: <V12 storage root>/v131/.

## PySR
- The earlier intraday PySR formula modelled the size of a drop, not which stock to short (-9 bps as a short picker), so it is not used.
- A new PySR search for 09:45 short picks (2015-19 only; judged on 2020-26) ships as pysr_short945_run.py for the laptop.

## Setup A v1.1 dip-buy on Railway (6 Oct 2026)
- `app/v11_setup_a.py`: port of the laptop `scanner_v11.py` (rules frozen 3 Oct). Engine functions are line-for-line identical;
  storage moved to `<V12 storage root>/v11/` (pickle, no pyarrow needed). Parity on DJ's real store: identical indicator arrays and
  ledger (28 closed, 89% win, +373 bps; SOLARINDS / TEJASNET open).
- Scheduler: weekdays 19:15-22:30 every 15 min until NSE posts the day's files; idempotent per date; catches up a missed evening
  (up to 5 days). First start bootstraps ~470 trading days of NSE bhavcopy + index closes (resumable, outside market hours).
  Needs no Kite login.
- Seeds signal history and paper ledger from the laptop CSVs (`app/v11_assets/`).
- `/admin/v130#setup-a`: gate KPIs + stress gauge, tonight's BUY list, open positions with target/stop, closed trades, scorecard,
  health, "Run scan now" (POST /api/v11/run), CSV exports. Owner-only: /api/v11/status, /api/v11/run, /api/v11/export/<ledger|signals|runs>.
