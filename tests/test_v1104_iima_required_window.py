import pandas as pd
import pytest

from app.iima_factors import parse_iima_monthly_factors


def test_trial24_required_window_ignores_bad_factor_rows_before_2010():
    csv = (
        "Date,SMB,HML,WML,MF,RF\n"
        "199310,0.10,0.20,0.30,NA,0.40\n"
        "200912,0.10,0.20,0.30,NA,0.40\n"
        "201001,0.10,0.20,NA,1.20,0.40\n"
        "201002,0.20,0.10,NA,-0.80,0.35\n"
    )

    out = parse_iima_monthly_factors(
        csv,
        start="2010-01-01",
        end="2023-05-31",
        required_factors=("rm_rf", "smb", "hml", "rf"),
    )

    assert out.index.tolist() == [pd.Timestamp("2010-01-31"), pd.Timestamp("2010-02-28")]
    assert list(out.columns) == ["rm_rf", "smb", "hml", "rf"]
    assert abs(out.iloc[0]["rm_rf"] - 0.012) < 1e-12


def test_trial24_required_window_still_fails_closed_on_bad_required_factor_inside_window():
    csv = (
        "Date,SMB,HML,WML,MF,RF\n"
        "200912,0.10,0.20,0.30,NA,0.40\n"
        "201001,0.10,0.20,0.30,NA,0.40\n"
    )

    with pytest.raises(ValueError) as exc:
        parse_iima_monthly_factors(
            csv,
            start="2010-01-01",
            end="2023-05-31",
            required_factors=("rm_rf", "smb", "hml", "rf"),
        )

    msg = str(exc.value)
    assert "rm_rf" in msg
    assert "2010-01" in msg
    assert "'NA'" in msg


def test_trial24_does_not_require_wml_because_ff3_residualisation_does_not_use_it():
    csv = (
        "Date,SMB,HML,MF,RF\n"
        "201001,0.10,0.20,1.20,0.40\n"
        "201002,0.20,0.10,-0.80,0.35\n"
    )

    out = parse_iima_monthly_factors(
        csv,
        start="2010-01-01",
        end="2023-05-31",
        required_factors=("rm_rf", "smb", "hml", "rf"),
    )

    assert list(out.columns) == ["rm_rf", "smb", "hml", "rf"]


def test_trial24_client_requests_only_the_preregistered_required_factor_window(monkeypatch, tmp_path):
    from app import v11_monthly_data

    seen = {}

    class FakeFactorClient:
        def __init__(self, cache_dir):
            pass
        def load_monthly(self, **kwargs):
            seen.update(kwargs)
            idx = pd.date_range("2010-01-31", "2023-05-31", freq="ME")
            frame = pd.DataFrame(0.0, index=idx, columns=["rm_rf", "smb", "hml", "rf"])
            return frame, {"sha256": "abc", "release": "2025-12"}

    monkeypatch.setattr(v11_monthly_data, "IIMAFactorClient", FakeFactorClient)

    # Stop after factor loading; this test is about the requested factor contract.
    class StopHere(Exception):
        pass
    def stop_snapshot(*args, **kwargs):
        raise StopHere
    monkeypatch.setattr(v11_monthly_data, "_resolve_snapshot", stop_snapshot)

    with pytest.raises(StopHere):
        v11_monthly_data.build_trial24_inputs(tmp_path)

    assert seen == {
        "start": v11_monthly_data.WARMUP_START,
        "end": v11_monthly_data.TRIAL24_PREFINAL_OUTCOME_END,
        "required_factors": ("rm_rf", "smb", "hml", "rf"),
        "require_complete_window": True,
    }
