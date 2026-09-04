# V10 Research Ledger

## Permanent locks

- Trial 18: LOCKED.
- Trial 19: CLOSED — association, not incremental.
- Trial 20: CLOSED — log-RV integrity closure confirmed rejection.
- Trial 23: CLOSED — component trials failed; Trial 23 itself was NEVER EVALUATED. Any redesigned combination requires a new trial number.
- Final 20% of the V10 directional window remains UNREAD.
- Live Opportunity Radar remains unchanged.

## Trial 21 — Hierarchical Residual Strength

Status: `DAILY_SPECIFICATION_REJECTED_FAMILY_NOT_GLOBALLY_REJECTED`.

Both observed validation reads are retained:

| Read | Sector histories loaded | Bull events/days | Bull net | Bear events/days | Bear net |
| --- | ---: | ---: | ---: | ---: | ---: |
| V10.0 | 15 | 586 / 145 | -0.345% | 284 / 119 | -0.101% |
| V10.2 | 10 | 564 / 142 | -0.338% | 252 / 118 | -0.267% |

Cause status: `CONFIRMED_RUNTIME_SECTOR_PANEL_DEPENDENCY`. Trial 21 constructs residuals only when both the market and mapped sector history are non-empty. A thinner runtime sector panel therefore changed residuals and the event set. Trial 22 does not consume sector history and remained bit-identical.

The V10.2 read is a second read and is not allowed to overwrite the V10.0 read. Neither read is promoted; both reject the registered one-day specification. The residual-momentum family remains untested at its published longer horizon.

Audit note: `SECTOR_CONCENTRATION` was already present in V10.0 source; its appearance in V10.2 reflects the changed event set, not a new gate.

## Trial 22 — Carry-Normalized Futures Basis Innovation

Status: `ABSOLUTE_BASIS_EVENT_SPEC_REJECTED_CROSS_SECTIONAL_HYPOTHESIS_UNTESTED`.

V10.2 reconciliation is bit-identical to V10.0 for the registered event set. The event/day weighting split is retained as hypothesis-generating evidence only; it cannot be used as a prior effect magnitude for a new trial.

A redesigned fixed-count cross-sectional basis trial requires an independent external prior effect magnitude and must pass the feasibility gate before registration.

## V10.2.1 integrity rules

- New alpha rereads of Trials 21/22 are disabled.
- Completed V10.2 summaries migrate read-only.
- Legacy raw event rows are not reconstructed. Future trials must persist exact validation event artifacts and hashes at first execution.
- Sector panels, membership, lot-size inputs, universe, cost model and gate battery are versioned/hashed.
- Incomplete required sector panels fail closed.
- Legacy registered estimand: event-weighted net, with historical gate verdicts frozen.
- Matching diagnostic added: date-clustered t on the same event-weighted mean.
- Future primary estimand: day-weighted fixed-capital net return with matching inference.
- Feasibility t-bar must be named; `DO_NOT_RUN` is binding and refuses registration.
