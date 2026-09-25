"""The status bar shows the name other Claude sessions address this one by.

Asked for 2026-09-24: "update the status bar to include the agent name" --
after a session that was meant to be Lane A turned out to be going by
``os-71``, which only the model could tell, by asking ``ListAgents``.

The name is read from the CLI, not predicted.  Every running CLI keeps a
registry file, ``<config dir>/sessions/<pid>.json``, and its ``name`` is
exactly what ``ListAgents`` prints and ``SendMessage`` addresses (read off a
live one: ``"name":"orchestrator2c"``).  orchestrator2 knows the name when it
set it -- ``--agent-name``, a title -- but not the one the CLI makes up for a
session nobody named, which is the case the field is most needed for.

The tooltip adds orchestrator2's own agent-registry identity -- a separate
name, the same one only when ``--agent-name`` set both.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server                                            # noqa: E402
from config import parse_args                            # noqa: E402
from sdk_bridge import SDKBridge                          # noqa: E402
from session_runtime import SessionRuntime               # noqa: E402
from state import init_state_from_config, state_to_status_dict  # noqa: E402

PID = 4242


def _bridge(tmp_path, argv=()):
    cfg = parse_args(["--config-dir", str(tmp_path), *argv])
    st = init_state_from_config(cfg)

    async def bc(m):
        pass

    br = SDKBridge(config=cfg, state=st, broadcaster=bc)
    br._cli_pid = lambda: PID
    return br, st


def _registry_file(tmp_path, content):
    sessions = tmp_path / "sessions"
    sessions.mkdir(exist_ok=True)
    path = sessions / f"{PID}.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content),
                    encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Reading it from the CLI
# --------------------------------------------------------------------------

def test_the_name_is_the_one_the_cli_advertises(tmp_path):
    _registry_file(tmp_path, {"pid": PID, "name": "os-71", "status": "idle"})
    br, st = _bridge(tmp_path)

    asyncio.run(br.refresh_cli_name())

    assert st.cli_name == "os-71"


def test_it_is_read_from_this_sessions_own_account(tmp_path):
    """A cross-account session's CLI writes under its own config dir."""
    other = tmp_path / "other-account"
    other.mkdir()
    _registry_file(tmp_path, {"name": "wrong-account"})
    _registry_file(other, {"name": "Lane-A"})
    br, st = _bridge(other)

    asyncio.run(br.refresh_cli_name())

    assert st.cli_name == "Lane-A"


def test_a_cli_that_is_not_running_has_no_name(tmp_path):
    br, st = _bridge(tmp_path)
    st.cli_name = "stale"
    br._cli_pid = lambda: None

    asyncio.run(br.refresh_cli_name())

    assert st.cli_name is None


@pytest.mark.parametrize("content", ["{not json", '{"name": 7}', '{"name": "  "}',
                                     "[]"])
def test_a_file_without_a_usable_name_means_no_name(tmp_path, content):
    """Not "keep showing the last one": a name that can no longer be read may
    no longer be the name."""
    _registry_file(tmp_path, content)
    br, st = _bridge(tmp_path)
    st.cli_name = "stale"

    asyncio.run(br.refresh_cli_name())

    assert st.cli_name is None


def test_no_file_yet_means_no_name(tmp_path):
    """The CLI writes it a moment after it starts."""
    br, st = _bridge(tmp_path)

    asyncio.run(br.refresh_cli_name())

    assert st.cli_name is None


def test_it_is_not_reread_on_every_tick(tmp_path):
    """The ticker runs every two seconds per runtime; the name changes rarely."""
    path = _registry_file(tmp_path, {"name": "os-71"})
    br, st = _bridge(tmp_path)
    asyncio.run(br.refresh_cli_name())
    path.write_text(json.dumps({"name": "Lane-A"}), encoding="utf-8")

    asyncio.run(br.refresh_cli_name())

    assert st.cli_name == "os-71"


def test_a_rename_shows_up_once_the_interval_has_passed(tmp_path):
    path = _registry_file(tmp_path, {"name": "os-71"})
    br, st = _bridge(tmp_path)
    asyncio.run(br.refresh_cli_name())
    path.write_text(json.dumps({"name": "Lane-A"}), encoding="utf-8")
    br._cli_name_read_at = time.monotonic() - br.CLI_NAME_REFRESH_S - 1

    asyncio.run(br.refresh_cli_name())

    assert st.cli_name == "Lane-A"


# --------------------------------------------------------------------------
# The registry identity, for the tooltip
# --------------------------------------------------------------------------

def test_the_registry_identity_is_mirrored_into_state(tmp_path):
    br, st = _bridge(tmp_path, ["--agent-name", "Lane-A"])
    st.session_id = "sid-1"

    asyncio.run(br.register_agent())

    assert st.agent_registry_name == "Lane-A"


def test_leaving_the_registry_clears_it(tmp_path):
    br, st = _bridge(tmp_path, ["--agent-name", "Lane-A"])
    st.session_id = "sid-1"
    asyncio.run(br.register_agent())

    asyncio.run(br.deregister_agent())

    assert st.agent_registry_name is None


# --------------------------------------------------------------------------
# Into the status bar
# --------------------------------------------------------------------------

def test_the_status_carries_all_three(tmp_path):
    cfg = parse_args(["--agent-name", "Lane-A"])
    st = init_state_from_config(cfg)
    st.cli_name = "Lane-A"
    st.agent_registry_name = "Lane-A"

    status = state_to_status_dict(st, cfg)

    assert (status["agent_name"], status["agent_registry"],
            status["agent_name_given"]) == ("Lane-A", "Lane-A", "Lane-A")


def test_an_unknown_name_is_not_invented(tmp_path):
    """The bar hides the field rather than show a guess."""
    cfg = parse_args([])
    st = init_state_from_config(cfg)
    st.human_title = "OS Lane A"

    assert state_to_status_dict(st, cfg)["agent_name"] is None


class _WS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))


def test_the_ticker_reads_it_before_it_sends_the_status(tmp_path, monkeypatch):
    """So a rename shows on the tick that notices it, not the one after."""
    cfg = parse_args([])
    st = init_state_from_config(cfg)
    rt = SessionRuntime(config=cfg, state=st)
    ws = _WS()
    rt.add_client(ws)

    class _Bridge:
        async def refresh_cli_name(self):
            st.cli_name = "Lane-A"

        async def poll_agent_comms(self):
            pass

    rt.bridge = _Bridge()

    asyncio.run(server._tick_runtime(rt))

    statuses = [m["status"] for m in ws.sent if m.get("type") == "status_update"]
    assert statuses and statuses[0]["agent_name"] == "Lane-A"
