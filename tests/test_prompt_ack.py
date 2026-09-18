"""A prompt's watchdog is answered by the prompt, not by ambient traffic.

The old frontend watchdog was a single 8s timer cleared by *any* inbound
WebSocket message. That premise is false here: ``_tick_runtime`` force-pushes a
status snapshot every ``_STATUS_HEARTBEAT_SECONDS`` (30s) even when nothing has
changed, and bells, panel updates and background-task notices arrive
independently of any prompt. Ambient traffic proves the *server* is running,
which was never the question — the question is whether *this prompt* is being
acted on.

It mattered. When a session's worker task was silently cancelled (see
known-issues.md, "A silently-cancelled worker made a session ignore every
prompt"), the server stayed perfectly responsive: it accepted the prompt onto
``event_queue``, kept broadcasting status, and reported "idle" — accurately.
Nothing was ever going to run it, and the tab never once complained.

The fix is a correlated ack. The tab tags a prompt with its own ``prompt_id``;
the server replies ``prompt_ack`` naming **where the prompt landed**; the tab
then checks, at the deadline, whether that disposition is consistent with what
the session is doing. The dispositions are not interchangeable:

* ``queued``    — parked in the visible pending panel behind a running turn.
* ``enqueued``  — handed to the worker to run *now*, so a turn must start.
* ``immediate`` — answered inline.
* ``rejected``  — refused; the prompt is gone and the user must be told.
* *(no ack)*    — the server never took it: half-open socket, or dead server.

These tests cover the server half: that each disposition is reported, reported
*to the sender only*, and reported for the right reason. The distinction the
outage turned on is ``queued`` vs ``enqueued`` — same prompt, same UI-visible
"nothing happening", opposite meanings.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server                                   # noqa: E402
from config import parse_args                   # noqa: E402
from session_runtime import SessionRuntime      # noqa: E402
from state import State                         # noqa: E402


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))


class FakeBridge:
    """Enough SDKBridge for the WS router: an event queue and the no-op hooks."""

    def __init__(self) -> None:
        self.event_queue: asyncio.Queue = asyncio.Queue()
        self.stopped = False

    async def flush_pending_turn_end(self) -> None:
        pass

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def hub(monkeypatch):
    """A single-runtime hub with the module globals saved and restored."""
    cfg = parse_args([])
    st = State()
    rt = SessionRuntime(config=cfg, state=st, rid="R1")
    rt.bridge = FakeBridge()

    saved = (dict(server.runtimes), server._default_runtime, server.config,
             server.state, server.bridge, dict(server._ws_runtime))
    server.runtimes.clear()
    server.runtimes[rt.rid] = rt
    server._default_runtime = rt
    server.config = cfg
    server.state = st
    server.bridge = rt.bridge
    yield rt
    server.runtimes.clear()
    server.runtimes.update(saved[0])
    server._default_runtime = saved[1]
    server.config = saved[2]
    server.state = saved[3]
    server.bridge = saved[4]
    server._ws_runtime.clear()
    server._ws_runtime.update(saved[5])


def _acks(ws: FakeWS) -> list[dict]:
    return [m for m in ws.sent if m.get("type") == "prompt_ack"]


def _send(ws, rt, text, **extra):
    msg = {"type": "message", "text": text, "client_echoed": True}
    msg.update(extra)
    return server._handle_ws_message(ws, msg)


# ---------------------------------------------------------------------------
# The two dispositions the outage could not tell apart
# ---------------------------------------------------------------------------

def test_idle_prompt_is_acked_enqueued(hub):
    """Idle session → the worker is expected to run it *now*.

    This is the disposition that carries an obligation: a turn must start. It is
    the one the wedged-worker outage produced, and the only one the tab escalates
    on.
    """
    async def go():
        ws = FakeWS()
        await _send(ws, hub, "do the thing", prompt_id="tab-1")
        assert _acks(ws) == [{"type": "prompt_ack", "prompt_id": "tab-1",
                              "disposition": "enqueued"}]
        assert hub.bridge.event_queue.get_nowait() == ("message", "do the thing")

    asyncio.run(go())


def test_busy_prompt_is_acked_queued(hub):
    """Busy session → parked in the visible panel. Benign; no escalation."""
    async def go():
        hub.state.busy = True
        ws = FakeWS()
        await _send(ws, hub, "do the thing", prompt_id="tab-2")
        assert _acks(ws) == [{"type": "prompt_ack", "prompt_id": "tab-2",
                              "disposition": "queued"}]
        assert list(hub.state.queued_prompts) == ["do the thing"]
        assert hub.bridge.event_queue.empty()

    asyncio.run(go())


def test_connecting_prompt_is_acked_queued(hub):
    """``connecting`` queues exactly like ``busy`` and must ack the same way.

    A separate test because these are separate flags on separate code paths, and
    the reconnect window is where the *other* bug in this session lived.
    """
    async def go():
        hub.state.connecting = True
        ws = FakeWS()
        await _send(ws, hub, "during connect", prompt_id="tab-3")
        assert _acks(ws)[0]["disposition"] == "queued"

    asyncio.run(go())


def test_prompt_before_the_bridge_exists_is_acked_queued(hub):
    """The default runtime's bridge is built off the hot path; a fast tab beats it.

    That path queues and warns, and must ack too — otherwise the watchdog fires
    "no response from the server" during a perfectly normal startup.
    """
    async def go():
        hub.bridge = None
        ws = FakeWS()
        await _send(ws, hub, "very early", prompt_id="tab-4")
        assert _acks(ws)[0]["disposition"] == "queued"
        assert list(hub.state.queued_prompts) == ["very early"]

    asyncio.run(go())


def test_rejected_when_the_prompt_cannot_be_delivered(hub):
    """``_enqueue_prompt`` returning False must not read as success.

    ``enqueued`` means "the worker has it"; if nothing took the prompt, saying so
    is the whole difference between a lost message and a reported one.
    """
    async def go():
        ws = FakeWS()

        async def _fail(*a, **kw):
            return False

        real = server._enqueue_prompt
        server._enqueue_prompt = _fail
        try:
            await _send(ws, hub, "goes nowhere", prompt_id="tab-5")
        finally:
            server._enqueue_prompt = real
        assert _acks(ws)[0]["disposition"] == "rejected"

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Correlation and addressing
# ---------------------------------------------------------------------------

def test_ack_carries_back_the_exact_id(hub):
    """Ids are echoed verbatim so a stale ack can't vouch for a later prompt.

    The tab discards an ack whose id it isn't waiting on; that only works if the
    server never rewrites, normalises or reuses the value.
    """
    async def go():
        ws = FakeWS()
        for pid in ("a9f3c1-1", "a9f3c1-2", "a9f3c1-3"):
            await _send(ws, hub, "p", prompt_id=pid)
        assert [a["prompt_id"] for a in _acks(ws)] == [
            "a9f3c1-1", "a9f3c1-2", "a9f3c1-3"]

    asyncio.run(go())


def test_ack_goes_only_to_the_sending_tab(hub):
    """Two tabs on one session: only the sender is waiting on this prompt.

    Broadcasting would disarm the other tab's watchdog for a prompt it never
    sent — reintroducing, by a narrower route, exactly the "someone else's
    traffic vouched for me" flaw this replaces.
    """
    async def go():
        sender, observer = FakeWS(), FakeWS()
        server._ws_runtime[sender] = hub
        server._ws_runtime[observer] = hub
        hub.clients.add(sender)
        hub.clients.add(observer)
        await _send(sender, hub, "mine", prompt_id="tab-6")
        assert len(_acks(sender)) == 1
        assert _acks(observer) == []

    asyncio.run(go())


def test_no_prompt_id_means_no_ack(hub):
    """Untagged sends stay byte-identical on the wire.

    Slash commands, control frames and any older cached tab don't opt in, and
    must not start receiving a message type they don't handle.
    """
    async def go():
        ws = FakeWS()
        await _send(ws, hub, "untagged")
        assert _acks(ws) == []

    asyncio.run(go())


def test_command_type_forwards_the_prompt_id(hub):
    """``{type:'command'}`` re-dispatches as a message and must keep the id.

    Dropping it there would leave a watchdog waiting on an ack that the server
    had already decided not to send.
    """
    async def go():
        ws = FakeWS()
        await server._handle_ws_message(
            ws, {"type": "command", "text": "status", "prompt_id": "tab-7"})
        assert [a["prompt_id"] for a in _acks(ws)] == ["tab-7"]

    asyncio.run(go())


def test_every_branch_acks_even_without_saying_so(hub):
    """A branch that dispositions nothing still produces exactly one ack.

    The wrapper defaults to ``immediate`` precisely so that the ~two dozen early
    returns in the dispatcher don't each have to remember.  Missing an ack does
    not fail safe: the tab reports "no response from the server" and *closes a
    healthy socket*.  ``/interrupt`` is one such branch — it returns long before
    any disposition is decided.
    """
    async def go():
        ws = FakeWS()

        async def _noop_interrupt(bridge, broadcast):
            pass

        real = server._do_interrupt
        server._do_interrupt = _noop_interrupt
        try:
            await _send(ws, hub, "/interrupt", prompt_id="tab-i")
        finally:
            server._do_interrupt = real
        assert _acks(ws) == [{"type": "prompt_ack", "prompt_id": "tab-i",
                              "disposition": "immediate"}]

    asyncio.run(go())


def test_a_prompt_is_never_acked_twice(hub):
    """One prompt, one answer.

    The ``{type:'command'}`` path re-enters the dispatcher, so it is the one
    place a naive implementation emits two acks for one prompt — the inner call's
    real disposition and then the outer wrapper's default on top of it. The tab
    keeps the *last* ack it sees, so a trailing ``immediate`` would overwrite a
    real ``enqueued`` and silently disable the escalation this all exists for.
    """
    async def go():
        ws = FakeWS()
        server._ws_runtime[ws] = hub
        hub.clients.add(ws)
        await server._handle_ws_message(
            ws, {"type": "command", "text": "connect", "prompt_id": "tab-d"})
        assert len(_acks(ws)) == 1

    asyncio.run(go())


def test_the_real_disposition_is_not_overwritten_by_the_default(hub):
    """The wrapper only fills a gap; it never speaks over a branch that answered.

    ``enqueued`` is the disposition that carries an obligation, so it is the one
    a stray default would be most damaging to erase.
    """
    async def go():
        ws = FakeWS()
        await _send(ws, hub, "run this", prompt_id="tab-e")
        assert [a["disposition"] for a in _acks(ws)] == ["enqueued"]

    asyncio.run(go())


def test_slash_command_is_acked_immediate(hub):
    """A non-immediate slash command goes to the worker as a control event.

    It will never make the session "busy", so reporting it as ``enqueued`` would
    have the tab escalate on a command that worked fine.
    """
    async def go():
        ws = FakeWS()
        await _send(ws, hub, "/connect", prompt_id="tab-8")
        assert _acks(ws)[0]["disposition"] == "immediate"
        assert hub.bridge.event_queue.get_nowait() == ("connect", "")

    asyncio.run(go())


def test_ack_does_not_disturb_the_queue_update(hub):
    """The ack is additive — the pending panel still gets its update.

    Cheap to assert, and it pins that the ack was added *after* the existing
    feedback rather than in place of it.
    """
    async def go():
        hub.state.busy = True
        ws = FakeWS()
        server._ws_runtime[ws] = hub
        hub.clients.add(ws)
        await _send(ws, hub, "queued one", prompt_id="tab-9")
        types = [m["type"] for m in ws.sent]
        assert "queue_update" in types
        assert "prompt_ack" in types
        assert types.index("queue_update") < types.index("prompt_ack")

    asyncio.run(go())
