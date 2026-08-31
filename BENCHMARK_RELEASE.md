# V9.2 Diagnostic Reset Release Notes

Build: `2026-08-30-INSTITUTIONAL-V9.2-DIAGNOSTIC-RESET`

V9.2 answers two post-final-test questions without adding a new tunable strategy:

1. Why did **Bull Institutional Accumulation** produce zero validation candidates? The Bull Gate Funnel counts the exact cumulative population loss at every unchanged gate.
2. Why did **Bear Fresh Short Buildup** pass validation but fail its consumed final 20%? The Bear regime decomposition compares validation and final across available market/derivatives contexts.

The Bull final 20% remains locked. The Bear final sample is already consumed and rejected; V9.2 disables its final-test button and treats all Bear regime tables as diagnostic-only.
