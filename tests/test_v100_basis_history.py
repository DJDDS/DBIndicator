import pandas as pd
from app import nse_futures_history as nf


class StubClient:
    def fetch_day(self, day):
        d = pd.Timestamp(day).normalize()
        return pd.DataFrame([
            {"date": d, "symbol": "AAA", "expiry": d + pd.Timedelta(days=10), "open_interest": 1000.0,
             "oi_share_equivalent": 1000.0, "settle": 101.0, "close": 100.5, "volume": 10.0,
             "turnover_notional": 100000.0, "lot_size": 50.0, "source_format": "TEST"},
            {"date": d, "symbol": "AAA", "expiry": d + pd.Timedelta(days=40), "open_interest": 500.0,
             "oi_share_equivalent": 500.0, "settle": 103.0, "close": 102.5, "volume": 4.0,
             "turnover_notional": 40000.0, "lot_size": 50.0, "source_format": "TEST"},
        ])


def test_histories_retain_near_next_prices_and_expiries():
    day = pd.Timestamp("2024-01-02")
    out = nf.build_symbol_histories([day], ["AAA"], StubClient())
    h = out["AAA"]
    assert h["near_settle"].loc[day] == 101.0
    assert h["next_settle"].loc[day] == 103.0
    assert h["near_expiry"].loc[day] == day + pd.Timedelta(days=10)
    assert h["next_expiry"].loc[day] == day + pd.Timedelta(days=40)
    assert h["near_dte"].loc[day] == 10
    assert h["next_dte"].loc[day] == 40
