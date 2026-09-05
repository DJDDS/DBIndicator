import numpy as np
import pandas as pd
import pytest


def _fixture(periods=60):
    idx = pd.date_range("2018-01-31", periods=periods, freq="ME")
    rng = np.random.default_rng(111)
    rets = pd.DataFrame({
        f"S{i:02d}": rng.normal(0.005 + i * 0.0002, 0.03, periods)
        for i in range(20)
    }, index=idx)
    mem = pd.DataFrame(True, index=idx, columns=rets.columns)
    fac = pd.DataFrame({
        "rm_rf": rng.normal(0.004, 0.03, periods),
        "smb": rng.normal(0.0, 0.02, periods),
        "hml": rng.normal(0.0, 0.02, periods),
        "rf": np.full(periods, 0.003),
    }, index=idx)
    return rets, fac, mem


def test_v111_firewall_rejects_any_candidate_return_frame_past_development_end():
    from app.v111_development import development_only_inputs
    rets, fac, mem = _fixture(66)
    # fixture reaches beyond 2023-05; V11.1 must fail rather than silently slice.
    with pytest.raises(ValueError, match="locked final"):
        development_only_inputs(rets, fac, mem)


def test_price_momentum_scores_are_point_in_time_12_1_and_decile_ranked():
    from app.v111_development import compute_price_momentum_scores
    rets, fac, mem = _fixture(60)
    # keep fixture inside V11.1 development window
    rets = rets.loc[:"2022-12-31"]
    mem = mem.reindex(rets.index)
    scores = compute_price_momentum_scores(rets, mem)
    assert not scores.empty
    first_date = scores["date"].min()
    first = scores[scores.date.eq(first_date)].sort_values("symbol")
    assert first["decile"].between(1, 10).all()

    changed = rets.copy()
    changed.iloc[-1, :] = 5.0
    scores2 = compute_price_momentum_scores(changed, mem)
    again = scores2[scores2.date.eq(first_date)].sort_values("symbol")
    assert np.allclose(first["score"].to_numpy(), again["score"].to_numpy())


def test_residual_candidate_reuses_frozen_trial24_point_in_time_score_contract():
    from app.v111_development import compute_residual_momentum_scores
    rets, fac, mem = _fixture(60)
    rets = rets.loc[:"2022-12-31"]
    fac = fac.reindex(rets.index)
    mem = mem.reindex(rets.index)
    out = compute_residual_momentum_scores(rets, fac, mem)
    assert list(out.columns) == ["date", "symbol", "score", "decile"]
    assert out["decile"].between(1, 10).all()
