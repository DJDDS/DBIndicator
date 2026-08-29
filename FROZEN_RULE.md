# V8.2 Operational / Research Lock

**Production build:** `2026-08-29-INSTITUTIONAL-V8.2-DERIVATIVE-INTELLIGENCE`

## Underlying screener — evidence locked from V8.1

V8.2 does not retune the stock-selection model after seeing the 90/180-day results.

- Universe: current NSE stock-F&O universe from Kite.
- Signal timeframe: 15 minute. 4H is context only.
- Bull pool: genuine 15m Recent-Range upside escapes.
- Bull ranking: cross-sectional Bull Alpha with the pre-existing Participation quality floor.
- Bear pool: genuine bearish breakout events.
- Bear ranking: Bear Pressure = median(Participation, Relative Weakness, direction-aware Derivatives, close-near-low acceptance). Bullish Structure is not mirrored into the bear formula.
- Operational breadth: Top 3 Bull + Top 3 Bear at each point in time.
- Anti-chase: 1.25 ATR extension guard.
- OI/futures basis: supporting evidence, never a universal veto.
- Intraday and 1–2D swing states remain separate.

## V8.2 derivative-expression layer — live/shadow, not historically promoted

The option layer is deliberately downstream of the stock rank. It **cannot promote or demote the underlying candidate**.

For the strongest three bullish and strongest three bearish candidates it reads the live NFO stock-option chain and evaluates:

- nearest live expiry for intraday expression;
- first expiry with at least 3 calendar DTE for 1–2D swing expression;
- near-ATM contracts only (no lottery-OTM promotion);
- live bid/ask midpoint and spread;
- model-estimated IV, delta, gamma, theta and vega;
- 20-session annualized realized volatility and IV/RV ratio;
- ATM-straddle priced move to expiry;
- ATM call/put IV spread;
- ATM volume PCR and OI PCR as **unsigned context only**;
- approximate put/call skew from nearby quoted strikes;
- option volume/OI and liquidity.

Expression labels are decision-support only:

- `OPTION BUYER EDGE`
- `UNDERLYING GOOD - OPTION EXPENSIVE`
- `PREMIUM RICH - DEFINED-RISK SELLING BIAS`
- `UNDERLYING ONLY / WAIT`
- `OPTION DATA INSUFFICIENT`

Kite's normal historical interface does not provide an honest point-in-time historical stock-option chain with the bid/ask/IV surface/signed trade flow required for a true option P&L backtest. Therefore V8.2 **does not fabricate one**.

Instead, every live analyzed option is written to `option_shadow.jsonl`, and registered contracts are forward-marked at 30m / 2h / EOD / 1D in `option_shadow_state.json`. The dashboard shows forward 30m sample/win-rate as evidence accumulates. Export `/api/option-shadow/export` before redeploying if the Railway container has no persistent volume.

No V8.2 option label should be called a validated edge until the forward sample is large and stable enough to justify promotion.

## Retired V7

The former V7 `RR_LONG_CATALYST60_15M_NEXTBAR_1D` final sample was consumed and rejected. It is audit history only and must not be rerun or tuned against the already-seen final data.
