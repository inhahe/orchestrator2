"""--resume-after-limit: a session a usage limit stops carries on once it resets.

Asked for 2026-10-21: "is there a command-line parameter to read the time a
session limit resets and then continue at that time? if there's not, make one.
it should probably include the previous prompt, with a note saying the prompt
was repeated ... it should probably read rate limit messages that came as the
last message before a resume, too (besides notifications of background tasks
completing and whatever)".

So: the reset time is read from what the CLI reports (the RateLimitEvent live,
the transcript's quota headers on open); the resume repeats the user's last
prompt, framed as a repeat; and a session opened while it was stopped by a
limit -- with notifications and peers' messages after the error -- is picked up
too.  Found on the way: the bridge took the CLI's limit message for the model
answering, so it wiped the rejection (and its reset time) the instant it
arrived, and a locked-out session read "idle".
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock  # noqa: E402
from claude_agent_sdk.types import RateLimitEvent, RateLimitInfo  # noqa: E402

import limit_resume  # noqa: E402
import sdk_bridge  # noqa: E402
import server  # noqa: E402
from commands import classify  # noqa: E402
from config import SLASH_COMMANDS, Config, parse_args  # noqa: E402
from sdk_bridge import SDKBridge  # noqa: E402
from session import _classify_user_text  # noqa: E402
from state import init_state_from_config, state_to_status_dict  # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Transcript records, shaped like the CLI's (2.1.280 / 2.1.301)
# ---------------------------------------------------------------------------

def _limit(ts, reset, kind="five_hour", **quota):
    q = {"status": "rejected", "resetsAt": reset, "rateLimitType": kind,
         "overageStatus": "rejected", "isUsingOverage": False, **quota}
    return {"type": "assistant", "timestamp": _iso(ts), "isApiErrorMessage": True,
            "error": "rate_limit", "apiErrorStatus": 429, "quotaLimits": q,
            "message": {"model": "<synthetic>", "role": "assistant",
                        "content": [{"type": "text", "text":
                                     "You've hit your session limit · resets 9pm "
                                     "(America/New_York)"}]}}


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


def _write(path: Path, records) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                    encoding="utf-8")
    return path


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
    assert stop == {"resets_at": RESET, "kind": "five_hour",
                    "hit_at": _iso(NOW - 400), "last_at": _iso(NOW - 400)}


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
    assert stop["hit_at"] == _iso(NOW - 800), "when it was stopped, not the last retry"
    assert stop["last_at"] == _iso(NOW - 590)


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


@pytest.mark.parametrize("quota", [{"resetsAt": None}, {"status": "allowed"}])
def test_a_limit_with_no_reset_to_wait_for_is_none(tmp_path, quota):
    p = _write(tmp_path / "s.jsonl", [_prompt("go", NOW - 900),
                                      _limit(NOW - 10, RESET, **quota)])
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
# The prompt it was given
# ---------------------------------------------------------------------------

def test_the_last_prompt_is_the_users_not_what_came_after(tmp_path):
    earlier_resume = limit_resume.build_resume_prompt(
        kind="five_hour", hit_at=None, resets_at=None,
        prompt={"text": "the real prompt", "at": None})
    p = _write(tmp_path / "s.jsonl", [
        _prompt("an older prompt", NOW - 2000),
        _prompt("the real prompt", NOW - 1000),
        _tool_use("t1", NOW - 990), _tool_result("t1", NOW - 980),
        _prompt("<command-name>/usage</command-name>", NOW - 970),
        _notification(NOW - 960), _peer(NOW - 950),
        _prompt("This session is being continued from a previous conversation",
                NOW - 940, isCompactSummary=True),
        _prompt("Autonomous-loop wakeup (scheduled by you). Resume work now",
                NOW - 930),
        _prompt(earlier_resume, NOW - 920),
        _limit(NOW - 910, RESET),
    ])
    found = limit_resume.last_prompt_in(p)
    assert found == {"text": "the real prompt", "at": _iso(NOW - 1000)}


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
        kind="seven_day", hit_at=_iso(NOW - 3600), resets_at=NOW - 60,
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
    limit_resume.save(str(tmp_path), SID, due_at=RESET + 60, resets_at=RESET)
    rec = limit_resume.load(str(tmp_path), SID)
    assert rec["due_at"] == RESET + 60 and rec["session_id"] == SID
    assert [r["session_id"] for r in limit_resume.iter_records()] == [SID]
    limit_resume.clear(str(tmp_path), SID)
    assert limit_resume.load(str(tmp_path), SID) is None


def test_a_record_out_of_its_slot_is_ignored(tmp_path):
    limit_resume.save(str(tmp_path), SID, due_at=RESET, resets_at=RESET)
    path = limit_resume.record_file_for(str(tmp_path), SID)
    path.rename(path.with_name("elsewhere__x.json"))
    assert list(limit_resume.iter_records()) == []


@pytest.mark.parametrize("due_in, reopen", [
    (600, True),                                   # waiting
    (-600, True),                                  # due while the hub was down
    (-(limit_resume.LATE_S + 60), False),          # a day-old plan
    (limit_resume.MAX_AHEAD_S + 60, False),        # no limit lasts that long
])
def test_which_records_reopen_their_session(due_in, reopen):
    rec = {"session_id": SID, "cwd": "C:/w", "due_at": NOW + due_in}
    plan = limit_resume.plan_reopen(rec, now=NOW)
    assert plan.reopen is reopen
    if reopen:
        assert plan.delay == max(0.0, due_in)


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


class _Client:
    """A CLI whose turns run into a usage limit (or answer, with *reply*)."""

    def __init__(self, br, reset, reply=None):
        self.br, self.reset, self.reply = br, reset, reply

    async def query(self, _prompt):
        q = self.br.turn_msg_queue
        if self.reply is None:
            q.put_nowait(RateLimitEvent(
                rate_limit_info=RateLimitInfo(status="rejected", resets_at=self.reset,
                                              rate_limit_type="five_hour"),
                uuid="u", session_id=SID))
            q.put_nowait(AssistantMessage(
                content=[TextBlock(text="You've hit your session limit · resets 9pm")],
                model="<synthetic>", error="rate_limit"))
        else:
            q.put_nowait(AssistantMessage(content=[TextBlock(text=self.reply)],
                                          model="claude-opus-5-5"))
        q.put_nowait(ResultMessage(subtype="success", duration_ms=1,
                                   duration_api_ms=0, is_error=self.reply is None,
                                   num_turns=1, session_id=SID,
                                   api_error_status=429 if self.reply is None else None))

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
    br, st, sent = _bridge(tmp_path, monkeypatch, "--resume-after-limit")
    br.client = _Client(br, RESET)

    async def go():
        await br.run_turn("go")
        try:
            assert br._limit_task is not None
            assert st.limit_resume_at == RESET + limit_resume.MARGIN_S
            rec = limit_resume.load(str(tmp_path), SID)
            assert rec is not None and rec["due_at"] == RESET + limit_resume.MARGIN_S
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


def test_a_reply_ends_the_limit_and_the_pending_resume(tmp_path, monkeypatch):
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")

    async def go():
        br.client = _Client(br, RESET)
        await br.run_turn("go")
        assert br._limit_task is not None
        br.client = _Client(br, RESET, reply="back at it")
        await br.run_turn("carry on")

    asyncio.run(go())
    assert br._limit_task is None and br._limit_hit is None
    assert limit_resume.load(str(tmp_path), SID) is None
    assert st.rate_limit_status == "allowed"


def test_a_background_tasks_turn_into_the_limit_arms_it_too(tmp_path, monkeypatch):
    """The commonest way a session is stopped while nobody watches: the turn
    that runs into the limit is the CLI's own, for a task's notification."""
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")

    async def go():
        await br._handle_async_message(RateLimitEvent(
            rate_limit_info=RateLimitInfo(status="rejected", resets_at=RESET,
                                          rate_limit_type="seven_day"),
            uuid="u", session_id=SID))
        await br._handle_async_message(AssistantMessage(
            content=[TextBlock(text="You've hit your weekly limit")],
            model="<synthetic>", error="rate_limit"))
        await br._handle_async_message(ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=0, is_error=True,
            num_turns=1, session_id=SID))
        try:
            assert br._limit_task is not None
            assert br._limit_hit["kind"] == "seven_day"
        finally:
            _stop_timers(br)

    asyncio.run(go())


def test_an_sdk_without_the_error_field_is_read_by_the_text():
    msg = AssistantMessage(content=[TextBlock(text="You've hit your weekly limit · x")],
                           model="<synthetic>")
    assert SDKBridge._is_limit_message(msg)
    assert not SDKBridge._is_limit_message(AssistantMessage(
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
    assert st.limit_resume_at is None and br._limit_resume_sent is True


def test_a_resume_waits_for_a_running_turn(tmp_path, monkeypatch):
    _due_now(monkeypatch)
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")
    br.client = object()
    br._limit_hit = {"resets_at": int(NOW) - 5, "kind": None, "hit_at": None}
    st.busy = True

    async def go():
        await br._arm_limit_resume(br._limit_hit)
        await asyncio.sleep(0.05)
        assert br.event_queue.empty(), "sent into a running turn"
        assert br._limit_defers >= 1
        st.busy = False
        await asyncio.wait_for(br._limit_task, 5)
        return br.event_queue.get_nowait()

    assert asyncio.run(go())[0] == "message"


def test_resumes_into_the_same_limit_back_off_then_stop(tmp_path, monkeypatch):
    br, st, sent = _bridge(tmp_path, monkeypatch, "--resume-after-limit")
    reset = int(NOW) - 5

    async def go():
        dues = []
        for _ in range(limit_resume.MAX_ATTEMPTS):
            br._limit_hit = {"resets_at": reset, "kind": "five_hour", "hit_at": None}
            br._limit_resume_sent, br._limit_sent_for = True, reset
            await br._after_limit_turn()
            dues.append(st.limit_resume_at)
            _stop_timers(br)
        return dues

    dues = asyncio.run(go())
    waits = [d - time.time() for d in dues[:-1]]
    assert all(b > a for a, b in zip(waits, waits[1:])), "each wait longer"
    assert dues[-1] is None, "kept resuming into the same limit"
    assert "stopped resuming by itself" in _texts(sent)


def test_a_later_limit_starts_the_count_again(tmp_path, monkeypatch):
    """The session limit reset, and the resume ran into the weekly one."""
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")

    async def go():
        br._limit_attempts = 3
        br._limit_hit = {"resets_at": RESET, "kind": "seven_day", "hit_at": None}
        br._limit_resume_sent, br._limit_sent_for = True, int(NOW) - 5
        await br._after_limit_turn()
        try:
            assert br._limit_attempts == 0
            assert st.limit_resume_at == RESET + limit_resume.MARGIN_S
        finally:
            _stop_timers(br)

    asyncio.run(go())


def test_opening_a_stopped_session_schedules_it_and_says_so(tmp_path, monkeypatch):
    p = _write(tmp_path / f"{SID}.jsonl", [_prompt("go", NOW - 900),
                                           _limit(NOW - 800, RESET)])
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


def test_opened_with_it_off_nothing_is_scheduled_until_asked(tmp_path, monkeypatch):
    p = _write(tmp_path / f"{SID}.jsonl", [_prompt("go", NOW - 900),
                                           _limit(NOW - 800, RESET)])
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


def test_the_status_says_when_on_but_nothing_is_scheduled(tmp_path, monkeypatch):
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")
    assert "No usage limit has stopped" in br.limit_resume_status()
    br._limit_hit = {"resets_at": RESET, "kind": "seven_day", "hit_at": None}
    assert "nothing is scheduled" in br.limit_resume_status()


def test_opened_long_after_the_reset_it_only_says_so(tmp_path, monkeypatch):
    late = int(NOW - limit_resume.LATE_S - 600)
    p = _write(tmp_path / f"{SID}.jsonl", [_prompt("go", late - 900),
                                           _limit(late - 800, late)])
    br, st, sent = _bridge(tmp_path, monkeypatch, "--resume-after-limit")
    asyncio.run(br._limit_on_open(SID, limit_resume.limit_stop_in(p)))
    assert br._limit_task is None
    assert "too long ago" in _texts(sent)


def test_a_record_turns_it_on_for_the_session_it_names(tmp_path, monkeypatch):
    """The hub reopens a session it scheduled: it was on for that session,
    whatever the hub was started with."""
    p = _write(tmp_path / f"{SID}.jsonl", [_prompt("go", NOW - 900),
                                           _limit(NOW - 800, RESET)])
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


@pytest.mark.parametrize("kw, kept", [
    ({}, False),                          # the user closed or left it
    ({"hub_exiting": True}, True),        # restored at the next start
    ({"idle_teardown": True}, True),      # reopened by the hub when due
])
def test_which_stops_keep_the_record(tmp_path, monkeypatch, kw, kept):
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")

    async def go():
        br._limit_hit = {"resets_at": RESET, "kind": None, "hit_at": None}
        await br._arm_limit_resume(br._limit_hit)
        await br._shutdown(**kw)
        assert br._limit_task is None

    asyncio.run(go())
    assert (limit_resume.load(str(tmp_path), SID) is not None) is kept


def test_clear_drops_it(tmp_path, monkeypatch):
    br, st, _ = _bridge(tmp_path, monkeypatch, "--resume-after-limit")

    async def go():
        br._limit_hit = {"resets_at": RESET, "kind": None, "hit_at": None}
        await br._arm_limit_resume(br._limit_hit)
        monkeypatch.setattr(br, "disconnect", _anoop)
        monkeypatch.setattr(br, "connect", _anoop)
        await br._clear_context()

    asyncio.run(go())
    assert br._limit_task is None and br._limit_hit is None
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
    assert server._hub_launch_kwargs(parse_args([]), None)["resume_after_limit"] is False


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
    def __init__(self, cwd, st):
        self.rid = "R1"
        self.clients = set()
        self.state = st
        self.config = type("C", (), {"cwd": cwd})()
        self.idle_timer = None


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


@pytest.mark.parametrize("st", [_St(limit_resume_at=time.time() + 30),
                                _St(limit_resume_at=time.time() - 1),
                                _St(connecting=True)])
def test_a_session_about_to_resume_is_not_closed(tmp_path, monkeypatch, st):
    """Closing it would only mean reopening it a moment later."""
    torn, _ = _idle(monkeypatch, _RT(str(tmp_path), st))
    assert torn == []


def _reopen(monkeypatch, rec, live=()):
    opened = []

    class _R:
        rid = "R2"
        clients = set()

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


def test_a_due_session_is_reopened_with_it_on(tmp_path, monkeypatch):
    limit_resume.save(str(tmp_path), SID, due_at=NOW - 5, resets_at=NOW - 65)
    opened = _reopen(monkeypatch, limit_resume.load(str(tmp_path), SID))
    assert opened == [{"cwd": str(tmp_path), "resume": SID, "config_dir": None,
                       "resume_after_limit": True}]


def test_a_session_already_open_is_left_to_its_bridge(tmp_path, monkeypatch):
    limit_resume.save(str(tmp_path), SID, due_at=NOW - 5, resets_at=NOW - 65)
    live = _RT(str(tmp_path), _St())
    assert _reopen(monkeypatch, limit_resume.load(str(tmp_path), SID), [live]) == []


def test_a_record_gone_by_then_reopens_nothing(tmp_path, monkeypatch):
    rec = {"session_id": SID, "cwd": str(tmp_path), "due_at": NOW - 5}
    assert _reopen(monkeypatch, rec) == []


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
