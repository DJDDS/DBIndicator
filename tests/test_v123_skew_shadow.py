import datetime as dt
import json

from app import v123_skew_shadow


def _contract(symbol, typ, spot, iv, spread, expiry="2026-10-29", dte=28):
    mid = 10.0
    half = mid * spread / 200.0
    return {
        "underlying": symbol,
        "type": typ,
        "expiry": expiry,
        "dte": dte,
        "strike": round(spot),
        "spot": spot,
        "best_bid": mid - half,
        "best_ask": mid + half,
        "spread_pct": spread,
        "mid_iv_pct": iv,
        "stale": False,
        "quote_stale": False,
        "tradingsymbol": f"{symbol}{typ}",
        "depth": {"buy": [{"quantity": 100}], "sell": [{"quantity": 100}]},
    }


def _slot(day, slot, spots, skews=None):
    contracts = []
    skews = skews or {}
    for symbol, spot in spots.items():
        skew = skews.get(symbol, 0.0)
        contracts.append(_contract(symbol, "CE", spot, 20.0 + skew / 2.0, 2.0))
        contracts.append(_contract(symbol, "PE", spot, 20.0 - skew / 2.0, 2.0))
    return {
        "record_type": "V12_OPTION_SLOT",
        "date": day,
        "slot": slot,
        "ts": f"{day}T" + ("09:30:00" if slot == "OPEN_STABLE" else "13:00:00"),
        "broad_contracts": contracts,
    }


def test_skew_shadow_market_filter_and_one_hour_outcome(tmp_path):
    snapshot = tmp_path / "snap.jsonl"
    state = tmp_path / "state.json"
    ledger = tmp_path / "ledger.jsonl"

    symbols = [f"S{i:02d}" for i in range(20)]
    prev_spots = {s: 100.0 for s in symbols}
    today_spots = {s: 101.0 for s in symbols}  # broad market +1%
    skews = {s: 0.0 for s in symbols}
    skews["S00"] = -8.0
    skews["S19"] = 9.0

    prev = _slot("2026-09-30", "PRE_CAS", prev_spots)
    prev["ts"] = "2026-09-30T15:10:00"
    cur = _slot("2026-10-01", "MIDDAY", today_spots, skews)

    snapshot.write_text(
        json.dumps(prev) + "\n" + json.dumps(cur) + "\n",
        encoding="utf-8",
    )

    now = dt.datetime(2026, 10, 1, 13, 0, 0)
    status = v123_skew_shadow.process_slot(
        snapshot_file=snapshot,
        state_file=state,
        ledger_file=ledger,
        now=now,
        slot="MIDDAY",
    )
    current = status["current"]
    assert current["market_direction"] == "Bullish"
    assert current["eligible_symbols"] == 20
    assert current["selected_count"] == 1
    assert current["selected"][0]["symbol"] == "S19"
    assert current["selected"][0]["side"] == "Bullish"

    live_rows = [{"symbol": s, "close": 101.0} for s in symbols]
    live_rows[-1]["close"] = 102.01  # S19 +1% from 13:00
    status2 = v123_skew_shadow.update_one_hour_outcomes(
        live_rows=live_rows,
        state_file=state,
        ledger_file=ledger,
        now=dt.datetime(2026, 10, 1, 14, 0, 5),
    )
    one = status2["current"]["one_hour_outcome"]
    assert one["outcome_count"] == 1
    assert one["mean_directional_spot_bps"] > 99
    assert one["mean_net_6bps_proxy"] > 93
    assert one["win_rate_pct"] == 100.0

    records = [json.loads(x) for x in ledger.read_text().splitlines()]
    assert any(x["record_type"] == "SKEW_SHADOW_SIGNAL" for x in records)
    assert any(x["record_type"] == "SKEW_SHADOW_60M" for x in records)


def test_skew_shadow_prefers_next_expiry_inside_three_dte():
    symbol = "TEST"
    snaps = [
        _contract(symbol, "CE", 100, 25, 2, expiry="2026-10-02", dte=1),
        _contract(symbol, "PE", 100, 15, 2, expiry="2026-10-02", dte=1),
        _contract(symbol, "CE", 100, 23, 2, expiry="2026-10-29", dte=28),
        _contract(symbol, "PE", 100, 20, 2, expiry="2026-10-29", dte=28),
    ]
    rows = v123_skew_shadow._eligible_rows({"broad_contracts": snaps})
    assert len(rows) == 1
    assert rows[0]["expiry"] == "2026-10-29"
    assert rows[0]["skew_pct_points"] == 3.0


def test_skew_shadow_rejects_wide_or_stale_quotes():
    good = [
        _contract("GOOD", "CE", 100, 22, 2),
        _contract("GOOD", "PE", 100, 20, 2),
    ]
    wide = [
        _contract("WIDE", "CE", 100, 22, 12),
        _contract("WIDE", "PE", 100, 20, 2),
    ]
    stale_ce = _contract("STALE", "CE", 100, 22, 2)
    stale_ce["stale"] = True
    stale = [stale_ce, _contract("STALE", "PE", 100, 20, 2)]
    rows = v123_skew_shadow._eligible_rows({"broad_contracts": good + wide + stale})
    assert [x["symbol"] for x in rows] == ["GOOD"]
