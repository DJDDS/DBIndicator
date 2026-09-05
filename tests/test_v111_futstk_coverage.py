import pandas as pd


def test_month_end_contract_metadata_freezes_nearest_nonexpired_settle_then_close_rule():
    from app.v11_monthly_data import selected_futstk_contract_metadata
    d = pd.Timestamp("2022-01-31")
    fo = pd.DataFrame([
        {"symbol":"AAA","expiry":"2022-02-24","settle":101.0,"close":100.5,"lot_size":50,"source_format":"LEGACY"},
        {"symbol":"AAA","expiry":"2022-03-31","settle":102.0,"close":101.5,"lot_size":50,"source_format":"LEGACY"},
        {"symbol":"BBB","expiry":"2022-02-24","settle":None,"close":80.0,"lot_size":25,"source_format":"LEGACY"},
        {"symbol":"OLD","expiry":"2022-01-27","settle":1.0,"close":1.0,"lot_size":1,"source_format":"LEGACY"},
    ])
    out = selected_futstk_contract_metadata(fo, d)
    assert out["AAA"]["expiry"] == "2022-02-24"
    assert out["AAA"]["price_field"] == "settle"
    assert out["BBB"]["price_field"] == "close"
    assert "OLD" not in out
    assert out["AAA"]["lot_size_available"] is True


def test_execution_coverage_fails_closed_when_any_required_portfolio_contract_is_missing():
    from app.v111_development import audit_futstk_execution_coverage
    d = pd.Timestamp("2022-01-31")
    weights = pd.DataFrame({"AAA":[1.0],"BBB":[-1.0]}, index=[d])
    meta = {
        d: {
            "AAA": {"expiry":"2022-02-24","lot_size_available":True,"price_available":True,"price_field":"settle"},
            # BBB intentionally missing
        }
    }
    out = audit_futstk_execution_coverage(weights, meta)
    assert out["coverage"] == 0.5
    assert out["pass"] is False
    assert out["status"] == "FUTSTK_EXECUTION_COVERAGE_INSUFFICIENT"
    assert out["missing_required"] == 1
    assert out["contract_selection_rule"] == "NEAREST_NONEXPIRED_FUTSTK_AT_SIGNAL_MONTH_END"
    assert out["price_rule"] == "SETTLE_IF_POSITIVE_ELSE_CLOSE"
    assert out["roll_rule"] == "MONTHLY_RESELECT_AT_SIGNAL_MONTH_END"
