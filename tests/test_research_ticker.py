import base64

import pytest

from app import security, v133_direction, web


def _no_days(name):
    return []


def _combined_reader(today, days_rows=None, rs_rows=None, h_rows=None):
    """v133_direction, v134_relstrength and v135_hierarchy all read through the SAME shared
    `_read_jsonl` on the v133_direction module (v134/v135 import it as `d`), so one fake must
    route all three file names rather than patching the attribute three times."""
    import app.v134_relstrength as rs
    import app.v135_hierarchy as h

    def reader(name):
        if name == 'days.jsonl': return days_rows or []
        if name == rs.FILE: return rs_rows or []
        if name == h.FILE: return h_rows or []
        return []
    return reader


def test_ticker_excludes_direction_desk_and_includes_rs_pick(monkeypatch):
    today = str(v133_direction.now_ist().date())
    reader = _combined_reader(today,
        days_rows=[{'date': today, 'lists': {'hc': {'lean': 'DOWN', 'strong': True, 'view': 'x',
                                                      'picks': [{'symbol': 'TCS', 'side': 'UP'}]}}}],
        rs_rows=[{'date': today, 'slots': {'11:15': {'picks': [
            {'symbol': 'HDFCBANK', 'side': 'UP', 'z': 3.8, 'grade': 'A'}]}}}])
    monkeypatch.setattr(v133_direction, '_read_jsonl', reader)
    v133_direction._DESK.update(at=0.0, value=None)
    v133_direction._RESEARCH_CACHE.update(at=0.0, ticker=[], scoreboard=[])
    items = v133_direction.research_ticker()
    assert not any(it['tag'] == 'DIRECTION' for it in items)                 # direction desk excluded from the ticker
    rsi = [it for it in items if it['tag'] == 'REL-STRENGTH']
    assert rsi and rsi[0]['symbol'] == 'HDFCBANK' and rsi[0]['side'] == 'UP' and 'grade A' in rsi[0]['text']


def test_ticker_includes_hierarchy_pick(monkeypatch):
    today = str(v133_direction.now_ist().date())
    reader = _combined_reader(today, h_rows=[{'date': today, 'picks': [
        {'symbol': 'IDEA', 'side': 'DOWN', 'industry': 'Telecom', 'role': 'PICK', 'entry': 10.0, 'out': {'30': -0.8}}]}])
    monkeypatch.setattr(v133_direction, '_read_jsonl', reader)
    v133_direction._DESK.update(at=0.0, value=None)
    v133_direction._RESEARCH_CACHE.update(at=0.0, ticker=[], scoreboard=[])
    items = v133_direction.research_ticker()
    hier = [it for it in items if it['tag'].startswith('HIERARCHY')]
    assert hier and hier[0]['symbol'] == 'IDEA' and hier[0]['side'] == 'DOWN' and '-0.80%' in hier[0]['text']
    assert 'PICK' in hier[0]['tag']


def test_ticker_is_empty_list_when_nothing_available(monkeypatch):
    today = str(v133_direction.now_ist().date())
    monkeypatch.setattr(v133_direction, '_read_jsonl', _combined_reader(today))
    v133_direction._DESK.update(at=0.0, value=None)
    v133_direction._RESEARCH_CACHE.update(at=0.0, ticker=[], scoreboard=[])
    assert v133_direction.research_ticker() == []


def test_scoreboard_includes_all_three_desks_with_settled_picks(monkeypatch):
    import app.v134_relstrength as rs
    import app.v135_hierarchy as h
    monkeypatch.setattr(v133_direction, '_read_jsonl', lambda name: (
        [{'date': '2026-09-01', 'lists': {'hc': {'traded': True, 'strong': True,
            'picks': [{'symbol': 'TCS', 'side': 'UP', 'win': True, 'ret_pct': 0.9}]}}}] if name == 'days.jsonl' else []))
    monkeypatch.setattr(rs, 'status', lambda: {'score': {'all': {'n': 10, 'win': 60.0}, 'edge_avg_pct': 0.21}})
    monkeypatch.setattr(h, 'status', lambda: {'score': {'pick': {'exit': {'n': 5, 'win': 52.0, 'net_bps': 4.5}}}})
    v133_direction._RESEARCH_CACHE.update(at=0.0, ticker=[], scoreboard=[])
    rows = v133_direction.research_scoreboard()
    names = {r['name'] for r in rows}
    assert names == {'Direction desk', 'Relative-strength desk', 'Hierarchy desk'}
    rs_row = next(r for r in rows if r['name'] == 'Relative-strength desk')
    assert rs_row['n'] == 10 and rs_row['win'] == 60.0 and '+0.210%' in rs_row['metric']
    h_row = next(r for r in rows if r['name'] == 'Hierarchy desk')
    assert h_row['n'] == 5 and h_row['win'] == 52.0 and '+4.5 bps' in h_row['metric']


def test_scoreboard_skips_a_desk_with_no_settled_picks(monkeypatch):
    import app.v134_relstrength as rs
    import app.v135_hierarchy as h
    monkeypatch.setattr(v133_direction, '_read_jsonl', _no_days)
    monkeypatch.setattr(rs, 'status', lambda: {'score': {'all': {'n': 0, 'win': None}}})
    monkeypatch.setattr(h, 'status', lambda: {'score': {'pick': {'exit': {'n': 0}}}})
    v133_direction._RESEARCH_CACHE.update(at=0.0, ticker=[], scoreboard=[])
    assert v133_direction.research_scoreboard() == []


def test_ticker_and_scoreboard_share_one_cached_read_per_ttl_window(monkeypatch):
    """Regression for the dashboard slowdown: research_ticker()/research_scoreboard() must not each re-read and
    re-score every desk's whole history on every call - the main dashboard calls both every 8s."""
    import app.v134_relstrength as rs
    import app.v135_hierarchy as h
    calls = {'rs': 0, 'h': 0, 'days': 0}

    def rs_status():
        calls['rs'] += 1
        return {'today': {'slots': {}}, 'score': {'all': {'n': 0, 'win': None}}}

    def h_status():
        calls['h'] += 1
        return {'today': {'picks': []}, 'score': {'pick': {'exit': {'n': 0}}}}

    def reader(name):
        calls['days'] += 1
        return []

    monkeypatch.setattr(rs, 'status', rs_status)
    monkeypatch.setattr(h, 'status', h_status)
    monkeypatch.setattr(v133_direction, '_read_jsonl', reader)
    v133_direction._RESEARCH_CACHE.update(at=0.0, ticker=[], scoreboard=[])
    v133_direction.research_ticker()
    v133_direction.research_scoreboard()
    v133_direction.research_ticker()
    v133_direction.research_scoreboard()
    assert calls == {'rs': 1, 'h': 1, 'days': 1}          # one read of each desk's history, not four


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('DBI_OWNER_USERNAME', 'owner')
    monkeypatch.setenv('DBI_OWNER_PASSWORD', 'test-owner-ticker-password')
    monkeypatch.setattr(web, '_scanner_started', True)
    security._AUTH_FAILURES.clear()
    return web.app.test_client()


def _auth():
    return {'Authorization': 'Basic ' + base64.b64encode(b'owner:test-owner-ticker-password').decode()}


def test_dashboard_state_api_exposes_ticker_and_scoreboard(client, monkeypatch):
    monkeypatch.setattr(v133_direction, 'research_ticker',
                         lambda: [{'tag': 'REL-STRENGTH', 'symbol': 'TCS', 'side': 'UP', 'text': 'x', 'cls': 'up'}])
    monkeypatch.setattr(v133_direction, 'research_scoreboard',
                         lambda: [{'name': 'Hierarchy desk', 'sub': 's', 'n': 5, 'win': 52.0, 'metric_label': 'm', 'metric': '+4.5 bps', 'href': '/x'}])
    r = client.get('/api/dashboard-state', headers=_auth())
    assert r.status_code == 200
    body = r.get_json()
    assert body.get('research_ticker') == [{'tag': 'REL-STRENGTH', 'symbol': 'TCS', 'side': 'UP', 'text': 'x', 'cls': 'up'}]
    assert body.get('research_scoreboard')[0]['name'] == 'Hierarchy desk'


def test_dashboard_page_renders_ticker_and_scoreboard_markup(client):
    r = client.get('/', headers=_auth())
    assert r.status_code == 200
    assert b'rticker-track' in r.data
    assert b'rscore-grid' in r.data
