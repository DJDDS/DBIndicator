import base64
import os
import stat

import pytest
from flask import Flask

from app import audit_access, config, kite_auth
from app.security import (
    AUDITOR,
    MEMBER,
    OWNER,
    install_security_headers,
    issue_kite_callback_token,
    require_roles,
    reset_security_state_for_tests,
    verify_kite_callback_token,
)


def _auth(username, password):
    raw = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


@pytest.fixture(autouse=True)
def security_env(monkeypatch):
    reset_security_state_for_tests()
    monkeypatch.setenv("DBI_OWNER_USERNAME", "owner")
    monkeypatch.setenv("DBI_OWNER_PASSWORD", "owner-secret")
    monkeypatch.setenv("DBI_MEMBER_USERNAME", "member")
    monkeypatch.setenv("DBI_MEMBER_PASSWORD", "member-secret")
    monkeypatch.setenv("DBI_AUDITOR_USERNAME", "auditor")
    monkeypatch.setenv("DBI_AUDITOR_PASSWORD", "auditor-secret")
    monkeypatch.setenv("DBI_CALLBACK_SECRET", "test-callback-secret")
    monkeypatch.setenv("DBI_AUTH_MAX_FAILURES", "8")
    monkeypatch.setenv("DBI_AUTH_WINDOW_SECONDS", "900")
    monkeypatch.setenv("DBI_AUTH_LOCKOUT_SECONDS", "900")
    yield
    reset_security_state_for_tests()


def _app():
    app = Flask(__name__)
    install_security_headers(app)

    @app.get("/scanner")
    @require_roles(OWNER, MEMBER)
    def scanner():
        return "scanner"

    @app.get("/audit")
    @require_roles(OWNER, AUDITOR)
    def audit():
        return "audit"

    @app.post("/admin")
    @require_roles(OWNER)
    def admin():
        return "admin"

    return app


def test_member_can_read_scanner_but_cannot_admin():
    client = _app().test_client()
    assert client.get("/scanner", headers=_auth("member", "member-secret")).status_code == 200
    assert client.post("/admin", headers=_auth("member", "member-secret")).status_code == 403


def test_auditor_is_read_only_and_separate_from_scanner():
    client = _app().test_client()
    assert client.get("/audit", headers=_auth("auditor", "auditor-secret")).status_code == 200
    assert client.get("/scanner", headers=_auth("auditor", "auditor-secret")).status_code == 403
    assert client.post("/admin", headers=_auth("auditor", "auditor-secret")).status_code == 403


def test_owner_can_use_all_role_surfaces():
    client = _app().test_client()
    headers = _auth("owner", "owner-secret")
    assert client.get("/scanner", headers=headers).status_code == 200
    assert client.get("/audit", headers=headers).status_code == 200
    assert client.post("/admin", headers=headers).status_code == 200


def test_wrong_password_is_rejected_and_username_is_not_ignored():
    client = _app().test_client()
    assert client.get("/scanner", headers=_auth("member", "wrong")).status_code == 401
    assert client.get("/scanner", headers=_auth("anything", "owner-secret")).status_code == 401


def test_browser_unauthenticated_challenges_never_trigger_lockout():
    client = _app().test_client()
    for _ in range(20):
        assert client.get("/scanner").status_code == 401
    assert client.get("/scanner", headers=_auth("owner", "owner-secret")).status_code == 200


def test_valid_credentials_bypass_and_clear_existing_lockout():
    client = _app().test_client()
    statuses = [
        client.get("/scanner", headers=_auth("owner", "wrong-secret")).status_code
        for _ in range(8)
    ]
    assert statuses[-1] == 429
    assert client.get("/scanner", headers=_auth("owner", "owner-secret")).status_code == 200
    assert client.get("/scanner", headers=_auth("owner", "wrong-secret")).status_code == 401


def test_security_headers_are_present():
    client = _app().test_client()
    response = client.get("/scanner", headers=_auth("owner", "owner-secret"))
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "camera=()" in response.headers["Permissions-Policy"]


def test_cross_site_write_is_blocked():
    client = _app().test_client()
    response = client.post(
        "/admin",
        headers={**_auth("owner", "owner-secret"), "Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_signed_kite_callback_token_round_trip():
    token = issue_kite_callback_token()
    assert verify_kite_callback_token(token)
    assert not verify_kite_callback_token(token + "tampered")
    assert not verify_kite_callback_token(None)


def test_audit_path_traversal_is_blocked():
    assert audit_access.resolve_audit_file("../.env") is None
    assert audit_access.resolve_audit_file("research/../../.env") is None
    assert audit_access.resolve_audit_file("top/.env") is None


def test_phase_a_contains_no_broker_order_execution_calls():
    from pathlib import Path

    forbidden = (
        ".place_order(", ".modify_order(", ".cancel_order(",
        ".place_gtt(", ".modify_gtt(", ".delete_gtt(",
        ".place_autoslice_order(", ".place_mf_order(",
    )
    offenders = []
    for path in Path("app").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            if needle in text:
                offenders.append(f"{path}:{needle}")
    assert offenders == [], "Phase A must remain scanner/research-only: " + ", ".join(offenders)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not meaningful on Windows")
def test_kite_token_cache_is_owner_only(tmp_path, monkeypatch):
    token_file = tmp_path / "kite-token.json"
    monkeypatch.setattr(config, "TOKEN_CACHE_FILE", str(token_file))
    kite_auth._save_cache("test-token")
    mode = stat.S_IMODE(token_file.stat().st_mode)
    assert mode == 0o600
    assert "test-token" in token_file.read_text()
