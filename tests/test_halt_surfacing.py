"""A halt nobody is left to lift must be recognisable as one.

Nothing lifts a halt but an explicit act (spec §6.4).  So a halt whose raiser
has gone -- finished, or moved to another machine along with the project it
protected -- stays up, and every new session on the machine is told to stop,
until somebody notices.  What lets somebody notice:

* the halt prompt gives the halt's **age**, and says when its **raiser has no
  live session** here, with how to lift it if the operator says so;
* the lobby shows **every halt in force**, with a **Lift** button;
* every command a session is told to run works **from the session's own
  directory**, which is any project on the machine, not this one.

Also pinned here: ``check-halt``'s lift hint names the halt's own scope.
``check-halt --scope repo`` reports a machine halt too, and the hint used to
be a bare ``agents.py lift``, whose default scope (repo) does not touch one.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_comms as ac  # noqa: E402
import server  # noqa: E402
from sdk_bridge import SDKBridge  # noqa: E402

DAY = 86400.0


@pytest.fixture
def conn():
    c = ac.connect()
    yield c
    c.close()


def _run_printed(command: str, cwd: Path) -> subprocess.CompletedProcess:
    """Run a command line exactly as a prompt printed it, from *cwd*.

    Only ``python`` is swapped for this interpreter: which one is on PATH is
    the machine's business, while the path after it is what is under test.
    """
    argv = shlex.split(command)
    assert argv[0] == "python", command
    return subprocess.run([sys.executable, *argv[1:]], cwd=str(cwd),
                          capture_output=True, text=True, timeout=180)


# ---------------------------------------------------------------------------
# The registry's answers
# ---------------------------------------------------------------------------

def test_halts_in_force_lists_every_scope_oldest_first(conn):
    now = time.time()
    m = ac.raise_halt("move", by="lane-f", scope_kind="machine", conn=conn,
                      now=now - 3 * DAY)
    r = ac.raise_halt("repair", by="lane-a", scope_kind="repo",
                      scope_value="D:/os", conn=conn, now=now - DAY)
    gone = ac.raise_halt("old", by="lane-b", scope_kind="machine", conn=conn,
                         now=now - 5 * DAY)
    ac.lift_halt(halt_id=gone, conn=conn)
    assert [h.id for h in ac.halts_in_force(conn=conn)] == [m, r]


def test_a_live_raiser_is_running(conn):
    ac.register("lane-f", cwd="D:/os", session_id="s", conn=conn)
    ac.raise_halt("x", by="lane-f", scope_kind="machine", conn=conn)
    assert ac.raiser_running(ac.active_halt(conn=conn), conn=conn) is True


def test_a_raiser_whose_heartbeat_stopped_is_not_running(conn):
    ac.register("lane-f", cwd="D:/os", session_id="s", conn=conn,
                now=time.time() - 10 * ac.AGENT_TTL)
    ac.raise_halt("x", by="lane-f", scope_kind="machine", conn=conn)
    assert ac.raiser_running(ac.active_halt(conn=conn), conn=conn) is False


def test_a_raiser_that_left_the_registry_is_not_running(conn):
    """The case that happened: a clean exit deletes the registration."""
    ac.raise_halt("x", by="lane-f", scope_kind="machine", conn=conn)
    assert ac.raiser_running(ac.active_halt(conn=conn), conn=conn) is False


@pytest.mark.parametrize("by", [ac.OPERATOR, ac.UNKNOWN_AGENT])
def test_a_halt_no_agent_raised_has_no_raiser_to_ask_about(conn, by):
    """Saying "the operator has no live session" would be nonsense."""
    ac.raise_halt("x", by=by, scope_kind="machine", conn=conn)
    assert ac.raiser_running(ac.active_halt(conn=conn), conn=conn) is None


@pytest.mark.parametrize("ago, text", [
    (30, "30s ago"), (600, "10m ago"), (5 * 3600, "5.0h ago"),
    (4 * DAY, "4 days ago")])
def test_age_text(ago, text):
    assert ac.age_text(1_000_000.0 - ago, now=1_000_000.0) == text


def test_the_cli_command_names_the_real_script_by_absolute_path():
    cmd = ac.cli_command("check-halt", "--scope", "machine")
    script = Path(shlex.split(cmd)[1])
    assert script.is_absolute(), cmd
    assert script.is_file(), cmd
    assert script.resolve() == (ROOT / "tools" / "agents.py").resolve()


# ---------------------------------------------------------------------------
# The prompt a session is shown
# ---------------------------------------------------------------------------

def _halt(**over) -> ac.Halt:
    fields = dict(id=1, scope_kind="machine", scope_value="",
                  raised_by="Lane-F", raised_at=1_000_000.0 - 4 * DAY,
                  reason="PC migration")
    fields.update(over)
    return ac.Halt(**fields)


def test_the_prompt_says_how_old_the_halt_is():
    text = SDKBridge._halt_prompt(_halt(), True, now=1_000_000.0)
    assert "4 days ago" in text, text
    assert "PC migration" in text and "Lane-F" in text


def test_the_prompt_says_when_the_raiser_has_gone():
    text = SDKBridge._halt_prompt(_halt(), False, now=1_000_000.0)
    assert "Lane-F, which raised it, has no live session" in text, text
    assert "LiftHalt" in text
    assert ac.cli_command("lift", "--scope", "machine") in text


@pytest.mark.parametrize("running", [True, None])
def test_the_prompt_says_nothing_of_a_raiser_that_is_running_or_is_nobody(running):
    text = SDKBridge._halt_prompt(_halt(), running, now=1_000_000.0)
    assert "no live session" not in text, text
    assert "LiftHalt" not in text, "invited a lift nobody needs"


def test_the_lift_offered_names_the_halts_own_scope():
    text = SDKBridge._halt_prompt(_halt(scope_kind="repo"), False,
                                  now=1_000_000.0)
    assert ac.cli_command("lift", "--scope", "repo") in text, text


def _check_line(text: str) -> str:
    return next(line.strip() for line in text.splitlines()
                if "check-halt" in line)


def test_the_check_command_in_the_prompt_runs_from_another_project(tmp_path):
    """The prompt reaches sessions in every project, and ``tools/agents.py``
    exists in only one of them."""
    ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine")
    text = SDKBridge._halt_prompt(ac.active_halt(), False)
    other_project = tmp_path / "foobar2000"
    other_project.mkdir()
    p = _run_printed(_check_line(text), other_project)
    assert p.returncode == 1, p.stdout + p.stderr
    assert "HALT IN FORCE" in p.stderr


def test_a_delivered_halt_prompt_carries_the_raisers_absence(tmp_path):
    """End to end through the bridge's poll: the registry's answer reaches the
    prompt the session queues."""
    from config import parse_args
    from state import init_state_from_config
    cfg = parse_args(["--agent-name", "lane-b", "--cwd", str(tmp_path),
                      "--config-dir", str(tmp_path / "acct")])
    st = init_state_from_config(cfg)
    st.session_id = "sb"

    async def bcast(_m):
        pass

    br = SDKBridge(config=cfg, state=st, broadcaster=bcast)
    asyncio.run(br.register_agent())
    ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine",
                  now=time.time() - 4 * DAY)
    br._agent_poll_at = 0.0
    asyncio.run(br.poll_agent_comms())
    assert len(st.queued_prompts) == 1
    text = st.queued_prompts[0]
    assert "4 days ago" in text, text
    assert "has no live session" in text, text


# ---------------------------------------------------------------------------
# check-halt
# ---------------------------------------------------------------------------

def test_check_halts_lift_hint_lifts_a_machine_halt_seen_from_a_repo(tmp_path):
    """``check-halt --scope repo`` reports a machine halt; the lift it then
    prints must be one that lifts it."""
    ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine")
    p = _run_printed(f'python "{ac.AGENTS_CLI.as_posix()}" check-halt '
                     f'--scope repo', tmp_path)
    assert p.returncode == 1, p.stdout + p.stderr
    hint = next(line for line in p.stderr.splitlines()
                if "lifted explicitly" in line)
    lift = hint.split(":", 1)[1].strip()
    assert "--scope machine" in lift, hint
    q = _run_printed(lift, tmp_path)
    assert q.returncode == 0, q.stdout + q.stderr
    assert ac.halt_in_force() is False, f"the printed hint lifted nothing: {lift}"


def test_check_halt_gives_the_age_and_the_raisers_absence(tmp_path):
    ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine",
                  now=time.time() - 4 * DAY)
    p = _run_printed(ac.cli_command("check-halt", "--scope", "machine"),
                     tmp_path)
    assert "4 days ago" in p.stderr, p.stderr
    assert "no live session on this machine" in p.stderr, p.stderr


# ---------------------------------------------------------------------------
# The lobby
# ---------------------------------------------------------------------------

class _Ws:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))


def test_the_lobby_payload_lists_the_halts_with_their_raisers_state():
    ac.register("lane-a", cwd="D:/os", session_id="s")
    a = ac.raise_halt("repair", by="lane-a", scope_kind="repo",
                      scope_value="D:/os")
    f = ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine")
    halts = server._halts_payload()
    assert [(h["id"], h["raised_by"], h["raiser_running"]) for h in halts] == [
        (a, "lane-a", True), (f, "Lane-F", False)]
    assert halts[1]["reason"] == "PC migration"
    assert halts[1]["scope_kind"] == "machine"


def test_an_unreadable_registry_gives_the_lobby_nothing(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("disk gone")
    monkeypatch.setattr(ac, "connect", boom)
    assert server._halts_payload() is None


def test_the_fast_landing_payload_carries_the_halts(monkeypatch):
    """A halt is why the sessions listed are idle: worth knowing before the
    disk scan finishes."""
    monkeypatch.setattr(server, "runtimes", {})
    monkeypatch.setattr(server, "stale_sources", lambda: {"count": 0})
    ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine")
    msg = asyncio.run(server._session_list_payload(include_recent=False))
    assert [h["reason"] for h in msg["halts"]] == ["PC migration"]


def test_the_full_session_list_carries_the_halts(monkeypatch):
    monkeypatch.setattr(server, "runtimes", {})
    monkeypatch.setattr(server, "stale_sources", lambda: {"count": 0})
    monkeypatch.setattr(server, "_recent_disk_sessions", lambda limit=40: [])
    monkeypatch.setattr(server, "_foreign_holders", lambda: {})
    ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine")
    msg = asyncio.run(server._session_list_payload())
    assert "recent_pending" not in msg, "took the fast path"
    assert [h["reason"] for h in msg["halts"]] == ["PC migration"]


def _lift(monkeypatch, ws, raw_id):
    pushed: list[object] = []

    async def push(target=None):
        pushed.append(target)

    monkeypatch.setattr(server, "_push_session_list", push)
    handled = asyncio.run(server._handle_lobby_message(
        ws, {"type": "lift_halt", "id": raw_id}))
    return handled, pushed


def test_lift_from_the_lobby_lifts_that_halt_and_no_other(monkeypatch):
    keep = ac.raise_halt("repair", by="lane-a", scope_kind="machine")
    gone = ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine")
    ws = _Ws()
    handled, pushed = _lift(monkeypatch, ws, gone)
    assert handled is True, "not consumed: it would reach the chat"
    assert [h.id for h in ac.halts_in_force()] == [keep]
    assert None in pushed, "the other lobbies were not shown the change"
    assert ws in pushed, "the tab that clicked was not shown the change"


def test_lift_from_the_lobby_refuses_a_bad_id(monkeypatch):
    ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine")
    ws = _Ws()
    handled, _ = _lift(monkeypatch, ws, "everything")
    assert handled is True
    assert ac.halt_in_force() is True
    assert any(m.get("type") == "lobby_notice" for m in ws.sent), ws.sent
