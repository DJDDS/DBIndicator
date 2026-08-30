# V9 Professional Playbook Scanner Design

## Goal
Build one NSE F&O screener that serves the user's actual workflow: find actionable bullish and bearish stocks for intraday and 1–2 day trades, then use V8.2 Derivative Intelligence to decide whether options are suitable.

## Principles
- Do not create one universal Bull/Bear composite score.
- Detect specific repeatable playbooks with explicit economic meaning.
- Bullish and bearish logic are independent.
- 15-minute is the signal timeframe; 4H is context only.
- OI/basis are evidence, never universal vetoes.
- Live real catalyst/news is distinct from historical price-volume catalyst proxies.
- V8.2 option intelligence remains an expression layer after the underlying playbook ranks are frozen.
- Historical final 20% stays locked.

## V9 playbooks
1. Bull Opening Drive — early-session opening-range escape with abnormal participation, strong close acceptance, relative leadership and anti-chase.
2. Bull Pullback/Reclaim — bullish breakout followed by a one-bar retest/reclaim that closes back above the escaped level; entry only after confirmation.
3. Bull Catalyst Continuation — live-only/shadow until a point-in-time historical event archive exists; real matched headline plus bullish price/participation confirmation.
4. Bear Fresh Short Buildup — downside event with price down + OI up, abnormal selling participation, relative weakness, and non-improving futures basis.
5. Bear Failed Breakout — prior bullish breakout fails back inside its escaped level, below VWAP, with bearish acceptance; trade direction becomes bearish only after failure confirmation.
6. Bear VWAP Retest Failure — bearish breakout followed by a retest that probes the level/VWAP region and closes back below, with selling evidence.

## Backtest
- Full NSE stock-F&O universe, 15-minute, fixed 180-day primary protocol.
- Entry is next executable 15-minute bar after each playbook's confirmation point.
- Report 30m, 1h, 2h, EOD, 1D, 2D separately per playbook.
- 60% development / 20% validation / final 20% locked.
- Validation shows N, win rate, avg net, PF, MFE/MAE, and four chronological blocks.
- Catalyst Continuation must show LIVE/SHADOW rather than a fabricated historical backtest when no event archive is supplied.

## Live selection
- Each row may match one or more V9 playbooks.
- Rank candidates inside each playbook by transparent evidence consensus.
- Operational dashboard presents best Bull and Bear opportunities, playbook name, evidence score, TRADE/WATCH state, and derivative-expression verdict.
- Alerts and shortlist fields must be populated from V9, not V8.1 legacy Top-K.

## UI
- Main console title: V9 Professional Playbook Scanner + Derivative Intelligence.
- Intraday and 1–2D tabs remain.
- Cards show playbook, score, participation, relative strength/weakness, derivatives/OI state, VWAP/extension context, reasons, and option-expression panel.
- Backtest page has a dedicated Run V9 Playbook Backtest button and playbook-by-playbook results; Legacy diagnostics remain separate.
