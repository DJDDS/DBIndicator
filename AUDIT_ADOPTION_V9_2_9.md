# V9.2.9 — External Validation Audit Adoption

This release reviewed `DBIndicator-Model-Validation-Audit.pdf` and separates changes that are safe measurement/protocol hardening from changes that would create a new trading hypothesis.

## Adopted in V9.2.9

- Stage-2 full-universe pipeline reliability and rank-level resume checkpoints.
- Net forward expectancy and net profit factor after the fixed 0.18% research friction.
- 95% Wilson confidence intervals beside forward net win rate, plus number of distinct trading days.
- Explicit historical trial accounting: 12 prior trials, family-wise alpha 0.05, Bonferroni reference alpha 0.004167.
- Reference sample-size warning: approximately 782 independent trades for 55% vs 50% at 80% power.
- Bull diagnostic evidence de-duplication: `Price up + OI up` and `Long Buildup` are one stream, not two confirmations.
- Bear FSB broad fresh-short candidate compaction with frozen thresholds applied exactly once at the immutable freeze boundary.
- Explicit survivorship-bias disclosure because point-in-time F&O membership is not available in the current data source.

## Deferred deliberately

- Deflated Sharpe / FDR: useful, but not claimed until a calibrated return/trial protocol is implemented.
- Point-in-time F&O universe reconstruction: cannot be manufactured from today's Kite instrument list; requires a historical membership dataset.
- Historical MWPL/ban exclusion: requires point-in-time ban/MWPL data.
- Short-squeeze reversal term: this is a new Bear hypothesis and must be researched separately rather than inserted into a rejected model.
- A new asymmetric Bear model: Bear FSB remains rejected; no sign-flipped replacement is activated.
- Running-minimum Bear entry replacement: the rejected rule remains frozen for audit continuity; a replacement would be a new preregistered hypothesis.

## Unchanged

- `ACTIVE_PLAYBOOKS = ()`.
- 1.25 ATR anti-chase ceiling.
- 15-minute execution / 4H context architecture.
- Bull final 20% remains locked.
- Bear FSB consumed final remains rejected and immutable.
