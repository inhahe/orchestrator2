"""``--agent-name`` names one session, not the hub.

It used to belong to the hub.  ``Config.agent_name`` was parsed once, for the
hub process, and ``_create_runtime`` builds every session from
``dataclasses.replace(config, ...)`` -- so every session a hub started with
``--agent-name orchestrator2`` later opened registered as "orchestrator2": one
inbox, one halt state, whichever session polled first got the message.
``ORCH2_AGENT_NAME`` did the same through the hub's environment, which every
session also shares.  The other direction was dropped outright: a launch that
*joined* a running hub never handed the flag over, so ``--agent-name Lane-D``
on it registered nothing called Lane-D.

Now it names the session its launch opens -- in both registries, since it is
also that session's ``ListAgents`` name -- and is remembered by session id, so
the session keeps it when it comes back without the flag.  Asked for
2026-09-22: "yes, make --agent-name apply per session".  ``--agent-label`` had
the same fault and gets the same treatment ("yes, do both"): a hub started
with ``--agent-label lane=b`` put lane=b on every session it opened.

The registry here is a real one in a temp directory (``ORCH2_AGENT_DB``), so
these run the real resolve/register/name code rather than stubs of it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_comms                                       # noqa: E402
import commands                                          # noqa: E402
import server                                            # noqa: E402
from config import parse_args                            # noqa: E402
from sdk_bridge import SDKBridge                          # noqa: E402
from state import State, init_state_from_config           # noqa: E402

ENV = "CLAUDE_CODE_SESSION_NAME"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """A private registry, and no names leaking in from the developer's shell."""
    monkeypatch.setenv("ORCH2_AGENT_DB", str(tmp_path / "agents.db"))
    monkeypatch.delenv("ORCH2_AGENT_NAME", raising=False)
    monkeypatch.delenv(ENV, raising=False)


def _bridge(argv=(), **state_over):
    cfg = parse_args(list(argv))
    st = init_state_from_config(cfg)
    for k, v in state_over.items():
        setattr(st, k, v)
    sent: list[dict] = []

    async def bc(m):
        sent.append(m)

    return SDKBridge(config=cfg, state=st, broadcaster=bc), st, sent


# --------------------------------------------------------------------------
# Which launch a name belongs to
# --------------------------------------------------------------------------

def test_the_flag_names_the_launch():
    assert parse_args(["--agent-name", "Lane-D"]).agent_name == "Lane-D"


def test_the_variable_names_the_launch_too(monkeypatch):
    monkeypatch.setenv("ORCH2_AGENT_NAME", "Lane-C")

    assert parse_args([]).agent_name == "Lane-C"


def test_the_variable_is_consumed_by_the_launch_that_read_it(monkeypatch):
    """Left in the environment it is inherited by every CLI the hub starts:
    ``tools/agents.py`` in every session would default to this identity's
    inbox, and a hub an agent starts for a test would claim the name."""
    monkeypatch.setenv("ORCH2_AGENT_NAME", "Lane-C")

    parse_args([])

    assert "ORCH2_AGENT_NAME" not in os.environ


def test_the_flag_beats_the_variable(monkeypatch):
    monkeypatch.setenv("ORCH2_AGENT_NAME", "Lane-C")

    assert parse_args(["--agent-name", "Lane-D"]).agent_name == "Lane-D"


def test_blank_means_not_given(monkeypatch):
    """As it does in agent_comms.resolve_identity: a blank flag falls through
    to the variable, and a blank variable is no name at all."""
    monkeypatch.setenv("ORCH2_AGENT_NAME", "Lane-C")
    assert parse_args(["--agent-name", "  "]).agent_name == "Lane-C"

    monkeypatch.setenv("ORCH2_AGENT_NAME", "   ")
    assert parse_args([]).agent_name is None


def test_the_launchs_session_starts_with_it():
    assert init_state_from_config(parse_args(["--agent-name", "Lane-D"])).agent_name \
        == "Lane-D"


def _config_of_new_runtime(monkeypatch, tmp_path, hub_argv, **kw):
    """The Config _create_runtime builds, caught before any bridge starts."""
    monkeypatch.setattr(server, "config", parse_args(hub_argv))
    seen = {}

    def stop(cfg):
        seen["cfg"] = cfg
        raise RuntimeError("stop here")

    monkeypatch.setattr(server, "init_state_from_config", stop)
    with pytest.raises(RuntimeError):
        asyncio.run(server._create_runtime(cwd=str(tmp_path), no_continue=True, **kw))
    return seen["cfg"]


def test_a_session_the_hub_opens_does_not_inherit_the_hubs_name(monkeypatch, tmp_path):
    """The bug: every session in a hub started with --agent-name was that
    agent."""
    cfg = _config_of_new_runtime(monkeypatch, tmp_path, ["--agent-name", "orchestrator2"])

    assert cfg.agent_name is None


def test_a_session_opened_with_a_name_gets_that_name(monkeypatch, tmp_path):
    cfg = _config_of_new_runtime(monkeypatch, tmp_path, ["--agent-name", "orchestrator2"],
                                 agent_name="Lane-D")

    assert cfg.agent_name == "Lane-D"


# --------------------------------------------------------------------------
# A launch that joins a running hub hands it over
# --------------------------------------------------------------------------

def test_the_hand_over_includes_it():
    assert server._hub_launch_kwargs(parse_args(["--agent-name", "Lane-D"]),
                                     None)["agent_name"] == "Lane-D"


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
        seen.update(json.loads(req.data.decode("utf-8")))
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    server._launch_into_hub(8787, cwd="C:\\w", resume=None, no_continue=False,
                            agent_name="Lane-D")

    assert seen["agent_name"] == "Lane-D"


def test_the_hub_opens_the_session_under_it(monkeypatch, tmp_path):
    got = {}

    class _RT:
        rid = "R7"

    async def fake_create_runtime(**kw):
        got.update(kw)
        return _RT()

    monkeypatch.setattr(server, "_create_runtime", fake_create_runtime)
    monkeypatch.setattr(server, "config", parse_args([]))

    asyncio.run(server.api_session_launch({
        "cwd": str(tmp_path), "no_continue": True, "agent_name": "Lane-D"}))

    assert got["agent_name"] == "Lane-D"


class _LiveBridge:
    def __init__(self):
        self.adopted: list[str] = []
        self.adopted_labels: list[dict] = []
        self.event_queue = asyncio.Queue()
        self.config = None

    async def adopt_agent_name(self, name):
        self.adopted.append(name)

    async def adopt_agent_labels(self, labels):
        self.adopted_labels.append(labels)


def _reuse(monkeypatch, body, *, current_name=None, current_labels=None,
           cli_path=None):
    """Launch at a session the hub already has open."""
    st = State()
    st.session_id = "sid-live"
    st.agent_name = current_name
    st.agent_labels = dict(current_labels or {})
    cfg = parse_args(["--cli-path", cli_path] if cli_path else [])
    br = _LiveBridge()
    br.config = cfg
    rt = types.SimpleNamespace(rid="R1", state=st, config=cfg, bridge=br)
    monkeypatch.setattr(server, "runtimes", {"R1": rt})
    monkeypatch.setattr(server, "config", parse_args([]))
    res = asyncio.run(server.api_session_launch(
        {"cwd": "C:\\w", "resume": "sid-live", **body}))
    return res, rt


def test_naming_a_session_that_is_already_open_names_it(monkeypatch):
    res, rt = _reuse(monkeypatch, {"agent_name": "Lane-D"})

    assert res["reused"] is True
    assert rt.bridge.adopted == ["Lane-D"]


def test_the_same_name_again_changes_nothing(monkeypatch):
    _res, rt = _reuse(monkeypatch, {"agent_name": "Lane-D"}, current_name="Lane-D")

    assert rt.bridge.adopted == []


def test_a_cli_for_a_session_that_is_already_open_is_applied(monkeypatch, tmp_path):
    """--cli-path had the same hole on this path: an open session kept the
    CLI it had.  Fixed at connect time like the model, so it reconnects."""
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"x")

    _res, rt = _reuse(monkeypatch, {"cli_path": str(exe)})

    assert rt.config.cli_path == str(exe)
    assert rt.bridge.config is rt.config, "runtime and bridge disagree on the CLI"
    assert rt.bridge.event_queue.get_nowait() == ("connect", "")


# --------------------------------------------------------------------------
# The processes a launch hands on to
# --------------------------------------------------------------------------

def test_a_detached_child_gets_the_resolved_name(monkeypatch):
    """The same launch finished in another process -- which inherits the
    environment as it is *now*, without the consumed variable."""
    monkeypatch.setenv("ORCH2_AGENT_NAME", "Lane-C")
    cfg = parse_args([])

    argv = server._detach_child_argv(["server.py", "--detach"], cfg, 8421)

    assert argv[argv.index("--agent-name") + 1] == "Lane-C"


def test_a_detached_child_is_not_named_twice():
    cfg = parse_args(["--agent-name", "Lane-D"])

    argv = server._detach_child_argv(
        ["server.py", "--detach", "--agent-name", "Lane-D", "--agent-name=Lane-D"],
        cfg, 8421)

    assert argv.count("--agent-name") == 1
    assert not [a for a in argv if a.startswith("--agent-name=")]


def test_a_restart_keeps_the_primary_sessions_current_name(monkeypatch):
    """The restart re-resumes the primary session by id; it must come back as
    the agent it is, not as whatever the launch command once said."""
    import subprocess
    seen = {}

    def fake_popen(args, **kw):
        seen["argv"] = list(args)
        raise OSError("not really")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sys, "argv", ["server.py", "--agent-name", "Stale", "--open"])
    monkeypatch.setattr(server, "config", parse_args([]))
    st = State()
    st.session_id = "sid-1"
    st.agent_name = "Lane-D"
    monkeypatch.setattr(server, "state", st)

    asyncio.run(server.api_restart())

    argv = seen["argv"]
    assert argv.count("--agent-name") == 1
    assert argv[argv.index("--agent-name") + 1] == "Lane-D"


# --------------------------------------------------------------------------
# The registry identity
# --------------------------------------------------------------------------

def test_the_identity_is_the_sessions_own_name():
    br, st, _sent = _bridge(["--agent-name", "Lane-D"], session_id="sid-1")

    asyncio.run(br.register_agent())

    assert br.agent_identity == "Lane-D"


def test_the_identity_is_never_taken_from_the_environment(monkeypatch):
    """The hub's environment is every session's environment."""
    br, st, _sent = _bridge(session_id="sid-1")
    monkeypatch.setenv("ORCH2_AGENT_NAME", "Lane-C")

    asyncio.run(br.register_agent())

    assert br.agent_identity != "Lane-C"


def test_a_name_the_registry_refuses_is_said_out_loud():
    """"Lane A" -- with its space -- used to vanish into the log, leaving a
    session that looked named and could not be messaged or halted."""
    br, st, sent = _bridge(["--agent-name", "Lane A"], session_id="sid-1")

    asyncio.run(br.register_agent())

    text = " ".join((m.get("data") or {}).get("message", "") for m in sent)
    assert br.agent_identity is None
    assert "ListAgents" in text
    assert "'Lane-A'" in text, "a spelling that works in both is not suggested"


# --------------------------------------------------------------------------
# Remembered with the session
# --------------------------------------------------------------------------

def test_the_name_is_remembered_against_the_session():
    br, st, _sent = _bridge(["--agent-name", "Lane-D"], session_id="sid-1")

    asyncio.run(br._remember_agent_name())

    assert agent_comms.session_name("sid-1") == "Lane-D"


def test_a_new_session_id_is_remembered_too():
    """/clear, or a new session's first turn: same runtime, same agent."""
    br, st, _sent = _bridge(["--agent-name", "Lane-D"], session_id="sid-1")
    asyncio.run(br._remember_agent_name())
    st.session_id = "sid-2"

    asyncio.run(br._remember_agent_name())

    assert agent_comms.session_name("sid-2") == "Lane-D"


def test_remembering_costs_nothing_when_nothing_changed(monkeypatch):
    """It runs on every tick."""
    writes = []
    real = agent_comms.name_session
    monkeypatch.setattr(agent_comms, "name_session",
                        lambda *a, **k: (writes.append(a), real(*a, **k)))
    br, st, _sent = _bridge(["--agent-name", "Lane-D"], session_id="sid-1")

    for _ in range(3):
        asyncio.run(br._remember_agent_name())

    assert len(writes) == 1


def test_it_is_remembered_even_when_the_registry_refused_it():
    """Still the session's ListAgents name, so still worth keeping."""
    br, st, _sent = _bridge(["--agent-name", "Lane A"], session_id="sid-1")

    asyncio.run(br.poll_agent_comms())

    assert agent_comms.session_name("sid-1") == "Lane A"


def _capture_connect(monkeypatch):
    import sdk_bridge
    envs: list[dict] = []

    class _Client:
        def __init__(self, options=None):
            envs.append(dict(options.env))

        async def connect(self):
            raise RuntimeError("stop here")

        async def disconnect(self):
            pass

    monkeypatch.setattr(sdk_bridge, "ClaudeSDKClient", _Client)
    monkeypatch.setattr(sdk_bridge, "read_human_title", lambda *a, **k: None)
    return envs


def _connect(br, resume_id):
    with pytest.raises(Exception):
        asyncio.run(br.connect(resume_id=resume_id))


def test_a_session_reopened_without_the_flag_keeps_its_name(monkeypatch):
    """From the lobby, or after a hub restart: no --agent-name this time."""
    agent_comms.name_session("sid-x", "Lane-D")
    envs = _capture_connect(monkeypatch)
    br, st, _sent = _bridge()

    _connect(br, "sid-x")

    assert st.agent_name == "Lane-D"
    assert envs[-1].get(ENV) == "Lane-D"


def test_the_lookup_is_not_repeated_per_reconnect(monkeypatch):
    agent_comms.name_session("sid-x", "Lane-D")
    lookups = []
    real = agent_comms.session_naming
    monkeypatch.setattr(agent_comms, "session_naming",
                        lambda sid, **k: (lookups.append(sid), real(sid, **k))[1])
    _capture_connect(monkeypatch)
    br, _st, _sent = _bridge()

    _connect(br, "sid-x")
    _connect(br, "sid-x")

    assert lookups == ["sid-x"]


def test_an_unnamed_session_is_not_looked_up_on_every_reconnect(monkeypatch):
    lookups = []
    real = agent_comms.session_naming
    monkeypatch.setattr(agent_comms, "session_naming",
                        lambda sid, **k: (lookups.append(sid), real(sid, **k))[1])
    _capture_connect(monkeypatch)
    br, st, _sent = _bridge()

    _connect(br, "sid-x")
    _connect(br, "sid-x")

    assert lookups == ["sid-x"]
    assert st.agent_name is None


def test_a_named_session_does_not_consult_the_record(monkeypatch):
    """The name it was launched with is newer than any record."""
    agent_comms.name_session("sid-x", "Old-Name")
    envs = _capture_connect(monkeypatch)
    br, st, _sent = _bridge(["--agent-name", "Lane-D"])

    _connect(br, "sid-x")

    assert envs[-1].get(ENV) == "Lane-D"


# --------------------------------------------------------------------------
# Naming a session that is already running
# --------------------------------------------------------------------------

def test_adopting_a_name_moves_the_session_to_it():
    br, st, _sent = _bridge(session_id="sid-1")
    asyncio.run(br.register_agent())
    before = br.agent_identity

    asyncio.run(br.adopt_agent_name("Lane-D"))

    assert st.agent_name == "Lane-D"
    assert br.agent_identity == "Lane-D"
    assert agent_comms.find_agent(before) is None, "the old identity still holds the inbox"
    assert agent_comms.find_agent("Lane-D").session_id == "sid-1"
    assert agent_comms.session_name("sid-1") == "Lane-D"


def test_adopting_a_name_tells_the_running_cli():
    """Its name is read at startup; without this it would change only at the
    next reconnect, which ends the session's background tasks."""
    br, st, _sent = _bridge(session_id="sid-1")

    asyncio.run(br.adopt_agent_name("Lane-D"))

    assert br._pending_cli_command == "/rename Lane-D"


# --------------------------------------------------------------------------
# /rename in a named session
# --------------------------------------------------------------------------

def _rename(st, title, monkeypatch):
    monkeypatch.setattr(commands, "write_session_title", lambda *a, **k: None)
    return commands.try_immediate_command("rename", title, st, parse_args([]))


def test_rename_in_a_named_session_changes_only_the_title(monkeypatch):
    """Handing the CLI the /rename would take it off its agent name until the
    next reconnect put it back -- the two registries out of step meanwhile."""
    st = State()
    st.session_id = "sid-1"
    st.agent_name = "Lane-D"

    res = _rename(st, "Networking", monkeypatch)
    text = " ".join((m.get("data") or {}).get("message", "") for m in res.messages)

    assert st.session_title == "Networking"
    assert not res.forward_to_sdk
    assert "Lane-D" in text and "title" in text


def test_the_same_before_the_session_exists(monkeypatch):
    st = State()
    st.session_id = None
    st.agent_name = "Lane-D"

    res = _rename(st, "Networking", monkeypatch)

    assert not res.forward_to_sdk


# --------------------------------------------------------------------------
# The record itself
# --------------------------------------------------------------------------

def test_the_record_round_trips_and_the_newest_wins():
    agent_comms.name_session("sid-1", "Lane-C")
    agent_comms.name_session("sid-1", "Lane-D")

    assert agent_comms.session_name("sid-1") == "Lane-D"


def test_the_record_ignores_blanks():
    agent_comms.name_session("sid-1", "   ")
    agent_comms.name_session("", "Lane-D")

    assert agent_comms.session_name("sid-1") is None
    assert agent_comms.session_name("") is None


def test_a_blank_write_does_not_erase_what_was_remembered():
    """Nothing to say is not "forget": the upsert would otherwise replace the
    session's name and labels with nothing."""
    agent_comms.name_session("sid-1", "Lane-D", labels={"lane": "d"})

    agent_comms.name_session("sid-1", "  ", labels={})

    assert agent_comms.session_naming("sid-1") == ("Lane-D", {"lane": "d"})


def test_it_outlives_the_registration():
    """The whole reason it is not a column of ``agents``: that row is deleted
    on a clean exit."""
    agent_comms.register("Lane-D", cwd="C:\\w", session_id="sid-1")
    agent_comms.name_session("sid-1", "Lane-D")

    agent_comms.deregister("Lane-D")

    assert agent_comms.session_name("sid-1") == "Lane-D"


def _old_store(path, schema):
    import sqlite3
    old = sqlite3.connect(str(path))
    old.executescript(schema)
    old.commit()
    old.close()


def test_an_existing_registry_gains_the_table(tmp_path):
    """Stores written before this existed must just work."""
    path = tmp_path / "old.db"
    before = agent_comms._SCHEMA.split("-- The name and labels a session")[0]
    assert "session_names" not in before
    _old_store(path, before)

    conn = agent_comms.connect(path)
    try:
        agent_comms.name_session("sid-1", "Lane-D", labels={"lane": "d"}, conn=conn)
        assert agent_comms.session_naming("sid-1", conn=conn) == ("Lane-D", {"lane": "d"})
    finally:
        conn.close()


def test_a_table_from_before_labels_gains_the_column(tmp_path):
    """Not hypothetical: the machine's real registry has exactly this table,
    created by a test run on the day names were added (2026-09-22), before
    labels were -- CREATE TABLE IF NOT EXISTS would leave it as it is."""
    path = tmp_path / "old.db"
    before = agent_comms._SCHEMA.split("-- The name and labels a session")[0]
    _old_store(path, before + (
        "CREATE TABLE session_names (session_id TEXT PRIMARY KEY, "
        "name TEXT NOT NULL, named_at REAL NOT NULL);"))

    conn = agent_comms.connect(path)
    try:
        agent_comms.name_session("sid-1", "Lane-D", labels={"lane": "d"}, conn=conn)
        assert agent_comms.session_naming("sid-1", conn=conn) == ("Lane-D", {"lane": "d"})
    finally:
        conn.close()


# --------------------------------------------------------------------------
# --agent-label: the same, for labels
# --------------------------------------------------------------------------

def test_a_session_the_hub_opens_does_not_inherit_its_labels(monkeypatch, tmp_path):
    """The hub's --agent-label lane=b is not every session's lane -- and
    ``agents.py list`` showing it on all of them is worse than no label."""
    cfg = _config_of_new_runtime(monkeypatch, tmp_path, ["--agent-label", "lane=b"])

    assert cfg.agent_labels == {}


def test_a_session_opened_with_labels_gets_them(monkeypatch, tmp_path):
    cfg = _config_of_new_runtime(monkeypatch, tmp_path, ["--agent-label", "lane=b"],
                                 agent_labels={"lane": "d"})

    assert cfg.agent_labels == {"lane": "d"}


def test_the_launchs_session_starts_with_its_labels():
    st = init_state_from_config(parse_args(["--agent-label", "lane=d"]))

    assert st.agent_labels == {"lane": "d"}


def test_labels_are_handed_over(monkeypatch):
    import urllib.request
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true, "rid": "R9"}'

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: (
        seen.update(json.loads(req.data.decode("utf-8"))), _Resp())[1])
    cfg = parse_args(["--agent-label", "lane=d", "--agent-label", "role=reviewer"])

    server._launch_into_hub(8787, **server._hub_launch_kwargs(cfg, None))

    assert seen["agent_labels"] == {"lane": "d", "role": "reviewer"}


def test_the_hub_opens_the_session_with_them(monkeypatch, tmp_path):
    got = {}

    class _RT:
        rid = "R7"

    async def fake_create_runtime(**kw):
        got.update(kw)
        return _RT()

    monkeypatch.setattr(server, "_create_runtime", fake_create_runtime)
    monkeypatch.setattr(server, "config", parse_args([]))

    asyncio.run(server.api_session_launch({
        "cwd": str(tmp_path), "no_continue": True,
        "agent_labels": {"lane": "d", "": "dropped", "n": None}}))

    assert got["agent_labels"] == {"lane": "d", "n": ""}


def test_labels_that_are_not_an_object_are_ignored(monkeypatch, tmp_path):
    """A malformed annotation must not refuse the launch."""
    got = {}

    class _RT:
        rid = "R7"

    async def fake_create_runtime(**kw):
        got.update(kw)
        return _RT()

    monkeypatch.setattr(server, "_create_runtime", fake_create_runtime)
    monkeypatch.setattr(server, "config", parse_args([]))

    res = asyncio.run(server.api_session_launch({
        "cwd": str(tmp_path), "no_continue": True, "agent_labels": ["lane=d"]}))

    assert res["ok"] is True
    assert got["agent_labels"] == {}


def test_labels_for_a_session_that_is_already_open_are_applied(monkeypatch):
    _res, rt = _reuse(monkeypatch, {"agent_labels": {"lane": "d"}})

    assert rt.bridge.adopted_labels == [{"lane": "d"}]


def test_the_same_labels_again_change_nothing(monkeypatch):
    _res, rt = _reuse(monkeypatch, {"agent_labels": {"lane": "d"}},
                      current_labels={"lane": "d"})

    assert rt.bridge.adopted_labels == []


def test_a_restart_keeps_the_primary_sessions_current_labels(monkeypatch):
    import subprocess
    seen = {}

    def fake_popen(args, **kw):
        seen["argv"] = list(args)
        raise OSError("not really")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sys, "argv", ["server.py", "--agent-label", "lane=stale",
                                      "--agent-label=role=old"])
    monkeypatch.setattr(server, "config", parse_args([]))
    st = State()
    st.session_id = "sid-1"
    st.agent_labels = {"lane": "d"}
    monkeypatch.setattr(server, "state", st)

    asyncio.run(server.api_restart())

    argv = seen["argv"]
    assert "lane=stale" not in argv and "--agent-label=role=old" not in argv
    assert argv.count("--agent-label") == 1
    assert argv[argv.index("--agent-label") + 1] == "lane=d"


def test_the_registry_entry_carries_the_sessions_labels():
    br, st, _sent = _bridge(["--agent-label", "lane=d"], session_id="sid-1")

    asyncio.run(br.register_agent())

    assert agent_comms.find_agent(br.agent_identity).labels.get("lane") == "d"


def test_labels_are_remembered_with_the_session():
    br, st, _sent = _bridge(["--agent-label", "lane=d"], session_id="sid-1")

    asyncio.run(br._remember_agent_name())

    assert agent_comms.session_naming("sid-1") == (None, {"lane": "d"})


def test_a_label_change_is_remembered_too():
    br, st, _sent = _bridge(["--agent-name", "Lane-D", "--agent-label", "lane=d"],
                            session_id="sid-1")
    asyncio.run(br._remember_agent_name())
    st.agent_labels = {"lane": "d", "role": "reviewer"}

    asyncio.run(br._remember_agent_name())

    assert agent_comms.session_naming("sid-1") == (
        "Lane-D", {"lane": "d", "role": "reviewer"})


def test_a_session_reopened_without_flags_keeps_its_labels(monkeypatch):
    agent_comms.name_session("sid-x", "Lane-D", labels={"lane": "d"})
    _capture_connect(monkeypatch)
    br, st, _sent = _bridge()

    _connect(br, "sid-x")

    assert st.agent_labels == {"lane": "d"}


def test_a_named_session_still_gets_its_remembered_labels(monkeypatch):
    """Launched with --agent-name but no labels: what it was not given this
    time comes back, as the name does."""
    agent_comms.name_session("sid-x", "Lane-D", labels={"lane": "d"})
    _capture_connect(monkeypatch)
    br, st, _sent = _bridge(["--agent-name", "Lane-D"])

    _connect(br, "sid-x")

    assert st.agent_labels == {"lane": "d"}


def test_labels_given_this_launch_beat_remembered_ones(monkeypatch):
    agent_comms.name_session("sid-x", "Lane-D", labels={"lane": "old"})
    _capture_connect(monkeypatch)
    br, st, _sent = _bridge(["--agent-label", "lane=new"])

    _connect(br, "sid-x")

    assert st.agent_labels == {"lane": "new"}
    assert st.agent_name == "Lane-D", "the name it was not given this time is still restored"


def test_labels_for_a_running_session_are_published_at_once():
    br, st, _sent = _bridge(["--agent-label", "lane=b"], session_id="sid-1")
    asyncio.run(br.register_agent())

    asyncio.run(br.adopt_agent_labels({"lane": "d", "role": "reviewer"}))

    labels = agent_comms.find_agent(br.agent_identity).labels
    assert labels.get("lane") == "d" and labels.get("role") == "reviewer"
    assert agent_comms.session_naming("sid-1")[1] == {"lane": "d", "role": "reviewer"}


def test_a_label_only_record_is_not_a_name():
    agent_comms.name_session("sid-1", None, labels={"lane": "d"})

    assert agent_comms.session_name("sid-1") is None
