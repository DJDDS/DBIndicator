import pandas as pd
import pytest


def test_iima_parser_accepts_current_market_premium_header():
    from app.iima_factors import parse_iima_monthly_factors
    csv = (
        "Month,Market Premium,SMB,HML,WML,RF\n"
        "2019-01,1.20,-0.50,0.25,0.70,0.55\n"
        "2019-02,-2.00,0.10,-0.20,1.10,0.50\n"
    )
    out = parse_iima_monthly_factors(csv)
    assert list(out.columns) == ["rm_rf", "smb", "hml", "wml", "rf"]
    assert out.index[0] == pd.Timestamp("2019-01-31")
    assert abs(out.iloc[0]["rm_rf"] - 0.012) < 1e-12


def test_iima_missing_column_error_reports_received_headers():
    from app.iima_factors import parse_iima_monthly_factors
    csv = "Month,Market Return,SMB,HML,WML,RF\n2019-01,1.2,-0.5,0.25,0.7,0.55\n"
    with pytest.raises(ValueError) as exc:
        parse_iima_monthly_factors(csv)
    msg = str(exc.value)
    assert "missing required columns: rm_rf" in msg
    assert "received columns:" in msg
    assert "Market Return" in msg


def test_v1101_release_marker_and_ui_identify_schema_hotfix():
    from pathlib import Path
    marker = Path("RESEARCH_BUILD.txt").read_text(encoding="utf-8").strip()
    assert marker == "2026-09-04-INSTITUTIONAL-V11.0.1-IIMA-FACTOR-SCHEMA-HOTFIX"
    text = Path("app/templates/backtest.html").read_text(encoding="utf-8")
    assert "V11.0.1" in text
    assert "IIMA factor schema hotfix" in text
