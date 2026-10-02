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

**2026-09-29: the model is no longer told from here.**  The loss went to it as
a prompt at the head of the queue, which ran as a turn of its own -- so a
session that had merely been opened started working: "sessions would often
start working automatically when i resumed them".  The CLI tells the model
itself now (2.1.258 and 2.1.280 both report, on resume, every background task
the previous process left unfinished, queued to go with the next prompt), so
this side only tells the *user*, in the tab.
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
    process. Persisted raw, how long each had been running would be
    nonsense."""
    _br, state, _sent = _bridge()
    _start(state, "b1", "Long one", ago_s=2400)

    item = bg_task_items(state.background_tasks)[0]

    assert abs((time.time() - item["started_wall"]) - 2400) < 5


def test_another_sessions_record_is_never_read(cfgdir):
    """Reading the wrong one tells the user that work died which this
    session never started -- the same failure the queue file was re-keyed by
    session id to prevent."""
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
# What the tab is told
# --------------------------------------------------------------------------

def _told_on_resume(tasks):
    """Leave *tasks* running in a session that is cut off, open it again, and
    return what the new bridge reported, its state, and what it said."""
    br, state, _sent = _bridge()
    for task_id, name in tasks:
        _start(state, task_id, name)
    br._save_bg_tasks()

    br2, state2, sent2 = _bridge()
    n = asyncio.run(br2._report_lost_bg_tasks(SID))
    return n, state2, sent2


def test_the_tab_is_told_which_tasks_died(cfgdir):
    n, _state, sent = _told_on_resume([("b1", "Run the full suite"),
                                       ("b2", "Re-render frames 200-799")])
    text = " ".join(_notices(sent))

    assert n == 2
    assert "Run the full suite" in text and "Re-render frames 200-799" in text


def test_we_never_claim_the_tasks_failed(cfgdir):
    """The measured counter-example: at 04:01:34,445 a teardown began and two
    tasks logged completed 1 s and 2 s later. "Aborted" would have been a
    guess, and a wrong one."""
    _n, _state, sent = _told_on_resume([("b1", "Run the full suite")])
    text = " ".join(_notices(sent)).lower()

    for word in ("aborted", "failed", "were killed", "did not finish"):
        assert word not in text, f"claims an outcome we cannot know: {word!r}"
    assert "unknown" in text


def test_an_unnamed_task_is_still_named_something(cfgdir):
    """A task registered from a ToolUseBlock we could not parse still has to be
    nameable, or the notice is less informative than the loss."""
    save_persisted_bg_tasks("D:/proj", {"babcdef123456": {}}, SID)
    br, _state, sent = _bridge()

    asyncio.run(br._report_lost_bg_tasks(SID))

    assert "babcdef" in " ".join(_notices(sent))


def test_it_is_kept_for_a_tab_that_opens_later(cfgdir):
    """The tab that opened the session is often not attached yet when this is
    said (server._send_initial_state replays it)."""
    _n, state, sent = _told_on_resume([("b1", "Run the full suite")])

    assert [n["data"]["message"] for n in state.open_notices] == _notices(sent)


# --------------------------------------------------------------------------
# Nothing goes to the model from here
# --------------------------------------------------------------------------

def test_a_resumed_session_is_not_sent_a_prompt(cfgdir):
    """The 2026-09-29 report: the prompt this used to queue started a turn in
    a session that had only been opened."""
    _n, state, _sent = _told_on_resume([("b1", "Run the full suite")])

    assert not state.queued_prompts


def test_a_resumed_session_is_told_only_once(cfgdir):
    """Otherwise every resume forever re-reports the same dead tasks."""
    _told_on_resume([("b1", "Run the full suite")])

    br3, state3, sent3 = _bridge()
    n = asyncio.run(br3._report_lost_bg_tasks(SID))

    assert n == 0
    assert not _notices(sent3) and not state3.open_notices


def test_a_fresh_session_is_told_nothing(cfgdir):
    br, state, sent = _bridge()
    n = asyncio.run(br._report_lost_bg_tasks(None))

    assert n == 0
    assert not _notices(sent) and not state.queued_prompts


def test_a_session_that_lost_nothing_is_told_nothing(cfgdir):
    br, state, sent = _bridge()
    n = asyncio.run(br._report_lost_bg_tasks(SID))

    assert n == 0
    assert not _notices(sent) and not state.queued_prompts


# --------------------------------------------------------------------------
# The reconnect path
# --------------------------------------------------------------------------

def test_orphaning_sends_the_model_nothing(cfgdir):
    """The new CLI reports what its predecessor left unfinished when it
    resumes the session.  A second account of it from here would be a second
    prompt, and a turn of its own."""
    br, state, _sent = _bridge()
    _start(state, "b1", "Run the full suite")

    n = asyncio.run(br._orphan_bg_tasks("a reconnect"))

    assert n == 1
    assert not state.queued_prompts


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


def test_clear_tells_only_the_browser(cfgdir):
    """``/clear`` wipes the conversation, and its new CLI starts a new session,
    so the model is told nothing at all -- rightly: it has lost all memory of
    starting those tasks.  The user still needs to know."""
    br, state, sent = _bridge()
    _start(state, "b1", "Run the full suite")

    n = asyncio.run(br._orphan_bg_tasks("/clear"))

    assert n == 1
    assert not state.queued_prompts
    assert _notices(sent)


def test_orphaning_nothing_says_nothing(cfgdir):
    br, state, sent = _bridge()

    n = asyncio.run(br._orphan_bg_tasks("a reconnect"))

    assert n == 0
    assert not state.queued_prompts and not _notices(sent)


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
