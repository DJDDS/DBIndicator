import pandas as pd
import pytest

from app.iima_factors import parse_iima_monthly_factors


def test_iima_parser_normalizes_official_numeric_formatting_without_changing_mf_semantics():
    csv = (
        "Date,SMB,HML,WML,MF,RF\n"
        "202501,0.30%,−0.20%,0.40%,\u00a01.20%\u00a0,0.50%\n"
        "202502,-0.10%,0.25%,-0.30%,-0.80%,0.45%\n"
    )

    out = parse_iima_monthly_factors(csv)

    assert out.index.tolist() == [pd.Timestamp("2025-01-31"), pd.Timestamp("2025-02-28")]
    assert abs(out.iloc[0]["rm_rf"] - 0.012) < 1e-12
    assert abs(out.iloc[0]["rf"] - 0.005) < 1e-12
    # MF is already market excess return; RF must not be subtracted again.
    assert abs(out.iloc[0]["rm_rf"] - (1.20 / 100.0)) < 1e-12
    assert abs(out.iloc[0]["hml"] - (-0.20 / 100.0)) < 1e-12


def test_iima_parser_accepts_comma_grouping_in_quoted_numeric_cells():
    csv = (
        "Date,SMB,HML,WML,MF,RF\n"
        '202501,"1,200.50",0.25,0.40,1.20,0.50\n'
        "202502,0.10,0.25,0.30,0.80,0.45\n"
    )

    out = parse_iima_monthly_factors(csv)

    assert abs(out.iloc[0]["smb"] - 12.005) < 1e-12
    assert abs(out.iloc[0]["rm_rf"] - 0.012) < 1e-12


def test_iima_parser_fails_closed_with_exact_month_and_raw_bad_value():
    csv = (
        "Date,SMB,HML,WML,MF,RF\n"
        "202501,0.30,-0.20,0.40,1.20,0.50\n"
        "202502,-0.10,0.25,-0.30,NA,0.45\n"
    )

    with pytest.raises(ValueError) as exc:
        parse_iima_monthly_factors(csv)

    msg = str(exc.value)
    assert "IIMA factor column rm_rf contains non-numeric value" in msg
    assert "2025-02" in msg
    assert "'NA'" in msg


def test_v1103_release_identifies_numeric_format_integrity_hotfix():
    from pathlib import Path

    marker = Path("RESEARCH_BUILD.txt").read_text(encoding="utf-8").strip()
    assert marker == "2026-09-04-INSTITUTIONAL-V11.0.5-STRICT-REQUIRED-WINDOW-FACTOR-CONTRACT"
    text = Path("app/templates/backtest.html").read_text(encoding="utf-8")
    assert "V11.0.3 IIMA numeric-format integrity hotfix" in text
    assert "exact month + raw value" in text
