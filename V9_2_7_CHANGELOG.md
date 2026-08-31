# V9.2.7 — Regime + Forward Validation

Build: `2026-08-31-INSTITUTIONAL-V9.2.7-REGIME-FORWARD-VALIDATION`

- Filter live F&O underlyings against the NSE cash instrument map; removes stale/non-stock names such as NIFTYFPI before scanning.
- Settings schema 4 migrates any persisted `MAX_ENTRY_EXTENSION_ATR > 1.25` back to 1.25 and caps future edits at 1.25.
- Replace OI-count-only market bias with six-factor regime scoring: NIFTY, price breadth, OI breadth, sector breadth, relative-strength distribution and VWAP participation. Missing factors reduce coverage instead of voting neutral.
- Keep market regime as an opportunity-ranking bonus only; never a veto and never a production-playbook bypass.
- Persist first top-5 Bull/Bear daily opportunity events and resolve 30m/1h/2h/4h/1D direction-adjusted forward returns.
- Add Dashboard forward-validation summary and raw-event export endpoint.
- No rejected V9 playbook was re-enabled. `ACTIVE_PLAYBOOKS` remains empty.
