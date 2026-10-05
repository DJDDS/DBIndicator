# V13.0 — Swing Desk (admin-only spotting engine)

**Owner-only. Spotting and recording only: no orders are ever placed.** Members get 403 on every V13 route.

## What it does
An evening F&O swing signal built from the Oct-2026 research (ledger Rounds 46–70), running inside kite-scanner:

1. **Fear gate**: a committee of 17 frozen PySR formulas on VIX, breadth and market drawdown. Open when the score is at or above the selected quantile (mode G = 50% of evenings).
2. **Model score**: the frozen research LightGBM ("which" model, 53 features), read with a dependency-free numpy tree reader. LightGBM is **not** added to requirements.
3. **Picks**:
   - mode G: top-2 on open-gate evenings, plus the top-1 on closed-gate evenings if it is BIG.
   - mode A: conservative.
   - mode H: G plus a 3rd pick in expiry week.
4. **Filters**: each rejected stock is shown with its reason.
   - F&O ban tomorrow.
   - Quiet stock (|5-day move| < 3.8%).
   - Results inside the hold, or within 7 days after results.
   - Bad-news veto (NSE announcements: promoter/insider/takeover filings or legal/regulatory items in 3 days, or news flow ≥ 3× normal).
   - 52-week low while Nifty is above its 200-DMA.
   - Already holding the stock.
   - Max open positions.
5. **Tags and routing** (separate **Futures / Cash / Options** lanes on the page):
   - EXPIRY → futures, plus an optional call.
   - BIG → call.
   - NORMAL → futures, or cash by setting.
   - Crash brake: half size when Nifty fell more than 8% in 10 days.
6. **Concrete levels**:
   - Futures/cash: target +6%, stop on the close at −12%, early exit at the first close at a 7-day high, time exit after 15 sessions. Levels are provisional from the evening close and reset to the actual open at 09:20.
7. **Option instruction** (live at 10:00 on the entry day):
   - Kite option chain, expiries with ≥ 15 trading days, strikes ATM/OTM1/OTM2.
   - Rejects spreads > 2.5%, low OI, or IV/RV > 1.3.
   - Picks the best PySR value score `log(VIX × sig20 / IV)`, preferring OTM1 within 0.02.
   - Output: contract, limit (mid), max price, target (+50%), stop on close (−50%), exit by session 8, lots.
8. **Spotting ledger**: entries, option instructions and exits are recorded each day, with a scorecard by instrument.

## Schedule (IST, trading days, fail-soft)
- 18:45–22:30: evening plan.
- 09:20: entry at the open.
- 10:00–11:15: option instructions.
- 15:35: outcomes.

## Routes (all `require_roles(OWNER)`)
- `GET /admin/v130`
- `GET /api/v130/status`
- `POST /api/v130/settings`
- `GET /api/v130/ledger/export`
- `GET /api/v130/plans/export`
- `POST /api/v130/run-evening` (refused during market hours)

## Verification
- **Feature parity:** the ported feature code reproduces the research features on 68,644 stock-days (Jun-2025 → Sep-2026). 52 of 53 features are identical; `res_since` differs only by its 60-day cap, which the model applies. Model score rank correlation is 1.0, and the top-2 is identical on 100% of evenings.
- **Gate parity:** every gate input is identical; open/closed agrees on 99.7% of evenings.
- **Rules parity:** the selection reproduces the research variant G on 1,905 of 1,908 evenings (99.8%). The 3 differences are dates with missing VIX.
- **Model reader:** max abs difference vs LightGBM is 0.0 on 3,000 rows (test fixture `tests/v130_model_reference.npz`).
- **Tests:** `tests/test_v130.py`, 11 tests. The full suite has the same 10 pre-existing failures as `main`, and nothing new fails.

## Storage
`<V12 storage root>/v130/`: plans.jsonl, ledger.jsonl, state.json, settings.json (on the Railway volume when mounted).

## Not in this patch (next)
Intraday paper engines (09:45 short detector, pre-open gap-down basket) and the pre-open imbalance recorder.
