"""Slash commands that wait on I/O run off the hub's event loop.

``/login`` and ``/logout`` wait on ``claude auth``; ``/rename`` and
``/export`` can read a whole transcript.  Their handlers are marked
``commands._off_loop``, and ``run_immediate_command`` runs those in a worker
thread, so every other session on the hub keeps running meanwhile.  That is
safe only while they never write ``state``: what they change comes back as
``state_writes`` and is made on the loop, and not at all if the session
changed under them.

``/history`` is drawn the way an attach draws it: "loading session..." first,
so the tab holds what the session says during the read and draws it after the
history.  An ``/export`` of a large transcript takes its turn among the other
large reads.  ``/logout`` reports a logout that failed.
"""

from __future__ import annotations

import asyncio
import copy
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth                                      # noqa: E402
import commands                                  # noqa: E402
import server                                    # noqa: E402
import session                                   # noqa: E402
from config import parse_args                    # noqa: E402
from hubkit import FakeWS                        # noqa: E402
from state import State                          # noqa: E402

SID = "1aa74fb0-ca4e-42cb-80c3-ef7302fee0a4"
SLOW = ("export", "login", "logout", "rename")


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Fakes for everything the slow handlers reach, each noting the thread
    it ran on: a transcript on disk, the title store, the auth CLI, and a
    sign-in that launches."""
    ran = []

    def note(what):
        ran.append((what, threading.get_ident()))

    (tmp_path / f"{SID}.jsonl").write_text("{}\n", encoding="utf-8")
    for mod in (commands, server):
        monkeypatch.setattr(mod, "find_session_dir", lambda _s, _d=None: tmp_path)

    def markdown(_jsonl):
        note("export")
        return "# s\n"

    def write_title(_sid, _title, _d=None):
        note("rename")

    def launch(**_kw):
        note("login")
        return True, "a window has opened."

    def cli(*a, **_kw):
        note("logout")
        return subprocess.CompletedProcess(a, 0, "", "")

    monkeypatch.setattr(commands, "render_session_markdown", markdown)
    monkeypatch.setattr(commands, "write_session_title", write_title)
    monkeypatch.setattr(commands, "read_session_title", lambda *_a: "Old title")
    monkeypatch.setattr(auth, "find_claude_cli", lambda: "claude")
    monkeypatch.setattr(auth, "is_logged_in", lambda _d=None: True)
    monkeypatch.setattr(auth, "launch_login", launch)
    monkeypatch.setattr(commands.subprocess, "run", cli)
    st = State()
    st.session_id = SID
    # So /login takes its launching path, the one with something to change.
    st.auth_error = True
    payload = {"export": str(tmp_path / "out.md"), "rename": "New title",
               "login": "", "logout": ""}
    return SimpleNamespace(state=st, ran=ran, payload=payload, tmp=tmp_path)


def _run(kind, w):
    """Run *kind* as the hub does; returns its result and the loop's thread."""
    async def go():
        res = await commands.run_immediate_command(
            kind, w.payload.get(kind, ""), w.state, parse_args([]))
        return res, threading.get_ident()
    return asyncio.run(go())


# --------------------------------------------------------------------------
# Which thread a command runs on
# --------------------------------------------------------------------------

def test_the_slow_commands_are_the_ones_marked():
    marked = {kind for kind, name in commands._IMMEDIATE_HANDLERS.items()
              if getattr(getattr(commands, name), "off_loop", False)}
    assert marked == set(SLOW)


@pytest.mark.parametrize("kind", SLOW)
def test_a_slow_command_runs_off_the_event_loop(kind, world):
    _res, loop_thread = _run(kind, world)
    threads = [t for what, t in world.ran if what == kind]
    assert threads, f"/{kind} never reached its slow part"
    assert loop_thread not in threads, f"/{kind} ran on the event loop"


def test_every_other_command_stays_on_the_loop(monkeypatch):
    """They write state, which only the loop's thread may do."""
    ran_on = []

    def record(_payload, _state, _config):
        ran_on.append(threading.get_ident())
        return commands.CommandResult()

    monkeypatch.setattr(commands, "_cmd_bell", record)
    world = SimpleNamespace(payload={}, state=State())
    _res, loop_thread = _run("bell", world)
    assert ran_on == [loop_thread]


# --------------------------------------------------------------------------
# What they change, they change on the loop
# --------------------------------------------------------------------------

def _snapshot(st):
    return {k: copy.deepcopy(v) for k, v in vars(st).items()}


@pytest.mark.parametrize("kind", SLOW)
def test_a_slow_handler_writes_no_state(kind, world):
    """Not an attribute, nor anything inside one: run as the thread runs it."""
    before = _snapshot(world.state)
    res = commands._handler_for(kind)(world.payload[kind], world.state,
                                      parse_args([]))
    assert res is not None and res.messages, f"/{kind} answered nothing"
    assert not [m for m in res.messages if m.get("subtype") == "error"], res.messages
    assert _snapshot(world.state) == before, f"/{kind} wrote state itself"


class _NotesWriters(State):
    """A State that remembers which thread last set each attribute."""

    def __setattr__(self, name, value):
        self.__dict__.setdefault("_set_on", {})[name] = threading.get_ident()
        object.__setattr__(self, name, value)


def test_a_rename_lands_on_the_loop(world):
    st = _NotesWriters()
    st.session_id = SID
    world.state = st

    res, loop_thread = _run("rename", world)

    assert (st.session_title, st.human_title, st.human_title_sid) == \
        ("New title", "New title", SID)
    for name in ("session_title", "human_title", "human_title_sid"):
        assert st._set_on[name] == loop_thread, f"{name} set off the loop"
    assert res.forward_payload == "/rename New title"


def test_a_rename_before_the_session_starts_is_kept_for_it(world):
    world.state.session_id = None

    res, _loop = _run("rename", world)

    assert world.state.pending_rename == "New title"
    assert world.state.session_title == "New title"
    assert res.forward_payload == "/rename New title"


def test_a_sign_in_that_launched_clears_the_expired_login(world):
    """Optimistic: a fresh 401 sets it again."""
    _run("login", world)
    assert world.state.auth_error is False


def test_a_sign_in_that_did_not_launch_leaves_it_set(world, monkeypatch):
    monkeypatch.setattr(auth, "launch_login",
                        lambda **_kw: (False, "could not open a window."))
    _run("login", world)
    assert world.state.auth_error is True


def test_what_was_for_a_session_that_changed_meanwhile_is_dropped(world,
                                                                  monkeypatch):
    """A /clear or /resume from another tab while the title was written: the
    new session must not take the old one's title, nor its CLI the /rename."""
    def write_title(_sid, _title, _d=None):
        world.state.session_id = "0bb85fc1-db5f-43dc-91d4-f0841ff1f1b5"

    monkeypatch.setattr(commands, "write_session_title", write_title)

    res, _loop = _run("rename", world)

    assert world.state.session_title != "New title"
    assert res.forward_to_sdk is False and res.forward_payload is None
    assert res.messages, "the rename that was made went unreported"


def test_a_session_that_starts_meanwhile_keeps_what_was_meant_for_it(monkeypatch):
    """No session yet: nothing it could have been meant for has gone."""
    def handler(_payload, st, _config):
        st.session_id = SID              # its init, landing meanwhile
        return commands.CommandResult(state_writes={"pending_rename": "t"},
                                      forward_to_sdk=True,
                                      forward_payload="/rename t")

    handler.off_loop = True
    monkeypatch.setattr(commands, "_cmd_rename", handler)
    world = SimpleNamespace(payload={"rename": "t"}, state=State())

    res, _loop = _run("rename", world)

    assert world.state.pending_rename == "t"
    assert res.forward_payload == "/rename t"


def test_title_writes_take_turns(monkeypatch):
    """Two /renames can now overlap, and healing a transcript's tail can
    truncate it under a record the other has just appended."""
    held = []
    monkeypatch.setattr(session, "_write_session_title",
                        lambda *_a: held.append(session._title_write_lock.locked()))
    session.write_session_title(SID, "t")
    assert held == [True]


# --------------------------------------------------------------------------
# /logout says what happened
# --------------------------------------------------------------------------

def _logout(monkeypatch, returncode, stdout="", stderr=""):
    monkeypatch.setattr(auth, "find_claude_cli", lambda: "claude")
    monkeypatch.setattr(
        commands.subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, returncode, stdout, stderr))
    res = commands.try_immediate_command("logout", "", State(), parse_args([]))
    (msg,) = res.messages
    return msg["subtype"], msg["data"]["message"]


def test_a_failed_logout_says_so(monkeypatch):
    """What the CLI does when it cannot sign out: "Failed to log out." on
    stderr, exit 1.  The account is still signed in, and still billed."""
    level, text = _logout(monkeypatch, 1, stderr="Failed to log out.\n")
    assert level == "error"
    assert "Failed to log out." in text
    assert "Signed out" not in text


def test_a_failed_logout_with_nothing_to_say_names_the_exit_status(monkeypatch):
    level, text = _logout(monkeypatch, 3)
    assert level == "error"
    assert "exited 3" in text


def test_a_logout_that_worked_says_signed_out(monkeypatch):
    level, text = _logout(
        monkeypatch, 0,
        stdout="Successfully logged out from your Anthropic account.\n")
    assert level == "info"
    assert text.startswith("Signed out.")


# --------------------------------------------------------------------------
# Through the hub
# --------------------------------------------------------------------------

def _type(text, ws=None):
    """*text* typed in a tab already viewing the hub's session; returns the
    tab and the loop's thread."""
    ws = ws or FakeWS()
    # Attached already, so the message is not preceded by an attach, which
    # sends a history of its own.
    server._ws_runtime[ws] = server._default_runtime

    async def go():
        await server._handle_ws_message(ws, {"type": "message", "text": text})
        return threading.get_ident()

    return ws, asyncio.run(go())


def test_the_hub_runs_a_slow_command_off_its_loop(hub, world):
    _ws, loop_thread = _type("/logout")
    threads = [t for what, t in world.ran if what == "logout"]
    assert threads and loop_thread not in threads


def test_the_hub_makes_what_a_slow_command_changes(hub, world):
    hub.state.auth_error = True
    _type("/login")
    assert hub.state.auth_error is False


@pytest.fixture
def transcript(hub, world, monkeypatch):
    """The hub's session on disk, and a render of it that notes how it was
    called and what the tab had been sent by then."""
    hub.state.session_id = SID
    reads = []

    def render(jsonl, **kw):
        reads.append(SimpleNamespace(thread=threading.get_ident(), kw=kw,
                                     sent=[dict(m) for m in reads_ws[0].sent]))
        return 1, [{"type": "user_message", "content": "hi"}], [], []

    reads_ws = [FakeWS()]
    monkeypatch.setattr(server, "render_session_history", render)
    return SimpleNamespace(ws=reads_ws[0], reads=reads)


def test_history_holds_the_live_view_before_it_reads(transcript):
    """Cleared, then "loading session..." on, both before the read: what the
    session says meanwhile is held by the tab and drawn after the history."""
    _ws, loop_thread = _type("/history 50", transcript.ws)

    (read,) = transcript.reads
    assert read.thread != loop_thread, "the transcript was read on the loop"
    assert read.kw == {"max_history": 50}
    assert [(m["type"], m.get("on")) for m in read.sent][-2:] == \
        [("clear_screen", None), ("session_loading", True)]
    assert transcript.ws.sent[-1]["type"] == "history"


def test_history_without_a_count_leaves_the_default(transcript):
    _type("/history", transcript.ws)
    assert transcript.reads[0].kw == {}


@pytest.mark.parametrize("arg, level, text", [
    ("x", "error", "Usage: /history [N]"),
    ("0", "error", "N must be positive."),
])
def test_history_refuses_a_bad_count_without_clearing(transcript, arg, level, text):
    ws, _loop = _type(f"/history {arg}", transcript.ws)
    assert [(m["subtype"], m["data"]["message"]) for m in ws.sent
            if m["type"] == "system_msg"] == [(level, text)]
    assert not [m for m in ws.sent if m["type"] == "clear_screen"]
    assert transcript.reads == []


def test_history_of_a_session_not_on_disk_says_so_without_clearing(transcript,
                                                                   monkeypatch):
    monkeypatch.setattr(server, "find_session_dir", lambda _s, _d=None: None)
    ws, _loop = _type("/history", transcript.ws)
    assert any("not found on disk" in m["data"]["message"]
               for m in ws.sent if m["type"] == "system_msg")
    assert not [m for m in ws.sent if m["type"] == "clear_screen"]


def test_history_with_nothing_in_it_turns_loading_off_and_says_so(transcript,
                                                                  monkeypatch):
    monkeypatch.setattr(server, "render_session_history",
                        lambda _j, **_kw: (0, [], [], []))
    ws, _loop = _type("/history", transcript.ws)
    assert {"type": "session_loading", "on": False} in ws.sent
    assert any(m["data"]["message"] == "No history to display."
               for m in ws.sent if m["type"] == "system_msg")


def test_a_large_export_takes_its_turn_among_the_large_reads(hub, world,
                                                             monkeypatch):
    import sdk_bridge
    monkeypatch.setattr(sdk_bridge, "HEAVY_CONNECT_BYTES", 0)
    monkeypatch.setattr(sdk_bridge, "_heavy_connect_lock", None)
    hub.state.session_id = SID
    held = []

    def markdown(_jsonl):
        held.append(sdk_bridge._heavy_lock().locked())
        return "# s\n"

    monkeypatch.setattr(commands, "render_session_markdown", markdown)
    _type(f"/export {world.tmp / 'out.md'}")
    assert held == [True]
