"""Telling a resumed session that its background work died with it.

Reported 2026-09-16, as the follow-up to the resumed-interrupted-turn fix:
"if the session is automatically continued, the agent has no idea that any
background tasks it had been running were aborted. is there any way to tell
it?"

There were three separate holes, and only one of them was about resuming:

1. ``_orphan_bg_tasks`` has told the *browser* about lost tasks since
   2026-08, and has never told the *model*. So even a plain reconnect left it
   waiting on ``TaskOutput`` handles that were already dead.
2. A teardown told nobody at all -- ``_orphan_bg_tasks`` is wired only to the
   reconnect paths, and there are zero orphan announcements in the entire
   2026-09-16 log, including the 04:01 teardown that started all this.
3. After a teardown we did not even *know*: ``State.background_tasks`` is an
   in-memory mirror of the CLI's registry, and ``session.py`` persisted the
   prompt queue and nothing else. A fresh runtime started empty, so there was
   nothing to report even if someone had asked.

**The accuracy question decided the wording.** Asked whether the record could
be trusted -- "i think that knowledge might often be incorrect, because the
client seems to often have bg tasks showing that were already finished" -- the
log says the registry is sound now: 50,886 starts against 50,841 completions,
and of the 44 unmatched, 4 were still in flight and almost all the rest are
accounted for by an announced orphan, a teardown or a reconnect. About 4 in
50,886 are unexplained. (The behaviour being remembered was real and is what
``_orphan_bg_tasks`` was written to fix.)

But "running when we last heard" still is not "failed": at 04:01:34,445 a
teardown began and two tasks logged *completed* at 04:01:35,327 and
04:01:36,182 -- one and two seconds into it. So the record is a list of tasks
whose outcome is **unknown**, and these tests pin that we never claim
otherwise.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import parse_args                                    # noqa: E402
from sdk_bridge import SDKBridge                                  # noqa: E402
from session import (                                             # noqa: E402
    bg_task_items,
    bg_tasks_file_for_cwd,
    describe_lost_bg_tasks,
    load_persisted_bg_tasks,
    save_persisted_bg_tasks,
)
from state import init_state_from_config                          # noqa: E402
from tool_manager import complete_bg_task, register_bg_task       # noqa: E402

SID = "1aa74fb0-ca4e-42cb-80c3-ef7302fee0a4"


@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
    """Keep every test's state files out of the real config dir."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    return tmp_path


def _bridge(cwd="D:/proj", sid=SID):
    cfg = parse_args([])
    object.__setattr__(cfg, "cwd", cwd)
    state = init_state_from_config(cfg)
    state.session_id = sid
    sent: list[dict] = []

    async def bcast(msg: dict) -> None:
        sent.append(msg)

    br = SDKBridge(config=cfg, state=state, broadcaster=bcast)
    return br, state, sent


def _start(state, task_id, name, *, ago_s=0.0):
    register_bg_task(state, task_id, "local_bash", name)
    if ago_s:
        state.background_tasks[task_id]["started_at"] -= ago_s


def _notices(sent):
    return [(m.get("data") or {}).get("message") or ""
            for m in sent if m.get("type") == "system_msg"]


# --------------------------------------------------------------------------
# The record itself
# --------------------------------------------------------------------------

def test_the_live_set_survives_the_process(cfgdir):
    br, state, _sent = _bridge()
    _start(state, "b1", "Run the full suite")
    _start(state, "b2", "Re-render frames 200-799")
    br._save_bg_tasks()

    got = load_persisted_bg_tasks("D:/proj", SID)

    assert sorted(i["name"] for i in got) == [
        "Re-render frames 200-799", "Run the full suite"]


def test_a_finished_task_leaves_the_record(cfgdir):
    """Reporting work that actually completed is the failure mode that makes
    the whole feature untrustworthy, so completion must erase it immediately --
    not at some later flush."""
    br, state, _sent = _bridge()
    _start(state, "b1", "Run the full suite")
    _start(state, "b2", "Commit the refactor")
    br._save_bg_tasks()

    complete_bg_task(state, "b1", "completed")
    br._save_bg_tasks()

    got = load_persisted_bg_tasks("D:/proj", SID)
    assert [i["name"] for i in got] == ["Commit the refactor"]


def test_an_empty_registry_removes_the_file(cfgdir):
    """A session that finished its work must leave nothing to report."""
    br, state, _sent = _bridge()
    _start(state, "b1", "Run the full suite")
    br._save_bg_tasks()
    assert bg_tasks_file_for_cwd("D:/proj", SID).exists()

    complete_bg_task(state, "b1", "completed")
    br._save_bg_tasks()

    assert not bg_tasks_file_for_cwd("D:/proj", SID).exists()


def test_the_record_is_wall_clock_not_monotonic(cfgdir):
    """``started_at`` is ``time.monotonic()``, which means nothing in another
    process. Persisted raw, "running for N minutes" would be nonsense -- and
    the duration is most of what makes the notice worth reading."""
    _br, state, _sent = _bridge()
    _start(state, "b1", "Long one", ago_s=2400)

    item = bg_task_items(state.background_tasks)[0]

    assert abs((time.time() - item["started_wall"]) - 2400) < 5


def test_another_sessions_record_is_never_read(cfgdir):
    """This record becomes a *prompt*. Reading the wrong one spends a turn
    telling the model a lie about work it never started -- the same failure the
    queue file was re-keyed by session id to prevent."""
    br, state, _sent = _bridge()
    _start(state, "b1", "Run the full suite")
    br._save_bg_tasks()

    assert load_persisted_bg_tasks("D:/proj", "some-other-session") == []


def test_a_stale_record_is_never_read(cfgdir):
    br, state, _sent = _bridge()
    _start(state, "b1", "Run the full suite")
    br._save_bg_tasks()

    path = bg_tasks_file_for_cwd("D:/proj", SID)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["saved_at"] = time.time() - (48 * 3600)
    path.write_text(json.dumps(data), encoding="utf-8")

    assert load_persisted_bg_tasks("D:/proj", SID) == []


def test_a_record_left_in_a_reused_slot_is_rejected(cfgdir):
    """The filename is keyed by session id, so a wrong id usually just misses
    the file -- which means the *recorded* id gate is never exercised by that
    path. It earns its keep when a slot is reused: same directory, same
    sanitised name, different session. Then the filename matches and only the
    recorded id can tell them apart."""
    br, state, _sent = _bridge()
    _start(state, "b1", "Run the full suite")
    br._save_bg_tasks()

    path = bg_tasks_file_for_cwd("D:/proj", SID)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["session_id"] = "a-previous-session-in-this-slot"
    path.write_text(json.dumps(data), encoding="utf-8")

    assert load_persisted_bg_tasks("D:/proj", SID) == []


def test_a_session_with_no_id_reads_nothing(cfgdir):
    """The load-bearing half, exactly as for the prompt queue: "start fresh" is
    made safe at the one place that reads the file, rather than by trusting
    every call site to ask correctly. A session with no id would otherwise read
    the shared "new" slot and inherit whatever is sitting in it."""
    path = bg_tasks_file_for_cwd("D:/proj", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "cwd": "D:/proj", "session_id": None, "saved_at": time.time(),
        "tasks": [{"task_id": "b1", "name": "Someone else's work"}],
    }), encoding="utf-8")

    assert load_persisted_bg_tasks("D:/proj", None) == []


def test_a_session_with_no_id_persists_nothing(cfgdir):
    """Nothing to restore *into*, so the file would be litter at best."""
    br, state, _sent = _bridge(sid=None)
    state.session_id = None
    _start(state, "b1", "Run the full suite")
    br._save_bg_tasks()

    assert not bg_tasks_file_for_cwd("D:/proj", "new").exists()


# --------------------------------------------------------------------------
# What we say
# --------------------------------------------------------------------------

def test_we_never_claim_the_tasks_failed():
    """The measured counter-example: at 04:01:34,445 a teardown began and two
    tasks logged completed 1 s and 2 s later. "Aborted" would have been a
    guess, and a wrong one."""
    text = describe_lost_bg_tasks(
        [{"task_id": "b1", "name": "Run the full suite"}]).lower()

    for word in ("aborted", "failed", "were killed", "did not finish"):
        assert word not in text, f"claims an outcome we cannot know: {word!r}"


def test_we_say_the_outcome_is_unknown():
    text = describe_lost_bg_tasks(
        [{"task_id": "b1", "name": "Run the full suite"}]).lower()

    assert "unknown" in text
    assert "do not assume" in text


def test_we_say_not_to_wait():
    """The concrete harm: a session parked waiting for a notification that has
    nobody left to send it."""
    text = describe_lost_bg_tasks(
        [{"task_id": "b1", "name": "Run the full suite"}]).lower()

    assert "do not wait" in text


def test_we_say_to_check_for_effects_first():
    """Blind re-running is the other way to lose: a task that *did* finish may
    have committed, written files, or started something still running."""
    text = describe_lost_bg_tasks(
        [{"task_id": "b1", "name": "Commit the refactor"}]).lower()

    assert "check" in text and "before re-running" in text


def test_the_notice_says_how_long_each_had_been_running():
    """"A task was running" is much weaker than "had been running for 40
    minutes": the duration is what tells the model whether to expect a
    half-finished side effect or nothing at all."""
    text = describe_lost_bg_tasks(
        [{"task_id": "b1", "name": "Run the full suite",
          "started_wall": time.time() - 2400}])

    assert "40 min" in text, text


def test_the_tasks_are_named():
    text = describe_lost_bg_tasks([
        {"task_id": "b1", "name": "Run the full suite"},
        {"task_id": "b2", "name": "Re-render frames 200-799"},
    ])

    assert "Run the full suite" in text
    assert "Re-render frames 200-799" in text


def test_an_unnamed_task_is_still_named_something():
    """A task registered from a ToolUseBlock we could not parse still has to be
    nameable, or the notice is less informative than the loss."""
    text = describe_lost_bg_tasks([{"task_id": "babcdef123456"}])

    assert "babcdef" in text


def test_nothing_lost_means_nothing_said():
    assert describe_lost_bg_tasks([]) == ""


# --------------------------------------------------------------------------
# How the model is told
# --------------------------------------------------------------------------

def test_the_notice_goes_to_the_front_of_the_queue(cfgdir):
    """Appended, a prompt the user queued earlier would be answered by a model
    still believing its background work was alive -- the one ordering where
    knowing late costs the most."""
    br, state, _sent = _bridge()
    state.queued_prompts.append("my own prompt")

    br._queue_lost_bg_notice(
        [{"task_id": "b1", "name": "Run the full suite"}], "cut off")

    assert "Run the full suite" in state.queued_prompts[0]
    assert state.queued_prompts[1] == "my own prompt"


def test_the_notice_is_a_queued_prompt_not_a_side_channel(cfgdir):
    """It has to show up in the left pane like anything else in the queue --
    asked for explicitly. Going through the deque is what gets that for free:
    ``on_change`` persists it and the listeners poke the worker and push the
    panel."""
    br, state, _sent = _bridge()
    fired = []
    state.queued_prompts.add_listener(lambda: fired.append(1))

    br._queue_lost_bg_notice(
        [{"task_id": "b1", "name": "Run the full suite"}], "cut off")

    assert len(state.queued_prompts) == 1
    assert fired, "no listener fired, so the worker and the queue panel never hear"


def test_a_resumed_session_is_told(cfgdir):
    """The reported case: torn down mid-work, reopened later."""
    br, state, _sent = _bridge()
    _start(state, "b1", "Run the full suite")
    br._save_bg_tasks()

    br2, state2, sent2 = _bridge()
    n = asyncio.run(br2._report_lost_bg_tasks(SID))

    assert n == 1
    assert state2.queued_prompts and "Run the full suite" in state2.queued_prompts[0]
    assert _notices(sent2), "the browser is told too"


def test_a_resumed_session_is_told_only_once(cfgdir):
    """Otherwise every resume forever re-reports the same dead tasks."""
    br, state, _sent = _bridge()
    _start(state, "b1", "Run the full suite")
    br._save_bg_tasks()

    br2, state2, _s2 = _bridge()
    asyncio.run(br2._report_lost_bg_tasks(SID))
    br3, state3, _s3 = _bridge()
    n = asyncio.run(br3._report_lost_bg_tasks(SID))

    assert n == 0
    assert not state3.queued_prompts


def test_a_fresh_session_is_told_nothing(cfgdir):
    br, state, _sent = _bridge()
    n = asyncio.run(br._report_lost_bg_tasks(None))

    assert n == 0
    assert not state.queued_prompts


def test_a_session_that_lost_nothing_is_told_nothing(cfgdir):
    br, state, _sent = _bridge()
    n = asyncio.run(br._report_lost_bg_tasks(SID))

    assert n == 0
    assert not state.queued_prompts


# --------------------------------------------------------------------------
# The reconnect path -- gap 1
# --------------------------------------------------------------------------

def test_orphaning_tells_the_model_not_just_the_browser(cfgdir):
    """Since 2026-08 this path has named the lost tasks in the UI and told the
    model nothing, so it kept waiting on dead handles."""
    br, state, _sent = _bridge()
    _start(state, "b1", "Run the full suite")

    n = asyncio.run(br._orphan_bg_tasks("a reconnect"))

    assert n == 1
    assert state.queued_prompts, "the model was never told"
    assert "Run the full suite" in state.queued_prompts[0]


def test_orphaning_still_tells_the_browser(cfgdir):
    br, state, sent = _bridge()
    _start(state, "b1", "Run the full suite")

    asyncio.run(br._orphan_bg_tasks("a reconnect"))

    assert any("orphaned" in m for m in _notices(sent))


def test_orphaning_clears_the_record_so_it_is_not_reported_twice(cfgdir):
    """Announced live *and* left on disk, the next resume would report the same
    loss a second time."""
    br, state, _sent = _bridge()
    _start(state, "b1", "Run the full suite")
    br._save_bg_tasks()

    asyncio.run(br._orphan_bg_tasks("a reconnect"))

    assert load_persisted_bg_tasks("D:/proj", SID) == []


def test_clear_does_not_tell_the_model(cfgdir):
    """``/clear`` wipes the conversation. A model that has just lost all memory
    of *starting* those tasks cannot act on being told they were lost, and the
    notice would be the first thing in its brand-new context."""
    br, state, sent = _bridge()
    _start(state, "b1", "Run the full suite")

    n = asyncio.run(br._orphan_bg_tasks("/clear", tell_model=False))

    assert n == 1
    assert not state.queued_prompts, "a cleared context was handed dead context"
    assert _notices(sent), "the user still needs to know"


def test_clear_is_the_only_silent_orphan_path(cfgdir):
    """Every other caller must tell the model: a reconnect, a dead CLI and a
    resume all leave a model that still remembers starting the work."""
    src = inspect.getsource(SDKBridge)
    silent = [l.strip() for l in src.splitlines()
              if "_orphan_bg_tasks(" in l and "tell_model=False" in l]

    assert len(silent) == 1 and "/clear" in silent[0], silent


def test_orphaning_nothing_says_nothing(cfgdir):
    br, state, _sent = _bridge()

    n = asyncio.run(br._orphan_bg_tasks("a reconnect"))

    assert n == 0
    assert not state.queued_prompts


# --------------------------------------------------------------------------
# The record has to outlive the process that wrote it
# --------------------------------------------------------------------------

def test_teardown_does_not_take_the_record_with_it(cfgdir):
    """The whole point. ``_teardown_runtime`` and ``_clear_context`` both clear
    ``background_tasks``; if either also saved, the file would be emptied at
    exactly the moment it becomes the only copy."""
    br, state, _sent = _bridge()
    _start(state, "b1", "Run the full suite")
    br._save_bg_tasks()

    state.background_tasks.clear()          # what teardown does

    assert load_persisted_bg_tasks("D:/proj", SID), \
        "the record died with the process it was supposed to outlive"


def test_no_clear_path_saves(cfgdir):
    """Pin the mechanism, since the behavioural test above only covers the two
    clears that exist today."""
    src = inspect.getsource(SDKBridge)
    # Every _save_bg_tasks call must be a start, a finish, or the orphan path.
    saves = [l.strip() for l in src.splitlines()
             if "_save_bg_tasks()" in l and "def " not in l]
    assert len(saves) == 3, (
        f"expected exactly 3 _save_bg_tasks call sites (start, finish, "
        f"orphan); found {len(saves)} -- a new one may be erasing the record"
    )
