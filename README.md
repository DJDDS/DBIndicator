# DBIndicator — NSE F&O Early-Movement Screener

DBIndicator is a Zerodha Kite-connected research and screening dashboard for **NSE stock F&O only**. Its live objective is narrow: surface developing moves early enough to investigate without filling the screen with late, already-extended names.

It does **not** place orders. Best Entries, alerts, stops/targets and research statistics are decision-support only.

## Current live architecture

The old 4-of-4 indicator-voting model is no longer the Best Entries engine. The live path is fixed to **15-minute execution** with a small **4-hour context** check and ranks seven evidence groups:

| Evidence | Weight | Purpose |
|---|---:|---|
| Futures OI velocity / acceleration | 25% | Is fresh positioning appearing now? |
| Compression / BB coil | 20% | Is volatility/range energy stored before expansion? |
| Time-of-day participation | 15% | Is volume accelerating versus the same clock slot historically? |
| Momentum inflection | 15% | Has RSI-vs-RSI-SMA / MACD histogram momentum just turned? |
| Relative-strength acceleration | 10% | Is the stock beginning to lead/lag NIFTY and its sector? |
| Entry structure | 10% | VWAP acceptance, breakout context and anti-chase location |
| Higher-timeframe context | 5% | Small confirmation, not a late-entry driver |

### Three live stages

1. **Energy Building** — compression/BB coil plus evidence beginning to wake up; direction may not be executable yet.
2. **Ignition** — a fresh directional momentum trigger is firing with participation/positioning evidence.
3. **Best Entry** — Ignition plus OI confirmation, adequate evidence coverage, relative-strength/context checks, correct VWAP side and no excessive ATR extension.

The screener may return **zero Best Entries**. That is preferable to manufacturing a shortlist from weak evidence.

### F&O OI handling

Live OI uses the first three stock-futures expiries (near / next / far) where available and tracks recent 15/30/60-minute change plus acceleration. OI is an important participation layer, but it is **not** used alone to predict direction.

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

Open **Backtest → F&O Early Movement Research**. This is the primary live-parity research surface.

It measures two different targets:

- **Energy Building:** after a coil/compression event, did price expand by at least 1 ATR within the next 4 or 8 bars, regardless of direction?
- **Ignition / Best Entry:** after direction appears, what are the net 1/2/3/5/10-bar outcomes when entering at the **next bar open**?

Research includes:

- brokerage/cost + slippage assumptions
- Bullish/Bearish directional returns
- win rate, average/median return, profit factor, average winner/loss
- **30% chronological holdout** (latest events kept untouched)
- component-ablation/lift ranking on holdout
- one-factor threshold sensitivity for compression, 60m OI, TOD RVOL, movement score and RS acceleration
- historical 4-hour context using only the previous fully closed HTF bucket (no look-ahead)
- historical sector context when the mapped NSE sector-index history is available

### Historical OI limitation

Kite historical futures data cannot reconstruct the exact near+next+far OI book for every old timestamp. Therefore historical rollover-period OI is an approximation using Kite's available futures-history series, while **live** OI aggregation remains near+next+far. The Research page states this explicitly.

Use research results to change **one threshold at a time**. Do not promote a rule because it has the highest in-sample win rate. Prefer positive chronological-holdout expectancy after costs, profit factor > 1, enough trades, and stability across directions/regimes.

## Useful live settings

Settings intentionally exposes only the controls that still matter to the live engine plus risk-display settings:

- Maximum entry extension in ATR (default 1.25)
- Maximum Best Entries (a ceiling, not a target)
- Scan interval
- RSI length and RSI smoothing
- MACD live preset (8/17/9 in Auto)
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
