# V11.0.4 — Required-Window Factor Contract

Research build: `2026-09-04-INSTITUTIONAL-V11.0.4-REQUIRED-WINDOW-FACTOR-CONTRACT`

- Replaces whole-file factor validation with a consumer-scoped contract for Trial 24.
- Pinned raw IIMA source bytes and SHA remain unchanged. Trial 24 validates only 2010-01 through 2023-05, the only factor window it can consume.
- Requires only `rm_rf` (IIMA `MF`), `SMB`, `HML`, and `RF`; WML is not used by the frozen India-FF3 residualisation and cannot block readiness.
- Any missing/non-numeric required factor inside the requested window still fails closed. No imputation, deletion, or backfill is permitted.
- Six-digit `YYYYMM` dates are parsed deterministically before generic date parsing.
- The V11.0.3 production failure at 1993-10 occurred before `alpha_read_started`; Trial-24 outcome data remain unconsumed.
- Trial-24 specification, feasibility gate, 0.36% stress spread cost, alpha-read boundary, final holdout lock, and live V10.2.2 scanner are unchanged.
