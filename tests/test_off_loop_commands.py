"""Slash commands that would stall the hub run off its event loop.

Reported by review 2026-10-21: ``/logout`` waits up to twenty seconds on
``claude auth logout``, ``/login`` on ``claude auth status``, and ``/history``
and ``/export`` read a whole transcript (``render_session_history`` says a
600 MB one takes ~40 s).  They ran inline in the WebSocket dispatcher, so every
session on the hub froze meanwhile.  ``commands.run_immediate_command`` runs
them in a worker thread, which is safe only while they never write ``state``.

Also here: ``/logout`` said "Signed out" whatever the CLI answered.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth                                      # noqa: E402
import commands                                  # noqa: E402
import server                                    # noqa: E402
from config import parse_args                    # noqa: E402
from session_runtime import SessionRuntime       # noqa: E402
from state import State                          # noqa: E402


# --------------------------------------------------------------------------
# Which thread a command runs on
# --------------------------------------------------------------------------

def _thread_of(kind, monkeypatch):
    """The thread *kind*'s handler ran on, and the loop's own."""
    ran_on = []
    handler = commands._IMMEDIATE_HANDLERS[kind]

    def record(_payload, _state, _config):
        ran_on.append(threading.get_ident())
        return commands.CommandResult()

    monkeypatch.setattr(commands, handler, record)

    async def go():
        await commands.run_immediate_command(kind, "", State(), parse_args([]))
        return threading.get_ident()

    loop_thread = asyncio.run(go())
    return ran_on[0], loop_thread


@pytest.mark.parametrize("kind", sorted(commands.OFF_LOOP_COMMANDS))
def test_a_slow_command_runs_off_the_event_loop(kind, monkeypatch):
    ran_on, loop_thread = _thread_of(kind, monkeypatch)
    assert ran_on != loop_thread, f"/{kind} ran on the event loop"


def test_every_other_command_stays_on_the_loop(monkeypatch):
    """They may write state, which only the loop's thread may do."""
    ran_on, loop_thread = _thread_of("bell", monkeypatch)
    assert ran_on == loop_thread


def test_only_real_commands_are_named_slow():
    """A misspelt name would leave its command on the loop without a word."""
    assert commands.OFF_LOOP_COMMANDS <= set(commands._IMMEDIATE_HANDLERS)


# --------------------------------------------------------------------------
# They never write state
# --------------------------------------------------------------------------

class _WatchedState(State):
    """A State that records every attribute written once it is armed."""

    def __setattr__(self, name, value):
        if self.__dict__.get("_armed"):
            self.__dict__.setdefault("_writes", []).append(name)
        object.__setattr__(self, name, value)


@pytest.fixture
def slow_world(monkeypatch, tmp_path):
    """Fakes for everything the slow handlers reach: a transcript on disk, the
    auth CLI, and a sign-in that launches."""
    sid = "1aa74fb0-ca4e-42cb-80c3-ef7302fee0a4"
    (tmp_path / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(commands, "find_session_dir", lambda _s, _d=None: tmp_path)
    monkeypatch.setattr(commands, "render_session_history",
                        lambda _j, max_history=0: (0, [], [], []))
    monkeypatch.setattr(commands, "render_session_markdown", lambda _j: "# s\n")
    monkeypatch.setattr(auth, "find_claude_cli", lambda: "claude")
    monkeypatch.setattr(auth, "is_logged_in", lambda _d=None: True)
    monkeypatch.setattr(auth, "launch_login",
                        lambda **_kw: (True, "a window has opened."))
    monkeypatch.setattr(commands.subprocess, "run",
                        lambda *a, **kw: subprocess.CompletedProcess(a, 0, "", ""))
    st = _WatchedState()
    st.session_id = sid
    # So /login takes its launching path, the one that used to clear it.
    st.auth_error = True
    return st, tmp_path


@pytest.mark.parametrize("kind", sorted(commands.OFF_LOOP_COMMANDS))
def test_a_slow_command_writes_no_state(kind, slow_world):
    st, tmp_path = slow_world
    payload = str(tmp_path / "out.md") if kind == "export" else ""
    st._armed = True

    res = commands.try_immediate_command(kind, payload, st, parse_args([]))

    assert res is not None and res.messages, f"/{kind} answered nothing"
    assert not [m for m in res.messages if m.get("subtype") == "error"], res.messages
    assert st.__dict__.get("_writes", []) == [], (
        f"/{kind} wrote state from a worker thread")


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

class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))


class _FakeBridge:
    def __init__(self):
        self.event_queue = asyncio.Queue()

    async def flush_pending_turn_end(self):
        pass

    async def stop(self, **_kw):
        pass


@pytest.fixture
def hub():
    cfg = parse_args([])
    st = State()
    rt = SessionRuntime(config=cfg, state=st, rid="R1")
    rt.bridge = _FakeBridge()
    saved = (dict(server.runtimes), server._default_runtime, server.config,
             server.state, server.bridge, dict(server._ws_runtime))
    server.runtimes.clear()
    server.runtimes[rt.rid] = rt
    server._default_runtime = rt
    server.config = cfg
    server.state = st
    server.bridge = rt.bridge
    yield rt
    server.runtimes.clear()
    server.runtimes.update(saved[0])
    server._default_runtime = saved[1]
    server.config = saved[2]
    server.state = saved[3]
    server.bridge = saved[4]
    server._ws_runtime.clear()
    server._ws_runtime.update(saved[5])


def test_the_hub_runs_history_off_its_loop(hub, monkeypatch):
    """The other half: the dispatcher must go through run_immediate_command."""
    ran_on = []

    def record(_payload, _state, _config):
        ran_on.append(threading.get_ident())
        return commands.CommandResult()

    monkeypatch.setattr(commands, "_cmd_history", record)

    async def go():
        await server._handle_ws_message(_FakeWS(),
                                        {"type": "message", "text": "/history"})
        return threading.get_ident()

    loop_thread = asyncio.run(go())
    assert ran_on and ran_on[0] != loop_thread


def test_a_sign_in_that_launched_clears_the_expired_login(hub, monkeypatch):
    """Optimistic, as before; the hub does it now, on its own thread, since
    the handler runs off the loop.  A fresh 401 sets it again."""
    monkeypatch.setattr(auth, "is_logged_in", lambda _d=None: True)
    monkeypatch.setattr(auth, "launch_login",
                        lambda **_kw: (True, "a window has opened."))

    async def nothing():
        pass

    monkeypatch.setattr(server, "_watch_login", lambda rt, method="claudeai": nothing())
    hub.state.auth_error = True

    asyncio.run(server._handle_ws_message(
        _FakeWS(), {"type": "message", "text": "/login"}))

    assert hub.state.auth_error is False


def test_a_sign_in_that_did_not_launch_leaves_it_set(hub, monkeypatch):
    monkeypatch.setattr(auth, "is_logged_in", lambda _d=None: True)
    monkeypatch.setattr(auth, "launch_login",
                        lambda **_kw: (False, "could not open a window."))
    hub.state.auth_error = True

    asyncio.run(server._handle_ws_message(
        _FakeWS(), {"type": "message", "text": "/login"}))

    assert hub.state.auth_error is True
