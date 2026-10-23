"""A finished background task's turn begins when the task ends.

Reported 2026-10-23: "sometimes when a background task completes and it sends
a queued prompt, the background task eats up the prompt and the model never
sees it."

The CLI runs a turn for a background task that finishes between turns, and
streams nothing at its start: the first thing on the stream is the model's
first output, a median 3.5 s later and up to 82 s (2868 such turns, measured).
The SDK reports the task's end within milliseconds, though.  The ghost turn
began only at that first output, so for those seconds the session looked idle,
and whatever waits for an idle session went through the gap into the CLI's
turn: the queued prompt the task's end released (``bg-all-done``), a loop's
wakeup, a /model switch deferred until the tasks were done.  The CLI folded the
prompt into the task's turn, or ran it after, and the bridge's turn ended on
the task's result: logged twelve times ("run_turn ended on a turn that was not
ours: origin=task-notification"), the 2026-10-20 one 0.56 s after the task.

Now the SDK's ``task_notification`` between turns begins the ghost turn.

Offline: a real ``SDKBridge`` with a list broadcaster; no SDK subprocess.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock  # noqa: E402
from claude_agent_sdk.types import SystemMessage, TaskNotificationMessage  # noqa: E402

import sdk_bridge                                     # noqa: E402
from config import parse_args                         # noqa: E402
from sdk_bridge import SDKBridge                      # noqa: E402
from state import init_state_from_config              # noqa: E402
from tool_manager import register_bg_task             # noqa: E402

SETTLE = 0.2


@pytest.fixture(autouse=True)
def short_waits(monkeypatch):
    monkeypatch.setattr(sdk_bridge, "TURN_END_SETTLE_S", SETTLE)
    monkeypatch.setattr(sdk_bridge, "BG_DONE_BELL_GRACE", 0.3)


def _bridge(*tasks: str):
    """A bridge between turns, with background tasks *tasks* running."""
    cfg = parse_args([])
    state = init_state_from_config(cfg)
    state.bell_events = {"turn-done", "bg-done"}
    sent: list[dict] = []

    async def bcast(msg: dict) -> None:
        sent.append(msg)

    for t in tasks:
        register_bg_task(state, t, "local_bash", f"task {t}", tool_use_id=f"tu-{t}")
    return SDKBridge(config=cfg, state=state, broadcaster=bcast), state, sent


def _updated(task_id: str, status: str = "completed") -> SystemMessage:
    """The SDK's patch saying a task ended: before the notification."""
    return SystemMessage(subtype="task_updated",
                         data={"task_id": task_id, "patch": {"status": status}})


def _notified(task_id: str) -> TaskNotificationMessage:
    """The SDK's word that the CLI queued the task's notification for the model."""
    return TaskNotificationMessage(
        subtype="task_notification", data={}, task_id=task_id,
        status="completed", output_file="", summary="done", uuid="u",
        session_id="sid-1")


def _output(text: str = "The build passed.") -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="m")


def _result(origin=None) -> ResultMessage:
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                         is_error=False, num_turns=1, session_id="sid-1",
                         origin=origin)


async def _task_ends(br, task_id: str) -> None:
    """A task ends between turns, as the SDK reports it: the patch, then the
    notification a few milliseconds later."""
    await br._handle_async_message(_updated(task_id))
    await asyncio.sleep(0.005)
    await br._handle_async_message(_notified(task_id))


def _bells(sent):
    return [m["event"] for m in sent if m.get("type") == "bell"]


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------

async def _queued_during_a_turn(br, state, text="my question"):
    """A prompt typed while the CLI was busy, so queued; then that turn ends,
    and the parked worker, poked, waits out the settle before sending it.
    Returns the parked worker."""
    await br._begin_ghost_turn_if_needed()
    state.queued_prompts.append(text)
    waiter = asyncio.create_task(br._await_next_prompt())
    await asyncio.sleep(0.02)
    await br._end_ghost_turn("success")
    return waiter


def test_a_queued_prompt_waits_out_the_turn_the_task_starts():
    """The report, as logged 2026-10-20: a turn ends, the queued prompt waits
    out the settle, the task ends inside it, and the prompt went out 0.56 s
    later into the task's turn."""
    br, state, _sent = _bridge("b1")

    async def go():
        waiter = await _queued_during_a_turn(br, state)
        await asyncio.sleep(SETTLE / 2)
        await _task_ends(br, "b1")
        await asyncio.sleep(SETTLE + 0.2)      # the CLI's turn shows nothing yet
        early = waiter.done()
        queued = list(state.queued_prompts)
        await br._handle_async_message(_output())
        await br._handle_async_message(_result({"kind": "task-notification"}))
        return early, queued, await asyncio.wait_for(waiter, SETTLE + 1)

    early, queued, prompt = asyncio.run(go())

    assert not early, "the prompt went into the task's turn"
    assert queued == ["my question"]
    assert prompt == "my question", "it never went out after the task's turn"


def test_one_typed_while_the_tasks_turn_is_quiet_is_queued_and_waits():
    """server.py queues a typed prompt while state.busy; the worker, poked,
    declines it until the turn is over."""
    br, state, _sent = _bridge("b1")

    async def go():
        waiter = asyncio.create_task(br._await_next_prompt())
        await asyncio.sleep(0.02)
        await _task_ends(br, "b1")
        assert state.busy, "server.py would have sent it straight to the CLI"
        state.queued_prompts.append("typed now")
        await asyncio.sleep(SETTLE + 0.1)
        early = waiter.done()
        await br._handle_async_message(_output())
        await br._handle_async_message(_result())
        return early, await asyncio.wait_for(waiter, SETTLE + 1)

    early, prompt = asyncio.run(go())

    assert not early and prompt == "typed now"


def test_the_session_is_busy_from_the_tasks_end():
    """Not from the model's first output, seconds later: so a typed prompt is
    queued (server.py routes by state.busy) and the status says working."""
    br, state, sent = _bridge("b1")

    async def go():
        await br._handle_async_message(_notified("b1"))

    asyncio.run(go())

    assert state.busy
    assert not br._ghost_settled.is_set(), "a run_turn would not wait for it"
    assert any(m.get("type") == "status_update" for m in sent)


def test_its_output_and_its_end_are_the_same_turn():
    """One ghost turn, from the task's end to its result -- not a second one
    begun by the output."""
    br, state, sent = _bridge("b1")

    async def go():
        await _task_ends(br, "b1")
        turns = state.turns
        await br._handle_async_message(_output())
        await br._handle_async_message(_result())
        return turns

    before = asyncio.run(go())

    assert not state.busy
    assert state.turns == before + 1
    assert [m["type"] for m in sent].count("turn_end") == 1


def test_a_loop_wakeup_due_then_waits():
    """The 2026-10-17 collisions were loop wakeups."""
    br, state, _sent = _bridge("b1")

    async def go():
        await _task_ends(br, "b1")
        await br._wakeup_timer(0.0, "Autonomous-loop wakeup")
        return [e for e in _drain(br.event_queue) if e[0] == "message"]

    sent_now = asyncio.run(go())

    assert sent_now == [], "the wakeup went into the task's turn"
    assert br._wakeup_defers == 1
    br._cancel_wakeup()


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_a_deferred_model_switch_waits_for_the_tasks_turn(monkeypatch):
    """A /model switch waits for the background tasks; the last one's end
    used to apply it at once, and its reconnect cut the task's turn off."""
    br, state, _sent = _bridge("b1")
    reconnects = []

    async def reconnect():
        reconnects.append(state.busy)
    monkeypatch.setattr(br, "reconnect", reconnect)
    br._deferred_reconnect = "model → claude-x"

    async def go():
        waiter = asyncio.create_task(br._await_next_prompt())
        await asyncio.sleep(0.02)
        await _task_ends(br, "b1")
        await asyncio.sleep(SETTLE + 0.1)
        during = list(reconnects)
        await br._handle_async_message(_output())
        await br._handle_async_message(_result())
        await asyncio.sleep(SETTLE + 0.15)       # it waits out the settle too
        waiter.cancel()
        return during

    during = asyncio.run(go())

    assert during == [], "it reconnected under the task's turn"
    assert reconnects == [False], "it was never applied after the turn"


def test_it_is_not_applied_under_any_turn_the_cli_started(monkeypatch):
    """The guard itself: a ghost turn holds no turn_active."""
    br, state, _sent = _bridge()
    called = []

    async def reconnect():
        called.append(1)
    monkeypatch.setattr(br, "reconnect", reconnect)
    br._deferred_reconnect = "effort → high"
    state.busy = True

    assert asyncio.run(br._flush_deferred_reconnect()) is False
    assert called == []


# --------------------------------------------------------------------------
# What it does not do
# --------------------------------------------------------------------------

def test_during_a_turn_it_begins_nothing():
    """The CLI hands the notification to the running turn."""
    br, state, _sent = _bridge("b1")

    async def go():
        br.turn_active.set()
        await br._handle_system_message(_notified("b1"), during_turn=True)

    asyncio.run(go())

    assert not state.busy
    assert br._task_turn_backstop is None
    assert not br._turn_for_task


def test_nor_during_a_ghost_turn():
    br, state, _sent = _bridge("b1")

    async def go():
        await br._begin_ghost_turn_if_needed()
        await br._handle_async_message(_notified("b1"))

    asyncio.run(go())

    assert br._task_turn_backstop is None
    assert not br._turn_for_task


def test_a_task_whose_end_starts_no_turn_holds_nothing_up():
    """Only the notification begins the turn: a task that ends without one
    (its patch alone) delays a queued prompt by the settle, no more."""
    br, state, _sent = _bridge("b1")

    async def go():
        waiter = await _queued_during_a_turn(br, state)
        await asyncio.sleep(SETTLE / 2)
        await br._handle_async_message(_updated("b1", "stopped"))
        return await asyncio.wait_for(waiter, 2 * SETTLE + 1)

    assert asyncio.run(go()) == "my question"
    assert not state.busy


def test_the_settle_runs_from_the_patch_that_arrives_first():
    """The patch can land just before the settle ends, and the notification
    that begins the turn just after: the settle must be extended by the
    patch, or the prompt goes out between the two."""
    br, state, _sent = _bridge("b1")

    async def go():
        waiter = await _queued_during_a_turn(br, state)
        await asyncio.sleep(SETTLE * 0.8)
        await br._handle_async_message(_updated("b1"))
        await asyncio.sleep(SETTLE * 0.4)         # past the first settle's end
        await br._handle_async_message(_notified("b1"))
        await asyncio.sleep(SETTLE)
        early = waiter.done()
        waiter.cancel()
        return early

    assert not asyncio.run(go()), "the prompt went out between the two"


# --------------------------------------------------------------------------
# A turn that never shows
# --------------------------------------------------------------------------

def test_a_tasks_turn_that_never_shows_is_let_go(monkeypatch):
    monkeypatch.setattr(sdk_bridge, "TASK_TURN_SHOW_TIMEOUT_S", 0.2)
    br, state, _sent = _bridge("b1")

    async def go():
        waiter = await _queued_during_a_turn(br, state)
        await _task_ends(br, "b1")
        return await asyncio.wait_for(waiter, 0.2 + 2 * SETTLE + 1)

    assert asyncio.run(go()) == "my question"
    assert not state.busy and br._ghost_settled.is_set()
    assert not br._turn_for_task


def test_one_that_shows_is_kept(monkeypatch):
    """Its first output disarms the backstop: the turn is real, however long."""
    monkeypatch.setattr(sdk_bridge, "TASK_TURN_SHOW_TIMEOUT_S", 0.2)
    br, state, _sent = _bridge("b1")

    async def go():
        await _task_ends(br, "b1")
        await asyncio.sleep(0.05)
        await br._handle_async_message(_output())
        await asyncio.sleep(0.3)
        return state.busy

    assert asyncio.run(go()), "a turn under way was let go"


def test_a_reconnect_lets_go_of_it_too():
    br, state, _sent = _bridge("b1")

    async def go():
        await _task_ends(br, "b1")
        br._abandon_ghost_turn("its CLI is being replaced")

    asyncio.run(go())

    assert not state.busy
    assert br._task_turn_backstop is None and not br._turn_for_task


# --------------------------------------------------------------------------
# The bg-done bell
# --------------------------------------------------------------------------

def test_bg_done_rings_when_the_tasks_turn_is_over():
    """The model has dealt with the task; the session waits for the user."""
    br, state, sent = _bridge("b1")

    async def go():
        await _task_ends(br, "b1")
        await asyncio.sleep(0.4)                  # past the old grace
        rung_early = _bells(sent)
        await br._handle_async_message(_output())
        await br._handle_async_message(_result())
        return rung_early

    rung_early = asyncio.run(go())

    assert rung_early == [], "it rang mid-work, before the model had answered"
    assert _bells(sent) == ["bg-done"]


def test_not_when_the_turn_left_more_work_running():
    """A task started in that turn will ring when it ends."""
    br, state, sent = _bridge("b1")

    async def go():
        await _task_ends(br, "b1")
        await br._handle_async_message(_output())
        register_bg_task(state, "b2", "local_bash", "next", tool_use_id="tu-b2")
        await br._handle_async_message(_result())

    asyncio.run(go())

    assert _bells(sent) == []


def test_not_when_the_turn_was_interrupted():
    br, state, sent = _bridge("b1")

    async def go():
        await _task_ends(br, "b1")
        await br._handle_async_message(_output())
        await br._end_ghost_turn("error_during_execution", interrupted=True)

    asyncio.run(go())

    assert _bells(sent) == []


def test_nor_for_a_turn_a_task_did_not_start():
    """A peer's message, a resumed turn: no task finished."""
    br, state, sent = _bridge()

    async def go():
        await br._handle_async_message(_output())
        await br._handle_async_message(_result())

    asyncio.run(go())

    assert _bells(sent) == []
