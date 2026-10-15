# V14.0 - NIFTY ACD desk (admin-only, paper, no orders)

Shown on /admin/intraday as the first card. Owner-only APIs: /api/v140/status, /api/v140/export (trades.jsonl).

## Rule (Mark Fisher ACD, 30-minute opening range)
- Levels before the trade: A = 0.15 x ATR10 (mean daily true range of the 10 prior sessions); pivot range from the prior session,
  P = (H+L+C)/3 and M = (H+L)/2 (top = max, bottom = min); opening range = 09:15-09:44 high/low.
- SHORT: 15 consecutive completed 1-minute closes below ORL - A and the latest close below the pivot-range bottom, 09:45-14:29 ->
  sell at the next 1-minute open. LONG mirror above ORH + A and the pivot-range top.
- Stop: distance to the OR midpoint capped at 40 points (always 40 in practice). No target; exit at the 15:20 open.
- First signal of the day only; at most 2 per ISO week (longs and shorts together, as in research).
- SHORT CALLS ONLY: if the day's first signal is LONG there is no trade that day (it still uses a weekly slot, exactly as in the
  tested shorts-only rule). Shorts-only rule: +20.4 / +14.8 / +21.6 per trade, ~0.9/week, max drawdown -307, positive every year.
  (Ignoring longs and waiting for a later short instead was weaker: +17.4 / +9.4 / +23.9, drawdown -507.)
- SHADOW LONG: on LONG-first days the long is simulated with the same entry/stop/exit and recorded (shadow_points in trades.jsonl,
  "shadow LONG" box on the card) - no call, no alert. Longs made +10.1 / +12.6 / -5.7 per signal (2021-24 / 2024-25 / last 13 months);
  no filter tested (trend, gap, prior day, 5-day move, FII, client options) was positive in the last 13 months. Re-enable long calls
  only if the live shadow record turns clearly positive.

## Research (NIFTY 50 index 1-minute bars, Oct 2021 - Oct 2026, gross index points, no costs)
- All signals: 2021-Mar 2024 +14.7/trade (221), Apr 2024-Aug 2025 +11.0 (128), Sep 2025-Oct 2026 holdout +12.0 (95);
  ~1.7 trades/week, 44% win, avg win ~+78, avg loss ~-37, max drawdown -389 points, longest losing run 7.
- SHORT signals +20.4 / +14.8 / +21.6 and positive in every calendar year 2021-26; LONG signals +8.4 / +7.8 / -1.3.
- Tested and rejected (did not hold out of sample): ADX / Supertrend / RSI / TWAP / EMA filters, ATR stops, partial exits and trailing
  (hold to 15:20 beat every exit), walk-forward logistic / tree / GBM formulas, FII and client participant-OI filters.
- Futures costs are not included: ~7 points per round trip until Mar 2026, ~15 points from Apr 2026 (STT 0.05% on the sell side).

## Parity
- The live functions (levels, opening range, signal scan, stop/exit simulation, weekly cap) reproduce the research backtest on the
  full 5-year 1-minute file: 444 of 444 trades, identical side, entry minute and points.

## Experimental lane (logged only, not for trading)
- Expiry days: if ACD fired before 13:00 and NIFTY at 12:59 is >= 50 points from the open in the same direction, a 13:00
  continuation (stop 30, half at +40, runner to break-even, rest out at the 15:20 close). Without look-ahead it made
  +14.9 / +29.0 / -1.2 (holdout, 14 trades), so it is recorded for forward evaluation only.

## Context (not a filter)
- Prior-day NSE participant OI: FII index-futures net and retail-client index-option lean.

## Schedule (IST, weekdays; fail-soft, idempotent per day)
prep 08:30-09:14 (levels, nearest NIFTY future, expiry flags, participant OI; needs Kite login) · every 30 s 09:45-16:30:
1-minute bars -> signal / entry / live P&L / stop / 15:20 exit · Telegram alert on signal and close when Telegram is configured.
Storage: <V12 storage root>/v140/ (day_<date>.json, trades.jsonl).
