# V11.0.3 — IIMA Numeric-Format Integrity Hotfix

Research build: `2026-09-04-INSTITUTIONAL-V11.0.3-IIMA-NUMERIC-FORMAT-INTEGRITY-HOTFIX`

- Bounded Trial-24 factor-value parser repair only; no alpha specification changes.
- Keeps production `MF -> rm_rf` semantics exactly; `RF` remains separate and is not subtracted again.
- Normalizes whitespace and non-breaking spaces, `%` suffixes, comma grouping, and Unicode minus characters before numeric conversion.
- Preserves raw factor cells by reading CSV with default NA coercion disabled, so missing/sentinel tokens can be audited rather than erased.
- Any factor value that remains non-numeric fails closed with the exact factor column, month, and raw value. No imputation and no silent deletion of dated months.
- Pinned IIMA release/URL/hash provenance, 36-month FF3 regression, 12-1M residual momentum, monthly rebalance, deciles, 0.36% stress spread cost, feasibility gate, alpha-read boundary, and final 20% lock are unchanged.
- The V11.0.2 production failure occurred during factor parsing before `alpha_read_started`; it did not consume Trial-24 outcome data.
- Live V10.2.2 scanner behavior remains unchanged.
