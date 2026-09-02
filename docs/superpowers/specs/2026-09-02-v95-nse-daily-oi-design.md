# V9.5 NSE Daily OI Evidence Design

## Goal
Replace Kite continuous futures OI as the primary historical input for V9.5 Trial 15 with an NSE-native 3–5 year daily futures archive, without changing Trial 15 thresholds, partitions, final-20% lock, or production status.

## Data sources
- Primary historical futures source: official NSE Equity Derivatives bhavcopy archives.
  - Legacy format before 2024-07-08: `foDDMMMYYYYbhav.csv.zip` under the historical DERIVATIVES archive.
  - UDiFF format from 2024-07-08 onward: `BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip`.
- Historical MWPL/ban: official NSE Combined Open Interest and Security-in-ban reports, validation window only.
- Cash-price history: Kite daily cash candles remain the price source for Trial 15; no intraday data is used.
- Kite continuous futures OI remains available only as an optional cross-check, never as the primary OI series when NSE coverage is sufficient.

## Historical futures model
For every trading date, parse all stock-futures contracts and retain:
- symbol
- expiry
- close/settlement price
- reported open interest
- change in open interest when published
- traded volume
- market lot size when published

Aggregate by symbol/date across active expiries. This yields total stock-futures OI and explicit near/next/far OI. Historical F&O membership is inferred point-in-time from the presence of at least one stock-futures contract in that day's official NSE bhavcopy.

## OI normalization
Legacy NSE bhavcopy `OPEN_INT` is treated as the exchange-reported underlying-quantity OI series. UDiFF `OpnIntrst` is preserved raw and accompanied by `NewBrdLotQty`. The loader exposes both raw OI and a normalized share-equivalent series. It never silently multiplies/divides by lot size unless the file format's unit convention is known. When a format/unit cannot be established, the normalization gate remains unavailable rather than fabricating a transform.

For the primary Trial 15 feature, prefer total exchange-reported OI aggregated across expiries. Near/next/far shares and roll ratios are diagnostics, not new Trial-15 gates.

## Expiry and roll controls
Use actual contract expiry dates from the NSE files. Compute DTE and near/next/far classifications from the active expiries each day. This removes the need for a synthetic Thursday/Tuesday expiry calendar when NSE history is available.

## Integrity gates
V9.5 may only move beyond INCONCLUSIVE when:
- NSE historical OI date coverage is at least 95% of expected trading dates in development+validation.
- point-in-time membership is derived from NSE contract presence.
- MWPL/ban coverage is at least 95% of the locked validation dates.
- OI normalization is either established from source semantics or explicitly verified against a second source.
- final 20% remains unread and unfetched for any validation-only control.

## Runtime/cache
Cache each downloaded daily NSE archive under the durable research volume. A date file is downloaded once and reused across all symbols and reruns. Parsing is day-wise and streaming; the entire 3-year raw derivative archive is never held in memory at once.

## Trial 15 invariants
No threshold, split, statistical gate, direction rule, or promotion rule changes. Trial 16 remains locked. `ACTIVE_PLAYBOOKS = ()` remains unchanged.
