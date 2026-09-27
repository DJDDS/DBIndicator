"""Phase-A collaborative security controls for DBIndicator.

This module intentionally keeps HTTP Basic authentication during the development
phase so existing clients remain simple, but adds named identities, role based
authorization, constant-time credential checks, login throttling, response
hardening, and a signed short-lived Kite callback cookie.

Phase B can replace Basic Auth with session/TOTP without changing route roles.
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Iterable

from flask import Response, g, jsonify, redirect, request, session

log = logging.getLogger(__name__)

OWNER = "owner"
MEMBER = "member"
AUDITOR = "auditor"
VALID_ROLES = frozenset({OWNER, MEMBER, AUDITOR})

_AUTH_FAILURES: dict[tuple[str, str], deque[float]] = defaultdict(deque)
_AUTH_LOCK = threading.Lock()


@dataclass(frozen=True)
class Credential:
    username: str
    password: str
    role: str


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _credentials() -> tuple[Credential, ...]:
    """Return configured identities without ever logging secret values."""
    owner_username = (os.getenv("DBI_OWNER_USERNAME") or "admin").strip()
    owner_password = os.getenv("DBI_OWNER_PASSWORD") or os.getenv("DASHBOARD_PASSWORD") or ""

    configured: list[Credential] = []
    if owner_username and owner_password:
        configured.append(Credential(owner_username, owner_password, OWNER))

    for role, user_var, pass_var in (
        (MEMBER, "DBI_MEMBER_USERNAME", "DBI_MEMBER_PASSWORD"),
        (AUDITOR, "DBI_AUDITOR_USERNAME", "DBI_AUDITOR_PASSWORD"),
    ):
        username = (os.getenv(user_var) or "").strip()
        password = os.getenv(pass_var) or ""
        # Fail closed on half-configured accounts.
        if username and password:
            configured.append(Credential(username, password, role))

    return tuple(configured)


def _safe_client_id() -> str:
    # Railway terminates TLS/proxying. The left-most XFF value is the original
    # client in normal Railway traffic; remote_addr remains the fallback.
    xff = (request.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
    return (xff or request.remote_addr or "unknown")[:128]


def _failure_key(username: str) -> tuple[str, str]:
    digest = hashlib.sha256((username or "").encode("utf-8", "ignore")).hexdigest()[:16]
    return _safe_client_id(), digest


def _prune_failures(q: deque[float], now: float, window_seconds: int) -> None:
    cutoff = now - window_seconds
    while q and q[0] < cutoff:
        q.popleft()


def _is_rate_limited(key: tuple[str, str]) -> tuple[bool, int]:
    now = time.monotonic()
    window = _env_int("DBI_AUTH_WINDOW_SECONDS", 900)
    limit = _env_int("DBI_AUTH_MAX_FAILURES", 8)
    lockout = _env_int("DBI_AUTH_LOCKOUT_SECONDS", 900)
    with _AUTH_LOCK:
        q = _AUTH_FAILURES[key]
        _prune_failures(q, now, window)
        if len(q) < limit:
            return False, 0
        remaining = int(max(1, lockout - (now - q[-1])))
        if remaining <= 1 and (now - q[-1]) >= lockout:
            q.clear()
            return False, 0
        return True, remaining


def _record_failure(key: tuple[str, str]) -> None:
    now = time.monotonic()
    window = _env_int("DBI_AUTH_WINDOW_SECONDS", 900)
    with _AUTH_LOCK:
        q = _AUTH_FAILURES[key]
        _prune_failures(q, now, window)
        q.append(now)


def _clear_failures(key: tuple[str, str]) -> None:
    with _AUTH_LOCK:
        _AUTH_FAILURES.pop(key, None)


def _match_credential(username: str, password: str) -> Credential | None:
    matched: Credential | None = None
    # Check every configured record to avoid role-dependent early-exit timing.
    for candidate in _credentials():
        user_ok = hmac.compare_digest(username or "", candidate.username)
        pass_ok = hmac.compare_digest(password or "", candidate.password)
        if user_ok and pass_ok:
            matched = candidate
    return matched


def authenticate_credentials(username: str, password: str) -> tuple[Credential | None, int]:
    """Validate a supplied username/password and apply throttling to real failures."""
    username = (username or "").strip()
    password = password or ""
    if not username or not password:
        return None, 0

    key = _failure_key(username)
    credential = _match_credential(username, password)
    if credential is not None:
        _clear_failures(key)
        return credential, 0

    limited, retry_after = _is_rate_limited(key)
    if limited:
        return None, retry_after

    _record_failure(key)
    limited, retry_after = _is_rate_limited(key)
    return None, retry_after if limited else 0


def _session_signature(credential: Credential) -> str:
    payload = f"{credential.username}|{credential.role}|{credential.password}".encode("utf-8")
    return hmac.new(_callback_secret(), payload, hashlib.sha256).hexdigest()


def start_login_session(credential: Credential) -> None:
    session.clear()
    session["dbi_username"] = credential.username
    session["dbi_role"] = credential.role
    session["dbi_sig"] = _session_signature(credential)
    session.permanent = True


def clear_login_session() -> None:
    session.clear()


def _session_credential() -> Credential | None:
    username = session.get("dbi_username")
    role = session.get("dbi_role")
    signature = session.get("dbi_sig")
    if not username or role not in VALID_ROLES or not signature:
        return None
    for credential in _credentials():
        if credential.username != username or credential.role != role:
            continue
        if hmac.compare_digest(signature, _session_signature(credential)):
            return credential
    session.clear()
    return None


def _challenge(message: str = "Login required.") -> Response:
    # Human-facing pages use the mobile-friendly form. APIs keep a standards-
    # compatible Basic challenge for tools and existing integrations.
    if request.method == "GET" and not request.path.startswith("/api/"):
        return redirect("/login")
    return Response(
        message,
        401,
        {"WWW-Authenticate": 'Basic realm="DBIndicator" charset="UTF-8"'},
    )


def require_roles(*allowed_roles: str):
    roles = frozenset(allowed_roles)
    if not roles or not roles.issubset(VALID_ROLES):
        raise ValueError(f"Invalid DBIndicator role set: {sorted(roles)}")

    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if not _credentials():
                return (
                    "DBIndicator authentication is not configured. "
                    "Set DBI_OWNER_PASSWORD or DASHBOARD_PASSWORD.",
                    500,
                )

            credential = _session_credential()
            if credential is None:
                auth = request.authorization

                # Browsers using HTTP Basic Auth commonly make an unauthenticated
                # request first and then retry after the 401 challenge. That is not
                # a failed password attempt and must never contribute to lockout.
                if not auth or not (auth.username or "") or not (auth.password or ""):
                    return _challenge()

                username = auth.username or ""
                credential, retry_after = authenticate_credentials(username, auth.password or "")
                if credential is None:
                    key = _failure_key(username)
                    if retry_after:
                        log.warning(
                            "security.auth_rate_limited path=%s client=%s",
                            request.path,
                            key[0],
                        )
                        response = jsonify({"error": "Too many failed login attempts. Try again later."})
                        response.status_code = 429
                        response.headers["Retry-After"] = str(retry_after)
                        return response
                    log.warning(
                        "security.auth_failed path=%s client=%s username_hash=%s",
                        request.path,
                        key[0],
                        key[1],
                    )
                    return _challenge("Invalid credentials.")

            g.security_role = credential.role
            g.security_username = credential.username

            if credential.role not in roles:
                log.warning(
                    "security.role_denied path=%s role=%s user=%s",
                    request.path,
                    credential.role,
                    credential.username,
                )
                return jsonify({"error": "This account does not have permission for this action."}), 403

            return view(*args, **kwargs)

        return wrapped

    return decorator


def require_dashboard_password(view):
    """Compatibility decorator: legacy-protected routes become owner-only."""
    return require_roles(OWNER)(view)


def current_role() -> str | None:
    return getattr(g, "security_role", None)


def current_username() -> str | None:
    return getattr(g, "security_username", None)


def _request_is_https() -> bool:
    if request.is_secure:
        return True
    return (request.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0].strip().lower() == "https"


def install_security_headers(app) -> None:
    """Install session settings, low-risk headers and a cross-site write guard."""
    app.secret_key = _callback_secret()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=kite_callback_cookie_secure(),
        PERMANENT_SESSION_LIFETIME=43200,
    )

    @app.before_request
    def _same_origin_write_guard():
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None
        origin = request.headers.get("Origin")
        if not origin:
            return None
        scheme = "https" if _request_is_https() else request.scheme
        expected = f"{scheme}://{request.host}".rstrip("/")
        if not hmac.compare_digest(origin.rstrip("/"), expected):
            log.warning(
                "security.cross_site_write_blocked path=%s origin=%s",
                request.path,
                origin[:256],
            )
            return jsonify({"error": "Cross-site write request blocked."}), 403
        return None

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        if _request_is_https():
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        if request.path.startswith("/api/") or request.path.startswith("/settings"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


def _callback_secret() -> bytes:
    explicit = os.getenv("DBI_CALLBACK_SECRET") or ""
    if explicit:
        return explicit.encode("utf-8")
    # Phase-A compatibility fallback. Phase B should require a dedicated secret.
    owner_password = os.getenv("DBI_OWNER_PASSWORD") or os.getenv("DASHBOARD_PASSWORD") or ""
    return hashlib.sha256(("dbindicator-kite-callback:" + owner_password).encode("utf-8")).digest()


def issue_kite_callback_token() -> str:
    ts = int(time.time())
    nonce = secrets.token_urlsafe(24)
    payload = f"{ts}.{nonce}"
    sig = hmac.new(_callback_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_kite_callback_token(token: str | None, max_age_seconds: int = 600) -> bool:
    if not token:
        return False
    try:
        ts_text, nonce, supplied_sig = token.split(".", 2)
        ts = int(ts_text)
    except (TypeError, ValueError):
        return False
    if not nonce or len(supplied_sig) != 64:
        return False
    now = int(time.time())
    if ts > now + 30 or now - ts > max_age_seconds:
        return False
    payload = f"{ts}.{nonce}"
    expected = hmac.new(_callback_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied_sig, expected)


def kite_callback_cookie_secure() -> bool:
    redirect_url = (os.getenv("REDIRECT_URL") or "").lower()
    return redirect_url.startswith("https://")


def reset_security_state_for_tests() -> None:
    with _AUTH_LOCK:
        _AUTH_FAILURES.clear()
