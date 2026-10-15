# V13.7 - Intraday desk redesign, pool rules from the 88-session replay, faster pages

## Evidence (Dhan 5-minute data, 211 F&O stocks, 8 Jun - 14 Oct 2026, 3 picks/day, 0.06% cost)
- 09:25 pool, DOWN only (A): 61.5% win, +41.5 bps. Both sides (B): 62.9%, +47.6 bps.
  UP calls won 67.5% and beat the Nifty 62% of the time, in a month-after-month falling intraday market.
  The model was frozen on 2026-10-01, so 81 of the 88 days are in-sample for A-G.
- Hierarchy DOWN only (live rule with volume): 58.2%, +49.7 bps. The UP side lost (45%).
- "Both must agree" mixes (M1-M3): 55.7-56.9%, worse than A. The desks pick different stocks (4% overlap).
- Best combination: B + hierarchy DOWN. 61.4%, +41.7 bps, 70% of days positive, t 3.11.
- Relative-strength desk: 49.7% overall. UP picks 47%, the 13:15 refresh ~0.
  Adding its DOWN picks lowered the pool's t (3.11 -> 2.88).

## Changes
- Daily pool takes both sides by default (hc_down_only default False; a saved DOWN-only choice still wins).
- Relative-strength picks no longer join the watched pool (add_rs default False).
  The desk keeps running and recording on its own.
- Pool lab: 09:25 model x 09:45 hierarchy comparison (H, H-DOWN, M1-M3) on recorded days.
- /admin/intraday:
  - sticky section menu, IST clock, market state and countdown to 15:15;
  - "today" strip;
  - live chart of every pool stock vs the F&O market;
  - stock cards with sparklines;
  - auto-refresh;
  - phone layout.
- Dashboard:
  - today-at-a-glance strip (owner-only desk tiles);
  - jump-to bar and back-to-top;
  - IBM Plex type.
- Speed:
  - chart library fetched after the page is visible on /admin/intraday;
  - web fonts load without blocking;
  - /static/vendor cached for a week.
  - Intraday desk ready time 1.3 s -> 0.12 s, dashboard 0.8 s -> 0.13 s (local, warm).
- OI screener: no JS error when the page has no scan yet.
- scripts/dhan: PowerShell downloader for Dhan candles. dhan.env is git-ignored; copy dhan.env.example to dhan.env.
