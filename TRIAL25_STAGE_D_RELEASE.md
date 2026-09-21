# Trial 25 Stage-D Shadow Recorder — Release Note

**Date:** 2026-09-21  
**Status:** Research-only release candidate; not deployed

- Trial 25 Stage-D shadow recorder.
- Research-only / no production activation and no broker order or alert surface.
- No-peeking P&L gate remains active until the deterministic first 40 eligible completed events.
- Baseline research universe remains the frozen 2026-09-21 feasibility cohort of 195 symbols.
- Event eligibility intersects that frozen cohort with current live NSE stock-option contract availability.
- Post-freeze F&O additions are recorded in an onboarding-only operational lane and cannot enter Trial 25.
- F&O membership observations are persisted point-in-time with an append-only ledger and symbol-set SHA-256.
- Frozen symbols without the required live contracts fail closed; they are never replaced.
- Entry/exit evidence uses executable top-of-book quotes, one-lot required-side quantity, and a separate live-book versus old-last-trade freshness audit.
- Charge model: `ZERODHA_NSE_EQ_OPT_2026_04_V1`.
- Stage D freezes only sigma, the exact first 40 event IDs, fee/code provenance, and the required independent Stage-C N.
- Trial 24 final holdout, V12/V12.1 recorder rules, frozen V12 feasibility thresholds, and directional scanner rules remain unchanged.

## Frozen V12 feasibility provenance

- `v12_option_state_10d_2026-09-21.json` — `4cb686ec834627c83103611229bf0924526655c36a44d9376abda131f32c33f4`
- `v12_feasibility_code_10d_2026-09-21.py` — `949f7c08c1ecfea9ff130468ef50a3687ae5241c94043513efd3809120f352d7`
- `v12_feasibility_10d_2026-09-21.json` — `d14361328e8ed09a5ecb81071e55ceff9d890f76dac3c5154e38e5dc32871260`

Railway merge/deployment and production-volume acceptance are intentionally deferred until the currently running production test is completed and deployment is explicitly allowed.
