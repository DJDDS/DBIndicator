# V7 Frozen changes

- Added one immutable production-candidate rule: `RR_LONG_CATALYST60_15M_NEXTBAR_1D`.
- Added deterministic rule fingerprint to the research result.
- Added exact frozen protocol validation: full live F&O universe, 15m setup/execution, 180 days, 0.08% cost, 0.05% slippage/side.
- Added one-click **Run Frozen V7 Final Test** button that launches that protocol directly.
- Reveals the final 20% only for the frozen rule and only on a valid protocol run.
- Added pre-declared PASS/REJECT gate: N >= 80, avg net >= +0.15%, PF >= 1.20, at least 3/4 final chronological blocks positive.
- Added four-block final-sample stability table.
- Permanently locks every legacy V6 final-test surface, including when the old V6 unlock environment variable is set.
- Leaves current V6 live shortlist behavior unchanged until the frozen V7 final verdict is known.

## 2026-08-29 — Frozen Final button hotfix
- Fixed a browser-side initialization crash caused by `updateUI()` referencing the V7 research button outside its scope.
- The crash happened before the `Run Frozen V7 Final Test` click handler was registered, making the button appear inert.
- The normal backtest controller no longer touches the V7 button.
- The early-research controller now owns both research buttons and disables/re-enables them while a research job is running.
- Frozen rule, fingerprint, protocol, thresholds, and final-sample logic are unchanged.
