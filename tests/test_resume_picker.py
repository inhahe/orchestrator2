"""``--resume`` with no session: the pickers, and what follows them.

* Esc in the terminal picker, or closing its window, ends the launch.  The
  browser picker is for a launch whose terminal picker could not run or
  crashed, and its ``--detach`` child goes straight to it rather than running
  the terminal one a second time.
* What the picker says about an empty choice is said where it can be read.
* Both pickers list the launch's account only, and the browser one says which.
* A session picked, or switched to with ``?resume=`` / ``?cwd=``, is shown at
  ``/?rid=default``: a tab at ``/`` lands in the lobby, a list of sessions that
  reads as another picker.
* A hub waiting in its picker serves the chat page to every tab, and sends a
  tab that reaches its own session, still to be picked, to the picker.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
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
# The launch, after its terminal picker
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
    # What the picker chose is settled as given: this is about the picker.
    monkeypatch.setattr(server, "_settle_launch_resume", lambda c: (c, None, None))

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


def test_a_picker_with_nothing_to_pick_says_why_once(monkeypatch, tmp_path, capsys):
    stopped = _launch(monkeypatch, tmp_path, {"session_id": None, "cwd": None,
                                              "message": "No sessions found in X"})
    assert isinstance(stopped, SystemExit) and stopped.code == 0
    out = capsys.readouterr().out
    assert out.count("No sessions found in X") == 1, out
    assert "No session was picked" not in out


def test_from_a_hidden_console_it_is_said_in_a_dialog(monkeypatch, tmp_path):
    # A tray launch: its console is hidden, and the picker's own is gone.
    shown = []
    monkeypatch.setattr(server, "_console_is_hidden", lambda: True)
    monkeypatch.setattr(server, "_show_error_dialog", lambda title, body: shown.append(body))
    cfg = parse_args(["--resume", "--detach", "--cwd", str(tmp_path)])
    monkeypatch.setattr(server, "config", None)
    monkeypatch.setattr(server, "_parse_args_or_report", lambda: cfg)
    monkeypatch.delenv("ORCH2_NO_DIALOG", raising=False)
    monkeypatch.setattr(server, "_run_launch_picker", lambda mode, cwd=None: {
        "session_id": None, "cwd": None, "message": "No sessions found in X"})
    with pytest.raises(SystemExit):
        server.main()
    assert shown == ["No sessions found in X"]


def test_a_picked_session_goes_on_to_open(monkeypatch, tmp_path):
    stopped = _launch(monkeypatch, tmp_path,
                      {"session_id": "abc", "cwd": None, "message": None})
    assert isinstance(stopped, _WentOn), stopped
    assert server.config.resume == "abc"


def test_a_failed_copy_says_so_and_starts_afresh(monkeypatch, tmp_path, capsys):
    stopped = _launch(monkeypatch, tmp_path, {"session_id": None, "cwd": None,
                                              "message": "copy failed: disk full"},
                      argv=("--copy", "--detach"))
    assert isinstance(stopped, _WentOn)
    assert "copy failed: disk full" in capsys.readouterr().out
    assert server.config.no_continue and not server.config.copy


def test_without_a_terminal_picker_the_browsers_is_next(monkeypatch, tmp_path):
    stopped = _launch(monkeypatch, tmp_path, None)
    assert isinstance(stopped, _WentOn), "the launch stopped"
    assert server.config.browser_picker
    assert server.config.resume == server._PICKER_SENTINEL


def test_only_that_launchs_detach_child_goes_straight_to_the_browser(tmp_path):
    marked = parse_args(["--resume", "--detach", "--browser-picker", "--cwd", str(tmp_path)])
    argv = server._detach_child_argv(["server.py", "--resume", "--detach"], marked, 8420)
    assert argv[argv.index("--resume") + 1] == "--browser-picker", argv
    plain = parse_args(["--resume", "--detach", "--cwd", str(tmp_path)])
    assert "--browser-picker" not in server._detach_child_argv(["server.py"], plain, 8420)
    assert server._launch_picker_mode(parse_args(["--resume", "--browser-picker"])) is None
    assert server._launch_picker_mode(parse_args(["--resume"])) == "resume"


# --------------------------------------------------------------------------
# The terminal picker's answer
# --------------------------------------------------------------------------

def _picker_process(monkeypatch, *, writes=None, code=0):
    """Make the picker process write *writes* (nothing if None) and exit with
    *code*; return what _run_launch_picker makes of it."""
    class _Proc:
        def wait(self):
            return code

    def fake_popen(args, **_kw):
        if writes is not None:
            with open(args[args.index("--out") + 1], "w", encoding="utf-8") as f:
                json.dump(writes, f)
        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(server, "_console_is_visible", lambda: True)
    monkeypatch.setattr(sys, "platform", "win32")
    return server._run_launch_picker("resume")


@pytest.mark.parametrize("written, expected", [
    ({"session_id": None, "cwd": None, "message": "No sessions found in X"},
     {"session_id": None, "cwd": None, "message": "No sessions found in X"}),
    ({"session_id": "abc", "cwd": "D:\\p"},
     {"session_id": "abc", "cwd": "D:\\p", "message": None}),
])
def test_the_terminal_pickers_answer_comes_back_whole(monkeypatch, written, expected):
    assert _picker_process(monkeypatch, writes=written) == expected


def test_a_picker_process_that_wrote_nothing_could_not_run(monkeypatch):
    assert _picker_process(monkeypatch, code=1) is None


@pytest.mark.parametrize("code", [0xC000013A, 0xC000013A - (1 << 32)])
def test_a_picker_window_closed_with_x_is_left_like_esc(monkeypatch, code):
    assert _picker_process(monkeypatch, code=code) == {
        "session_id": None, "cwd": None, "message": None}


def _tui(monkeypatch, *, chosen=None, message=None, return_code=0):
    """The picker app, faked: what it would have chosen and said."""
    import copy_session_tui

    class FakeApp:
        def __init__(self, *_a):
            self.chosen_id, self.chosen_cwd = chosen, None
            self.status_message, self.return_code = message, return_code

        def run(self):
            pass

    monkeypatch.setattr(copy_session_tui, "LaunchPickerApp", FakeApp)
    return copy_session_tui


def test_the_picker_says_why_it_chose_nothing(monkeypatch, capsys):
    tui = _tui(monkeypatch, message="No sessions found in X")
    assert tui.pick_session_for_launch("resume") == {
        "session_id": None, "cwd": None, "message": "No sessions found in X"}
    # Said by the launch (main), once: a picker sharing its console would
    # otherwise print it a second time.
    assert capsys.readouterr().out == ""


def test_a_picker_that_crashed_gives_no_answer(monkeypatch, tmp_path):
    # Textual prints the error and returns; it is not the user leaving.
    tui = _tui(monkeypatch, return_code=1)
    assert tui.pick_session_for_launch("resume") is None
    out = tmp_path / "out.json"
    monkeypatch.setattr(sys, "argv", ["copy_session.py", "--pick", "resume", "--out", str(out)])
    assert tui.main() == 1
    assert not out.exists() or out.read_text(encoding="utf-8") == ""


def test_the_picker_process_writes_its_whole_answer(monkeypatch, tmp_path):
    tui = _tui(monkeypatch, chosen="abc", message=None)
    out = tmp_path / "out.json"
    monkeypatch.setattr(sys, "argv", ["copy_session.py", "--pick", "resume", "--out", str(out)])
    assert tui.main() == 0
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "session_id": "abc", "cwd": None, "message": None}


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
    own = types.SimpleNamespace(rid="s1", state=object(), config=object())
    monkeypatch.setattr(server, "_default_runtime", own)
    monkeypatch.setattr(server, "runtimes", {"s1": own})
    return own


def _page(query: str = ""):
    """Which page GET /?*query* serves: "picker" or "chat"."""
    req = Request({"type": "http", "method": "GET", "path": "/",
                   "query_string": query.encode(), "headers": []})
    body = asyncio.run(server.index(req)).body.decode("utf-8")
    return "picker" if "Resume Session" in body else "chat"


@pytest.mark.parametrize("query", ["", "rid=default", "rid=s1", "rid=s2", "lobby=1",
                                   "open=abc", "new=1"])
def test_every_tab_gets_the_chat_page(picking, query):
    # Its socket decides: a joined session's tab must reach that session.
    assert _page(query) == "chat", f"/?{query} was given the picker"


def test_the_picker_itself_is_at_bare_resume(picking):
    assert _page("resume") == "picker"


def _initial(monkeypatch, rt):
    """What a tab attached to *rt* is sent first."""
    sent = []

    async def send_to(_ws, msg):
        sent.append(msg)

    monkeypatch.setattr(server, "send_to", send_to)
    monkeypatch.setattr(server, "_runtime_for_ws", lambda _ws: rt)
    asyncio.run(server._send_initial_state(object()))
    return sent


def test_a_tab_reaching_the_session_to_pick_is_sent_to_the_picker(picking, monkeypatch):
    # The launch's tab, the lobby's card, a phone attaching in place: all of
    # them reach it by its socket.
    assert _initial(monkeypatch, picking) == [{"type": "navigate", "url": "/?resume"}]


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


def test_the_browser_picker_resumes_in_a_running_hub_too(picking, monkeypatch):
    # /resume in a running hub's own session opens it, outside picker mode.
    seen = {}

    async def reconfigure(**kw):
        seen.update(kw)
        return True, None

    monkeypatch.setattr(server, "_picker_mode", False)
    monkeypatch.setattr(server, "_reconfigure", reconfigure)
    got = asyncio.run(server.api_resume({"session_id": "abc"}))
    assert got == {"ok": True, "session_id": "abc", "url": "/?rid=default"}
    assert seen == {"resume": "abc"}


def test_the_browser_picker_goes_where_the_hub_says():
    page = (STATIC / "resume.html").read_text(encoding="utf-8")
    assert "window.location.href = result.url;" in page
    assert "window.location.href = '/" not in page


def test_the_browser_picker_is_told_whose_sessions_it_lists(monkeypatch):
    monkeypatch.setattr(server, "list_projects", lambda: [])
    monkeypatch.setattr(server, "account_label", lambda: ".claude-account-c")
    got = asyncio.run(server.api_sessions())
    assert got == {"projects": [], "account": ".claude-account-c"}
    page = (STATIC / "resume.html").read_text(encoding="utf-8")
    assert "sessions on <code>' + esc(data.account)" in page


# --------------------------------------------------------------------------
# The routes
# --------------------------------------------------------------------------

def test_restart_is_handled_by_the_restart(monkeypatch):
    routes = {(m, p): fn for m, p, _k, fn in server._HTTP_ROUTES}
    assert routes[("post", "/api/restart")] is server.api_restart


def test_a_route_refuses_a_helper_for_its_handler():
    # A helper written between a decorator and its handler is taken for it.
    def helper(argv: list) -> list:
        return argv

    with pytest.raises(TypeError, match="helper, which is not an async def"):
        server._route("post", "/api/x")(helper)
    with pytest.raises(TypeError, match="WebSocket /x would be handled by helper"):
        server._ws_route("/x")(helper)


def test_a_restart_does_not_carry_the_marker(monkeypatch):
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
