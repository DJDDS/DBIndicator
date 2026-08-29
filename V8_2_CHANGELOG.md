# V8.2 Derivative Intelligence Changelog

Build: `2026-08-29-INSTITUTIONAL-V8.2-DERIVATIVE-INTELLIGENCE`

- Preserves V8.1 Bull Top-3 and Bear Pressure Top-3 as the underlying selection engine.
- Splits the live option-analysis API budget equally across bullish and bearish leaders (top three each).
- Adds live stock-option chain analysis without allowing option data to alter the underlying stock rank.
- Uses near-ATM options only; does not promote lottery OTM contracts.
- Uses nearest expiry for intraday and an expiry with at least 3 DTE for the 1–2D swing view.
- Estimates IV, delta, gamma, theta and vega from live bid/ask midpoint using Black-Scholes (model estimates, not exchange-provided Greeks).
- Adds 20-session annualized realized volatility for IV-vs-RV comparison.
- Adds ATM straddle priced move, call-put ATM IV spread, approximate nearby-strike skew, ATM volume PCR/OI PCR, DTE, OI/volume and bid/ask liquidity.
- Treats PCR/OI as unsigned context; it is not mislabeled as buyer-initiated opening flow.
- Adds option-expression labels: OPTION BUYER EDGE, UNDERLYING GOOD - OPTION EXPENSIVE, PREMIUM RICH - DEFINED-RISK SELLING BIAS, UNDERLYING ONLY / WAIT.
- Adds a dynamic Derivative Intelligence block inside each Bull/Bear leader card.
- Adds separate option expression to the Intraday and 1–2D Swing tabs.
- Adds actual live forward option-premium validation at 30m / 2h / EOD / 1D for registered contracts.
- Adds dashboard 30m forward-validation sample/win-rate and `/api/option-shadow/export`.
- Writes raw live option evidence to `option_shadow.jsonl` and forward state to `option_shadow_state.json`.
- Does not fake an historical option backtest; V8.2 derivative evidence remains explicitly LIVE/SHADOW until point-in-time historical option-chain data is available or the forward sample proves itself.
