# V11.0.2 — Exact IIMA MF Schema Hotfix

Research build: `2026-09-04-INSTITUTIONAL-V11.0.2-IIMA-MF-SCHEMA-HOTFIX`

- Bounded Trial-24 factor-input repair only; the preregistered Trial 24 specification is unchanged.
- The pinned IIM Ahmedabad monthly factor parser now accepts the production header `MF` as the already-excess market factor `rm_rf`.
- `RF` remains a separate risk-free series. V11.0.2 never computes `MF - RF`; doing so would subtract the risk-free rate twice.
- Existing `Market Premium`, `RM-RF`, `MKT-RF`, and other accepted aliases remain supported.
- Missing-column errors continue to report the received production headers for auditability.
- The pinned release, URL, source hashing, FF3 regression, 12-1M formation, monthly rebalance, decile construction, 0.36% stress spread cost, feasibility gate, alpha-read boundary, and final 20% lock are unchanged.
- The V11.0.1 production failure occurred during factor parsing before `alpha_read_started`; no Trial-24 alpha outcome was consumed by that attempt.
- Live V10.2.2 scanner behavior is unchanged.
