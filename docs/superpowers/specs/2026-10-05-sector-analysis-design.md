# Sector Analysis approved design

Purpose: observational market → sector → stock workspace covering the official Nifty sectoral catalogue and all current constituents. No scanner decision or recorder changes. User approved the full scope and authorized build and deployment at 16:00 Asia/Calcutta on 2026-10-05.

## Data and calculations
Discover official sector pages and their constituent CSVs; persist dated last-good membership. Include unsupported index rows with explicit gaps. Kite instrument aliases resolve official index names. Cache up to five years daily OHLCV, serialize finite JSON, refresh completed daily history outside market hours, throttle history calls and coordinate with existing heavy slot. Quote snapshots every 60 seconds; preserve per-symbol last-good data with visible timestamps. Missing data is never zero.

Daily historical SMA/EMA 20/50/100/200 use completed bars; current price distance uses quote when fresh. Returns: today vs previous close, week/month/3M/6M/52W rolling calendar lookbacks using last trading close at or before boundary; completed calendar-quarter returns from previous quarter-end close; QTD separately. Current quotes do not alter completed quarter calculations. Current-membership historical breadth explicitly labelled, not historical index membership. Corporate-action adjustment status disclosed; unexplained discontinuities flagged rather than presented as trustworthy returns.

VWAP stock only: session-reset bar-based typical-price×volume/volume from current 5-minute bars, visibly approximate, never label index volume as VWAP. Top/bottom 2 separately for absolute and vs-sector return in selected period; tie break symbol; gaps excluded. Contribution requires dated weights: if official CSV lacks weights, show unavailable, never invent equal index weights. Relative strength uses return difference; rotation plot uses sector/index ratio change and its change across consecutive windows, explained as descriptive, not proprietary RRG or trade signal.

## UI
Separate authenticated /sector-analysis with /api/sector-analysis and selected-sector detail. Sortable full-period tables, SMA/EMA expandable values, 52W position, participation/concentration, normal-volatility distance, events and cached sourced headlines, sector verdict and conditional scenarios. Four views Overview, Heat map, Rotation, Detail. Mobile responsive cards, touch controls, fullscreen charts, clickable headings/navigation. Chart ranges 1M/3M/6M/1Y/3Y/5Y; candles, moving-average toggles, rebased benchmark and stock comparison, breadth, quarter bars, contributions when available, rotation trails. Period state drives rankings/heatmaps/explanations coherently. Safe text rendering and source-link validation. Coverage and freshness shown everywhere.

## Validation/release
Financial arithmetic fixtures, gaps/short history, quarter boundaries, session VWAP reset, membership HTML/CSV parsing, rankings and coverage, no network on page/API handlers, authentication, refresh single-flight and cache persistence. Browser QA desktop/mobile, all charts and selections, missing-data states. Compare whole-suite regression against baseline. Publish feature branch/PR only; main auto-deploys and must not be updated before 16:00 IST. Scheduled task must identify exact tested SHA and recheck CI before merge/deploy.

## Additional chart types approved during build
Horizontal sector-ranking bars, return-versus-SMA20-breadth bubbles, selected-stock return-versus-same-clock-relative-volume scatter, and sector/NIFTY running-high drawdown curves. Every missing point is omitted or visibly unavailable.
