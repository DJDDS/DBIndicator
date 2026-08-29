# DBIndicator — NSE F&O Early-Movement Screener
## Institutional V6 — current live/research architecture

V6 focuses on NSE stock-F&O intraday and 1–2D swing continuation. Direction comes from a real Recent-Range escape; Stock-in-Play participation, cross-sectional turnover, sector/stock leadership, price location, volume, OI **or** futures-basis sponsorship, 4H context and a bounded 5-minute finalist check determine whether the move deserves promotion. OI is a supporting sponsorship feature rather than a universal hard gate.

Research uses a 60/20/20 chronological split, with the final 20% locked by default, and includes a path-aware first-touch target/stop lab. See `BENCHMARK_RELEASE.md` for the full release logic and promotion rules.


DBIndicator is a Zerodha Kite-connected research and screening dashboard for **NSE stock F&O only**. Its live objective is narrow: surface developing moves early enough to investigate without filling the screen with late, already-extended names.

It does **not** place orders. Best Entries, alerts, stops/targets and research statistics are decision-support only.

## Current live architecture

The live path uses **15-minute setup detection** with an optional **5-minute execution check** only for the best bounded finalist set:

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