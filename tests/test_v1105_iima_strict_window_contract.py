import pandas as pd
import pytest

from app.iima_factors import parse_iima_monthly_factors


def _strict_trial24_csv(*, omit: str | None = None) -> str:
    rows = ["Date,SMB,HML,WML,MF,RF"]
    # Malformed factor values are permitted only where the month proves the row
    # is outside Trial 24's consumer window.
    rows.append("199310,0.10,0.20,0.30,NA,0.40")
    for period in pd.period_range("2010-01", "2023-05", freq="M"):
        key = period.strftime("%Y%m")
        if omit == period.strftime("%Y-%m"):
            continue
        rows.append(f"{key},0.10,0.20,0.30,1.20,0.40")
    rows.append("202306,0.10,0.20,0.30,NA,0.40")
    return "\n".join(rows) + "\n"


def test_strict_trial24_window_ignores_only_provably_out_of_window_bad_values():
    out = parse_iima_monthly_factors(
        _strict_trial24_csv(),
        start="2010-01-01",
        end="2023-05-31",
        required_factors=("rm_rf", "smb", "hml", "rf"),
        require_complete_window=True,
    )

    assert out.index[0] == pd.Timestamp("2010-01-31")
    assert out.index[-1] == pd.Timestamp("2023-05-31")
    assert len(out) == len(pd.period_range("2010-01", "2023-05", freq="M"))
    assert out.notna().all().all()


def test_strict_trial24_window_fails_closed_when_an_in_window_month_is_missing():
    with pytest.raises(ValueError) as exc:
        parse_iima_monthly_factors(
            _strict_trial24_csv(omit="2017-08"),
            start="2010-01-01",
            end="2023-05-31",
            required_factors=("rm_rf", "smb", "hml", "rf"),
            require_complete_window=True,
        )

    msg = str(exc.value)
    assert "missing required month" in msg
    assert "2017-08" in msg
