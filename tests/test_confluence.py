import base64

import pytest

from app import chart_patterns, confluence, security, v135_hierarchy, web


def _patch(monkeypatch, pattern_rows, ind_map, results):
    monkeypatch.setattr(chart_patterns, 'load_results', lambda: {'results': []})
    monkeypatch.setattr(chart_patterns, 'public_rows', lambda payload: pattern_rows)
    monkeypatch.setattr(v135_hierarchy, 'industry_map', lambda: ind_map)
    confluence._CACHE.update(at=0.0, value=[])
    return results


# IT Services industry (4 members, meets MIN_INDUSTRY_GROUP): TCS +0.5, INFY -0.5, WIPRO +0.3, HCLTECH +0.1
# -> industry median +0.2. Plus four unmapped stocks (RELIANCE 0.0, ONGC -0.05, TATASTEEL +0.02, HINDALCO -0.02)
# -> whole-market median +0.01. Industry edge = +0.19, over the +0.15pp threshold either way.
_IND_MAP = {'TCS': 'IT Services', 'INFY': 'IT Services', 'WIPRO': 'IT Services', 'HCLTECH': 'IT Services'}
_RESULTS = [
    {'symbol': 'TCS', 'oi_structure': 'Long Buildup', 'price_chg_today_pct': 0.5},
    {'symbol': 'INFY', 'oi_structure': 'Short Buildup', 'price_chg_today_pct': -0.5},
    {'symbol': 'WIPRO', 'price_chg_today_pct': 0.3},
    {'symbol': 'HCLTECH', 'price_chg_today_pct': 0.1},
    {'symbol': 'RELIANCE', 'price_chg_today_pct': 0.0},
    {'symbol': 'ONGC', 'price_chg_today_pct': -0.05},
    {'symbol': 'TATASTEEL', 'price_chg_today_pct': 0.02},
    {'symbol': 'HINDALCO', 'price_chg_today_pct': -0.02},
]
_PATTERN_ROWS = [
    {'symbol': 'TCS', 'pattern': 'Bull Flag', 'status': 'BREAKOUT', 'direction': 'BULL', 'tier': 'A', 'tier_rank': 0},
    {'symbol': 'INFY', 'pattern': 'Head and Shoulders', 'status': 'RETEST_HOLD', 'direction': 'BEAR', 'tier': 'WATCH', 'tier_rank': 1},
    {'symbol': 'WIPRO', 'pattern': 'Ascending Triangle', 'status': 'FORMING', 'direction': 'BULL', 'tier': 'NONE', 'tier_rank': 2},
    {'symbol': 'ZEEL', 'pattern': 'Descending Triangle', 'status': 'EXTENDED', 'direction': 'BEAR', 'tier': 'NONE', 'tier_rank': 2},
]


def test_confluence_excludes_inactive_status_patterns(monkeypatch):
    _patch(monkeypatch, _PATTERN_ROWS, _IND_MAP, _RESULTS)
    rows = confluence.watch(_RESULTS)
    assert not any(r['symbol'] == 'WIPRO' for r in rows)   # FORMING never gets to confirmation stage


def test_confluence_excludes_patterns_with_only_one_signal(monkeypatch):
    _patch(monkeypatch, _PATTERN_ROWS, _IND_MAP, _RESULTS)
    rows = confluence.watch(_RESULTS)
    assert not any(r['symbol'] == 'ZEEL' for r in rows)    # no OI row, no mapped industry -> pattern alone isn't enough


def test_confluence_oi_and_sector_both_confirm_and_sort_first(monkeypatch):
    _patch(monkeypatch, _PATTERN_ROWS, _IND_MAP, _RESULTS)
    rows = confluence.watch(_RESULTS)
    assert rows[0]['symbol'] == 'TCS'
    assert rows[0]['agree'] == 3
    assert any('OI' in s for s in rows[0]['signals'])
    assert any('Sector' in s for s in rows[0]['signals'])
    assert rows[0]['direction'] == 'UP'


def test_confluence_oi_only_confirms_bear_pattern(monkeypatch):
    _patch(monkeypatch, _PATTERN_ROWS, _IND_MAP, _RESULTS)
    rows = confluence.watch(_RESULTS)
    infy = next(r for r in rows if r['symbol'] == 'INFY')
    assert infy['agree'] == 2                               # OI (Short Buildup) agrees, sector doesn't (wrong side)
    assert infy['direction'] == 'DOWN'
    assert any('OI' in s for s in infy['signals'])
    assert not any('Sector' in s for s in infy['signals'])


def test_confluence_tier_rank_breaks_ties_when_agree_equal(monkeypatch):
    pattern_rows = [
        {'symbol': 'HDFCBANK', 'pattern': 'Flag', 'status': 'BREAKOUT', 'direction': 'BULL', 'tier': 'WATCH', 'tier_rank': 1},
        {'symbol': 'ICICIBANK', 'pattern': 'Flag', 'status': 'BREAKOUT', 'direction': 'BULL', 'tier': 'NONE', 'tier_rank': 2},
    ]
    results = [
        {'symbol': 'HDFCBANK', 'oi_structure': 'Long Buildup', 'price_chg_today_pct': 0.1},
        {'symbol': 'ICICIBANK', 'oi_structure': 'Long Buildup', 'price_chg_today_pct': 0.1},
    ]
    _patch(monkeypatch, pattern_rows, {}, results)
    rows = confluence.watch(results)
    assert [r['symbol'] for r in rows] == ['HDFCBANK', 'ICICIBANK']   # both agree=2; better tier_rank sorts first


def test_confluence_is_cached_per_ttl_window(monkeypatch):
    calls = {'patterns': 0, 'ind': 0}

    def fake_public_rows(payload):
        calls['patterns'] += 1
        return []

    monkeypatch.setattr(chart_patterns, 'load_results', lambda: {'results': []})
    monkeypatch.setattr(chart_patterns, 'public_rows', fake_public_rows)
    monkeypatch.setattr(v135_hierarchy, 'industry_map', lambda: (calls.__setitem__('ind', calls['ind'] + 1), {})[1])
    confluence._CACHE.update(at=0.0, value=[])
    confluence.watch([])
    confluence.watch([])
    confluence.watch([])
    assert calls == {'patterns': 1, 'ind': 1}


def test_confluence_keeps_stale_value_on_error(monkeypatch):
    confluence._CACHE.update(at=0.0, value=[{'symbol': 'STALE'}])

    def boom():
        raise RuntimeError('pattern file corrupt')

    monkeypatch.setattr(chart_patterns, 'load_results', boom)
    try:
        rows = confluence.watch([])
        assert rows == [{'symbol': 'STALE'}]
    finally:
        confluence._CACHE.update(at=0.0, value=[])   # don't leak the malformed stale row into later tests


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('DBI_OWNER_USERNAME', 'owner')
    monkeypatch.setenv('DBI_OWNER_PASSWORD', 'test-owner-confluence-password')
    monkeypatch.setattr(web, '_scanner_started', True)
    security._AUTH_FAILURES.clear()
    confluence._CACHE.update(at=0.0, value=[])   # earlier tests may have left a stale/fake cached value
    return web.app.test_client()


def _auth():
    return {'Authorization': 'Basic ' + base64.b64encode(b'owner:test-owner-confluence-password').decode()}


def test_dashboard_state_api_exposes_confluence_watch(client, monkeypatch):
    monkeypatch.setattr(web, '_confluence_watch_safe',
                         lambda results: [{'symbol': 'TCS', 'direction': 'UP', 'pattern': 'Bull Flag',
                                            'status': 'BREAKOUT', 'tier': 'A', 'industry': 'IT Services',
                                            'signals': ['Pattern: Bull Flag (Breakout)'], 'agree': 2}])
    r = client.get('/api/dashboard-state', headers=_auth())
    assert r.status_code == 200
    body = r.get_json()
    assert body.get('confluence_watch')[0]['symbol'] == 'TCS'


def test_dashboard_page_renders_confluence_watch_markup(client):
    r = client.get('/', headers=_auth())
    assert r.status_code == 200
    assert b'cwatch-list' in r.data
