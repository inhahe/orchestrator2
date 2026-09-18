"""Two defects from one report: "it said what it had to say and then scheduled
a wakeup, but the client shows still 'working'".

The status was not stuck — it was *accurate*.  The turn genuinely never ended,
and the reason was invisible.  Reconstructed from the session JSONL and
orchestrator2.log:

1. The model emitted its closing text plus a ``ScheduleWakeup`` tool call.
   ScheduleWakeup is a *tool*, so that assistant message carries
   ``stop_reason: "tool_use"`` and the harness must make another API call — the
   turn cannot end there.
2. That next API step tripped an auto-compaction: ``durationMs: 140989``
   (141 seconds), 167,897 tokens in, 5,941 out, and *no output at all* for the
   duration.  The bar said "working" with nothing being printed, which is
   exactly what a wedged session looks like.
3. The post-compaction continuation prompt restarted the work, so the turn ran
   on — and orchestrator2's own wakeup then fired into that still-live turn and
   injected "Resume work now…" as a user message the model had to obey.

So: surface the compaction (the CLI already says so and we were dropping it on
the floor), and defer a wakeup that fires mid-turn rather than injecting it.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_agent_sdk import SystemMessage     # noqa: E402
from config import Config                      # noqa: E402
from sdk_bridge import SDKBridge               # noqa: E402
from state import State, state_to_status_dict  # noqa: E402


def _bridge() -> tuple[SDKBridge, list[dict]]:
    """A bridge with no SDK behind it, plus the list of what it broadcast."""
    sent: list[dict] = []

    async def broadcaster(msg):
        sent.append(msg)

    return SDKBridge(Config(), State(), broadcaster), sent


def _status(subtype_status: str | None) -> SystemMessage:
    """What the CLI sends around a compaction.

    ``services/compact/compact.ts`` calls ``setSDKStatus('compacting')`` before
    the summarisation call and ``setSDKStatus(null)`` after it; both arrive here
    as ``{type: 'system', subtype: 'status', status: ...}``, which the SDK
    parser hands us as a SystemMessage with the payload in ``.data``.
    """
    return SystemMessage(
        subtype="status",
        data={"type": "system", "subtype": "status", "status": subtype_status},
    )


# --------------------------------------------------------------------------
# The CLI's status message -> state.cli_status
# --------------------------------------------------------------------------

def test_compacting_status_is_recorded_and_broadcast():
    br, sent = _bridge()

    asyncio.run(br._handle_system_message(_status("compacting"),
                                          during_turn=True))

    assert br.state.cli_status == "compacting"
    assert br.state.cli_status_started_at is not None, (
        "no start time — the label could not be timed, and 'how long has this "
        "been silent' is the only question that matters during a compaction")
    assert "status_update" in [m.get("type") for m in sent], (
        "state changed with no push: the bar would not update until the 2 s "
        "ticker happened to fire")


def test_clearing_status_releases_the_label():
    br, _sent = _bridge()

    asyncio.run(br._handle_system_message(_status("compacting"),
                                          during_turn=True))
    asyncio.run(br._handle_system_message(_status(None), during_turn=True))

    assert br.state.cli_status is None
    assert br.state.cli_status_started_at is None, (
        "a stale start time would date the *next* compaction from this one")


def test_a_repeat_status_does_not_restart_the_clock():
    """The CLI may resend the same status; a duplicate must be a no-op.

    Restamping ``cli_status_started_at`` on every repeat would peg the elapsed
    time near zero for the whole compaction — the timer would look broken in
    precisely the case it exists for.
    """
    br, sent = _bridge()

    asyncio.run(br._handle_system_message(_status("compacting"),
                                          during_turn=True))
    started = br.state.cli_status_started_at
    before = len(sent)
    asyncio.run(br._handle_system_message(_status("compacting"),
                                          during_turn=True))

    assert br.state.cli_status_started_at == started
    assert len(sent) == before, "re-broadcast an unchanged status"


def test_a_new_turn_clears_a_stranded_compacting_flag():
    """A compaction cannot span a turn boundary.

    If the CLI dies mid-compaction it never sends the clearing status, and
    without this the bar would read "compacting" forever — for the rest of the
    session, on a session that is otherwise perfectly healthy.  run_turn is the
    hard reset.

    Driven for real: with no SDK client attached, run_turn gets as far as
    ``self.client.query()`` and dies there — which is *past* the clearing, so
    the AttributeError is the fixture, not a failure.
    """
    br, _sent = _bridge()
    br.state.cli_status = "compacting"
    br.state.cli_status_started_at = 1.0

    with pytest.raises(AttributeError):
        asyncio.run(br.run_turn("a fresh prompt"))

    assert br.state.cli_status is None
    assert br.state.cli_status_started_at is None


# --------------------------------------------------------------------------
# state_to_status_dict — what the bar actually shows
# --------------------------------------------------------------------------

def test_compacting_outranks_working():
    """A compaction happens *inside* a turn, so ``busy`` is true at the same
    time.  "working" is the less informative of the two: it is "compacting"
    that explains why nothing is being printed."""
    st = State()
    st.busy = True
    st.turn_started_at = 0.0
    st.cli_status = "compacting"
    st.cli_status_started_at = None      # untimed -> bare label, easy to assert

    blob = state_to_status_dict(st, Config())

    assert blob["busy_class"] == "compacting"
    assert blob["busy_label"] == "compacting"


def test_compacting_label_is_timed():
    """During a silent stretch the only interesting question is how long it has
    been going on, so the label carries a clock."""
    st = State()
    st.busy = True
    st.cli_status = "compacting"
    # 141 s: the duration actually measured in the run that prompted all this.
    st.cli_status_started_at = time.monotonic() - 141

    blob = state_to_status_dict(st, Config())

    assert blob["busy_class"] == "compacting"
    assert blob["busy_label"].startswith("compacting (0:2:2"), (
        f"expected ~2m21s of elapsed time, got {blob['busy_label']!r}")


def test_connecting_still_outranks_compacting():
    """A stale ``cli_status`` must not mask a reconnect in progress."""
    st = State()
    st.connecting = True
    st.cli_status = "compacting"

    assert state_to_status_dict(st, Config())["busy_class"] == "connecting"


def test_no_status_leaves_working_alone():
    st = State()
    st.busy = True
    st.turn_started_at = None

    blob = state_to_status_dict(st, Config())

    assert blob["busy_class"] == "working"
    assert blob["busy_label"] == "working"


# --------------------------------------------------------------------------
# _wakeup_timer — firing into a live turn
# --------------------------------------------------------------------------

def test_wakeup_defers_when_a_turn_is_still_running():
    """The second half of the report.

    The loop is not stalled — a turn is running — so it needs no nudge, and
    injecting one would drop "Resume work now…" into a conversation that is
    already mid-thought, as a user message the model must then obey.  Defer
    rather than drop: a turn that ends without the model re-arming would
    otherwise strand the loop for good.
    """
    br, sent = _bridge()
    br.state.busy = True

    async def go():
        await br._wakeup_timer(0, "keep going")
        # Whatever it re-armed, don't leave it pending in the loop.
        armed = br._wakeup_task
        br._cancel_wakeup()
        if armed is not None:
            await asyncio.gather(armed, return_exceptions=True)
        return armed

    armed = asyncio.run(go())

    assert br.event_queue.empty(), (
        "the wakeup prompt was injected into a live turn")
    assert not any(m.get("type") == "injected_prompt" for m in sent), (
        "announced an injection that must not happen")
    assert armed is not None, (
        "deferred by dropping the timer — the loop is now stranded with no "
        "pending wakeup and a model that already made its ScheduleWakeup call")


def test_wakeup_injects_when_the_turn_has_ended():
    """The control: idle is exactly when a nudge is wanted."""
    br, _sent = _bridge()
    br.state.busy = False

    asyncio.run(br._wakeup_timer(0, "keep going"))

    assert not br.event_queue.empty(), "an idle loop was left un-nudged"
    assert br.event_queue.get_nowait() == ("message", "keep going")
    assert br._wakeup_task is None, "a fired timer is still marked pending"


def test_wakeup_is_dropped_once_shutdown_has_begun():
    br, _sent = _bridge()
    br.state.busy = False
    br.stop_event.set()

    asyncio.run(br._wakeup_timer(0, "keep going"))

    assert br.event_queue.empty(), "queued a turn during shutdown"


# --------------------------------------------------------------------------
# _cancel_wakeup — the self-cancel trap, defensively
# --------------------------------------------------------------------------

def test_cancel_wakeup_never_cancels_its_own_caller():
    """``_wakeup_timer`` re-arms itself, and ``_arm_wakeup`` begins by
    cancelling the pending timer — which at that moment *is* the caller.

    An unconditional ``.cancel()`` there would mark the running task for
    cancellation, so everything after the re-arm would silently never run.
    That exact shape — a task cancelling itself out of its own callback — cost
    this hub 19 live CLI subprocesses via ``server._cancel_idle_timer``, and it
    left no trace at all: ``CancelledError`` is a BaseException, so
    ``except Exception`` never sees it, and a task that ends *cancelled* raises
    no "exception was never retrieved" warning either.
    """
    br, _sent = _bridge()
    reached_the_end = []

    async def go():
        async def body():
            br._cancel_wakeup()          # cancels self, on the buggy version
            await asyncio.sleep(0)       # where the CancelledError would land
            reached_the_end.append(True)

        t = asyncio.create_task(body())
        br._wakeup_task = t              # the caller *is* the pending timer
        await asyncio.gather(t, return_exceptions=True)
        return t

    t = asyncio.run(go())

    assert not t.cancelled(), "_cancel_wakeup cancelled the running task"
    assert reached_the_end, (
        "the caller never got past its next await — a self-cancel silently "
        "truncated it")


def test_cancel_wakeup_still_cancels_a_different_task():
    """The guard must not turn the cancel into a no-op for the normal case."""
    br, _sent = _bridge()

    async def go():
        t = asyncio.create_task(asyncio.sleep(60))
        br._wakeup_task = t
        br._cancel_wakeup()
        await asyncio.gather(t, return_exceptions=True)
        return t

    t = asyncio.run(go())

    assert t.cancelled(), "a pending wakeup timer outlived _cancel_wakeup"
