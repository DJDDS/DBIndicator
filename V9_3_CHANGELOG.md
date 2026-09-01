# V9.3.0 — Component Edge + Anticipation Research

Build: `2026-09-01-INSTITUTIONAL-V9.3.1-V93-ISOLATION`

## Research architecture
- Added Component Edge Laboratory: independent OI acceleration, OI state, TOD RVOL, Coil/compression, relative direction, VWAP, scaled ATR and anti-chase component measurement across 2H/4H/1D/2D.
- Added directionless precursor movement tests for Silent OI, compression onset and point-in-time daily continuous OI anomaly versus a fixed-time baseline.
- Pre-registered historical model Trial 13: Silent OI Build → first Ignition within four 15m bars, absolute NIFTY regime agreement, 1.25 ATR chase ceiling, 1D primary / 2D secondary, final 20% locked.
- Chronological Trial-13 partitions now split by whole trading days to prevent same-session leakage.
- Preserved full 0.18% research friction and multiple-testing disclosure.

## Data integrity
- Added point-in-time daily continuous-futures OI baseline for longer-horizon research; daily values become usable only after session completion.
- Intraday OI coverage remains explicitly disclosed; missing expired-contract intraday OI is never fabricated.
- V9.3 compact Stage-2 payload now preserves ATR% and all required precursor fields.
- Retained V9.2.9 single-load Stage-2 ranking/checkpoint pipeline and restart coverage metadata.

## 4H diagnostic
- Repaired the user-facing 4H Diagnostic path with a dedicated mode fixed to 4H setup / 15m execution / 180 days.
- Existing completed-4H-candle and first-subsequent-15m execution rules remain enforced.

## Dashboard / swing
- Added research-only 1D/2D Swing Console with one-horizon-per-symbol routing.
- Added true 2D live forward maturation using the second later trading session, not 48 calendar hours.
- Forward events persist the research horizon and Dashboard reports routed 1D and 2D net expectancy / PF separately.

## Cleanup
- Removed dedicated overnight-test UI/API and old gate-sweep UI.
- Removed user-facing BTST/STBT terminology while retaining session-aware 1D/2D swing outcome machinery.
- Legacy confirmation columns remain diagnostics only; Component Edge Laboratory is now the primary replacement for legacy gate-slicing research.

## Production safety
- No production playbook activated.
- Rejected Bear FSB remains rejected.
- No final 20% sample unlocked.
