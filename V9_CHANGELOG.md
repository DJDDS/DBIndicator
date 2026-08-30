# V9 Professional Playbook Scanner

## Why V9 exists
The 90/180-day V8 experiments showed that generic breakout ranking and Bear Pressure Top-K were not robust enough for production. V9 changes the unit of research from a universal score to explicit professional-style playbooks.

## Added
- Bull Opening Drive.
- Bull Pullback/Reclaim.
- Bull Catalyst Continuation using real live news/event matches only.
- Bear Fresh Short Buildup with explicit price-down/OI-up positioning.
- Bear Failed Breakout with following-bar failure confirmation and VWAP rejection.
- Bear VWAP Retest Failure.
- Independent 30m/1h/2h/EOD/1D/2D historical report per backtestable playbook.
- 60/20/20 chronological split and four validation blocks per playbook.
- V9 live shortlist projection used by dashboard and alerts.
- V9 dynamic Bull/Bear dashboard payload and playbook reasons.
- Existing Derivative Intelligence kept downstream of playbook selection.

## Removed from primary fast backtest
- Retired V8 Top-1/3/5 operational report.
- V6 sensitivity/interaction/exit/audit workloads.

Legacy diagnostic code remains available only through the explicit Legacy / 4H Diagnostic path.

## Research integrity
Bull Catalyst Continuation is LIVE/SHADOW until a historical point-in-time event archive exists. Final 20% remains locked until an individual playbook is frozen after adequate validation.
