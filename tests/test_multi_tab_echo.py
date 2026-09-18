"""A prompt must reach every tab watching the session, not just the sender.

Reported 2026-09-06: a session open in two tabs, a turn interrupted, then a new
prompt typed. It appeared only in the tab it was typed in -- yet *both* tabs
showed the agent start working and reply. So one tab displayed an answer to a
question it had never shown.

The frontend echoes a prompt optimistically when it is not busy at send time,
and reports that with ``client_echoed``. The server read that as "the prompt is
already on screen" and skipped the broadcast entirely -- but it only meant "the
*sender* drew it". Every other viewer was left without it, while the reply
(broadcast normally, like all turn output) arrived regardless. That asymmetry
is exactly the reported shape.

The fix is to skip one *viewer*, not the whole send.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server                                       # noqa: E402
from session_runtime import SessionRuntime          # noqa: E402


class FakeWS:
    def __init__(self, name):
        self.name = name
        self.sent = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))

    def texts(self, kind="user_message"):
        return [m.get("content") for m in self.sent if m.get("type") == kind]

    def __repr__(self):
        return f"<FakeWS {self.name}>"


class FakeBridge:
    def __init__(self):
        self.event_queue = SimpleNamespace(put_nowait=lambda item: None)

    async def flush_pending_turn_end(self):
        pass


def _runtime(*clients):
    rt = SessionRuntime(
        config=SimpleNamespace(cwd="D:/proj", session_idle_timeout=300),
        state=SimpleNamespace(session_id="sid", session_title=None, busy=False,
                              queued_prompts=[], connect_blocked_msg=None),
    )
    rt.bridge = FakeBridge()
    for c in clients:
        rt.add_client(c)
    return rt


# ---------------------------------------------------------------------------
# The reported bug
# ---------------------------------------------------------------------------

def test_the_other_tab_still_sees_a_locally_echoed_prompt():
    a, b = FakeWS("typed-here"), FakeWS("other-tab")
    rt = _runtime(a, b)
    ok = asyncio.run(server._enqueue_prompt(
        rt, "fix the parser", echoed_by=a))
    assert ok
    assert b.texts() == ["fix the parser"], (
        "the tab that didn't type it never saw the prompt")


def test_the_sender_is_not_shown_it_twice():
    """It already drew the prompt optimistically; echoing again would double it."""
    a, b = FakeWS("typed-here"), FakeWS("other-tab")
    rt = _runtime(a, b)
    asyncio.run(server._enqueue_prompt(
        rt, "fix the parser", echoed_by=a))
    assert a.texts() == []


def test_three_tabs_all_but_the_sender():
    a, b, c = FakeWS("a"), FakeWS("b"), FakeWS("c")
    rt = _runtime(a, b, c)
    asyncio.run(server._enqueue_prompt(rt, "go", echoed_by=a))
    assert a.texts() == []
    assert b.texts() == ["go"]
    assert c.texts() == ["go"]


# ---------------------------------------------------------------------------
# ...without breaking the paths that were already right
# ---------------------------------------------------------------------------

def test_a_prompt_the_client_did_not_echo_reaches_everyone():
    """Busy at send time (or a slash command) -- nobody drew it locally."""
    a, b = FakeWS("a"), FakeWS("b")
    rt = _runtime(a, b)
    asyncio.run(server._enqueue_prompt(rt, "hello", echoed_by=None))
    assert a.texts() == ["hello"]
    assert b.texts() == ["hello"]


def test_an_unknown_origin_still_reaches_everyone():
    """Server-side callers (queue drain, forwarded command output) have no
    originating socket. Broadcasting to all is the safe direction: a duplicate
    line is self-evident, a missing prompt reads as the agent answering
    something nobody asked."""
    a, b = FakeWS("a"), FakeWS("b")
    rt = _runtime(a, b)
    asyncio.run(server._enqueue_prompt(rt, "queued work"))
    assert a.texts() == ["queued work"]
    assert b.texts() == ["queued work"]


def test_a_single_tab_that_echoed_gets_nothing_extra():
    a = FakeWS("only")
    rt = _runtime(a)
    asyncio.run(server._enqueue_prompt(rt, "solo", echoed_by=a))
    assert a.texts() == []


# ---------------------------------------------------------------------------
# The broadcast primitive
# ---------------------------------------------------------------------------

def test_exclude_skips_exactly_one_socket():
    a, b, c = FakeWS("a"), FakeWS("b"), FakeWS("c")
    rt = _runtime(a, b, c)
    asyncio.run(rt.broadcast({"type": "user_message", "content": "x"}, exclude=b))
    assert a.texts() == ["x"] and c.texts() == ["x"]
    assert b.texts() == []


def test_exclude_none_reaches_everyone():
    a, b = FakeWS("a"), FakeWS("b")
    rt = _runtime(a, b)
    asyncio.run(rt.broadcast({"type": "user_message", "content": "x"}))
    assert a.texts() == ["x"] and b.texts() == ["x"]


def test_only_the_sender_is_skipped_among_look_alike_viewers():
    """Two viewers that look identical are still two viewers.

    (This does not distinguish ``is`` from ``==``: neither a real WebSocket nor
    this double defines ``__eq__``, so those are the same operation. What it
    pins is that exactly one viewer is skipped, not a class of them.)"""
    a, b = FakeWS("same"), FakeWS("same")
    rt = _runtime(a, b)
    asyncio.run(rt.broadcast({"type": "user_message", "content": "x"}, exclude=a))
    assert a.texts() == []
    assert b.texts() == ["x"]


def test_other_message_types_are_unaffected():
    """Only the prompt echo has a sender to skip; turn output must reach all."""
    a, b = FakeWS("a"), FakeWS("b")
    rt = _runtime(a, b)
    asyncio.run(rt.broadcast({"type": "assistant", "content": "reply"}))
    for ws in (a, b):
        assert [m["content"] for m in ws.sent if m["type"] == "assistant"] == ["reply"]


def test_the_handler_tells_enqueue_which_socket_echoed():
    """The call site, not just the helper.

    A mutation sweep showed this was unverified: dropping the origin from
    ``_handle_ws_message``'s call left every test green while restoring the
    exact reported bug. Correct plumbing in the helper is worth nothing if the
    one caller that knows the sender stops passing it.
    """
    src = (Path(__file__).resolve().parent.parent / "server.py").read_text(
        encoding="utf-8")
    body = src[src.index("async def _handle_ws_message"):]
    # The handler has several _enqueue_prompt calls (forwarded command output,
    # queue drain, plain message); only the plain-message one has a sender.
    calls = [body[i:i + 240] for i in range(len(body))
             if body.startswith("_enqueue_prompt(", i)]
    assert calls, "no _enqueue_prompt call in the websocket handler"
    echoing = [c for c in calls if "echoed_by" in c]
    assert echoing, (
        "the websocket handler no longer tells _enqueue_prompt who sent the "
        "prompt, so every other tab loses it again")
    assert any("echoed_by=ws" in c for c in echoing), (
        "the sender socket is no longer passed, so nothing can be excluded")
    assert any('msg.get("client_echoed")' in c for c in echoing), (
        "the handler stopped honouring the client's own report of whether it "
        "echoed, so the sender will see its prompt twice")
