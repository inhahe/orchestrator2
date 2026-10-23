"""A turn the CLI runs by itself is a turn from the moment the CLI says so.

Reported 2026-10-23: "sometimes when a background task completes and it sends
a queued prompt, the background task eats up the prompt and the model never
sees it."

The CLI runs a turn of its own for a finished background task, or a peer's
message, and streams nothing until the model's first output: seconds later (a
median of 3.5 s, up to 82 s, over 2868 such turns).  The hub took the session
for idle meanwhile, and what waits for an idle session -- the queued prompt
the task's end released, a typed one, a loop's wakeup, a deferred /model --
went into that turn; the hub's own turn then ended on the task's result.

The CLI says when it is working, though.  Measured against CLI 2.1.280, the
messages below are the sequences it sends: "running" 20-80 ms after a task's
end, an ``init`` at the start of every turn, and "idle" only once it has
nothing left to run -- none between back-to-back turns, one after an
interrupted turn's result.  Its word is what holds things back now.

Offline: a real ``SDKBridge`` with a list broadcaster; no SDK subprocess.
Messages go through the dispatcher's own routing (``_route``).
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
    """A bridge between turns, the CLI idle, background tasks *tasks* running."""
    cfg = parse_args([])
    state = init_state_from_config(cfg)
    state.bell_events = {"turn-done", "bg-done"}
    sent: list[dict] = []

    async def bcast(msg: dict) -> None:
        sent.append(msg)

    for t in tasks:
        register_bg_task(state, t, "local_bash", f"task {t}", tool_use_id=f"tu-{t}")
    br = SDKBridge(config=cfg, state=state, broadcaster=bcast)
    br._set_cli_state("idle")
    return br, state, sent


def _cli(word: str) -> SystemMessage:
    return SystemMessage(subtype="session_state_changed",
                         data={"type": "system", "subtype": "session_state_changed",
                               "state": word})


def _init() -> SystemMessage:
    return SystemMessage(subtype="init", data={"session_id": "sid-1"})


def _updated(task_id: str, status: str = "completed") -> SystemMessage:
    return SystemMessage(subtype="task_updated",
                         data={"task_id": task_id, "patch": {"status": status}})


def _notified(task_id: str) -> TaskNotificationMessage:
    return TaskNotificationMessage(
        subtype="task_notification", data={}, task_id=task_id,
        status="completed", output_file="", summary="done", uuid="u",
        session_id="sid-1")


def _output(text: str = "The build passed.") -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="m")


def _result(origin=None, subtype="success", reason=None) -> ResultMessage:
    r = ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1,
                      is_error=False, num_turns=1, session_id="sid-1",
                      origin=origin)
    if reason:
        r.terminal_reason = reason
    return r


TASK = {"kind": "task-notification"}


async def _send(br, *msgs, gap=0.005):
    for m in msgs:
        await br._route(m)
        await asyncio.sleep(gap)


async def _task_ends(br, task_id):
    """A task's end between turns, as the CLI sends it, up to the start of the
    turn it runs for it -- which shows nothing yet."""
    await _send(br, _updated(task_id), _notified(task_id), _cli("running"), _init())


async def _queued_during_a_turn(br, state, text="my question"):
    """A prompt typed while the CLI was busy, so queued; then that turn ends
    and the CLI is idle.  Returns the worker, poked and waiting out the settle."""
    await br._begin_ghost_turn_if_needed()
    br._set_cli_state("running")
    state.queued_prompts.append(text)
    waiter = asyncio.create_task(br._await_next_prompt())
    await asyncio.sleep(0.02)
    await _send(br, _output(), _result(), _cli("idle"))
    return waiter


def _bells(sent):
    return [m["event"] for m in sent if m.get("type") == "bell"]


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------

def test_a_queued_prompt_waits_for_the_turns_the_cli_runs_by_itself():
    """The report.  Two tasks end, and the CLI runs a turn for each, one
    straight after the other with no "idle" between; the prompt waits for
    both and then gets a turn of its own."""
    br, state, _sent = _bridge("b1", "b2")

    async def go():
        waiter = await _queued_during_a_turn(br, state)
        await asyncio.sleep(SETTLE / 2)
        await _task_ends(br, "b1")
        await _send(br, _updated("b2"), _notified("b2"))
        await asyncio.sleep(SETTLE + 0.1)            # the turn shows nothing yet
        early = [waiter.done()]
        await _send(br, _output(), _result(TASK), _init())   # b1's turn, then b2's
        await asyncio.sleep(SETTLE + 0.1)
        early.append(waiter.done())
        await _send(br, _output("Both done."), _result(TASK), _cli("idle"))
        return early, await asyncio.wait_for(waiter, SETTLE + 1)

    early, prompt = asyncio.run(go())

    assert early == [False, False], "the prompt went into one of the CLI's turns"
    assert prompt == "my question"


def test_the_session_is_working_from_the_clis_word():
    """Not from the model's first output, seconds later: so a typed prompt is
    queued (server.py routes by state.busy), and the status says working."""
    br, state, sent = _bridge("b1")

    asyncio.run(_task_ends(br, "b1"))

    assert state.busy
    assert not br._ghost_settled.is_set()
    assert any(m.get("type") == "status_update" for m in sent)


def test_its_turns_one_after_another_are_each_a_turn():
    br, state, sent = _bridge("b1", "b2")

    async def go():
        await _task_ends(br, "b1")
        await _send(br, _output(), _result(TASK), _init())
        between = state.busy
        await _send(br, _output(), _result(TASK), _cli("idle"))
        return between

    between = asyncio.run(go())

    assert between, "the second turn was not shown as working"
    assert not state.busy
    assert [m["type"] for m in sent].count("turn_end") == 2


def test_a_turn_of_ours_waits_while_the_cli_says_running():
    """Between one of the CLI's results and its next init, nothing is
    streaming, and only its "running" says a turn is coming."""
    br, state, _sent = _bridge()
    br._set_cli_state("running")

    async def go():
        waiting = asyncio.create_task(br._await_ghost_settled(timeout=5))
        await asyncio.sleep(0.1)
        early = waiting.done()
        await br._route(_cli("idle"))
        await asyncio.wait_for(waiting, 1)
        return early

    assert not asyncio.run(go()), "it started while the CLI was running"


def test_a_queued_prompt_waits_while_the_cli_says_running():
    """With nothing yet shown -- between one of its results and its next
    init, or just after "running" -- its word alone holds the prompt."""
    br, state, _sent = _bridge()

    async def go():
        br._set_cli_state("running")
        state.queued_prompts.append("my question")
        waiter = asyncio.create_task(br._await_next_prompt())
        br.event_queue.put_nowait(("wakeup", "bg-all-done"))
        await asyncio.sleep(SETTLE + 0.1)
        early = waiter.done()
        await br._route(_cli("idle"))
        return early, await asyncio.wait_for(waiter, SETTLE + 1)

    early, prompt = asyncio.run(go())

    assert not early, "the prompt went to a CLI that said it was running"
    assert prompt == "my question"


def test_a_loop_wakeup_due_then_waits():
    br, state, _sent = _bridge("b1")

    async def go():
        await _task_ends(br, "b1")
        await br._wakeup_timer(0.0, "Autonomous-loop wakeup")
        out = []
        while not br.event_queue.empty():
            out.append(br.event_queue.get_nowait())
        return [e for e in out if e[0] == "message"]

    assert asyncio.run(go()) == [], "the wakeup went into the task's turn"
    assert br._wakeup_defers == 1
    br._cancel_wakeup()


# --------------------------------------------------------------------------
# Reconnects
# --------------------------------------------------------------------------

def _reconnects(monkeypatch, br, state):
    done = []

    async def reconnect():
        done.append(state.busy)
    monkeypatch.setattr(br, "reconnect", reconnect)
    return done


def test_a_switch_deferred_for_the_tasks_waits_for_their_turn(monkeypatch):
    """The last task's end used to apply it at once, and the reconnect cut off
    the turn the CLI had started for that task."""
    br, state, _sent = _bridge("b1")
    done = _reconnects(monkeypatch, br, state)
    br._deferred_reconnect = "model → claude-x"

    async def go():
        waiter = asyncio.create_task(br._await_next_prompt())
        await asyncio.sleep(0.02)
        await _task_ends(br, "b1")
        await asyncio.sleep(SETTLE + 0.1)
        during = list(done)
        await _send(br, _output(), _result(TASK), _cli("idle"))
        await asyncio.sleep(SETTLE + 0.15)
        waiter.cancel()
        return during

    assert asyncio.run(go()) == [], "it reconnected under the task's turn"
    assert done == [False], "it was never applied after the turn"


def test_a_switch_typed_during_one_of_the_clis_turns_waits_for_it(monkeypatch):
    br, state, sent = _bridge()
    done = _reconnects(monkeypatch, br, state)

    async def go():
        await _send(br, _cli("running"), _init())
        now = await br._reconnect_or_defer("model → claude-x")
        waiter = asyncio.create_task(br._await_next_prompt())
        await asyncio.sleep(0.02)
        during = list(done)
        await _send(br, _output(), _result(TASK), _cli("idle"))
        await asyncio.sleep(SETTLE + 0.15)
        waiter.cancel()
        return now, during

    now, during = asyncio.run(go())

    assert now is False and during == [], "the turn was cut off"
    assert done == [False], "the switch was never made"
    assert any("takes effect when the turn" in (m.get("data") or {}).get("message", "")
               for m in sent), "the user was not told why it waits"


def test_one_typed_just_after_a_turn_of_ours_is_not_held_up(monkeypatch):
    """Our turn's "idle" is on its way: a moment tells it apart from a turn
    of the CLI's own."""
    br, state, _sent = _bridge()
    done = _reconnects(monkeypatch, br, state)
    br._set_cli_state("running")

    async def go():
        later = asyncio.get_running_loop().call_later(
            0.05, lambda: br._set_cli_state("idle"))
        now = await br._reconnect_or_defer("model → claude-x")
        later.cancel()
        return now

    assert asyncio.run(go()) is True
    assert done == [False]


def test_a_turn_begun_while_the_switch_is_announced_is_not_cut_off(monkeypatch):
    """The announcement awaits a broadcast, in which the CLI's word can
    arrive: the switch asks again before it reconnects."""
    br, state, _sent = _bridge()
    done = _reconnects(monkeypatch, br, state)
    br._deferred_reconnect = "model → claude-x"

    async def bcast(msg):
        if "Applying" in (msg.get("data") or {}).get("message", ""):
            await br._route(_cli("running"))
    monkeypatch.setattr(br, "broadcast", bcast)

    assert asyncio.run(br._flush_deferred_reconnect()) is False
    assert done == [] and br._deferred_reconnect is not None


def test_the_guard_holds_under_a_turn_the_cli_started(monkeypatch):
    br, state, _sent = _bridge()
    done = _reconnects(monkeypatch, br, state)
    br._deferred_reconnect = "effort → high"
    br._set_cli_state("running")

    assert asyncio.run(br._flush_deferred_reconnect()) is False
    assert done == []


# --------------------------------------------------------------------------
# What the CLI's word does not mean
# --------------------------------------------------------------------------

def test_a_notification_that_starts_no_turn_holds_nothing_up():
    """One the CLI does not run -- it says nothing -- costs a queued prompt
    the settle, no more."""
    br, state, _sent = _bridge("b1")

    async def go():
        waiter = await _queued_during_a_turn(br, state)
        await asyncio.sleep(SETTLE / 2)
        await _send(br, _updated("b1", "stopped"), _notified("b1"))
        return await asyncio.wait_for(waiter, 2 * SETTLE + 1)

    assert asyncio.run(go()) == "my question"
    assert not state.busy


def test_a_turn_that_shows_nothing_for_long_is_not_let_go():
    """A compaction, a long think, retries: as long as the CLI says it is
    running, it is."""
    br, state, _sent = _bridge("b1")

    async def go():
        await _task_ends(br, "b1")
        await asyncio.sleep(0.5)
        return state.busy

    assert asyncio.run(go())


def test_idle_without_a_result_ends_the_turn():
    br, state, _sent = _bridge("b1")

    async def go():
        await _task_ends(br, "b1")
        await _send(br, _cli("idle"))

    asyncio.run(go())

    assert not state.busy and br._ghost_settled.is_set()


def test_an_init_with_the_cli_idle_begins_nothing():
    br, state, _sent = _bridge()

    asyncio.run(_send(br, _init()))

    assert not state.busy


def test_its_word_is_kept_when_it_comes_into_our_turn():
    """The "idle" that comes with one of our results goes to the turn's queue,
    which stops being read at the result."""
    br, _state, _sent = _bridge()

    async def go():
        br.turn_active.set()
        await _send(br, _cli("running"), _init(), _output())
        during = (br._cli_state, br._cli_quiet.is_set())
        await _send(br, _result(), _cli("idle"))
        br.turn_active.clear()
        return during

    during = asyncio.run(go())

    assert during == ("running", False), f"the turn's 'running' was not noted: {during}"
    assert br._cli_state == "idle" and br._cli_quiet.is_set()


def test_a_stale_word_read_late_by_a_turn_does_not_count():
    """A turn reads its queue after the dispatcher noted what came later."""
    br, _state, _sent = _bridge()

    async def go():
        br.turn_active.set()
        await _send(br, _cli("running"), _cli("idle"))
        await br._handle_system_message(_cli("running"), during_turn=True)

    asyncio.run(go())

    assert br._cli_state == "idle"


# --------------------------------------------------------------------------
# The bg-done bell
# --------------------------------------------------------------------------

def test_bg_done_rings_once_the_cli_has_dealt_with_the_task():
    br, state, sent = _bridge("b1")

    async def go():
        await _task_ends(br, "b1")
        await asyncio.sleep(0.4)                  # past the old grace
        rung_early = _bells(sent)
        await _send(br, _output(), _result(TASK))
        rung_at_result = _bells(sent)
        await _send(br, _cli("idle"))
        return rung_early, rung_at_result

    rung_early, rung_at_result = asyncio.run(go())

    assert rung_early == [], "it rang mid-work, before the model had answered"
    assert rung_at_result == [], "it rang before the CLI was done"
    assert _bells(sent) == ["bg-done"]


def test_not_when_a_prompt_waits_to_go():
    """The session is about to work again, not waiting for the user."""
    br, state, sent = _bridge("b1")

    async def go():
        await _task_ends(br, "b1")
        state.queued_prompts.append("next thing")
        await _send(br, _output(), _result(TASK), _cli("idle"))

    asyncio.run(go())

    assert _bells(sent) == []


def test_not_when_the_turn_left_more_work_running():
    br, state, sent = _bridge("b1")

    async def go():
        await _task_ends(br, "b1")
        register_bg_task(state, "b2", "local_bash", "next", tool_use_id="tu-b2")
        await _send(br, _output(), _result(TASK), _cli("idle"))

    asyncio.run(go())

    assert _bells(sent) == []


def test_not_when_the_turn_was_interrupted():
    br, state, sent = _bridge("b1")

    async def go():
        await _task_ends(br, "b1")
        await _send(br, _output(),
                    _result(TASK, "error_during_execution", "aborted_streaming"),
                    _cli("idle"))

    asyncio.run(go())

    assert _bells(sent) == []


def test_nor_for_a_task_that_ended_while_the_session_worked():
    br, state, sent = _bridge("b1")

    async def go():
        await _send(br, _cli("running"), _init(), _output())
        await _send(br, _updated("b1"), _notified("b1"))
        await _send(br, _result(), _cli("idle"))

    asyncio.run(go())

    assert _bells(sent) == []


def test_nor_for_a_turn_no_task_started():
    """A peer's message, a resumed turn."""
    br, _state, sent = _bridge()

    asyncio.run(_send(br, _cli("running"), _init(), _output(), _result(), _cli("idle")))

    assert _bells(sent) == []
