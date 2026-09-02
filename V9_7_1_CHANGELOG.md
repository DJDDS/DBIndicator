# V9.7.1 — Trial 19 MWPL/ban Integrity Closure

- Trial 19 is unchanged: fixed 2018-09-01 to 2021-08-31 window and `total FUTSTK OI z >= 1.5` binary event.
- Adds legacy NSE `nseoi_DDMMYYYY.xml` parsing using the official MWPL/NSE Open Interest field set.
- Supports legacy CSV/XML/ZIP payloads and `combineoi`, `nseoi`, and `ncloi` report generations.
- Probes historical `content/nsccl`, `archives/nsccl`, and F&O archive locations, then remembers the working route to avoid repeated failed requests.
- Adds legacy `fo_secban_DDMMYYYY.csv` archive locations.
- Adds `Accept-Language` and XML content negotiation for NSE archive compatibility.
- Backtest now displays MWPL date coverage and reason directly.
- No threshold, dates, matched-baseline definition, inference hurdle, Trial 18 lock, or production playbook changed.

## MWPL performance hotfix — 2026-09-02

- Corrected the legacy 2018-2021 MWPL archive family to probe NSE's historical `/archives/nsccl/mwpl/nseoi_DDMMYYYY.zip` / `combineoi_DDMMYYYY.zip` files before obsolete CSV/XML paths.
- Legacy `nseoi` is attempted before `combineoi` for pre-2024 dates, eliminating the per-date dead-route probe storm that could leave Trial 19 at the MWPL stage for 30+ minutes.
- Trial-19 now publishes MWPL date progress to the durable job state every five dates.
- Trial 19 threshold, evidence dates, matching, statistics, promotion rules, Trial-18 lock, and `ACTIVE_PLAYBOOKS = ()` are unchanged.
