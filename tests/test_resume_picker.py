"""``--resume`` with no session: the pickers, and what follows them.

* Esc in the terminal picker ends the launch.  The browser picker is for a
  launch whose terminal picker could not run, and its ``--detach`` child goes
  straight to it rather than running the terminal one a second time.
* Both pickers list the launch's account only, and the browser one says which.
* A session picked, or switched to with ``?resume=`` / ``?cwd=``, is shown at
  ``/?rid=default``: a tab at ``/`` lands in the lobby, a list of sessions that
  reads as another picker.
* A hub waiting in its picker serves the picker only to its own session's tab:
  a launch that joins it, the lobby, and a tab that opens a session get theirs.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server                                            # noqa: E402
from config import parse_args                            # noqa: E402
from starlette.requests import Request                   # noqa: E402

STATIC = Path(__file__).resolve().parent.parent / "static"


# --------------------------------------------------------------------------
# The terminal picker, cancelled
# --------------------------------------------------------------------------

class _WentOn(Exception):
    """main() got past the picker to something that starts or joins a hub."""


def _launch(monkeypatch, tmp_path, picked, argv=("--resume", "--detach")):
    """Run main() for a launch with *argv*, whose terminal picker returns
    *picked*; return what stopped it."""
    cfg = parse_args(list(argv) + ["--cwd", str(tmp_path)])
    monkeypatch.setattr(server, "config", None)        # main() rebinds it
    monkeypatch.setattr(server, "_parse_args_or_report", lambda: cfg)
    monkeypatch.setattr(server, "LAUNCH_ERROR_LOG", str(tmp_path / "launch-error.log"))
    monkeypatch.setenv("ORCH2_NO_DIALOG", "1")
    monkeypatch.setattr(server, "_run_launch_picker", lambda mode, cwd=None: picked)

    def went_on(*_a, **_k):
        raise _WentOn()

    monkeypatch.setattr(server, "_probe_hub", went_on)
    monkeypatch.setattr(server, "_ensure_logged_in", went_on)
    with pytest.raises((SystemExit, _WentOn)) as ei:
        server.main()
    return ei.value


def test_esc_in_the_terminal_picker_ends_the_launch(monkeypatch, tmp_path, capsys):
    stopped = _launch(monkeypatch, tmp_path,
                      {"session_id": None, "cwd": None, "message": None})
    assert isinstance(stopped, SystemExit) and stopped.code == 0, (
        "the launch went on to show the picker again")
    assert "No session was picked, so nothing was started." in capsys.readouterr().out


def test_a_picker_with_nothing_to_pick_says_why(monkeypatch, tmp_path, capsys):
    # The picker may have had a console of its own, gone when it closed.
    stopped = _launch(monkeypatch, tmp_path, {"session_id": None, "cwd": None,
                                              "message": "No sessions found in X"})
    assert isinstance(stopped, SystemExit) and stopped.code == 0
    assert "No sessions found in X" in capsys.readouterr().out


def test_a_picked_session_goes_on_to_open(monkeypatch, tmp_path):
    stopped = _launch(monkeypatch, tmp_path,
                      {"session_id": "abc", "cwd": None, "message": None})
    assert isinstance(stopped, (_WentOn, SystemExit))
    assert not (isinstance(stopped, SystemExit) and stopped.code == 0)


def test_without_a_terminal_picker_the_browsers_is_next(monkeypatch, tmp_path):
    stopped = _launch(monkeypatch, tmp_path, None)
    assert isinstance(stopped, _WentOn), "the launch stopped"
    assert server.config.browser_picker
    assert server.config.resume == server._PICKER_SENTINEL


@pytest.mark.parametrize("written, expected", [
    ({"session_id": None, "cwd": None, "message": "No sessions found in X"},
     {"session_id": None, "cwd": None, "message": "No sessions found in X"}),
    ({"session_id": "abc", "cwd": "D:\\p"},
     {"session_id": "abc", "cwd": "D:\\p", "message": None}),
])
def test_the_terminal_pickers_answer_comes_back_whole(monkeypatch, written, expected):
    # The picker runs in a process of its own, and answers in a file.
    import json
    import subprocess

    class _Proc:
        def wait(self):
            return 0

    def fake_popen(args, **_kw):
        out = args[args.index("--out") + 1]
        with open(out, "w", encoding="utf-8") as f:
            json.dump(written, f)
        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(server, "_console_is_visible", lambda: True)
    monkeypatch.setattr(sys, "platform", "win32")
    assert server._run_launch_picker("resume") == expected


def test_a_picker_process_that_wrote_nothing_could_not_run(monkeypatch):
    import subprocess

    class _Proc:
        def wait(self):
            return 1

    monkeypatch.setattr(subprocess, "Popen", lambda args, **_kw: _Proc())
    monkeypatch.setattr(server, "_console_is_visible", lambda: True)
    monkeypatch.setattr(sys, "platform", "win32")
    assert server._run_launch_picker("resume") is None


def test_the_detach_child_goes_straight_to_the_browser_picker(tmp_path):
    cfg = parse_args(["--resume", "--detach", "--cwd", str(tmp_path)])
    argv = server._detach_child_argv(["server.py", "--resume", "--detach"], cfg, 8420)
    i = argv.index("--resume")
    assert argv[i + 1] == "--browser-picker", argv
    assert server._launch_picker_mode(parse_args(["--resume", "--browser-picker"])) is None
    assert server._launch_picker_mode(parse_args(["--resume"])) == "resume"


def test_a_restart_does_not_carry_the_marker(monkeypatch):
    import subprocess
    import state as state_mod
    seen = {}

    def fake_popen(args, **kw):
        seen["args"] = args
        raise OSError("not really")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sys, "argv", ["server.py", "--resume", "--browser-picker"])
    monkeypatch.setattr(server, "config", parse_args([]))
    st = state_mod.State()
    st.session_id = "sid-1"
    monkeypatch.setattr(server, "state", st)
    monkeypatch.setattr(server, "_picker_mode", False)
    asyncio.run(server.api_restart())
    assert "--browser-picker" not in seen["args"]


# --------------------------------------------------------------------------
# A hub waiting in its picker
# --------------------------------------------------------------------------

@pytest.fixture
def picking(monkeypatch):
    """A hub in picker mode, with its own runtime and a joined session s2."""
    from starlette import responses
    for name in ("HTMLResponse", "RedirectResponse"):
        monkeypatch.setattr(server, name, getattr(responses, name), raising=False)
    monkeypatch.setattr(server, "_picker_mode", True)
    own = types.SimpleNamespace(rid="s1")
    monkeypatch.setattr(server, "_default_runtime", own)
    monkeypatch.setattr(server, "runtimes", {"s1": own, "s2": types.SimpleNamespace(rid="s2")})


def _page(query: str = ""):
    """Which page GET /?*query* serves: "picker" or "chat"."""
    req = Request({"type": "http", "method": "GET", "path": "/",
                   "query_string": query.encode(), "headers": []})
    body = asyncio.run(server.index(req)).body.decode("utf-8")
    return "picker" if "Resume Session" in body else "chat"


@pytest.mark.parametrize("query", ["", "rid=default", "rid=s1"])
def test_the_hubs_own_session_is_still_to_pick(picking, query):
    assert _page(query) == "picker"


@pytest.mark.parametrize("query", ["rid=s2", "lobby=1", "open=abc", "new=1"])
def test_a_tab_for_another_session_gets_its_session(picking, query):
    # The joined session's tab, the lobby, and a session a tab opens itself.
    assert _page(query) == "chat", f"/?{query} was given the picker"


def test_out_of_picker_mode_every_tab_gets_the_chat(picking, monkeypatch):
    monkeypatch.setattr(server, "_picker_mode", False)
    assert _page("") == "chat"


@pytest.mark.parametrize("query", ["resume=abc", "cwd=."])
def test_switching_the_hubs_session_shows_it_not_the_lobby(picking, monkeypatch, query):
    async def reconfigure(**_k):
        return True, None

    monkeypatch.setattr(server, "_picker_mode", False)
    monkeypatch.setattr(server, "_reconfigure", reconfigure)
    req = Request({"type": "http", "method": "GET", "path": "/",
                   "query_string": query.encode(), "headers": []})
    resp = asyncio.run(server.index(req))
    assert resp.status_code == 303
    assert resp.headers["location"] == "/?rid=default"


def test_the_browser_picker_goes_to_the_session_it_picked():
    page = (STATIC / "resume.html").read_text(encoding="utf-8")
    assert "window.location.href = '/?rid=default';" in page
    assert "window.location.href = '/';" not in page


def test_the_browser_picker_is_told_whose_sessions_it_lists(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "list_projects", lambda: [])
    monkeypatch.setattr(server, "account_label", lambda: ".claude-account-c")
    got = asyncio.run(server.api_sessions())
    assert got == {"projects": [], "account": ".claude-account-c"}
    page = (STATIC / "resume.html").read_text(encoding="utf-8")
    assert "sessions on <code>' + esc(data.account)" in page
