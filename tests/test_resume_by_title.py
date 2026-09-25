"""``--resume <title>`` into a running hub, and two sessions sharing a name.

Reported 2026-09-24::

    i tried '/rename OS A' and I got "Rename failed: session OS Lane A not
    found on disk". also, 'session' in the status bar shows 'OS Lane' even
    though the session should be OS Lane A

The log had the whole thing.  ``orch2 --resume "OS Lane A"`` joined the running
hub, and the hub opened a runtime with ``resume="OS Lane A"`` -- the *title*.
The CLI accepts a title for ``--resume`` and resumed the right session, but
orchestrator2 had seeded ``state.session_id`` with the title, where it stayed
until the session's first turn: the transcript did not load ("session_dir not
found for OS Lane A"), the status bar showed its first eight characters, and
``/rename`` looked on disk for a session called "OS Lane A".

The hub's own startup already resolved titles, through a helper that fell back
to a *substring* match across every project -- ``--resume "Lane A"`` could open
"OS Lane A".  Both paths now share ``session.resolve_session_ref``: the exact
title of one session in the launch's directory, as the CLI itself does
(2.1.280 resolves with ``{exact: true}`` and refuses a title several sessions
carry).

The same log showed a second fault: the launch before it had opened an empty
session also named ``Lane-A``.  Two live sessions shared the registration, and
when the empty one was torn down for idleness its ``deregister`` deleted the
row -- leaving the real lane session out of the registry, unaddressable and
deaf to halts, while believing itself registered.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_comms                                       # noqa: E402
import sdk_bridge                                        # noqa: E402
import server                                            # noqa: E402
from config import parse_args                            # noqa: E402
from sdk_bridge import SDKBridge                          # noqa: E402
from session import (                                    # noqa: E402
    _sanitize_cwd,
    resolve_session_ref,
    sessions_titled,
)
from state import State, init_state_from_config           # noqa: E402

LANE = "b36977c6-545e-4f0a-9d1e-000000000001"
OTHER = "5a1f0000-0000-4000-8000-000000000002"


def _session(root, cwd, sid, title=None, mtime=None):
    project = root / "projects" / _sanitize_cwd(str(Path(cwd).resolve(strict=False)))
    project.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "user", "sessionId": sid, "cwd": str(cwd),
              "message": {"role": "user", "content": "hi"}}]
    if title:
        lines.append({"type": "custom-title", "customTitle": title, "sessionId": sid})
    path = project / f"{sid}.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def work(tmp_path):
    cwd = tmp_path / "os"
    cwd.mkdir()
    return tmp_path, str(cwd)


# --------------------------------------------------------------------------
# What a title means
# --------------------------------------------------------------------------

def test_a_session_id_is_itself(work):
    root, cwd = work
    _session(root, cwd, LANE, "OS Lane A")

    assert resolve_session_ref(LANE, cwd, str(root)) == LANE


def test_a_title_is_the_session_that_carries_it(work):
    """The report."""
    root, cwd = work
    _session(root, cwd, LANE, "OS Lane A")
    _session(root, cwd, OTHER, "Something else")

    assert resolve_session_ref("OS Lane A", cwd, str(root)) == LANE


def test_case_does_not_matter(work):
    root, cwd = work
    _session(root, cwd, LANE, "OS Lane A")

    assert resolve_session_ref("os LANE a", cwd, str(root)) == LANE


def test_part_of_a_title_is_not_the_title(work):
    """The old startup helper's substring fallback: ``--resume "Lane A"``
    would have opened "OS Lane A" -- the wrong lane's session, silently."""
    root, cwd = work
    _session(root, cwd, LANE, "OS Lane A")

    assert resolve_session_ref("Lane A", cwd, str(root)) == "Lane A"


def test_a_title_two_sessions_carry_is_not_guessed(work):
    """The CLI refuses it too, naming both; handing it through lets it."""
    root, cwd = work
    _session(root, cwd, LANE, "OS Lane A")
    _session(root, cwd, OTHER, "OS Lane A")

    assert resolve_session_ref("OS Lane A", cwd, str(root)) == "OS Lane A"
    assert {sid for sid, _t in sessions_titled("OS Lane A", cwd, str(root))} \
        == {LANE, OTHER}


def test_only_this_directorys_sessions_count(work, tmp_path):
    """The CLI resumes from the launch directory's project; a session with the
    title somewhere else cannot be the one it opens."""
    root, cwd = work
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _session(root, str(elsewhere), LANE, "OS Lane A")

    assert resolve_session_ref("OS Lane A", cwd, str(root)) == "OS Lane A"


def test_only_this_accounts_sessions_count(work, tmp_path):
    root, cwd = work
    other_account = tmp_path / "account-b"
    _session(other_account, cwd, LANE, "OS Lane A")

    assert resolve_session_ref("OS Lane A", cwd, str(root)) == "OS Lane A"


def test_every_session_is_considered_not_just_the_newest_few(work):
    """The listing shown on a failed resume stops at six; resolving must not."""
    root, cwd = work
    now = time.time()
    _session(root, cwd, LANE, "OS Lane A", mtime=now - 3600)
    for i in range(10):
        _session(root, cwd, f"0000000{i}-0000-4000-8000-00000000000{i}",
                 f"newer {i}", mtime=now - i)

    assert resolve_session_ref("OS Lane A", cwd, str(root)) == LANE


# --------------------------------------------------------------------------
# A launch into a running hub
# --------------------------------------------------------------------------

def _launch(monkeypatch, root, cwd, body, runtimes=None):
    got = {}

    class _RT:
        rid = "R9"

    async def fake_create_runtime(**kw):
        got.update(kw)
        return _RT()

    monkeypatch.setattr(server, "_create_runtime", fake_create_runtime)
    monkeypatch.setattr(server, "config", parse_args([]))
    monkeypatch.setattr(server, "runtimes", runtimes or {})
    res = asyncio.run(server.api_session_launch(
        {"cwd": cwd, "config_dir": str(root), **body}))
    return res, got


def test_the_hub_opens_the_titled_session_by_its_id(monkeypatch, work):
    root, cwd = work
    _session(root, cwd, LANE, "OS Lane A")

    _res, got = _launch(monkeypatch, root, cwd, {"resume": "OS Lane A"})

    assert got["resume"] == LANE, "the runtime would carry the title as its session id"


def test_a_titled_session_already_open_is_reused(monkeypatch, work):
    """Keyed on the title, the reuse check could never match, so a second
    launch opened a second runtime on the same session."""
    root, cwd = work
    _session(root, cwd, LANE, "OS Lane A")
    st = State()
    st.session_id = LANE
    open_rt = types.SimpleNamespace(rid="R1", state=st, config=parse_args([]),
                                    bridge=None)

    res, got = _launch(monkeypatch, root, cwd, {"resume": "OS Lane A"},
                       runtimes={"R1": open_rt})

    assert res == {"ok": True, "rid": "R1", "reused": True}
    assert not got, "a second runtime was opened on a session already open"


def test_an_unknown_title_still_goes_to_the_cli(monkeypatch, work):
    """Its refusal is what the connect loop explains; inventing one here would
    be a second, different message for the same thing."""
    root, cwd = work

    _res, got = _launch(monkeypatch, root, cwd, {"resume": "No Such Session"})

    assert got["resume"] == "No Such Session"


# --------------------------------------------------------------------------
# A title several sessions carry
# --------------------------------------------------------------------------

AMBIGUOUS = ('--resume "OS Lane A" matches 2 sessions. Pass one of these '
             'session IDs to disambiguate:\n  b36977c6  (modified ...)')


def test_the_clis_refusal_is_recognised_as_settled():
    """Retrying cannot make a title unique."""
    assert sdk_bridge._is_unknown_session_error(AMBIGUOUS)


def test_it_is_not_reported_as_a_session_that_does_not_exist(monkeypatch):
    class _Failing:
        def __init__(self, options=None):
            pass

        async def connect(self):
            raise RuntimeError(AMBIGUOUS)

        async def disconnect(self):
            pass

    monkeypatch.setattr(sdk_bridge, "ClaudeSDKClient", _Failing)
    cfg = dataclasses.replace(parse_args([]), resume="OS Lane A", no_continue=True)
    st = init_state_from_config(cfg)
    sent: list[dict] = []

    async def bc(m):
        sent.append(m)

    br = SDKBridge(config=cfg, state=st, broadcaster=bc)
    br._initial_resume_id = "OS Lane A"

    async def go():
        task = asyncio.ensure_future(br.worker_loop())
        for _ in range(400):
            await asyncio.sleep(0.01)
            if any(m.get("subtype") == "error" for m in sent) and not st.connecting:
                break
        br.event_queue.put_nowait(("quit", ""))
        await asyncio.wait_for(task, 10)

    asyncio.run(go())
    errors = " ".join((m.get("data") or {}).get("message", "") for m in sent
                      if m.get("subtype") == "error")

    assert "More than one session here is called 'OS Lane A'" in errors
    assert "neither a session id nor" not in errors


# --------------------------------------------------------------------------
# Two sessions under one name
# --------------------------------------------------------------------------

def test_a_closing_session_leaves_anothers_registration_alone():
    """The report's second fault: the empty Lane-A session's teardown deleted
    the registration the real one was using."""
    agent_comms.register("Lane-A", cwd="E:\\os", session_id=LANE)

    agent_comms.deregister("Lane-A", session_id="the-empty-one")

    assert agent_comms.find_agent("Lane-A") is not None


def test_a_closing_session_still_removes_its_own():
    agent_comms.register("Lane-A", cwd="E:\\os", session_id=LANE)

    agent_comms.deregister("Lane-A", session_id=LANE)

    assert agent_comms.find_agent("Lane-A") is None


def test_an_entry_made_before_the_session_had_an_id_is_still_its_own():
    """A new session registers before its first turn gives it an id; if it
    closes before a heartbeat records the id, the entry still reads ''."""
    agent_comms.register("Lane-A", cwd="E:\\os", session_id=None)

    agent_comms.deregister("Lane-A", session_id=LANE)

    assert agent_comms.find_agent("Lane-A") is None


def _bridge(argv=(), session_id=LANE):
    cfg = parse_args(list(argv))
    st = init_state_from_config(cfg)
    st.session_id = session_id
    sent: list[dict] = []

    async def bc(m):
        sent.append(m)

    return SDKBridge(config=cfg, state=st, broadcaster=bc), st, sent


def test_the_bridge_deregisters_only_its_own():
    br, st, _sent = _bridge(["--agent-name", "Lane-A"], session_id="empty-one")
    asyncio.run(br.register_agent())
    agent_comms.register("Lane-A", cwd="E:\\os", session_id=LANE)   # taken over

    asyncio.run(br.deregister_agent())

    assert agent_comms.find_agent("Lane-A").session_id == LANE


def test_a_vanished_registration_is_restored_on_the_next_heartbeat():
    """Otherwise the session goes on believing it is registered while nothing
    can reach it and no halt arrives."""
    br, st, _sent = _bridge(["--agent-name", "Lane-A"])
    asyncio.run(br.register_agent())
    agent_comms.deregister("Lane-A")          # someone else's doing
    br._agent_hb_at = 0.0                     # a heartbeat is due

    asyncio.run(br.poll_agent_comms())

    held = agent_comms.find_agent("Lane-A")
    assert held is not None and held.session_id == LANE


def test_it_is_restored_under_the_same_name():
    """Re-resolving could hand the session a different address.  Here it
    adopted an identity it would never be given fresh, so re-resolving -- with
    the entry gone -- would create the directory's default name instead."""
    br, st, _sent = _bridge(session_id=LANE)
    agent_comms.register("adopted-earlier", cwd=br.config.cwd, session_id="gone",
                         now=time.time() - agent_comms.AGENT_TTL - 60)
    asyncio.run(br.register_agent())
    assert br.agent_identity == "adopted-earlier"
    agent_comms.deregister("adopted-earlier")
    br._agent_hb_at = 0.0

    asyncio.run(br.poll_agent_comms())

    assert br.agent_identity == "adopted-earlier"
    assert agent_comms.find_agent("adopted-earlier").session_id == LANE


def _warnings(sent):
    return " ".join((m.get("data") or {}).get("message", "") for m in sent
                    if m.get("subtype") == "warning")


def test_taking_a_name_another_live_session_holds_is_said_out_loud():
    agent_comms.register("Lane-A", cwd="E:\\os", session_id=OTHER)
    br, st, sent = _bridge(["--agent-name", "Lane-A"], session_id=LANE)

    asyncio.run(br.register_agent())

    assert "Another live session already answers to 'Lane-A'" in _warnings(sent)


def test_the_same_session_reconnecting_is_not_another():
    """A hub restart, a /model reconnect: the entry is this session's own."""
    agent_comms.register("Lane-A", cwd="E:\\os", session_id=LANE)
    br, st, sent = _bridge(["--agent-name", "Lane-A"], session_id=LANE)

    asyncio.run(br.register_agent())

    assert "already answers" not in _warnings(sent)


def test_a_holder_that_stopped_heartbeating_is_not_live():
    agent_comms.register("Lane-A", cwd="E:\\os", session_id=OTHER,
                         now=time.time() - agent_comms.AGENT_TTL - 60)
    br, st, sent = _bridge(["--agent-name", "Lane-A"], session_id=LANE)

    asyncio.run(br.register_agent())

    assert "already answers" not in _warnings(sent)
