import datetime as dt
import json

from app.v124_orderflow_recorder import OrderFlowMinuteRecorder, book_features


def _tick(px, vol, bid, ask, bq=100, aq=100, tbq=1000, tsq=1000):
    return {
        "last_price": px,
        "volume_traded": vol,
        "total_buy_quantity": tbq,
        "total_sell_quantity": tsq,
        "depth": {
            "buy": [{"price": bid, "quantity": bq}] + [{"price": bid - 0.05 * i, "quantity": 50} for i in range(1, 5)],
            "sell": [{"price": ask, "quantity": aq}] + [{"price": ask + 0.05 * i, "quantity": 50} for i in range(1, 5)],
        },
    }


def test_book_features_signs():
    f = book_features(_tick(100, 1, 99.95, 100.05, bq=300, aq=100, tbq=3000, tsq=1000))
    assert f["l1_imb"] == 0.5
    assert f["l5_imb"] > 0
    assert f["tot_imb"] == 0.5
    assert f["micro_bps"] > 0
    assert round(f["spread_bps"], 1) == 10.0


def test_minute_row_classifies_aggressor_and_emits_on_rollover(tmp_path):
    path = tmp_path / "of.jsonl"
    rec = OrderFlowMinuteRecorder(path)
    t0 = dt.datetime(2026, 10, 1, 10, 0, 1)
    rec.on_cash_tick("ABC", _tick(100.00, 1000, 99.95, 100.05), t0)
    rec.on_cash_tick("ABC", _tick(100.05, 1300, 100.00, 100.10), t0 + dt.timedelta(seconds=1))  # lifts ask: buy 300
    rec.on_cash_tick("ABC", _tick(100.00, 1400, 99.95, 100.05), t0 + dt.timedelta(seconds=2))   # hits bid: sell 100
    assert not path.exists()
    rec.on_cash_tick("ABC", _tick(100.05, 1500, 100.00, 100.10), t0 + dt.timedelta(seconds=60))
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    assert len(rows) == 1
    r = rows[0]
    assert r["minute"].startswith("2026-10-01T10:00")
    assert r["buy_vol"] == 300 and r["sell_vol"] == 100
    assert r["delta_ratio"] == 0.5
    assert r["close"] == 100.0 and r["ticks"] == 3


def test_outside_session_and_no_path_are_ignored(tmp_path):
    path = tmp_path / "of.jsonl"
    rec = OrderFlowMinuteRecorder(path)
    rec.on_cash_tick("ABC", _tick(100, 1, 99.95, 100.05), dt.datetime(2026, 10, 1, 9, 10))
    rec.on_cash_tick("ABC", _tick(100, 2, 99.95, 100.05), dt.datetime(2026, 10, 1, 15, 31))
    assert rec.status()["symbols_live"] == 0
    OrderFlowMinuteRecorder(None).on_cash_tick("ABC", _tick(100, 1, 99.95, 100.05), dt.datetime(2026, 10, 1, 10))
