# V10.2 Research Integrity & Feasibility Repair Implementation Plan

**Goal:** Make Trials 21/22 auditable on consistent weighting/cost bases, close Trial 23 correctly, and add a pre-trial feasibility gate without spending new alpha data or reading the final holdout.

**Architecture:** Extend the research-only V10 reporting layer with event/day weighted gross/net diagnostics and clustering density measures. Add a generic feasibility module that rejects a proposed trial before registration when its prior net effect is non-positive or smaller than its minimum detectable effect. Preserve all Trial 21/22 signals, gates, costs, date split, final lock, and live Opportunity Radar logic.

**Tech Stack:** Python, pandas, NumPy, pytest, Jinja/vanilla JS.

**Global Constraints:**
- No new alpha trial or new threshold.
- Final 20% remains unread.
- Trial 21/22 registered verdicts are not reinterpreted as passes.
- Trial 23 is CLOSED because its component trials failed; it was never evaluated.
- Live Opportunity Radar and ACTIVE_PLAYBOOKS remain unchanged.
- ROUND_TRIP_COST remains 0.18% for the already-run Trials 21/22.

### Task 1: Reporting basis repair
- Add event/day weighted gross/net means, events-per-day distribution, naive t, clustered t ratio, approximate within-day correlation, and effective clustering unit.
- Preserve existing gate calculations and verdicts.
- Add focused regression tests.

### Task 2: Research record semantics
- Record Trial 21 as daily specification rejected; family not globally rejected.
- Record Trial 22 as absolute basis-event specification rejected; true cross-sectional hypothesis untested.
- Close Trial 23 as component trials failed / not evaluated.
- Add tests and UI copy.

### Task 3: Pre-trial feasibility gate
- Implement prior-effect, cost, MDE, and normal-approximation power calculations.
- Fail closed when prior effect magnitude is absent, prior net effect <= 0, or MDE exceeds prior net effect.
- Add retroactive V10 feasibility diagnostics without consuming new data.

### Task 4: Release integration and verification
- Update build markers/changelog/research ledger.
- Run V10-focused tests, full regression batches, Python compilation, JS syntax, clean ZIP extraction, and packaged-artifact smoke tests.
