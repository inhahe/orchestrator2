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
* A refused socket is accepted and then closed with a ``WS_CLOSE_*`` code and a
  short reason, so the page can say why rather than wait for ever.
* Refusals are logged: each address's first, within a cap across addresses,
  with what was held back counted; never the query string, and never a path
  that could forge a line.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import pathlib
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server                                            # noqa: E402
from server import _ExternalAuthMiddleware               # noqa: E402


async def _inner_app(scope, receive, send):
    """Trivial downstream app: 200 OK, marks that it ran."""
    scope.setdefault("_ran", [])
    scope["_ran"].append(True)
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _drive(mw, *, ip="8.8.8.8", headers=None, query=b"", scheme="http", path="/"):
    """Run one HTTP request through *mw*; return (status, headers-dict, ran)."""
    scope = {
        "type": "http",
        "path": path,
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


def test_failures_are_forgotten_after_a_quiet_spell(mw):
    # A count a scan ran up must not turn the owner's typo, later, into a
    # lockout.
    for _ in range(mw._FREE_ATTEMPTS):
        _drive(mw, headers=_basic("nope"))
    mw._last_failure -= mw._FORGET_AFTER
    status, _, _ = _drive(mw, headers=_basic("nope"))
    assert status == 401
    assert mw._fails == 1, "the old failures were still counted"
    assert mw._lockout_remaining() == 0, "a first failure locked the hub"


def test_failures_short_of_the_quiet_spell_are_kept(mw):
    for _ in range(mw._FREE_ATTEMPTS):
        _drive(mw, headers=_basic("nope"))
    mw._last_failure -= mw._FORGET_AFTER - 60
    _drive(mw, headers=_basic("nope"))
    assert mw._lockout_remaining() > 0, "failures were forgotten too soon"


def test_each_failure_restarts_the_quiet_spell(mw):
    # A guesser who keeps at it is never quiet for long, however slowly.
    for _ in range(mw._FREE_ATTEMPTS):
        _drive(mw, headers=_basic("nope"))
        mw._last_failure -= mw._FORGET_AFTER * 0.9
    _drive(mw, headers=_basic("nope"))
    assert mw._fails == mw._FREE_ATTEMPTS + 1
    assert mw._lockout_remaining() > 0


def test_waiting_out_the_decay_gains_a_guesser_nothing(monkeypatch):
    # Two ways to guess: steadily, one try per lockout once it is at its cap;
    # or in bursts -- the free tries and the escalation -- each followed by
    # the wait that has the count forgotten.  If bursts were the faster, the
    # decay would be a gift to whoever waits for it.
    start = 1e6                                      # a machine up for days
    clock = [start]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    mw = _ExternalAuthMiddleware(_inner_app, password="secret")
    for guesses in range(1, 100):
        clock[0] += mw._lockout_remaining()          # wait out any lockout
        mw._record_failure()
        if mw._lockout_remaining() >= mw._MAX_LOCKOUT:
            break                                    # at the cap: steady from here
    else:
        pytest.fail("the lockout never reached its cap")
    burst = clock[0] - start
    # The wait does have the count forgotten, so bursts can repeat...
    clock[0] += mw._FORGET_AFTER
    mw._record_failure()
    assert mw._fails == 1 and mw._lockout_remaining() == 0
    # ...and repeating them is no faster than guessing steadily.
    per_burst = guesses / (burst + mw._FORGET_AFTER)
    steady = 1 / mw._MAX_LOCKOUT
    assert per_burst <= steady, (
        f"{guesses} guesses every {burst + mw._FORGET_AFTER:.0f}s beats one "
        f"every {mw._MAX_LOCKOUT:.0f}s")


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


# --- A refused socket is told why --------------------------------------------
#
# Closing a socket before accepting it reaches the browser as a failed
# handshake, code 1006 and no reason, so the page could not tell a refusal from
# a network fault and showed "loading sessions" for ever.  Accepted, then
# closed with a code of the hub's own, it can say what would help.

def _drive_ws(mw, *, ip="8.8.8.8", headers=None, query=b""):
    """Run one WebSocket upgrade through *mw*; return (sent messages, ran)."""
    scope = {"type": "websocket", "client": (ip, 12345), "scheme": "ws",
             "path": "/ws", "headers": headers or [], "query_string": query}
    sent = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    import asyncio
    asyncio.run(mw(scope, receive, send))
    return sent, scope.get("_ran", [])


def _closed_with(sent):
    """The (code, reason) *sent* closed with, after accepting."""
    assert [m["type"] for m in sent] == ["websocket.accept", "websocket.close"], sent
    return sent[1]["code"], sent[1]["reason"]


def test_socket_without_credentials_is_told_to_sign_in(mw):
    sent, ran = _drive_ws(mw)
    assert not ran
    assert _closed_with(sent) == (server.WS_CLOSE_SIGN_IN, "Authentication required.")


def test_socket_with_a_wrong_password_is_told_to_sign_in(mw):
    sent, ran = _drive_ws(mw, query=b"password=nope")
    assert not ran
    assert _closed_with(sent)[0] == server.WS_CLOSE_SIGN_IN


def test_socket_during_a_lockout_is_told_so(mw):
    for _ in range(mw._FREE_ATTEMPTS + 1):
        _drive(mw, headers=_basic("nope"))
    sent, ran = _drive_ws(mw)
    code, reason = _closed_with(sent)
    assert not ran
    assert code == server.WS_CLOSE_LOCKED_OUT
    assert reason.startswith("Too many failed password attempts. Try again in ")


def test_socket_when_external_access_is_off_gets_a_short_reason():
    long = "External access is disabled on this server.\n\n" + "how to " * 100
    mw = _ExternalAuthMiddleware(_inner_app, password=None, disabled_message=long)
    code, reason = _closed_with(_drive_ws(mw)[0])
    assert code == server.WS_CLOSE_DISABLED
    # The first paragraph only: a close frame's reason holds 123 bytes, and a
    # longer one makes the close itself fail.
    assert reason == "External access is disabled on this server."


def test_close_reason_is_cut_to_what_a_close_frame_holds():
    reason = server._ws_close_reason("é" * 100)       # 200 bytes of UTF-8
    assert len(reason.encode("utf-8")) <= 123
    assert reason == "é" * 61                          # no half character


def test_the_page_knows_every_refusal_code():
    # app.js cannot import them: a code renumbered on one side only would
    # have the page retry a refusal twenty times again.
    src = (pathlib.Path(server.__file__).parent / "static" / "app.js").read_text(
        encoding="utf-8")
    block = re.search(r"const HUB_REFUSALS = \{(.*?)\n  \};", src, re.S).group(1)
    entries = dict(re.findall(r"(\d+): \{ label: '([^']*)'", block))
    assert {int(code) for code in entries} == {
        server.WS_CLOSE_SIGN_IN, server.WS_CLOSE_DISABLED, server.WS_CLOSE_LOCKED_OUT}
    # And each code means what the hub means by it.
    assert entries[str(server.WS_CLOSE_SIGN_IN)] == "sign-in needed"
    assert entries[str(server.WS_CLOSE_DISABLED)] == "outside access off"
    assert entries[str(server.WS_CLOSE_LOCKED_OUT)] == "locked out"


def _ws_untouched(sent):
    """Nothing socket-level was sent: the middleware let the socket through."""
    return not any(m["type"].startswith("websocket.") for m in sent)


def test_lan_socket_is_not_refused(mw):
    sent, ran = _drive_ws(mw, ip="192.168.1.5")
    assert ran and _ws_untouched(sent)


def test_cookie_authenticated_socket_is_let_in(mw):
    token = hashlib.sha256(b"orch2-auth:secret").hexdigest()
    sent, ran = _drive_ws(mw, headers=[(b"cookie", ("orch2_auth=" + token).encode())])
    assert ran and _ws_untouched(sent)


# --- Refusals are logged, without a flood flooding the log ---------------------

@pytest.fixture
def refusals(caplog):
    """The lines the middleware logs about refusals."""
    caplog.set_level(logging.INFO, logger="orchestrator2")
    return lambda: [r.getMessage() for r in caplog.records
                    if r.getMessage().startswith("refused ")]


def test_a_refused_socket_is_logged_with_its_address_and_why(mw, refusals):
    _drive_ws(mw, ip="203.0.113.7")
    assert refusals() == ["refused an outside socket from 203.0.113.7: no password "
                          "and no valid sign-in cookie"]


def test_the_password_challenge_itself_is_not_logged(mw, refusals):
    # A page request with no credential is the browser being asked for the
    # password: every first visit makes several.
    _drive(mw)
    assert refusals() == []


def test_a_wrong_password_is_logged_without_the_query_string(mw, refusals):
    _drive(mw, query=b"password=hunter2")
    lines = refusals()
    assert lines == ["refused an outside request for / from 8.8.8.8: wrong password"]
    assert "hunter2" not in " ".join(lines)


def test_lan_is_never_logged(mw, refusals):
    _drive_ws(mw, ip="192.168.1.5")
    assert refusals() == []


def test_a_path_cannot_forge_lines_in_the_log(mw, refusals):
    # ASGI hands the path over percent-decoded: written raw, %0A would end
    # the line and start a forged one.
    forged = "/x\n2026-10-27 12:00:00,000 [orchestrator2] INFO: all is well\x1b[2J"
    _drive(mw, path=forged, query=b"password=nope")
    (line,) = refusals()
    assert "\n" not in line and "\x1b" not in line, repr(line)
    assert line.startswith("refused an outside request for /x%0A2026"), line


def test_a_long_path_is_cut_short(mw, refusals):
    _drive(mw, path="/" + "a" * 5000, query=b"password=nope")
    (line,) = refusals()
    assert len(line) < 300, len(line)


def test_each_address_gets_its_first_refusal_written(mw, refusals):
    # A bot's refusal a moment earlier must not hide the phone's.
    _drive(mw, ip="198.51.100.9", query=b"password=guess")
    _drive_ws(mw, ip="203.0.113.7")
    assert [line.split(" from ")[1].split(":")[0] for line in refusals()] == [
        "198.51.100.9", "203.0.113.7"]


def test_an_address_repeating_itself_is_written_once(mw, refusals):
    for _ in range(5):
        _drive_ws(mw, ip="203.0.113.7")
    assert len(refusals()) == 1


def test_an_address_is_written_again_after_a_while(mw, refusals):
    _drive_ws(mw, ip="203.0.113.7")
    mw._refusals._last_from["203.0.113.7"] -= mw._refusals.REPEAT_AFTER
    _drive_ws(mw, ip="203.0.113.7")
    assert len(refusals()) == 2


def test_lines_are_capped_across_addresses(mw, refusals):
    cap = mw._refusals.PER_MINUTE
    for i in range(cap + 5):
        _drive_ws(mw, ip=f"203.0.113.{i}")
    assert len(refusals()) == cap, "a scan from many addresses flooded the log"
    # A minute on, there is room again.
    mw._refusals._written = type(mw._refusals._written)(
        t - 60.0 for t in mw._refusals._written)
    _drive_ws(mw, ip="198.51.100.1")
    assert len(refusals()) == cap + 1


def test_what_was_held_back_is_counted(mw, caplog, refusals):
    for _ in range(3):
        _drive_ws(mw, ip="203.0.113.7")         # one line, two held
    _drive_ws(mw, ip="203.0.113.8")             # a line of its own
    _drive_ws(mw, ip="203.0.113.8")             # held
    mw._refusals.flush()
    assert refusals()[-1] == ("refused 3 more outside connection(s) from 2 "
                              "address(es) without a line each: repeats, or "
                              "past 10 lines a minute")
    # Counted once: a second flush with nothing new held writes nothing.
    n = len(refusals())
    mw._refusals.flush()
    assert len(refusals()) == n


def test_the_count_is_written_by_itself_later(refusals):
    # In the running hub nobody calls flush(): holding schedules it, once.
    import asyncio

    async def run():
        log = server._RefusalLog()
        log.FLUSH_AFTER = 0.05
        for _ in range(4):                      # one line, three held
            log.note({"type": "websocket"}, "203.0.113.7", "why")
        await asyncio.sleep(0.2)

    asyncio.run(run())
    lines = refusals()
    assert lines[1:] == ["refused 3 more outside connection(s) from 1 address(es) "
                         "without a line each: repeats, or past 10 lines a minute"], lines
