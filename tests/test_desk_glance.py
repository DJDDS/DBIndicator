from app import desk_glance as g


def _day(date, picks):
    return {'date': date, 'lists': {'hc': {'picks': picks, 'lean': 'DOWN', 'strong': False, 'exit_at': '15:15'}}}


def test_pod_block_today_and_record():
    pk = lambda sym, grade, win=None: {'symbol': sym, 'side': 'DOWN', 'grade': grade, 'conf': 0.15, 'entry': 100.0, 'status': 'OPEN', **({'win': win, 'ret_pct': 0.5 if win else -0.5} if win is not None else {})}
    days = [_day('2026-10-12', [pk('AAA', 'A', True)]), _day('2026-10-13', [pk('BBB', 'B', False)]), _day('2026-10-14', [pk('CCC', 'A'), pk('DDD', 'B')])]
    out = g.pod_block(days, '2026-10-14')
    assert out['today']['symbol'] == 'CCC' and out['today']['grade'] == 'A' and out['today']['trades'] == 2
    assert out['record'] == {'n': 2, 'win': 50.0, 'a_n': 1, 'a_win': 100.0}
    assert g.pod_block([], '2026-10-14') == {'today': None, 'record': None}


def test_rs_ng_and_glance_assemble():
    rs = [{'date': '2026-10-14', 'slots': {'11:15': {'picks': [{'symbol': 'X', 'side': 'UP', 'z': 4.1, 'grade': 'A', 'ret_pct': 0.3, 'win': True}, {'symbol': 'Y', 'side': 'DOWN', 'z': -3.1, 'grade': 'B'}], 'regime': 'FLAT', 'market_pct': 0.2}}}]
    r = g.rs_block(rs, '2026-10-14'); assert r['n'] == 2 and r['top']['symbol'] == 'X' and r['settled'] == 1 and r['wins'] == 1
    assert g.rs_block(rs, '2026-10-15') is None
    ng = g.ng_block({'signal': {'side': 'PUT', 'leg': 'THU', 'hold': 1, 'provisional': True}, 'next_check': None, 'session': {'is_open': True}})
    assert ng['signal']['side'] == 'PUT' and ng['open'] is True and g.ng_block(None) is None
    out = g.glance([], rs, '2026-10-14', None, {'ok': 17, 'problems': 0}, True)
    assert out['kite'] is True and out['rec']['ok'] == 17 and out['pod'] == {'today': None, 'record': None}


def test_glance_route_is_owner_only_and_dashboard_has_the_strip(monkeypatch):
    import base64
    import run  # noqa: F401
    from app import security, web, commodity_desk
    monkeypatch.setenv("DBI_OWNER_USERNAME", "owner"); monkeypatch.setenv("DBI_OWNER_PASSWORD", "test-owner-glance-pw")
    monkeypatch.setenv("DBI_MEMBER_USERNAME", "member"); monkeypatch.setenv("DBI_MEMBER_PASSWORD", "test-member-glance-pw")
    monkeypatch.setattr(web, "_scanner_started", True); monkeypatch.setattr(commodity_desk, "cached_status", lambda *a, **k: None)
    security._AUTH_FAILURES.clear()
    auth = lambda u, p: {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}
    c = web.app.test_client()
    assert c.get("/api/desks/glance").status_code == 401
    m = c.get("/api/desks/glance", headers=auth("member", "test-member-glance-pw"))
    assert m.status_code == 200 and m.get_json()["role"] == "member"             # members see the strip too (read-only; no links to the admin desks)
    r = c.get("/api/desks/glance", headers=auth("owner", "test-owner-glance-pw"))
    assert r.status_code == 200 and {"kite", "pod", "rs", "ng", "rec"} <= set(r.get_json())
    page = c.get("/", headers=auth("owner", "test-owner-glance-pw")).data
    assert b'id="mo-desks"' in page and b"/api/desks/glance" in page
