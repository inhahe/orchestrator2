"""Popping a pending prompt must both send it *and* remove it from the panel.

From a report: "i entered a prompt while it was working, it queued it, then i
ctrl-c interrupted it, it sent the prompt, but then the prompt was still also
in the queue."

Two separate defects were behind that, and both are pinned here.

1. The queue panel is rendered from a snapshot (``queue_update``, or the
   ``panels`` blob on ``status_update``).  ``_between_turns`` popped the
   prompt and echoed it to the transcript but never pushed a new snapshot, so
   the row lingered until the 2 s status ticker happened to fire — long enough
   to look like a double-send, short enough that it never got pinned down.

2. ``_between_turns`` returned early on ``interrupted`` *before* the queue
   drain, so an interrupted turn stranded the queued prompt entirely.  Which
   of the two symptoms you got was a race: ``interrupted`` is only True when
   run_turn spots the interrupt before the SDK's ResultMessage, and on a short
   turn the ResultMessage wins.  Same keypress, two behaviours.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_agent_sdk import ResultMessage    # noqa: E402
from config import Config                     # noqa: E402
from sdk_bridge import SDKBridge              # noqa: E402
from state import State                       # noqa: E402


class _FakeClient:
    """Enough of ClaudeSDKClient for interrupt().  With no client at all there
    is nothing to wind down, so the bridge correctly holds nothing back — the
    race only exists when a real CLI is on the other end."""

    def __init__(self) -> None:
        self.interrupts = 0

    async def interrupt(self) -> None:
        self.interrupts += 1


def _result_message(subtype: str = "success") -> ResultMessage:
    """The CLI's terminating message for a stream — what an interrupt's
    wind-down ends with, and what a turn started too early would inherit."""
    return ResultMessage(
        subtype=subtype, duration_ms=0, duration_api_ms=0,
        is_error=(subtype != "success"), num_turns=1, session_id="sid-1",
    )


def _bridge() -> tuple[SDKBridge, list[dict]]:
    """A bridge with no SDK behind it, plus the list of what it broadcast."""
    sent: list[dict] = []

    async def broadcaster(msg):
        sent.append(msg)

    return SDKBridge(Config(), State(), broadcaster), sent


def _kinds(sent: list[dict]) -> list[str]:
    return [m.get("type") for m in sent]


def _latest_queue(sent: list[dict]) -> list[str] | None:
    """Text of the most recent queue_update, or None if none was sent."""
    for msg in reversed(sent):
        if msg.get("type") == "queue_update":
            return [item["text"] for item in msg["queue"]]
    return None


# --------------------------------------------------------------------------
# _pop_queued_prompt — the shared helper every drain site now funnels through
# --------------------------------------------------------------------------

def test_pop_removes_echoes_and_republishes_the_queue():
    """The three things a pop must do, in one go."""
    br, sent = _bridge()
    br.state.queued_prompts.extend(["first", "second"])

    prompt = asyncio.run(br._pop_queued_prompt())

    assert prompt == "first"
    assert list(br.state.queued_prompts) == ["second"], "prompt not dequeued"
    assert {"type": "user_message", "content": "first"} in sent, \
        "prompt never echoed to the transcript"
    assert _latest_queue(sent) == ["second"], (
        "no queue_update after the pop — the panel would keep showing a "
        "prompt that is already running")


def test_pop_clears_stale_editing_index():
    br, _sent = _bridge()
    br.state.queued_prompts.extend(["a", "b"])
    br.state.queue_editing_index = 1     # points at "b"

    asyncio.run(br._pop_queued_prompt())
    # "b" is now index 0; keeping the old 1 would guard the wrong row.
    assert br.state.queue_editing_index is None


def test_pop_holds_off_while_the_head_is_being_edited():
    br, sent = _bridge()
    br.state.queued_prompts.append("half-typed")
    br.state.queue_editing_index = 0

    assert asyncio.run(br._pop_queued_prompt()) is None
    assert list(br.state.queued_prompts) == ["half-typed"]
    assert sent == [], "nothing should be broadcast for a held-back pop"


def test_pop_on_an_empty_queue_is_a_noop():
    br, sent = _bridge()
    assert asyncio.run(br._pop_queued_prompt()) is None
    assert sent == []


def test_pop_closes_out_a_deferred_turn_end_before_echoing():
    """From a report: "sometimes it says 'Turn 102 completed (0:26:41) —
    success' not after the turn completes but after the prompt for the next
    turn.  maybe only when the prompt was queued".

    A turn that ends on a compaction doesn't broadcast its ``turn_end``; it
    parks it in ``pending_compact_turn_end`` so a post-compact ghost turn can
    claim it, with a 10 s timer and ``run_turn``'s own start-of-turn flush as
    backstops.  That last backstop runs *after* ``_between_turns`` has already
    popped and echoed the next prompt — hence the inversion.

    The user's "maybe only when the prompt was queued" is a correlation rather
    than a rule: a queued prompt starts the next turn at once, comfortably
    inside the 10 s grace, so it loses that race every time; a hand-typed one
    usually takes longer than 10 s, letting the timer win.
    """
    br, sent = _bridge()
    br.state.pending_compact_turn_end = {"subtype": "success",
                                         "started_at": 0.0}
    br.state.queued_prompts.append("what's next")

    asyncio.run(br._pop_queued_prompt())

    kinds = _kinds(sent)
    assert "turn_end" in kinds, "the compact-closed turn was never marked"
    assert kinds.index("turn_end") < kinds.index("user_message"), (
        "the previous turn's completion marker landed after the prompt that "
        "follows it")
    assert br.state.pending_compact_turn_end is None


def test_pop_with_nothing_pending_emits_no_turn_end():
    br, sent = _bridge()
    br.state.queued_prompts.append("plain prompt")

    asyncio.run(br._pop_queued_prompt())

    assert "turn_end" not in _kinds(sent), "invented a turn_end out of nothing"


def test_held_back_pop_does_not_flush_the_turn_end():
    """Nothing is being echoed, so there's nothing to get ahead of — and the
    ghost-turn claim path still deserves its chance at the marker."""
    br, sent = _bridge()
    br.state.pending_compact_turn_end = {"subtype": "success",
                                         "started_at": 0.0}
    br.state.queued_prompts.append("half-typed")
    br.state.queue_editing_index = 0

    assert asyncio.run(br._pop_queued_prompt()) is None
    assert sent == []
    assert br.state.pending_compact_turn_end is not None


def test_pop_clears_the_attention_flag():
    """Running a real user prompt means the session no longer needs the user."""
    br, _sent = _bridge()
    br.state.needs_user_attention = "done"
    br.state.queued_prompts.append("go on")

    asyncio.run(br._pop_queued_prompt())
    assert br.state.needs_user_attention is None


# --------------------------------------------------------------------------
# The interrupt path
# --------------------------------------------------------------------------

def test_interrupted_turn_still_sends_the_queued_prompt():
    """The reported bug: Ctrl-C used to strand the prompt in the panel.

    ``_between_turns`` is called with ``interrupted=True`` exactly as
    ``worker_loop`` calls it.  Nothing pokes the worker after an interrupt, so
    if this returns without draining, the prompt never goes out — it would
    have blocked in ``_await_next_prompt`` forever.
    """
    br, sent = _bridge()
    br.state.queued_prompts.append("stop — do this instead")

    # Bounded: on the old code this parked in _await_next_prompt() and would
    # hang the suite rather than fail it.
    async def go():
        return await asyncio.wait_for(
            br._between_turns("", interrupted=True), timeout=2)

    prompt = asyncio.run(go())

    assert prompt == "stop — do this instead"
    assert list(br.state.queued_prompts) == []
    assert _latest_queue(sent) == []


def test_interrupt_with_nothing_queued_does_not_invent_a_prompt():
    """No queue → park.  Guard against the drain turning into an auto-resume."""
    br, _sent = _bridge()

    async def go():
        task = asyncio.create_task(br._between_turns("", interrupted=True))
        await asyncio.sleep(0)          # let it reach the idle wait
        assert not task.done(), "returned a prompt out of an empty queue"
        br.stop_event.set()
        br.event_queue.put_nowait(("wakeup", "test"))
        return await asyncio.wait_for(task, timeout=2)

    assert asyncio.run(go()) is None


def test_interrupt_does_not_auto_continue():
    """An interrupted turn must never resume itself, queue or no queue."""
    br, _sent = _bridge()
    br.state.queued_prompts.append("user text")

    async def go():
        return await asyncio.wait_for(
            br._between_turns("", interrupted=True), timeout=2)

    assert asyncio.run(go()) == "user text"


def test_normal_turn_end_drains_the_queue_too():
    """The non-interrupt path must behave identically — same helper, same push."""
    br, sent = _bridge()
    br.state.queued_prompts.extend(["one", "two"])

    async def go():
        return await asyncio.wait_for(
            br._between_turns("some reply", interrupted=False), timeout=2)

    prompt = asyncio.run(go())

    assert prompt == "one"
    assert list(br.state.queued_prompts) == ["two"]
    assert _latest_queue(sent) == ["two"]


# --------------------------------------------------------------------------
# /btw while idle
# --------------------------------------------------------------------------

def test_btw_while_idle_sends_the_btw_text_not_the_queue_head():
    """``/btw`` used to append-then-popleft, which is only a no-op when empty.

    With anything already pending it returned *that* prompt and left the /btw
    text sitting in the queue.

    Both signals are made pending *before* the worker parks, which is the only
    way they can now coexist: appending to ``queued_prompts`` pokes the worker
    (``_poke_for_queued_prompt``), so a prompt queued while the worker is
    already parked is taken immediately, and there is never a queue head left
    sitting there for a later ``/btw`` to lose to.  The reachable race is the
    one modelled here — the user queued a prompt *and* typed ``/btw`` while a
    turn was still running, and both land in front of the worker at the moment
    it goes idle.

    So this pins two things at once: ``/btw`` returns its own text (the
    original bug), and it outranks the queue poke it shares the event queue
    with — jumping the queue is the entire point of ``/btw``.
    """
    br, _sent = _bridge()
    br.state.queued_prompts.append("previously queued")   # also enqueues a poke

    async def go():
        br.event_queue.put_nowait(("btw", "quick side question"))
        task = asyncio.create_task(br._await_next_prompt())
        return await asyncio.wait_for(task, timeout=2)

    assert asyncio.run(go()) == "quick side question"
    assert list(br.state.queued_prompts) == ["previously queued"], \
        "/btw consumed the queue head instead of jumping it"


# --------------------------------------------------------------------------
# /btw typed *during* a turn
# --------------------------------------------------------------------------

def test_btw_typed_during_a_turn_jumps_the_queue():
    """Reported 2026-09-18: "I did a /btw and it didn't send it and show me the
    result until after the turn ended. That seems to defeat the purpose?"

    Half of that is unavoidable -- a turn owns the CLI, so nothing can be
    answered until it ends.  The other half was ours: this path *appended*, so
    the /btw landed behind everything already queued.  "The moment the turn
    ends" and "after the three prompts you queued earlier" are very different
    things, and only the first is what /btw means.

    The idle path already returns the /btw text ahead of the queue.  A /btw
    typed three seconds earlier, while the turn was still running, is the same
    gesture and must not be demoted for it.
    """
    br, _sent = _bridge()
    br.state.queued_prompts.extend(["queued first", "queued second"])

    async def go():
        br.event_queue.put_nowait(("btw", "quick side question"))
        return await asyncio.wait_for(
            br._between_turns("some reply", interrupted=False), timeout=2)

    prompt = asyncio.run(go())

    assert prompt == "quick side question",         "/btw waited behind the prompts it is supposed to jump"
    assert list(br.state.queued_prompts) == ["queued first", "queued second"]


def test_btw_during_a_turn_does_not_displace_an_earlier_btw():
    """Two asides in one turn stay in the order they were typed: appendleft
    per item would reverse them."""
    br, _sent = _bridge()

    async def go():
        br.event_queue.put_nowait(("btw", "first aside"))
        br.event_queue.put_nowait(("btw", "second aside"))
        return await asyncio.wait_for(
            br._between_turns("reply", interrupted=False), timeout=2)

    prompt = asyncio.run(go())

    assert (prompt, list(br.state.queued_prompts)) ==         ("first aside", ["second aside"])


def test_btw_during_a_turn_is_still_only_a_main_context_prompt():
    """Pin the gap rather than let the help text keep promising past it: /btw
    is documented as a "side question in separate context", and what actually
    happens is an ordinary prompt in the main conversation.  A turn owns the
    one CLI, so a real separate context needs a second one.  See
    known-issues.md."""
    import inspect
    src = inspect.getsource(SDKBridge._between_turns)

    assert "queued_prompts.appendleft(text)" in src, (
        "if /btw ever grows a real separate context, this test and the "
        "known-issues entry it points at should both be revisited"
    )


# --------------------------------------------------------------------------
# The wakeup path (ghost turn / queue-edit-done pokes)
# --------------------------------------------------------------------------

def test_interrupt_while_parked_drains_the_queue():
    """Ctrl-C in bg-wait, with a prompt queued during a ghost turn.

    ``turn_active`` is set only by ``run_turn``, so while a background task
    streams output the worker is parked in ``_await_next_prompt`` even though
    ``state.busy`` is True and server.py is routing typed prompts into
    ``queued_prompts``.  ``_between_turns`` never runs on that path, so the
    interrupt has to poke the parked worker itself.
    """
    br, _sent = _bridge()
    br.state.background_tasks["t1"] = {"name": "long build"}
    br.state.queued_prompts.append("never mind, do this instead")

    async def go():
        task = asyncio.create_task(br._await_next_prompt())
        await asyncio.sleep(0)
        assert not br.turn_active.is_set(), "precondition: no live turn"
        await br.interrupt()
        return await asyncio.wait_for(task, timeout=2)

    assert asyncio.run(go()) == "never mind, do this instead"
    assert list(br.state.queued_prompts) == []


def test_interrupt_while_parked_with_an_empty_queue_stays_parked():
    """The poke must not invent a turn out of nothing."""
    br, _sent = _bridge()

    async def go():
        task = asyncio.create_task(br._await_next_prompt())
        await asyncio.sleep(0)
        await br.interrupt()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done(), "interrupt auto-resumed an idle session"
        br.stop_event.set()
        br.event_queue.put_nowait(("wakeup", "test"))
        return await asyncio.wait_for(task, timeout=2)

    assert asyncio.run(go()) is None


def test_interrupt_during_a_live_turn_uses_the_sentinel_not_the_wakeup():
    """The two pokes are mutually exclusive — a live turn must not get both."""
    br, _sent = _bridge()
    br.turn_active.set()

    asyncio.run(br.interrupt())

    assert br.turn_msg_queue.qsize() == 1
    assert br.event_queue.qsize() == 0, \
        "a live turn got a stray wakeup that would outlive the interrupt"


# --------------------------------------------------------------------------
# The interrupt wind-down must not be inherited by the next turn
# --------------------------------------------------------------------------

def test_interrupting_a_ghost_turn_holds_the_next_turn_until_it_winds_down():
    """From a report: "i had a queued prompt and then ctrl+c'd the turn and
    then it sent the queued prompt (or at least showed it as coming from me)
    but it didn't start working again, it stayed idle".

    Interrupting a ghost turn pokes the parked worker, which drains and starts
    a turn within a millisecond or two — while the CLI's answer to our
    ``client.interrupt()`` is still in flight.  It landed ~40 ms later, after
    ``turn_active`` was set, so the *new* turn consumed the *old* stream's
    terminating ResultMessage and ended 40 ms after starting.
    """
    br, _sent = _bridge()
    br.client = _FakeClient()
    br.state.busy = True                    # a ghost turn is streaming
    assert not br.turn_active.is_set()      # …with no run_turn consuming it

    asyncio.run(br.interrupt())

    assert not br._interrupt_settled.is_set(), (
        "nothing is holding the next turn back — it will race the CLI's "
        "wind-down and inherit its ResultMessage")


def test_interrupting_an_idle_session_does_not_hold_anything_back():
    """No stream, no terminator — waiting for one would stall every later turn
    for the full timeout."""
    br, _sent = _bridge()
    br.state.busy = False

    asyncio.run(br.interrupt())

    assert br._interrupt_settled.is_set()


def test_interrupting_a_live_turn_does_not_hold_anything_back():
    """run_turn is right there to absorb its own terminator."""
    br, _sent = _bridge()
    br.state.busy = True
    br.turn_active.set()

    asyncio.run(br.interrupt())

    assert br._interrupt_settled.is_set()


def test_the_wind_down_result_releases_the_next_turn():
    """The terminator reaching the between-turns handler is what unblocks."""
    br, _sent = _bridge()
    br.client = _FakeClient()
    br.state.busy = True
    asyncio.run(br.interrupt())
    assert not br._interrupt_settled.is_set()

    asyncio.run(br._handle_async_message(_result_message("error_during_execution")))

    assert br._interrupt_settled.is_set()


def test_a_starting_turn_waits_for_the_wind_down():
    """The wait happens before turn_active is set, so the terminator still
    reaches the between-turns handler rather than the new turn."""
    br, _sent = _bridge()
    br.client = _FakeClient()
    br.state.busy = True

    async def go():
        await br.interrupt()
        task = asyncio.create_task(br._await_interrupt_settled(timeout=2))
        await asyncio.sleep(0)
        assert not task.done(), "turn started while the interrupt was in flight"
        assert not br.turn_active.is_set(), (
            "turn_active was set before the wind-down landed — the dispatcher "
            "would route the terminator into the new turn")
        await br._handle_async_message(_result_message("error_during_execution"))
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(go())


def test_the_wait_gives_up_rather_than_stalling_forever():
    """A CLI that never answers must not wedge the session."""
    br, _sent = _bridge()
    br.client = _FakeClient()
    br.state.busy = True

    async def go():
        await br.interrupt()
        await asyncio.wait_for(br._await_interrupt_settled(timeout=0.05),
                               timeout=2)

    asyncio.run(go())
    assert br._interrupt_settled.is_set(), \
        "a timed-out wait must not leave every future turn waiting too"


def test_wakeup_drain_republishes_the_queue():
    br, sent = _bridge()
    br.state.queued_prompts.extend(["queued while ghosting", "and another"])

    async def go():
        task = asyncio.create_task(br._await_next_prompt())
        await asyncio.sleep(0)
        br.event_queue.put_nowait(("wakeup", "queue-edit-done"))
        return await asyncio.wait_for(task, timeout=2)

    assert asyncio.run(go()) == "queued while ghosting"
    assert _latest_queue(sent) == ["and another"]


# --------------------------------------------------------------------------
# _wait_for_reconnect_trigger — the DuplicateSessionError park-alive loop
#
# When connect is refused because the session is already open elsewhere, the
# worker must NOT exit (a dead worker strands every later prompt on an unread
# event_queue, which the tab misreports as "worker may be wedged").  It parks
# here until an explicit reconnect trigger, so /connect can recover once the
# other process closes.
# --------------------------------------------------------------------------

def test_reconnect_trigger_returns_false_on_connect():
    """/connect (kind 'connect') means re-attempt — return False (don't stop)."""
    br, _sent = _bridge()
    br.event_queue.put_nowait(("connect", ""))
    assert asyncio.run(br._wait_for_reconnect_trigger()) is False


def test_reconnect_trigger_returns_true_on_quit():
    """quit means stop — return True, worker exits cleanly."""
    br, _sent = _bridge()
    br.event_queue.put_nowait(("quit", ""))
    assert asyncio.run(br._wait_for_reconnect_trigger()) is True


def test_reconnect_trigger_already_stopped_returns_true_without_blocking():
    """stop_event already set: return True at once rather than block on get()."""
    br, _sent = _bridge()
    br.stop_event.set()
    assert asyncio.run(
        asyncio.wait_for(br._wait_for_reconnect_trigger(), timeout=2)) is True


def test_reconnect_trigger_requeues_a_message_and_retries():
    """A message that slips through re-triggers connect AND is preserved, so it
    still runs once we're back up rather than being consumed and lost."""
    br, _sent = _bridge()
    br.event_queue.put_nowait(("message", "run this once reconnected"))
    assert asyncio.run(br._wait_for_reconnect_trigger()) is False
    assert br.event_queue.get_nowait() == ("message", "run this once reconnected")


def test_reconnect_trigger_ignores_wakeups_then_acts_on_connect():
    """Stray pokes (wakeups) must not end the park; only a real trigger does."""
    br, _sent = _bridge()

    async def go():
        task = asyncio.create_task(br._wait_for_reconnect_trigger())
        await asyncio.sleep(0)
        br.event_queue.put_nowait(("wakeup", "bg-all-done"))
        await asyncio.sleep(0)
        assert not task.done(), "a wakeup wrongly ended the reconnect park"
        br.event_queue.put_nowait(("connect", ""))
        return await asyncio.wait_for(task, timeout=2)

    assert asyncio.run(go()) is False
