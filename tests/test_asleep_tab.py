"""A background tab Chrome kills is asleep, not gone: its session is kept.

Asked 2026-09-27: "what i would like is for orchestrator2 not to automatically
kill a process after n seconds when a tab disconnects due to chrome forcing
it".

The hub's idle teardown stops a session's CLI once its last viewer has been
gone for ``--session-idle-timeout`` (300 s).  A phone was already exempt --
``--mobile-idle-timeout``, never by default -- because a sleeping phone is not
a viewer who left.  A background tab Chrome discards (Memory Saver) or whose
socket it drops is the same case, and it was reaped.

What identifies it: Chrome only discards or freezes *background* tabs, and a
tab closed by hand sends a Close frame (1001) even from the background, while
a killed page sends nothing (the disconnect reports 1006 -- measured against
this uvicorn).  So the page tells the hub when it is hidden, and a socket that
ended without a close while hidden counts as asleep.  A tab killed while you
were looking at it still counts as leaving: "any abnormal close" was rejected
as too broad in design.md.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server                                            # noqa: E402
from config import parse_args                            # noqa: E402
from session_runtime import SessionRuntime               # noqa: E402
from state import init_state_from_config                 # noqa: E402


def _rt(rid="s-bg"):
    cfg = parse_args([])
    return SessionRuntime(config=cfg, state=init_state_from_config(cfg), rid=rid)


class _Ws:
    headers: dict = {}
    query_params: dict = {}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(server, "_hidden_ws", set())
    monkeypatch.setattr(server, "_asleep_ws", set())
    monkeypatch.setattr(server, "_mobile_ws", set())
    monkeypatch.setattr(server, "_says_bye_ws", set())
    monkeypatch.setattr(server, "_leaving_ws", set())
    monkeypatch.setattr(server, "config", parse_args([]))     # 300 s / never
    monkeypatch.setattr(server, "_default_runtime", None)


# --------------------------------------------------------------------------
# The page says when it is in the background
# --------------------------------------------------------------------------

def test_the_hub_hears_a_tab_go_to_the_background_and_come_back():
    ws = _Ws()

    async def say(hidden):
        return await server._handle_lobby_message(
            ws, {"type": "visibility", "hidden": hidden})

    assert asyncio.run(say(True)) is True, "not consumed: it would reach the chat"
    assert ws in server._hidden_ws
    asyncio.run(say(False))
    assert ws not in server._hidden_ws


# --------------------------------------------------------------------------
# How the socket ended
# --------------------------------------------------------------------------

@pytest.mark.parametrize("code, hidden, asleep", [
    (1006, True, True),     # killed in the background: Chrome
    (None, True, True),     # the send side found it gone first
    (1001, True, False),    # closed by hand from the background
    (1000, True, False),    # the page's own ws.close()
    (1006, False, False),   # killed while you were looking: a crash
    (None, False, False),
])
def test_only_a_background_tab_gone_without_a_close_is_asleep(code, hidden, asleep):
    ws = _Ws()
    if hidden:
        server._hidden_ws.add(ws)

    server._note_how_it_ended(ws, code)

    assert (ws in server._asleep_ws) is asleep


# --------------------------------------------------------------------------
# The idle timer
# --------------------------------------------------------------------------

def _depart(rt, ws):
    """Run the idle-timer decision for *ws* leaving *rt*; report whether a
    teardown countdown was armed."""
    async def go():
        server._maybe_start_idle_timer(rt, departing=ws)
        armed = rt.idle_timer is not None and not rt.idle_timer.done()
        if rt.idle_timer is not None:
            rt.idle_timer.cancel()
        return armed

    return asyncio.run(go())


def test_a_tab_the_browser_put_to_sleep_does_not_start_the_countdown(monkeypatch):
    rt = _rt()
    monkeypatch.setattr(server, "runtimes", {rt.rid: rt})
    ws = _Ws()
    server._asleep_ws.add(ws)

    assert _depart(rt, ws) is False


def test_a_tab_that_left_still_does(monkeypatch):
    rt = _rt()
    monkeypatch.setattr(server, "runtimes", {rt.rid: rt})

    assert _depart(rt, _Ws()) is True


def test_an_asleep_tab_gets_the_sleeping_grace_when_one_is_set(monkeypatch):
    """--mobile-idle-timeout is the grace for any viewer its browser put to
    sleep, not only a phone."""
    rt = _rt()
    monkeypatch.setattr(server, "runtimes", {rt.rid: rt})
    monkeypatch.setattr(server, "config", parse_args(["--mobile-idle-timeout", "7200"]))
    ws = _Ws()
    server._asleep_ws.add(ws)

    async def go():
        server._maybe_start_idle_timer(rt, departing=ws)
        deadline, mobile = rt.idle_deadline, rt.idle_mobile
        rt.idle_timer.cancel()
        return deadline, mobile

    deadline, mobile = asyncio.run(go())
    assert mobile is True
    assert deadline is not None and deadline - __import__("time").time() > 7000


# --------------------------------------------------------------------------
# Through the real endpoint
# --------------------------------------------------------------------------

def _connect(monkeypatch, rt, frames, code, query=None):
    """One tab attached to *rt*: it sends *frames*, then its socket ends with
    *code*.  *query* is added to its connection URL's parameters.  Returns
    whether *rt*'s idle countdown was armed."""
    server.build_app()                      # populates WebSocketDisconnect
    monkeypatch.setattr(server, "runtimes", {rt.rid: rt})
    monkeypatch.setattr(server, "_ws_runtime", {})

    async def light_attach(ws, r):
        r.add_client(ws)
        server._ws_runtime[ws] = r

    async def nothing(*a, **k):
        pass

    monkeypatch.setattr(server, "_attach_ws", light_attach)
    monkeypatch.setattr(server, "_push_session_list", nothing)
    pending = [json.dumps(f) for f in frames]

    class FakeWS:
        headers: dict = {}
        query_params = {"rid": rt.rid, **(query or {})}

        async def accept(self):
            pass

        async def send_text(self, _t):
            pass

        async def send_json(self, _o):
            pass

        async def close(self, *a, **k):
            pass

        async def receive_text(self):
            if pending:
                return pending.pop(0)
            if code is None:
                # The hub's own send found the socket gone first; the receive
                # then fails like this (the "websocket error" in the log).
                raise RuntimeError('WebSocket is not connected. Need to call '
                                   '"accept" first.')
            raise server.WebSocketDisconnect(code)

    async def go():
        await server.websocket_endpoint(FakeWS())
        armed = rt.idle_timer is not None and not rt.idle_timer.done()
        if rt.idle_timer is not None:
            rt.idle_timer.cancel()
        return armed

    return asyncio.run(go())


HIDDEN = {"type": "visibility", "hidden": True}
SHOWN = {"type": "visibility", "hidden": False}


def test_a_background_tab_chrome_kills_keeps_its_session(monkeypatch):
    """The request."""
    assert _connect(monkeypatch, _rt(), [HIDDEN], 1006) is False


def test_so_does_one_the_hub_found_gone_first(monkeypatch):
    """No close frame this way either: the send side noticed before the
    receive did (the 13:09:16 'websocket error' in the reported log)."""
    assert _connect(monkeypatch, _rt(), [HIDDEN], None) is False


def test_a_tab_closed_by_hand_is_idled_out_as_before(monkeypatch):
    assert _connect(monkeypatch, _rt(), [HIDDEN], 1001) is True


def test_a_tab_killed_while_you_were_looking_is_too(monkeypatch):
    assert _connect(monkeypatch, _rt(), [HIDDEN, SHOWN], 1006) is True


def test_nothing_is_left_behind_for_a_socket_that_is_gone(monkeypatch):
    _connect(monkeypatch, _rt(), [HIDDEN], 1006)

    assert not server._hidden_ws and not server._asleep_ws


def test_the_hub_says_it_hears_visibility(monkeypatch):
    """The page sends it only to a hub that says so: an older one answers an
    unknown type with a line in the chat."""
    assert "visibility" in server.HUB_HEARS


# --------------------------------------------------------------------------
# A page that says when it leaves
# --------------------------------------------------------------------------
#
# Reported 2026-09-28: "i never closed my 'Good Photons' tab, but it now says
# 'This tab's session (Good Photons) was closed after 5 minutes with no tab
# connected'".  Chrome had the tab frozen, woke it at 18:02, let it reconnect
# and resume, and its socket went again a minute later; nothing marked it
# asleep.  The page is frozen again moments after reconnecting, so it may never
# get to say it is hidden, and real Chrome ends a sleeping tab's socket with
# 1005 (every one the hub had spared) as well as 1006.  Now the page says
# "leaving" when it really goes, having declared at connect (?bye=1) that it
# will; any other ending is a tab asleep.

BYE = {"bye": "1"}
LEAVING = {"type": "leaving"}


@pytest.mark.parametrize("code", [1000, 1001, 1005, 1006, None])
def test_a_page_that_says_bye_is_asleep_however_it_ends(monkeypatch, code):
    """The report: it never got to say it was hidden, and the close code was
    no guide."""
    assert _connect(monkeypatch, _rt(), [], code, query=BYE) is False


@pytest.mark.parametrize("code", [1000, 1001, 1005, 1006, None])
def test_unless_it_said_it_was_leaving(monkeypatch, code):
    """Closed, reloaded or navigated away: its viewer left."""
    assert _connect(monkeypatch, _rt(), [LEAVING], code, query=BYE) is True


def test_what_it_said_about_being_hidden_is_beside_the_point(monkeypatch):
    assert _connect(monkeypatch, _rt(), [SHOWN], 1006, query=BYE) is False
    assert _connect(monkeypatch, _rt(), [HIDDEN, LEAVING], 1006, query=BYE) is True


def test_a_page_from_before_keeps_the_old_rule(monkeypatch):
    """No ?bye=1: it says nothing when it goes, so it is judged as before."""
    assert _connect(monkeypatch, _rt(), [HIDDEN], 1001) is True
    assert _connect(monkeypatch, _rt(), [HIDDEN], 1005) is False
    assert _connect(monkeypatch, _rt(), [SHOWN], 1006) is True


def test_only_bye_1_declares_it(monkeypatch):
    assert _connect(monkeypatch, _rt(), [], 1001, query={"bye": "0"}) is True


def test_the_hub_hears_a_page_say_it_is_leaving():
    ws = _Ws()

    assert asyncio.run(server._handle_lobby_message(ws, LEAVING)) is True, \
        "not consumed: it would reach the chat"
    assert ws in server._leaving_ws


def test_the_hub_says_it_hears_leaving():
    assert "leaving" in server.HUB_HEARS


def test_nothing_is_left_behind_for_a_page_that_said_bye(monkeypatch):
    _connect(monkeypatch, _rt(), [LEAVING], 1001, query=BYE)

    assert not server._says_bye_ws and not server._leaving_ws


@pytest.mark.parametrize("frames, code, says", [
    ([], 1005, "went without saying it was leaving (close code 1005)"),
    ([LEAVING], 1001, "left (it said so; close code 1001)"),
])
def test_every_ending_is_logged_with_its_close_code(monkeypatch, caplog,
                                                     frames, code, says):
    """Nothing said why the Good Photons session went, which is why it took
    a transcript and a timeline to find out."""
    with caplog.at_level("INFO", logger="orchestrator2"):
        _connect(monkeypatch, _rt(), frames, code, query=BYE)

    assert says in caplog.text


def test_so_is_a_page_from_before(monkeypatch, caplog):
    with caplog.at_level("INFO", logger="orchestrator2"):
        _connect(monkeypatch, _rt(), [SHOWN], 1001)

    assert "left (close code 1001; last said it was visible" in caplog.text
