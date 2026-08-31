# V9.2.8 — Backtest Integrity + Shadow Early Radar

Build: `2026-08-31-INSTITUTIONAL-V9.2.8-BACKTEST-INTEGRITY-SHADOW-RADAR`

- Corrects research friction to cost + two-sided slippage (0.08% + 0.05% per side = 0.18% default round-trip drag).
- Bull Institutional Accumulation seeds are rising-edge episodes; sustained Price↑ + OI↑ no longer creates one pseudo-trade per bar.
- Historical price retrieval uses the existing conservative Kite chunked downloader.
- Current incomplete intraday candles are removed before research; incomplete 4H buckets are also excluded.
- V9.2 reports explicit price/OI historical coverage, including OI-era timestamps and symbol coverage.
- Production Early Radar remains evidence-gated; a separate Shadow Early Radar exposes Energy Building / Ignition research stages without creating TRADE/WATCH or alerts.
- Dashboard legacy diagnostic columns are renamed so they cannot be confused with the Live Opportunity Score.
- No playbook threshold, 1.25 ATR chase guard, rejected Bear FSB status, final-sample lock, or Live Opportunity Radar weight was loosened.
