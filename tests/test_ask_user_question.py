"""A question Claude asks is shown once, and reads as waiting for a reply.

Reported 2026-11-06: "a question collapsed automatically because questions
apparently count as tool uses. i could have missed it if i hadn't been watching
before it collapsed."

The hub cannot run AskUserQuestion, so it denies it, and Claude goes on in
plain text.  The question is shown from the tool call itself, in a block the
chat never collapses (tests/activity_collapse.test.js); the denial is drawn as
the question waiting for its reply, live and from history alike, so the hub
marks it (``awaiting_reply``).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import parse_args                                   # noqa: E402
from sdk_bridge import PermissionResultDeny, SDKBridge          # noqa: E402
from session import render_session_history                     # noqa: E402
from state import init_state_from_config                       # noqa: E402
from tool_manager import (                                      # noqa: E402
    ASK_DENIAL, awaits_reply, format_tool_result_msg,
)

QUESTIONS = {"questions": [{
    "question": "Which one?", "header": "Pick", "multiSelect": False,
    "options": [{"label": "A", "description": "a"}, {"label": "B", "description": "b"}],
}]}

# As the CLI records the denial, in a session from before this change.
RECORDED = ("AskUserQuestion isn't supported in this interface, so it was not run. "
            "Your questions have already been shown to the user in the chat. Do not "
            "repeat them — end your turn and wait for the user's plain-text reply, "
            "then continue with their answer.")


def _bridge():
    cfg = parse_args([])
    sent: list[dict] = []

    async def bcast(msg: dict) -> None:
        sent.append(msg)

    return SDKBridge(config=cfg, state=init_state_from_config(cfg), broadcaster=bcast), sent


def test_the_question_is_denied_and_shown_only_by_its_own_block():
    br, sent = _bridge()

    result = asyncio.run(br._handle_tool_permission("AskUserQuestion", QUESTIONS, None))

    assert isinstance(result, PermissionResultDeny)
    assert result.message == ASK_DENIAL
    # A second copy of the question, as a notice, is what collapsed away.
    assert sent == [], sent


def test_the_denial_is_known_for_what_it_is():
    assert awaits_reply(ASK_DENIAL, True)
    assert awaits_reply(RECORDED, True)
    # By its opening words: a session recorded under other wording still is.
    assert awaits_reply("AskUserQuestion isn't supported in this interface, so it "
                        "was not run. Answer in plain text.", True)


def test_and_nothing_else_is():
    assert not awaits_reply(ASK_DENIAL, False), "a result that is no error"
    assert not awaits_reply("InputValidationError: questions is required", True)
    assert not awaits_reply("", True)


def test_a_live_result_says_so():
    msg = format_tool_result_msg("tu-1", 3, "AskUserQuestion", ASK_DENIAL, True)
    assert msg["awaiting_reply"] is True
    assert format_tool_result_msg("tu-2", 4, "Bash", "boom", True)["awaiting_reply"] is False


def test_so_does_one_from_history(tmp_path: Path):
    jsonl = tmp_path / "s.jsonl"
    records = [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "q1", "name": "AskUserQuestion", "input": QUESTIONS}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "q1", "content": RECORDED,
             "is_error": True}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "x"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "b1", "content": "boom", "is_error": True}]}},
    ]
    jsonl.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    _count, messages, _orphans, _todos = render_session_history(jsonl)

    results = {m["tool_use_id"]: m for m in messages if m["type"] == "tool_result"}
    assert results["q1"]["awaiting_reply"] is True
    assert results["b1"]["awaiting_reply"] is False
