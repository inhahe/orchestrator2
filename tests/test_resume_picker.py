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


def _launch(monkeypatch, tmp_path, picked, argv=("--resume", "--detach"),
            *, dialogs=None):
    """Run main() for a launch with *argv*, whose terminal picker returns
    *picked*; return what stopped it.  With *dialogs* (a list), the launch's
    console is not visible -- the picker had one of its own -- and the dialogs
    it shows are appended to *dialogs*."""
    cfg = parse_args(list(argv) + ["--cwd", str(tmp_path)])
    monkeypatch.setattr(server, "config", None)        # main() rebinds it
    monkeypatch.setattr(server, "_parse_args_or_report", lambda: cfg)
    monkeypatch.setattr(server, "LAUNCH_ERROR_LOG", str(tmp_path / "launch-error.log"))
    if dialogs is None:
        monkeypatch.setenv("ORCH2_NO_DIALOG", "1")
        monkeypatch.setattr(server, "_console_is_visible", lambda: True)
    else:
        monkeypatch.delenv("ORCH2_NO_DIALOG", raising=False)
        monkeypatch.setattr(server, "_console_is_visible", lambda: False)
        monkeypatch.setattr(server, "_show_error_dialog",
                            lambda title, body: dialogs.append(body))
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


def test_when_the_picker_had_its_own_console_it_is_said_in_a_dialog(monkeypatch, tmp_path):
    # A tray launch, or one with no console at all: the picker's own console
    # closed with it.
    shown = []
    stopped = _launch(monkeypatch, tmp_path, {"session_id": None, "cwd": None,
                                              "message": "No sessions found in X"},
                      dialogs=shown)
    assert isinstance(stopped, SystemExit) and stopped.code == 0
    assert shown == ["No sessions found in X"]


def test_in_the_launchs_own_console_no_dialog_is_needed(monkeypatch, tmp_path):
    shown = []
    monkeypatch.setattr(server, "_show_error_dialog", lambda t, b: shown.append(b))
    _launch(monkeypatch, tmp_path, {"session_id": None, "cwd": None,
                                    "message": "No sessions found in X"})
    assert shown == []


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


def test_without_a_terminal_picker_the_browsers_is_next(monkeypatch, tmp_path, capsys):
    stopped = _launch(monkeypatch, tmp_path, None)
    assert isinstance(stopped, _WentOn), "the launch stopped"
    assert "could not run, so it opens in the browser" in capsys.readouterr().out
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


def test_a_picker_window_closed_with_x_is_left_like_esc(monkeypatch):
    # STATUS_CONTROL_C_EXIT, as Popen.wait gives it.
    assert _picker_process(monkeypatch, code=0xC000013A) == {
        "session_id": None, "cwd": None, "message": None}


def test_a_picker_that_gave_no_answer_is_logged(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.WARNING, logger="orchestrator2")
    _picker_process(monkeypatch, code=2)
    assert "the resume picker gave no answer (exit code 2)" in caplog.text


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

    async def _private_helper(port: int) -> None:
        pass

    with pytest.raises(TypeError, match="helper, which is not a public async def"):
        server._route("post", "/api/x")(helper)
    with pytest.raises(TypeError, match="_private_helper, which is not a public async def"):
        server._route("post", "/api/x")(_private_helper)
    with pytest.raises(TypeError, match="WebSocket /x would be handled by helper"):
        server._ws_route("/x")(helper)
    assert len([r for r in server._HTTP_ROUTES if r[1] == "/api/x"]) == 0


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


# --------------------------------------------------------------------------
# Tabs reaching a hub waiting in its picker, by their sockets
# --------------------------------------------------------------------------

def test_a_joined_sessions_tab_is_not_sent_to_the_picker(picking, monkeypatch):
    # Only the session still to be picked goes there.
    class _Stop(Exception):
        pass

    sent = []

    async def send_to(_ws, msg):
        sent.append(msg)
        raise _Stop()                   # the first message is the answer

    joined = types.SimpleNamespace(rid="s2", state=object(), config=object(),
                                   meta=lambda: {"rid": "s2"}, epoch="e", seq=0)
    monkeypatch.setattr(server, "send_to", send_to)
    monkeypatch.setattr(server, "_runtime_for_ws", lambda _ws: joined)
    with pytest.raises(_Stop):
        asyncio.run(server._send_initial_state(object()))
    assert sent[0]["type"] == "attached", sent


def _land(monkeypatch, **query):
    """Where the real endpoint puts a socket with *query*: "lobby", or the
    rid of the runtime it attached to."""
    server.build_app()                  # populates WebSocketDisconnect
    where = []

    async def attached(_ws, rt):
        where.append(rt.rid)

    async def lobby(_ws, notice=None):
        where.append("lobby")

    monkeypatch.setattr(server, "_attach_ws", attached)
    monkeypatch.setattr(server, "_enter_lobby", lobby)

    class FakeWS:
        headers: dict = {}
        query_params = query

        async def accept(self):
            pass

        async def send_text(self, _t):
            pass

        async def send_json(self, _o):
            pass

        async def close(self, *a, **k):
            pass

        async def receive_text(self):
            raise server.WebSocketDisconnect(1000)

    asyncio.run(server.websocket_endpoint(FakeWS()))
    return where


def test_the_address_a_picking_hub_printed_reaches_its_picker(picking, monkeypatch):
    # Bare /: attached to the session still to be picked, which sends it on.
    assert _land(monkeypatch) == ["s1"]
    assert _land(monkeypatch, lobby="1") == ["lobby"]


def test_out_of_picker_mode_a_bare_tab_lands_in_the_lobby(picking, monkeypatch):
    monkeypatch.setattr(server, "_picker_mode", False)
    assert _land(monkeypatch) == ["lobby"]


# --------------------------------------------------------------------------
# Switching the hub's own session: one set of checks for every way in
# --------------------------------------------------------------------------

@pytest.fixture
def hub(monkeypatch):
    """A running hub (not picking): its own session on "own", and "other"
    open in s2."""
    async def broadcast(_msg):
        pass

    own = types.SimpleNamespace(rid="s1", clients=set(), config=parse_args([]),
                                broadcast=broadcast,
                                state=types.SimpleNamespace(session_id="own"))
    other = types.SimpleNamespace(rid="s2", state=types.SimpleNamespace(session_id="other"))
    monkeypatch.setattr(server, "_picker_mode", False)
    monkeypatch.setattr(server, "_default_runtime", own)
    monkeypatch.setattr(server, "runtimes", {"s1": own, "s2": other})
    monkeypatch.setattr(server, "_trashing_sids", set())
    monkeypatch.setattr(server.proc_guard, "find_foreign_session_holder", lambda sid: None)
    return own


def test_a_session_open_elsewhere_in_the_hub_cannot_be_switched_to(hub):
    why = asyncio.run(server._why_not_resumable("other"))
    assert "already open in this hub (s2)" in why


def test_a_session_another_process_holds_cannot_be_switched_to(hub, monkeypatch):
    monkeypatch.setattr(server.proc_guard, "find_foreign_session_holder",
                        lambda sid: types.SimpleNamespace(holder_pid=4242))
    assert "pid 4242" in asyncio.run(server._why_not_resumable("x"))


def test_a_session_being_deleted_cannot_be_switched_to(hub, monkeypatch):
    monkeypatch.setattr(server, "_trashing_sids", {"x"})
    assert "Recycle Bin" in asyncio.run(server._why_not_resumable("x"))


def test_any_other_session_can(hub):
    assert asyncio.run(server._why_not_resumable("x")) is None


def test_the_picker_goes_to_a_session_open_elsewhere_in_the_hub(hub, monkeypatch):
    async def reconfigure(**_kw):
        raise AssertionError("the hub's own session was switched")

    monkeypatch.setattr(server, "_reconfigure", reconfigure)
    got = asyncio.run(server.api_resume({"session_id": "other"}))
    assert got == {"ok": True, "session_id": "other", "url": "/?rid=s2"}


def test_a_picking_hub_refuses_a_session_open_elsewhere_in_it(hub, monkeypatch):
    monkeypatch.setattr(server, "_picker_mode", True)
    monkeypatch.setattr(server, "bridge", _Bridge())    # built, as it would be
    got = asyncio.run(server.api_resume({"session_id": "other"}))
    assert got["ok"] is False and "already open in this hub" in got["error"]


def test_two_picks_at_once_start_the_session_once(hub, monkeypatch, tmp_path):
    # Both tabs pass the picker-mode check before either has started the
    # bridge; the second must find the hub picked, not start it again.
    from state import State
    started = []

    class Counting(_Bridge):
        async def start(self):
            started.append(self._initial_resume_id)

    reconfigured = []

    async def reconfigure(**kw):
        reconfigured.append(kw)
        return True, None

    monkeypatch.setattr(server, "_picker_mode", True)
    monkeypatch.setattr(server, "bridge", Counting())
    monkeypatch.setattr(server, "config", parse_args(["--cwd", str(tmp_path)]))
    monkeypatch.setattr(server, "state", State())
    monkeypatch.setattr(server, "find_session_dir", lambda sid, cfg=None: tmp_path)
    monkeypatch.setattr(server, "read_session_title", lambda sid, cfg=None: None)
    monkeypatch.setattr(server, "_session_dir", lambda sid, cfg=None: None)
    monkeypatch.setattr(server, "_attach_queue_persistence", lambda *a, **k: 0)
    monkeypatch.setattr(server, "_reconfigure", reconfigure)

    # Each pick waits in its check until the other is there too -- which only
    # an unserialised second pick can be -- or a moment has passed.  In the hub
    # the picks are two requests, and the bridge's own awaits let one overtake
    # the other.
    entered = []

    async def both():
        both_in = asyncio.Event()

        async def why(_sid):
            entered.append(_sid)
            if len(entered) == 2:
                both_in.set()
            try:
                await asyncio.wait_for(both_in.wait(), 0.2)
            except asyncio.TimeoutError:
                pass
            return None

        monkeypatch.setattr(server, "_why_not_resumable", why)
        return await asyncio.gather(server.api_resume({"session_id": "a"}),
                                    server.api_resume({"session_id": "b"}))

    first, second = asyncio.run(both())
    assert first["ok"] and second["ok"]
    assert started == ["a"], f"the bridge was started for {started}"
    assert reconfigured == [{"resume": "b"}], "the second pick was not the hub's own switch"


class _Bridge:
    """Stands in for SDKBridge inside _reconfigure."""

    def __init__(self, *a, **k):
        self._initial_resume_id = None
        self.stopped = False

    async def start(self):
        pass

    async def stop(self, **_kw):
        self.stopped = True


def _reconfigure_on(monkeypatch, tmp_path, hub, target, **where):
    """_reconfigure(**where) in *hub*, where it would show session *target*;
    return (result, the old bridge, tabs refreshed)."""
    import sdk_bridge
    from state import State
    old = _Bridge()
    refreshed = []

    async def refresh(ws):
        refreshed.append(ws)

    monkeypatch.setattr(sdk_bridge, "SDKBridge", _Bridge)
    monkeypatch.setattr(server, "_attach_queue_persistence", lambda *a, **k: 0)
    monkeypatch.setattr(server, "_session_to_open", lambda *a, **k: target)
    monkeypatch.setattr(server, "_send_initial_state", refresh)
    monkeypatch.setattr(server, "config", parse_args([]))
    st = State()
    st.session_id = "own"
    monkeypatch.setattr(server, "state", st)
    monkeypatch.setattr(server, "bridge", old)
    monkeypatch.chdir(tmp_path)
    got = asyncio.run(server._reconfigure(**where))
    return got, old, refreshed


def test_switching_to_the_session_already_shown_leaves_it_running(hub, monkeypatch, tmp_path):
    got, old, refreshed = _reconfigure_on(monkeypatch, tmp_path, hub, "own",
                                          cwd=str(tmp_path))
    assert got == (True, None)
    assert not old.stopped, "the session shown was stopped, cutting off its turn"


def test_switching_to_a_session_open_elsewhere_is_refused_before_anything_stops(
        hub, monkeypatch, tmp_path):
    got, old, _ = _reconfigure_on(monkeypatch, tmp_path, hub, "other", cwd=str(tmp_path))
    assert got[0] is False and "already open in this hub" in got[1]
    assert not old.stopped


def test_every_tab_on_the_hubs_session_is_shown_the_switch(hub, monkeypatch, tmp_path):
    # Not only the one that asked: a phone left showing the old session would
    # send its prompts to the new one.
    hub.clients.update({"desktop", "phone"})
    got, old, refreshed = _reconfigure_on(monkeypatch, tmp_path, hub, "new",
                                          cwd=str(tmp_path))
    assert got == (True, None) and old.stopped
    assert sorted(refreshed) == ["desktop", "phone"]
