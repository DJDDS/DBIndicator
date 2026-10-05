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

## Lane 2 - Gap-down bounce basket (bullish, cash)
- Only on days the F&O universe opens more than 0.5% lower on average. Buy up to 10 stocks that open below yesterday's low by more than one
  90-day sigma while yesterday's close is above the 20-day average; entry at the opening price (pre-open order), exits recorded at 09:45
  and 10:15, 15 bps cost. Research 2019-26: +131 bps (76%) at 10:15, ~11 days a year. Parity: identical baskets on 10 of 10 sampled days.

## Recorder
- 09:09-09:14 pre-open snapshot of every F&O stock (indicative price, total buy/sell quantity) for future order-flow research.

## Schedule (IST, weekdays; fail-soft, idempotent per day)
prep 08:00-09:12 (needs Kite login) · pre-open 09:09 · gap basket 09:16 · shorts 09:45 · gap outcomes 10:16 · short outcomes 15:16.
Storage: <V12 storage root>/v131/.

## PySR
- The earlier intraday PySR formula modelled the size of a drop, not which stock to short (-9 bps as a short picker), so it is not used.
- A new PySR search for 09:45 short picks (2015-19 only; judged on 2020-26) ships as pysr_short945_run.py for the laptop.
