# Sector Analysis release — 5 October 2026

Approved observational workspace: official sector/index → all current constituents → price structure, relative strength, participation and conditional scenarios. User explicitly authorized build and deployment at 16:00 IST on 2026-10-05.

## Exact production change

Add `/sector-analysis` and authenticated read-only `/api/sector-analysis`, `/api/sector-analysis/<sector_id>` and `/api/sector-analysis/stock/<symbol>` routes. Add navbar buttons on Dashboard, OI Screener and Patterns. Add isolated analytics/provider/cache modules, responsive page, local ECharts 5.6.0 asset, official dated seed and sector tests/CI. No existing trading, recorder, frozen-research or background-scanner code changes. One pre-existing alert regression test is corrected to match the existing safety-flag contract and assert capture-file immutability.

Official seed fetched 2026-10-05: **34 sectoral indices, every membership file retrieved successfully, 476 unique stocks**. Catalogue and membership are refreshed with last-good fallback.

## Reading the page

Overview offers sortable calendar-period returns, quarter history, relative strength, MA coverage, breadth, ranking bars and breadth scatter. Heat Map offers sector return and SMA breadth matrices. Rotation has selectable 5/20/60-session relative-ratio trails. Sector Detail offers interactive candle/MA charts, rebased comparison, breadth/sector-price chart, completed-quarter bars, performance timeline, drawdown curves, top/bottom two explanations, constituent heatmap/table, relative-volume scatter and stock session VWAP. Range and period controls update associated charts/rankings. Stock headings open charts; mobile controls and fullscreen supported.

## Data limits that must remain visible

- Vendor OHLC corporate-action adjustment is not independently verified; large discontinuities are flagged. These are price returns, not total returns.
- Daily SMA/EMA and historical charts use completed daily bars. Quotes retain their own exchange timestamps and expire on reads. Mismatched return dates are not compared.
- Cash index VWAP is unavailable; stock VWAP is a completed 5-minute typical-price approximation. Intraday data is requested lazily on stock selection; the page can show queued/missing data. Same-clock relative volume uses up to five prior sessions with explicit coverage. Stale intraday metrics are not mixed with current quotes.
- Current-member historical breadth is not point-in-time reconstructed membership.
- Official membership CSVs supplied **no weights**. Weighted treemap/contribution are unavailable, with an explicit explanation. Do not substitute invented/equal index weights.
- News and upcoming results use existing cached evidence, with dates and source links. Absence of a verified cause is shown honestly. No new news quota or research-calendar mutations.
- Some official index names may not have a Kite history token. Their catalogue rows and constituents remain visible with missing index metrics.
- Initial five-year daily hydration occurs after market hours, index/benchmark first, then stocks; 511 or fewer supported instruments at approximately one historical call/second plus latency. Do not mistake initial warmup for a failed recorder. Daily candles stay on disk to limit memory.

## Verification

New financial/provider/cache/auth tests pass. Whole-suite baseline comparison: baseline 1215 passing + 10 failing; latest feature run 1241 passing + 9 of the 10 pre-existing failures; zero new failure nodes. The stale Friday-weekend equality assertion was repaired to require both safety flags false and verify the captured file is unchanged; no alert behaviour changed. Independent review's quote expiry, hydration retry, intraday freshness and scanner-slot isolation findings repaired with RED→GREEN tests. Desktop and 390px browser checks passed every view, range/period controls, stock comparison, VWAP, missing-weight state and page overflow; no JS exceptions. UI screenshots use synthetic fixtures only and are not evidence of live market calculations.

The pre-existing failures are:
- Six in `test_stock_in_play_breakout.py`: intraday sponsorship, missing OI, swing stage/persistence, dashboard/backtest copy, retention bar, numpy boolean flags.
- The baseline Friday-weekend capture test failed because it omitted two required safety flags. This release corrects that test only; it now passes and verifies no recapture/file overwrite.
- Two in `tests/test_v120_trade_console.py`: futures executable promotion and extended high-score downgrade.
- `tests/test_v927_market_regime_forward.py::test_opportunity_radar_uses_multifactor_regime_as_bonus_not_veto`.

## Deployment handoff

Railway project `c756af2b-bab5-49d9-974e-9a96860d6d46`; production environment `c8de8763-f800-4c45-ae46-55954a04878d`; service `c9c0010b-f81f-4d7b-ac0b-c3af2411c6c0`. Source is `DJDDS/DBIndicator`, branch `main`, and main pushes auto-deploy. Therefore **do not merge before 16:00 Asia/Calcutta**. Before merging, identify exact reviewed feature SHA, recheck CI and diff against current main, and verify no unrelated changes entered. Preserve any concurrent main changes. After merge follow Railway deployment until SUCCESS; inspect `/sector-analysis` and sector API with existing owner/member authentication, page assets and navigation. Opening the page starts the isolated cache daemon. Verify first warmup progress and zero new recorder errors. If CI introduces new failures or source/access is unavailable, do not deploy; report blocker.

## Advance / decline addition
Each sector reports advancing, declining, unchanged and unavailable constituent counts versus previous close, fresh-quote coverage, net advances and A/D ratio. Missing or stale quotes are excluded from unchanged counts. With no declines the ratio is null and UI says “no declines”; with no coverage it is unavailable. The overview has a clickable stacked count chart; sector tables, mobile cards and detail summaries carry the counts. This is today-only, independent of selected historical return period.
