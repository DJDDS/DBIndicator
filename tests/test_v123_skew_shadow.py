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


def test_skew_shadow_backfills_missed_same_day_slot_idempotently(tmp_path):
    snapshot = tmp_path / "snap.jsonl"
    state = tmp_path / "state.json"
    ledger = tmp_path / "ledger.jsonl"

    symbols = [f"S{i:02d}" for i in range(20)]
    prev = _slot("2026-09-30", "PRE_CAS", {s: 100.0 for s in symbols})
    prev["ts"] = "2026-09-30T15:10:00"
    today = _slot(
        "2026-10-01",
        "OPEN_STABLE",
        {s: 99.0 for s in symbols},
        {**{s: 0.0 for s in symbols}, "S00": -7.0, "S19": 8.0},
    )
    snapshot.write_text(json.dumps(prev) + "\n" + json.dumps(today) + "\n", encoding="utf-8")

    now = dt.datetime(2026, 10, 1, 11, 45)
    first = v123_skew_shadow.backfill_today_entries(
        snapshot_file=snapshot,
        state_file=state,
        ledger_file=ledger,
        now=now,
    )
    assert first["current"]["signal_clock"] == "09:30"
    assert first["current"]["market_direction"] == "Bearish"
    assert first["current"]["selected_count"] == 1
    assert first["current"]["selected"][0]["symbol"] == "S00"

    # Running backfill again must not duplicate the historical batch.
    second = v123_skew_shadow.backfill_today_entries(
        snapshot_file=snapshot,
        state_file=state,
        ledger_file=ledger,
        now=now + dt.timedelta(minutes=1),
    )
    records = [json.loads(x) for x in ledger.read_text().splitlines()]
    signals = [x for x in records if x.get("record_type") == "SKEW_SHADOW_SIGNAL"]
    assert len(signals) == 1
    assert second["open_batches"] == 1


def test_skew_shadow_status_has_prominent_research_warning(tmp_path):
    status = v123_skew_shadow.shadow_status(tmp_path / "missing.json")
    assert "17-session hypothesis" in status["research_warning"]
    assert status["controls_trading"] is False


def test_dashboard_marks_skew_shadow_not_validated():
    from pathlib import Path
    html = Path("app/templates/index.html").read_text(encoding="utf-8")
    assert "RESEARCH IN PROGRESS — NOT VALIDATED FOR TRADING." in html
    assert "17-session hypothesis" in html


def test_live_skew_status_surfaces_primary_and_reversal_without_trade_control(tmp_path):
    snapshot = tmp_path / "snap.jsonl"
    state = tmp_path / "state.json"
    ledger = tmp_path / "ledger.jsonl"

    symbols = [f"S{i:02d}" for i in range(20)]
    prev = _slot("2026-09-30", "PRE_CAS", {s: 100.0 for s in symbols})
    prev["ts"] = "2026-09-30T15:10:00"
    skews = {s: 0.0 for s in symbols}
    skews["S00"] = -8.0
    skews["S19"] = 9.0
    today = _slot("2026-10-01", "OPEN_STABLE", {s: 101.0 for s in symbols}, skews)
    snapshot.write_text(json.dumps(prev) + "\n" + json.dumps(today) + "\n", encoding="utf-8")

    now = dt.datetime(2026, 10, 1, 9, 30)
    v123_skew_shadow.process_slot(
        snapshot_file=snapshot,
        state_file=state,
        ledger_file=ledger,
        now=now,
        slot="OPEN_STABLE",
    )

    live_rows = [
        {
            "symbol": "S19",
            "live_price": 102.0,
            "direction_lock_state": "BULLISH",
            "movement_significant": True,
            "movement_direction": "Bullish",
            "movement_p_value": 0.001,
            "movement_familywise_alpha": 0.01,
            "movement_z": 4.0,
            "movement_horizon_seconds": 300,
        },
        {
            "symbol": "S00",
            "live_price": 102.0,
            "direction_lock_state": "BEARISH",
            "movement_significant": True,
            "movement_direction": "Bearish",
            "movement_p_value": 0.001,
            "movement_familywise_alpha": 0.01,
            "movement_z": -4.0,
            "movement_horizon_seconds": 300,
        },
    ]
    live = v123_skew_shadow.live_status(
        state_file=state,
        live_rows=live_rows,
        now=dt.datetime(2026, 10, 1, 10, 0),
    )
    cur = live["current"]
    # Market is bullish from 100 -> 101, so S19 is the primary skew name.
    assert cur["market_direction"] == "Bullish"
    assert cur["live_primary"][0]["symbol"] == "S19"
    assert cur["live_primary"][0]["opportunity_lane"] == "PRIMARY_CONFIRMED"
    # S00 is opposite the market anchor but now has a significant bearish
    # underlying move, so it remains visible as a reversal research watch.
    assert cur["live_reversal_watch"][0]["symbol"] == "S00"
    assert cur["live_reversal_watch"][0]["opportunity_lane"] == "REVERSAL_WATCH"
    assert live["controls_trading"] is False
    assert live["validation_status"] == "VALIDATION_IN_PROCESS"


def test_dashboard_has_production_skew_opportunity_desk():
    from pathlib import Path
    html = Path("app/templates/index.html").read_text(encoding="utf-8")
    assert 'id="skew-opportunity-desk"' in html
    assert "17-SESSION HYPOTHESIS · NOT YET VALIDATED" in html
    assert 'id="skew-desk-primary"' in html
    assert 'id="skew-desk-reversal"' in html
    assert 'id="skew-desk-extremes"' in html
    assert "REVERSAL WATCH" in html
    assert "Math Recorder, CALL V1, quant shadow, Trial 25 and missed-mover forensics remain separate and unchanged." in html


def test_live_skew_primary_carries_research_instrument_and_timing(tmp_path):
    snapshot = tmp_path / "snap.jsonl"
    state = tmp_path / "state.json"
    ledger = tmp_path / "ledger.jsonl"
    symbols = [f"S{i:02d}" for i in range(20)]
    prev = _slot("2026-09-30", "PRE_CAS", {s: 100.0 for s in symbols})
    prev["ts"] = "2026-09-30T15:10:00"
    skews = {s: 0.0 for s in symbols}
    skews["S19"] = 9.0
    today = _slot("2026-10-01", "OPEN_STABLE", {s: 101.0 for s in symbols}, skews)
    snapshot.write_text(json.dumps(prev) + "\n" + json.dumps(today) + "\n", encoding="utf-8")
    v123_skew_shadow.process_slot(
        snapshot_file=snapshot,
        state_file=state,
        ledger_file=ledger,
        now=dt.datetime(2026, 10, 1, 9, 30),
        slot="OPEN_STABLE",
    )
    live = v123_skew_shadow.live_status(
        state_file=state,
        live_rows=[{"symbol":"S19","live_price":101.5}],
        now=dt.datetime(2026, 10, 1, 9, 40),
    )
    row = live["current"]["live_primary"][0]
    assert row["research_primary_instrument"] == "STOCK_FUTURES"
    assert row["entry_window_state"] == "ANCHOR_WINDOW"
    assert row["research_exit_clock"] == "15:10"
    # Synthetic fixture uses 2% spread on both option legs.
    assert row["option_spread_le_3pct"] is True
    assert row["research_option_status"] == "OPTION_ELIGIBLE"

    late = v123_skew_shadow.live_status(
        state_file=state,
        live_rows=[{"symbol":"S19","live_price":102.0}],
        now=dt.datetime(2026, 10, 1, 12, 0),
    )
    assert late["current"]["live_primary"][0]["entry_window_state"] == "LATE_TRACK_ONLY"


def test_dashboard_colour_codes_skew_trade_window():
    from pathlib import Path
    html = Path("app/templates/index.html").read_text(encoding="utf-8")
    assert "RESEARCH ENTRY WINDOW" in html
    assert "LATE · TRACK ONLY" in html
    assert "NO FRESH TRADE" in html
    assert "FUTURES = PRIMARY RESEARCH VEHICLE" in html
    assert "OPTION SPREAD OK ≤3%" in html
    assert "OPTIONS AVOID" in html
    assert "GREEN = research entry window (anchor to +15m)" in html
