# DBIndicator — V9 Professional Playbook Scanner

**Build:** `2026-08-30-INSTITUTIONAL-V9-PROFESSIONAL-PLAYBOOKS`

V9 is an NSE stock-F&O research and screening dashboard built around the actual objective: **find high-quality bullish and bearish opportunities for intraday and 1–2 day swing trading, then evaluate whether the option itself is a sensible way to express that underlying opportunity.**

It does not place orders and it does not promise a probability of profit.

## V9 playbooks

### Bullish
- **Bull Opening Drive** — early Opening-Range escape + abnormal participation + relative leadership + strong price acceptance.
- **Bull Pullback/Reclaim** — Recent-Range breakout + confirmed pullback/reclaim + relative strength/VWAP acceptance.
- **Bull Catalyst Continuation** — real live news/event catalyst + bullish participation. This remains live/shadow until point-in-time historical news exists.

### Bearish
- **Bear Fresh Short Buildup** — price weakness + fresh short positioning (price down/OI up) + relative weakness + selling participation + basis evidence.
- **Bear Failed Breakout** — bullish breakout fails back into its range, rejects VWAP and closes with bearish acceptance.
- **Bear VWAP Retest Failure** — breakdown retest fails while relative weakness persists.

V9 deliberately avoids a single generic Bull/Bear composite as the trading thesis. Cross-sectional ranks remain evidence features inside each playbook.

## Timeframes
- Signal generation: **15 minute**.
- 4H: context only.
- Intraday validation: **30m / 1h / 2h / EOD**.
- Swing validation: **1D / 2D**.

## V9 backtest
Open **Backtest → Run V9 Professional Playbook Backtest**. The one-click primary protocol uses:
- full NSE stock-F&O universe;
- 15-minute signals;
- 180 calendar days;
- costs/slippage from the configured research engine;
- 60% development / 20% validation / 20% locked final;
- four chronological validation blocks;
- independent metrics for every playbook.

Bull Catalyst Continuation is not given a counterfeit historical backtest because the application does not possess a clean point-in-time historical event/news archive.

The primary V9 fast path also skips retired V8 Top-K and V6 diagnostic reports. Use **Legacy / 4H Diagnostic** only when intentionally auditing older research.

## Derivative Intelligence
After an underlying V9 playbook qualifies, the downstream option layer inspects live near-ATM stock options and exposes IV/RV, ATM implied move, spread, DTE, Greeks, liquidity, skew/PCR context and an option-expression label.

Historical option-chain P&L is not fabricated. Live option snapshots are forward-marked at 30m / 2h / EOD / 1D and can be exported from `/api/option-shadow/export`.

## Live dashboard
The dynamic decision console separates:
- **Bullish Playbooks**
- **Bearish Playbooks**
- **Intraday**
- **1–2D Swing**

Each candidate shows the matched playbook, score/state, participation, relative performance, derivatives/OI state, reasons, chase/extension information and derivative-expression evidence when available.

## Research discipline
- No RSI/MACD/ADX voting as mandatory trade logic.
- No mirrored bearish = inverted bullish assumption.
- No OI universal veto.
- No threshold sweep after seeing final data.
- No invented dealer-gamma or signed option-flow claims from unsigned public OI.
- No historical option-P&L claim without point-in-time option-chain evidence.

## Run locally
```bash
python -m pip install -r requirements.txt
python run.py
```

## Verify
```bash
PYTHONPATH=. pytest -q
python -m compileall -q app run.py
```

## Deployment
Railway can auto-deploy from your GitHub branch. After deployment, log in to Kite, verify the dashboard build marker, and run **V9 Professional Playbook Backtest** before promoting any individual playbook.

## V9 research reliability
The V9 backtest checkpoints progress/result state to `EARLY_RESEARCH_STATE_PATH` and uses a lower-memory full-universe feature representation. For recovery across a full Railway container replacement, configure `EARLY_RESEARCH_STATE_PATH` on a mounted persistent volume; the default `/tmp` path is best-effort process-restart recovery only.
