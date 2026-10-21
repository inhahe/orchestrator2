"""A launch's ``--compact-at`` and ``--auto-compact``, for its session alone.

Each launch's compaction settings belong to the session it opens or lands
on, as its name does: handed to a running hub with the launch, set on that
session without touching the hub's own settings, never inherited by other
sessions from whichever launch started the hub, and carried by ``/move``.
What a launch was not given is not sent, so that relaunching a session does
not undo a ``/autocompact`` set on it since.

A session's own settings live on its State, and are kept by its id: merged
with what was kept the first time each id appears, and written again when a
launch or ``/autocompact`` changes them, so a session reopened without its
launch keeps them.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server                                        # noqa: E402
from config import default_compact_at, parse_args    # noqa: E402

MODEL = "claude-opus-5-5"


# -- What the launch hands over ----------------------------------------------

def test_what_the_launch_was_given_is_handed_over():
    cfg = parse_args(["--model", MODEL, "--auto-compact", "--compact-at", "300000"])
    kw = server._hub_launch_kwargs(cfg, None)

    assert kw["compact_at"] == 300000
    assert kw["auto_compact"] is True


def test_no_compact_is_handed_over_as_off():
    cfg = parse_args(["--no-compact"])

    assert server._hub_launch_kwargs(cfg, None)["auto_compact"] is False


def test_what_it_was_not_given_is_not():
    cfg = parse_args(["--model", MODEL])
    kw = server._hub_launch_kwargs(cfg, None)

    assert kw["compact_at"] is None
    assert kw["auto_compact"] is None


def test_the_models_default_given_explicitly_is_handed_over():
    """Given, it is meant: it overrides a /autocompact set on the session."""
    default = default_compact_at(MODEL)
    cfg = parse_args(["--model", MODEL, "--compact-at", str(default)])

    assert server._hub_launch_kwargs(cfg, None)["compact_at"] == default


@pytest.mark.parametrize("bad", ["0", "-5", "3.5", "lots"])
def test_a_threshold_that_is_not_a_positive_whole_number_is_refused(bad, capsys):
    with pytest.raises(SystemExit):
        parse_args(["--compact-at", bad])


def test_both_go_on_the_wire(monkeypatch):
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true, "rid": "R1"}'

    def fake_urlopen(req, timeout=None):
        seen.update(json.loads(req.data.decode("utf-8")))
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    server._launch_into_hub(8787, cwd="C:/w", resume=None, no_continue=False,
                            compact_at=300000, auto_compact=True)

    assert seen["compact_at"] == 300000
    assert seen["auto_compact"] is True


# -- A session the hub opens ---------------------------------------------------

def _opened_with(monkeypatch, tmp_path, body):
    got = {}

    class _RT:
        rid = "R2"

    async def fake_create_runtime(**kw):
        got.update(kw)
        return _RT()

    monkeypatch.setattr(server, "_create_runtime", fake_create_runtime)
    monkeypatch.setattr(server, "config", parse_args([]))
    res = asyncio.run(server.api_session_launch(
        {"cwd": str(tmp_path), "no_continue": True, **body}))
    assert res == {"ok": True, "rid": "R2"}
    return got


def test_a_new_session_is_opened_with_them(monkeypatch, tmp_path):
    got = _opened_with(monkeypatch, tmp_path,
                       {"compact_at": 300000, "auto_compact": True})

    assert got["compact_at"] == 300000
    assert got["auto_compact"] is True


@pytest.mark.parametrize("bad", [0, -5, "300000", True, 3.5])
def test_a_threshold_that_makes_no_sense_is_ignored(monkeypatch, tmp_path, bad):
    assert _opened_with(monkeypatch, tmp_path, {"compact_at": bad})["compact_at"] is None


@pytest.mark.parametrize("bad", [1, "yes", None])
def test_a_switch_that_is_not_true_or_false_is_ignored(monkeypatch, tmp_path, bad):
    assert _opened_with(monkeypatch, tmp_path, {"auto_compact": bad})["auto_compact"] is None


def _new_runtime(monkeypatch, tmp_path, hub_argv, **kw):
    """The Config and State _create_runtime builds, caught before its bridge."""
    monkeypatch.setattr(server, "config", parse_args(hub_argv))
    monkeypatch.setattr(server, "runtimes", {})
    seen = {}

    def stop(cfg, st, _broadcast):
        seen.update(cfg=cfg, st=st)
        raise RuntimeError("stop here")

    monkeypatch.setattr(server, "_new_bridge", stop)
    with pytest.raises(RuntimeError):
        asyncio.run(server._create_runtime(cwd=str(tmp_path), no_continue=True, **kw))
    return seen["cfg"], seen["st"]


def test_a_session_does_not_inherit_the_hubs_launch(monkeypatch, tmp_path):
    """The hub was started by a launch with a trial's settings: the sessions
    it opens for other launches, or from the lobby, are not in the trial."""
    cfg, st = _new_runtime(
        monkeypatch, tmp_path,
        ["--model", MODEL, "--auto-compact", "--compact-at", "300000"])

    assert cfg.compact_at == default_compact_at(MODEL)
    assert cfg.auto_compact is False
    assert server.own_compaction(st) == (None, None)


def test_a_session_gets_the_default_for_its_own_model(monkeypatch, tmp_path):
    cfg, _st = _new_runtime(monkeypatch, tmp_path, ["--model", MODEL],
                            model="claude-sonnet-4-5")

    assert cfg.compact_at == default_compact_at("claude-sonnet-4-5")


def test_a_session_gets_what_its_launch_gave_as_its_own(monkeypatch, tmp_path):
    _cfg, st = _new_runtime(monkeypatch, tmp_path, ["--model", MODEL],
                            compact_at=300000, auto_compact=True)

    assert server.own_compaction(st) == (300000, True)


# -- A session already open ----------------------------------------------------

def _land_on_open_session(monkeypatch, tmp_path, body, synced=False):
    """A launch onto the hub's own session S1; *synced*: its record has been
    merged into it already (``_sync_session_compaction``)."""
    hub = parse_args(["--model", MODEL])
    hub_values = (hub.compact_at, hub.auto_compact)

    class _State:
        session_id = "S1"
        model = MODEL
        effort = None
        bell_events = set()
        queued_prompts = []

    if synced:
        _State._compaction_sid = "S1"

    class _RT:
        rid = "R4"
        state = _State()
        # The hub's own session runs on the hub's Config itself.
        config = hub
        bridge = None

    rt = _RT()
    monkeypatch.setattr(server, "config", hub)
    monkeypatch.setattr(server, "runtimes", {"R4": rt})
    monkeypatch.setattr(server, "_session_to_open", lambda *a, **k: "S1")
    res = asyncio.run(server.api_session_launch({"cwd": str(tmp_path), **body}))
    assert res.get("rid") == "R4"
    assert (hub.compact_at, hub.auto_compact) == hub_values, \
        "the hub's own settings changed"
    return rt


def test_a_session_already_open_takes_them(monkeypatch, tmp_path):
    """The usual case with go.bat: the hub is up and the session is live."""
    rt = _land_on_open_session(monkeypatch, tmp_path,
                               {"compact_at": 300000, "auto_compact": True})

    assert rt.state._compact_at == 300000
    assert rt.state._auto_compact is True


def test_a_launch_without_them_leaves_the_session_alone(monkeypatch, tmp_path):
    rt = _land_on_open_session(monkeypatch, tmp_path, {})

    assert not hasattr(rt.state, "_compact_at")
    assert not hasattr(rt.state, "_auto_compact")



def test_autocompact_reports_the_sessions_own_setting():
    import commands
    hub = parse_args(["--model", MODEL])
    st = types.SimpleNamespace(_compact_at=300000, _auto_compact=True)

    res = commands._cmd_autocompact("", st, hub)

    assert res.messages[0]["data"]["message"] == "auto-compact ON: the CLI compacts at ~300000 tok"


# -- /move ---------------------------------------------------------------------

def _runtime(state_values, **config_values):
    st = types.SimpleNamespace(**state_values)
    cfg = types.SimpleNamespace(api_key=None, **config_values)
    return types.SimpleNamespace(state=st, config=cfg)


def test_a_move_carries_what_was_set_on_the_session(monkeypatch):
    monkeypatch.setattr(server.wakeup_store, "load_wakeup", lambda *a: None)
    rt = _runtime({"_compact_at": 300000, "_auto_compact": True},
                  compact_at=950000, auto_compact=False)

    carry = server._move_carry(rt, "C:/w", "S1")

    assert carry["compact_at"] == 300000
    assert carry["auto_compact"] is True


def test_a_move_carries_no_default_as_if_it_were_set(monkeypatch):
    """A default carried as given would be kept as the copy's own, and stay
    when the copy later moves to a model whose default differs."""
    monkeypatch.setattr(server.wakeup_store, "load_wakeup", lambda *a: None)
    rt = _runtime({}, compact_at=160000, auto_compact=False)

    carry = server._move_carry(rt, "C:/w", "S1")

    assert (carry["compact_at"], carry["auto_compact"]) == (None, None)


# -- Remembered by session id ------------------------------------------------
#
# A session reopened without the launch that set its compaction -- from the
# session list, revived by a hub restart, after a reboot -- keeps it.

from session import load_session_compaction, save_session_compaction  # noqa: E402


def test_the_record_holds_exactly_what_was_set():
    save_session_compaction("S9", 300000, True)
    assert load_session_compaction("S9") == (300000, True)

    save_session_compaction("S9", None, False)
    assert load_session_compaction("S9") == (None, False)
    assert load_session_compaction("never-seen") == (None, None)


def _got_id(sid, cfg, st):
    """The bridge reporting *sid* for *st*: the hub's hook, run where no event
    loop is, so its disk work is done by the time it returns."""
    st.session_id = sid
    server._on_session_id(sid, cfg, st)


def test_a_session_reopened_without_its_launch_keeps_its_compaction(monkeypatch, tmp_path):
    save_session_compaction("S1", 300000, True)
    cfg, st = _new_runtime(monkeypatch, tmp_path, ["--model", MODEL], resume="S1")

    _got_id("S1", cfg, st)

    assert server.own_compaction(st) == (300000, True)


SID = "0d19661f-62b7-47b2-a94f-d7537aec0fa5"


def test_a_session_about_to_be_resumed_has_it_before_its_cli_starts(monkeypatch, tmp_path):
    """The CLI takes its compaction from its environment, so the State must
    hold it before the CLI starts -- while its session_id is still the
    session it is leaving, or none."""
    save_session_compaction(SID, 300000, True)
    cfg, st = _new_runtime(monkeypatch, tmp_path, ["--model", MODEL], resume=SID)
    st.session_id = None

    asyncio.run(server._await_session_compaction(SID, cfg, st))

    assert server.own_compaction(st) == (300000, True)


def test_a_merge_already_under_way_is_waited_for(monkeypatch, tmp_path):
    """The CLI named the session, starting the merge, and a reconnect came
    before it finished: the new CLI must not start without it."""
    save_session_compaction(SID, 300000, True)
    cfg, st = _new_runtime(monkeypatch, tmp_path, ["--model", MODEL], resume=SID)

    async def scenario():
        _got_id(SID, cfg, st)
        await server._await_session_compaction(SID, cfg, st)
        return server.own_compaction(st)

    assert asyncio.run(scenario()) == (300000, True)


def test_a_title_nothing_resolved_gets_no_record(monkeypatch, tmp_path):
    """--resume "OS A" with no session of that title reaches the connect as
    it is, and the CLI refuses it; a record kept under it would be junk."""
    cfg, st = _new_runtime(monkeypatch, tmp_path, ["--model", MODEL],
                           compact_at=300000, auto_compact=True)

    asyncio.run(server._await_session_compaction("OS A", cfg, st))

    assert not session_record_exists("OS A")
    assert getattr(st, "_compaction_sid", None) is None


def test_what_a_launch_gives_beats_what_was_kept_and_is_kept(monkeypatch, tmp_path):
    save_session_compaction("S1", 300000, True)
    cfg, st = _new_runtime(monkeypatch, tmp_path, ["--model", MODEL],
                           resume="S1", auto_compact=False)

    _got_id("S1", cfg, st)

    assert server.own_compaction(st) == (300000, False)
    assert load_session_compaction("S1") == (300000, False)


def test_a_new_session_is_kept_once_it_has_an_id(monkeypatch, tmp_path):
    cfg, st = _new_runtime(monkeypatch, tmp_path, ["--model", MODEL],
                           compact_at=300000, auto_compact=True)

    _got_id("S7", cfg, st)

    assert load_session_compaction("S7") == (300000, True)


def test_a_session_that_never_set_it_leaves_no_record(monkeypatch, tmp_path):
    cfg, st = _new_runtime(monkeypatch, tmp_path, ["--model", MODEL])

    _got_id("S8", cfg, st)

    assert not session_record_exists("S8")


def session_record_exists(sid):
    import session
    return session._session_record_file("compaction", sid, None).exists()


def test_a_later_turn_does_not_undo_autocompact(monkeypatch, tmp_path):
    """The bridge reports the session's id on every turn: the record is merged
    once per id, not rewritten from the launch's values each time."""
    import commands
    cfg, st = _new_runtime(monkeypatch, tmp_path, ["--model", MODEL],
                           compact_at=300000, auto_compact=True)
    _got_id("S1", cfg, st)

    commands._cmd_autocompact("off", st, cfg)
    _got_id("S1", cfg, st)
    _got_id("S1", cfg, st)

    assert load_session_compaction("S1") == (300000, False)
    assert server.own_compaction(st) == (300000, False)


def test_a_new_transcript_keeps_the_sessions_own_under_its_new_id(monkeypatch, tmp_path):
    """A trim or /clear gives the running session a new id: what it has set,
    /autocompact included, is kept under that one too."""
    import commands
    cfg, st = _new_runtime(monkeypatch, tmp_path, ["--model", MODEL])
    _got_id("S1", cfg, st)
    commands._cmd_autocompact("200000", st, cfg)

    _got_id("S2", cfg, st)

    assert load_session_compaction("S2") == (200000, True)


def test_autocompact_before_the_first_id_is_kept_when_it_comes(monkeypatch, tmp_path):
    import commands
    cfg, st = _new_runtime(monkeypatch, tmp_path, ["--model", MODEL],
                           compact_at=300000, auto_compact=True)

    commands._cmd_autocompact("off", st, cfg)
    _got_id("S3", cfg, st)

    assert load_session_compaction("S3") == (300000, False)


def test_a_launch_onto_an_open_session_is_kept(monkeypatch, tmp_path):
    rt = _land_on_open_session(monkeypatch, tmp_path,
                               {"compact_at": 300000, "auto_compact": True}, synced=True)

    assert load_session_compaction("S1") == (300000, True)
    assert server.own_compaction(rt.state) == (300000, True)


def test_autocompact_is_kept_and_reports_the_sessions_threshold():
    import commands
    hub = parse_args(["--model", MODEL])
    st = types.SimpleNamespace(session_id="S5", _compaction_sid="S5")

    commands._cmd_autocompact("300000", st, hub)
    assert load_session_compaction("S5") == (300000, True)

    commands._cmd_autocompact("off", st, hub)
    assert load_session_compaction("S5") == (300000, False)

    res = commands._cmd_autocompact("on", st, hub)
    assert res.messages[0]["data"]["message"] == "auto-compact ON: the CLI compacts at ~300000 tok"


# -- The hub's own session -----------------------------------------------------

def test_the_hubs_launch_is_its_own_sessions(monkeypatch, tmp_path):
    from state import State
    cfg = parse_args(["--model", MODEL, "--auto-compact", "--compact-at", "300000"])
    st = State()

    server._launch_compaction_to_state(cfg, st)

    assert server.own_compaction(st) == (300000, True)


def test_a_restart_lets_the_kept_record_speak_for_the_resumed_session(monkeypatch):
    """Passed again, the launch's flags would undo a /autocompact since."""
    import subprocess
    from state import State
    seen = {}

    def fake_popen(args, **kw):
        seen["argv"] = list(args)
        raise OSError("not really")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sys, "argv", ["server.py", "--auto-compact", "--compact-at",
                                      "300000", "--no-compact", "--compact-at=5", "--open"])
    monkeypatch.setattr(server, "config", parse_args([]))
    st = State()
    st.session_id = "sid-1"
    monkeypatch.setattr(server, "state", st)

    asyncio.run(server.api_restart())

    assert not [a for a in seen["argv"] if "compact" in a], seen["argv"]


class _QuietBridge:
    def __init__(self, config=None, state=None, broadcaster=None):
        self.config, self.state = config, state
        self.on_session_id = None

    async def start(self):
        pass

    async def stop(self, **_kw):
        pass


def _switch(monkeypatch, tmp_path, *, picking=False, **where):
    """The hub's own session, launched with a trial's settings, switched with
    /resume or /cwd: the (Config, State) it runs on afterwards."""
    import dataclasses
    import sdk_bridge
    from session_runtime import SessionRuntime
    from state import State
    monkeypatch.setattr(sdk_bridge, "SDKBridge", _QuietBridge)
    monkeypatch.setattr(server, "_attach_queue_persistence",
                        lambda st, cwd, config_dir=None: 0)
    monkeypatch.setattr(server, "resolve_session_ref", lambda ref, _c, _d: ref)
    monkeypatch.setattr(server, "find_session_dir", lambda _s, _d=None: tmp_path)
    monkeypatch.setattr(server, "read_session_title", lambda _s, _d=None: None)
    monkeypatch.setattr(server, "find_session_cwd", lambda _s, _d=None: None)
    cfg = dataclasses.replace(
        parse_args(["--model", MODEL, "--auto-compact", "--compact-at", "300000"]),
        cwd=str(tmp_path))
    st = State()
    st.session_id = "sid-a"
    rt = SessionRuntime(config=cfg, state=st, rid="R0")
    monkeypatch.setattr(server, "config", cfg)
    monkeypatch.setattr(server, "state", st)
    monkeypatch.setattr(server, "bridge", None)
    monkeypatch.setattr(server, "_default_runtime", rt)
    monkeypatch.setattr(server, "_picker_mode", picking)
    monkeypatch.chdir(tmp_path)
    ok, err = asyncio.run(server._reconfigure(**where))
    assert ok, err
    return server.config, server.state


def test_another_session_in_the_hubs_tab_is_not_in_its_launchs_trial(monkeypatch, tmp_path):
    cfg, st = _switch(monkeypatch, tmp_path, resume="sid-b")

    assert (cfg.auto_compact, cfg.compact_at) == (False, default_compact_at(MODEL))
    assert server.own_compaction(st) == (None, None)


def test_the_session_a_picker_launch_picks_gets_its_settings(monkeypatch, tmp_path):
    _cfg, st = _switch(monkeypatch, tmp_path, picking=True, resume="sid-b")

    assert server.own_compaction(st) == (300000, True)


def test_on_the_event_loop_the_disk_is_read_off_it_and_the_state_follows(monkeypatch, tmp_path):
    """In the hub the hook runs on the loop that serves every tab: the record
    is read and written in a worker thread, and the State takes the kept
    values when that returns -- unless something set them meanwhile."""
    save_session_compaction("S1", 300000, True)
    cfg, st = _new_runtime(monkeypatch, tmp_path, ["--model", MODEL], resume="S1")
    import threading
    main = threading.get_ident()
    readers = []
    real_load = server.load_session_compaction

    def load(*a, **k):
        readers.append(threading.get_ident())
        return real_load(*a, **k)

    monkeypatch.setattr(server, "load_session_compaction", load)

    async def scenario():
        _got_id("S1", cfg, st)
        for _ in range(200):
            if server.own_compaction(st) != (None, None):
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())

    assert readers and main not in readers
    assert server.own_compaction(st) == (300000, True)
