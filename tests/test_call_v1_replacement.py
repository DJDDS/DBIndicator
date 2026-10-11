import base64
import datetime as dt
import copy
import pytest


def day():
    return {'date':'2026-10-12', 'at':'2026-10-12 09:27:00',
            'settings':{'hc_quantile':0.8,'window':250,'hc_top_n':3},
            'lists':{'hc':{'strong':True,'traded':True,'view':0.7,'threshold':0.1,
                          'picks':[{'symbol':'AAA','side':'DOWN','p_up':0.2,'entry':100},
                                   {'symbol':'BBB','side':'UP','p_up':0.7,'entry':200}]}}}


def build(d=None, hour=10, minute=0):
    from app.direction_replacement import presentation
    return presentation(day() if d is None else d, dt.datetime(2026,10,12,hour,minute))


def test_replacement_preview_exists_and_is_owner_only(monkeypatch, tmp_path):
    from app import security, v133_direction
    from app.web import app
    monkeypatch.setenv('DBI_OWNER_PASSWORD','t')
    monkeypatch.setenv('DBI_MEMBER_USERNAME','m')
    monkeypatch.setenv('DBI_MEMBER_PASSWORD','mp')
    monkeypatch.setattr(v133_direction,'ROOT',tmp_path)
    security.reset_security_state_for_tests()
    client=app.test_client()
    owner={'Authorization':'Basic '+base64.b64encode(b'admin:t').decode()}
    member={'Authorization':'Basic '+base64.b64encode(b'm:mp').decode()}
    response=client.get('/admin/call-v1-replacement',headers=owner)
    assert response.status_code == 200
    assert b'RESEARCH' in response.data
    assert client.get('/admin/call-v1-replacement',headers=member).status_code == 403
    assert not list(tmp_path.iterdir())  # reading the preview creates no recorder or file


def test_first_pick_only_and_no_option_decision_or_live_entry():
    d=day(); before=copy.deepcopy(d); result=build(d)
    assert result['state']=='DIRECTION_WATCH'
    assert result['pick']['symbol']=='AAA' and result['pick']['side']=='DOWN'
    assert result['entry_allowed'] is False and result['validated'] is False
    assert 'option' not in result['pick'] and d==before


def test_normal_day_never_inherits_every_day_pool_trade_flag():
    d=day();d['lists']['hc']['strong']=False;d['lists']['hc']['view']=0.55
    assert build(d)['state']=='NO_SETUP' and build(d)['pick'] is None


@pytest.mark.parametrize('change',[{'hc_quantile':0.9},{'window':100}])
def test_settings_drift_cannot_borrow_q80_result(change):
    d=day();d['settings'].update(change)
    assert build(d)['state']=='INCOMPATIBLE_PROFILE'


def test_prior_day_is_not_current_and_future_timestamp_not_usable():
    d=day();d['date']='2026-10-09'
    assert build(d)['state']=='WAITING'
    d=day();d['at']='2026-10-12 10:01:00'
    assert build(d)['state']=='INVALID_RECORD'


def test_persistence_is_tracking_not_entry_permission_and_ends_at_exit():
    assert build(hour=15,minute=14)['state']=='DIRECTION_WATCH'
    assert build(hour=15,minute=15)['state']=='HORIZON_ENDED'
    assert build(hour=15,minute=15)['entry_allowed'] is False


def test_invalid_first_pick_does_not_refill_from_second_name():
    d=day();d['lists']['hc']['picks'][0]['p_up']=float('nan')
    assert build(d)['state']=='INVALID_RECORD' and build(d)['pick'] is None


def test_default_cards_retire_call_entry_prompts_but_keep_both_scout_directions():
    import json
    import subprocess
    from pathlib import Path
    source=Path('app/templates/index.html').read_text()
    # The next function is a reliable template boundary.
    end=source.find('\n  function ',source.index('  function renderV123Focus')+10)
    code=source[source.index('  function v12Esc'):end]
    fixture={'symbol':'AAA','direction':'Bullish','lifecycle':'READY','live_price':100,
             'call_v1_scout':True,'call_execution_candidate':{'state':'PASS','trade_window':{'contract':'AAA100CE','phase':'ENTRY_OPEN','entry_seconds_left':60,'entry_price_ok':True}}}
    down={'symbol':'BBB','direction':'Bearish','flow_scout':True}
    js='''const nodes={};const document={body:{dataset:{callV1Mode:'retired'}},getElementById:(id)=>nodes[id]||(nodes[id]={innerHTML:'',textContent:''})};\n'''+code+'\nrenderV123Focus('+json.dumps({'active_now':[fixture]})+',{},'+json.dumps({'candidates':[fixture,down]})+''');
    console.log(JSON.stringify({active:nodes['v123-focus-active'].innerHTML,scouts:nodes['v123-call-scouts'].innerHTML}));'''
    result=subprocess.run(['node','-e',js],capture_output=True,text=True,check=True)
    html=json.loads(result.stdout)
    assert 'CALL V1 ENTRY OPEN' not in html['active']
    assert 'ENTRY OPEN' not in html['scouts']
    assert 'AAA' in html['scouts'] and 'BBB' in html['scouts']


def test_serialized_rounding_preserves_producer_strong_and_up_classification():
    d=day()
    # Producer computed view .60004, threshold .10000, p_up .50004, then rounded four places.
    d['lists']['hc'].update(view=0.6,threshold=0.1,strong=True)
    d['lists']['hc']['picks'][0].update(side='UP',p_up=0.5)
    result=build(d)
    assert result['state']=='DIRECTION_WATCH' and result['pick']['side']=='UP'


def test_large_strong_flag_inconsistency_is_rejected():
    d=day();d['lists']['hc'].update(view=0.5,strong=True)
    assert build(d)['state']=='INVALID_RECORD'


@pytest.mark.parametrize('field,value',[('settings',['bad']),('lists',['bad'])])
def test_malformed_nested_record_types_fail_closed(field,value):
    d=day();d[field]=value
    assert build(d)['state']=='INVALID_RECORD'


def test_unreadable_existing_storage_returns_data_unavailable(tmp_path):
    from app.direction_replacement import stored_presentation
    (tmp_path/'days.jsonl').mkdir()
    result=stored_presentation(tmp_path,dt.datetime(2026,10,12,10))
    assert result['state']=='DATA_UNAVAILABLE' and result['pick'] is None
