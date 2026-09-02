# V9.6.1 — Trial 17 Integrity Completion

Build: `2026-09-02-INSTITUTIONAL-V9.6.1-TRIAL17-INTEGRITY-COMPLETE`

- Trial 17 remains frozen at total FUTSTK OI z >= 1.5 on 2021-09-01 through 2023-09-01.
- Historical F&O membership is derived point-in-time from official NSE FUTSTK contract presence.
- Historical cash OHLC is loaded from official NSE CM bhavcopy, removing dependence on today's Kite cash-token universe.
- Historical cash outcome coverage is a separate fail-closed integrity gate.
- MWPL loader falls back to legacy direct NSE `combineoi_DDMMYYYY.csv` / `nseoi_DDMMYYYY.csv` archives when the modern report API cannot serve an old date.
- Historical security-ban lookup has direct `archives/fo/sec_ban/fo_secban_DDMMYYYY.csv` fallback.
- Trial 18 remains locked; prior Trial 13/15 finals remain untouched.
- `ACTIVE_PLAYBOOKS = ()` remains unchanged.
