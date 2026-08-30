# V8.2.1 Fast Backtest Patch

Build: `2026-08-29-INSTITUTIONAL-V8.2.1-FAST-BACKTEST`

## What changed

- Primary **Run V8.1 Evidence-Locked Backtest** now uses a dedicated `v8_fast` workload.
- Fast workload skips legacy V6 sensitivity tables, interaction sweeps, Recent-Range lab, V6 edge lab, path-exit grid, excursion diagnostics and legacy promotion calculations.
- Fast replay also skips the V6 candidate classifier, V6 first-touch exit grid, compression-baseline expansion work and retention/retest research that V8 Top-K does not consume.
- **Legacy / 4H Diagnostic** explicitly uses the full legacy research path and preserves those diagnostic tables.
- Backtest progress is now four real stages instead of stopping visually at `211/211`:
  1. Fetching F&O history
  2. Building cross-sectional ranks
  3. Validating Bull/Bear Top-K
  4. Preparing report
- Stage 1 occupies 0–70% of the progress bar, so `211/211` no longer falsely displays 100% while server post-processing continues.
- Fast V8 results hide legacy report sections in the browser.

## Research integrity

No Bull/Bear scoring formula, Top-K breadth, participation floor, costs, holdout split, derivative-intelligence logic or final-sample policy was changed by this patch.
