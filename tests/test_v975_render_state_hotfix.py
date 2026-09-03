import json

import pandas as pd

from app import backtest


def _assert_json_safe(value):
    json.dumps(value, allow_nan=True)


def test_v975_recent_mwpl_incomplete_diagnostic_does_not_expose_raw_pandas(monkeypatch):
    bad = {
        "available": False,
        "reason": "INSUFFICIENT_MWPL_DATE_COVERAGE:0.3%",
        "date_coverage": 0.003,
        "month_coverage": 0.0,
        "observation_coverage": 0.0,
        "source": "TEST",
        "mwpl_by_symbol": {"AAA": pd.Series([100.0], index=pd.to_datetime(["2021-01-01"]))},
        "ban_by_symbol": {"AAA": pd.Series([False], index=pd.to_datetime(["2021-01-01"]))},
    }

    # Exercise the scalar diagnostic boundary directly: this is exactly the
    # payload shape returned when recent-window MWPL cannot be established.
    out = backtest._v97_recent_mwpl_incomplete_result(bad)

    assert out["status"] == "INCONCLUSIVE_RECENT_MWPL"
    assert out["reason"] == "INSUFFICIENT_MWPL_DATE_COVERAGE:0.3%"
    assert "mwpl" not in out
    assert out["mwpl_date_coverage"] == 0.003
    _assert_json_safe(out)


def test_v975_v97_state_accessor_sanitizes_pandas_before_template_json(monkeypatch):
    original = backtest._v97_state
    try:
        backtest._v97_state = backtest._default_v97_state()
        backtest._v97_state.update({
            "status": "done",
            "result": {
                "confound_controls": {
                    "recent_mwpl_bound": {
                        "status": "INCONCLUSIVE_RECENT_MWPL",
                        "debug_series": pd.Series([1.0, 2.0]),
                        "debug_index": pd.DatetimeIndex(["2021-01-01", "2021-01-02"]),
                    }
                }
            },
        })
        out = backtest.get_v97_trial19_state()
        _assert_json_safe(out)
        bound = out["result"]["confound_controls"]["recent_mwpl_bound"]
        assert isinstance(bound["debug_series"], list)
        assert isinstance(bound["debug_index"], list)
    finally:
        backtest._v97_state = original



def test_v975_backtest_template_tojson_survives_unserializable_v97_diagnostic():
    from jinja2 import Environment

    original = backtest._v97_state
    try:
        backtest._v97_state = backtest._default_v97_state()
        backtest._v97_state.update({
            "status": "done",
            "result": {
                "confound_controls": {
                    "recent_mwpl_bound": {
                        "status": "INCONCLUSIVE_RECENT_MWPL",
                        "debug_series": pd.Series([1.0]),
                    }
                }
            },
        })
        safe_state = backtest.get_v97_trial19_state()
        # Flask/Jinja's backtest page fails at the same `|tojson` boundary.
        rendered = Environment().from_string("{{ state | tojson }}").render(state=safe_state)
        assert "INCONCLUSIVE_RECENT_MWPL" in rendered
        assert "debug_series" in rendered
    finally:
        backtest._v97_state = original


def test_v975_atomic_v97_persistence_sanitizes_unexpected_pandas(monkeypatch, tmp_path):
    target = tmp_path / "v97-state.json"
    monkeypatch.setattr(backtest, "_V97_STATE_PATH", target)
    state = backtest._default_v97_state()
    state["result"] = {"diagnostic": pd.Series([3.0, 4.0])}
    backtest._atomic_write_v97_state(state)
    loaded = json.loads(target.read_text())
    assert loaded["result"]["diagnostic"] == [3.0, 4.0]
