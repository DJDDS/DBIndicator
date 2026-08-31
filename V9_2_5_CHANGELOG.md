# V9.2.5 Production Diagnostics

Build: `2026-08-31-INSTITUTIONAL-V9.2.5-PRODUCTION-DIAGNOSTICS`

- Added per-symbol scan failure stages (`instrument_lookup`, `candle_fetch`, `signal_compute`, `oi_attach`).
- Persisted per-symbol scan health so a current failure shows the last successful scan timestamp instead of hiding the problem behind aggregate counts.
- Settings now distinguishes the F&O master universe, valid live symbols, attempted symbols, and research universe.
- Dashboard exposes current failed symbols with stage/error/last-success details.
- Added Live Market State telemetry from the existing OI engine: Long/Short Buildup breadth, OI bias, top OI expansion, price+OI confirmation, unusual volume+OI, and rolling-acceleration readiness.
- Kept V9 evidence gating unchanged: research/shadow/rejected playbooks still cannot generate production candidates.
- Corrected Backtest copy from “V9.1 focuses” to “V9.2 focuses” and aligned the visible build marker.
