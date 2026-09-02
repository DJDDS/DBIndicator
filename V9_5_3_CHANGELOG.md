# V9.5.3 Trial 15 Closure + Contract Structure Research

**Build:** `2026-09-02-INSTITUTIONAL-V9.5.3-TRIAL15-CLOSED-CONTRACT-STRUCTURE`

## Trial 15 closure semantics

- Trial 15 thresholds and the 60/20/20 split are unchanged.
- The final 20% remains locked and unread.
- Primary efficacy gates are evaluated before missing secondary integrity controls. A failed 1D lift/CI, volatility-control, tail-sensitivity or chronological-stability gate is now reported as a terminal `FAIL_*` rather than being hidden behind `INCONCLUSIVE_MISSING_MWPL_CONTROL`.
- Missing MWPL/history controls remain disclosed for audit and can block a potential PASS; they cannot rescue or mask a failed feature.
- Trial 16 remains LOCKED and is not auto-run.

## Historical MWPL hardening

- Combined Open Interest ZIP payloads are unpacked before parsing.
- Current `Open Interest` and legacy `NSE Open Interest` column names are accepted.
- The existing Security-in-ban report plus 95% entry / 80% exit state logic is preserved.
- NCL Open Interest report loading is available as a historical-format fallback; the report source remains disclosed.

## Contract Structure Feature Research

A separate research-only layer decomposes point-in-time NSE near/next/far OI into four pre-defined feature states:

- fresh near creation;
- rollover-dominant transfer;
- fresh total OI expansion;
- abnormal OI unwind.

Each feature reports 1D/2D magnitude lift and day-cluster bootstrap intervals on the validation partition only. The final 20% is not read. This feature lab has no trial number and cannot rescue Trial 15, unlock Trial 16, or create a production TRADE/WATCH signal.

## Production safety

`ACTIVE_PLAYBOOKS = ()` remains unchanged.
