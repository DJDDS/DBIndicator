import pandas as pd


def test_build_monthly_inputs_uses_point_in_time_fno_membership_and_actions():
    from app.v11_monthly_data import build_monthly_inputs_from_snapshots
    snapshots = [
        {"date":"2020-01-31","fno_symbols":{"AAA"},"cash_close":{"AAA":100.0,"BBB":50.0}},
        {"date":"2020-02-28","fno_symbols":{"AAA","BBB"},"cash_close":{"AAA":55.0,"BBB":55.0}},
        {"date":"2020-03-31","fno_symbols":{"BBB"},"cash_close":{"AAA":56.0,"BBB":60.0}},
    ]
    actions = {
        "AAA": [{"symbol":"AAA","ex_date":"2020-02-10","kind":"BONUS","share_multiplier":2.0,"cash_per_share":0.0}],
        "BBB": [],
    }
    rets, membership, meta = build_monthly_inputs_from_snapshots(snapshots, actions)
    feb = pd.Timestamp("2020-02-29")
    assert bool(membership.at[feb, "AAA"]) is True
    assert bool(membership.at[feb, "BBB"]) is True
    # AAA: one share at 100 -> bonus makes two shares -> 2*55 / 100 - 1 = 10%
    assert abs(rets.at[feb, "AAA"] - 0.10) < 1e-12
    assert meta["unhandled_action_returns"] == 0


def test_build_monthly_inputs_fails_closed_per_symbol_month_on_unhandled_action():
    from app.v11_monthly_data import build_monthly_inputs_from_snapshots
    snapshots = [
        {"date":"2020-01-31","fno_symbols":{"AAA"},"cash_close":{"AAA":100.0}},
        {"date":"2020-02-28","fno_symbols":{"AAA"},"cash_close":{"AAA":105.0}},
    ]
    actions = {"AAA": [{"symbol":"AAA","ex_date":"2020-02-10","kind":"RIGHTS","share_multiplier":1.0,"cash_per_share":0.0}]}
    rets, membership, meta = build_monthly_inputs_from_snapshots(snapshots, actions)
    assert pd.isna(rets.iloc[-1]["AAA"])
    assert meta["unhandled_action_returns"] == 1
