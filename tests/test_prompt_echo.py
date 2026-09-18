"""Every prompt that runs must appear in the transcript as a "you:" message.

From a report: a queued prompt wasn't sent by an interrupt, so the user clicked
the green send arrow next to it in the queue panel — "then it answered the
prompt, but my prompt never showed up as a message from 'you:'".  An answer
under no question.

Only one producer echoes on its own: the browser's optimistic echo in
``app.js send()``, which fires when it believes the session is idle and reports
which way it went via ``client_echoed``.  Every other producer — the send
arrow (``/api/queue/send``), ``/queue send``, ``/graphify`` — is a REST call or
a server-side synthesis that echoes nowhere.  ``_enqueue_prompt`` is the single
choke point that fixes that, and these tests pin it there.

Offline: hand-built runtimes with fake bridges, no SDK.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from session_runtime import SessionRuntime  # noqa: E402


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))


class FakeBridge:
    """Just the parts of the bridge the enqueue path touches.

    ``pending_turn_end`` stands in for a turn that ended on a compaction: its
    "Turn N completed" marker is deferred, and whoever is about to echo the
    next prompt owes it to the transcript first.
    """

    def __init__(self, rt) -> None:
        self.event_queue = asyncio.Queue()
        self._rt = rt
        self.pending_turn_end = False

    async def flush_pending_turn_end(self) -> None:
        if not self.pending_turn_end:
            return
        self.pending_turn_end = False
        await self._rt.broadcast({"type": "turn_end", "subtype": "success",
                                  "duration": "0:26:41", "turns": 102})

    async def stop(self) -> None:
        pass

    def drained(self) -> list[tuple[str, str]]:
        out = []
        while not self.event_queue.empty():
            out.append(self.event_queue.get_nowait())
        return out


def _runtime(*, busy=False, queued=()):
    state = SimpleNamespace(
        session_id="sid-1", session_title=None, busy=busy, connecting=False,
        queued_prompts=deque(queued), queue_editing_index=None,
    )
    cfg = SimpleNamespace(cwd="C:/proj", session_idle_timeout=300)
    rt = SessionRuntime(config=cfg, state=state, rid=None)
    rt.bridge = FakeBridge(rt)
    return rt


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    saved_runtimes = dict(server.runtimes)
    saved_default = server._default_runtime
    server.runtimes.clear()
    server._default_runtime = None
    try:
        yield
    finally:
        server.runtimes.clear()
        server.runtimes.update(saved_runtimes)
        server._default_runtime = saved_default


def _echoes(ws_or_list) -> list[str]:
    sent = ws_or_list.sent if hasattr(ws_or_list, "sent") else ws_or_list
    return [m["content"] for m in sent if m.get("type") == "user_message"]


# --------------------------------------------------------------------------
# _enqueue_prompt
# --------------------------------------------------------------------------

def test_enqueue_echoes_when_the_client_did_not():
    rt = _runtime()
    ws = FakeWS()
    rt.clients.add(ws)

    async def go():
        ok = await server._enqueue_prompt(rt, "do the thing")
        await asyncio.sleep(0)          # let the channel writer flush
        return ok

    assert asyncio.run(go()) is True
    assert _echoes(ws) == ["do the thing"], \
        "prompt ran with no 'you:' message in the transcript"
    assert rt.bridge.drained() == [("message", "do the thing")]


def test_enqueue_does_not_echo_when_the_client_already_did():
    """The browser's optimistic echo must not be doubled."""
    rt = _runtime()
    ws = FakeWS()
    rt.clients.add(ws)

    async def go():
        await server._enqueue_prompt(rt, "typed while idle", echoed_by=ws)
        await asyncio.sleep(0)

    asyncio.run(go())
    assert _echoes(ws) == []
    assert rt.bridge.drained() == [("message", "typed while idle")]


def test_enqueue_without_a_bridge_reports_failure_and_swallows_nothing():
    rt = _runtime()
    rt.bridge = None
    assert asyncio.run(server._enqueue_prompt(rt, "x")) is False
    assert asyncio.run(server._enqueue_prompt(None, "x")) is False


def test_enqueue_rejects_and_resurfaces_reason_when_connect_blocked():
    """A session parked unable to connect must reject the prompt with the real
    reason, not drop it onto an event_queue the parked worker isn't draining
    (which the tab would misreport as "worker may be wedged")."""
    rt = _runtime()
    rt.state.connect_blocked_msg = "Refusing to resume — already running as PID 108256."
    ws = FakeWS()
    rt.clients.add(ws)

    async def go():
        ok = await server._enqueue_prompt(rt, "do the thing")
        await asyncio.sleep(0)          # let the channel writer flush
        return ok

    # Rejected (caller acks PROMPT_REJECTED, which suppresses the watchdog)...
    assert asyncio.run(go()) is False
    # ...nothing left on the event_queue for a non-existent turn to strand...
    assert rt.bridge.drained() == []
    # ...and the actual reason was re-surfaced to the tab.
    errors = [m["data"]["message"] for m in ws.sent
              if m.get("type") == "system_msg" and m.get("subtype") == "error"]
    assert errors == ["Refusing to resume — already running as PID 108256."]


# --------------------------------------------------------------------------
# The green send arrow  (POST /api/queue/send)
# --------------------------------------------------------------------------

def test_send_arrow_echoes_the_prompt_it_sends():
    """The exact reported path: click ▶ on a queued prompt while idle."""
    rt = _runtime(busy=False, queued=["the prompt I queued"])
    ws = FakeWS()
    rt.clients.add(ws)
    server.runtimes[rt.rid] = rt
    server._default_runtime = rt

    async def go():
        res = await server.api_queue_send({"rid": rt.rid, "index": 0})
        await asyncio.sleep(0)
        return res

    res = asyncio.run(go())
    assert res["ok"] is True
    assert _echoes(ws) == ["the prompt I queued"], \
        "the send arrow ran a prompt that never appeared as 'you:'"
    assert rt.bridge.drained() == [("message", "the prompt I queued")]
    assert list(rt.state.queued_prompts) == [], "prompt left in the queue"


def test_send_arrow_while_busy_reorders_instead_of_sending():
    """Busy path is untouched: move to front, no echo, no enqueue."""
    rt = _runtime(busy=True, queued=["first", "second"])
    ws = FakeWS()
    rt.clients.add(ws)
    server.runtimes[rt.rid] = rt
    server._default_runtime = rt

    async def go():
        res = await server.api_queue_send({"rid": rt.rid, "index": 1})
        await asyncio.sleep(0)
        return res

    res = asyncio.run(go())
    assert res.get("moved_to_front") is True
    assert list(rt.state.queued_prompts) == ["second", "first"]
    assert _echoes(ws) == [], "reordering is not sending — nothing to echo"
    assert rt.bridge.drained() == []


def test_send_arrow_republishes_the_queue():
    """The panel must stop showing a prompt the arrow just sent."""
    rt = _runtime(busy=False, queued=["a", "b"])
    ws = FakeWS()
    rt.clients.add(ws)
    server.runtimes[rt.rid] = rt
    server._default_runtime = rt

    async def go():
        await server.api_queue_send({"rid": rt.rid, "index": 0})
        await asyncio.sleep(0)

    asyncio.run(go())
    updates = [m for m in ws.sent if m.get("type") == "queue_update"]
    assert updates, "no queue_update after the send arrow"
    assert [i["text"] for i in updates[-1]["queue"]] == ["b"]


def test_send_arrow_echo_precedes_the_enqueue():
    """Ordering matters: the question has to reach the transcript before the
    answer starts streaming into it."""
    rt = _runtime(busy=False, queued=["q"])
    ws = FakeWS()
    rt.clients.add(ws)
    server.runtimes[rt.rid] = rt
    server._default_runtime = rt

    async def go():
        await server.api_queue_send({"rid": rt.rid, "index": 0})
        # Echo is awaited before the put_nowait, so by the time the bridge
        # could observe the event the broadcast is already queued.
        assert rt.bridge.event_queue.qsize() == 1
        await asyncio.sleep(0)

    asyncio.run(go())
    assert _echoes(ws) == ["q"]


# --------------------------------------------------------------------------
# A deferred turn_end must not surface *after* the next prompt
# --------------------------------------------------------------------------

def _kinds(ws: FakeWS) -> list[str]:
    return [m.get("type") for m in ws.sent]


def test_enqueue_closes_out_a_deferred_turn_end_first():
    """From a report: "sometimes it says 'Turn 102 completed' not after the
    turn completes but after the prompt for the next turn".

    A turn that ends on a compaction defers its marker; ``run_turn`` flushes it
    at the *start* of the following turn, which is after the next prompt has
    been echoed.  Whoever echoes has to close the previous turn out first.
    """
    rt = _runtime()
    rt.bridge.pending_turn_end = True
    ws = FakeWS()
    rt.clients.add(ws)

    async def go():
        await server._enqueue_prompt(rt, "next thing")
        await asyncio.sleep(0)

    asyncio.run(go())
    assert _kinds(ws) == ["turn_end", "user_message"], (
        "the previous turn's completion marker landed after the prompt that "
        "follows it")


def test_send_arrow_closes_out_a_deferred_turn_end_first():
    rt = _runtime(busy=False, queued=["queued while compacting"])
    rt.bridge.pending_turn_end = True
    ws = FakeWS()
    rt.clients.add(ws)
    server.runtimes[rt.rid] = rt
    server._default_runtime = rt

    async def go():
        await server.api_queue_send({"rid": rt.rid, "index": 0})
        await asyncio.sleep(0)

    asyncio.run(go())
    kinds = [k for k in _kinds(ws) if k in ("turn_end", "user_message")]
    assert kinds == ["turn_end", "user_message"]


def test_enqueue_flushes_even_when_the_client_echoed():
    """The browser's optimistic echo is local, so the server can't get ahead of
    it — but it must still not sit on the marker until the next turn starts."""
    rt = _runtime()
    rt.bridge.pending_turn_end = True
    ws = FakeWS()
    rt.clients.add(ws)

    async def go():
        await server._enqueue_prompt(rt, "typed", echoed_by=ws)
        await asyncio.sleep(0)

    asyncio.run(go())
    assert _kinds(ws) == ["turn_end"]


def test_enqueue_with_nothing_pending_emits_no_marker():
    rt = _runtime()
    ws = FakeWS()
    rt.clients.add(ws)

    async def go():
        await server._enqueue_prompt(rt, "hello")
        await asyncio.sleep(0)

    asyncio.run(go())
    assert _kinds(ws) == ["user_message"], "invented a turn_end out of nothing"
