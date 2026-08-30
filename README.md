# DBIndicator — NSE F&O Evidence-Locked Screener
## Institutional V8.2 — Dual Alpha + Derivative Intelligence

V8.2 keeps V8.1's evidence-locked Bull Top-3 / Bear Pressure Top-3 underlying selection unchanged and adds a **live derivative-expression layer** after selection. The option layer never changes the stock rank. For the strongest bullish and bearish names it inspects the nearest live stock-option chain, chooses a liquid near-ATM CE/PE, estimates IV and Greeks, compares IV with 20-session realized volatility, measures ATM-straddle priced move, bid/ask spread, DTE and option liquidity, then labels the expression as **OPTION BUYER EDGE / UNDERLYING GOOD - OPTION EXPENSIVE / PREMIUM RICH - DEFINED-RISK SELLING BIAS / UNDERLYING ONLY-WAIT**.

Historical option-chain claims are deliberately avoided: Kite does not supply the point-in-time historical chain needed for an honest options replay. V8.2 therefore writes live option evidence to `option_shadow.jsonl` for forward validation while the existing Backtest page continues to validate the underlying Bull/Bear engine. IV/Greeks are Black-Scholes estimates from live quotes and should be read as decision-support, not exchange-provided Greeks.


V8.1 is the production-candidate architecture derived from the 90/180-day evidence review. It deliberately removes the sparse `Alpha >= 85` trade gate and does not mirror the bullish formula into bearish trades.

**Bull engine:** 15-minute Recent-Range upside escape, fixed Participation quality floor, then point-in-time cross-sectional ranking by Bull Alpha. Predeclared breadth is Top 1 / Top 3 / Top 5 for research; **Top 3 is the operational live candidate set**.

**Bear engine:** any genuine bearish breakout source can enter the candidate pool. Ranking is by **Bear Pressure = median(Participation, Relative Weakness, direction-aware Derivatives, close-near-low acceptance)**. Bullish Structure is intentionally excluded. The same predefined Top 1 / Top 3 / Top 5 breadth is reported, with **Pressure Top 3** as the operational live set.

Both sides keep the fixed 70 WATCH-quality / participation floor and the existing 1.25 ATR anti-chase guard. These are not re-optimized from the 180-day result. 4H remains context only.

The Backtest page now reports full four-block chronological validation tables for every primary Top-K variant and keeps the final 20% locked. The rejected V7 one-shot final is retired from the UI and is no longer recomputed by normal V8.1 research runs.

The live dashboard, `shortlist_rank`, swing ranks and alerts now use the same V8.1 operational decisions. Old V6 shortlist cards are removed from the production dashboard; V6 code remains only for legacy research/audit compatibility.

See `V8_1_CHANGELOG.md`.

DBIndicator is a Zerodha Kite-connected research and screening dashboard for **NSE stock F&O only**. Its live objective is narrow: surface developing moves early enough to investigate without filling the screen with late, already-extended names.

It does **not** place orders. Best Entries, alerts, stops/targets and research statistics are decision-support only.

## Legacy V6/V7 research retained for audit compatibility only

The items below describe older V6 research fields that remain in the codebase for diagnostics. They no longer populate the production shortlist or alert path:

1. **Stock in Play / Energy Building** — catalyst-like gap/range activity, time-of-day participation and cross-sectional turnover. Direction is optional.
2. **Recent-Range Setup** — price itself reveals direction by escaping the recent six-bar decision range.
3. **Sponsored Recent-Range** — TOD volume plus either OI confirmation or expanding futures basis. OI disagreement is not a universal veto.
4. **V6 Intraday Entry** — Recent-Range + turnover/catalyst + sector/stock leadership + price location + sponsorship + anti-chase, refined by 5-minute execution quality when available.
5. **V6 Swing 1–2D** — currently long-only until the short model clears its own benchmark; requires retention/retest, 4H context and non-opposing sector context.

Opening-range and compression breakouts remain radar/research observations. RSI/MACD are diagnostics rather than live direction generators. The screener may legitimately return **zero entries**.

### F&O OI handling

Live OI uses the first three stock-futures expiries (near / next / far) where available and tracks recent 15/30/60-minute change plus acceleration. In V6 it is a **soft sponsorship feature**: strong volume plus an expanding futures basis can sponsor a setup even when OI is missing or unhelpful.

The OI Screener intentionally shows the current stock-F&O universe whenever valid live OI exists. “Unusual OI only” is optional; a z-score is supporting evidence rather than a hard requirement for the base radar.

## Parameters intentionally removed from live Best Entries

These can remain in legacy diagnostics/backtests, but they do not drive the current live shortlist:

- 4-of-4 / indicator-count voting
- generic candlestick-pattern gate
- big-candle gate
- strong-close gate
- delivery percentage as directional evidence
- breadth as a mandatory entry filter
- generic ADX regime as an entry gate
- fixed/static relative-volume vote
- BTST/STBT recommendation engine

BTST/STBT is research-only because the broad overnight tests did not demonstrate positive net expectancy after costs.

## Backtesting and improvement workflow

Open **Backtest → F&O Stock-in-Play & Breakout Research**. The primary research uses 15-minute execution and reports real intraday horizons (**30m / 1h / 2h / 4h / EOD**) plus swing horizons (**1D / 2D**).

The focused **Recent-Range Edge Lab** compares motivated variants only: bullish/bearish Recent Range, TOD volume, OI, 4H, no-chase, VWAP proximity, one-bar retention and retest entries. It does not brute-force thousands of combinations.

Research includes:

- brokerage/cost + slippage assumptions
- **60% development / 20% validation / 20% locked final-test split** for V6
- the older 30% holdout diagnostics remain visible for legacy comparison
- profit factor and net expectancy
- MFE / MAE and time-to-ATR diagnostics
- OI/4H/VWAP raw coverage diagnostics
- Research / Promising / Benchmark promotion status
- next-executable-bar entries for both first-escape and confirmation-bar variants

### Historical OI limitation

Kite historical futures data cannot reconstruct the exact near+next+far OI book for every old timestamp. Therefore historical rollover-period OI is an approximation using Kite's available futures-history series, while **live** OI aggregation remains near+next+far. The Research page states this explicitly.

Use research results to change **one threshold at a time**. Do not promote a rule because it has the highest in-sample win rate. Prefer positive chronological-holdout expectancy after costs, profit factor > 1, enough trades, and stability across directions/regimes.

## Useful live settings

Settings intentionally exposes only the controls that still matter to the live engine plus risk-display settings:

- Maximum entry extension in ATR (default 1.25)
- Maximum Best Entries (a ceiling, not a target)
- Scan interval
- risk/ATR stop and position-sizing display inputs

BB/compression, OI acceleration, TOD RVOL, RS/context and fresh-trigger quality are calculated automatically.

## Zerodha login

Kite requires a fresh authenticated access token each trading day. Open the dashboard and use **Login to Kite** each morning. The app deliberately does not store your Zerodha password or 2FA secret.

Required Railway/environment variables typically include your Kite app credentials (see `.env.example`). Never commit `.env`, access tokens, passwords or personal secrets to GitHub.

## Run locally

```bash
python -m pip install -r requirements.txt
python run.py
```

Then open the local URL printed by Flask.

## Run tests

From the project root:

```bash
PYTHONPATH=. pytest -q
python -m compileall -q app run.py
```

## Railway deployment

Railway can auto-deploy from the GitHub repository's `main` branch. After replacing the project files in your local GitHub Desktop clone:

1. Review the changed files.
2. Commit to `main`.
3. Click **Push origin**.
4. Wait for Railway to show the new deployment as **Active**.
5. Log into Kite and smoke-test Early Radar, Best Entries, OI Screener and F&O Early Movement Research.

## Safety / interpretation

A high Movement Score is not a probability of profit. OI can reflect hedging/arbitrage, compressed stocks can break either way, and options can lose even when the underlying direction is correct because of IV, spread and theta. Use the system as a research/shortlisting tool and validate changes out-of-sample before risking capital.
## V8.2.1 fast backtest
The primary V8 evidence-locked backtest now bypasses legacy V6 diagnostics and reports four explicit progress stages. Use **Legacy / 4H Diagnostic** only when you intentionally want the older research tables.

## V8.2.2 Stage-3 fast validation
The primary V8 backtest now computes Top-1/Top-3/Top-5 in one contemporaneous ranking pass per direction and omits audit-only V8 ablations. This is a performance-only change; trading logic and validation rules are unchanged. Legacy / 4H Diagnostic retains the full audit calculations.
