"""Opening a session whose last turn was cut off: it waits, and says so.

Reported 2026-09-29: "sessions would often start working automatically when i
resumed them, when i don't think they had had anything scheduled, and without
showing me the prompt that caused it to start working again."  Asked what a
resumed session should do: wait for me.

So opening a session no longer finishes a cut-off turn by itself
(tests/test_resume_interrupted_turn.py).  What replaces it is here:

* ``session.cut_off_turn`` reads the end of the transcript, before the new CLI
  writes to it, and says whether the last turn was cut off and which one.  The
  CLI's own test cannot be asked (it says nothing on the stream, and writes its
  placeholder pair only when the next prompt goes out).  The shapes below are
  the real ones, from the transcripts of the sessions in the report.
* the tab is told -- when the connect works, and again to a tab that attaches
  later (the tab that opened it is often not there yet), until a turn starts.
* history draws the CLI's placeholder pair as what it is, not as a prompt the
  user typed answered by a refusal.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sdk_bridge                                        # noqa: E402
import server                                            # noqa: E402
from config import parse_args                            # noqa: E402
from sdk_bridge import SDKBridge                         # noqa: E402
from session import (                                    # noqa: E402
    CLI_CONTINUE_PROMPT,
    CLI_NO_RESPONSE,
    cut_off_turn,
    describe_cut_off_turn,
    render_session_history,
    save_persisted_bg_tasks,
)
from state import init_state_from_config                 # noqa: E402

SID = "13d470f2-3b2b-4ced-b1f8-70c2dcf29025"


# --------------------------------------------------------------------------
# Records, shaped as the CLI writes them
# --------------------------------------------------------------------------

def _ts(n: int) -> str:
    return f"2026-09-27T20:{n // 60:02d}:{n % 60:02d}.000Z"


def prompt(text, t, **extra):
    return {"type": "user", "timestamp": _ts(t),
            "message": {"role": "user", "content": text}, **extra}


def result(tool_id, t):
    return {"type": "user", "timestamp": _ts(t), "message": {
        "role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}]}}


def say(text, t):
    return {"type": "assistant", "timestamp": _ts(t), "message": {
        "role": "assistant", "model": "claude-x",
        "content": [{"type": "text", "text": text}]}}


def call(tool_id, name, t):
    return {"type": "assistant", "timestamp": _ts(t), "message": {
        "role": "assistant", "model": "claude-x",
        "content": [{"type": "tool_use", "id": tool_id, "name": name,
                     "input": {}}]}}


def think(t):
    return {"type": "assistant", "timestamp": _ts(t), "message": {
        "role": "assistant", "model": "claude-x",
        "content": [{"type": "thinking", "thinking": "", "signature": "x"}]}}


def attachment(t, kind="total_tokens_reminder", **extra):
    return {"type": "attachment", "timestamp": _ts(t),
            "attachment": {"type": kind, **extra}}


def api_error(t):
    return {"type": "assistant", "timestamp": _ts(t), "isApiErrorMessage": True,
            "message": {"role": "assistant", "model": "<synthetic>", "content": [
                {"type": "text",
                 "text": "You've hit your weekly limit · resets Sep 30, 1am"}]}}


def cli_continue(t):
    return prompt(CLI_CONTINUE_PROMPT, t, isMeta=True)


def cli_placeholder(t):
    return {"type": "assistant", "timestamp": _ts(t), "message": {
        "role": "assistant", "model": "<synthetic>",
        "content": [{"type": "text", "text": CLI_NO_RESPONSE}]}}


def orphan_report(t):
    return prompt("<task-notification>\n<task-id>b1</task-id>\n"
                  "<status>stopped</status>\n<summary>Background shell command "
                  "didn't finish before the previous session ended</summary>\n"
                  "</task-notification>", t,
                  origin={"kind": "task-notification"})


NOT_MESSAGES = [{"type": "last-prompt"}, {"type": "custom-title", "title": "OS F"},
                {"type": "ai-title"}, {"type": "mode"}, {"type": "queue-operation"}]


def _write(tmp_path, records, name=SID):
    path = Path(tmp_path) / f"{name}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return path


def _cut(tmp_path, records, **kw):
    return cut_off_turn(_write(tmp_path, records), **kw)


# --------------------------------------------------------------------------
# cut_off_turn: the shapes from the report
# --------------------------------------------------------------------------

def test_a_tool_call_that_never_came_back(tmp_path):
    """OS F at 17:00 on 2026-09-27: text, then a Bash call with no result."""
    info = _cut(tmp_path, [
        prompt("answers to the open questions: do the first one", 1),
        call("t1", "Read", 2), result("t1", 3),
        say("Filling in the numbers properly", 4),
        call("t2", "Bash", 5),
    ] + NOT_MESSAGES)

    assert info["phase"] == "tool" and info["tool"] == "Bash"
    assert info["stopped"] == _ts(5)
    assert info["trigger"] == "prompt"
    assert info["prompt"] == "answers to the open questions: do the first one"
    assert info["started"] == _ts(1)


def test_a_result_the_model_never_answered(tmp_path):
    """Lane A: an Edit's result, then the next turn's context, and nothing."""
    info = _cut(tmp_path, [
        prompt("Continue.", 1), call("t1", "Edit", 2), result("t1", 3),
        attachment(4),
    ])

    assert info["phase"] == "reply" and info["tool"] == "Edit"
    assert info["prompt"] == "Continue."


def test_a_prompt_it_never_answered(tmp_path):
    info = _cut(tmp_path, [say("done", 1), prompt("now do the next one", 2)])

    assert info["phase"] == "reply" and info["tool"] is None
    assert info["prompt"] == "now do the next one"


def test_output_that_had_only_begun(tmp_path):
    """A thinking block and nothing after it: the model had not said anything,
    so the turn is still the one its result started."""
    info = _cut(tmp_path, [prompt("go", 1), call("t1", "Grep", 2),
                           result("t1", 3), think(4)])

    assert info is not None and info["tool"] == "Grep"


def test_a_finished_turn_is_not_cut_off(tmp_path):
    assert _cut(tmp_path, [prompt("go", 1), call("t1", "Bash", 2),
                           result("t1", 3), say("All done.", 4)]
                + NOT_MESSAGES) is None


def test_a_turn_that_ended_on_an_api_error_is_not_cut_off(tmp_path):
    """OS C and OS D at 16:45: the weekly limit.  The CLI would re-run that
    turn; to the user it failed, and said so when it did."""
    assert _cut(tmp_path, [prompt("go", 1), result("t0", 2), attachment(3),
                           api_error(4)]) is None


def test_an_api_error_ends_the_turn_whatever_it_says(tmp_path):
    """It is the error flag that decides, not the wording -- one recorded
    without text still ended the turn the user saw fail."""
    wordless = api_error(4)
    wordless["message"]["content"] = [{"type": "text", "text": ""}]

    assert _cut(tmp_path, [prompt("go", 1), result("t0", 2), attachment(3),
                           wordless]) is None


def test_an_unanswered_peer_message_is_not_cut_off(tmp_path):
    """The CLI's own rule: a turn ending on a meta message is not interrupted."""
    assert _cut(tmp_path, [say("done", 1), prompt(
        "Another Claude session sent a message: ...", 2, isMeta=True,
        origin={"kind": "peer", "name": "Lane-F"})]) is None


def test_commands_are_not_prompts_owed_a_reply(tmp_path):
    """A slash command's record and its output, after a finished turn."""
    assert _cut(tmp_path, [
        say("done", 1),
        prompt("<command-name>/model</command-name>\n<command-message>model"
               "</command-message>\n<command-args></command-args>", 2),
        prompt("<local-command-stdout>Set model to Opus</local-command-stdout>", 3),
    ]) is None


def test_an_earlier_resumes_bookkeeping_is_looked_past(tmp_path):
    """Opened once already and closed without a word: that resume's report of
    the dead tasks, and a placeholder pair, sit after the cut-off turn.  It is
    still cut off, and still the same turn."""
    info = _cut(tmp_path, [
        prompt("run the suite", 1), call("t1", "Bash", 2),
        orphan_report(3), cli_continue(4), cli_placeholder(5),
    ])

    assert info["phase"] == "tool" and info["tool"] == "Bash"
    assert info["prompt"] == "run the suite"


def test_a_prompt_sent_mid_turn_and_never_reached(tmp_path):
    """The CLI records it as an attachment, not a prompt -- after the model's
    words, which would otherwise read as the end of the turn."""
    info = _cut(tmp_path, [
        prompt("go", 1), say("On it.", 2),
        attachment(3, "queued_command", prompt="and also this"),
    ])

    assert info is not None and info["prompt"] == "go"


def test_other_context_is_not_a_prompt(tmp_path):
    assert _cut(tmp_path, [prompt("go", 1), say("Done.", 2),
                           attachment(3, "skill_listing")]) is None


def test_a_turn_a_background_task_started(tmp_path):
    info = _cut(tmp_path, [
        say("waiting on the build", 1),
        prompt("<task-notification><task-id>b2</task-id><status>completed"
               "</status></task-notification>", 2,
               origin={"kind": "task-notification"}),
        call("t1", "Write", 3),
    ])

    assert info["trigger"] == "task" and info["prompt"] is None


def test_a_turn_a_loop_command_started(tmp_path):
    info = _cut(tmp_path, [
        prompt("<command-message>loop</command-message>\n<command-name>/loop"
               "</command-name>\n<command-args>Work through the list"
               "</command-args>", 1),
        prompt("# Autonomous loop check\n...", 2, isMeta=True),
        call("t1", "Bash", 3),
    ])

    assert info["trigger"] == "prompt"
    assert info["prompt"] == "/loop Work through the list"


def test_a_turn_older_than_the_tail_is_still_named(tmp_path):
    """A lane's turn can run for hours of tool calls; the prompt is found
    further back without the tail having to hold it."""
    records = [prompt("the long job", 1)]
    for i in range(300):
        records += [call(f"t{i}", "Bash", 2), result(f"t{i}", 3)]
    records.append(call("last", "Edit", 4))

    info = _cut(tmp_path, records, tail_bytes=2000)

    assert info["tool"] == "Edit" and info["prompt"] == "the long job"


def test_a_compaction_inside_the_turn_is_looked_past(tmp_path):
    info = _cut(tmp_path, [
        prompt("the long job", 1), call("t1", "Bash", 2), result("t1", 3),
        prompt("This session is being continued from a previous conversation",
               4, isCompactSummary=True),
        call("t2", "Bash", 5),
    ])

    assert info["prompt"] == "the long job"


def test_sidechains_are_not_the_session(tmp_path):
    side = say("a subagent's last word", 3)
    side["isSidechain"] = True
    info = _cut(tmp_path, [prompt("go", 1), call("t1", "Task", 2), side])

    assert info is not None and info["tool"] == "Task"


def test_no_transcript_is_no_answer(tmp_path):
    assert cut_off_turn(Path(tmp_path) / "missing.jsonl") is None


# --------------------------------------------------------------------------
# What the tab says
# --------------------------------------------------------------------------

def _clock(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone() \
        .strftime("%I:%M %p").lstrip("0")


NOW = datetime.fromisoformat("2026-09-27T21:00:00+00:00").astimezone()


def test_it_says_when_it_stopped_and_doing_what():
    text = describe_cut_off_turn({"stopped": _ts(5), "phase": "tool",
                                  "tool": "Bash", "trigger": None}, now=NOW)

    assert f"cut off at {_clock(_ts(5))}, while running Bash." in text


def test_it_says_what_started_it():
    text = describe_cut_off_turn({
        "stopped": _ts(5), "phase": "reply", "tool": "Edit", "trigger": "prompt",
        "prompt": "answers to the open questions:\nQ2: do the first one", "started": _ts(1),
    }, now=NOW)

    assert "just after Edit finished" in text
    assert f"“answers to the open questions:” ({_clock(_ts(1))})" in text


def test_a_long_prompt_is_cut_short():
    text = describe_cut_off_turn({"stopped": _ts(5), "phase": "reply",
                                  "trigger": "prompt", "prompt": "x" * 200},
                                 now=NOW)

    assert "x" * 79 + "…" in text and "x" * 81 not in text


@pytest.mark.parametrize("trigger,expect", [
    ("task", "when a background task finished"),
    ("peer", "a message from Lane-F"),
])
def test_a_turn_nobody_typed_says_who_started_it(trigger, expect):
    text = describe_cut_off_turn({"stopped": _ts(5), "phase": "reply",
                                  "trigger": trigger, "from": "Lane-F"}, now=NOW)

    assert expect in text


def test_it_says_what_to_do():
    """Nothing happens next until the user does something."""
    text = describe_cut_off_turn({"phase": "reply"}, now=NOW)

    assert "won't carry on by itself" in text and "“continue”" in text


def test_another_day_says_which():
    other_day = datetime.fromisoformat("2026-09-29T12:00:00+00:00").astimezone()
    text = describe_cut_off_turn({"stopped": _ts(5), "phase": "reply"},
                                 now=other_day)

    assert "Sep 27, " in text


# --------------------------------------------------------------------------
# History: the CLI's placeholder pair
# --------------------------------------------------------------------------

def test_the_clis_prompt_is_drawn_as_the_harnesss(tmp_path):
    """2026-09-06's report was this pair drawn as the user's own prompt."""
    _n, msgs, _o, _t = render_session_history(_write(tmp_path, [
        prompt("go", 1), call("t1", "Bash", 2), cli_continue(3),
        cli_placeholder(4), prompt("continue", 5)]))
    kinds = [(m["type"], m.get("content")) for m in msgs
             if m["type"] in ("user", "injected_prompt")]

    assert ("injected_prompt", CLI_CONTINUE_PROMPT) in kinds
    assert ("user", CLI_CONTINUE_PROMPT) not in kinds
    assert ("user", "continue") in kinds


def test_the_placeholder_is_not_drawn_as_a_reply(tmp_path):
    """"No response requested." as the model's word read as a refusal."""
    _n, msgs, _o, _t = render_session_history(_write(tmp_path, [
        prompt("go", 1), cli_placeholder(2)]))

    assert not [m for m in msgs if m["type"] == "assistant"]
    notes = [m for m in msgs if m["type"] == "system"]
    assert notes and "cut off" in notes[-1]["content"]


def test_a_real_reply_that_says_the_same_is_still_the_models(tmp_path):
    _n, msgs, _o, _t = render_session_history(_write(tmp_path, [
        prompt("say it", 1), say(CLI_NO_RESPONSE, 2)]))

    assert [m["content"] for m in msgs if m["type"] == "assistant"] == [CLI_NO_RESPONSE]


def test_a_typed_prompt_that_says_the_same_is_still_the_users(tmp_path):
    _n, msgs, _o, _t = render_session_history(_write(tmp_path, [
        prompt(CLI_CONTINUE_PROMPT, 1)]))

    assert [m["type"] for m in msgs] == ["user"]


# --------------------------------------------------------------------------
# The bridge: told when the connect works
# --------------------------------------------------------------------------

class _CLI:
    """A ClaudeSDKClient that connects at once and says nothing."""

    last = None

    def __init__(self, options=None):
        self.options = options
        _CLI.last = self

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def receive_messages(self):
        while True:
            await asyncio.sleep(3600)
            yield None      # pragma: no cover


@pytest.fixture
def opened(monkeypatch, tmp_path):
    """Connect a bridge to SID, whose transcript is *records*; return what it
    said, its state and the env its CLI was given."""
    monkeypatch.setenv("ORCH2_AGENT_DB", str(tmp_path / "agents.db"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("ORCH2_AGENT_NAME", raising=False)
    monkeypatch.setattr(sdk_bridge, "ClaudeSDKClient", _CLI)
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr(sdk_bridge, "find_session_dir",
                        lambda sid, cfg=None: project if sid == SID else None)

    def run(records, argv=(), *, recovering=False, lost=None):
        _write(project, records)
        cfg = dataclasses.replace(parse_args(list(argv)), cwd=str(tmp_path))
        st = init_state_from_config(cfg)
        st.session_id = SID
        sent: list[dict] = []

        async def bc(m):
            sent.append(m)

        br = SDKBridge(config=cfg, state=st, broadcaster=bc)
        br._initial_resume_id = SID
        if lost:
            save_persisted_bg_tasks(str(tmp_path), lost, SID)

        async def go():
            try:
                if recovering:
                    await br.connect(resume_id=SID, recovering=True)
                else:
                    await br.connect()
            finally:
                if br._dispatcher_task is not None:
                    br._dispatcher_task.cancel()
                    await asyncio.gather(br._dispatcher_task,
                                         return_exceptions=True)

        asyncio.run(go())
        notices = [(m.get("subtype"), (m.get("data") or {}).get("message", ""))
                   for m in sent if m.get("type") == "system_msg"
                   and m.get("subtype") in ("warning", "info")]
        return notices, st, dict(_CLI.last.options.env)

    return run


CUT = [prompt("run the suite", 1), call("t1", "Bash", 2)]
DONE = [prompt("run the suite", 1), call("t1", "Bash", 2), result("t1", 3),
        say("All green.", 4)]


def test_opening_it_says_the_turn_is_waiting(opened):
    notices, st, env = opened(CUT)

    assert env["CLAUDE_CODE_RESUME_INTERRUPTED_TURN"] == "0"
    texts = [t for _s, t in notices if "cut off" in t]
    assert len(texts) == 1 and "while running Bash" in texts[0]
    assert "“run the suite”" in texts[0]


def test_it_is_kept_for_a_tab_that_opens_later(opened):
    notices, st, _env = opened(CUT)

    assert [n["data"]["message"] for n in st.open_notices] == \
        [t for _s, t in notices if "cut off" in t]


def test_a_finished_session_says_nothing(opened):
    notices, st, _env = opened(DONE)

    assert not [t for _s, t in notices if "cut off" in t]
    assert st.open_notices == []


def test_a_recovery_says_nothing_of_it(opened):
    """Its CLI finishes the turn, and the ghost turn says so
    (tests/test_resumed_turn_notice.py)."""
    notices, st, env = opened(CUT, recovering=True)

    assert env["CLAUDE_CODE_RESUME_INTERRUPTED_TURN"] == "1"
    assert not [t for _s, t in notices if "cut off" in t]


def test_nor_an_open_that_finishes_it(opened):
    notices, _st, env = opened(CUT, ["--resume-interrupted-turn"])

    assert env["CLAUDE_CODE_RESUME_INTERRUPTED_TURN"] == "1"
    assert not [t for _s, t in notices if "cut off" in t]


def test_the_dead_tasks_come_first_and_the_call_to_action_last(opened):
    notices, st, _env = opened(CUT, lost={"b1": {"name": "Run the full suite"}})
    texts = [t for _s, t in notices]

    assert "Run the full suite" in texts[0] and "cut off" in texts[-1]
    assert len(st.open_notices) == 2
    assert not st.queued_prompts, "the model is told by the CLI, not by a prompt"


def test_a_new_connect_replaces_what_the_last_one_said():
    """A reconnect with nothing to say must not leave the last open's notices
    to be replayed to every tab that attaches."""
    cfg = parse_args([])
    st = init_state_from_config(cfg)

    async def bc(_m):
        pass

    br = SDKBridge(config=cfg, state=st, broadcaster=bc)
    st.open_notices = [{"subtype": "warning", "data": {"message": "stale"}}]

    asyncio.run(br._announce_on_open(None, None))

    assert st.open_notices == []


def test_the_transcript_is_read_before_the_cli_starts(opened, monkeypatch):
    """Anything the new CLI writes on resume is not the old turn."""
    order = []
    real = SDKBridge._read_cut_off_turn

    def spy(self, sid):
        order.append("read")
        return real(self, sid)

    monkeypatch.setattr(SDKBridge, "_read_cut_off_turn", spy)
    monkeypatch.setattr(_CLI, "connect", lambda self: _mark(order))

    opened(CUT)

    assert order == ["read", "connect"]


async def _mark(order):
    order.append("connect")


# --------------------------------------------------------------------------
# ...and not after a turn has started
# --------------------------------------------------------------------------

def test_a_ghost_turn_ends_it(tmp_path):
    cfg = parse_args([])
    st = init_state_from_config(cfg)

    async def bc(_m):
        pass

    br = SDKBridge(config=cfg, state=st, broadcaster=bc)
    st.open_notices = [{"subtype": "warning", "data": {"message": "cut off"}}]

    asyncio.run(br._begin_ghost_turn_if_needed())

    assert st.open_notices == []


def test_a_turn_of_ours_ends_it():
    head = inspect.getsource(SDKBridge.run_turn).split("_prompt_preview")[0]

    assert "state.open_notices = []" in head


# --------------------------------------------------------------------------
# The hub: a tab that attaches later is told too
# --------------------------------------------------------------------------

class _Rt:
    def __init__(self, st, cfg):
        self.state, self.config = st, cfg
        self.rid, self.epoch, self.seq = "s1", "e1", 0

    def meta(self):
        return {"rid": self.rid}


def test_a_tab_that_attaches_later_is_told(monkeypatch):
    cfg = parse_args([])
    st = init_state_from_config(cfg)
    st.session_id = None                   # no history to wait for
    note = {"subtype": "warning", "data": {"message": "This session's last "
                                           "turn was cut off"}}
    st.open_notices = [note]
    rt = _Rt(st, cfg)
    ws = object()
    got: list[dict] = []

    async def send_to(_ws, msg):
        got.append(msg)

    monkeypatch.setattr(server, "send_to", send_to)
    monkeypatch.setattr(server, "_runtime_for_ws", lambda w: rt)
    monkeypatch.setattr(server, "_picker_mode", False)

    asyncio.run(server._send_initial_state(ws))

    assert got[-1] == {"type": "system_msg", **note}
