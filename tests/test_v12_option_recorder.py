import datetime as dt

from app import v12_option_recorder


def test_deep_rank_uses_continuous_spread_before_symbol_name():
    summaries = {
        "ZZZ": {
            "liquidity_score": 100.0,
            "primary": {"straddle_spread_pct": 0.90},
        },
        "AAA": {
            "liquidity_score": 100.0,
            "primary": {"straddle_spread_pct": 0.55},
        },
    }
    assert v12_option_recorder.rank_deep_symbols(summaries, set(), limit=1) == ["AAA"]


def test_quote_freshness_is_separate_from_last_trade_inactivity():
    now = dt.datetime(2026, 9, 25, 10, 0)
    contract = {
        "underlying": "ABC",
        "tradingsymbol": "ABC26OCT100CE",
        "instrument_token": 1,
        "instrument_type": "CE",
        "strike": 100.0,
        "expiry": dt.date(2026, 10, 29),
        "lot_size": 100,
    }
    quote = {
        "timestamp": now,
        "last_trade_time": now - dt.timedelta(minutes=20),
        "last_price": 5.0,
        "volume": 1000,
        "oi": 2000,
        "depth": {
            "buy": [{"price": 4.9, "quantity": 100, "orders": 1}],
            "sell": [{"price": 5.1, "quantity": 100, "orders": 1}],
        },
    }
    row = v12_option_recorder.normalize_contract_snapshot(
        contract, quote, 100.0, now, "OPEN_STABLE"
    )
    assert row["stale"] is False
    assert row["quote_stale"] is False
    assert row["last_trade_stale"] is True
    assert row["quote_age_s"] == 0.0
    assert row["last_trade_age_s"] == 1200.0


def test_slot_claim_is_atomic_until_released(tmp_path):
    now = dt.datetime(2026, 9, 25, 15, 37)
    state_file = tmp_path / "v12_option_state.json"

    claim1, ok1 = v12_option_recorder._acquire_slot_claim(
        state_file, now, "POST_CAS"
    )
    claim2, ok2 = v12_option_recorder._acquire_slot_claim(
        state_file, now, "POST_CAS"
    )

    assert ok1 is True
    assert ok2 is False
    assert claim2 == claim1

    v12_option_recorder._release_slot_claim(claim1)
    claim3, ok3 = v12_option_recorder._acquire_slot_claim(
        state_file, now, "POST_CAS"
    )
    assert ok3 is True
    v12_option_recorder._release_slot_claim(claim3)
