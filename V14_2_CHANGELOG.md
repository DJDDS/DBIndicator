# V14.2 - ACD desk: participant positions table and NIFTY futures prices

Additions to the V14.0 NIFTY ACD card on /admin/intraday (admin-only, paper). No rule change.

## Participant positions table (information only - not a filter)
- From the previous session's NSE participant OI file: Retail (client), FII, Pro and DII rows with index-futures net (long - short)
  and % long, index calls net and index puts net, each with its change from the session before (two NSE files fetched at prep).
- Research reminder: retail is net bullish in index options on ~84% of days; as a filter it added nothing beyond "shorts only".

## NIFTY futures prices on each call
- The desk records the near-month NIFTY future's 1-minute open at the same minute as the index entry and exit (fut_entry,
  fut_stop, fut_exit, fut_points), shown on the card, in the recent-trades table, in the export, and as a total on the future.
- Signals and the stop still run on the NIFTY index exactly as researched; the futures figures show what the traded instrument did
  (premium/basis included). Fail-soft: if the futures bars are unavailable the index trade is unaffected.
