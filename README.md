# DBIndicator V11.0.3 — IIMA Numeric-Format Integrity Hotfix

Bounded Trial-24 input repair: the pinned IIM Ahmedabad factor parser accepts the production `MF` header as the already-excess market factor (`rm_rf`) while retaining `RF` separately. RF is not subtracted from MF again. No Trial-24 research rule, cost, holdout boundary, or live scanner behavior changes.

# DBIndicator V10.2 — Research Integrity & Feasibility Repair

Current research build repairs V10 reporting/feasibility only. No new alpha trial is run; Trial 21/22 specifications remain closed, Trial 23 is closed-but-never-evaluated, and the final 20% remains unread.

# DBIndicator — V9.8 Incremental OI Validation

**Build:** `2026-09-03-INSTITUTIONAL-V9.8.0-INCREMENTAL-OI-VALIDATION`

V9.8 preserves the frozen Trial-19 event (`total FUTSTK OI z >= 1.5`), evidence window (2018-09-01 through 2021-08-31), same-day/same-DTE baseline and the V9.7.2 replicated result. It does not retune Trial 19 and does not unlock direction.

The V9.8 layer asks the narrower professional question: does extreme OI add next-session variance information beyond variables that a competent volatility model already knows? It adds next-session Yang-Zhang-style daily variance with Garman-Klass robustness, full HAR daily/weekly/monthly realised-variance controls, abnormal total FUTSTK volume in a joint horse race, and an auditable NSE earnings join with inside/outside ±5-session splits.

Trial 18 remains **LOCKED** and `ACTIVE_PLAYBOOKS = ()` remains unchanged. V9.8 cannot create TRADE/WATCH signals.

Open **Backtest → Run V9.8 Incremental Validation**.

---

# DBIndicator — V9.7.2 Trial 19 Confound & Integrity Closure

**Build:** `2026-09-02-INSTITUTIONAL-V9.7.2-TRIAL19-CONFOUND-INTEGRITY-CLOSURE`

V9.7.2 preserves the frozen Trial-19 event (`total FUTSTK OI z >= 1.5`), evidence window (2018-09-01 through 2021-08-31), same-day/same-DTE baseline and binary two-way clustered inference. It adds only declared confound/integrity controls: monthly MWPL + targeted ban reconstruction, recent-window MWPL sensitivity bound, prior 5-day realised-volatility matching, t-1/t-2 pre-signal diagnostics, and NSE board/result-date ±5-session exclusion.

The replicated planning effect is ~1.13x; the 1.22x discovery estimate is retired for economic projection. Trial 18 remains locked unless the combined promotion gate passes and even then becomes only eligible for preregistration. `ACTIVE_PLAYBOOKS = ()`.

Open **Backtest → Run V9.7.2 Trial 19**.

## V11.0.3 IIMA numeric-format integrity hotfix

- Bounded Trial-24 input-parser repair only; the preregistered Trial 24 specification is unchanged.
- Production `MF` remains mapped directly to `rm_rf`; `RF` is never subtracted from `MF`.
- Legitimate numeric formatting is normalized: whitespace/NBSP, `%` suffix, comma grouping, and Unicode minus signs.
- Missing/sentinel/non-numeric factor values are never imputed or silently dropped; parsing fails closed with the exact factor column, month, and raw value.
- The pinned IIMA release/source hash, FF3 regression, 12-1M formation, monthly rebalance, decile construction, 0.36% stress cost, feasibility gate, alpha-read boundary, final 20% lock, and live V10.2.2 scanner are unchanged.
