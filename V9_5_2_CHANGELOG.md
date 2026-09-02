# V9.5.2 — NSE Daily OI Evidence

**Build:** `2026-09-02-INSTITUTIONAL-V9.5.2-NSE-DAILY-OI-EVIDENCE`

V9.5.2 changes the historical data/integrity layer only. Trial 15 thresholds, the 60/20/20 date split, the locked final 20%, the non-rescuing 2D rule and Trial 16 lock are unchanged.

- Official NSE F&O data is now the primary 3+ year historical stock-futures OI source. The compact F&O Market Activity contract-wise futures report is preferred; legacy and UDiFF bhavcopies are date-appropriate fallbacks normalized into the same contract-wise schema.
- Trial 15 remains on reconstructed **near-month share-equivalent OI** so the hypothesis is not silently changed. Total, next and far OI are diagnostics.
- Actual exchange expiries drive days-to-expiry; the old derived expiry calendar is not used on this path.
- UDiFF `OpnIntrst` is retained in its published underlying-quantity units; `NewBrdLotQty` is used only to derive an OI-contracts diagnostic. Legacy `OPEN_INT` is likewise treated as underlying-share-equivalent OI, and historical lot is inferred from futures turnover when available.
- Historical FUTSTK presence supplies point-in-time membership and the archive pass can discover members outside today's F&O list. Incomplete cash-price coverage fails the membership integrity gate.
- Official NSE Combined Open Interest / security-ban inputs are loaded only for the frozen validation dates. The final 20% is never requested for MWPL inspection.
- Kite historical OI is no longer a fallback for Trial 15; Kite remains the daily cash-price/live cross-check source.
- NSE Market Activity/bhavcopy downloads and completed per-symbol frames are cached under the research volume for restart recovery.
- Trial 16 stays `LOCKED`; `ACTIVE_PLAYBOOKS = ()` stays unchanged.
