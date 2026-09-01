# V9.3.2 — Research UI Isolation

Build: `2026-09-01-INSTITUTIONAL-V9.3.2-RESEARCH-UI-ISOLATION`

## Fixes
- V9.3 Anticipation Lab now owns its progress, error, status, and result area inside the V9.3 card.
- V9.2 Diagnostic Reset is explicitly labelled a manual legacy diagnostic and owns a separate progress/error/result area.
- 4H Diagnostic is a separate card with its own progress/error/status and legacy diagnostic results.
- The shared backend research job is routed by `research_mode`; only the owning card can render the current job state.
- V9.3 can no longer visually appear to be a V9.2 run simply because the shared status object is active.

No strategy thresholds, evidence gates, costs, final-sample locks, Trial 13 rules, or production playbooks changed.
