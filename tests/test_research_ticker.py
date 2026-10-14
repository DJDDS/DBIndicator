import base64

import pytest

from app import security, v133_direction, web


def _no_days(name):
    return []


def test_ticker_includes_direction_pick_when_present(monkeypatch):
    today = str(v133_direction.now_ist().date())
    monkeypatch.setattr(v133_direction, '_read_jsonl', lambda name: (
        [{'date': today, 'lists': {'hc': {'lean': 'DOWN', 'strong': True, 'view': 'x',
                                           'picks': [{'symbol': 'TCS', 'side': 'UP'}]}}}] if name == 'days.jsonl' else []))
    v133_direction._DESK.update(at=0.0, value=None)
    items = v133_direction.research_ticker()
    assert any(it['tag'] == 'DIRECTION' and it['symbol'] == 'TCS' and it['side'] == 'UP' for it in items)


def test_ticker_includes_hierarchy_pick(monkeypatch):
    import app.v135_hierarchy as h
    today = str(v133_direction.now_ist().date())
    monkeypatch.setattr(v133_direction, '_read_jsonl', _no_days)
    v133_direction._DESK.update(at=0.0, value=None)
    monkeypatch.setattr(h.d, '_read_jsonl', lambda name: (
        [{'date': today, 'picks': [{'symbol': 'IDEA', 'side': 'DOWN', 'industry': 'Telecom', 'role': 'PICK',
                                     'entry': 10.0, 'out': {'30': -0.8}}]}] if name == h.FILE else []))
    items = v133_direction.research_ticker()
    hier = [it for it in items if it['tag'].startswith('HIERARCHY')]
    assert hier and hier[0]['symbol'] == 'IDEA' and hier[0]['side'] == 'DOWN' and '-0.80%' in hier[0]['text']


def test_ticker_is_empty_list_when_nothing_available(monkeypatch):
    import app.v134_relstrength as rs
    import app.v135_hierarchy as h
    monkeypatch.setattr(v133_direction, '_read_jsonl', _no_days)
    v133_direction._DESK.update(at=0.0, value=None)
    monkeypatch.setattr(rs.d, '_read_jsonl', _no_days)
    monkeypatch.setattr(h.d, '_read_jsonl', _no_days)
    assert v133_direction.research_ticker() == []


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('DBI_OWNER_USERNAME', 'owner')
    monkeypatch.setenv('DBI_OWNER_PASSWORD', 'test-owner-ticker-password')
    monkeypatch.setattr(web, '_scanner_started', True)
    security._AUTH_FAILURES.clear()
    return web.app.test_client()


def _auth():
    return {'Authorization': 'Basic ' + base64.b64encode(b'owner:test-owner-ticker-password').decode()}


def test_dashboard_state_api_exposes_ticker(client, monkeypatch):
    monkeypatch.setattr(v133_direction, 'research_ticker',
                         lambda: [{'tag': 'DIRECTION', 'symbol': 'TCS', 'side': 'UP', 'text': 'x', 'cls': 'up'}])
    r = client.get('/api/dashboard-state', headers=_auth())
    assert r.status_code == 200
    assert r.get_json().get('research_ticker') == [{'tag': 'DIRECTION', 'symbol': 'TCS', 'side': 'UP', 'text': 'x', 'cls': 'up'}]


def test_dashboard_page_renders_ticker_markup(client):
    r = client.get('/', headers=_auth())
    assert r.status_code == 200
    assert b'rticker-track' in r.data
