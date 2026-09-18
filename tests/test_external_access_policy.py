"""External access is off until the operator says otherwise, with a password.

Two defects, reported together on 2026-09-03.

**1. A shipped password is not a password.**  ``config.DEFAULT_EXTERNAL_PASSWORD``
was ``"uncommon11"``, applied automatically whenever neither the flag nor the
env var was set.  Every install was therefore reachable from the internet with
a secret published in the source.  It is gone, and there is no replacement
default: external access is now **off** unless switched on *and* given a
password, and "on with no password" is refused with a warning rather than
being left open or silently ignored.

**2. The brute-force throttle counted non-attempts.**  Any credential-less
request incremented the global failure counter, so an ordinary first visit --
one document, a dozen static assets, and a stream of WebSocket upgrades from
``app.js``'s 20-try reconnect loop -- burned through ``_FREE_ATTEMPTS`` on its
own.  The user was told "Too many failed attempts. Try again in 9s." *while
typing the correct password for the first time*, and a reload appeared to fix
it only because by then the auth cookie existed.  Only a credential that was
presented **and wrong** is an attempt.
"""

from __future__ import annotations

import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server                                        # noqa: E402
from config import parse_args                        # noqa: E402
from server import _ExternalAuthMiddleware           # noqa: E402
from tests.test_external_auth import _drive, _inner_app  # noqa: E402


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch):
    """The host running the tests may itself have these set."""
    monkeypatch.delenv("ORCH2_EXTERNAL_PASSWORD", raising=False)
    monkeypatch.delenv("ORCH2_EXTERNAL_ACCESS", raising=False)
    yield


def _policy(argv=()):
    return server._resolve_external_auth(parse_args(list(argv)))


# ---------------------------------------------------------------------------
# The shipped default
# ---------------------------------------------------------------------------

def test_there_is_no_built_in_password_any_more():
    """The constant itself must be gone, not merely unused -- an unused default
    is one import away from coming back."""
    import config
    assert not hasattr(config, "DEFAULT_EXTERNAL_PASSWORD")


def test_external_access_is_off_out_of_the_box():
    p = _policy()
    assert p.enabled is False
    assert p.password is None


def test_the_default_is_not_a_warning():
    """Off is the intended state; nagging about it would train users to ignore
    the message that does matter."""
    assert _policy().warn is None


def test_a_default_server_refuses_a_public_client():
    mw = _ExternalAuthMiddleware(_inner_app, password=_policy().password)
    status, hdrs, ran = _drive(mw, ip="8.8.8.8")
    assert not ran
    assert status == 401
    assert b"www-authenticate" not in hdrs, (
        "prompted for a password that cannot possibly work")


def test_lan_still_works_with_everything_off():
    """Turning the internet off must not lock the owner out of their own LAN."""
    mw = _ExternalAuthMiddleware(_inner_app, password=_policy().password)
    for ip in ("127.0.0.1", "192.168.1.5", "10.0.0.7"):
        status, _, ran = _drive(mw, ip=ip)
        assert ran, ip
        assert status == 200, ip


# ---------------------------------------------------------------------------
# On without a password: warn, and stay shut
# ---------------------------------------------------------------------------

def test_on_without_a_password_is_refused():
    p = _policy(["--external-access", "on"])
    assert p.enabled is False, "opened up with no password"
    assert p.password is None


def test_on_without_a_password_warns():
    """Failing silently here would be baffling: the operator asked for external
    access and would have no idea why it does not work."""
    p = _policy(["--external-access", "on"])
    assert p.warn
    assert "no password" in p.warn.lower()


def test_the_warning_says_how_to_fix_it():
    p = _policy(["--external-access", "on"])
    assert "--external-password" in p.warn
    assert "ORCH2_EXTERNAL_PASSWORD" in p.warn


@pytest.mark.parametrize("pw", ["", "   ", "\t\n"])
def test_a_blank_password_does_not_count_as_one(pw):
    """An empty env var or a fat-fingered flag is a typo, never a secret --
    and this is exactly the "blank password lets the internet in" case."""
    p = _policy(["--external-access", "on", "--external-password", pw])
    assert p.enabled is False
    assert p.password is None
    assert p.warn


def test_a_password_alone_does_not_switch_it_on():
    """A password left in the environment from some other purpose must not be
    enough; enabling the internet is a separate, deliberate act."""
    p = _policy(["--external-password", "s3cret"])
    assert p.enabled is False
    assert p.password is None


def test_on_with_a_password_is_allowed():
    """The feature still has to work, or all of the above is just breakage."""
    p = _policy(["--external-access", "on", "--external-password", "s3cret"])
    assert p.enabled is True
    assert p.password == "s3cret"
    assert p.warn is None


def test_the_env_vars_work_too(monkeypatch):
    """Documented as equivalent, so a launcher can set them without flags."""
    monkeypatch.setenv("ORCH2_EXTERNAL_ACCESS", "on")
    monkeypatch.setenv("ORCH2_EXTERNAL_PASSWORD", "from-env")
    p = _policy()
    assert p.enabled is True and p.password == "from-env"


def test_explicit_off_beats_a_password(monkeypatch):
    monkeypatch.setenv("ORCH2_EXTERNAL_PASSWORD", "from-env")
    p = _policy(["--external-access", "off"])
    assert p.enabled is False and p.password is None


# ---------------------------------------------------------------------------
# Discoverability
# ---------------------------------------------------------------------------

def test_a_refused_client_is_told_how_to_enable_it():
    """Someone hitting this from outside sees a wall of "no"; the way to say
    yes has to be in the response, not only in --help."""
    mw = _ExternalAuthMiddleware(_inner_app, password=None)
    assert "ORCH2_EXTERNAL_ACCESS" in mw.disabled_message
    assert "--external-access" in mw.disabled_message


def test_the_instructions_are_written_once():
    """Three places explain this; if they drift, two of them become wrong."""
    assert "--external-access on" in server.EXTERNAL_HOWTO
    assert "ORCH2_EXTERNAL_PASSWORD" in server.EXTERNAL_HOWTO


# ---------------------------------------------------------------------------
# The throttle: only real guesses count
# ---------------------------------------------------------------------------

def _mw():
    return _ExternalAuthMiddleware(_inner_app, password="secret")


def _basic(pw: str) -> list:
    return [(b"authorization",
             b"Basic " + base64.b64encode(("u:" + pw).encode()))]


def test_a_credential_less_request_is_not_an_attempt():
    """The bug, in one assertion.  A browser that has not been asked for a
    password yet is not guessing at one."""
    mw = _mw()
    for _ in range(20):
        _drive(mw, ip="8.8.8.8")
    assert mw._fails == 0
    assert mw._lockout_remaining() == 0


def test_a_first_visit_does_not_lock_the_user_out():
    """A realistic cold page load: the document, its assets, and a run of
    WebSocket retries -- all before any password has been typed.  Then the
    right password is entered.  It must simply work."""
    mw = _mw()
    _drive(mw, ip="8.8.8.8")                      # GET /
    for _ in range(12):                           # static assets
        _drive(mw, ip="8.8.8.8")
    for _ in range(20):                           # app.js reconnect loop
        _drive(mw, ip="8.8.8.8")
    status, _, ran = _drive(mw, ip="8.8.8.8", headers=_basic("secret"))
    assert status == 200 and ran, (
        "correct password refused after an ordinary first page load")


def test_a_wrong_password_still_counts():
    """The protection must survive the fix."""
    mw = _mw()
    for _ in range(3):
        _drive(mw, ip="8.8.8.8", headers=_basic("nope"))
    assert mw._fails == 3


def test_a_wrong_query_password_counts_too():
    mw = _mw()
    _drive(mw, ip="8.8.8.8", query=b"password=nope")
    assert mw._fails == 1


def test_enough_wrong_guesses_still_lock_out():
    mw = _mw()
    for _ in range(mw._FREE_ATTEMPTS + 3):
        _drive(mw, ip="8.8.8.8", headers=_basic("nope"))
    assert mw._lockout_remaining() > 0
    status, _, ran = _drive(mw, ip="8.8.8.8", headers=_basic("nope"))
    assert status == 429 and not ran


def test_the_lockout_message_names_the_cause():
    """"Too many failed attempts" was indistinguishable from "your password is
    wrong", which is why the reporter could not tell which had happened."""
    mw = _mw()
    for _ in range(mw._FREE_ATTEMPTS + 3):
        _drive(mw, ip="8.8.8.8", headers=_basic("nope"))
    scope = {"type": "http", "client": ("8.8.8.8", 1), "headers": [],
             "query_string": b"", "scheme": "http"}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(m):
        sent.append(m)

    import asyncio
    asyncio.run(mw(scope, receive, send))
    body = b"".join(m.get("body", b"") for m in sent
                    if m["type"] == "http.response.body").decode()
    assert "password" in body.lower()


def test_an_authenticated_cookie_survives_someone_elses_lockout():
    """The counter is global by design (per-IP would be defeated by a botnet),
    which used to mean a stranger guessing from anywhere could lock the owner
    out of their own remote session for up to five minutes.  A cookie is proof
    of a past success, not a guess, so it is honoured during the window."""
    mw = _mw()
    cookie = [(b"cookie", ("orch2_auth=" + mw.token).encode())]
    _drive(mw, ip="8.8.8.8", headers=cookie)      # establish it works
    for _ in range(mw._FREE_ATTEMPTS + 5):        # attacker, elsewhere
        _drive(mw, ip="1.2.3.4", headers=_basic("nope"))
    assert mw._lockout_remaining() > 0, "test did not actually arm a lockout"
    status, _, ran = _drive(mw, ip="8.8.8.8", headers=cookie)
    assert status == 200 and ran, "the owner was locked out by a stranger"


def test_a_wrong_cookie_is_not_a_free_pass():
    mw = _mw()
    status, _, ran = _drive(
        mw, ip="8.8.8.8", headers=[(b"cookie", b"orch2_auth=forged")])
    assert not ran and status == 401
