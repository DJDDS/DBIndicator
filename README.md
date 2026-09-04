# DBIndicator V11.0.5 — Strict Required-Window Factor Contract

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

## V11.0.5 strict required-window factor contract

- Trial 24 still consumes only IIMA `2010-01` through `2023-05`; the 1993-10 `MF=NA` row is provably outside that window and is ignored for Trial-24 numeric validation.
- The consumer now explicitly requires a **complete** monthly factor window. Any missing calendar month inside `2010-01` through `2023-05` fails closed before `alpha_read_started`.
- Required factors remain exactly `rm_rf`/MF, SMB, HML and RF. WML remains non-load-bearing for the frozen FF3 residualisation.
- No imputation, forward-fill, row deletion inside the window, threshold search, Trial-24 rule change, cost change, or holdout read is permitted.
- The raw IIMA bytes remain hashed for provenance even though irrelevant out-of-window factor values cannot abort the experiment.

## V11.0.4 required-window factor contract

- Trial 24 requests and validates only IIMA factor rows from `2010-01` through the pre-final `2023-05` boundary.
- Rows outside that consumer window remain part of the pinned raw-file SHA/provenance but cannot block a trial that never consumes them.
- Trial 24 requires only `rm_rf`/MF, SMB, HML and RF. WML remains available in the source but is not an FF3 residualisation input and is not a readiness gate.
- Missing or non-numeric required values *inside* the requested window still fail closed with exact month/raw-value diagnostics.
- Compact `YYYYMM` month keys are parsed explicitly, avoiding ambiguous generic date parsing.
- Trial-24 scoring, 0.36% stress cost, feasibility gate, alpha-read boundary, final 20% lock, and V10.2.2 live scanner are unchanged.

## V11.0.3 IIMA numeric-format integrity hotfix

- Bounded Trial-24 input-parser repair only; the preregistered Trial 24 specification is unchanged.
- Production `MF` remains mapped directly to `rm_rf`; `RF` is never subtracted from `MF`.
- Legitimate numeric formatting is normalized: whitespace/NBSP, `%` suffix, comma grouping, and Unicode minus signs.
- Missing/sentinel/non-numeric factor values are never imputed or silently dropped; parsing fails closed with the exact factor column, month, and raw value.
- The pinned IIMA release/source hash, FF3 regression, 12-1M formation, monthly rebalance, decile construction, 0.36% stress cost, feasibility gate, alpha-read boundary, final 20% lock, and live V10.2.2 scanner are unchanged.
