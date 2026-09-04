# V11.0.1 — IIMA Factor Schema Hotfix

Research build: `2026-09-04-INSTITUTIONAL-V11.0.1-IIMA-FACTOR-SCHEMA-HOTFIX`

- Bounded input-schema repair only; Trial 24 research specification is unchanged.
- The pinned IIM Ahmedabad monthly factor parser now maps `Market Premium` / `Market Premium %` to the required `rm_rf` market-minus-risk-free factor field.
- Missing-column failures now include the received CSV headers for auditability.
- The pinned factor release, URL, hashing, FF3 regression, 12-1M formation, monthly rebalance, decile construction, 0.36% stress spread cost, feasibility gate, alpha-read boundary and final 20% lock are unchanged.
- The V11.0 production failure occurred before `alpha_read_started`; therefore the preregistered Trial 24 outcome has not been consumed and may be run once after this repair.
- Live V10.2.2 scanner behavior is unchanged.
