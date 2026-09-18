"""A ``bg-done`` bell must outlive a moment's scrutiny before it rings.

From a report: *"i seem to still be hearing bells when no turn is starting or
stopping, even though i have --bell turn-done bg-done rate-hit"*.

The `--bell` filter was innocent — every ring in the log was a `turn-done` or a
`bg-done`, and the two `interrupt` rings were correctly suppressed. What the log
showed instead was a perfect correlation: **all nine** `bg-done` rings were
followed 2.3–5.0 s later by a `ghost turn begin` in the *same* session::

    22:49:06,679  bg task bb1hiiw91 (Benchmark boot test (background)) completed
    22:49:06,681  bell: bg-done rung session=2e281cb3-47e busy=False turns=7
    22:49:10,233  ghost turn begin: SDK streaming without active run_turn

That is the CLI's documented behaviour, not a fluke: a finished background task
becomes a ``<task-notification>`` fed to the model, which starts streaming
again. So the session was never parked — it was between tool calls, with
``run_turn`` already exited and ``state.busy`` False, which is precisely the
shape ``in_bg_wait()`` reads as "idle".

The bell's contract is *come and look, this is done and nothing else is
happening*. Ringing three seconds before the model picks the result up and
carries on inverts it, and it is the worst kind of false alarm: indistinguishable
by ear from a real one, so it trains the user to ignore all of them.

The fix defers the judgement rather than the bell's meaning: arm on completion,
re-ask at the deadline, and cancel outright the moment ``busy`` flips True.
These tests pin the four things that makes true — that a resumption silences it,
that a genuinely idle session still gets its bell, that a burst of completions is
one bell and not one per task, and that shutdown doesn't leave the timer running.
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
    sent: list[dict] = []

    async def broadcaster(msg):
        sent.append(msg)

    st = State()
    st.bell_events = {"turn-done", "bg-done", "rate-hit"}
    st.session_id = "sess-1234567890"
    return SDKBridge(Config(), st, broadcaster), sent


def _bells(sent: list[dict]) -> list[str]:
    return [m["event"] for m in sent if m.get("type") == "bell"]


# ---------------------------------------------------------------------------
# The reported failure
# ---------------------------------------------------------------------------

def test_a_resumption_inside_the_grace_withdraws_the_bell(monkeypatch):
    """The exact logged sequence: task completes, model resumes ~3 s later.

    ``_begin_ghost_turn_if_needed`` is the real resumption path — the SDK
    streaming with no ``run_turn`` to own it — so the test goes through it
    rather than setting ``busy`` by hand.
    """
    monkeypatch.setattr(sdk_bridge, "BG_DONE_BELL_GRACE", 0.20)

    async def go():
        br, sent = _bridge()
        br._arm_bg_done_bell()
        await asyncio.sleep(0.05)              # the 3 s gap, to scale
        await br._begin_ghost_turn_if_needed()
        await asyncio.sleep(0.30)              # well past the deadline
        assert _bells(sent) == []
        assert br.state.pending_bell is None

    asyncio.run(go())


def test_a_resumption_that_finishes_inside_the_grace_still_withdraws_it(monkeypatch):
    """A short ghost turn must not be papered over by the fire-time re-check.

    Ghost turns of 2.1 s have been logged, so a resumption can begin *and* end
    well inside the grace — leaving the session idle again at the deadline and
    ``in_bg_wait()`` true. Only the cancellation at the ``busy`` flip records
    that the model already dealt with this task, which is why the fix cancels
    rather than relying on re-asking.
    """
    monkeypatch.setattr(sdk_bridge, "BG_DONE_BELL_GRACE", 0.30)

    async def go():
        br, sent = _bridge()
        br._arm_bg_done_bell()
        await asyncio.sleep(0.05)
        await br._begin_ghost_turn_if_needed()
        await asyncio.sleep(0.05)
        br.state.busy = False                  # what _end_ghost_turn does
        await asyncio.sleep(0.40)
        assert _bells(sent) == []

    asyncio.run(go())


def test_a_real_turn_starting_also_withdraws_it(monkeypatch):
    """``run_turn`` is the other way ``busy`` goes True, and means the same thing.

    A ``bg-all-done`` wakeup can pop a queued prompt and start a genuine turn
    inside the grace; the user is no more "free to come and look" then than
    during a ghost turn.
    """
    monkeypatch.setattr(sdk_bridge, "BG_DONE_BELL_GRACE", 0.20)

    async def go():
        br, sent = _bridge()
        br._arm_bg_done_bell()
        await asyncio.sleep(0.05)
        br.state.busy = True                   # what run_turn does at its top
        br._cancel_bg_done_bell()
        await asyncio.sleep(0.30)
        assert _bells(sent) == []

    asyncio.run(go())


def test_a_session_that_stays_idle_still_gets_its_bell(monkeypatch):
    """The bell is deferred, not removed.

    This is the case it exists for — a long build finishes and the session has
    genuinely nothing left to do. Losing it would be a worse bug than the false
    alarm, because the user would be waiting on a notification that never comes.
    """
    monkeypatch.setattr(sdk_bridge, "BG_DONE_BELL_GRACE", 0.10)

    async def go():
        br, sent = _bridge()
        br._arm_bg_done_bell()
        await asyncio.sleep(0.25)
        assert _bells(sent) == ["bg-done"]

    asyncio.run(go())


def test_the_check_is_made_at_the_deadline_not_at_arm_time(monkeypatch):
    """A session busy when the timer fires gets no bell, however it got there.

    ``_cancel_bg_done_bell`` covers the two ``busy`` flips, but a reconnect
    (``connecting``) or a rate-limit rejection can land inside the grace too and
    neither is a moment to interrupt the user. The fire-time re-check is what
    makes the guarantee independent of remembering every such path.
    """
    monkeypatch.setattr(sdk_bridge, "BG_DONE_BELL_GRACE", 0.10)

    async def go():
        br, sent = _bridge()
        br._arm_bg_done_bell()
        br.state.connecting = True             # nothing calls the canceller
        await asyncio.sleep(0.25)
        assert _bells(sent) == []

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Burst behaviour
# ---------------------------------------------------------------------------

def test_a_burst_of_completions_rings_once(monkeypatch):
    """Five tasks finishing together is one event to a listener, not five.

    The logged sessions complete background tasks in clusters — six inside forty
    seconds was typical — so without this the deferral would only have changed
    *when* the pile of bells arrived.
    """
    monkeypatch.setattr(sdk_bridge, "BG_DONE_BELL_GRACE", 0.10)

    async def go():
        br, sent = _bridge()
        for _ in range(5):
            br._arm_bg_done_bell()
        await asyncio.sleep(0.25)
        assert _bells(sent) == ["bg-done"]

    asyncio.run(go())


def test_a_later_completion_can_arm_again(monkeypatch):
    """Once a ring has fired, the next task's completion is a fresh event.

    Coalescing must not latch: a session that rings, sits idle, then finishes
    another background task half an hour later still deserves to be announced.
    """
    monkeypatch.setattr(sdk_bridge, "BG_DONE_BELL_GRACE", 0.10)

    async def go():
        br, sent = _bridge()
        br._arm_bg_done_bell()
        await asyncio.sleep(0.25)
        br._arm_bg_done_bell()
        await asyncio.sleep(0.25)
        assert _bells(sent) == ["bg-done", "bg-done"]

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Lifetime
# ---------------------------------------------------------------------------

def test_shutdown_leaves_no_pending_timer(monkeypatch):
    """A torn-down bridge must not ring into a session that no longer exists.

    Idle runtimes are reaped while background work may still be settling, and a
    stray task holding a reference to a dead bridge's state is how the last
    process leak started.
    """
    monkeypatch.setattr(sdk_bridge, "BG_DONE_BELL_GRACE", 0.10)

    async def go():
        br, sent = _bridge()
        br._arm_bg_done_bell()
        task = br._bg_done_bell_task
        assert task is not None
        br._cancel_bg_done_bell()
        assert br._bg_done_bell_task is None
        await asyncio.sleep(0.25)
        assert task.cancelled()
        assert _bells(sent) == []

    asyncio.run(go())


def test_the_completion_path_defers_rather_than_ringing_inline(monkeypatch):
    """``_announce_bg_completion`` must arm the timer, not ring on the spot.

    The whole fix lives or dies at this call site: the helpers can be perfect
    and the bug survives if the completion handler still calls ``ring_bell``
    directly. Asserting on ``_bg_done_bell_task`` pins the wiring, and asserting
    nothing was broadcast pins that nothing slipped past it.
    """
    monkeypatch.setattr(sdk_bridge, "BG_DONE_BELL_GRACE", 5.0)

    async def go():
        br, sent = _bridge()
        st = br.state
        st.background_tasks["t1"] = {"seq": 1, "name": "Boot test", "status": "running"}
        await br._announce_bg_completion(
            task_id="t1", status="completed", summary=None, output=None,
            source="task_updated")
        assert br._bg_done_bell_task is not None
        assert _bells(sent) == []
        assert st.pending_bell is None
        br._cancel_bg_done_bell()

    asyncio.run(go())
