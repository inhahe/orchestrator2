"""An API key for the one session whose launch had it.

Asked 2026-10-10: "can you make ANTHROPIC_API_KEY only work for the session
that was loaded with that key set? i think there's another key that was
already made to behave that way?"  The other one is the agent name
(``--agent-name`` / ``ORCH2_AGENT_NAME``; tests/test_agent_name_per_session.py),
and the key now goes the same way:

* The launch reads ``ANTHROPIC_API_KEY`` from its environment.  The session it
  opens is given the key; no other session is.  The hub drops the variable
  from its own environment, which every session's CLI inherits.
* A launch that joins a running hub hands the key over with its request.
* The hub remembers it against the session for as long as the hub runs, and
  hands it to its replacement on a restart, so the session keeps it when it
  is reopened from the lobby.  In memory only: never on disk, on a command
  line, in a log, or sent to a browser.
* ``/apikey`` says whether a session has one; ``/apikey off`` drops it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import btw                                       # noqa: E402
import server                                    # noqa: E402
from config import parse_args                    # noqa: E402
from sdk_bridge import SDKBridge                 # noqa: E402
import state as state_mod                        # noqa: E402
from state import (State, apply_api_key_source,  # noqa: E402
                   billed_to_api_key, init_state_from_config)

KEY = "sk-ant-api03-not-a-real-key"
OTHER = "sk-ant-api03-another-fake-key"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv(server.HANDOVER_KEYS_ENV, raising=False)
    monkeypatch.setattr(server, "_session_api_keys", {})


# --------------------------------------------------------------------------
# The launch's own key
# --------------------------------------------------------------------------

def test_the_launch_takes_its_key_out_of_the_environment(monkeypatch):
    # Left there, every session's CLI would inherit it -- the report.
    monkeypatch.setenv("ANTHROPIC_API_KEY", f"  {KEY}  ")
    assert server._take_launch_api_key() == KEY
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_a_blank_key_is_no_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    assert server._take_launch_api_key() is None
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_a_detached_child_is_given_the_key_its_launch_took(monkeypatch):
    # The same launch, finished in another process.
    import dataclasses
    cfg = dataclasses.replace(parse_args([]), api_key=KEY)
    assert server._detach_child_env(cfg)["ANTHROPIC_API_KEY"] == KEY
    assert "ANTHROPIC_API_KEY" not in server._detach_child_env(parse_args([]))


def test_a_launch_with_a_key_does_not_ask_for_a_sign_in(monkeypatch):
    import auth

    def no(*a, **k):
        raise AssertionError("asked about a subscription sign-in")

    monkeypatch.setattr(auth, "is_logged_in", no)
    monkeypatch.setattr(auth, "launch_login", no)
    assert server._ensure_logged_in(block=False, api_key=KEY) is True


def test_the_key_is_never_in_the_configs_repr():
    cfg = parse_args([])
    import dataclasses
    cfg = dataclasses.replace(cfg, api_key=KEY)
    assert KEY not in repr(cfg)


def _config_of_new_runtime(monkeypatch, tmp_path, hub_key=None, **kw):
    """The Config _create_runtime builds, caught before any bridge starts."""
    import dataclasses
    monkeypatch.setattr(server, "config",
                        dataclasses.replace(parse_args([]), api_key=hub_key))
    seen = {}

    def stop(cfg):
        seen["cfg"] = cfg
        raise RuntimeError("stop here")

    monkeypatch.setattr(server, "init_state_from_config", stop)
    kw.setdefault("no_continue", True)
    with pytest.raises(RuntimeError):
        asyncio.run(server._create_runtime(cwd=str(tmp_path), **kw))
    return seen["cfg"]


def test_a_session_the_hub_opens_does_not_inherit_the_launchs_key(monkeypatch, tmp_path):
    # The hub's own config is its launch's session's: its key is that
    # session's alone.
    cfg = _config_of_new_runtime(monkeypatch, tmp_path, hub_key=KEY)
    assert cfg.api_key is None


def test_a_session_opened_with_a_key_gets_that_key(monkeypatch, tmp_path):
    cfg = _config_of_new_runtime(monkeypatch, tmp_path, hub_key=KEY, api_key=OTHER)
    assert cfg.api_key == OTHER


def test_a_session_reopened_by_id_keeps_the_key_it_had(monkeypatch, tmp_path):
    server._session_api_keys["sid-1"] = KEY
    cfg = _config_of_new_runtime(monkeypatch, tmp_path, resume="sid-1",
                                 no_continue=False)
    assert cfg.api_key == KEY


def test_a_key_given_now_beats_the_one_remembered(monkeypatch, tmp_path):
    server._session_api_keys["sid-1"] = KEY
    cfg = _config_of_new_runtime(monkeypatch, tmp_path, resume="sid-1",
                                 no_continue=False, api_key=OTHER)
    assert cfg.api_key == OTHER


def test_another_session_does_not_get_a_remembered_key(monkeypatch, tmp_path):
    server._session_api_keys["sid-1"] = KEY
    cfg = _config_of_new_runtime(monkeypatch, tmp_path, resume="sid-2",
                                 no_continue=False)
    assert cfg.api_key is None


# --------------------------------------------------------------------------
# The session's CLI, and its /btw fork, get it; nothing else does
# --------------------------------------------------------------------------

def _options_env(cfg):
    st = init_state_from_config(cfg)

    async def bc(_m):
        pass

    br = SDKBridge(config=cfg, state=st, broadcaster=bc)
    return br._make_options().env


def test_the_sessions_cli_is_given_its_key():
    import dataclasses
    cfg = dataclasses.replace(parse_args([]), api_key=KEY)
    assert _options_env(cfg)["ANTHROPIC_API_KEY"] == KEY


def test_a_session_without_one_is_given_none():
    assert "ANTHROPIC_API_KEY" not in _options_env(parse_args([]))


def test_a_side_question_is_billed_like_its_session():
    import dataclasses
    cfg = dataclasses.replace(parse_args([]), api_key=KEY)
    st = State()
    st.session_id = "sid-1"
    opts = btw.build_btw_options(lambda **kw: types.SimpleNamespace(**kw),
                                 config=cfg, state=st)
    assert opts.env["ANTHROPIC_API_KEY"] == KEY


# --------------------------------------------------------------------------
# A launch that joins a running hub hands it over
# --------------------------------------------------------------------------

def test_the_hand_over_includes_it():
    import dataclasses
    cfg = dataclasses.replace(parse_args([]), api_key=KEY)
    assert server._hub_launch_kwargs(cfg, None)["api_key"] == KEY


def test_the_hand_over_puts_it_on_the_wire(monkeypatch):
    import urllib.request
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true, "rid": "R9"}'

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen.update(json.loads(req.data.decode("utf-8")))
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    server._launch_into_hub(8787, cwd="C:\\w", resume=None, no_continue=False,
                            api_key=KEY)
    assert seen["api_key"] == KEY
    assert seen["url"].startswith("http://127.0.0.1:"), "only over loopback"


def test_the_hub_opens_the_session_with_it(monkeypatch, tmp_path):
    got = {}

    class _RT:
        rid = "R7"

    async def fake_create_runtime(**kw):
        got.update(kw)
        return _RT()

    monkeypatch.setattr(server, "_create_runtime", fake_create_runtime)
    monkeypatch.setattr(server, "config", parse_args([]))
    res = asyncio.run(server.api_session_launch({
        "cwd": str(tmp_path), "no_continue": True, "api_key": KEY}))
    assert got["api_key"] == KEY
    assert KEY not in json.dumps(res)


class _LiveBridge:
    def __init__(self, cfg):
        self.event_queue = asyncio.Queue()
        self.config = cfg


def _open_session(monkeypatch, *, key=None, sid="sid-live"):
    import dataclasses
    st = State()
    st.session_id = sid
    cfg = dataclasses.replace(parse_args([]), api_key=key)
    rt = types.SimpleNamespace(rid="R1", state=st, config=cfg,
                               bridge=_LiveBridge(cfg))
    monkeypatch.setattr(server, "runtimes", {"R1": rt})
    monkeypatch.setattr(server, "config", parse_args([]))
    return rt


def test_a_key_for_a_session_already_open_is_applied(monkeypatch):
    rt = _open_session(monkeypatch)
    asyncio.run(server.api_session_launch(
        {"cwd": "C:\\w", "resume": "sid-live", "api_key": KEY}))
    assert rt.config.api_key == KEY
    assert rt.bridge.config is rt.config, "runtime and bridge disagree on the key"
    assert rt.bridge.event_queue.get_nowait() == ("connect", "")


def test_a_launch_without_one_leaves_an_open_sessions_key_alone(monkeypatch):
    rt = _open_session(monkeypatch, key=KEY)
    asyncio.run(server.api_session_launch({"cwd": "C:\\w", "resume": "sid-live"}))
    assert rt.config.api_key == KEY
    assert rt.bridge.event_queue.empty()


# --------------------------------------------------------------------------
# Remembered while the hub runs, and handed to a restart
# --------------------------------------------------------------------------

def test_closing_a_session_remembers_its_key(monkeypatch):
    import dataclasses
    from session_runtime import SessionRuntime
    st = State()
    st.session_id = "sid-9"
    rt = SessionRuntime(config=dataclasses.replace(parse_args([]), api_key=KEY),
                        state=st, rid="R9")

    class _Bridge:
        async def stop(self):
            pass

    rt.bridge = _Bridge()

    async def nothing(*_a, **_k):
        pass

    monkeypatch.setattr(server, "runtimes", {"R9": rt})
    monkeypatch.setattr(server, "_push_session_list", nothing)
    asyncio.run(server._teardown_runtime(rt, force=True))
    assert server._session_api_keys == {"sid-9": KEY}


def test_a_session_without_a_key_leaves_nothing_to_remember(monkeypatch):
    rt = _open_session(monkeypatch, sid="sid-9")
    server._remember_session_key(rt)
    assert server._session_api_keys == {}


def _restart(monkeypatch, rt, *, primary):
    """The replacement a restart would start: its argv and environment."""
    import subprocess
    seen = {}

    def fake_popen(args, **kw):
        seen["argv"] = list(args)
        seen["env"] = dict(kw.get("env") or {})
        raise OSError("not really")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sys, "argv", ["server.py"])
    monkeypatch.setattr(server, "state", rt.state)
    if primary:
        monkeypatch.setattr(server, "config", rt.config)
    asyncio.run(server.api_restart())
    return seen


def test_a_restart_hands_the_keys_over_in_the_childs_environment(monkeypatch):
    rt = _open_session(monkeypatch, key=KEY, sid="sid-live")
    server._session_api_keys["sid-closed"] = OTHER

    seen = _restart(monkeypatch, rt, primary=True)

    handed = json.loads(seen["env"][server.HANDOVER_KEYS_ENV])
    assert handed == {"sid-live": KEY, "sid-closed": OTHER}
    assert not any(KEY in a or OTHER in a for a in seen["argv"]), \
        "a key on a command line"


def test_the_session_a_restart_resumes_gets_its_key_by_its_id(monkeypatch):
    # Not as the launch key: the replacement looks it up for whichever
    # session it opens.
    rt = _open_session(monkeypatch, key=KEY, sid="sid-live")
    seen = _restart(monkeypatch, rt, primary=True)
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    assert json.loads(seen["env"][server.HANDOVER_KEYS_ENV]) == {"sid-live": KEY}
    assert seen["argv"][seen["argv"].index("--resume") + 1] == "sid-live"


def test_a_new_conversation_keeps_its_key_across_a_restart(monkeypatch):
    # No session id to keep it by: the replacement is that launch again, and
    # starts a new conversation where the primary is now.
    rt = _open_session(monkeypatch, key=KEY, sid=None)
    rt.config = dataclasses.replace(rt.config, cwd="D:\\now-here")
    seen = _restart(monkeypatch, rt, primary=True)
    assert seen["env"]["ANTHROPIC_API_KEY"] == KEY
    assert "--start-fresh" in seen["argv"] and "--resume" not in seen["argv"]
    assert "--no-continue" not in seen["argv"], "a lasting flag for a one-off"
    assert seen["argv"][seen["argv"].index("--cwd") + 1] == "D:\\now-here"


def test_a_picker_still_to_choose_is_a_picker_again_after_a_restart(monkeypatch):
    # With the launch's key, for the session it is still to pick -- not the
    # directory's most recent, which nobody chose.
    rt = _open_session(monkeypatch, key=KEY, sid=None)
    monkeypatch.setattr(server, "_picker_mode", True)
    seen = _restart(monkeypatch, rt, primary=True)
    assert seen["env"]["ANTHROPIC_API_KEY"] == KEY
    argv = seen["argv"]
    assert "--resume" in argv and "--start-fresh" not in argv
    after = argv[argv.index("--resume") + 1:]
    assert not after or after[0].startswith("--"), "--resume took an id"


def test_a_closed_primarys_key_goes_only_by_its_id(monkeypatch):
    # The replacement continues the most recent session, which may be
    # another: the key goes to that session only if it is the one.
    rt = _open_session(monkeypatch, key=KEY, sid="sid-p")
    server._session_api_keys["sid-p"] = KEY
    monkeypatch.setattr(server, "runtimes", {})
    rt.state = None
    seen = _restart(monkeypatch, rt, primary=True)
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    assert json.loads(seen["env"][server.HANDOVER_KEYS_ENV]) == {"sid-p": KEY}


def test_a_restart_gives_no_launch_key_to_a_primary_without_one(monkeypatch):
    rt = _open_session(monkeypatch, sid="sid-live")
    seen = _restart(monkeypatch, rt, primary=True)
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    assert server.HANDOVER_KEYS_ENV not in seen["env"]


def _seeded(monkeypatch, *args):
    """The config and state the hub's own launch opens its session with, made
    as lifespan makes them."""
    monkeypatch.setattr(server, "resolve_session_ref", lambda ref, _c, _d: ref)
    monkeypatch.setattr(server, "read_session_title", lambda _s, _d=None: None)
    cfg, resume_id, _title = server._seed_primary_session(parse_args(list(args)))
    st = init_state_from_config(cfg)
    st.session_id = resume_id
    return cfg, st, resume_id


def test_a_restarts_replacement_opens_a_session_with_the_key_it_ran_with(monkeypatch):
    server._session_api_keys["sid-p"] = KEY
    cfg, st, resume_id = _seeded(monkeypatch, "--resume", "sid-p")
    assert resume_id == "sid-p"
    assert cfg.api_key == KEY
    assert billed_to_api_key(st)


def test_a_session_the_launch_brought_a_key_for_keeps_that_one(monkeypatch):
    server._session_api_keys["sid-p"] = OTHER
    monkeypatch.setattr(server, "resolve_session_ref", lambda ref, _c, _d: ref)
    monkeypatch.setattr(server, "read_session_title", lambda _s, _d=None: None)
    launched = dataclasses.replace(parse_args(["--resume", "sid-p"]), api_key=KEY)
    cfg, _id, _title = server._seed_primary_session(launched)
    assert cfg.api_key == KEY


def test_a_new_session_at_startup_gets_no_remembered_key(monkeypatch):
    server._session_api_keys["sid-p"] = KEY
    cfg, st, resume_id = _seeded(monkeypatch, "--no-continue")
    assert resume_id is None and cfg.api_key is None
    assert not billed_to_api_key(st)


def _recent_is(monkeypatch, sid):
    monkeypatch.setattr(server, "find_most_recent_session_for_cwd",
                        lambda _c, _d=None: types.SimpleNamespace(stem=sid))


def test_a_picker_still_to_choose_opens_no_session(monkeypatch):
    # Not the directory's most recent: nobody chose it, and seeded, it would
    # be remembered as running with the launch's key.
    server._session_api_keys["sid-recent"] = OTHER
    _recent_is(monkeypatch, "sid-recent")
    cfg, st, resume_id = _seeded(monkeypatch, "--resume")
    assert resume_id is None and st.session_id is None
    assert cfg.api_key is None


def test_a_restart_replacing_a_new_conversation_opens_a_new_one(monkeypatch):
    _recent_is(monkeypatch, "sid-recent")
    cfg, st, resume_id = _seeded(monkeypatch, "--start-fresh")
    assert resume_id is None and st.session_id is None
    # Only at startup: /cwd and a worker's own connect still continue.
    assert cfg.no_continue is False


def test_a_restarts_replacement_still_to_choose_shows_no_console_picker():
    # It goes straight to the picker in the tabs it takes over.
    assert server._launch_picker_mode(parse_args(["--resume"])) == "resume"
    assert server._launch_picker_mode(parse_args(["--resume", "--wait-port"])) is None
    assert server._launch_picker_mode(parse_args(["--copy", "--wait-port"])) == "copy"


# --------------------------------------------------------------------------
# A key follows its session, not the tab showing it
# --------------------------------------------------------------------------

def _named_by_the_cli(monkeypatch, key, *messages):
    """A hub bridge running with *key*, handed *messages* by its CLI."""
    import sdk_bridge
    monkeypatch.setattr(sdk_bridge, "read_session_title", lambda _s, _d=None: None)

    async def bcast(_msg):
        pass

    br = server._new_bridge(dataclasses.replace(parse_args([]), api_key=key),
                            State(), bcast)

    async def go():
        for m in messages:
            await br._handle_system_message(m, during_turn=True)
    asyncio.run(go())
    return br


def _init(sid):
    return types.SimpleNamespace(subtype="init", data={
        "session_id": sid, "apiKeySource": "ANTHROPIC_API_KEY"})


def test_a_sessions_key_is_remembered_the_moment_its_cli_names_it(monkeypatch):
    # So an id the runtime leaves at once -- /clear, /resume -- keeps it.
    _named_by_the_cli(monkeypatch, KEY, _init("sid-new"))
    assert server._session_api_keys == {"sid-new": KEY}


def test_each_id_the_cli_gives_keeps_the_key(monkeypatch):
    # The SDK started another session than the one asked for.
    _named_by_the_cli(monkeypatch, KEY, _init("sid-asked"), _init("sid-started"))
    assert server._session_api_keys == {"sid-asked": KEY, "sid-started": KEY}


def test_a_session_without_a_key_is_named_with_none(monkeypatch):
    _named_by_the_cli(monkeypatch, None, _init("sid-new"))
    assert server._session_api_keys == {}


class _QuietBridge:
    def __init__(self, config, state, broadcaster):
        self.config = config
        self._initial_resume_id = None

    async def start(self):
        pass

    async def stop(self):
        pass


def _switch(monkeypatch, tmp_path, *, key, picking=False, sid="sid-a", **where):
    """The primary tab, on *sid* with *key*, switched with /resume or /cwd."""
    import sdk_bridge
    from session_runtime import SessionRuntime
    monkeypatch.setattr(sdk_bridge, "SDKBridge", _QuietBridge)
    monkeypatch.setattr(server, "_attach_queue_persistence",
                        lambda st, cwd, config_dir=None: 0)
    monkeypatch.setattr(server, "resolve_session_ref", lambda ref, _c, _d: ref)
    monkeypatch.setattr(server, "find_session_dir", lambda _s, _d=None: tmp_path)
    monkeypatch.setattr(server, "read_session_title", lambda _s, _d=None: None)
    monkeypatch.setattr(server, "find_session_cwd", lambda _s, _d=None: None)
    cfg = dataclasses.replace(parse_args([]), api_key=key, cwd=str(tmp_path))
    st = State()
    st.session_id = sid
    rt = SessionRuntime(config=cfg, state=st, rid="R0")
    monkeypatch.setattr(server, "config", cfg)
    monkeypatch.setattr(server, "state", st)
    monkeypatch.setattr(server, "bridge", None)
    monkeypatch.setattr(server, "_default_runtime", rt)
    monkeypatch.setattr(server, "_picker_mode", picking)
    monkeypatch.chdir(tmp_path)
    ok, err = asyncio.run(server._reconfigure(**where))
    assert ok, err
    return server.config


def test_resume_leaves_the_tabs_key_with_the_session_it_leaves(monkeypatch, tmp_path):
    cfg = _switch(monkeypatch, tmp_path, key=KEY, resume="sid-b")
    assert cfg.api_key is None, "the key followed the tab to another session"
    assert server._session_api_keys == {"sid-a": KEY}


def test_resume_gives_a_session_the_key_it_ran_with(monkeypatch, tmp_path):
    server._session_api_keys["sid-b"] = OTHER
    cfg = _switch(monkeypatch, tmp_path, key=KEY, resume="sid-b")
    assert cfg.api_key == OTHER
    assert billed_to_api_key(server.state)


def test_cwd_gives_the_session_it_continues_the_key_it_ran_with(monkeypatch, tmp_path):
    server._session_api_keys["sid-recent"] = OTHER
    monkeypatch.setattr(server, "find_most_recent_session_for_cwd",
                        lambda _c, _d=None: types.SimpleNamespace(stem="sid-recent"))
    cfg = _switch(monkeypatch, tmp_path, key=KEY, cwd=str(tmp_path))
    assert cfg.api_key == OTHER
    assert server.state.session_id == "sid-recent"


def test_a_tab_yet_to_start_a_session_takes_its_key_along(monkeypatch, tmp_path):
    """A new conversation has no id to leave the key with before its first
    turn: /cwd then must not lose the key the launch brought."""
    monkeypatch.setattr(server, "find_most_recent_session_for_cwd",
                        lambda _c, _d=None: None)
    cfg = _switch(monkeypatch, tmp_path, key=KEY, sid=None, cwd=str(tmp_path))
    assert cfg.api_key == KEY


def test_the_session_picked_in_the_picker_gets_the_launchs_key(monkeypatch, tmp_path):
    cfg = _switch(monkeypatch, tmp_path, key=KEY, picking=True, resume="sid-b")
    assert cfg.api_key == KEY


def test_the_picker_gives_a_session_the_launch_brought_no_key_for_its_own(monkeypatch, tmp_path):
    from session_runtime import SessionRuntime

    class _Idle:
        async def start(self):
            pass

    server._session_api_keys["sid-p"] = KEY
    monkeypatch.setattr(server, "find_session_dir", lambda _s, _d=None: tmp_path)
    monkeypatch.setattr(server, "read_session_title", lambda _s, _d=None: None)
    monkeypatch.setattr(server, "find_session_cwd", lambda _s, _d=None: None)
    cfg = parse_args([])
    st = State()
    rt = SessionRuntime(config=cfg, state=st, rid="R0")
    rt.bridge = _Idle()
    for name, value in (("config", cfg), ("state", st), ("bridge", rt.bridge),
                        ("_default_runtime", rt), ("_picker_mode", True)):
        monkeypatch.setattr(server, name, value)
    out = asyncio.run(server.api_resume({"session_id": "sid-p"}))
    assert out["ok"], out
    assert server.config.api_key == KEY and rt.config.api_key == KEY
    assert billed_to_api_key(st)


def test_the_new_hub_takes_the_keys_out_of_its_environment(monkeypatch):
    monkeypatch.setenv(server.HANDOVER_KEYS_ENV, json.dumps({"sid-1": KEY}))
    assert server._take_handed_over_keys() == {"sid-1": KEY}
    assert server.HANDOVER_KEYS_ENV not in os.environ


@pytest.mark.parametrize("junk", ["not json", "[1, 2]", '{"sid": 3}'])
def test_a_garbled_hand_over_is_ignored(monkeypatch, junk):
    monkeypatch.setenv(server.HANDOVER_KEYS_ENV, junk)
    assert server._take_handed_over_keys() == {}
    assert server.HANDOVER_KEYS_ENV not in os.environ


# --------------------------------------------------------------------------
# /apikey
# --------------------------------------------------------------------------

class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))


def _apikey(monkeypatch, rt, payload):
    ws = _FakeWS()
    asyncio.run(server._handle_apikey(ws, rt, payload))
    text = " ".join((m.get("data") or {}).get("message", "") for m in ws.sent)
    assert KEY not in text, "the key itself was shown"
    return text


def test_apikey_says_a_session_has_its_own_key(monkeypatch):
    rt = _open_session(monkeypatch, key=KEY)
    assert "its own API key" in _apikey(monkeypatch, rt, "")


def test_apikey_says_a_session_has_none(monkeypatch):
    rt = _open_session(monkeypatch)
    assert "no API key of its own" in _apikey(monkeypatch, rt, "")


def test_apikey_off_drops_it_and_reconnects(monkeypatch):
    rt = _open_session(monkeypatch, key=KEY, sid="sid-live")
    server._session_api_keys["sid-live"] = KEY
    text = _apikey(monkeypatch, rt, "off")
    assert rt.config.api_key is None
    assert rt.bridge.config is rt.config
    assert "sid-live" not in server._session_api_keys
    assert rt.bridge.event_queue.get_nowait() == ("connect", "")
    assert "account's own login" in text


def test_apikey_off_on_the_primary_session_changes_the_hubs_config_too(monkeypatch):
    # /cwd and /resume rebuild the primary session from the hub's config:
    # left there, the key would come back.
    rt = _open_session(monkeypatch, key=KEY)
    monkeypatch.setattr(server, "_default_runtime", rt)
    monkeypatch.setattr(server, "config", rt.config)
    _apikey(monkeypatch, rt, "off")
    assert server.config.api_key is None


def test_apikey_off_without_a_key_changes_nothing(monkeypatch):
    rt = _open_session(monkeypatch)
    _apikey(monkeypatch, rt, "off")
    assert rt.bridge.event_queue.empty()


def test_apikey_refuses_what_it_does_not_know(monkeypatch):
    rt = _open_session(monkeypatch, key=KEY)
    text = _apikey(monkeypatch, rt, "on")
    assert rt.config.api_key == KEY
    assert "/apikey off" in text


def test_a_key_pasted_after_apikey_is_not_repeated_or_taken(monkeypatch):
    # _apikey also checks the reply never contains the key.
    rt = _open_session(monkeypatch)
    text = _apikey(monkeypatch, rt, OTHER.replace("another", "pasted"))
    assert rt.config.api_key is None
    assert "settings.json" in text


def test_apikey_reaches_the_hub_not_the_cli():
    import commands
    assert commands.classify("/apikey off") == ("apikey", "off")
    assert commands.classify("/apikey") == ("apikey", "")


def test_a_session_given_a_key_is_billed_to_it_from_the_start():
    # Until its CLI's init says, the key is taken at its word: the status bar
    # and /usage treat the session as billed to it.
    import dataclasses
    st = init_state_from_config(dataclasses.replace(parse_args([]), api_key=KEY))
    assert st.is_subscription is False
    assert billed_to_api_key(st)


def _subscribed(monkeypatch):
    monkeypatch.setattr(state_mod, "detect_subscription", lambda: True)
    monkeypatch.setattr(state_mod, "detect_subscription_plan", lambda _d=None: "Max 20x")


def test_a_session_reads_its_plan_from_its_own_account(monkeypatch):
    # The plan a key that goes away gives back is the session's account's.
    import dataclasses
    monkeypatch.setattr(state_mod, "detect_subscription", lambda: True)
    monkeypatch.setattr(state_mod, "detect_subscription_plan",
                        lambda d=None: f"the plan of {d}")
    st = init_state_from_config(
        dataclasses.replace(parse_args([]), config_dir="D:/accounts/b"))
    assert st.subscription_plan == "the plan of D:/accounts/b"


def test_apikey_off_before_any_turn_gives_back_the_accounts_login(monkeypatch):
    _subscribed(monkeypatch)
    rt = _open_session(monkeypatch, key=KEY)
    rt.state = init_state_from_config(rt.config)     # opened with the key
    _apikey(monkeypatch, rt, "off")
    assert not billed_to_api_key(rt.state)
    assert (rt.state.is_subscription, rt.state.subscription_plan) == (True, "Max 20x")
    # ...and the first init, naming no key, leaves it so.
    apply_api_key_source(rt.state, "none", None)
    assert (rt.state.is_subscription, rt.state.subscription_plan) == (True, "Max 20x")


def test_a_key_given_to_an_open_session_bills_it_at_once(monkeypatch):
    _subscribed(monkeypatch)
    rt = _open_session(monkeypatch)
    rt.state = init_state_from_config(rt.config)
    rt.state.session_id = "sid-live"
    asyncio.run(server.api_session_launch(
        {"cwd": "C:\\w", "resume": "sid-live", "api_key": KEY}))
    assert billed_to_api_key(rt.state)
    assert rt.state.is_subscription is False


# --------------------------------------------------------------------------
# What the hub says at launch
# --------------------------------------------------------------------------

def test_no_key_no_notice(monkeypatch):
    assert server._env_api_key_notice(None) is None


def test_the_launch_is_told_only_its_session_is_billed_to_it():
    text = server._env_api_key_notice(KEY)
    assert "only the session this launch opens" in text
    assert KEY not in text
