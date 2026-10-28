"""``--confirm-close``: a tab asks before it closes on a session not resting idle.

Asked 2026-10-27: "add a command-line parameter to make orchestrator2 confirm
with a popup before the user can close a tab that's in any state other than
idle (and maybe waiting for a loop)".  The asking is the page's
(static/leave_guard.js, tests/leave_guard.test.js); here, that the flag
reaches it: in every session's status snapshot, from the hub's own launch or
from a launch that joins the hub.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server                                         # noqa: E402
from config import parse_args                         # noqa: E402
from state import init_state_from_config, state_to_status_dict  # noqa: E402


def _status(argv):
    cfg = parse_args(argv)
    return state_to_status_dict(init_state_from_config(cfg), cfg)


def test_the_flag_reaches_the_tab():
    assert _status(["--confirm-close"])["confirm_close"] is True
    assert _status([])["confirm_close"] is False


def test_a_launch_that_joins_the_hub_hands_it_over():
    """A joining launch builds no Config the hub uses: a flag it does not
    hand over is dropped."""
    assert server._hub_launch_kwargs(parse_args(["--confirm-close"]), None)["confirm_close"] is True
    assert server._hub_launch_kwargs(parse_args([]), None)["confirm_close"] is False


class _RT:
    def __init__(self, rid, cfg):
        self.rid = rid
        self.config = cfg
        self.bridge = None
        self.state = None


def _hub(monkeypatch, tmp_path):
    """A hub started without the flag, with a session open; returns what a
    launch would create, and the open session."""
    open_rt = _RT("R1", parse_args([]))
    monkeypatch.setattr(server, "config", parse_args([]))
    monkeypatch.setattr(server, "runtimes", {"R1": open_rt})
    monkeypatch.setattr(server, "_default_runtime", None)
    created = {}

    async def create(**kw):
        created["confirm_close"] = server.config.confirm_close
        return _RT("R2", server.config)

    monkeypatch.setattr(server, "_create_runtime", create)
    return created, open_rt


def test_a_launch_with_it_turns_it_on_for_the_hub(monkeypatch, tmp_path):
    """For the session it opens, the ones open already, and those opened
    from now: a preference about tabs, not about one session."""
    created, open_rt = _hub(monkeypatch, tmp_path)

    asyncio.run(server.api_session_launch({"cwd": str(tmp_path), "no_continue": True,
                                           "confirm_close": True}))

    assert created["confirm_close"] is True
    assert open_rt.config.confirm_close is True
    assert server.config.confirm_close is True


def test_and_one_without_it_leaves_it_as_it_was(monkeypatch, tmp_path):
    created, open_rt = _hub(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "config", parse_args(["--confirm-close"]))

    asyncio.run(server.api_session_launch({"cwd": str(tmp_path), "no_continue": True}))

    assert server.config.confirm_close is True
    assert created["confirm_close"] is True
    assert open_rt.config.confirm_close is False, "a launch turned on what it did not ask for"


def _restart_argv(monkeypatch, argv, cfg):
    """The command line a Restart gives the hub that replaces this one."""
    import subprocess
    from state import State
    seen = {}

    def fake_popen(args, **kw):
        seen["argv"] = list(args)
        raise OSError("not really")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(server, "config", cfg)
    st = State()
    st.session_id = "sid-1"
    monkeypatch.setattr(server, "state", st)
    asyncio.run(server.api_restart())
    return seen["argv"]


def test_a_restart_keeps_it_when_a_joining_launch_turned_it_on(monkeypatch):
    """The hub's own command line does not have it: a launch that joined
    gave it, and nothing takes it back."""
    argv = _restart_argv(monkeypatch, ["server.py"], parse_args(["--confirm-close"]))

    assert argv.count("--confirm-close") == 1, argv


def test_and_does_not_say_it_twice(monkeypatch):
    argv = _restart_argv(monkeypatch, ["server.py", "--confirm-close"],
                         parse_args(["--confirm-close"]))

    assert argv.count("--confirm-close") == 1, argv


def test_a_launch_the_hub_refuses_changes_nothing(monkeypatch, tmp_path):
    """Refused for the name it asked to give a session it did not choose."""
    created, open_rt = _hub(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_session_to_open", lambda *a, **k: "sid-x")
    monkeypatch.setattr(server, "_disk_names", lambda *a, **k: ("Old name", None))
    monkeypatch.setattr(server, "_rename_refusal", lambda *a, **k: "refused")

    got = asyncio.run(server.api_session_launch({
        "cwd": str(tmp_path), "agent_name": "New name", "confirm_close": True}))

    assert got.get("refused") is True, got
    assert server.config.confirm_close is False
    assert open_rt.config.confirm_close is False


def test_the_tab_is_told_of_a_resume_after_a_usage_limit():
    """Work to come, as a loop's wakeup is: the tab's dot, and the guard on
    closing it, count it (static/util.js restsIdle)."""
    cfg = parse_args([])
    st = init_state_from_config(cfg)
    st.limit_resume_at = 1893456000.0

    assert state_to_status_dict(st, cfg)["limit_resume_at"] == 1893456000.0
