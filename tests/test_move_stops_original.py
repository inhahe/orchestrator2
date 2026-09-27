"""``/move`` stops the session it moves.

Reported 2026-09-27: "i did a /move to move a session to another account, and
it apparently never killed the old session. now both versions of the session
are running."

``_do_move`` copied the transcript, started a runtime on the copy and attached
the tab to it -- and never touched the original.  An idle, viewer-less
original is eventually closed by the idle timer, but one that is busy, has
background tasks running or a loop scheduled is "working, not idle" and never
is, so both copies went on acting on one conversation.

Now the original is stopped, *before* the copy is taken so the copy starts
from its final transcript, and what would be lost with it comes across:
queued prompts (taken from the original's queue file too, or reopening it
would send them again), the loop's pending wakeup, the background-task record
(so the copy is told which tasks died) and its explicit name and labels.  If
anything after the stop fails, the original is reopened -- and if even that
fails, the tab is sent to the session list rather than left on a dead session.

These drive the real ``_do_move`` and the real ``_teardown_runtime`` against a
runtime registered with the hub; only the SDK side is faked.
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

import server                                            # noqa: E402
import wakeup_store                                      # noqa: E402
from config import parse_args                            # noqa: E402
from copy_session import _sanitize_cwd                   # noqa: E402
from session import load_persisted_bg_tasks, load_persisted_queue  # noqa: E402
from session_runtime import SessionRuntime               # noqa: E402
from state import init_state_from_config                 # noqa: E402

SID = "11111111-1111-1111-1111-111111111111"


class _Bridge:
    """The SDK side: records being stopped and having a loop armed on it."""

    def __init__(self, events):
        self.events = events
        self.armed: list[tuple[float, str]] = []

    async def stop(self):
        self.events.append("stop")

    def _arm_wakeup(self, delay, prompt):
        self.armed.append((delay, prompt))


class _Ws:
    def __init__(self, name):
        self.name = name


@pytest.fixture
def move(monkeypatch, tmp_path):
    """A live original registered with the hub, and a way to /move it."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "hub-account"))
    src_cwd = tmp_path / "work"
    src_cwd.mkdir()
    src_cfg = tmp_path / "acct-a"
    dst_cfg = tmp_path / "acct-b"
    src_proj = src_cfg / "projects" / _sanitize_cwd(str(src_cwd))
    src_proj.mkdir(parents=True)
    (src_proj / f"{SID}.jsonl").write_text(
        json.dumps({"type": "user", "sessionId": SID, "cwd": str(src_cwd),
                    "message": {"role": "user", "content": "hi"}}) + "\n",
        encoding="utf-8")

    events: list[str] = []
    cfg = parse_args(["--cwd", str(src_cwd), "--config-dir", str(src_cfg)])
    st = init_state_from_config(cfg)
    st.session_id = SID
    # As _create_runtime does: the queue is mirrored to disk on every change.
    server._attach_queue_persistence(st, cfg.cwd)
    orig = SessionRuntime(config=cfg, state=st, rid="orig")
    orig.bridge = _Bridge(events)
    mover, watcher = _Ws("mover"), _Ws("watcher")
    orig.add_client(mover)
    orig.add_client(watcher)

    sent: dict[str, list[dict]] = {"mover": [], "watcher": []}
    created: list[dict] = []
    attached: list[tuple[str, str]] = []
    fail = {"copy_runtime": False, "reopen": False}
    lobby: list[str] = []
    # Whether a runtime restores its queue from disk as it starts, as the real
    # _create_runtime does when the file is where it looks (the hub's own
    # account).  Off by default: the other-account case, where it is not.
    opts = {"restore_queue": False}

    async def fake_send_to(ws, m):
        sent.setdefault(ws.name, []).append(m)

    async def fake_create_runtime(**kw):
        created.append(kw)
        if fail["copy_runtime"] and kw.get("resume") != SID:
            raise RuntimeError("the copy would not start")
        if fail["reopen"] and kw.get("resume") == SID:
            raise RuntimeError("the original would not start either")
        c = parse_args(["--cwd", kw["cwd"]])
        s = init_state_from_config(c)
        s.session_id = kw["resume"]
        if opts["restore_queue"]:
            server._attach_queue_persistence(s, c.cwd)
        rt = SessionRuntime(config=c, state=s,
                            rid="reopened" if kw.get("resume") == SID else "copy")
        rt.bridge = _Bridge(events)
        return rt

    async def fake_attach(ws, rt):
        attached.append((ws.name, rt.rid))

    async def nothing(*a, **k):
        pass

    async def fake_enter_lobby(ws):
        lobby.append(ws.name)

    import copy_session
    real_copy = copy_session.copy_session_file

    def recording_copy(*a, **k):
        events.append("copy")
        return real_copy(*a, **k)

    monkeypatch.setattr(copy_session, "copy_session_file", recording_copy)
    monkeypatch.setattr(copy_session, "git_branch_for", lambda cwd: "")
    monkeypatch.setattr(server, "send_to", fake_send_to)
    monkeypatch.setattr(server, "_create_runtime", fake_create_runtime)
    monkeypatch.setattr(server, "_attach_ws", fake_attach)
    monkeypatch.setattr(server, "_enter_lobby", fake_enter_lobby)
    monkeypatch.setattr(server, "_push_session_list", nothing)
    monkeypatch.setattr(server, "write_session_title", lambda *a, **k: None)
    monkeypatch.setattr(server, "find_session_dir",
                        lambda s, c=None: src_proj if s == SID else None)
    monkeypatch.setattr(server, "runtimes", {"orig": orig})
    monkeypatch.setattr(server, "_ws_runtime", {mover: orig, watcher: orig})
    monkeypatch.setattr(server, "_default_runtime", None)

    def run():
        asyncio.run(server._do_move(mover, {
            "config_dir": str(dst_cfg), "new_name": "the copy", "cwd": ""}))

    return {"orig": orig, "state": st, "events": events, "sent": sent,
            "created": created, "attached": attached, "fail": fail, "run": run,
            "opts": opts, "lobby": lobby,
            "cwd": str(src_cwd), "mover": mover, "watcher": watcher}


def _new_id(m):
    return next(kw["resume"] for kw in m["created"] if kw["resume"] != SID)


# --------------------------------------------------------------------------
# The original stops
# --------------------------------------------------------------------------

def test_the_original_is_stopped(move):
    """The report."""
    move["run"]()

    assert "stop" in move["events"]
    assert "orig" not in server.runtimes


def test_it_is_stopped_before_the_copy_is_taken(move):
    """So the copy starts from the original's final transcript, not one it
    went on writing to after the copy was made."""
    move["run"]()

    assert move["events"].index("stop") < move["events"].index("copy")


def test_this_tab_goes_with_the_conversation(move):
    """Not back to the lobby with the tabs that stay behind."""
    move["run"]()

    assert not [m for m in move["sent"]["mover"] if m.get("type") == "session_closed"]
    assert ("mover", "copy") in move["attached"]


def test_another_tab_on_the_original_is_told_where_it_went(move):
    move["run"]()

    closed = [m for m in move["sent"]["watcher"] if m.get("type") == "session_closed"]
    assert closed and "moved" in closed[0]["message"]
    assert "the copy" in closed[0]["message"]


def test_the_copy_is_told_the_original_stopped(move):
    move["run"]()

    note = next(kw for kw in move["created"] if kw["resume"] != SID)["session_note"]
    assert "was stopped" in note and "may still be running" not in note


def test_the_user_is_told_too(move):
    move["run"]()

    said = " ".join((m.get("data") or {}).get("message", "")
                    for m in move["sent"]["mover"] if m.get("type") == "system_msg")
    assert "original session was stopped" in said


# --------------------------------------------------------------------------
# What comes across
# --------------------------------------------------------------------------

def test_the_user_is_told_the_queue_came_along(move):
    move["state"].queued_prompts.extend(["next thing", "and then this"])

    move["run"]()

    said = " ".join((m.get("data") or {}).get("message", "")
                    for m in move["sent"]["mover"] if m.get("type") == "system_msg")
    assert "2 queued prompts came along" in said, said


def test_the_queue_lands_in_the_copy_itself(move, monkeypatch):
    move["state"].queued_prompts.extend(["next thing"])
    landed = _capture_runtimes(monkeypatch)

    move["run"]()

    assert list(landed["copy"].state.queued_prompts) == ["next thing"]


def test_reopening_the_original_will_not_send_its_queue_again(move):
    """A closed session keeps its queue file, and reopening one restores that
    file and *sends* it.  After a move those prompts belong to the copy; left
    behind, opening the original from Recent would run them a second time."""
    move["state"].queued_prompts.extend(["next thing"])
    assert load_persisted_queue(move["cwd"], SID) == ["next thing"]   # precondition

    move["run"]()

    assert load_persisted_queue(move["cwd"], SID) == []


def _capture_runtimes(monkeypatch):
    landed = {}
    real = server._create_runtime

    async def capture(**kw):
        rt = await real(**kw)
        landed[rt.rid] = rt
        return rt

    monkeypatch.setattr(server, "_create_runtime", capture)
    return landed


def test_a_failed_move_leaves_the_original_its_queue(move, monkeypatch):
    """Only a move that *worked* takes the queue away.  The reopen gets it
    back even where its own restore finds nothing: that read looks in the
    session's account while every save lands in the hub's."""
    move["state"].queued_prompts.extend(["next thing"])
    move["fail"]["copy_runtime"] = True
    landed = _capture_runtimes(monkeypatch)

    move["run"]()

    assert list(landed["reopened"].state.queued_prompts) == ["next thing"]
    assert load_persisted_queue(move["cwd"], SID) == ["next thing"]


def test_a_reopen_that_finds_its_queue_does_not_get_it_twice(move, monkeypatch):
    """The hub's own account: the reopen restores the queue itself, and
    putting it back by hand as well would run every prompt twice."""
    move["opts"]["restore_queue"] = True
    move["state"].queued_prompts.extend(["next thing"])
    move["fail"]["copy_runtime"] = True
    landed = _capture_runtimes(monkeypatch)

    move["run"]()

    assert list(landed["reopened"].state.queued_prompts) == ["next thing"]


def test_a_loop_due_during_the_move_waits_for_the_copy_to_settle(move, monkeypatch):
    """The copy's CLI has only just started.  Firing now would find it "not
    busy" and inject the prompt beside the interrupted turn it is about to
    resume -- the restart path waits for the same reason."""
    wakeup_store.save_wakeup(move["cwd"], SID, due_at=time.time() + 3,
                             prompt="check the build")
    landed = _capture_runtimes(monkeypatch)

    move["run"]()

    [(delay, _prompt)] = landed["copy"].bridge.armed
    assert delay == server.WAKEUP_RESTORE_SETTLE_S


def test_the_loop_comes_along_due_when_it_was(move, monkeypatch):
    wakeup_store.save_wakeup(move["cwd"], SID, due_at=time.time() + 600,
                             prompt="check the build")
    landed = _capture_runtimes(monkeypatch)

    move["run"]()

    armed = landed["copy"].bridge.armed
    assert len(armed) == 1 and armed[0][1] == "check the build"
    assert 590 <= armed[0][0] <= 600


def test_the_name_and_labels_come_along(move):
    """The same agent, carrying on; the original's registration went with it."""
    move["state"].agent_name = "Lane-A"
    move["state"].agent_labels = {"lane": "a"}

    move["run"]()

    kw = next(kw for kw in move["created"] if kw["resume"] != SID)
    assert kw["agent_name"] == "Lane-A" and kw["agent_labels"] == {"lane": "a"}


def test_the_copy_is_told_which_background_tasks_died(move):
    """They were processes of the original's CLI; they cannot move.  The copy
    gets the same notice a session cut off by a restart does."""
    move["state"].background_tasks = {
        "t1": {"name": "full build", "command": "make all",
               "started_at": time.monotonic() - 120}}

    move["run"]()

    items = load_persisted_bg_tasks(move["cwd"], _new_id(move))
    assert [i["name"] for i in items] == ["full build"]


# --------------------------------------------------------------------------
# A move that fails after the stop
# --------------------------------------------------------------------------

def test_a_copy_that_will_not_start_puts_the_original_back(move):
    move["fail"]["copy_runtime"] = True

    move["run"]()

    assert any(kw["resume"] == SID for kw in move["created"]), "original not reopened"
    assert ("mover", "reopened") in move["attached"]
    errors = [m["message"] for m in move["sent"]["mover"] if m.get("type") == "move_error"]
    assert errors and "reopened" in errors[0]


def test_the_reopened_original_keeps_its_loop(move, monkeypatch):
    """The stop erased the wakeup record; reopening must re-arm it."""
    wakeup_store.save_wakeup(move["cwd"], SID, due_at=time.time() + 300,
                             prompt="check the build")
    move["fail"]["copy_runtime"] = True
    landed = _capture_runtimes(monkeypatch)

    move["run"]()

    assert [p for _d, p in landed["reopened"].bridge.armed] == ["check the build"]


def test_if_even_the_reopen_fails_this_tab_is_not_left_on_a_dead_session(move):
    """Told the original could not be reopened, and sent to the session list
    it is told to open it from -- not left showing, and able to type into, a
    session that has been stopped."""
    move["fail"]["copy_runtime"] = True
    move["fail"]["reopen"] = True

    move["run"]()

    errors = [m["message"] for m in move["sent"]["mover"] if m.get("type") == "move_error"]
    assert errors and "could not be reopened" in errors[0]
    assert "mover" in move["lobby"]


def test_a_successful_move_does_not_send_this_tab_to_the_lobby(move):
    move["run"]()

    assert "mover" not in move["lobby"]
    assert "watcher" in move["lobby"]


def test_a_failed_copy_puts_the_original_back_too(move, monkeypatch):
    import copy_session

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(copy_session, "copy_session_file", broken)

    move["run"]()

    assert ("mover", "reopened") in move["attached"]
