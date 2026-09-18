"""A prompt typed while a `/model` switch reconnects must still be sent.

From a report: "i changed the model, and while it was reconnecting to switch
models, i typed in a prompt, which it queued, and when it finished changing the
model, it sat there idle without sending the queued prompt."

The two halves of the deadlock:

1. ``server.py`` routes a typed message to ``state.queued_prompts`` (the visible
   queue panel) whenever ``state.busy or state.connecting``.  Every config
   command — ``/model``, ``/effort``, ``/thinking``, ``/connect``, ``/clear`` —
   reconnects the SDK, and ``connect()`` holds ``state.connecting`` True for the
   several seconds that takes.  So a prompt typed in that window is *always*
   queued rather than delivered to the bridge's event_queue.

2. ``_await_next_prompt`` applied the command, then went straight back to
   blocking on ``event_queue.get()``.  Nothing else pokes it, so the prompt sat
   in the panel forever while the status bar read idle.

The between-turns path never had the bug — ``_between_turns`` reconnects at the
top and drains ``queued_prompts`` further down — so the same keystrokes worked
or didn't purely on whether a turn happened to be ending at that moment.

Offline: a real ``SDKBridge`` with ``reconnect`` stubbed to move
``state.connecting`` exactly the way ``connect()`` does, and a stand-in for
server.py's routing rule.  No SDK subprocess.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import parse_args  # noqa: E402
from sdk_bridge import SDKBridge  # noqa: E402
from state import init_state_from_config  # noqa: E402


def _bridge():
    cfg = parse_args([])
    state = init_state_from_config(cfg)
    sent: list[dict] = []

    async def bcast(msg: dict) -> None:
        sent.append(msg)

    br = SDKBridge(config=cfg, state=state, broadcaster=bcast)

    reconnects: list[str] = []

    async def fake_reconnect() -> None:
        # Mirror connect(): connecting is True for the duration and cleared
        # before returning.  The awaits are what let a "user" task interleave.
        state.connecting = True
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        state.connecting = False
        reconnects.append(state.model or "auto")

    br.reconnect = fake_reconnect  # type: ignore[method-assign]
    return br, state, sent, reconnects


def _type_a_prompt(state, br, text: str) -> None:
    """Exactly server.py's routing rule for an inbound user message."""
    if state.busy or state.connecting:
        state.queued_prompts.append(text)
    else:
        br.event_queue.put_nowait(("message", text))


def _idle_wait_while(br, state, *, during) -> str | None:
    """Park in the real idle wait; run *during* concurrently."""
    async def go():
        waiter = asyncio.create_task(br._await_next_prompt())
        await during(waiter)
        return await asyncio.wait_for(waiter, timeout=1.0)

    return asyncio.run(go())


# --------------------------------------------------------------------------
# The reported sequence
# --------------------------------------------------------------------------

def test_a_prompt_typed_during_a_model_switch_is_sent_afterwards():
    br, state, sent, reconnects = _bridge()

    async def during(waiter):
        br.event_queue.put_nowait(("model", "claude-opus-5"))
        await asyncio.sleep(0)          # bridge picks it up, starts reconnect
        assert state.connecting, "test premise: the switch reconnects"
        _type_a_prompt(state, br, "carry on then")
        assert list(state.queued_prompts) == ["carry on then"], \
            "test premise: server.py queues a prompt typed while connecting"

    prompt = _idle_wait_while(br, state, during=during)

    assert reconnects == ["claude-opus-5"], "the model switch itself must happen"
    assert prompt == "carry on then", \
        "the queued prompt was stranded — idle, with it still in the panel"
    assert not state.queued_prompts, "sent, so it must leave the queue panel"


def test_the_stranded_prompt_is_echoed_into_the_transcript():
    """It was queued, so the browser skipped its optimistic echo."""
    br, state, sent, _ = _bridge()

    async def during(waiter):
        br.event_queue.put_nowait(("model", "claude-opus-5"))
        await asyncio.sleep(0)
        _type_a_prompt(state, br, "carry on then")

    _idle_wait_while(br, state, during=during)

    echoes = [m for m in sent
              if m.get("type") == "user_message" and m.get("content") == "carry on then"]
    assert len(echoes) == 1, "sent with no transcript row = an invisible turn"


# --------------------------------------------------------------------------
# ...and the same for every other command that reconnects
# --------------------------------------------------------------------------

def _reconnecting_command_strands_nothing(kind: str, payload: str) -> None:
    br, state, sent, _ = _bridge()

    async def during(waiter):
        br.event_queue.put_nowait((kind, payload))
        await asyncio.sleep(0)
        assert state.connecting, f"/{kind} is expected to reconnect"
        _type_a_prompt(state, br, "carry on then")

    assert _idle_wait_while(br, state, during=during) == "carry on then"


def test_effort_does_not_strand_a_prompt():
    _reconnecting_command_strands_nothing("effort", "max")


def test_thinking_does_not_strand_a_prompt():
    _reconnecting_command_strands_nothing("thinking", "on")


def test_connect_does_not_strand_a_prompt():
    _reconnecting_command_strands_nothing("connect", "")


# --------------------------------------------------------------------------
# Guard rails on the drain
# --------------------------------------------------------------------------

def test_a_config_command_with_an_empty_queue_keeps_waiting():
    """The drain must not invent a turn out of nothing."""
    br, state, sent, reconnects = _bridge()

    async def during(waiter):
        br.event_queue.put_nowait(("model", "claude-opus-5"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not waiter.done(), "returned a prompt when the user typed nothing"
        br.event_queue.put_nowait(("message", "typed later, calmly"))

    assert _idle_wait_while(br, state, during=during) == "typed later, calmly"
    assert reconnects == ["claude-opus-5"]


def test_a_half_typed_queue_head_is_not_sent():
    """Mid-edit in the panel: waiting beats sending half a sentence."""
    br, state, sent, _ = _bridge()

    async def during(waiter):
        br.event_queue.put_nowait(("model", "claude-opus-5"))
        await asyncio.sleep(0)
        _type_a_prompt(state, br, "carry on th")
        state.queue_editing_index = 0        # user is still editing it
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not waiter.done(), "sent a prompt the user was still editing"
        br.event_queue.put_nowait(("message", "never mind"))

    assert _idle_wait_while(br, state, during=during) == "never mind"
    assert list(state.queued_prompts) == ["carry on th"], "edit must survive"


def test_the_queue_order_is_respected():
    br, state, sent, _ = _bridge()

    async def during(waiter):
        state.queued_prompts.append("first")
        br.event_queue.put_nowait(("model", "claude-opus-5"))
        await asyncio.sleep(0)
        _type_a_prompt(state, br, "second")

    assert _idle_wait_while(br, state, during=during) == "first"
    assert list(state.queued_prompts) == ["second"]


# --------------------------------------------------------------------------
# The worker_loop's own idle wait is the same wait
# --------------------------------------------------------------------------

def test_the_first_prompt_wait_is_not_a_second_divergent_loop():
    """worker_loop used to hand-roll its own idle wait, which dropped
    /compact, /btw and wakeups and had this same stranding bug.  Anything
    that reintroduces a bespoke ``event_queue.get()`` loop there brings the
    whole family back."""
    import inspect

    src = inspect.getsource(SDKBridge.worker_loop)
    body = src.split("# --- Initial prompt ---", 1)[1]
    body = body.split("# --- Turn loop ---", 1)[0]

    assert "_await_next_prompt" in body, \
        "the initial-prompt wait must reuse the one idle wait point"
    assert "event_queue.get()" not in body, \
        "a second hand-rolled idle wait loop is back in worker_loop"
