"""--resume-after-limit: a session a usage limit stops carries on once it resets.

Asked for 2026-10-21: "is there a command-line parameter to read the time a
session limit resets and then continue at that time? if there's not, make one.
it should probably include the previous prompt, with a note saying the prompt
was repeated ... it should probably read rate limit messages that came as the
last message before a resume, too (besides notifications of background tasks
completing and whatever)".  Then: a ``/compact`` that ran into the limit is
one more such message; the existing detection that sets the state to "rate
limited" is to be reused rather than a second one written; and
``/resume-after-limit`` must work issued before or after the limit is
announced, with the limit hit before or after the session was opened.

So: what has a session stopped is the State's rate-limit rejection, which the
CLI's RateLimitEvent sets, and on open the transcript's record of it; the
resume repeats the user's last prompt, framed as a repeat; and a session
opened while stopped by a limit -- with notifications, peers' messages and a
failed ``/compact`` after the error -- is picked up too.  Found on the way:
the bridge took the CLI's limit message for the model answering, so it wiped
the rejection (and its reset time) the instant it arrived, and a locked-out
session read "idle".
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock  # noqa: E402
from claude_agent_sdk.types import RateLimitEvent, RateLimitInfo  # noqa: E402

import limit_resume  # noqa: E402
import sdk_bridge  # noqa: E402
import server  # noqa: E402
import wakeup_store  # noqa: E402
from commands import classify  # noqa: E402
from config import SLASH_COMMANDS, Config, parse_args  # noqa: E402
from sdk_bridge import SDKBridge  # noqa: E402
from session import _classify_user_text  # noqa: E402
from state import init_state_from_config, state_to_status_dict  # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"
NEW_SID = "99999999-8888-7777-6666-555555555555"


def _iso(ts: float) -> str:
    from datetime import timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _at(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


# ---------------------------------------------------------------------------
# Transcript records, shaped like the CLI's (2.1.280 / 2.1.301)
# ---------------------------------------------------------------------------

LIMIT_TEXT = "You've hit your session limit · resets 9:30am (America/New_York)"


def _limit(ts, reset, kind="five_hour", text=LIMIT_TEXT, **quota):
    q = {"status": "rejected", "resetsAt": reset, "rateLimitType": kind,
         "overageStatus": "rejected", "isUsingOverage": False, **quota}
    return {"type": "assistant", "timestamp": _iso(ts), "isApiErrorMessage": True,
            "error": "rate_limit", "apiErrorStatus": 429, "quotaLimits": q,
            "message": {"model": "<synthetic>", "role": "assistant",
                        "content": [{"type": "text", "text": text}]}}


def _prompt(text, ts, **extra):
    return {"type": "user", "timestamp": _iso(ts),
            "message": {"role": "user", "content": text}, **extra}


def _reply(text, ts):
    return {"type": "assistant", "timestamp": _iso(ts),
            "message": {"model": "claude-opus-5-5", "role": "assistant",
                        "content": [{"type": "text", "text": text}]}}


def _tool_use(tid, ts):
    return {"type": "assistant", "timestamp": _iso(ts),
            "message": {"model": "claude-opus-5-5", "role": "assistant",
                        "content": [{"type": "tool_use", "id": tid, "name": "Bash",
                                     "input": {"command": "ls"}}]}}


def _tool_result(tid, ts):
    return {"type": "user", "timestamp": _iso(ts),
            "message": {"role": "user", "content": [
                {"tool_use_id": tid, "type": "tool_result", "content": "ok"}]}}


def _notification(ts):
    return _prompt("<task-notification>\n<task-id>b1</task-id>\n</task-notification>",
                   ts, origin={"kind": "task-notification"})


def _peer(ts):
    return _prompt("Another Claude session sent a message:\n<cross-session-message>"
                   "hi</cross-session-message>", ts, isMeta=True,
                   origin={"kind": "peer", "name": "Lane-F"})


def _queue_op(ts):
    return {"type": "queue-operation", "operation": "enqueue", "timestamp": _iso(ts)}


def _failed_compact(ts, text=LIMIT_TEXT):
    """A /compact that ran into the limit, as 2.1.301 records it: the caveat,
    the command, and its error as a local_command system record."""
    return [
        _prompt("<local-command-caveat>Caveat: The messages below were generated "
                "by the user while running local commands.</local-command-caveat>",
                ts, isMeta=True),
        _prompt("<command-name>/compact</command-name>\n            "
                "<command-message>compact</command-message>\n            "
                "<command-args></command-args>", ts),
        {"type": "system", "subtype": "local_command", "timestamp": _iso(ts),
         "isMeta": False, "level": "info",
         "content": f"<local-command-stderr>Error during compaction: {text}"
                    f"</local-command-stderr>"},
    ]


def _write(path: Path, records) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                    encoding="utf-8")
    return path


#: Now, and a limit's reset an hour after it: set as each test starts
#: (_fresh_clock).  Taken once, at import, the reset has passed by the time a
#: long suite reaches the tests that need it ahead.
NOW = 0.0
RESET = 0


@pytest.fixture(autouse=True)
def _fresh_clock():
    global NOW, RESET
    NOW = time.time()
    RESET = int(NOW) + 3600


# ---------------------------------------------------------------------------
# What stopped the session
# ---------------------------------------------------------------------------

def test_a_session_whose_last_word_is_a_limit_is_found(tmp_path):
    p = _write(tmp_path / "s.jsonl", [_prompt("build it", NOW - 600),
                                      _reply("working", NOW - 500),
                                      _limit(NOW - 400, RESET)])
    stop = limit_resume.limit_stop_in(p)
    assert stop["resets_at"] == RESET and stop["kind"] == "five_hour"
    assert stop["hit_at"] == pytest.approx(_at(_iso(NOW - 400)))


def test_what_arrived_while_it_was_stopped_is_looked_past(tmp_path):
    """The case the request named: notifications and the like after the error,
    each of which ran into the limit again."""
    p = _write(tmp_path / "s.jsonl", [
        _prompt("build it", NOW - 900), _limit(NOW - 800, RESET),
        _queue_op(NOW - 700), _notification(NOW - 700), _limit(NOW - 690, RESET),
        _peer(NOW - 600), _limit(NOW - 590, RESET), _queue_op(NOW - 580),
        _notification(NOW - 500),
    ])
    stop = limit_resume.limit_stop_in(p)
    assert stop["resets_at"] == RESET
    assert stop["hit_at"] == pytest.approx(_at(_iso(NOW - 800))), \
        "when it was stopped, not the last retry"


def test_a_compaction_that_ran_into_the_limit_is_looked_past(tmp_path):
    """The sequence reported: the limit, then "Error during compaction:
    You've hit your session limit", then a task's notification."""
    p = _write(tmp_path / "s.jsonl", [
        _prompt("go", NOW - 900), _tool_use("t1", NOW - 800),
        _tool_result("t1", NOW - 790), _limit(NOW - 780, RESET),
        _queue_op(NOW - 700), *_failed_compact(NOW - 700), _notification(NOW - 600),
    ])
    stop = limit_resume.limit_stop_in(p)
    assert stop is not None and stop["resets_at"] == RESET, \
        "the quota's reset, not one read from the text"


def test_a_later_limit_named_only_in_text_wins(tmp_path):
    """The session limit's error, then a /compact hours later that ran into
    the weekly one: the weekly one is what still holds."""
    at = datetime(2026, 10, 21, 8, 0).astimezone().timestamp()
    weekly = f"You've hit your weekly limit · resets Oct 25, 6pm ({NY})"
    p = _write(tmp_path / "s.jsonl", [_prompt("go", at - 900),
                                      _limit(at - 800, int(at) - 60),
                                      *_failed_compact(at, weekly)])
    stop = limit_resume.limit_stop_in(p)
    assert stop["kind"] == "seven_day"
    assert stop["resets_at"] == _ny(2026, 10, 25, 18, 0)


def test_a_compaction_alone_is_read_for_its_reset(tmp_path):
    """No limit error before it -- the /compact was the first thing to run
    into the limit -- so the reset comes from its text."""
    at = datetime(2026, 10, 21, 8, 0).astimezone().timestamp()
    p = _write(tmp_path / "s.jsonl", [_prompt("go", at - 600), _reply("done", at - 300),
                                      *_failed_compact(at)])
    stop = limit_resume.limit_stop_in(p)
    assert stop is not None and stop["kind"] == "five_hour"
    want = limit_resume.reset_from_text(LIMIT_TEXT, at)
    assert stop["resets_at"] == want and want > at


def test_a_compaction_failing_for_another_reason_is_not_a_limit(tmp_path):
    p = _write(tmp_path / "s.jsonl", [_prompt("go", NOW - 600), _reply("done", NOW - 300),
                                      *_failed_compact(NOW - 10, "Not enough messages")])
    assert limit_resume.limit_stop_in(p) is None


def test_a_prompt_after_the_limit_means_it_moved_on(tmp_path):
    p = _write(tmp_path / "s.jsonl", [_prompt("build it", NOW - 900),
                                      _limit(NOW - 800, RESET),
                                      _prompt("carry on", NOW - 10)])
    assert limit_resume.limit_stop_in(p) is None


def test_a_reply_after_the_limit_means_it_moved_on(tmp_path):
    p = _write(tmp_path / "s.jsonl", [_limit(NOW - 800, RESET),
                                      _notification(NOW - 700),
                                      _reply("done", NOW - 690)])
    assert limit_resume.limit_stop_in(p) is None


def test_a_limit_in_the_middle_of_a_turn_is_found(tmp_path):
    """A lane stopped between a tool's result and the next call."""
    p = _write(tmp_path / "s.jsonl", [_prompt("go", NOW - 900),
                                      _tool_use("t1", NOW - 800),
                                      _tool_result("t1", NOW - 790),
                                      _limit(NOW - 780, RESET, kind="seven_day")])
    stop = limit_resume.limit_stop_in(p)
    assert stop is not None and stop["kind"] == "seven_day"


def test_another_api_error_is_not_a_limit(tmp_path):
    rec = _limit(NOW - 10, RESET)
    rec["error"] = "server_error"
    rec.pop("quotaLimits")
    p = _write(tmp_path / "s.jsonl", [_prompt("go", NOW - 900), rec])
    assert limit_resume.limit_stop_in(p) is None


def test_a_limit_with_no_quota_is_read_for_its_reset(tmp_path):
    p = _write(tmp_path / "s.jsonl", [_prompt("go", NOW - 900),
                                      _limit(NOW - 10, None)])
    stop = limit_resume.limit_stop_in(p)
    assert stop is not None and stop["resets_at"] > NOW - 10


@pytest.mark.parametrize("quota, text", [
    ({"resetsAt": None}, "You've hit your session limit"),     # no time at all
    ({"status": "allowed"}, LIMIT_TEXT),                        # not a rejection
])
def test_a_limit_with_no_reset_to_wait_for_is_none(tmp_path, quota, text):
    p = _write(tmp_path / "s.jsonl", [_prompt("go", NOW - 900),
                                      _limit(NOW - 10, RESET, text=text, **quota)])
    assert limit_resume.limit_stop_in(p) is None


def test_the_plans_reset_is_used_not_extra_usages(tmp_path):
    """Extra usage resetting earlier is billed; resuming onto it would spend
    money nobody chose to spend."""
    p = _write(tmp_path / "s.jsonl", [
        _prompt("go", NOW - 900),
        _limit(NOW - 10, RESET, overageResetsAt=RESET - 1800,
               overageDisabledReason="out_of_credits")])
    assert limit_resume.limit_stop_in(p)["resets_at"] == RESET


def test_the_quota_is_read_from_the_error_params_too(tmp_path):
    rec = _limit(NOW - 10, RESET)
    q = rec.pop("quotaLimits")
    rec["apiError"] = "usage_limit_reached"
    rec["apiErrorParams"] = {"rate_limit_info": q}
    p = _write(tmp_path / "s.jsonl", [_prompt("go", NOW - 900), rec])
    assert limit_resume.limit_stop_in(p)["resets_at"] == RESET


def test_the_clis_resume_bookkeeping_is_looked_past(tmp_path):
    p = _write(tmp_path / "s.jsonl", [
        _prompt("go", NOW - 900), _limit(NOW - 800, RESET),
        _prompt("Continue from where you left off.", NOW - 10, isMeta=True),
        {"type": "assistant", "timestamp": _iso(NOW - 9),
         "message": {"model": "<synthetic>", "role": "assistant",
                     "content": [{"type": "text", "text": "No response requested."}]}},
    ])
    assert limit_resume.limit_stop_in(p) is not None


# ---------------------------------------------------------------------------
# A reset read from the CLI's text
# ---------------------------------------------------------------------------

NY = "America/New_York"


def _ny(*args):
    from zoneinfo import ZoneInfo
    return datetime(*args, tzinfo=ZoneInfo(NY)).timestamp()


@pytest.mark.parametrize("text, at, want", [
    (f"resets 9:30am ({NY})", _ny(2026, 10, 21, 8, 0), _ny(2026, 10, 21, 9, 30)),
    (f"resets 9pm ({NY})", _ny(2026, 10, 21, 8, 0), _ny(2026, 10, 21, 21, 0)),
    # A clock time already past today is tomorrow's.
    (f"resets 9:30am ({NY})", _ny(2026, 10, 21, 10, 0), _ny(2026, 10, 22, 9, 30)),
    (f"resets Oct 23, 9am ({NY})", _ny(2026, 10, 21, 8, 0), _ny(2026, 10, 23, 9, 0)),
    (f"resets Jan 2, 2027, 6pm ({NY})", _ny(2026, 12, 30, 8, 0), _ny(2027, 1, 2, 18, 0)),
    # A date with no year, already past, is next year's.
    (f"resets Jan 2, 6pm ({NY})", _ny(2026, 12, 30, 8, 0), _ny(2027, 1, 2, 18, 0)),
])
def test_the_clis_reset_texts_are_read(text, at, want):
    assert limit_resume.reset_from_text("You've hit your weekly limit · " + text, at) == want


def test_a_reset_with_no_zone_is_local_time():
    at = datetime(2026, 10, 21, 8, 0).astimezone().timestamp()
    want = datetime(2026, 10, 21, 21, 0).astimezone().timestamp()
    assert limit_resume.reset_from_text("resets 9pm", at) == want


def test_an_unknown_zone_falls_back_to_local_time():
    at = datetime(2026, 10, 21, 8, 0).astimezone().timestamp()
    assert limit_resume.reset_from_text("resets 9pm (Nowhere/Atlantis)", at) == \
        limit_resume.reset_from_text("resets 9pm", at)


# ---------------------------------------------------------------------------
# The prompt it was given
# ---------------------------------------------------------------------------

def test_the_last_prompt_is_the_users_not_what_came_after(tmp_path):
    earlier_resume = limit_resume.build_resume_prompt(
        kind="five_hour", hit_at=None, resets_at=None,
        prompt={"text": "the real prompt", "at": None})
    p = _write(tmp_path / "s.jsonl", [
        _prompt("an older prompt", NOW - 2000),
        _peer(NOW - 1500),
        _prompt("the real prompt", NOW - 1000),
        _tool_use("t1", NOW - 990), _tool_result("t1", NOW - 980),
        _prompt("<command-name>/usage</command-name>", NOW - 970),
        _notification(NOW - 960),
        _prompt("This session is being continued from a previous conversation",
                NOW - 940, isCompactSummary=True),
        _prompt("Autonomous-loop wakeup (scheduled by you). Resume work now",
                NOW - 930),
        _prompt(earlier_resume, NOW - 920),
        _limit(NOW - 910, RESET),
    ])
    found = limit_resume.last_prompt_in(p)
    assert found == {"text": "the real prompt", "at": _iso(NOW - 1000), "from": None}


def test_a_session_note_is_not_part_of_the_prompt(tmp_path):
    """/move's one-shot note goes in front of the next prompt; the resume
    repeats the prompt, not the note."""
    p = _write(tmp_path / "s.jsonl", [
        _prompt("[orchestrator2] This session was moved to D:\\x.\n\nport it", NOW - 100),
        _limit(NOW - 10, RESET)])
    assert limit_resume.last_prompt_in(p)["text"] == "port it"


def test_a_resume_that_carried_a_note_is_still_passed_over(tmp_path):
    br_note = "This session was moved."
    resume = limit_resume.build_resume_prompt(kind=None, hit_at=None,
                                              resets_at=None, prompt=None)
    p = _write(tmp_path / "s.jsonl", [
        _prompt("the real prompt", NOW - 200),
        _prompt(f"{resume}\n\n[orchestrator2] {br_note}", NOW - 100),
        _limit(NOW - 10, RESET)])
    assert limit_resume.last_prompt_in(p)["text"] == "the real prompt"


def test_a_prompt_far_back_is_found_across_chunks(tmp_path):
    """Read backwards in chunks: a line cut by a chunk boundary is joined, not
    lost or misparsed."""
    filler = [_tool_result(f"t{i}", NOW - 500 + i) for i in range(60)]
    p = _write(tmp_path / "s.jsonl", [_prompt("x" * 300 + " the one", NOW - 1000),
                                      *filler, _limit(NOW, RESET)])
    for chunk in (97, 211, 1000, 10**6):
        found = limit_resume.last_prompt_in(p, chunk=chunk)
        assert found is not None and found["text"].endswith(" the one"), chunk


def test_a_prompt_beyond_the_limit_is_not_searched_for(tmp_path):
    filler = [_tool_result(f"t{i}", NOW - 500 + i) for i in range(60)]
    p = _write(tmp_path / "s.jsonl", [_prompt("too far", NOW - 1000), *filler])
    assert limit_resume.last_prompt_in(p, limit=2000, chunk=500) is None


# ---------------------------------------------------------------------------
# The resume prompt
# ---------------------------------------------------------------------------

def test_the_resume_repeats_the_prompt_and_says_it_is_a_repeat():
    text = limit_resume.build_resume_prompt(
        kind="seven_day", hit_at=NOW - 3600, resets_at=NOW - 60,
        prompt={"text": "port the scheduler\nand test it", "at": _iso(NOW - 7200)})
    assert text.startswith(limit_resume.PROMPT_PREFIX)
    assert "weekly limit" in text
    assert "Nobody typed this message" in text
    assert "<repeated-prompt" in text and "port the scheduler\nand test it" in text
    assert "not a new request" in text
    # Drawn in history as the orchestrator's, not as something the user typed.
    assert _classify_user_text(text) == "injected_prompt"


def test_a_huge_prompt_is_repeated_in_part():
    big = "y" * (limit_resume.REPEAT_MAX_CHARS + 500)
    text = limit_resume.build_resume_prompt(kind=None, hit_at=None, resets_at=None,
                                            prompt={"text": big, "at": None})
    assert "500 more characters" in text
    assert len(text) < limit_resume.REPEAT_MAX_CHARS + 2000


def test_with_no_prompt_to_repeat_there_is_no_block():
    text = limit_resume.build_resume_prompt(kind=None, hit_at=None, resets_at=None,
                                            prompt=None)
    assert "<repeated-prompt" not in text and "usage limit" in text


# ---------------------------------------------------------------------------
# The record on disk
# ---------------------------------------------------------------------------

def test_a_record_round_trips_and_is_cleared(tmp_path):
    limit_resume.save(str(tmp_path), SID, due_at=RESET + 60, resets_at=RESET,
                      settings={"model": "claude-opus-5-5", "effort": "max",
                                "cli_path": None})
    rec = limit_resume.load(str(tmp_path), SID)
    assert rec["due_at"] == RESET + 60 and rec["session_id"] == SID
    assert rec["model"] == "claude-opus-5-5" and rec["effort"] == "max"
    assert "cli_path" not in rec
    assert [r["session_id"] for r in limit_resume.iter_records()] == [SID]
    limit_resume.clear(str(tmp_path), SID)
    assert limit_resume.load(str(tmp_path), SID) is None


def test_a_record_out_of_its_slot_is_ignored(tmp_path):
    limit_resume.save(str(tmp_path), SID, due_at=RESET, resets_at=RESET)
    path = limit_resume._store.path_for(str(tmp_path), SID)
    path.rename(path.with_name("elsewhere__x.json"))
    assert list(limit_resume.iter_records()) == []


@pytest.mark.parametrize("due_in, reopen", [
    (600, True),                                   # waiting
    (-600, True),                                  # due while the hub was down
    (-(limit_resume.LATE_S + 60), False),          # a half-day-old plan
    (limit_resume.MAX_AHEAD_S + 60, False),        # no limit lasts that long
])
def test_which_records_reopen_their_session(due_in, reopen):
    rec = {"session_id": SID, "cwd": "C:/w", "due_at": NOW + due_in}
    plan = limit_resume.plan_reopen(rec, now=NOW)
    assert plan.reopen is reopen
    if reopen:
        assert plan.delay == max(0.0, due_in)


def test_a_session_reopened_too_often_is_dropped(tmp_path):
    """Review: one that cannot connect never schedules its resume, so every
    idle teardown would reopen it again."""
    limit_resume.save(str(tmp_path), SID, due_at=NOW - 5, resets_at=NOW - 65)
    for _ in range(limit_resume.MAX_REOPENS):
        rec = limit_resume.load(str(tmp_path), SID)
        assert limit_resume.plan_reopen(rec).reopen
        limit_resume.note_reopened(rec)
    assert not limit_resume.plan_reopen(limit_resume.load(str(tmp_path), SID)).reopen


def test_junk_is_not_a_record():
    assert limit_resume.plan_reopen({"cwd": "x"}).reopen is False


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------

def _bridge(tmp_path, monkeypatch, *argv):
    cfg = parse_args(["--cwd", str(tmp_path), *argv])
    st = init_state_from_config(cfg)
    st.session_id = SID
    sent: list[dict] = []

    async def bc(m):
        sent.append(m)

    br = SDKBridge(config=cfg, state=st, broadcaster=bc)
    monkeypatch.setattr(sdk_bridge, "find_session_dir", lambda sid, cd=None: tmp_path)
    monkeypatch.setattr(sdk_bridge, "TURN_END_SETTLE_S", 0.0)
    return br, st, sent


def _texts(sent):
    return " | ".join(m.get("data", {}).get("message", "") for m in sent
                      if m.get("type") == "system_msg")


def _event(reset, kind="five_hour"):
    return RateLimitEvent(
        rate_limit_info=RateLimitInfo(status="rejected", resets_at=reset,
                                      rate_limit_type=kind),
        uuid="u", session_id=SID)


def _result(error=True):
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=0,
                         is_error=error, num_turns=1, session_id=SID,
                         api_error_status=429 if error else None)


class _Client:
    """A CLI whose turns run into a usage limit -- or answer, with *reply*;
    or end on a synthetic error that is not a limit, with *other_error*; or
    are a /compact that runs into it (an event and no message at all)."""

    def __init__(self, br, reset, reply=None, other_error=None, compact=False,
                 event=True):
        self.br, self.reset, self.reply = br, reset, reply
        self.other_error, self.compact, self.event = other_error, compact, event

    async def query(self, _prompt):
        q = self.br.turn_msg_queue
        if self.reply is not None:
            q.put_nowait(AssistantMessage(content=[TextBlock(text=self.reply)],
                                          model="claude-opus-5-5"))
            q.put_nowait(_result(error=False))
            return
        if self.other_error is not None:
            q.put_nowait(AssistantMessage(content=[TextBlock(text=self.other_error)],
                                          model="<synthetic>", error="unknown"))
            q.put_nowait(_result())
            return
        if self.event:
            q.put_nowait(_event(self.reset))
        if not self.compact:
            q.put_nowait(AssistantMessage(
                content=[TextBlock(text="You've hit your session limit · resets 9pm")],
                model="<synthetic>", error="rate_limit"))
        q.put_nowait(_result())

    async def interrupt(self):  # pragma: no cover
        pass


def _stop_timers(br):
    br._cancel_limit_resume(forget=False)


def test_the_limit_message_does_not_wipe_the_rejection(tmp_path, monkeypatch):
    """The bug found on the way: the synthetic message was taken for the model
    answering, so the reset time was gone before the turn ended."""
    br, st, _ = _bridge(tmp_path, monkeypatch)
    br.client = _Client(br, RESET)
    asyncio.run(br.run_turn("go"))
    assert st.rate_limit_status == "rejected"
    assert st.rate_limit_resets_at == RESET
    assert state_to_status_dict(st, br.config)["busy_label"] == "rate limited"


def test_off_by_default_it_only_says_it_could(tmp_path, monkeypatch):
    br, st, sent = _bridge(tmp_path, monkeypatch)
    br.client = _Client(br, RESET)

    async def go():
        await br.run_turn("go")
        await br.run_turn("again")       # the same limit: said once

    asyncio.run(go())
    assert br._limit_task is None and st.limit_resume_at is None
    assert limit_resume.load(str(tmp_path), SID) is None
    assert _texts(sent).count("/resume-after-limit on") == 1


def test_on_it_schedules_a_resume_for_after_the_reset(tmp_path, monkeypatch):
    br, st, sent = _bridge(tmp_path, monkeypatch, "--resume-after-limit",
                           "--model", "claude-opus-5-5")
    br.client = _Client(br, RESET)

    async def go():
        await br.run_turn("go")
        try:
            assert br._limit_task is not None
            assert st.limit_resume_at == RESET + limit_resume.MARGIN_S
            rec = limit_resume.load(str(tmp_path), SID)
            assert rec is not None and rec["due_at"] == RESET + limit_resume.MARGIN_S
            assert rec["model"] == "claude-opus-5-5" and rec["effort"] == "auto"
            assert "carries on by itself" in _texts(sent)
            assert "resumes" in state_to_status_dict(st, br.config)["busy_label"]
            # Another turn running into the same limit leaves it be.
            n = len(sent)
            task = br._limit_task
            await br.run_turn("again")
            assert br._limit_task is task
            assert "carries on by itself" not in _texts(sent[n:])
        finally:
            _stop_timers(br)

    asyncio.run(go())


def test_a_compact_that_runs_into_the_limit_arms_it_too(tmp_path, monkeypatch):
    """No synthetic message at all: only the RateLimitEvent, which is why the
    State's rejection, not the message, is what is read."""
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")
    br.client = _Client(br, RESET, compact=True)

    async def go():
        await br.run_turn("/compact")
        try:
            assert br._limit_task is not None
        finally:
            _stop_timers(br)

    asyncio.run(go())


def test_a_repeat_hit_without_an_event_still_counts(tmp_path, monkeypatch):
    """The CLI sends a RateLimitEvent only when its limit state changes; a
    second hit has none, and the State still says rejected."""
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")

    async def go():
        br.client = _Client(br, RESET)
        await br.run_turn("go")
        _stop_timers(br)
        br.client = _Client(br, RESET, event=False)
        await br.run_turn("again")
        try:
            assert br._limit_task is not None
        finally:
            _stop_timers(br)

    asyncio.run(go())


def test_a_reply_ends_the_limit_and_the_pending_resume(tmp_path, monkeypatch):
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")

    async def go():
        br.client = _Client(br, RESET)
        await br.run_turn("go")
        assert br._limit_task is not None
        br.client = _Client(br, RESET, reply="back at it")
        await br.run_turn("carry on")

    asyncio.run(go())
    assert br._limit_task is None and br._stopped_by_limit() is None
    assert limit_resume.load(str(tmp_path), SID) is None
    assert st.rate_limit_status == "allowed"


def test_another_synthetic_error_does_not_end_the_limit(tmp_path, monkeypatch):
    """Review: a background task's turn failing on "API Error: Connection
    error" is not the model answering, and cancelled the pending resume."""
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")

    async def go():
        br.client = _Client(br, RESET)
        await br.run_turn("go")
        task = br._limit_task
        br.client = _Client(br, RESET, other_error="API Error: Connection error.")
        await br.run_turn("again")
        try:
            assert br._limit_task is task and st.rate_limit_status == "rejected"
            assert limit_resume.load(str(tmp_path), SID) is not None
        finally:
            _stop_timers(br)

    asyncio.run(go())


def test_a_background_tasks_turn_into_the_limit_arms_it_too(tmp_path, monkeypatch):
    """The commonest way a session is stopped while nobody watches: the turn
    that runs into the limit is the CLI's own, for a task's notification."""
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")

    async def go():
        await br._handle_async_message(_event(RESET, "seven_day"))
        await br._handle_async_message(AssistantMessage(
            content=[TextBlock(text="You've hit your weekly limit")],
            model="<synthetic>", error="rate_limit"))
        await br._handle_async_message(_result())
        try:
            assert br._limit_task is not None
            assert br._stopped_by_limit()["kind"] == "seven_day"
        finally:
            _stop_timers(br)

    asyncio.run(go())


def test_the_cli_is_told_apart_from_the_model():
    assert SDKBridge._is_synthetic(AssistantMessage(
        content=[TextBlock(text="You've hit your weekly limit · x")], model="<synthetic>"))
    assert SDKBridge._is_synthetic(AssistantMessage(
        content=[TextBlock(text="x")], model="claude-opus-5-5", error="server_error"))
    assert not SDKBridge._is_synthetic(AssistantMessage(
        content=[TextBlock(text="You've hit your weekly limit, I see")],
        model="claude-opus-5-5"))


def _due_now(monkeypatch):
    monkeypatch.setattr(limit_resume, "MARGIN_S", 0.0)
    monkeypatch.setattr(limit_resume, "SETTLE_S", 0.0)
    monkeypatch.setattr(limit_resume, "DEFER_S", 0.01)


def test_when_due_the_resume_is_sent_repeating_the_prompt(tmp_path, monkeypatch):
    _due_now(monkeypatch)
    _write(tmp_path / f"{SID}.jsonl", [_prompt("render the flyby", NOW - 900),
                                       _limit(NOW - 800, int(NOW) - 5)])
    br, st, sent = _bridge(tmp_path, monkeypatch, "--resume-after-limit")
    br.client = object()

    async def go():
        await br._limit_on_open(SID, limit_resume.limit_stop_in(
            tmp_path / f"{SID}.jsonl"))
        await asyncio.wait_for(br._limit_task, 5)
        return br.event_queue.get_nowait()

    kind, text = asyncio.run(go())
    assert kind == "message" and text.startswith(limit_resume.PROMPT_PREFIX)
    assert "render the flyby" in text
    assert any(m.get("type") == "injected_prompt" and m["content"] == text for m in sent)
    assert limit_resume.load(str(tmp_path), SID) is None
    assert st.limit_resume_at is None and br._limit_resume_queued is True


def test_a_resume_waits_for_a_running_turn(tmp_path, monkeypatch):
    _due_now(monkeypatch)
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")
    br.client = object()
    br._adopt_limit_stop({"resets_at": int(NOW) - 5, "kind": None, "hit_at": None})
    st.busy = True

    async def go():
        await br._arm_limit_resume(br._stopped_by_limit())
        await asyncio.sleep(0.05)
        assert br.event_queue.empty(), "sent into a running turn"
        assert br._limit_defers >= 1
        st.busy = False
        await asyncio.wait_for(br._limit_task, 5)
        return br.event_queue.get_nowait()

    assert asyncio.run(go())[0] == "message"


def _resumes_into_the_limit(br, reset, n):
    async def go():
        dues = []
        for _ in range(n):
            br._adopt_limit_stop({"resets_at": reset, "kind": "five_hour",
                                  "hit_at": None})
            br._limit_sent_for = reset
            await br._after_limit_turn(sent=True)
            dues.append(br.state.limit_resume_at)
            _stop_timers(br)
        return dues
    return go()


def test_resumes_into_the_same_limit_back_off_then_stop(tmp_path, monkeypatch):
    br, st, sent = _bridge(tmp_path, monkeypatch, "--resume-after-limit")
    dues = asyncio.run(_resumes_into_the_limit(br, int(NOW) - 5,
                                               limit_resume.MAX_ATTEMPTS))
    waits = [d - time.time() for d in dues[:-1]]
    assert all(b > a for a, b in zip(waits, waits[1:])), "each wait longer"
    assert dues[-1] is None, "kept resuming into the same limit"
    assert "stopped resuming by itself" in _texts(sent)


def test_giving_up_holds_until_a_later_limit_or_asked(tmp_path, monkeypatch):
    """Review: after giving up, the next notification's turn into the same
    limit started a fresh five attempts."""
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")
    reset = int(NOW) - 5

    async def go():
        await _resumes_into_the_limit(br, reset, limit_resume.MAX_ATTEMPTS)
        # A ghost turn into the same limit, then a reconnect reading it again.
        await br._after_limit_turn()
        assert br._limit_task is None
        await br._limit_on_open(SID, {"resets_at": reset, "kind": "five_hour",
                                      "hit_at": None})
        assert br._limit_task is None
        # Asked, it tries again.
        await br.set_resume_after_limit(True)
        try:
            assert br._limit_task is not None
        finally:
            _stop_timers(br)
        # And a later limit is resumed by itself.
        br._limit_gave_up_for = reset
        br._adopt_limit_stop({"resets_at": RESET, "kind": "seven_day", "hit_at": None})
        await br._after_limit_turn()
        try:
            assert br._limit_task is not None
        finally:
            _stop_timers(br)

    asyncio.run(go())


def test_an_interrupted_resume_is_not_an_attempt(tmp_path, monkeypatch):
    """Review: the user stopping the resume before it said anything counted
    as the resume running back into the limit, and it was sent again."""
    br, st, sent = _bridge(tmp_path, monkeypatch, "--resume-after-limit")

    async def go():
        br._adopt_limit_stop({"resets_at": int(NOW) - 5, "kind": None, "hit_at": None})
        br._limit_sent_for = int(NOW) - 5
        await br._after_limit_turn(sent=True, interrupted=True)

    asyncio.run(go())
    assert br._limit_task is None and br._limit_attempts == 0
    assert "ran straight back" not in _texts(sent)


def test_a_later_limit_starts_the_count_again(tmp_path, monkeypatch):
    """The session limit reset, and the resume ran into the weekly one."""
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")

    async def go():
        br._limit_attempts = 3
        br._adopt_limit_stop({"resets_at": RESET, "kind": "seven_day", "hit_at": None})
        br._limit_sent_for = int(NOW) - 5
        await br._after_limit_turn(sent=True)
        try:
            assert br._limit_attempts == 0
            assert st.limit_resume_at == RESET + limit_resume.MARGIN_S
        finally:
            _stop_timers(br)

    asyncio.run(go())


def test_the_notice_is_kept_for_tabs_that_attach_later(tmp_path, monkeypatch):
    """Review: a ghost turn clears the open notices, and the armed resume was
    not announced again, so a tab opening the session later was not told."""
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")

    async def go():
        br._adopt_limit_stop({"resets_at": RESET, "kind": None, "hit_at": None})
        await br._after_limit_turn()
        st.open_notices = []                  # a ghost turn began
        await br._after_limit_turn()          # ...and ran into the limit too
        try:
            assert any("carries on by itself" in n["data"]["message"]
                       for n in st.open_notices)
        finally:
            _stop_timers(br)

    asyncio.run(go())


def test_a_new_session_id_takes_the_schedules_with_it(tmp_path, monkeypatch):
    """Review: the rolling context trim gives the session a new id, and the
    record stayed under the old one, cleared by nothing."""
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")

    async def go():
        br._adopt_limit_stop({"resets_at": RESET, "kind": None, "hit_at": None})
        await br._arm_limit_resume(br._stopped_by_limit())
        br._arm_wakeup(600, "loop on")
        br._take_session_id(NEW_SID)
        try:
            assert limit_resume.load(str(tmp_path), SID) is None
            assert limit_resume.load(str(tmp_path), NEW_SID)["due_at"] == \
                RESET + limit_resume.MARGIN_S
            assert wakeup_store.load_wakeup(str(tmp_path), SID) is None
            assert wakeup_store.load_wakeup(str(tmp_path), NEW_SID)["prompt"] == "loop on"
            br._cancel_limit_resume()
            br._cancel_wakeup()
            assert limit_resume.load(str(tmp_path), NEW_SID) is None
            assert wakeup_store.load_wakeup(str(tmp_path), NEW_SID) is None
        finally:
            _stop_timers(br)
            br._cancel_wakeup(forget=False)

    asyncio.run(go())


def test_a_resume_with_a_session_note_still_begins_as_a_resume(tmp_path, monkeypatch):
    """Review: after /move the note went in front, and the recorded resume
    read as something the user typed."""
    br, st, _ = _bridge(tmp_path, monkeypatch)
    br._session_note = "This session was moved."
    resume = limit_resume.build_resume_prompt(kind=None, hit_at=None,
                                              resets_at=None, prompt=None)
    sent = br._with_session_note(resume)
    assert sent.startswith(limit_resume.PROMPT_PREFIX)
    assert sent.endswith("[orchestrator2] This session was moved.")
    br._session_note = "Moved."
    assert br._with_session_note("hi") == "[orchestrator2] Moved.\n\nhi"


def test_the_check_on_open_never_breaks_a_connect(tmp_path, monkeypatch):
    """Review: an exception in it escaped connect() and could kill the
    worker."""
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")

    async def boom(*a, **k):
        raise TypeError("int() argument must be ... not 'NoneType'")

    monkeypatch.setattr(br, "_limit_on_open", boom)
    asyncio.run(br._announce_on_open(SID, None, {"resets_at": RESET}))


# --- /resume-after-limit, whenever it is said ---

def _hit_live(br):
    br.client = _Client(br, RESET)
    return br.run_turn("go")


def test_on_before_a_live_limit(tmp_path, monkeypatch):
    br, st, _ = _bridge(tmp_path, monkeypatch)

    async def go():
        await br.set_resume_after_limit(True)
        await _hit_live(br)
        try:
            assert br._limit_task is not None
        finally:
            _stop_timers(br)

    asyncio.run(go())


def test_on_after_a_live_limit_was_announced(tmp_path, monkeypatch):
    br, st, sent = _bridge(tmp_path, monkeypatch)

    async def go():
        await _hit_live(br)
        assert "/resume-after-limit on" in _texts(sent) and br._limit_task is None
        said = await br.set_resume_after_limit(True)
        try:
            assert br._limit_task is not None
            assert said == "Resume after limit: on for this session."
        finally:
            _stop_timers(br)

    asyncio.run(go())


def _stopped_transcript(tmp_path, reset=None):
    return _write(tmp_path / f"{SID}.jsonl", [_prompt("go", NOW - 900),
                                              _limit(NOW - 800, RESET if reset is None else reset)])


def test_on_after_opening_a_session_a_limit_had_stopped(tmp_path, monkeypatch):
    p = _stopped_transcript(tmp_path)
    br, st, sent = _bridge(tmp_path, monkeypatch)

    async def go():
        await br._limit_on_open(SID, limit_resume.limit_stop_in(p))
        assert br._limit_task is None
        assert "/resume-after-limit on" in _texts(sent), "not told it could"
        assert "off for this session. It was stopped" in br.limit_resume_status()
        n = len(sent)
        said = await br.set_resume_after_limit(True)
        try:
            assert br._limit_task is not None
            assert said == "Resume after limit: on for this session."
            assert _texts(sent[n:]).count("carries on by itself") == 1, \
                "the schedule is announced once"
            assert "carries on by itself at" in br.limit_resume_status()
        finally:
            _stop_timers(br)
        off = await br.set_resume_after_limit(False)
        assert br._limit_task is None and "off" in off
        assert limit_resume.load(str(tmp_path), SID) is None
        assert "on carries it on" in br.limit_resume_status()

    asyncio.run(go())


def test_on_while_the_session_was_still_connecting(tmp_path, monkeypatch):
    """Said before the open's check had run: it is on by then."""
    p = _stopped_transcript(tmp_path)
    br, st, _ = _bridge(tmp_path, monkeypatch)

    async def go():
        assert "No usage limit" in await br.set_resume_after_limit(True)
        await br._limit_on_open(SID, limit_resume.limit_stop_in(p))
        try:
            assert br._limit_task is not None
        finally:
            _stop_timers(br)

    asyncio.run(go())


def test_on_from_the_start_for_a_session_a_limit_had_stopped(tmp_path, monkeypatch):
    p = _stopped_transcript(tmp_path)
    br, st, sent = _bridge(tmp_path, monkeypatch, "--resume-after-limit")

    async def go():
        await br._limit_on_open(SID, limit_resume.limit_stop_in(p))
        try:
            assert br._limit_task is not None
            assert st.rate_limit_status == "rejected", "a fresh CLI says nothing yet"
            assert "This session was stopped by the 5-hour session limit" in _texts(sent)
            assert st.open_notices, "a tab attaching later is not told"
        finally:
            _stop_timers(br)

    asyncio.run(go())


def test_on_for_a_limit_long_past_resumes_when_asked(tmp_path, monkeypatch):
    late = int(NOW - limit_resume.LATE_S - 600)
    p = _write(tmp_path / f"{SID}.jsonl", [_prompt("go", late - 900),
                                           _limit(late - 800, late)])
    br, st, sent = _bridge(tmp_path, monkeypatch, "--resume-after-limit")

    async def go():
        await br._limit_on_open(SID, limit_resume.limit_stop_in(p))
        assert br._limit_task is None and "too long ago" in _texts(sent)
        assert st.rate_limit_status != "rejected", "a day-old limit is not the State's"
        await br.set_resume_after_limit(True)
        try:
            assert br._limit_task is not None
        finally:
            _stop_timers(br)

    asyncio.run(go())


def test_a_record_turns_it_on_for_the_session_it_names(tmp_path, monkeypatch):
    """The hub reopens a session it scheduled: it was on for that session,
    whatever the hub was started with."""
    p = _stopped_transcript(tmp_path)
    limit_resume.save(str(tmp_path), SID, due_at=RESET + 60, resets_at=RESET)
    br, st, _ = _bridge(tmp_path, monkeypatch)

    async def go():
        await br._limit_on_open(SID, limit_resume.limit_stop_in(p))
        try:
            assert st.resume_after_limit is True and br._limit_task is not None
        finally:
            _stop_timers(br)

    asyncio.run(go())


def test_a_record_for_a_session_that_moved_on_is_dropped(tmp_path, monkeypatch):
    p = _write(tmp_path / f"{SID}.jsonl", [_limit(NOW - 800, RESET),
                                           _reply("carried on elsewhere", NOW - 10)])
    limit_resume.save(str(tmp_path), SID, due_at=RESET + 60, resets_at=RESET)
    br, st, _ = _bridge(tmp_path, monkeypatch)
    asyncio.run(br._limit_on_open(SID, limit_resume.limit_stop_in(p)))
    assert limit_resume.load(str(tmp_path), SID) is None
    assert br._limit_task is None


def test_the_status_says_when_on_but_nothing_is_scheduled(tmp_path, monkeypatch):
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")
    assert "No usage limit has stopped" in br.limit_resume_status()
    br._adopt_limit_stop({"resets_at": RESET, "kind": "seven_day", "hit_at": None})
    assert "nothing is scheduled" in br.limit_resume_status()


@pytest.mark.parametrize("kw, kept", [
    ({}, False),                          # the user closed or left it
    ({"hub_exiting": True}, True),        # restored at the next start
    ({"idle_teardown": True}, True),      # reopened by the hub when due
])
def test_which_stops_keep_the_record(tmp_path, monkeypatch, kw, kept):
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")

    async def go():
        br._adopt_limit_stop({"resets_at": RESET, "kind": None, "hit_at": None})
        await br._arm_limit_resume(br._stopped_by_limit())
        await br._shutdown(**kw)
        assert br._limit_task is None

    asyncio.run(go())
    assert (limit_resume.load(str(tmp_path), SID) is not None) is kept


def test_clear_drops_it(tmp_path, monkeypatch):
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")

    async def go():
        br._adopt_limit_stop({"resets_at": RESET, "kind": None, "hit_at": None})
        await br._arm_limit_resume(br._stopped_by_limit())
        monkeypatch.setattr(br, "disconnect", _anoop)
        monkeypatch.setattr(br, "connect", _anoop)
        await br._clear_context()

    asyncio.run(go())
    assert br._limit_task is None
    assert limit_resume.load(str(tmp_path), SID) is None


async def _anoop(*a, **k):
    return None


# ---------------------------------------------------------------------------
# The status bar
# ---------------------------------------------------------------------------

def test_the_label_says_when_it_resumes():
    st = init_state_from_config(Config())
    st.rate_limit_status, st.rate_limit_resets_at = "rejected", RESET
    st.limit_resume_at = RESET + 60
    label = state_to_status_dict(st, Config())["busy_label"]
    assert label.startswith("rate limited · resumes ")
    st.rate_limit_resets_at = int(NOW) - 5          # reset, not yet resumed
    assert state_to_status_dict(st, Config())["busy_label"].startswith("idle · resumes ")


def test_the_rejection_records_when_it_began():
    from state import apply_rate_limit_info
    st = init_state_from_config(Config())
    apply_rate_limit_info(st, {"status": "rejected", "resets_at": RESET,
                               "rate_limit_type": "five_hour"})
    began = st.rate_limit_hit_at
    assert began and abs(began - time.time()) < 5
    apply_rate_limit_info(st, {"status": "rejected", "resets_at": RESET})
    assert st.rate_limit_hit_at == began, "a repeat is not a new stop"
    apply_rate_limit_info(st, {"status": "allowed"})
    assert st.rate_limit_hit_at is None


# ---------------------------------------------------------------------------
# Flags, the command, the hand-over
# ---------------------------------------------------------------------------

def test_the_flag():
    assert Config().resume_after_limit is False
    assert parse_args([]).resume_after_limit is False
    assert parse_args(["--resume-after-limit"]).resume_after_limit is True
    assert parse_args(["--resume-after-limit",
                       "--no-resume-after-limit"]).resume_after_limit is False
    assert init_state_from_config(parse_args(["--resume-after-limit"])).resume_after_limit


def test_the_command_is_known():
    assert classify("/resume-after-limit on") == ("resume-after-limit", "on")
    assert classify("/resume-after-limit") == ("resume-after-limit", "")
    assert "/resume-after-limit" in SLASH_COMMANDS


def _run_cmd(br, payload, monkeypatch):
    channels = []

    async def send_to(_ws, m):
        channels.append(("send_to", m["data"]["message"]))

    async def bcast(m):
        channels.append(("broadcast", m.get("data", {}).get("message", "")))

    monkeypatch.setattr(server, "send_to", send_to)
    br.broadcast = bcast
    asyncio.run(server._handle_resume_after_limit(object(), br, payload))
    return channels


def test_the_command_answers_status_to_the_asker_and_changes_to_all(tmp_path, monkeypatch):
    br, st, _ = _bridge(tmp_path, monkeypatch)
    assert [c for c, _ in _run_cmd(br, "", monkeypatch)] == ["send_to"]
    out = _run_cmd(br, "on", monkeypatch)
    assert ("broadcast" in [c for c, _ in out]) and st.resume_after_limit is True
    _run_cmd(br, "off", monkeypatch)
    assert st.resume_after_limit is False
    assert "usage" in _run_cmd(br, "sideways", monkeypatch)[0][1]


def test_the_hand_over_includes_it(monkeypatch):
    kw = server._hub_launch_kwargs(parse_args(["--resume-after-limit"]), None)
    assert kw["resume_after_limit"] is True
    assert server._hub_launch_kwargs(parse_args([]), None)["resume_after_limit"] is None


def test_a_launch_says_for_its_own_session(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "config", parse_args([]))
    seen = {}

    def stop(cfg):
        seen["cfg"] = cfg
        raise RuntimeError("stop here")

    monkeypatch.setattr(server, "init_state_from_config", stop)
    with pytest.raises(RuntimeError):
        asyncio.run(server._create_runtime(cwd=str(tmp_path), no_continue=True,
                                           resume_after_limit=True))
    assert seen["cfg"].resume_after_limit is True


def test_move_takes_the_setting_along():
    import inspect
    src = inspect.getsource(server._do_move)
    assert src.count('resume_after_limit=carry["resume_after_limit"]') == 2


# ---------------------------------------------------------------------------
# The hub: the idle teardown, and reopening
# ---------------------------------------------------------------------------

class _St:
    def __init__(self, **kw):
        self.busy = False
        self.background_tasks = {}
        self.wakeup_at = None
        self.connecting = False
        self.limit_resume_at = None
        self.session_id = SID
        self.__dict__.update(kw)


class _RT:
    def __init__(self, cwd, st, rid="R1"):
        self.rid = rid
        self.clients = set()
        self.state = st
        self.config = type("C", (), {"cwd": cwd})()
        self.idle_timer = None
        self.idle_connecting_defers = 0


def _idle(monkeypatch, rt, timeout=300):
    """One pass of the idle timer at *timeout*, without waiting it out."""
    torn, scheduled = [], []
    real_sleep = asyncio.sleep

    async def teardown(r, **kw):
        torn.append(kw)

    monkeypatch.setattr(server, "runtimes", {rt.rid: rt})
    monkeypatch.setattr(server, "_teardown_runtime", teardown)
    monkeypatch.setattr(server, "_schedule_limit_reopen", scheduled.append)
    monkeypatch.setattr(server, "_maybe_start_idle_timer", lambda *a, **k: None)
    monkeypatch.setattr(asyncio, "sleep", lambda _s: real_sleep(0))
    asyncio.run(server._idle_teardown_after(rt, timeout))
    return torn, scheduled


def test_a_session_waiting_hours_on_a_limit_is_closed_and_scheduled(tmp_path, monkeypatch):
    limit_resume.save(str(tmp_path), SID, due_at=RESET + 60, resets_at=RESET)
    rt = _RT(str(tmp_path), _St(limit_resume_at=RESET + 60))
    torn, scheduled = _idle(monkeypatch, rt)
    assert torn and torn[0]["idle"] is True
    assert [r["session_id"] for r in scheduled] == [SID]


@pytest.mark.parametrize("due_in, connecting", [(30, False), (-1, False), (None, True)])
def test_a_session_about_to_resume_is_not_closed(tmp_path, monkeypatch, due_in, connecting):
    """Closing it would only mean reopening it a moment later."""
    st = (_St(connecting=True) if connecting
          else _St(limit_resume_at=time.time() + due_in))
    torn, _ = _idle(monkeypatch, _RT(str(tmp_path), st))
    assert torn == []


def test_a_session_that_never_connects_is_closed_in_the_end(tmp_path, monkeypatch):
    """Review: with prompts queued, a connect that cannot succeed retries
    without end, and the deferral kept it open for ever."""
    rt = _RT(str(tmp_path), _St(connecting=True))
    for _ in range(server.IDLE_CONNECTING_DEFERS):
        assert _idle(monkeypatch, rt)[0] == []
    assert _idle(monkeypatch, rt)[0], "still deferring"


def _reopen(monkeypatch, rec, live=()):
    opened = []

    class _R:
        clients = set()

        def __init__(self):
            self.rid = f"R{len(opened)}"

    async def create(**kw):
        opened.append(kw)
        return _R()

    monkeypatch.setattr(server, "_create_runtime", create)
    monkeypatch.setattr(server, "runtimes", {f"L{i}": r for i, r in enumerate(live)})
    monkeypatch.setattr(server, "_maybe_start_idle_timer", lambda *a, **k: None)
    monkeypatch.setattr(server, "LIMIT_REOPEN_STAGGER_S", 0.0)
    monkeypatch.setattr(server, "_limit_reopen_lock", None)
    asyncio.run(server._reopen_for_limit(rec, 0.0))
    return opened


def test_a_due_session_is_reopened_as_it_ran(tmp_path, monkeypatch):
    """Review: it was reopened on the hub's model, effort and CLI."""
    limit_resume.save(str(tmp_path), SID, due_at=NOW - 5, resets_at=NOW - 65,
                      settings={"model": "claude-opus-5-5", "effort": "max",
                                "cli_path": "C:/claude-new.exe", "bell_on": "none"})
    opened = _reopen(monkeypatch, limit_resume.load(str(tmp_path), SID))
    assert opened == [{"cwd": str(tmp_path), "resume": SID, "config_dir": None,
                       "model": "claude-opus-5-5", "effort": "max",
                       "cli_path": "C:/claude-new.exe", "bell_on": "none",
                       "resume_after_limit": True}]
    assert limit_resume.load(str(tmp_path), SID)["reopens"] == 1


def test_a_session_already_open_is_left_to_its_bridge(tmp_path, monkeypatch):
    limit_resume.save(str(tmp_path), SID, due_at=NOW - 5, resets_at=NOW - 65)
    live = _RT(str(tmp_path), _St())
    assert _reopen(monkeypatch, limit_resume.load(str(tmp_path), SID), [live]) == []


def test_a_record_gone_by_then_reopens_nothing(tmp_path, monkeypatch):
    rec = {"session_id": SID, "cwd": str(tmp_path), "due_at": NOW - 5}
    assert _reopen(monkeypatch, rec) == []


def test_reopens_past_the_cap_wait_for_a_place(tmp_path, monkeypatch):
    """Review: every session on an account comes due at once, and each is a
    CLI; past the cap one waits rather than opening."""
    limit_resume.save(str(tmp_path), SID, due_at=NOW - 5, resets_at=NOW - 65)
    full = {f"X{i}" for i in range(server.LIMIT_REOPEN_MAX_OPEN)}
    live = [_RT(str(tmp_path), _St(session_id=f"other-{i}"), rid=rid)
            for i, rid in enumerate(sorted(full))]
    later = []
    monkeypatch.setattr(server, "_limit_reopened", set(full))
    monkeypatch.setattr(server, "_schedule_limit_reopen",
                        lambda rec, delay=None: later.append(delay))

    async def create(**kw):  # pragma: no cover - must not be reached
        raise AssertionError("opened past the cap")

    monkeypatch.setattr(server, "_create_runtime", create)
    monkeypatch.setattr(server, "runtimes", {r.rid: r for r in live})
    monkeypatch.setattr(server, "_limit_reopen_lock", None)
    asyncio.run(server._reopen_for_limit(limit_resume.load(str(tmp_path), SID), 0.0))
    assert later == [server.LIMIT_REOPEN_WAIT_S]


def test_the_hub_start_schedules_every_record(tmp_path, monkeypatch):
    limit_resume.save(str(tmp_path), SID, due_at=RESET, resets_at=RESET)
    seen = []
    monkeypatch.setattr(server, "_schedule_limit_reopen", seen.append)
    asyncio.run(server._restore_limit_resumes())
    assert [r["session_id"] for r in seen] == [SID]


def test_only_the_idle_teardown_keeps_it():
    """Every other teardown is the user closing the session."""
    import inspect
    src = inspect.getsource(server)
    assert src.count("idle=True") == 1
    assert "rt.bridge.stop(idle_teardown=idle)" in inspect.getsource(server._teardown_runtime)


# ---------------------------------------------------------------------------
# The second review
# ---------------------------------------------------------------------------

def test_a_login_meanwhile_does_not_drop_the_resume(tmp_path, monkeypatch):
    """Review: a /login (or the CLI reporting the limit lifted) clears the
    rejection with the session still stopped; the timer took that for the
    session having moved on, and left the resume half-cancelled."""
    from state import reset_rate_limit
    _due_now(monkeypatch)
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")
    br.client = object()

    async def go():
        br._adopt_limit_stop({"resets_at": int(NOW) - 5, "kind": "five_hour",
                              "hit_at": None})
        await br._arm_limit_resume(br._stopped_by_limit())
        task = br._limit_task
        reset_rate_limit(st)                  # a /login in another tab
        await asyncio.wait_for(task, 5)
        return br.event_queue.get_nowait()

    kind, text = asyncio.run(go())
    assert kind == "message" and "5-hour session limit" in text
    assert br._limit_task is None and st.limit_resume_at is None
    assert limit_resume.load(str(tmp_path), SID) is None


def test_an_older_limits_headers_do_not_outrank_a_later_text(tmp_path):
    """Review: the same kind of limit hit again later, named only in a
    /compact's text, lost to the headers of the earlier one, long reset."""
    at = datetime(2026, 10, 21, 22, 0).astimezone().timestamp()
    old_reset = int(at) - 8 * 3600
    later = "You've hit your session limit · resets 2:30am (America/New_York)"
    p = _write(tmp_path / "s.jsonl", [_prompt("go", at - 13 * 3600),
                                      _limit(at - 13 * 3600, old_reset),
                                      *_failed_compact(at, later)])
    stop = limit_resume.limit_stop_in(p)
    assert stop["resets_at"] == limit_resume.reset_from_text(later, at) > at


def test_the_retries_go_on_counting_across_a_reopen(tmp_path, monkeypatch):
    """Review: closed between retries and reopened, the count started again
    from nothing, and the session retried for ever."""
    reset = int(NOW) - 5
    p = _stopped_transcript(tmp_path, reset)
    limit_resume.save(str(tmp_path), SID, due_at=NOW + 900, resets_at=reset,
                      attempts=3)
    br, st, _ = _bridge(tmp_path, monkeypatch)

    async def go():
        await br._limit_on_open(SID, limit_resume.limit_stop_in(p))
        try:
            assert br._limit_attempts == 3 and br._limit_sent_for == reset
            assert st.limit_resume_at >= time.time() + limit_resume.RETRY_S * 4 - 5
        finally:
            _stop_timers(br)

    asyncio.run(go())


def test_the_record_keeps_the_attempts_and_the_bell(tmp_path, monkeypatch):
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")
    st.bell_events = set()                    # /bell none

    async def go():
        br._limit_attempts = 2
        br._adopt_limit_stop({"resets_at": RESET, "kind": None, "hit_at": None})
        await br._arm_limit_resume(br._stopped_by_limit())
        _stop_timers(br)

    asyncio.run(go())
    rec = limit_resume.load(str(tmp_path), SID)
    assert rec["attempts"] == 2
    assert rec["bell_on"] == "none", "the launch's set, not the one /bell made"


def test_a_reconnect_does_not_put_back_a_limit_a_login_cleared(tmp_path, monkeypatch):
    """Review: after /login to another account, /connect read the old
    transcript and the session was 'rate limited' again."""
    from state import reset_rate_limit
    p = _stopped_transcript(tmp_path)
    br, st, _ = _bridge(tmp_path, monkeypatch)

    async def go():
        stop = limit_resume.limit_stop_in(p)
        await br._limit_on_open(SID, stop)
        assert st.rate_limit_status == "rejected"
        reset_rate_limit(st)
        await br._limit_on_open(SID, stop)    # the reconnect
        assert st.rate_limit_status is None

    asyncio.run(go())


def test_no_hint_for_a_reset_already_past(tmp_path, monkeypatch):
    br, st, sent = _bridge(tmp_path, monkeypatch)
    asyncio.run(br._hint_limit_resume(int(NOW) - 60))
    assert "/resume-after-limit on" not in _texts(sent)


def test_a_ghost_turn_the_user_interrupted_arms_nothing(tmp_path, monkeypatch):
    """Review: the ghost path read the interrupt only from the result's
    terminal_reason; the bridge knows it interrupted the turn."""
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")
    br._adopt_limit_stop({"resets_at": RESET, "kind": None, "hit_at": None})

    class _Quiet:
        async def interrupt(self):
            pass

    async def go():
        st.busy = True                        # a ghost turn is streaming
        br.client = _Quiet()
        await br.interrupt()
        await br._handle_async_message(_result())

    asyncio.run(go())
    assert br._limit_task is None


def test_a_record_with_a_bad_count_is_not_a_record(tmp_path):
    """Review: one such file stopped the hub's restore at it."""
    rec = {"session_id": SID, "cwd": str(tmp_path), "due_at": NOW + 60, "reopens": "x"}
    assert limit_resume.plan_reopen(rec).reopen is False
    assert not limit_resume._valid({**rec, "reopens": True})
    assert limit_resume._valid({**rec, "reopens": 1})


def test_the_restore_goes_on_past_a_record_it_cannot_schedule(tmp_path, monkeypatch):
    limit_resume.save(str(tmp_path / "a"), "a-sid", due_at=RESET, resets_at=RESET)
    limit_resume.save(str(tmp_path / "b"), "b-sid", due_at=RESET + 1, resets_at=RESET)
    seen = []

    def schedule(rec):
        seen.append(rec["session_id"])
        if rec["session_id"] == "a-sid":
            raise ValueError("bad")

    monkeypatch.setattr(server, "_schedule_limit_reopen", schedule)
    asyncio.run(server._restore_limit_resumes())
    assert seen == ["a-sid", "b-sid"]


def test_a_reopened_session_someone_is_watching_takes_no_place(tmp_path, monkeypatch):
    """Review: six sessions the user had opened and kept working in held the
    places, and every other due session waited until it went stale."""
    limit_resume.save(str(tmp_path), SID, due_at=NOW - 5, resets_at=NOW - 65)
    full = {f"X{i}" for i in range(server.LIMIT_REOPEN_MAX_OPEN)}
    watched = [_RT(str(tmp_path), _St(session_id=f"other-{i}"), rid=rid)
               for i, rid in enumerate(sorted(full))]
    for r in watched:
        r.clients = {object()}
    opened = []

    class _R:
        rid = "R9"
        clients = set()

    async def create(**kw):
        opened.append(kw)
        return _R()

    monkeypatch.setattr(server, "_limit_reopened", set(full))
    monkeypatch.setattr(server, "_create_runtime", create)
    monkeypatch.setattr(server, "runtimes", {r.rid: r for r in watched})
    monkeypatch.setattr(server, "_maybe_start_idle_timer", lambda *a, **k: None)
    monkeypatch.setattr(server, "LIMIT_REOPEN_STAGGER_S", 0.0)
    monkeypatch.setattr(server, "_limit_reopen_lock", None)
    asyncio.run(server._reopen_for_limit(limit_resume.load(str(tmp_path), SID), 0.0))
    assert [o["resume"] for o in opened] == [SID]


# ---------------------------------------------------------------------------
# The Fable review
# ---------------------------------------------------------------------------

def test_a_peers_message_that_came_last_is_what_is_repeated(tmp_path):
    """Review: a lane whose work another session assigned had an older,
    unrelated prompt of the user's repeated as the one to pick back up."""
    p = _write(tmp_path / "s.jsonl", [
        _prompt("set up the repo", NOW - 3000),
        _prompt("Another Claude session sent a message: ...", NOW - 1000, isMeta=True,
                origin={"kind": "peer", "name": "Lane-A",
                        "body": "port the scheduler to the new IPC and test it"}),
        _limit(NOW - 900, RESET)])
    found = limit_resume.last_prompt_in(p)
    assert found["text"] == "port the scheduler to the new IPC and test it"
    assert found["from"] == "Lane-A"
    text = limit_resume.build_resume_prompt(kind=None, hit_at=None, resets_at=None,
                                            prompt=found)
    assert 'from="Lane-A"' in text and "from Lane-A (another session)" in text


def test_another_cli_error_after_the_limit_is_looked_past(tmp_path):
    """Review: a task's turn failing on a dropped connection while the session
    was stopped read as the session having moved on, and its record went."""
    err = _limit(NOW - 100, RESET, text="API Error: Connection error.")
    err["error"] = "unknown"
    err.pop("quotaLimits")
    p = _write(tmp_path / "s.jsonl", [_prompt("go", NOW - 900), _limit(NOW - 800, RESET),
                                      _notification(NOW - 120), err])
    stop = limit_resume.limit_stop_in(p)
    assert stop is not None and stop["resets_at"] == RESET


def test_only_the_resumes_own_turn_counts_as_its_outcome(tmp_path, monkeypatch):
    """Review: the flag that said "a resume was sent" was taken by whichever
    turn ended first -- a background task's included."""
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")

    async def go():
        br._adopt_limit_stop({"resets_at": int(NOW) - 5, "kind": None, "hit_at": None})
        br._limit_sent_for = int(NOW) - 5
        br._limit_resume_queued = True        # a resume waits on the queue
        await br._after_limit_turn()          # ...and a ghost turn ends first
        try:
            assert br._limit_attempts == 0, "the ghost turn was charged as the resume"
        finally:
            _stop_timers(br)

    asyncio.run(go())


def test_a_resume_that_became_moot_is_not_sent(tmp_path, monkeypatch):
    """Review: a ghost turn answered while the resume waited on the queue,
    and the resume still went out, into a session already working."""
    br, st, sent = _bridge(tmp_path, monkeypatch, "--resume-after-limit")
    resume = limit_resume.build_resume_prompt(kind=None, hit_at=None,
                                              resets_at=None, prompt=None)

    async def go():
        br._limit_resume_queued = True
        br.event_queue.put_nowait(("message", resume))
        br._note_limit_over()                 # the model answered meanwhile
        br.event_queue.put_nowait(("message", "the user's own"))
        return await br._await_next_prompt()

    assert asyncio.run(go()) == "the user's own"
    assert "was not sent" in _texts(sent)


def test_a_resume_still_wanted_goes_out(tmp_path, monkeypatch):
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")
    resume = limit_resume.build_resume_prompt(kind=None, hit_at=None,
                                              resets_at=None, prompt=None)

    async def go():
        br._limit_resume_queued = True
        br.event_queue.put_nowait(("message", resume))
        return await br._await_next_prompt()

    assert asyncio.run(go()) == resume
    assert br._limit_resume_queued is False


def test_on_while_the_resume_runs_arms_nothing_more(tmp_path, monkeypatch):
    """Review: a second timer, armed while the resume's own turn ran, fired
    with no backoff and burned an attempt."""
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")
    br._adopt_limit_stop({"resets_at": int(NOW) - 5, "kind": None, "hit_at": None})

    async def go():
        br._limit_resume_running = True
        await br.set_resume_after_limit(True)
        assert br._limit_task is None
        br._limit_resume_running, br._limit_resume_queued = False, True
        await br.set_resume_after_limit(True)
        assert br._limit_task is None

    asyncio.run(go())


def test_an_old_ghost_interrupt_does_not_outlive_its_turn(tmp_path, monkeypatch):
    """Review: an interrupt whose result never came (the CLI died) left the
    flag set, and a later ghost turn into the limit armed nothing."""
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")

    async def go():
        br._ghost_interrupted = True          # from a ghost turn long gone
        await br._handle_async_message(_event(RESET, "seven_day"))
        await br._handle_async_message(AssistantMessage(
            content=[TextBlock(text="You've hit your weekly limit")],
            model="<synthetic>", error="rate_limit"))
        await br._handle_async_message(_result())
        try:
            assert br._limit_task is not None
        finally:
            _stop_timers(br)

    asyncio.run(go())


def test_a_launch_without_the_flag_takes_the_hubs_setting(monkeypatch, tmp_path):
    """Review: it handed over False, which overrode a hub started with it."""
    assert server._hub_launch_kwargs(parse_args([]), None)["resume_after_limit"] is None
    assert server._hub_launch_kwargs(parse_args(["--no-resume-after-limit"]),
                                     None)["resume_after_limit"] is False
    got = {}

    class _RT:
        rid = "R7"

    async def create(**kw):
        got.update(kw)
        return _RT()

    monkeypatch.setattr(server, "_create_runtime", create)
    monkeypatch.setattr(server, "config", parse_args([]))
    for body, want in (({}, None), ({"resume_after_limit": None}, None),
                       ({"resume_after_limit": True}, True),
                       ({"resume_after_limit": False}, False),
                       ({"resume_after_limit": "yes"}, None)):
        got.clear()
        asyncio.run(server.api_session_launch({"cwd": str(tmp_path),
                                               "no_continue": True, **body}))
        assert got["resume_after_limit"] is want, body


def test_a_resumes_turn_into_the_same_limit_is_counted(tmp_path, monkeypatch):
    """Through run_turn: its prompt says it is a resume."""
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")
    reset = int(NOW) - 5
    resume = limit_resume.build_resume_prompt(kind=None, hit_at=None,
                                              resets_at=reset, prompt=None)

    async def go():
        br._adopt_limit_stop({"resets_at": reset, "kind": None, "hit_at": None})
        br._limit_sent_for = reset
        br.client = _Client(br, reset, event=False)
        await br.run_turn(resume)
        try:
            assert br._limit_attempts == 1
        finally:
            _stop_timers(br)

    asyncio.run(go())
