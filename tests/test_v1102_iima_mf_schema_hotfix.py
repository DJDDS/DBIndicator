from app.iima_factors import parse_iima_monthly_factors


def test_exact_production_mf_header_maps_to_market_excess_return_without_double_subtracting_rf():
    csv = (
        "Date,SMB,HML,WML,MF,RF\n"
        "202501,0.30,-0.20,0.40,1.20,0.50\n"
        "202502,-0.10,0.25,-0.30,-0.80,0.45\n"
    )

    out = parse_iima_monthly_factors(csv)

    assert list(out.columns) == ["rm_rf", "smb", "hml", "wml", "rf"]
    assert abs(out.iloc[0]["rm_rf"] - 0.012) < 1e-12
    assert abs(out.iloc[0]["rf"] - 0.005) < 1e-12
    assert abs(out.iloc[0]["rm_rf"] - (1.20 / 100.0)) < 1e-12


def test_v1102_release_identifies_exact_mf_schema_hotfix():
    from pathlib import Path

    marker = Path("RESEARCH_BUILD.txt").read_text(encoding="utf-8").strip()
    assert marker == "2026-09-04-INSTITUTIONAL-V11.1-DEVELOPMENT-FEASIBILITY-LAB"
    text = Path("app/templates/backtest.html").read_text(encoding="utf-8")
    assert "V11.0.2 exact IIMA MF schema hotfix" in text
    assert "<code>MF</code>" in text
    assert "MF is already the market excess return" in text
