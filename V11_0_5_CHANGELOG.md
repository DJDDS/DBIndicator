# V11.0.5 — Strict Required-Window Factor Contract

Research build: `2026-09-04-INSTITUTIONAL-V11.0.5-STRICT-REQUIRED-WINDOW-FACTOR-CONTRACT`

## Root cause closed

V11.0.3 validated numeric factor values across the entire pinned IIMA history before applying the Trial-24 consumer window. The historical `1993-10` `MF/rm_rf = NA` row therefore aborted Trial 24 even though Trial 24 cannot consume that row.

V11.0.4 bounded numeric validation to the preregistered Trial-24 factor window (`2010-01` through `2023-05`) and to the frozen FF3 inputs (`rm_rf`, `SMB`, `HML`, `RF`). V11.0.5 closes the remaining integrity gap by making that bounded window complete-month fail-closed.

## Contract

- Out-of-window rows may remain malformed in unused factor fields without blocking Trial 24, provided their month key is parseable enough to prove they are outside the required window.
- Every calendar month from `2010-01` through `2023-05` must exist when Trial 24 loads factors.
- Every required in-window `rm_rf`, `SMB`, `HML`, and `RF` value must be numeric after the already-approved formatting normalization.
- Missing or bad required in-window values fail before `alpha_read_started`.
- WML is not required by the frozen India FF3 residualisation and is not a readiness gate.
- Raw source bytes and SHA-256 provenance are unchanged.
- Trial-24 hypothesis, 36-month regression, 12-1 residual-momentum formation, deciles, one-month hold, equal weights, 0.36% stress spread cost, feasibility gate, alpha-read boundary, final 20% lock, and production activation rules are unchanged.
