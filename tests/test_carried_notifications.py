"""What the CLI carries to the next message on resume is shown with it.

Reported 2026-10-21: "i got a notification saying a background task was cut
off and the session will be told with the next prompt it gets, but it didn't
show the modified prompt to me after "You" when i sent one".

On resume the CLI reports each background task the previous process left
unfinished as a ``<task-notification>`` of its own, written into the
conversation without a turn (``queueTranscriptOnly``), which goes to the model
with whatever is sent next.  History dropped every ``<task-notification>``,
and nothing showed it live, so the message the model got was not the one the
user saw.  Now it is shown -- live with the first turn after the connect, in
history where it sits -- under a line saying where it came from.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock  # noqa: E402

import sdk_bridge  # noqa: E402
from config import parse_args  # noqa: E402
from sdk_bridge import SDKBridge  # noqa: E402
from session import (  # noqa: E402
    CARRIED_NOTE,
    carried_notifications,
    read_tail,
    render_session_history,
)
from state import init_state_from_config  # noqa: E402

SID = "22222222-3333-4444-5555-666666666666"

#: As 2.1.301 wrote it on resume (a real record, trimmed).
REPORT = ("<task-notification>\n<task-id>bozs0skmu</task-id>\n<status>stopped</status>\n"
          "<summary>Background shell command didn't finish before the previous "
          "session ended</summary>\n</task-notification>")


def _carried(text=REPORT):
    return {"type": "user", "timestamp": "2026-10-21T13:34:46.108Z",
            "origin": {"kind": "task-notification"}, "promptSource": "system",
            "queueSkipAttachments": True, "queueTranscriptOnly": True,
            "message": {"role": "user", "content": text}}


def _notified():
    """An ordinary notification: one that started a turn of its own."""
    return {"type": "user", "timestamp": "2026-10-21T13:30:00.000Z",
            "origin": {"kind": "task-notification"},
            "message": {"role": "user", "content": REPORT.replace("bozs0skmu", "other")}}


def _reply(text="done"):
    return {"type": "assistant", "timestamp": "2026-10-21T13:00:00.000Z",
            "message": {"model": "claude-opus-5-5", "role": "assistant",
                        "content": [{"type": "text", "text": text}]}}


def _write(path: Path, records) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def test_history_shows_a_carried_notification_and_not_an_ordinary_one(tmp_path):
    p = _write(tmp_path / f"{SID}.jsonl", [_notified(), _reply(), _carried(),
                                           {"type": "user", "message": {
                                               "role": "user", "content": "continue"}}])
    _n, messages, _o, _t = render_session_history(p)
    injected = [m for m in messages if m["type"] == "injected_prompt"]
    assert len(injected) == 1
    assert injected[0]["content"].startswith(CARRIED_NOTE)
    assert "bozs0skmu" in injected[0]["content"]
    assert [m["content"] for m in messages if m["type"] == "user"] == ["continue"]


def test_only_what_no_turn_has_taken_yet_is_carried(tmp_path):
    p = _write(tmp_path / "s.jsonl", [_carried(REPORT.replace("bozs0skmu", "old")),
                                      _reply(), _carried()])
    texts = carried_notifications(read_tail(p)[0])
    assert len(texts) == 1 and "bozs0skmu" in texts[0]


def _bridge(tmp_path, monkeypatch, records):
    _write(tmp_path / f"{SID}.jsonl", records)
    cfg = parse_args(["--cwd", str(tmp_path)])
    st = init_state_from_config(cfg)
    st.session_id = SID
    sent: list[dict] = []

    async def bc(m):
        sent.append(m)

    br = SDKBridge(config=cfg, state=st, broadcaster=bc)
    monkeypatch.setattr(sdk_bridge, "find_session_dir", lambda sid, cd=None: tmp_path)
    monkeypatch.setattr(sdk_bridge, "TURN_END_SETTLE_S", 0.0)
    return br, sent


class _Client:
    def __init__(self, br):
        self.br = br

    async def query(self, _prompt):
        self.br.turn_msg_queue.put_nowait(AssistantMessage(
            content=[TextBlock(text="ok")], model="claude-opus-5-5"))
        self.br.turn_msg_queue.put_nowait(ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=0, is_error=False,
            num_turns=1, session_id=SID))

    async def interrupt(self):  # pragma: no cover
        pass


def test_the_first_prompt_after_opening_shows_what_went_with_it(tmp_path, monkeypatch):
    """After the "You" line: the turn starts, and what the CLI added follows."""
    br, sent = _bridge(tmp_path, monkeypatch, [_reply(), _carried()])
    br.client = _Client(br)
    br._carried_unseen = True        # as connect() leaves it after a resume

    async def go():
        await br.run_turn("continue")
        await br.run_turn("and again")

    asyncio.run(go())
    kinds = [m["type"] for m in sent if m["type"] in ("turn_start", "injected_prompt")]
    assert kinds == ["turn_start", "injected_prompt", "turn_start"], \
        "shown once, with the first prompt after the turn starts"
    shown = next(m for m in sent if m["type"] == "injected_prompt")
    assert shown["content"].startswith(CARRIED_NOTE) and shown["during_turn"] is True


def test_a_turn_the_cli_starts_first_shows_it_instead(tmp_path, monkeypatch):
    """A background task finishing can start a turn before the user types:
    that turn takes it to the model."""
    br, sent = _bridge(tmp_path, monkeypatch, [_reply(), _carried()])
    br._carried_unseen = True
    asyncio.run(br._begin_ghost_turn_if_needed())
    assert [m["content"].startswith(CARRIED_NOTE) for m in sent
            if m["type"] == "injected_prompt"] == [True]


def test_nothing_is_shown_without_a_resume(tmp_path, monkeypatch):
    br, sent = _bridge(tmp_path, monkeypatch, [_reply(), _carried()])
    br.client = _Client(br)
    asyncio.run(br.run_turn("hi"))
    assert not [m for m in sent if m["type"] == "injected_prompt"]


def test_a_transcript_that_cannot_be_found_never_stops_the_prompt(tmp_path, monkeypatch):
    """Review: the lookup ran on the event loop, outside the read's guard, so
    an error in it was raised into run_turn before the prompt was sent."""
    br, sent = _bridge(tmp_path, monkeypatch, [_reply(), _carried()])
    queried = []

    class _Recording(_Client):
        async def query(self, prompt):
            queried.append(prompt)
            await super().query(prompt)

    def broken(*_a, **_k):
        raise OSError("projects directory unreadable")

    monkeypatch.setattr(sdk_bridge, "find_session_dir", broken)
    br.client = _Recording(br)
    br._carried_unseen = True
    asyncio.run(br.run_turn("continue"))
    assert queried == ["continue"]
