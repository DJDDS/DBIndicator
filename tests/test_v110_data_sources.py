import pandas as pd


def test_iima_factor_parser_normalizes_aliases_and_percent_units():
    from app.iima_factors import parse_iima_monthly_factors
    csv = "Date,RM-RF,SMB,HML,WML,RF\n2019-01,1.20,-0.50,0.25,0.70,0.55\n2019-02,-2.00,0.10,-0.20,1.10,0.50\n"
    out = parse_iima_monthly_factors(csv)
    assert list(out.columns) == ["rm_rf", "smb", "hml", "wml", "rf"]
    assert out.index[0] == pd.Timestamp("2019-01-31")
    assert abs(out.iloc[0]["rm_rf"] - 0.012) < 1e-12
    assert abs(out.iloc[0]["rf"] - 0.0055) < 1e-12


def test_corporate_actions_total_return_handles_dividend_and_bonus():
    from app.nse_corporate_actions import total_return_between
    actions = [
        {"symbol":"ABC","ex_date":"2020-01-10","kind":"DIVIDEND","cash_per_share":2.0,"share_multiplier":1.0},
        {"symbol":"ABC","ex_date":"2020-01-20","kind":"BONUS","cash_per_share":0.0,"share_multiplier":2.0},
    ]
    # Start with one share worth 100. Dividend creates 2 cash; 1:1 bonus makes 2 shares; end price 55.
    out = total_return_between(100.0, 55.0, actions)
    assert abs(out - 0.12) < 1e-12


def test_unhandled_corporate_action_fails_closed():
    from app.nse_corporate_actions import total_return_between, UnhandledCorporateAction
    actions = [{"symbol":"ABC","ex_date":"2020-01-10","kind":"RIGHTS","cash_per_share":0.0,"share_multiplier":1.0}]
    try:
        total_return_between(100.0, 105.0, actions)
    except UnhandledCorporateAction:
        pass
    else:
        raise AssertionError("rights issue must fail closed")
