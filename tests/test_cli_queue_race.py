"""A prompt sent as the CLI starts a turn of its own.

Reported 2026-09-27, session "OS D": "i interrupted it, sent a message, it
showed my message, the ai responded with 'reply sent' and never showed me
anything. then just by accident i reloaded the page, and i didn't see my
message shown, instead i saw a message sent from another agent that wasn't
showing before the reload."

The hub's log and the session's transcript, together (local time)::

    11:41:50  CLI enqueues a message from peer session Lane-A (its own queue)
    11:41:52  user's prompt typed during a ghost turn -> held in OUR queue
    11:41:53.947  interrupt ends the ghost turn
    11:41:53      CLI dequeues Lane-A's message and starts a turn with it
    11:41:53.959  we send the held prompt, 12 ms after the ghost turn ended
    11:41:54      CLI folds it into Lane-A's turn as a queued_command
    ...           model answers Lane-A: "Reply sent. Back to publishing..."

Three faults, and a fourth thing worth saying:

* **The race.**  The CLI starts the head of its own queue the instant a turn
  ends, and our prompt, sent in the same instant, got no turn of its own.  A
  queued prompt now waits ``TURN_END_SETTLE_S`` after a turn ends; a turn the
  CLI starts announces itself inside that, and the prompt waits for it.
* **Live, the peer's message was never shown.**  Its text has no harness
  prefix, so it was taken for the user's own and skipped as an echo.  It is
  recognised by ``origin.kind == "peer"`` now and shown as the peer's.
* **History showed it as the user's**, and **lost the user's own message**,
  which the CLI records only as a ``queued_command`` attachment.
* **When it still happens**, the result's ``origin`` says the turn was not
  ours, and the user is told to check the reply and resend if need be.

Offline: a real ``SDKBridge`` with a list broadcaster; no SDK subprocess.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ResultMessage,
    TextBlock,
    UserMessage,
)

import sdk_bridge                                     # noqa: E402
from config import parse_args                         # noqa: E402
from sdk_bridge import SDKBridge                      # noqa: E402
from session import render_session_history            # noqa: E402
from state import init_state_from_config              # noqa: E402

SETTLE = 0.3

PEER_TEXT = (
    'Another Claude session sent a message:\n<cross-session-message '
    'from="uds:\\\\.\\pipe\\LOCAL\\cc-msg-1" from-name="Lane-A">\n'
    'Lane A -> Lane D: two operator answers\n</cross-session-message>')
PEER_ORIGIN = {"kind": "peer", "from": "uds:\\\\.\\pipe\\LOCAL\\cc-msg-1",
               "name": "Lane-A", "body": "Lane A -> Lane D: two operator answers"}


@pytest.fixture(autouse=True)
def short_settle(monkeypatch):
    monkeypatch.setattr(sdk_bridge, "TURN_END_SETTLE_S", SETTLE)


def _bridge():
    cfg = parse_args([])
    state = init_state_from_config(cfg)
    sent: list[dict] = []

    async def bcast(msg: dict) -> None:
        sent.append(msg)

    return SDKBridge(config=cfg, state=state, broadcaster=bcast), state, sent


def _peer_message(origin=PEER_ORIGIN) -> UserMessage:
    return UserMessage(content=PEER_TEXT, origin=origin)


def _result(origin=None) -> ResultMessage:
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                         is_error=False, num_turns=1, session_id="sid-1",
                         origin=origin)


def _of(sent, kind):
    return [m for m in sent if m.get("type") == kind]


# --------------------------------------------------------------------------
# The race
# --------------------------------------------------------------------------

def test_a_prompt_held_through_a_ghost_turn_waits_for_the_turn_the_cli_starts():
    """The report.  The ghost turn ends; in the same instant the CLI starts a
    peer's turn; the held prompt must not go into it."""
    br, state, sent = _bridge()

    async def go():
        await br._begin_ghost_turn_if_needed()
        state.queued_prompts.append("my question")
        waiter = asyncio.create_task(br._await_next_prompt())
        await asyncio.sleep(0.02)
        await br._end_ghost_turn("error_during_execution")   # the interrupt
        await asyncio.sleep(0.02)
        await br._handle_async_message(_peer_message())        # CLI's own turn
        await asyncio.sleep(SETTLE + 0.2)
        popped_early = waiter.done()
        still_queued = list(state.queued_prompts)
        await br._end_ghost_turn("success")                    # peer turn ends
        return popped_early, still_queued, await asyncio.wait_for(waiter, SETTLE + 1)

    popped_early, still_queued, prompt = asyncio.run(go())

    assert not popped_early, "the prompt was sent into the turn the CLI started"
    assert still_queued == ["my question"], "it left the queue without a turn"
    assert prompt == "my question", "it never went out after that turn ended"


def test_without_one_it_goes_out_once_the_settle_has_passed():
    br, state, _sent = _bridge()

    async def go():
        await br._begin_ghost_turn_if_needed()
        state.queued_prompts.append("my question")
        waiter = asyncio.create_task(br._await_next_prompt())
        await asyncio.sleep(0.02)
        ended = time.monotonic()
        await br._end_ghost_turn("success")
        prompt = await asyncio.wait_for(waiter, SETTLE + 1)
        return prompt, time.monotonic() - ended

    prompt, took = asyncio.run(go())

    assert prompt == "my question"
    assert took >= SETTLE * 0.9, f"sent {took:.2f}s after the turn ended"


def test_a_prompt_long_after_a_turn_is_not_delayed():
    """The settle is the instant after a turn ends, nothing more."""
    br, state, _sent = _bridge()
    br._turn_ended_at = time.monotonic() - 60

    async def go():
        state.queued_prompts.append("my question")
        started = time.monotonic()
        prompt = await br._pop_queued_prompt()
        return prompt, time.monotonic() - started

    prompt, took = asyncio.run(go())

    assert prompt == "my question" and took < SETTLE / 2


def test_the_end_of_our_own_turn_starts_the_settle_too():
    """The same race follows a turn we ran: the next queued prompt goes out
    straight after it, into whatever the CLI starts there."""
    br, _state, _sent = _bridge()

    class Client:
        async def query(self, prompt):
            br.turn_msg_queue.put_nowait(_result())

        async def interrupt(self):  # pragma: no cover
            pass

    br.client = Client()
    before = time.monotonic()
    asyncio.run(br.run_turn("first"))

    assert br._turn_ended_at is not None and br._turn_ended_at >= before


def test_a_ghost_turns_end_starts_it():
    br, _state, _sent = _bridge()

    async def go():
        await br._begin_ghost_turn_if_needed()
        await br._end_ghost_turn("success")

    before = time.monotonic()
    asyncio.run(go())

    assert br._turn_ended_at is not None and br._turn_ended_at >= before


def test_a_bridge_stopped_during_the_settle_sends_nothing():
    """The pop would otherwise hand a prompt to a turn on a client that is
    shutting down."""
    br, state, _sent = _bridge()
    br._turn_ended_at = time.monotonic()

    async def go():
        state.queued_prompts.append("my question")
        task = asyncio.create_task(br._pop_queued_prompt())
        await asyncio.sleep(0.02)
        br.stop_event.set()
        return await task

    assert asyncio.run(go()) is None


def test_editing_the_head_during_the_settle_still_holds_it():
    br, state, _sent = _bridge()
    br._turn_ended_at = time.monotonic()

    async def go():
        state.queued_prompts.append("half-typed")
        task = asyncio.create_task(br._pop_queued_prompt())
        await asyncio.sleep(0.02)
        state.queue_editing_index = 0
        return await task

    assert asyncio.run(go()) is None
    assert list(state.queued_prompts) == ["half-typed"]


# --------------------------------------------------------------------------
# The peer's message, live
# --------------------------------------------------------------------------

def test_a_peers_message_between_turns_is_shown_as_the_peers():
    br, state, sent = _bridge()

    asyncio.run(br._handle_async_message(_peer_message()))

    [m] = _of(sent, "peer_message")
    assert m["name"] == "Lane-A"
    assert m["body"] == "Lane A -> Lane D: two operator answers"
    assert state.busy, "the turn it starts was not marked as running"


def test_without_a_decoded_body_the_message_itself_is_shown():
    br, _state, sent = _bridge()
    origin = {"kind": "peer", "name": "Lane-A"}

    asyncio.run(br._handle_async_message(_peer_message(origin)))

    [m] = _of(sent, "peer_message")
    assert "Lane A -> Lane D" in m["body"]


def test_the_users_own_text_is_not_taken_for_a_peers():
    br, _state, sent = _bridge()

    asyncio.run(br._handle_async_message(UserMessage(content="just me")))

    assert not _of(sent, "peer_message")


def _peer_turn_client(br, *, origin=PEER_ORIGIN):
    """A CLI that, handed our prompt, runs a peer's turn instead."""
    class Client:
        async def query(self, prompt):
            br.turn_msg_queue.put_nowait(_peer_message())
            br.turn_msg_queue.put_nowait(AssistantMessage(
                content=[TextBlock("Reply sent. Back to publishing.")],
                model="claude-x"))
            br.turn_msg_queue.put_nowait(_result(origin))

        async def interrupt(self):  # pragma: no cover
            pass

    return Client()


def test_a_peers_message_inside_our_turn_is_shown_too():
    br, _state, sent = _bridge()
    br.client = _peer_turn_client(br)

    asyncio.run(br.run_turn("my question"))

    assert [m["name"] for m in _of(sent, "peer_message")] == ["Lane-A"]


# --------------------------------------------------------------------------
# When it still happens: say so
# --------------------------------------------------------------------------

def _warnings(sent):
    return [(m.get("data") or {}).get("message", "") for m in sent
            if m.get("type") == "system_msg"
            and m.get("subtype") == "warning"]


def test_a_turn_that_ends_on_a_peers_result_says_the_reply_was_not_to_you():
    br, _state, sent = _bridge()
    br.client = _peer_turn_client(br)

    asyncio.run(br.run_turn("my question"))

    said = " ".join(_warnings(sent))
    assert "a message from Lane-A" in said, said
    assert "send it again" in said


def test_our_own_turn_says_nothing():
    br, _state, sent = _bridge()
    br.client = _peer_turn_client(br, origin=None)

    asyncio.run(br.run_turn("my question"))

    assert not [w for w in _warnings(sent) if "not to your message" in w]


@pytest.mark.parametrize("origin, expected", [
    (None, None),
    ({"kind": "human"}, None),
    ({"kind": "peer", "name": "Lane-B"}, "a message from Lane-B"),
    ({"kind": "task-notification"}, "a background task finishing"),
    ({"kind": "task-notification", "subkind": "scheduled-trigger"},
     "a scheduled task"),
    ({"kind": "coordinator"}, "something the session started on its own"),
])
def test_what_the_notice_says_the_turn_was(origin, expected):
    br, _state, sent = _bridge()

    asyncio.run(br._announce_if_not_our_turn(_result(origin)))

    said = _warnings(sent)
    if expected is None:
        assert not said
    else:
        assert said and expected in said[0], said


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------

def _history(tmp_path, records):
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                    encoding="utf-8")
    _n, messages, _orphans, _todos = render_session_history(path)
    return messages


PEER_RECORD = {
    "type": "user", "uuid": "u-peer", "isMeta": True,
    "origin": {**PEER_ORIGIN, "msg_id": "m1", "fromMode": "bypass"},
    "message": {"role": "user", "content": PEER_TEXT},
}

QUEUED = {
    "type": "attachment", "uuid": "a-1",
    "attachment": {"type": "queued_command", "prompt": "my question",
                   "source_uuid": "u-mine", "commandMode": "prompt"},
}


def test_a_peers_message_in_history_is_the_peers_not_yours(tmp_path):
    msgs = _history(tmp_path, [PEER_RECORD])

    assert [m["type"] for m in msgs] == ["peer_message"]
    assert msgs[0]["name"] == "Lane-A"
    assert msgs[0]["body"] == "Lane A -> Lane D: two operator answers"


def test_a_prompt_passed_to_a_running_turn_survives_a_reload(tmp_path):
    """The other half of the report: the message the user watched go out was
    gone after the reload."""
    msgs = _history(tmp_path, [PEER_RECORD, QUEUED])

    mine = [m for m in msgs if m["type"] == "user"]
    assert [m["content"] for m in mine] == ["my question"]
    assert mine[0].get("mid_turn") is True


def test_it_is_not_shown_twice_if_it_was_also_a_turn(tmp_path):
    own = {"type": "user", "uuid": "u-mine",
           "message": {"role": "user", "content": "my question"}}

    msgs = _history(tmp_path, [own, QUEUED])

    assert [m["content"] for m in msgs if m["type"] == "user"] == ["my question"]


def test_a_queued_command_that_is_not_a_prompt_is_not_shown(tmp_path):
    other = json.loads(json.dumps(QUEUED))
    other["attachment"]["commandMode"] = "bash"

    assert not _history(tmp_path, [other])


def test_the_reported_session_reads_in_order(tmp_path):
    """Interrupt, the peer's message, a tool call, then the user's message
    where the model saw it, then the reply to the peer."""
    records = [
        {"type": "user", "uuid": "u-int",
         "message": {"role": "user", "content": "[Request interrupted by user]"}},
        PEER_RECORD,
        {"type": "assistant", "uuid": "a-tool", "message": {
            "role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "ToolSearch",
                 "input": {"query": "select:SendMessage"}}]}},
        QUEUED,
        {"type": "assistant", "uuid": "a-text", "message": {
            "role": "assistant", "content": [
                {"type": "text", "text": "Reply sent. Back to publishing."}]}},
    ]

    kinds = [m["type"] for m in _history(tmp_path, records)]

    assert kinds == ["user", "peer_message", "tool_use", "user", "assistant"]
