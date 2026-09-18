"""A prompt sent while a ghost turn is streaming must wait for it.

Reported 2026-09-16: "I sent this prompt to the Good Photons session [...] it
showed it sent as a 'You:' message, but it just kept working and never answered
me."

The log has the whole thing::

    08:55:01  run_turn exit: normal (turns=62)          <- real turn ends
    08:56:07  bg task bheyhqc8u completed
    08:56:17  ghost turn begin: SDK streaming without active run_turn
    08:59:14  run_turn start: ta.is_set=False queue_size=0 state.busy=True
              prompt='You said the 5x map bought 25%...'
    09:18:33  run_turn ResultMessage: subtype=success elapsed=1159.1s
    09:18:33  run_turn exit: normal (turns=63)

A background task woke the model, which started a **ghost turn** -- a stream
with no ``run_turn`` consuming it. Ghost turns set ``state.busy`` but not
``turn_active``, so the worker was parked in ``_await_next_prompt``, looking
idle while the CLI was three minutes into an answer.

``server.py`` routed the prompt correctly: ``state.busy`` was True, so it went
to ``queued_prompts``. Then the queue poke woke the parked worker, which popped
it -- echoing it as "You:" -- and started a turn on top of the live stream. That
turn then consumed the *ghost* turn's terminating ResultMessage (note the
1159 s elapsed, which is the ghost turn's duration, not this turn's) and exited
"normally" having never answered anything. The prompt had been handed to a busy
CLI, which replied much later as yet another ghost turn.

``_await_interrupt_settled`` already guarded the identical race when an
*interrupt* caused it. Nothing guarded it when a background task did.

The park point's own comment carried the bad assumption: "a queued user prompt
is always sendable once we're back to waiting". True for a ghost turn that
streams briefly; false for one that runs nineteen minutes.

Offline: a real ``SDKBridge`` with a list broadcaster; no SDK subprocess.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ResultMessage,
    TextBlock,
)

from config import parse_args                        # noqa: E402
from sdk_bridge import SDKBridge                      # noqa: E402
from state import init_state_from_config              # noqa: E402


def _bridge():
    cfg = parse_args([])
    state = init_state_from_config(cfg)
    sent: list[dict] = []

    async def bcast(msg: dict) -> None:
        sent.append(msg)

    return SDKBridge(config=cfg, state=state, broadcaster=bcast), state, sent


def _assistant() -> AssistantMessage:
    return AssistantMessage(content=[TextBlock("working...")], model="claude-x")


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success", duration_ms=1159100, duration_api_ms=1,
        is_error=False, num_turns=1, session_id="8d532b0a",
    )


async def _ghost_begin(br):
    await br._begin_ghost_turn_if_needed()


async def _ghost_end(br):
    await br._end_ghost_turn("success")


# --------------------------------------------------------------------------
# The park point must decline while a ghost turn owns the stream
# --------------------------------------------------------------------------

def test_a_queued_prompt_is_not_popped_during_a_ghost_turn():
    """The reported bug. Popping here echoes "You:" and starts a turn that
    ends on the ghost turn's result with the prompt unanswered."""
    br, state, _sent = _bridge()

    async def go():
        await _ghost_begin(br)
        state.queued_prompts.append("You said the 5x map bought 25%...")
        # The queue poke that _poke_for_queued_prompt puts on the event queue.
        br.event_queue.put_nowait(("wakeup", br.QUEUE_POKE))
        return await asyncio.wait_for(br._await_next_prompt(), 0.5)

    with_timeout = False
    try:
        asyncio.run(go())
    except asyncio.TimeoutError:
        with_timeout = True

    assert with_timeout, (
        "the worker popped a queued prompt while a ghost turn was streaming; "
        "that turn would consume the ghost turn's result and answer nothing"
    )


def test_the_prompt_stays_in_the_queue():
    """Declining has to leave it *visible*. The old behaviour echoed it as a
    "You:" message and dropped it from the queue, so the user could see neither
    an answer nor a pending prompt."""
    br, state, _sent = _bridge()

    async def go():
        await _ghost_begin(br)
        state.queued_prompts.append("my prompt")
        br.event_queue.put_nowait(("wakeup", br.QUEUE_POKE))
        try:
            await asyncio.wait_for(br._await_next_prompt(), 0.3)
        except asyncio.TimeoutError:
            pass

    asyncio.run(go())

    assert list(state.queued_prompts) == ["my prompt"]


def test_no_you_echo_while_it_waits():
    """The "You:" echo is what made this look like a delivered message."""
    br, state, sent = _bridge()

    async def go():
        await _ghost_begin(br)
        state.queued_prompts.append("my prompt")
        br.event_queue.put_nowait(("wakeup", br.QUEUE_POKE))
        try:
            await asyncio.wait_for(br._await_next_prompt(), 0.3)
        except asyncio.TimeoutError:
            pass

    asyncio.run(go())

    assert not [m for m in sent if m.get("type") == "user_message"]


def test_the_prompt_goes_out_when_the_ghost_turn_ends():
    """Declining is only safe because the end of the ghost turn re-pokes --
    the same contract ``connect()`` has for the ``state.connecting`` case."""
    br, state, _sent = _bridge()

    async def go():
        await _ghost_begin(br)
        state.queued_prompts.append("my prompt")
        br.event_queue.put_nowait(("wakeup", br.QUEUE_POKE))
        waiter = asyncio.create_task(br._await_next_prompt())
        await asyncio.sleep(0.05)
        await _ghost_end(br)
        return await asyncio.wait_for(waiter, 1.0)

    assert asyncio.run(go()) == "my prompt"


def test_an_idle_worker_still_pops_immediately():
    """The guard must not cost the normal case a single beat."""
    br, state, _sent = _bridge()

    async def go():
        state.queued_prompts.append("my prompt")
        br.event_queue.put_nowait(("wakeup", br.QUEUE_POKE))
        return await asyncio.wait_for(br._await_next_prompt(), 1.0)

    assert asyncio.run(go()) == "my prompt"


# --------------------------------------------------------------------------
# run_turn's own guard, for every other way in
# --------------------------------------------------------------------------

def test_run_turn_waits_for_a_streaming_ghost_turn():
    br, _state, _sent = _bridge()

    async def go():
        await _ghost_begin(br)
        try:
            await asyncio.wait_for(br._await_ghost_settled(), 0.3)
        except asyncio.TimeoutError:
            return "waited"
        return "started"

    assert asyncio.run(go()) == "waited"


def test_run_turn_does_not_wait_when_nothing_is_streaming():
    br, _state, _sent = _bridge()

    async def go():
        await asyncio.wait_for(br._await_ghost_settled(), 0.5)
        return "started"

    assert asyncio.run(go()) == "started"


def test_the_wait_is_long_enough_for_a_real_ghost_turn():
    """The reported one ran 1159 s. A guard that gave up after the usual few
    seconds would reproduce the bug on exactly the turns that matter."""
    sig = inspect.signature(SDKBridge._await_ghost_settled)

    assert sig.parameters["timeout"].default >= 1200


def test_a_timed_out_wait_does_not_wedge_every_later_turn():
    """A ghost turn whose terminator never lands must cost one slow turn, not
    the session."""
    br, _state, _sent = _bridge()

    async def go():
        await _ghost_begin(br)
        await br._await_ghost_settled(timeout=0.05)   # gives up
        return br._ghost_settled.is_set()

    assert asyncio.run(go()) is True


def test_run_turn_calls_the_guard_before_claiming_the_queue():
    """Order is the whole point: the wait has to happen before ``turn_active``
    is set, so the ghost turn's result still reaches the between-turns handler
    that closes it properly."""
    src = inspect.getsource(SDKBridge.run_turn)
    guard = src.index("_await_ghost_settled")
    drain = src.index("Drain stale messages")

    assert guard < drain


# --------------------------------------------------------------------------
# The event tracks the ghost turn honestly
# --------------------------------------------------------------------------

def test_the_event_clears_when_a_ghost_turn_begins():
    br, _state, _sent = _bridge()

    asyncio.run(_ghost_begin(br))

    assert not br._ghost_settled.is_set()


def test_the_event_sets_when_the_ghost_turn_ends():
    br, _state, _sent = _bridge()

    async def go():
        await _ghost_begin(br)
        await _ghost_end(br)

    asyncio.run(go())

    assert br._ghost_settled.is_set()


def test_a_real_turn_does_not_clear_it():
    """``turn_active`` already covers a real turn; conflating the two would
    make every ordinary prompt wait on an event nothing ever sets."""
    br, _state, _sent = _bridge()
    br.turn_active.set()

    asyncio.run(br._begin_ghost_turn_if_needed())

    assert br._ghost_settled.is_set()
