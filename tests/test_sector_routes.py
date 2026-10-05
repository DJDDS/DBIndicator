import base64
import pytest
from app import web, security


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('DBI_OWNER_USERNAME','owner')
    monkeypatch.setenv('DBI_OWNER_PASSWORD','test-owner-sector-password')
    monkeypatch.setattr(web,'_scanner_started',True)
    security._AUTH_FAILURES.clear()
    return web.app.test_client()


def auth():
    return {'Authorization':'Basic '+base64.b64encode(b'owner:test-owner-sector-password').decode()}


def test_sector_routes_exist_and_require_auth(client):
    assert client.get('/sector-analysis').status_code==401
    assert client.get('/api/sector-analysis').status_code==401


def test_page_uses_local_assets_and_mobile_viewport(client,monkeypatch):
    assert hasattr(web,'sector_service'), 'Sector routes not implemented'
    monkeypatch.setattr(web.sector_service,'ensure_started',lambda:None)
    r=client.get('/sector-analysis',headers=auth())
    assert r.status_code==200
    assert b'width=device-width' in r.data
    assert b'/static/sector_analysis.js' in r.data


def test_api_returns_cached_data_and_bad_selection_is_404(client,monkeypatch):
    assert hasattr(web,'sector_service'), 'Sector routes not implemented'
    monkeypatch.setattr(web.sector_service,'ensure_started',lambda:None)
    monkeypatch.setattr(web.sector_service,'overview',lambda:{'sectors':[],'status':{'error':'Kite login required'}})
    assert client.get('/api/sector-analysis',headers=auth()).json['sectors']==[]
    monkeypatch.setattr(web.sector_service,'detail',lambda sector,window:None)
    assert client.get('/api/sector-analysis/missing',headers=auth()).status_code==404
    assert client.get('/api/sector-analysis/missing?window=bad',headers=auth()).status_code==400
