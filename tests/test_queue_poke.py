"""The pending-prompt queue pokes the worker — from the container, not the callers.

From a report: *"i started a session, sent a prompt while it was connecting, it
queued it, after it connected, the prompt didn't send. it seems there's always
another 'the prompt didn't send' bug since the beginning."*

The "always another one" is the important half. Every instance has had the same
shape and a different trigger:

* a prompt typed during a ``/model`` reconnect;
* a prompt typed during a ghost turn that ended on ``bg-all-done`` instead of
  ``queue-edit-done``;
* and this one — a prompt typed during the reconnect ``api_session_launch``
  fires when a second launch reuses a live session, which is an
  ``asyncio.create_task(bridge.reconnect())`` from *outside* the worker.

The mechanism is always: ``state.busy or state.connecting`` routes the prompt to
``state.queued_prompts`` instead of the worker's ``event_queue``, the worker is
parked on ``event_queue.get()``, and whatever transition caused the queueing
ends without reaching a drain checkpoint. The prompt is visible in the panel and
unreachable forever.

Each previous fix added a drain at one more checkpoint, which closes one
instance and leaves the class open. So the poke is wired to
``PersistentDeque``'s listener list — the container every writer must go through
— and re-fired by ``connect()`` on the way out. A writer added tomorrow cannot
reintroduce the bug, because it doesn't have to know the poke exists.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sdk_bridge                              # noqa: E402
from config import Config                      # noqa: E402
from sdk_bridge import SDKBridge               # noqa: E402
from state import State                        # noqa: E402


def _bridge() -> tuple[SDKBridge, list[dict]]:
    """A bridge with no SDK behind it, plus the list of what it broadcast."""
    sent: list[dict] = []

    async def broadcaster(msg):
        sent.append(msg)

    return SDKBridge(Config(), State(), broadcaster), sent


def _pokes(br: SDKBridge) -> list[tuple[str, str]]:
    """Everything currently sitting on the worker's event queue."""
    out = []
    while not br.event_queue.empty():
        out.append(br.event_queue.get_nowait())
    return out


class _FakeClient:
    """A ClaudeSDKClient whose ``connect()`` takes measurable time.

    The delay is the whole point: it is the window in which ``state.connecting``
    is True and a typed prompt is therefore routed to ``queued_prompts``.  A
    zero-cost connect cannot reproduce the bug at all.
    """

    def __init__(self, options=None, connect_delay: float = 0.05) -> None:
        self.options = options
        self.connect_delay = connect_delay

    async def connect(self) -> None:
        await asyncio.sleep(self.connect_delay)

    async def disconnect(self) -> None:
        pass

    async def receive_messages(self):
        # The dispatcher iterates this forever; nothing is ever sent.
        while True:
            await asyncio.sleep(3600)
            yield None      # pragma: no cover


@pytest.fixture
def fake_sdk(monkeypatch):
    """Swap in the fake client and hand back a cleanup hook for the dispatcher."""
    monkeypatch.setattr(sdk_bridge, "ClaudeSDKClient",
                        lambda options=None: _FakeClient(options))
    return None


# --------------------------------------------------------------------------
# The container-level hook
# --------------------------------------------------------------------------

def test_appending_a_prompt_pokes_the_worker():
    """Any writer at all — this test doesn't call one, it mutates the deque."""
    br, _sent = _bridge()

    br.state.queued_prompts.append("hello")

    assert _pokes(br) == [("wakeup", br.QUEUE_POKE)], (
        "appending to queued_prompts did not poke the worker — a prompt "
        "queued while it is parked would sit there forever")


def test_the_hook_is_on_the_container_not_the_call_sites():
    """The property that closes the bug class.

    Nothing in this test knows about ``server.py``'s router, ``/queue`` editing
    or ``_between_turns``.  It reaches past all of them and mutates the deque
    directly, the way a writer nobody has written yet would.
    """
    br, _sent = _bridge()

    br.state.queued_prompts.extend(["a", "b"])
    _pokes(br)
    br.state.queued_prompts.insert(0, "urgent")

    assert _pokes(br) == [("wakeup", br.QUEUE_POKE)], (
        "a writer that isn't one of the known call sites got no poke")


def test_emptying_the_queue_does_not_poke():
    """Popping is a mutation too, and there is nothing left to send."""
    br, _sent = _bridge()
    br.state.queued_prompts.append("only one")
    _pokes(br)

    br.state.queued_prompts.popleft()

    assert _pokes(br) == [], "poked with an empty queue"


def test_popping_to_a_non_empty_queue_still_pokes():
    """The remaining prompts are still sendable, so the poke is correct."""
    br, _sent = _bridge()
    br.state.queued_prompts.extend(["first", "second"])
    _pokes(br)

    br.state.queued_prompts.popleft()

    assert _pokes(br) == [("wakeup", br.QUEUE_POKE)]


def test_persistence_and_the_poke_coexist():
    """``on_change`` is a single slot; the poke must not have taken it.

    Losing queue persistence to fix queue delivery would be a straight trade,
    not a fix — the persisted queue is what survives a server restart.
    """
    br, _sent = _bridge()
    saved: list[list[str]] = []
    br.state.queued_prompts.on_change = (
        lambda: saved.append(list(br.state.queued_prompts)))

    br.state.queued_prompts.append("hello")

    assert saved == [["hello"]], "persistence callback stopped firing"
    assert _pokes(br) == [("wakeup", br.QUEUE_POKE)], "poke stopped firing"


def test_a_listener_that_raises_cannot_break_the_queue():
    br, _sent = _bridge()
    br.state.queued_prompts.add_listener(lambda: 1 / 0)

    br.state.queued_prompts.append("hello")     # must not raise

    assert list(br.state.queued_prompts) == ["hello"]


def test_two_bridges_do_not_share_a_listener_list():
    """A hub hosts many runtimes.  A class-level mutable default would make
    every session's queue poke every other session's worker.

    Both queues are left non-empty on purpose.  ``_poke_for_queued_prompt``
    returns early on an empty queue, so a test with an idle second session
    passes against a shared list without noticing — the cross-talk is invisible
    until the wrongly-poked worker actually has something it could send.
    """
    a, _ = _bridge()
    b, _ = _bridge()
    a.state.queued_prompts.append("for a")
    b.state.queued_prompts.append("for b")
    _pokes(a), _pokes(b)                       # clear both

    a.state.queued_prompts.append("second for a")

    assert _pokes(a) == [("wakeup", a.QUEUE_POKE)]
    assert _pokes(b) == [], "one session's prompt poked another session's worker"


# --------------------------------------------------------------------------
# The parked worker
# --------------------------------------------------------------------------

def test_a_parked_worker_wakes_and_returns_the_queued_prompt():
    br, _sent = _bridge()

    async def go():
        parked = asyncio.create_task(br._await_next_prompt())
        await asyncio.sleep(0)                  # let it reach event_queue.get()
        br.state.queued_prompts.append("typed while parked")
        return await asyncio.wait_for(parked, timeout=2.0)

    assert asyncio.run(go()) == "typed while parked"


def test_the_poke_is_declined_while_a_connect_is_in_flight():
    """It would otherwise hand a prompt to run_turn against a half-open client.

    ``connect()`` assigns ``self.client`` *before* awaiting its handshake, so
    the client is non-None and unusable at the same time — ``query()`` on it
    would fail.  Declining is only safe because ``connect()`` re-pokes; the
    next test is the one that proves it does.
    """
    br, _sent = _bridge()
    br.state.connecting = True

    async def go():
        parked = asyncio.create_task(br._await_next_prompt())
        await asyncio.sleep(0)
        br.state.queued_prompts.append("typed mid-connect")
        await asyncio.sleep(0.05)
        still_parked = not parked.done()
        parked.cancel()
        await asyncio.gather(parked, return_exceptions=True)
        return still_parked

    assert asyncio.run(go()), (
        "popped a queued prompt while connecting — run_turn would have called "
        "query() on a client whose connect() had not returned")
    assert list(br.state.queued_prompts) == ["typed mid-connect"], \
        "the prompt was consumed rather than left for connect() to re-poke"


def test_the_poke_yields_to_pending_events_but_is_not_dropped():
    """Yielding must not become a new way to strand a prompt.

    The poke steps aside for anything already pending, because everything else
    on the event queue is something the user explicitly asked for. But if
    yielding *dropped* it, any pending event whose handler doesn't itself drain
    the queue would strand the prompt all over again — the same bug, now
    triggered by the fix for it. So it goes to the back of the queue instead.

    The pending event here is an unrecognised kind: the one branch that
    deliberately does nothing at all, so the prompt can only arrive if the poke
    genuinely survived the yield.

    Order matters, and getting it backwards makes this test vacuous: the queue
    is FIFO, so the poke has to be enqueued *ahead* of the other event for the
    yield to be reached at all. Queue the prompt first, then the event.
    """
    br, _sent = _bridge()

    async def go():
        br.state.queued_prompts.append("must still arrive")   # poke goes first
        br.event_queue.put_nowait(("no-such-kind", "ignored"))
        task = asyncio.create_task(br._await_next_prompt())
        return await asyncio.wait_for(task, timeout=2.0)

    assert asyncio.run(go()) == "must still arrive"


def test_a_queue_full_of_pokes_terminates():
    """Yielding is bounded to one hop, so pokes can't shuffle forever."""
    br, _sent = _bridge()

    async def go():
        br.state.queued_prompts.extend(["a", "b", "c"])   # three pokes
        task = asyncio.create_task(br._await_next_prompt())
        return await asyncio.wait_for(task, timeout=2.0)

    assert asyncio.run(go()) == "a"


# --------------------------------------------------------------------------
# The reported bug, end to end
# --------------------------------------------------------------------------

def test_a_prompt_typed_during_an_external_reconnect_still_sends(fake_sdk):
    """The exact reported sequence.

    ``api_session_launch`` reuses a live session and applies the launcher's
    ``--model``/``--effort`` by firing ``asyncio.create_task(bridge.reconnect())``
    — from the server, with nothing awaiting it and no worker checkpoint after
    it.  Measured on the real thing, that connect took **33 seconds**
    (21:38:55 → 21:39:28 in orchestrator2.log), which is a very comfortable
    window in which to type a prompt.

    The worker is parked the whole time. Before the fix, the prompt landed in
    ``queued_prompts``, the reconnect finished, and nothing ever looked at the
    queue again.
    """
    br, _sent = _bridge()

    async def go():
        parked = asyncio.create_task(br._await_next_prompt())
        await asyncio.sleep(0)

        # Verbatim api_session_launch: fire and forget.
        reconnecting = asyncio.create_task(br.connect())
        await asyncio.sleep(0.01)
        assert br.state.connecting, "test premise: the connect must be in flight"

        # Verbatim server.py's router.
        if br.state.busy or br.state.connecting:
            br.state.queued_prompts.append("the prompt the user typed")

        await reconnecting
        try:
            return await asyncio.wait_for(parked, timeout=2.0)
        finally:
            if br._dispatcher_task is not None:
                br._dispatcher_task.cancel()
                await asyncio.gather(br._dispatcher_task, return_exceptions=True)

    assert asyncio.run(go()) == "the prompt the user typed", (
        "the prompt typed during a reconnect nobody awaits was never sent — "
        "it sat in the queue panel while the session showed idle")


def test_connect_does_not_poke_for_an_empty_queue(fake_sdk):
    """The re-poke is conditional; an ordinary connect leaves no litter."""
    br, _sent = _bridge()

    async def go():
        await br.connect()
        if br._dispatcher_task is not None:
            br._dispatcher_task.cancel()
            await asyncio.gather(br._dispatcher_task, return_exceptions=True)

    asyncio.run(go())

    assert _pokes(br) == []


def test_the_prompt_is_echoed_exactly_once_on_the_way_out():
    """Waking up is not enough — the transcript has to show it.

    The frontend skips its own optimistic echo while ``_isBusy``, and
    ``connecting`` is one of the things that makes ``_isBusy`` true. So a
    prompt queued during a connect has *never* been echoed by anyone at the
    point the worker picks it up.
    """
    br, sent = _bridge()

    async def go():
        parked = asyncio.create_task(br._await_next_prompt())
        await asyncio.sleep(0)
        br.state.queued_prompts.append("say it once")
        return await asyncio.wait_for(parked, timeout=2.0)

    asyncio.run(go())

    echoes = [m for m in sent
              if m.get("type") == "user_message" and m.get("content") == "say it once"]
    assert len(echoes) == 1, f"expected exactly one echo, got {len(echoes)}"
    assert list(br.state.queued_prompts) == [], "prompt left in the queue panel"
