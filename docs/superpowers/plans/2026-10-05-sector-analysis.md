# Sector Analysis Implementation Plan

> **For agentic workers:** Execute inline using superpowers:executing-plans.

**Goal:** Build the user-approved observational sector workspace with interactive mobile charts.
**Architecture:** Pure analytics + official membership provider + isolated background cached service + authenticated Flask endpoints + self-contained static frontend.
**Tech Stack:** Existing Flask, pandas, requests; locally bundled chart library.
**Spec:** docs/superpowers/specs/2026-10-05-sector-analysis-design.md

## Global Constraints
No trading/recorder changes. No main push before 16:00 IST. Missing is null. Official membership and dated sources. Index VWAP unavailable.

## Review Focus
- Holidays and incomplete sessions must not distort completed quarter returns.
- Missing benchmark/stock history must not fabricate relative strength.
- Stale membership/cache must remain visibly dated and must not be overwritten by failed fetch.
- Mobile chart and tab controls must fit at 360px with readable tooltips.
- New workload must not hold live scanner's heavy slot during market hours.

### Task 1: Analytics
Files: app/sector_analytics.py, tests/test_sector_analysis.py. Interface: summarize(candles, now, quote=None) -> dict; relative_returns(row, benchmark) -> dict; rank_members(rows, period, relative=False) -> dict; vwap_series(candles) -> list.
- [x] Write and run failing financial fixtures, including missing history, quarter boundaries, VWAP reset and rankings.
- [x] Implement pure functions; run fixtures.

### Task 2: Provider and cache service
Files: app/sector_data.py, app/sector_service.py; tests/test_sector_service.py. Interface: parse_catalogue(html), parse_members(csv); ensure_started(); overview(); detail(sector_id); stock_chart(symbol).
- [x] Write/run failing official HTML/CSV and persistence/no-data tests.
- [x] Implement dated official provider, single-flight off-market hydration and isolated last-good snapshot.
- [x] Run provider and service tests.

### Task 3: Routes and dynamic UI
Files: app/web.py, app/templates/sector_analysis.html, app/static/sector_analysis.js/css, navigation templates, tests/test_sector_routes.py.
- [x] Write/run route auth and empty-state tests.
- [x] Implement authenticated cached endpoints and interactive UI with locally bundled charts.
- [x] Verify chart controls desktop/mobile, empty/error states, no page overflow.

### Task 4: Release
Files: .github/workflows/sector-analysis.yml, docs/sector-analysis-release.md.
- [x] Compile, run new tests and full-suite delta versus baseline; review changes.
- [x] Commit feature branch, publish PR and inspect CI.
- [x] Update scheduled deployment task with exact reviewed SHA and instructions; leave main untouched.
