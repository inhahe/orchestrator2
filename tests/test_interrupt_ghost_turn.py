"""What Ctrl-C does, and — deliberately — what it does not do.

From a report: "often when i hit ctrl-c to interrupt a turn, it says it's
interrupted but then it keeps working and outputting text, and i have to hit
ctrl-c once or maybe twice more to get it to stop."

Two separate things were happening there.

1. "it says it's interrupted" — the server broadcast "Turn interrupted." the
   instant the Ctrl-C arrived, *before* the control request had even been sent,
   and on top of run_turn's own reporting.  That was a real bug; fixed in
   ``server._do_interrupt``.

2. "but then it keeps working" — not a bug, and not ours to suppress.  In the
   logged case (pid 6760, bridge=6990)::

       23:05:11,152  UserMessage during turn: '[Request interrupted by user for tool use]'
       23:05:11,153  run_turn exit: normal (turns=2)
       23:05:22,033  async AssistantMessage between turns: ... blocks=['ThinkingBlock']
       23:05:22,033  ghost turn begin: SDK streaming without active run_turn

   the session JSONL shows a background Bash task completing at 23:05:15 and
   enqueueing a <task-notification>, which the CLI fed to the model.  The CLI
   builds its abortController per dequeued command (print.ts:2133) and the SDK
   `interrupt` control request only aborts the *current* one — it never clears
   the queue.  Interactive Claude Code behaves the same way (print.ts:2010-2013
   calls this "matches TUI behavior").  The output is written to the session
   JSONL regardless, so discarding it would make the live view disagree with
   the history on reload.  These tests pin that we render it.

Offline: a real ``SDKBridge`` with a fake client and a list broadcaster; no SDK
subprocess is ever started.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    UserMessage,
)

from config import parse_args  # noqa: E402
from sdk_bridge import SDKBridge  # noqa: E402
from state import init_state_from_config  # noqa: E402


class FakeClient:
    """Only the control surface an interrupt touches."""

    def __init__(self) -> None:
        self.interrupts = 0

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def query(self, prompt: str) -> None:  # pragma: no cover - unused
        pass


def _bridge():
    cfg = parse_args([])
    state = init_state_from_config(cfg)
    sent: list[dict] = []

    async def bcast(msg: dict) -> None:
        sent.append(msg)

    br = SDKBridge(config=cfg, state=state, broadcaster=bcast)
    br.client = FakeClient()
    return br, state, sent


def _assistant(text: str = "still going") -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text)], model="claude-x")


def _tool_results() -> UserMessage:
    return UserMessage(content=[ToolResultBlock("toolu_1", "done", False)])


def _result(subtype: str = "error_during_execution") -> ResultMessage:
    return ResultMessage(
        subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id="sid-1",
    )


def _kinds(sent: list[dict]) -> list[str]:
    return [m.get("type") for m in sent]


# --------------------------------------------------------------------------
# Output after an interrupt is a background task's result — show it
# --------------------------------------------------------------------------

def test_output_after_an_interrupt_is_still_rendered():
    """The exact log sequence.  A guard that swallowed this would be hiding a
    completed background task whose result is already in the session JSONL —
    the live view would then disagree with the history on reload."""
    br, state, sent = _bridge()

    async def go():
        await br.interrupt()
        sent.clear()
        await br._handle_async_message(_assistant("bg task said: exit code 0"))

    asyncio.run(go())

    assert "assistant_text" in _kinds(sent), \
        "post-interrupt output was swallowed; it is a real bg-task result"
    assert state.busy is True, "the UI must show that the SDK is producing"


def test_the_interrupt_is_asserted_exactly_once():
    """No automatic re-assertion: a woken model is not a CLI ignoring us, and
    firing extra control requests at it would cancel work the user can see."""
    br, _state, _sent = _bridge()

    async def go():
        await br.interrupt()
        before = br.client.interrupts
        for _ in range(20):
            await br._handle_async_message(_assistant())
        return before, br.client.interrupts

    before, after = asyncio.run(go())
    assert before == 1, "the interrupt never reached the client"
    assert after == before, "20 blocks produced extra interrupt control requests"


def test_tool_results_after_an_interrupt_are_rendered_too():
    br, state, sent = _bridge()

    async def go():
        await br.interrupt()
        sent.clear()
        await br._handle_async_message(_tool_results())

    asyncio.run(go())
    assert "tool_result" in _kinds(sent)
    assert state.busy is True


# --------------------------------------------------------------------------
# Ghost-turn bookkeeping
# --------------------------------------------------------------------------

def test_unrelated_output_starts_a_ghost_turn():
    """No interrupt in play: a resumed mid-turn session must render."""
    br, state, sent = _bridge()

    async def go():
        await br._handle_async_message(_assistant("resumed work"))

    asyncio.run(go())
    assert "assistant_text" in _kinds(sent)
    assert state.busy is True


def test_a_ghost_turn_gets_closed_out_after_an_interrupt():
    """The ResultMessage that clears ``state.busy`` must land, or the UI is
    stranded on "working"."""
    br, state, sent = _bridge()

    async def go():
        await br._handle_async_message(_assistant("ghost work"))
        assert state.busy is True
        await br.interrupt()
        await br._handle_async_message(_assistant("more ghost work"))
        sent.clear()
        await br._handle_async_message(_result())

    asyncio.run(go())
    assert state.busy is False, "ghost turn left stuck on 'working'"
    assert "turn_end" in _kinds(sent)


def test_a_live_turn_owns_its_own_messages():
    """``turn_active`` set means run_turn is the consumer; the between-turns
    path must not render behind its back."""
    br, _state, sent = _bridge()

    async def go():
        br.turn_active.set()
        await br._handle_async_message(_assistant("turn content"))

    asyncio.run(go())
    assert "assistant_text" not in _kinds(sent)


# --------------------------------------------------------------------------
# The announcement — the half of the report that WAS a bug
# --------------------------------------------------------------------------

def test_server_does_not_claim_a_live_turn_is_already_interrupted():
    """"it says it's interrupted but then it keeps working" — the server used
    to broadcast "Turn interrupted." the instant Ctrl-C arrived, while the CLI
    was still streaming.  For a live turn, run_turn owns the announcement."""
    import server

    class Bridge:
        def __init__(self, live: bool) -> None:
            self.turn_active = asyncio.Event()
            if live:
                self.turn_active.set()
            self.interrupted = False

        async def interrupt(self) -> None:
            self.interrupted = True
            self.turn_active.clear()      # run_turn exits during the await

    def run(live: bool):
        sent: list[dict] = []

        async def bcast(msg: dict) -> None:
            sent.append(msg)

        br = Bridge(live)
        asyncio.run(server._do_interrupt(br, bcast))
        assert br.interrupted
        return sent

    assert run(live=True) == [], \
        "server announced the interrupt over run_turn's own reporting"
    idle = run(live=False)
    assert len(idle) == 1 and idle[0]["subtype"] == "interrupted", \
        "no feedback at all when no run_turn is there to give it"
    assert "interrupted." not in idle[0]["data"]["message"].lower(), \
        "still claiming the turn has already stopped"
