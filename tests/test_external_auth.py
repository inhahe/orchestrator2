"""External-password auth + brute-force throttle tests.

``_ExternalAuthMiddleware`` gates non-LAN clients behind a password.  These
tests drive the ASGI middleware directly (no real HTTP stack) to verify:

* LAN/loopback clients bypass auth entirely.
* A public IP with the right password (Basic header / ``?password=`` / cookie)
  passes through and gets an ``orch2_auth`` cookie planted.
* A public IP with the wrong password is refused, and after ``_FREE_ATTEMPTS``
  failures the hub is locked out with a ``429`` (before the password is even
  examined), with the lockout window growing exponentially.
* The lockout is *global* (shared across source IPs, not per-IP) so a botnet
  can't get a fresh budget per address.
* A successful auth clears the failure counter.
* Password/token comparisons use ``hmac.compare_digest`` (constant time).
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
from server import _ExternalAuthMiddleware


async def _inner_app(scope, receive, send):
    """Trivial downstream app: 200 OK, marks that it ran."""
    scope.setdefault("_ran", [])
    scope["_ran"].append(True)
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _drive(mw, *, ip="8.8.8.8", headers=None, query=b"", scheme="http"):
    """Run one HTTP request through *mw*; return (status, headers-dict, ran)."""
    scope = {
        "type": "http",
        "client": (ip, 12345),
        "headers": headers or [],
        "query_string": query,
        "scheme": scheme,
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    import asyncio
    asyncio.run(mw(scope, receive, send))

    start = next((m for m in sent if m["type"] == "http.response.start"), None)
    status = start["status"] if start else None
    hdrs = {}
    if start:
        for k, v in start.get("headers", []):
            hdrs.setdefault(bytes(k), bytes(v))
    return status, hdrs, scope.get("_ran", [])


def _basic(password: str) -> list:
    tok = base64.b64encode(("u:" + password).encode()).decode()
    return [(b"authorization", ("Basic " + tok).encode())]


@pytest.fixture
def mw():
    return _ExternalAuthMiddleware(_inner_app, password="secret")


def test_lan_bypasses_auth(mw):
    status, _, ran = _drive(mw, ip="192.168.1.5")
    assert status == 200 and ran


def test_loopback_bypasses_auth(mw):
    status, _, ran = _drive(mw, ip="127.0.0.1")
    assert status == 200 and ran


def test_public_no_creds_rejected(mw):
    status, hdrs, ran = _drive(mw)
    assert status == 401 and not ran
    assert b"www-authenticate" in hdrs


def test_public_correct_basic_password(mw):
    status, hdrs, ran = _drive(mw, headers=_basic("secret"))
    assert status == 200 and ran
    assert b"set-cookie" in hdrs  # cookie planted for the WS upgrade


def test_public_correct_query_password(mw):
    status, _, ran = _drive(mw, query=b"password=secret")
    assert status == 200 and ran


def test_public_wrong_password_rejected(mw):
    status, _, ran = _drive(mw, headers=_basic("nope"))
    assert status == 401 and not ran


def test_password_none_blocks_external():
    mw = _ExternalAuthMiddleware(_inner_app, password=None)
    status, hdrs, ran = _drive(mw)
    assert status == 401 and not ran
    # No challenge — external access is off, not "please enter a password".
    assert b"www-authenticate" not in hdrs


def test_lockout_after_free_attempts(mw):
    # Burn through the free attempts — each is a plain 401.
    for _ in range(mw._FREE_ATTEMPTS):
        status, _, _ = _drive(mw, headers=_basic("nope"))
        assert status == 401
    # The next failure crosses the threshold and arms the lockout.
    status, _, _ = _drive(mw, headers=_basic("nope"))
    assert status == 401
    # Now the IP is locked: even a *correct* password is refused with 429.
    status, hdrs, ran = _drive(mw, headers=_basic("secret"))
    assert status == 429 and not ran
    assert b"retry-after" in hdrs


def test_lockout_is_global_across_ips(mw):
    # Spread the failures across many distinct IPs (simulating a botnet) — the
    # lockout is global, so this still trips it (per-IP would give each a fresh
    # budget and never lock).
    for i in range(mw._FREE_ATTEMPTS + 1):
        _drive(mw, ip=f"8.8.8.{i}", headers=_basic("nope"))
    # A brand-new IP with the *correct* password is still refused — the whole
    # hub is locked, not just the offending addresses.
    status, hdrs, ran = _drive(mw, ip="9.9.9.9", headers=_basic("secret"))
    assert status == 429 and not ran
    assert b"retry-after" in hdrs


def test_success_clears_failures(mw):
    for _ in range(mw._FREE_ATTEMPTS - 1):
        _drive(mw, headers=_basic("nope"))
    # A success wipes the counter…
    status, _, ran = _drive(mw, headers=_basic("secret"))
    assert status == 200 and ran
    # …so we get the full budget of free attempts again (no lockout yet).
    for _ in range(mw._FREE_ATTEMPTS):
        status, _, _ = _drive(mw, headers=_basic("nope"))
        assert status == 401


def test_lockout_window_grows(mw):
    for _ in range(mw._FREE_ATTEMPTS):
        _drive(mw, headers=_basic("nope"))
    # First lockout-arming failure → base window (~2s).
    _drive(mw, headers=_basic("nope"))
    first_remaining = mw._lockout_remaining()
    assert 0 < first_remaining <= mw._BASE_LOCKOUT
    # Manually expire the lock, then fail again → a longer window.
    mw._locked_until = 0.0
    _drive(mw, headers=_basic("nope"))
    second_remaining = mw._lockout_remaining()
    # Second failure over the threshold → 2*2^1 = 4s window (vs 2s for the first).
    assert second_remaining > mw._BASE_LOCKOUT


def test_valid_cookie_authenticates(mw):
    token = hashlib.sha256(b"orch2-auth:secret").hexdigest()
    hdrs = [(b"cookie", ("orch2_auth=" + token).encode())]
    status, resp_hdrs, ran = _drive(mw, headers=hdrs)
    assert status == 200 and ran
    # Already authed by cookie → no need to re-plant one.
    assert b"set-cookie" not in resp_hdrs
