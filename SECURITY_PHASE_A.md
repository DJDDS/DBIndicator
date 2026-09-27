# DBIndicator Phase A — Collaborative Development Security

Status: development branch only until owner approves production deployment.

## Objective

Secure the current collaborative development period without blocking:
- owner development through GitHub and Railway,
- a team member using the scanner,
- third-party auditors reviewing research evidence.

No scanner, scoring, research, Trial-25, V12/V12.3, scheduling, or trading logic is changed by Phase A.

## Roles

| Capability | OWNER | MEMBER | AUDITOR |
| --- | --- | --- | --- |
| Dashboard / scanner | Yes | Read-only | No |
| Charts / OI / patterns | Yes | Read-only | No |
| Settings | Yes | No | No |
| Start scans/research jobs | Yes | No | No |
| Kite login initiation | Yes | No | No |
| Research status / approved exports | Yes | No | Read-only |
| Curated audit files | Yes | No | Read-only |
| Railway variables / secrets | Owner infrastructure only | No | No |
| GitHub write / deployment | Owner-controlled workflow only | No | No |

## Authentication

Phase A keeps HTTPS Basic Auth for compatibility but replaces the old
"password only / username ignored" behavior with named role accounts.

Environment variables:
- DBI_OWNER_USERNAME / DBI_OWNER_PASSWORD
- DBI_MEMBER_USERNAME / DBI_MEMBER_PASSWORD
- DBI_AUDITOR_USERNAME / DBI_AUDITOR_PASSWORD
- DBI_CALLBACK_SECRET
- DBI_AUTH_MAX_FAILURES
- DBI_AUTH_WINDOW_SECONDS
- DBI_AUTH_LOCKOUT_SECONDS
- DBI_AUDIT_ROOT

DBI_OWNER_PASSWORD falls back to the existing DASHBOARD_PASSWORD so the owner
can migrate without immediately rotating the production secret.

## Auditor boundary

Auditors receive no Railway, broker, deployment, or secret access.
The /audit and /api/audit/* endpoints expose only:
- files under repository research/,
- explicitly whitelisted top-level research documents,
- files copied into DBI_AUDIT_ROOT (default /data/audit_exports).

File resolution is traversal-safe and read-only.

## Kite token and callback

The cached daily Kite access token is written with POSIX mode 0600.
Kite login can only be initiated by OWNER. The callback requires a signed
short-lived HttpOnly SameSite=Lax cookie before a request token is exchanged.

## HTTP hardening

Phase A adds:
- constant-time credential comparison,
- per-client + username failed-login throttling,
- owner/member/auditor authorization,
- cross-site write blocking when an Origin header is present,
- X-Content-Type-Options: nosniff,
- X-Frame-Options: DENY,
- Referrer-Policy: strict-origin-when-cross-origin,
- restrictive Permissions-Policy,
- HSTS on HTTPS requests,
- no-store caching on API/settings paths.

A stricter nonce-based CSP, session authentication and MFA/TOTP are deliberately
reserved for Phase B because the current UI contains substantial inline scripts
and the application is still under active development.

## Dependency audit status

Phase A upgrades the direct vulnerable pins identified by pip-audit:
- Flask 3.0.3 -> 3.1.3
- python-dotenv 1.0.1 -> 1.2.2
- requests 2.32.3 -> 2.33.0

KiteConnect currently pins `autobahn[twisted]==19.11.2` exactly upstream. The
security workflow records PYSEC-2020-25 and CVE-2026-77528 as temporary,
explicit exceptions so that every other dependency vulnerability remains a hard
CI failure. Replacing or overriding KiteConnect's WebSocket dependency is deferred
until compatibility can be validated against live ticker/recorder behaviour.

## Regression-gate note

A full-suite run on the deployed code line currently contains nine stale/failing
expectations in untouched stock-in-play, V12 trade-console and market-regime
areas. Their implementation blob hashes are unchanged by Phase A. Security CI
therefore:
- hard-fails every other DBIndicator regression,
- runs those nine exact known node IDs separately as visible non-blocking debt,
- includes the recorder-health test in the hard gate after updating its expected
  behavior from public access to OWNER-authenticated access.

This prevents Phase A from masking any new failure while keeping unrelated,
pre-existing test debt visible for later cleanup.

## Production deployment preconditions

Before Phase A is deployed:
1. Configure a known OWNER username.
2. Configure the team MEMBER username/password.
3. Configure the AUDITOR username/password only when audit access is required.
4. Configure a random DBI_CALLBACK_SECRET.
5. Verify all Phase A CI checks.
6. Owner reviews and explicitly approves the production diff.
