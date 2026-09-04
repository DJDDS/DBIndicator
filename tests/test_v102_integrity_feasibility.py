import math
import pandas as pd

from app import v10_directional_edge as v10
from app import research_feasibility as rf


def _clustered_events():
    rows=[]
    d1=pd.Timestamp('2024-01-02'); d2=pd.Timestamp('2024-01-03'); d3=pd.Timestamp('2024-01-04')
    # deliberately uneven cluster sizes so event/day weighting diverges
    for i,r in enumerate([0.010,0.008,0.006,0.004,0.002]):
        rows.append({'date':d1,'symbol':f'A{i}','sector':'S1','net':r})
    rows.append({'date':d2,'symbol':'B','sector':'S2','net':-0.020})
    rows.append({'date':d3,'symbol':'C','sector':'S3','net':0.003})
    return pd.DataFrame(rows)


def test_direction_report_exposes_event_and_day_weighted_gross_net_and_density():
    r=v10._direction_report(_clustered_events(),'net',bootstrap_reps=20)
    assert math.isclose(r['event_weighted_net'], _clustered_events()['net'].mean())
    expected_day=_clustered_events().groupby('date')['net'].mean().mean()
    assert math.isclose(r['day_weighted_net'], expected_day)
    assert math.isclose(r['event_weighted_gross'], r['event_weighted_net'] + v10.ROUND_TRIP_COST)
    assert math.isclose(r['day_weighted_gross'], r['day_weighted_net'] + v10.ROUND_TRIP_COST)
    epd=r['events_per_day']
    assert epd['mean'] == 7/3
    assert epd['median'] == 1.0
    assert epd['max'] == 5
    assert epd['p90'] >= epd['median']
    assert r['effective_sample_unit'] == 'DAYS'
    assert r['effective_sample_size'] == 3
    assert 'naive_event_t' in r
    assert 'cluster_to_naive_t_ratio' in r
    assert 'implied_within_day_corr_approx' in r


def test_direction_report_preserves_registered_gate_on_event_weighted_net():
    ev=_clustered_events()
    r=v10._direction_report(ev,'net',bootstrap_reps=20)
    assert math.isclose(r['mean_net'], r['event_weighted_net'])


def test_v10_research_record_closes_trial23_without_claiming_it_was_tested():
    out=v10.evaluate_v10({}, {})
    assert out['trial23_state'] == 'CLOSED_COMPONENT_TRIALS_FAILED_NOT_EVALUATED'
    assert out['trial23_evaluated'] is False
    assert out['trial21_family_state'] == 'DAILY_SPECIFICATION_REJECTED_FAMILY_NOT_GLOBALLY_REJECTED'
    assert out['trial22_family_state'] == 'ABSOLUTE_BASIS_EVENT_SPEC_REJECTED_CROSS_SECTIONAL_HYPOTHESIS_UNTESTED'


def test_feasibility_gate_fails_closed_when_prior_net_is_negative():
    out=rf.assess_pretrial_feasibility(
        prior_gross_effect=0.00070,
        round_trip_cost=0.0018,
        sigma_day=0.01,
        effective_days=250,
        t_bar=3.25,
        source='published prior',
        horizon='1D',
    )
    assert out['prior_net_effect'] < 0
    assert out['decision'] == 'DO_NOT_RUN_COST_WALL'
    assert out['feasible'] is False


def test_feasibility_gate_fails_closed_without_cited_prior_effect():
    out=rf.assess_pretrial_feasibility(
        prior_gross_effect=None,
        round_trip_cost=0.0018,
        sigma_day=0.01,
        effective_days=250,
        t_bar=3.25,
        source=None,
        horizon='1D',
    )
    assert out['decision'] == 'DO_NOT_RUN_PRIOR_EFFECT_REQUIRED'
    assert out['feasible'] is False


def test_feasibility_gate_rejects_underpowered_positive_net_effect():
    out=rf.assess_pretrial_feasibility(
        prior_gross_effect=0.0030,
        round_trip_cost=0.0010,
        sigma_day=0.05,
        effective_days=100,
        t_bar=3.25,
        source='published prior',
        horizon='10D',
    )
    assert out['prior_net_effect'] > 0
    assert out['mde_net_effect'] > out['prior_net_effect']
    assert out['decision'] == 'DO_NOT_RUN_UNDERPOWERED'


def test_retro_trial21_feasibility_records_audit_cost_wall_without_new_alpha_read():
    out=v10.retro_feasibility_diagnostics()
    t21=out['trial21_one_day_prior']
    assert t21['prior_gross_low'] == 0.00033
    assert t21['prior_gross_high'] == 0.00070
    assert t21['expected_net_low'] < 0 and t21['expected_net_high'] < 0
    assert t21['decision'] == 'DO_NOT_RUN_COST_WALL'
    assert out['trial22_prior']['decision'] == 'DO_NOT_RUN_PRIOR_EFFECT_REQUIRED'
