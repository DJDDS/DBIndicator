import numpy as np
import pandas as pd


def _fixture():
    # 60 months is enough for 36-month beta + 12-1 formation and several outcomes.
    idx = pd.date_range("2015-01-31", periods=60, freq="ME")
    rng = np.random.default_rng(7)
    fac = pd.DataFrame({
        "rm_rf": rng.normal(0.005,0.03,len(idx)),
        "smb": rng.normal(0.0,0.02,len(idx)),
        "hml": rng.normal(0.0,0.02,len(idx)),
        "wml": rng.normal(0.0,0.02,len(idx)),
        "rf": np.full(len(idx),0.004),
    }, index=idx)
    rets={}
    for j in range(20):
        alpha=(j-9.5)*0.0002
        idio=np.linspace(-0.01,0.01,len(idx))*(j-9.5)/9.5
        rets[f"S{j:02d}"]=0.004+0.8*fac.rm_rf+0.2*fac.smb-0.1*fac.hml+alpha+idio
    ret=pd.DataFrame(rets,index=idx)
    mem=pd.DataFrame(True,index=idx,columns=ret.columns)
    return ret,fac,mem


def test_residual_momentum_scores_are_point_in_time_and_decile_ranked():
    from app.v11_research import compute_trial24_scores
    ret,fac,mem=_fixture()
    scores=compute_trial24_scores(ret,fac,mem)
    assert not scores.empty
    first_date=scores["date"].min()
    sub=scores[scores.date.eq(first_date)]
    assert sub["score"].notna().all()
    assert sub["decile"].between(1,10).all()
    # Changing a future return cannot change an already-formed score.
    ret2=ret.copy(); ret2.iloc[-1,:]=99
    scores2=compute_trial24_scores(ret2,fac,mem)
    a=scores[scores.date.eq(first_date)].sort_values("symbol")["score"].to_numpy()
    b=scores2[scores2.date.eq(first_date)].sort_values("symbol")["score"].to_numpy()
    assert np.allclose(a,b)


def test_trial24_evaluation_never_reads_final_20_percent():
    from app.v11_research import evaluate_trial24_from_scores
    ret,fac,mem=_fixture()
    from app.v11_research import compute_trial24_scores
    scores=compute_trial24_scores(ret,fac,mem)
    out=evaluate_trial24_from_scores(scores,ret)
    assert out["final_read"] is False
    assert out["final_months"] > 0
    assert out["confirmatory_months"] > 0
    assert out["months_evaluated"] == out["confirmatory_months"]
    assert "final_returns" not in out


def test_confirmatory_only_runner_reports_planned_final_without_reading_it():
    from app.v11_research import compute_trial24_scores, evaluate_trial24_confirmatory_only
    ret, fac, mem = _fixture()
    scores = compute_trial24_scores(ret, fac, mem)
    out = evaluate_trial24_confirmatory_only(scores, ret, planned_final_months=31)
    assert out["final_read"] is False
    assert out["final_months"] == 31
    assert out["months_evaluated"] == out["confirmatory_months"]
    assert "final_returns" not in out
