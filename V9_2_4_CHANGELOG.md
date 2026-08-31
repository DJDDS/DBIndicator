# V9.2.4 Live Production + OI Repair

Build: `2026-08-31-INSTITUTIONAL-V9.2.4-LIVE-PRODUCTION-OI-FIX`

## Production correctness
- No unvalidated/rejected V9 playbook can drive live TRADE/WATCH shortlists.
- Bull Institutional Accumulation and real Catalyst Continuation remain SHADOW/research only.
- Bear Fresh Short Buildup remains REJECTED and is blocked from live shortlists/alerts.
- Dashboard scan health now separates Attempted / Valid / Errors instead of reporting all attempted rows as "scanned".

## OI Screener resilience
- `/api/oi-screener` returns a compact OI-only JSON contract instead of the full scanner row.
- Numeric pandas/numpy values and restored numeric strings are normalized to finite JSON numbers or null.
- The OI browser renderer defensively converts values before formatting and checks non-200 API responses explicitly.

## Backtest
- V9.2 Diagnostic Reset remains hard-wired to the established 15-minute / 180-day / full-F&O research path.
- No trading thresholds or consumed final-test results changed in this release.
