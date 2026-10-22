"""How agents *discover* the comms features.

Implements the surfacing addendum to the agent-comms spec (its § numbers below;
the addendum itself was never kept in a tree, and agent-comms' design.md,
"Surfacing", sums it up).  It gets a suite of its own because it changes no
data model and no semantics, only what an agent can find.

Its two rules pull in opposite directions, and both matter:

* **Messaging gets no new tool and no documentation** (§1).  The description
  already shipped to every agent says ``ListAgents`` lists "other local Claude
  sessions on this machine", and on 2026-09-06 an agent reached for it
  unprompted with nothing in any project file telling it to.

  But the same episode shows the limit, and it is why these tests exist at all:
  the call answered "no other Claude session is running on this machine" while
  two were demonstrably working, and the agent then **stopped**.  It never
  tried ``SendMessage``, concluded the capability was absent, and spent the
  next hour building a file-based workaround.  **An empty ``ListAgents`` is
  indistinguishable from "this feature does not exist here"**, and no quality
  of tool description compensates for a listing that comes back empty -- by
  then the agent has its answer.  So widening the registry scope is not merely
  a correctness fix that makes shipped text true; it is what makes the
  capability discoverable at all.
* **Halt gets exactly one tool** (§2), because the spec never said how an agent
  *raises* one, and an agent cannot use what it cannot discover.

Found by inspection rather than assumption: the CLI advertises itself in
``<CLAUDE_CONFIG_DIR>/sessions/<pid>.json`` (plus a ``<pid>.<hash>.key``) and
peers then talk over a **machine-global** named pipe recorded in that file.  The
transport was never account-scoped -- only the directory listing was.

Verified on this machine by *observation*, which §1 and §4.2 both insist on --
"do not treat 'the tools already describe it' as evidence that agents will use
it", and verify delivery "by observing a real message arrive in a second
session's transcript, not by checking that the call returns success":

* ``ListAgents`` went from **1 peer to 4** once the mirror was in place, the
  three new ones running under two other accounts.
* A ``SendMessage`` from an account-c session was then found in the
  **recipient's own transcript** under account-b::

      22:14:01.645Z  queue-operation
      22:14:01.677Z  user       <cross-session-message from-name="orchestrator2-0c" ...>
      22:14:07.372Z  assistant  "a connectivity test from another session
                                 (orchestrator2-0c, account C)"

  Queued at :01, processed at :07 -- the recipient's next turn boundary.  The
  ``from`` attribute was the machine-global pipe path, confirming the transport
  was never the account-scoped part.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_comms as ac       # noqa: E402
from agent_comms import server as halt_server  # noqa: E402
from config import parse_args  # noqa: E402
from sdk_bridge import SDKBridge, halt_server_config  # noqa: E402
from state import init_state_from_config  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv(ac.DB_ENV, str(tmp_path / "agents.db"))
    monkeypatch.delenv("ORCH2_AGENT_NAME", raising=False)
    yield


# The mirror and the tools themselves are agent-comms', and tested there.  What
# is left here is that every session orchestrator2 runs is given the tools.

def _servers(tmp_path, *extra):
    cfg = parse_args(["--agent-name", "lane-a", "--cwd", str(tmp_path), *extra])
    st = init_state_from_config(cfg)
    br = SDKBridge(config=cfg, state=st, broadcaster=lambda m: None)
    return getattr(br._make_options(), "mcp_servers", None) or {}


def test_the_bridge_exposes_the_halt_tools_to_the_model(tmp_path):
    """Discoverable in the tool list is the whole point: an agent cannot use
    what it does not know exists.  They are agent-comms' own server, run by
    this interpreter, which has the package."""
    srv = _servers(tmp_path).get("agent-halt")
    assert srv == halt_server_config(), srv
    assert srv["type"] == "stdio" and [srv["command"], *srv["args"]] == ac.server_command()
    assert Path(srv["command"]).is_file() and "pythonw" not in Path(srv["command"]).name.lower()


MINE = {"type": "stdio", "command": "x"}


@pytest.mark.parametrize("given", [
    json.dumps({"mcpServers": {"mine": MINE}}),          # the CLI's own shape
    json.dumps({"mine": MINE}),                          # the bare map
    "servers.json",                                      # a file, from the session's directory
])
def test_a_user_supplied_mcp_config_gets_the_halt_tools_beside_it(tmp_path, given):
    """Adding ours must not drop theirs, nor theirs ours."""
    (tmp_path / "servers.json").write_text(json.dumps({"mcpServers": {"mine": MINE}}),
                                           encoding="utf-8")
    servers = _servers(tmp_path, "--mcp-config", given)
    assert servers.get("mine") == MINE, "the user's MCP server was dropped"
    assert servers.get("agent-halt") == halt_server_config(), "the halt tools were dropped"


def test_an_mcp_config_this_cannot_read_is_passed_on_as_given(tmp_path):
    """For the CLI to read or refuse: there is nothing to add ours to."""
    assert _servers(tmp_path, "--mcp-config", "no-such-file.json") == "no-such-file.json"


def test_an_unreadable_mcp_config_is_warned_of_once(tmp_path, monkeypatch, caplog):
    """The options are built at every connect: a warning each time would bury
    the one that mattered."""
    import sdk_bridge
    monkeypatch.setattr(sdk_bridge, "_UNREADABLE_MCP_CONFIGS", set())
    for _ in range(3):
        _servers(tmp_path, "--mcp-config", "no-such-file.json")
    assert sum("neither a JSON file nor JSON" in r.getMessage() for r in caplog.records) == 1


def test_a_server_of_theirs_by_the_halt_tools_name_wins(tmp_path):
    servers = _servers(tmp_path, "--mcp-config", json.dumps({"agent-halt": MINE}))
    assert servers["agent-halt"] == MINE


# ---------------------------------------------------------------------------
# §4 -- "what done looks like", as executable statements
# ---------------------------------------------------------------------------

def test_done_3_an_agent_can_raise_a_halt_through_its_tool_list(tmp_path):
    assert "agent-halt" in _servers(tmp_path)
    listed = halt_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "RaiseHalt" in [t["name"] for t in listed["result"]["tools"]]
