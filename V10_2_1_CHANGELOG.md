# V10.2.1 — Provenance & Statistical Integrity Lock

Build: `2026-09-04-INSTITUTIONAL-V10.2.1-PROVENANCE-STATISTICAL-INTEGRITY-LOCK`

Integrity-only release. No new alpha trial, no Trial-21/22 reread, no final-holdout read, and no live Opportunity Radar change.

## What changed

- Completed V10.2 state migrates read-only into V10.2.1; production start refuses a new alpha reread.
- Trial-21 read history preserves both observed validation reads:
  - V10.0: 15 sector histories; Bull 586/145, Bear 284/119.
  - V10.2: 10 sector histories; Bull 564/142, Bear 252/118.
- Code-level cause confirmed: Trial 21 consumes runtime sector histories and silently skipped construction when a sector frame was unavailable. Trial 22 does not consume sector histories, explaining its bit-identical reread.
- Sector panel is now fail-closed for any explicit internal replay: expected sectors, loaded sectors and missing sectors are recorded; incomplete panels cannot construct Trial 21.
- Input provenance manifest hashes the research universe, symbol-sector map, sector histories, historical membership, lot-size inputs, cost model and gate-battery version.
- Future trial executions can persist exact validation event rows with canonical SHA-256 content hashes. Legacy V10.0/V10.2 raw event rows are explicitly marked unavailable rather than reconstructed.
- Event-weighted inference now includes a date-clustered t-statistic for the same event-weighted estimand.
- Unequal cluster sizes use `sum(n_d^2)/sum(n_d)` in the design-effect decomposition; effective N is derived from clustered-vs-naive standard errors.
- `rho >= 0.95` is reported as not identified instead of being displayed as a valid 1.00 correlation.
- Legacy Trial-21/22 gate verdicts are frozen and are not rewritten. The historical gate battery is versioned as `V10.0-TRIAL21-22-FROZEN-1`.
- Future research primary estimand is declared as `DAY_WEIGHTED_FIXED_CAPITAL_NET`; legacy event-weighted results remain historical records.
- Pre-trial feasibility assessments now carry a named t-bar and `require_feasible_registration()` refuses registration on any `DO_NOT_RUN` verdict.
- Trial 23 remains CLOSED because component trials failed; Trial 23 itself was never evaluated.

## Audit correction

The repair-verification audit inferred that `SECTOR_CONCENTRATION` may have been added in V10.2. Source comparison confirms the gate already existed in V10.0; it appeared in the V10.2 failed-gate list because the changed Trial-21 event set crossed the existing concentration threshold.
