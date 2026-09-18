"""How agents *discover* the comms features.

Implements ``specs/agent-comms-addendum-surfacing.md``, which is a separate
document from the main spec and gets a separate suite for the same reason: it
changes no data model and no semantics, only what an agent can find.

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

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_comms as ac       # noqa: E402
import agent_tools             # noqa: E402
from config import parse_args  # noqa: E402
from sdk_bridge import SDKBridge  # noqa: E402
from state import init_state_from_config  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH2_AGENT_DB", str(tmp_path / "agents.db"))
    monkeypatch.delenv("ORCH2_AGENT_NAME", raising=False)
    yield


@pytest.fixture
def all_alive(monkeypatch):
    monkeypatch.setattr(ac, "_pid_is_a_live_claude", lambda pid: True)


def _config_dirs(tmp_path, names=("a", "b", "c")):
    out = []
    for n in names:
        d = tmp_path / f".claude-{n}"
        (d / "sessions").mkdir(parents=True)
        out.append(d)
    return out


def _advertise(d, pid, name="sess"):
    """Write a session advertisement the way the CLI does."""
    (d / "sessions" / f"{pid}.json").write_text(
        json.dumps({"pid": pid, "sessionId": f"sid-{pid}", "name": name,
                    "cwd": "D:/x", "messagingSocketPath": r"\\.\pipe\x"}),
        encoding="utf-8")
    (d / "sessions" / f"{pid}.deadbeef.key").write_text("k", encoding="utf-8")


# ---------------------------------------------------------------------------
# §1 -- widening the scope, so the shipped tools simply start working
# ---------------------------------------------------------------------------

def test_a_live_session_becomes_visible_from_every_account(tmp_path, all_alive):
    """The defect, at the level the shipped tools actually read."""
    a, b, c = _config_dirs(tmp_path)
    _advertise(a, 111, "lane-a")
    added, _ = ac.mirror_peer_registries([a, b, c])
    assert added == 4, "expected the json and the key in each of the other dirs"
    assert (b / "sessions" / "111.json").exists()
    assert (c / "sessions" / "111.json").exists()


def test_the_auth_key_travels_with_the_advertisement(tmp_path, all_alive):
    """The peer pipe is machine-global but authenticated. A session that could
    be listed and not messaged would be worse than one left invisible."""
    a, b = _config_dirs(tmp_path, ("a", "b"))
    _advertise(a, 111)
    ac.mirror_peer_registries([a, b])
    assert list((b / "sessions").glob("111.*.key")), "key not mirrored"


def test_mirroring_is_idempotent(tmp_path, all_alive):
    a, b = _config_dirs(tmp_path, ("a", "b"))
    _advertise(a, 111)
    first, _ = ac.mirror_peer_registries([a, b])
    second, _ = ac.mirror_peer_registries([a, b])
    assert first > 0 and second == 0


def test_every_session_is_visible_from_every_other_account(tmp_path, all_alive):
    a, b, c = _config_dirs(tmp_path)
    _advertise(a, 111)
    _advertise(b, 222)
    _advertise(c, 333)
    ac.mirror_peer_registries([a, b, c])
    for d in (a, b, c):
        seen = {p.stem for p in (d / "sessions").glob("*.json")}
        assert seen == {"111", "222", "333"}, f"{d.name} sees {seen}"


def test_a_dead_session_is_not_advertised(tmp_path, monkeypatch):
    a, b = _config_dirs(tmp_path, ("a", "b"))
    _advertise(a, 111)
    monkeypatch.setattr(ac, "_pid_is_a_live_claude", lambda pid: False)
    added, _ = ac.mirror_peer_registries([a, b])
    assert added == 0
    assert not (b / "sessions" / "111.json").exists()


def test_a_mirror_is_reaped_when_its_session_exits(tmp_path, monkeypatch):
    """A stale advertisement is worse than a missing one: a peer that cannot be
    reached looks like a hang rather than an absence."""
    a, b = _config_dirs(tmp_path, ("a", "b"))
    _advertise(a, 111)
    monkeypatch.setattr(ac, "_pid_is_a_live_claude", lambda pid: True)
    ac.mirror_peer_registries([a, b])
    assert (b / "sessions" / "111.json").exists()

    monkeypatch.setattr(ac, "_pid_is_a_live_claude", lambda pid: False)
    _added, removed = ac.mirror_peer_registries([a, b])
    assert removed >= 1
    assert not (b / "sessions" / "111.json").exists()


def test_the_reaper_never_deletes_a_real_advertisement(tmp_path, monkeypatch):
    """The one outcome worse than the bug being fixed: unregistering a live
    session somebody else owns.

    Mirrors are tracked in a manifest rather than inferred, because once a
    source is gone a copy is indistinguishable from an original by inspection.
    """
    a, b = _config_dirs(tmp_path, ("a", "b"))
    _advertise(a, 111)
    _advertise(b, 222)
    monkeypatch.setattr(ac, "_pid_is_a_live_claude", lambda pid: False)
    ac.mirror_peer_registries([a, b])
    assert (a / "sessions" / "111.json").exists(), "deleted a real session file"
    assert (b / "sessions" / "222.json").exists(), "deleted a real session file"


def test_a_mirror_is_not_re_mirrored_as_an_original(tmp_path, all_alive):
    """Otherwise each pass treats the last pass's copies as fresh sources and
    the manifest grows without bound."""
    a, b, c = _config_dirs(tmp_path)
    _advertise(a, 111)
    ac.mirror_peer_registries([a, b, c])
    n1 = len(ac._load_manifest())
    ac.mirror_peer_registries([a, b, c])
    assert len(ac._load_manifest()) == n1


def test_turning_it_off_removes_only_the_copies(tmp_path, all_alive):
    a, b = _config_dirs(tmp_path, ("a", "b"))
    _advertise(a, 111)
    ac.mirror_peer_registries([a, b])
    assert ac.unmirror_all() >= 1
    assert (a / "sessions" / "111.json").exists(), "removed the original"
    assert not (b / "sessions" / "111.json").exists()


def test_liveness_requires_the_process_to_actually_be_claude(monkeypatch):
    """Pids are recycled, so 'a process with this number exists' would keep a
    dead session advertised under a stranger's pid."""
    import psutil
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)

    class NotClaude:
        def __init__(self, pid):
            pass

        def name(self):
            return "explorer.exe"

    monkeypatch.setattr(psutil, "Process", NotClaude)
    assert ac._pid_is_a_live_claude(1234) is False


def test_a_malformed_advertisement_is_skipped_not_fatal(tmp_path, all_alive):
    """Another process owns these files; half-written or foreign content must
    not stop the mirror doing its job for everything else."""
    a, b = _config_dirs(tmp_path, ("a", "b"))
    (a / "sessions" / "junk.json").write_text("{not json", encoding="utf-8")
    _advertise(a, 111)
    added, _ = ac.mirror_peer_registries([a, b])
    assert added >= 1
    assert (b / "sessions" / "111.json").exists()


# ---------------------------------------------------------------------------
# §2 -- the one tool an agent gets
# ---------------------------------------------------------------------------

def _tools():
    return {t.name: t for t in agent_tools.build_halt_tools(
        identity_getter=lambda: "lane-a", cwd_getter=lambda: "D:/proj")}


def test_halt_gets_a_tool_and_messaging_does_not():
    """Both halves of the addendum in one assertion. Adding a messaging tool
    here would be the wrong fix: ListAgents/SendMessage already advertise it,
    and only needed the registry scope widened.

    Asserted on the *list*, not a set of names: a set collapses duplicates, so
    an extra tool slipped into the list was invisible to this test until a
    mutation sweep added one and nothing failed.
    """
    tools = agent_tools.build_halt_tools(identity_getter=lambda: "lane-a",
                                         cwd_getter=lambda: "D:/proj")
    assert [t.name for t in tools] == ["RaiseHalt", "LiftHalt"], (
        "the agent-facing tool list changed; §1 forbids adding a messaging "
        "tool and §2 requires exactly the two halt tools")


def test_the_descriptions_say_when_not_just_what():
    """§2: 'a description that only says raises a halt will be read and not
    acted on.' The occasion is the part that earns its tokens."""
    d = agent_tools.RAISE_DESCRIPTION.lower()
    assert "use this when" in d, "no occasion given"
    assert "migrat" in d, "no concrete example of an occasion"
    # Specific sentences, not just the word "lift" -- the description mentions
    # lifting twice, so a looser check passed even with the sentence that
    # carries the meaning deleted.
    assert "stays in force until you lift it" in d, (
        "does not say the halt persists until explicitly lifted")
    assert "never on a timer" in d, "does not say it will not expire on its own"
    assert "use this" in agent_tools.LIFT_DESCRIPTION.lower()


def test_the_raise_description_says_to_prefer_it_to_relaying():
    """The project-level line the addendum says is worth writing is about
    occasion -- so the tool carries it, where it is present at the moment of
    choosing, rather than an instruction file read once at session start."""
    d = agent_tools.RAISE_DESCRIPTION.lower()
    assert "request" in d and "operator" in d


def test_raising_through_the_tool_puts_a_halt_in_force():
    out = asyncio.run(_tools()["RaiseHalt"].handler(
        {"reason": "moving the tree", "scope": "repo"}))
    assert not out.get("isError")
    assert ac.halt_in_force(repo=ac.repo_key("D:/proj")) is True
    text = out["content"][0]["text"]
    assert "moving the tree" in text
    assert "lane-a" in text, "does not name who raised it"


def test_lifting_through_the_tool_clears_it():
    asyncio.run(_tools()["RaiseHalt"].handler({"reason": "x", "scope": "repo"}))
    out = asyncio.run(_tools()["LiftHalt"].handler({"scope": "repo"}))
    assert not out.get("isError")
    assert ac.halt_in_force(repo=ac.repo_key("D:/proj")) is False


def test_a_halt_with_no_reason_is_refused():
    """Every other agent is about to be told to stop, and 'no reason given' is
    not something any of them can act on."""
    out = asyncio.run(_tools()["RaiseHalt"].handler({"reason": "  ",
                                                    "scope": "repo"}))
    assert out.get("isError") is True
    assert ac.halt_in_force(repo=ac.repo_key("D:/proj")) is False


def test_an_unknown_scope_is_refused_by_the_tool():
    out = asyncio.run(_tools()["RaiseHalt"].handler({"reason": "x",
                                                    "scope": "galaxy"}))
    assert out.get("isError") is True


def test_the_tool_reports_who_is_in_scope():
    """A halt whose effect is invisible is one the agent cannot reason about."""
    ac.register("lane-b", cwd="D:/proj", repo=ac.repo_key("D:/proj"))
    out = asyncio.run(_tools()["RaiseHalt"].handler({"reason": "x",
                                                    "scope": "repo"}))
    assert "lane-b" in out["content"][0]["text"]


def test_the_bridge_exposes_the_halt_tools_to_the_model(tmp_path):
    """Discoverable in the tool list is the whole point: an agent cannot use
    what it does not know exists."""
    cfg = parse_args(["--agent-name", "lane-a", "--cwd", str(tmp_path)])
    st = init_state_from_config(cfg)
    br = SDKBridge(config=cfg, state=st, broadcaster=lambda m: None)
    servers = getattr(br._make_options(), "mcp_servers", None) or {}
    assert "agent-halt" in servers


def test_a_user_supplied_mcp_config_is_not_displaced(tmp_path):
    """Adding ours must not silently drop theirs."""
    cfg = parse_args(["--cwd", str(tmp_path), "--mcp-config",
                      '{"mine": {"type": "stdio", "command": "x"}}'])
    st = init_state_from_config(cfg)
    br = SDKBridge(config=cfg, state=st, broadcaster=lambda m: None)
    servers = getattr(br._make_options(), "mcp_servers", None) or {}
    assert "mine" in servers, "the user's MCP server was dropped"


# ---------------------------------------------------------------------------
# §4 -- "what done looks like", as executable statements
# ---------------------------------------------------------------------------

def test_done_1_cross_account_visibility(tmp_path, all_alive):
    """An agent that has read no documentation calls ListAgents and sees
    sessions under other accounts."""
    a, b = _config_dirs(tmp_path, ("a", "b"))
    _advertise(a, 111, "lane-a")
    ac.mirror_peer_registries([a, b])
    assert (b / "sessions" / "111.json").exists()


def test_done_3_an_agent_can_raise_a_halt_through_its_tool_list(tmp_path):
    cfg = parse_args(["--agent-name", "lane-a", "--cwd", str(tmp_path)])
    st = init_state_from_config(cfg)
    br = SDKBridge(config=cfg, state=st, broadcaster=lambda m: None)
    assert "agent-halt" in (getattr(br._make_options(), "mcp_servers", None) or {})
    assert "RaiseHalt" in _tools()


def test_done_4_a_bare_shell_can_ask_with_no_client_running():
    """The checking CLI must work with no agent and no client involved: if
    checking were only reachable through the client, every script that wanted
    to be well-behaved would have to boot an agent to ask permission."""
    import os
    import subprocess
    env = dict(os.environ)
    p = subprocess.run([sys.executable, str(ROOT / "tools" / "agents.py"),
                        "check-halt", "--scope", "repo"],
                       capture_output=True, text=True, timeout=180,
                       cwd=str(ROOT), env=env)
    assert p.returncode == 0
    ac.raise_halt("x", by="op", scope_kind="repo",
                  scope_value=ac.repo_key(str(ROOT)))
    p2 = subprocess.run([sys.executable, str(ROOT / "tools" / "agents.py"),
                         "check-halt", "--scope", "repo"],
                        capture_output=True, text=True, timeout=180,
                        cwd=str(ROOT), env=env)
    assert p2.returncode != 0


def test_the_failure_mode_is_an_EMPTY_listing_not_a_wrong_one(tmp_path, all_alive):
    """The sharpened point of addendum §1.

    The reported episode was not "ListAgents showed the wrong peers", it was
    that it showed *none* -- and an agent that gets one negative answer
    reasonably concludes the capability is absent and stops looking. It never
    tried SendMessage at all. So the property under test is not "the listing is
    accurate" but "the listing is not empty when peers exist", which is a
    weaker claim and a much more important one.
    """
    a, b = _config_dirs(tmp_path, ("a", "b"))
    _advertise(a, 111, "lane-a")

    # Before: account b can see nothing, which is the answer that teaches an
    # agent the feature does not exist here.
    assert list((b / "sessions").glob("*.json")) == []

    ac.mirror_peer_registries([a, b])
    assert list((b / "sessions").glob("*.json")), (
        "account b still sees an empty listing, which reads to an agent as "
        "'no such capability' rather than 'no peers'")


def test_one_live_peer_is_enough_to_avoid_the_empty_answer(tmp_path, monkeypatch):
    """Even when most sessions are dead, the live one must still show: the
    difference between one peer and none is the difference between a working
    feature and an absent one."""
    a, b = _config_dirs(tmp_path, ("a", "b"))
    _advertise(a, 111, "dead-one")
    _advertise(a, 222, "live-one")
    monkeypatch.setattr(ac, "_pid_is_a_live_claude", lambda pid: pid == 222)
    ac.mirror_peer_registries([a, b])
    seen = {p.stem for p in (b / "sessions").glob("*.json")}
    assert seen == {"222"}, seen


def test_a_mirror_records_the_ORIGINAL_as_its_source(tmp_path, all_alive):
    """Provenance, not just presence.

    Each pass skips a destination that already exists, which hides the "treat a
    mirror as a source" bug in the simple case. It shows up when a copy is
    removed and re-made: if mirrors are eligible sources, the new copy is
    recorded as descending from another *mirror*, so reaping it depends on that
    mirror rather than on the session that actually exited.
    """
    a, b, c = _config_dirs(tmp_path)
    # The original lives in the LAST directory scanned, so a *mirror* of it is
    # encountered first on the second pass. With the original first, it always
    # wins the race by luck and the bug cannot show -- which is exactly how the
    # first version of this test passed against the mutation.
    _advertise(c, 111)
    ac.mirror_peer_registries([a, b, c])

    # Something removes b's copy (a stray cleanup, antivirus, a user).
    (b / "sessions" / "111.json").unlink()
    ac.mirror_peer_registries([a, b, c])

    manifest = ac._load_manifest()
    src = manifest.get(str(b / "sessions" / "111.json"))
    assert src is not None, "the copy was not re-made"
    assert Path(src) == c / "sessions" / "111.json", (
        f"recorded provenance is {src}, a mirror rather than the original "
        f"advertisement -- reaping it would then depend on another copy "
        f"instead of on the session that actually exited")
