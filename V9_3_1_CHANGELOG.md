# V9.3.1 — V9.3 Isolation Hotfix

Build: `2026-09-01-INSTITUTIONAL-V9.3.1-V93-ISOLATION`

- Fixes a V9.3 orchestration bug where every `v93_lab` run also executed the V9.2 goal-focused report before running the Component Edge Laboratory.
- V9.3 and V9.2 are now mutually exclusive research modes: V9.3 runs only Component Edge + Trial 13; V9.2 runs only when the user explicitly clicks its diagnostic button.
- Research progress now includes the active job label (`V9.3 Anticipation Lab`, `V9.2 Diagnostic Reset`, or `Legacy 4H Diagnostic`) so the server-side job cannot be mistaken for another mode.
- Backtest copy now identifies V9.3 as the primary research architecture and V9.2 as manual diagnostic-only.
- No strategy thresholds, evidence gates, costs, final-sample locks, swing routing, or production playbook eligibility changed.
