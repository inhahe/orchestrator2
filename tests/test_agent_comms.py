"""Cross-account agent identity, registry, messaging and halt.

Implements ``D:/visual studio projects/specs/agent-comms-spec.md``; section
numbers below refer to it.

The defect the whole feature exists to fix: several Claude Code sessions work
one machine and one repository at once and **cannot address each other at
all**, because the session registry is scoped per ``CLAUDE_CONFIG_DIR`` and
they run under different accounts.  So the first thing these tests pin is that
two agents under *different* accounts can see each other (§4.1, acceptance
§8.1) -- everything else is downstream of that.

The two properties worth stating up front, because they are the ones a
plausible-looking implementation gets wrong:

* **Refusing beats guessing** (§3.3).  A fresh session in a directory with
  several registered identities must refuse to pick one.  Adopting the wrong
  one inherits another agent's message queue and halt state, and *neither*
  agent can detect it: the wrong one is told to stop, the right one never is.
  A failure with no symptom.
* **A notice is not a halt** (§6.3).  ``check-halt`` must exit non-zero when
  halted and zero when a mere message is pending.  A checker tested only on
  halts passes for one that reports everything; tested only on quiet, for one
  that reports nothing.  Both directions, every rule.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_comms as ac  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """A private registry per test -- never the machine-wide real one."""
    monkeypatch.setenv("ORCH2_AGENT_DB", str(tmp_path / "agents.db"))
    monkeypatch.delenv("ORCH2_AGENT_NAME", raising=False)
    yield


@pytest.fixture
def conn():
    c = ac.connect()
    yield c
    c.close()


def _reg(conn, ident, *, cwd="D:/proj", sid=None, repo="R", account="acct",
         now=None):
    return ac.register(ident, cwd=cwd, session_id=sid or f"sess-{ident}",
                       repo=repo, account=account, conn=conn, now=now)


# ---------------------------------------------------------------------------
# §4.1 -- the defect itself
# ---------------------------------------------------------------------------

def test_agents_under_different_accounts_see_each_other(conn):
    """Acceptance §8.1. This is the whole point: per-CLAUDE_CONFIG_DIR scoping
    is why sessions are mutually invisible today."""
    _reg(conn, "lane-a", account=r"C:\Users\x\.claude")
    _reg(conn, "lane-b", account=r"C:\Users\x\.claude-account-b")
    _reg(conn, "lane-c", account=r"C:\Users\x\.claude-account-c")
    live = ac.live_agents(conn=conn)
    assert [a.identity for a in live] == ["lane-a", "lane-b", "lane-c"]
    assert len({a.account for a in live}) == 3, "accounts collapsed together"


def test_the_store_is_not_inside_a_config_dir(monkeypatch):
    """If it lived under CLAUDE_CONFIG_DIR the feature would reproduce the bug
    it exists to fix."""
    monkeypatch.delenv("ORCH2_AGENT_DB", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", r"C:\Users\x\.claude-account-b")
    p = str(ac.db_path()).lower()
    assert ".claude" not in p, f"registry lives under a config dir: {p}"


# ---------------------------------------------------------------------------
# §3.3 -- identity resolution, row by row
# ---------------------------------------------------------------------------

def test_an_explicit_name_is_used_verbatim(conn):
    ident, how = ac.resolve_identity(cwd="D:/proj", explicit="lane-a", conn=conn)
    assert (ident, how) == ("lane-a", "explicit")


def test_a_resumed_session_inherits_its_prior_identity(conn):
    """Unambiguous by definition -- the session id names exactly one."""
    _reg(conn, "lane-a", cwd="D:/proj", sid="s1")
    assert ac.resolve_identity(cwd="D:/proj", session_id="s1", conn=conn) == (
        "lane-a", "resumed")


def test_a_fresh_directory_creates_and_persists_one(conn):
    ident, how = ac.resolve_identity(cwd="D:/proj/alpha", conn=conn)
    assert how == "created"
    _reg(conn, ident, cwd="D:/proj/alpha", sid="s1")
    assert ac.resolve_identity(cwd="D:/proj/alpha", session_id="s1",
                               conn=conn)[0] == ident


def test_a_lone_dead_identity_is_adopted(conn):
    """Row 3: exactly one registered for this cwd, and not live."""
    _reg(conn, "lane-a", cwd="D:/proj", now=time.time() - 10 * ac.AGENT_TTL)
    assert ac.resolve_identity(cwd="D:/proj", conn=conn) == ("lane-a", "adopted")


def test_a_lone_LIVE_identity_is_never_adopted(conn):
    """Also row 3, and the qualifier is load-bearing: adopting a live identity
    would put two concurrent sessions on one name, so each would consume the
    other's messages."""
    _reg(conn, "lane-a", cwd="D:/proj")
    ident, how = ac.resolve_identity(cwd="D:/proj", conn=conn)
    assert how == "created"
    assert ident != "lane-a"


def test_several_identities_and_no_name_is_refused(conn):
    """Row 4, the load-bearing refusal. Guessing here is undetectable by
    either agent."""
    dead = time.time() - 10 * ac.AGENT_TTL
    _reg(conn, "lane-a", cwd="D:/proj", now=dead)
    _reg(conn, "lane-b", cwd="D:/proj", now=dead)
    with pytest.raises(ac.IdentityRefused) as e:
        ac.resolve_identity(cwd="D:/proj", conn=conn)
    assert sorted(e.value.candidates) == ["lane-a", "lane-b"]


def test_the_refusal_explains_itself_and_names_the_way_out(conn):
    dead = time.time() - 10 * ac.AGENT_TTL
    _reg(conn, "lane-a", cwd="D:/proj", now=dead)
    _reg(conn, "lane-b", cwd="D:/proj", now=dead)
    with pytest.raises(ac.IdentityRefused) as e:
        ac.resolve_identity(cwd="D:/proj", conn=conn)
    msg = str(e.value)
    assert "--agent-name" in msg, "no way out offered"
    assert "lane-a" in msg and "lane-b" in msg, "candidates not listed"


def test_an_explicit_name_resolves_even_when_ambiguous(conn):
    """The refusal is about *not knowing*; being told is the answer to it."""
    dead = time.time() - 10 * ac.AGENT_TTL
    _reg(conn, "lane-a", cwd="D:/proj", now=dead)
    _reg(conn, "lane-b", cwd="D:/proj", now=dead)
    assert ac.resolve_identity(cwd="D:/proj", explicit="lane-b",
                               conn=conn) == ("lane-b", "explicit")


def test_a_resumed_session_resolves_even_when_ambiguous(conn):
    dead = time.time() - 10 * ac.AGENT_TTL
    _reg(conn, "lane-a", cwd="D:/proj", sid="sa", now=dead)
    _reg(conn, "lane-b", cwd="D:/proj", sid="sb", now=dead)
    assert ac.resolve_identity(cwd="D:/proj", session_id="sb",
                               conn=conn)[0] == "lane-b"


@pytest.mark.parametrize("bad", ["a b", "x/y", "-lead", "e" * 65, "semi;colon"])
def test_an_unusable_name_is_rejected_rather_than_stored(conn, bad):
    """The identity reaches a filesystem-ish key space and a prompt; junk in it
    is a problem discovered much later, somewhere less obvious."""
    with pytest.raises(ac.AgentCommsError):
        ac.resolve_identity(cwd="D:/proj", explicit=bad, conn=conn)


@pytest.mark.parametrize("blank", ["", "   ", "	"])
def test_a_blank_name_means_unspecified_not_invalid(conn, blank):
    """``--agent-name ""`` and an unset ORCH2_AGENT_NAME are the same input and
    must behave the same way. They did not: the empty string fell through to
    normal resolution while whitespace raised, so the flag and the environment
    variable disagreed about identical intent."""
    ident, how = ac.resolve_identity(cwd="D:/proj", explicit=blank, conn=conn)
    assert how == "created"
    assert ident


def test_identity_is_not_derived_from_the_account(conn):
    """§3.1: two agents under one account must still be distinguishable."""
    a, _ = ac.resolve_identity(cwd="D:/one", conn=conn)
    _reg(conn, a, cwd="D:/one", account="same")
    b, _ = ac.resolve_identity(cwd="D:/two", conn=conn)
    _reg(conn, b, cwd="D:/two", account="same")
    assert a != b


def test_identity_survives_the_tree_moving_to_another_drive(conn):
    """§7.2. The cwd changes; the session id does not."""
    _reg(conn, "lane-a", cwd="D:/proj", sid="s1")
    ac.register("lane-a", cwd="E:/proj", session_id="s1", conn=conn)
    assert ac.resolve_identity(cwd="E:/proj", session_id="s1",
                               conn=conn) == ("lane-a", "resumed")
    assert (ac.find_agent("lane-a", conn=conn) or ac.Agent("", "")).cwd == \
        ac.norm_path("E:/proj")


# ---------------------------------------------------------------------------
# §4.3 -- liveness
# ---------------------------------------------------------------------------

def test_a_registration_expires(conn):
    _reg(conn, "lane-a", now=time.time() - 10 * ac.AGENT_TTL)
    assert ac.live_agents(conn=conn) == []


def test_a_heartbeat_revives_it(conn):
    _reg(conn, "lane-a", now=time.time() - 10 * ac.AGENT_TTL)
    assert ac.heartbeat("lane-a", conn=conn) is True
    assert [a.identity for a in ac.live_agents(conn=conn)] == ["lane-a"]


def test_a_heartbeat_for_an_unknown_agent_reports_failure(conn):
    assert ac.heartbeat("nobody", conn=conn) is False


def test_deregistering_removes_it(conn):
    _reg(conn, "lane-a")
    assert ac.deregister("lane-a", conn=conn) is True
    assert ac.live_agents(conn=conn) == []
    assert ac.deregister("lane-a", conn=conn) is False


def test_registering_again_keeps_the_original_start_time(conn):
    """A reconnect is not a new agent; 'running since' should not reset."""
    t0 = time.time() - 5000
    _reg(conn, "lane-a", now=t0)
    again = ac.register("lane-a", cwd="D:/proj", conn=conn)
    assert again.started_at == pytest.approx(t0)


def test_lookup_by_cwd_reports_liveness_per_identity(conn):
    _reg(conn, "live-one", cwd="D:/proj")
    _reg(conn, "dead-one", cwd="D:/proj", now=time.time() - 10 * ac.AGENT_TTL)
    assert ac.identities_for_cwd("D:/proj", conn=conn) == [
        ("dead-one", False), ("live-one", True)]


def test_lookup_is_scoped_by_repo(conn):
    _reg(conn, "a", repo="R1")
    _reg(conn, "b", repo="R2")
    assert [x.identity for x in ac.live_agents(repo="R1", conn=conn)] == ["a"]


def test_paths_are_normalised_so_one_directory_is_one_directory(conn):
    """Otherwise §3.3's "how many are registered here" answers wrongly -- in
    the direction that guesses."""
    _reg(conn, "lane-a", cwd="D:/proj/../proj")
    assert ac.identities_for_cwd("D:/proj", conn=conn) == [("lane-a", True)]


# ---------------------------------------------------------------------------
# §5 -- messaging
# ---------------------------------------------------------------------------

def test_a_direct_message_reaches_only_its_addressee(conn):
    _reg(conn, "a"); _reg(conn, "b"); _reg(conn, "c")
    ac.send_message("stop", frm="a", to="b", conn=conn)
    assert [m.body for m in ac.pending_messages("b", conn=conn)] == ["stop"]
    assert ac.pending_messages("c", conn=conn) == []


def test_a_broadcast_reaches_the_repo_and_not_beyond(conn):
    _reg(conn, "a", repo="R1"); _reg(conn, "b", repo="R1")
    _reg(conn, "z", repo="R2")
    ac.send_message("tree is moving", frm="a", scope_kind="repo",
                    scope_value="R1", conn=conn)
    assert len(ac.pending_messages("b", repo="R1", conn=conn)) == 1
    assert ac.pending_messages("z", repo="R2", conn=conn) == []


def test_a_broadcast_does_not_come_back_to_its_sender(conn):
    _reg(conn, "a", repo="R1"); _reg(conn, "b", repo="R1")
    ac.send_message("moving", frm="a", scope_kind="repo", scope_value="R1",
                    conn=conn)
    assert ac.pending_messages("a", repo="R1", conn=conn) == []


def test_a_machine_broadcast_crosses_repos(conn):
    _reg(conn, "a", repo="R1"); _reg(conn, "b", repo="R2")
    ac.send_message("drive migration", frm="a", scope_kind="machine", conn=conn)
    assert len(ac.pending_messages("b", repo="R2", conn=conn)) == 1


def test_delivery_is_recorded_and_not_repeated(conn):
    """§5.3: without this a sender cannot tell an ignored message from an
    undelivered one."""
    _reg(conn, "a"); _reg(conn, "b")
    mid, _ = ac.send_message("stop", frm="a", to="b", conn=conn)
    assert ac.message_status(mid, conn=conn) == []
    ac.mark_delivered(mid, "b", conn=conn)
    assert [i for i, _ in ac.message_status(mid, conn=conn)] == ["b"]
    assert ac.pending_messages("b", conn=conn) == []


def test_marking_delivered_twice_is_harmless(conn):
    _reg(conn, "a"); _reg(conn, "b")
    mid, _ = ac.send_message("x", frm="a", to="b", conn=conn)
    ac.mark_delivered(mid, "b", conn=conn)
    ac.mark_delivered(mid, "b", conn=conn)
    assert len(ac.message_status(mid, conn=conn)) == 1


def test_a_broadcast_tracks_each_recipient_separately(conn):
    _reg(conn, "a", repo="R"); _reg(conn, "b", repo="R"); _reg(conn, "c", repo="R")
    mid, _ = ac.send_message("all stop", frm="a", scope_kind="repo",
                             scope_value="R", conn=conn)
    ac.mark_delivered(mid, "b", conn=conn)
    assert [i for i, _ in ac.message_status(mid, conn=conn)] == ["b"]
    assert len(ac.pending_messages("c", repo="R", conn=conn)) == 1, (
        "one recipient's delivery consumed another's copy")


def test_the_sender_learns_whether_the_target_was_live(conn):
    """§5.3 asks for this explicitly."""
    _reg(conn, "a")
    _reg(conn, "gone", now=time.time() - 10 * ac.AGENT_TTL)
    _, live = ac.send_message("x", frm="a", to="gone", conn=conn)
    assert live is False
    _reg(conn, "here")
    _, live2 = ac.send_message("x", frm="a", to="here", conn=conn)
    assert live2 is True


def test_a_message_waits_for_an_agent_that_is_restarting(conn):
    """§5.3: queued for a non-live identity rather than lost."""
    _reg(conn, "a")
    ac.send_message("welcome back", frm="a", to="b", conn=conn)
    _reg(conn, "b")                      # b starts up afterwards
    assert [m.body for m in ac.pending_messages("b", conn=conn)] == [
        "welcome back"]


def test_a_message_expires(conn):
    """...but not forever: a week-old 'stop now' delivered to a session with no
    idea what it refers to is worse than losing it."""
    _reg(conn, "a"); _reg(conn, "b")
    ac.send_message("stale", frm="a", to="b", conn=conn,
                    now=time.time() - 2 * ac.MESSAGE_TTL)
    assert ac.pending_messages("b", conn=conn) == []


def test_an_unknown_scope_is_refused(conn):
    with pytest.raises(ac.AgentCommsError):
        ac.send_message("x", frm="a", scope_kind="galaxy", conn=conn)


def test_a_direct_message_needs_a_target(conn):
    with pytest.raises(ac.AgentCommsError):
        ac.send_message("x", frm="a", conn=conn)


# ---------------------------------------------------------------------------
# §6 -- halt
# ---------------------------------------------------------------------------

def test_nothing_pending_is_not_halted(conn):
    assert ac.halt_in_force(repo="R", conn=conn) is False


def test_a_message_alone_is_not_a_halt(conn):
    """§6.3 names this case specifically. Conflating the two would train every
    caller to ignore the exit code."""
    _reg(conn, "a", repo="R"); _reg(conn, "b", repo="R")
    ac.send_message("just so you know", frm="a", scope_kind="repo",
                    scope_value="R", conn=conn)
    assert ac.pending_messages("b", repo="R", conn=conn), "fixture is vacuous"
    assert ac.halt_in_force(repo="R", conn=conn) is False


def test_a_halt_is_in_force_for_its_repo_only(conn):
    ac.raise_halt("migrating", by="a", scope_kind="repo", scope_value="R1",
                  conn=conn)
    assert ac.halt_in_force(repo="R1", conn=conn) is True
    assert ac.halt_in_force(repo="R2", conn=conn) is False


def test_a_machine_halt_covers_every_repo(conn):
    """§7.4: a drive migration spans repositories."""
    ac.raise_halt("drive migration", by="a", scope_kind="machine", conn=conn)
    assert ac.halt_in_force(repo="R1", conn=conn) is True
    assert ac.halt_in_force(repo="R2", conn=conn) is True


def test_a_halt_carries_who_and_why(conn):
    ac.raise_halt("migrating to E:", by="lane-a", scope_kind="repo",
                  scope_value="R", conn=conn)
    h = ac.active_halt(repo="R", conn=conn)
    assert h is not None
    assert h.raised_by == "lane-a"
    assert h.reason == "migrating to E:"
    assert h.raised_at > 0


def test_lifting_is_explicit_and_reports_what_it_did(conn):
    hid = ac.raise_halt("x", by="a", scope_kind="repo", scope_value="R", conn=conn)
    assert ac.lift_halt(halt_id=hid, conn=conn) == 1
    assert ac.lift_halt(halt_id=hid, conn=conn) == 0
    assert ac.halt_in_force(repo="R", conn=conn) is False


def test_lifting_by_scope_works_without_knowing_the_id(conn):
    ac.raise_halt("x", by="a", scope_kind="repo", scope_value="R", conn=conn)
    assert ac.lift_halt(scope_kind="repo", scope_value="R", conn=conn) == 1
    assert ac.halt_in_force(repo="R", conn=conn) is False


def test_a_halt_never_expires_on_its_own(conn):
    """§6.4 is emphatic: one that timed out would clear itself in the middle of
    the maintenance it exists to protect."""
    ac.raise_halt("long migration", by="a", scope_kind="repo", scope_value="R",
                  conn=conn, now=time.time() - 30 * 24 * 3600)
    assert ac.halt_in_force(repo="R", conn=conn) is True, (
        "a month-old halt lifted itself")


def test_an_unknown_halt_scope_is_refused(conn):
    with pytest.raises(ac.AgentCommsError):
        ac.raise_halt("x", by="a", scope_kind="cwd", conn=conn)


def test_a_halt_is_announced_to_each_agent_once(conn):
    """It stays in force until lifted; re-announcing every turn is the noise
    §6 says agents learn to skip."""
    hid = ac.raise_halt("x", by="a", scope_kind="repo", scope_value="R", conn=conn)
    got = ac.pending_halt_for("b", repo="R", conn=conn)
    assert got is not None and got.id == hid
    ac.mark_halt_delivered(hid, "b", conn=conn)
    assert ac.pending_halt_for("b", repo="R", conn=conn) is None


def test_announcing_to_one_agent_does_not_announce_to_another(conn):
    hid = ac.raise_halt("x", by="a", scope_kind="repo", scope_value="R", conn=conn)
    ac.mark_halt_delivered(hid, "b", conn=conn)
    assert ac.pending_halt_for("c", repo="R", conn=conn) is not None


def test_a_halt_remains_in_force_after_being_announced(conn):
    """Announcement is the soft layer; the hard layer must not be consumed by
    it, or the first agent told would silently un-halt everyone else."""
    hid = ac.raise_halt("x", by="a", scope_kind="repo", scope_value="R", conn=conn)
    ac.mark_halt_delivered(hid, "b", conn=conn)
    assert ac.halt_in_force(repo="R", conn=conn) is True


# ---------------------------------------------------------------------------
# §6.3 -- the exit-code contract, through the CLI a script actually calls
# ---------------------------------------------------------------------------

CLI = ROOT / "tools" / "agents.py"


def _cli(*args, env_extra=None, cwd=None):
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(CLI), *args],
                          capture_output=True, text=True, timeout=180,
                          cwd=str(cwd or ROOT), env=env)


def test_check_halt_exits_zero_when_clear():
    assert _cli("check-halt", "--scope", "repo").returncode == 0


def test_check_halt_exits_nonzero_when_halted():
    _cli("halt", "maintenance", "--scope", "repo", env_extra={"ORCH2_AGENT_NAME": "op"})
    assert _cli("check-halt", "--scope", "repo").returncode != 0


def test_check_halt_exits_zero_with_a_message_but_no_halt():
    """The direction a naive implementation gets wrong."""
    _cli("broadcast", "fyi rebasing later", "--scope", "repo",
         env_extra={"ORCH2_AGENT_NAME": "op"})
    assert _cli("check-halt", "--scope", "repo").returncode == 0


def test_check_halt_recovers_after_a_lift():
    _cli("halt", "x", "--scope", "repo", env_extra={"ORCH2_AGENT_NAME": "op"})
    assert _cli("check-halt", "--scope", "repo").returncode != 0
    _cli("lift", "--scope", "repo")
    assert _cli("check-halt", "--scope", "repo").returncode == 0


def test_the_refusal_explains_itself_on_stderr():
    """An operation refusing should not look like a crash."""
    _cli("halt", "migrating the tree", "--scope", "repo",
         env_extra={"ORCH2_AGENT_NAME": "lane-a"})
    p = _cli("check-halt", "--scope", "repo")
    assert "HALT IN FORCE" in p.stderr
    assert "lane-a" in p.stderr and "migrating the tree" in p.stderr
    assert "not a failure" in p.stderr.lower()


def test_the_cli_self_test_passes():
    """The tool ships its own fixtures (§6.3); a broken one must fail here too
    rather than only when someone thinks to run it by hand."""
    p = _cli("--self-test")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "self-test passed" in p.stdout


def test_the_self_test_covers_both_directions():
    """Guard against the suite being quietly gutted: a self-test asserting only
    that halts halt would pass for a checker that reports everything."""
    p = _cli("--self-test")
    assert "a notice present, no halt: NOT halted" in p.stdout
    assert "a halt in force IS halted" in p.stdout


# ---------------------------------------------------------------------------
# §5.1 / §6.1 -- delivery, which is the part the spec says a file-based
# scheme cannot provide and the reason to build this in the client
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402

from config import parse_args                    # noqa: E402
from sdk_bridge import SDKBridge                  # noqa: E402
from state import init_state_from_config          # noqa: E402


def _bridge(tmp_path, name, *, sid="s1", account="acct"):
    cfg = parse_args(["--agent-name", name, "--cwd", str(tmp_path),
                      "--config-dir", str(tmp_path / account)])
    st = init_state_from_config(cfg)
    st.session_id = sid
    sent: list[dict] = []

    async def bcast(m):
        sent.append(m)

    br = SDKBridge(config=cfg, state=st, broadcaster=bcast)
    return br, st, sent


def _poll(br):
    """Force a poll regardless of the internal rate limit."""
    br._agent_poll_at = 0.0
    br._agent_hb_at = 0.0
    asyncio.run(br.poll_agent_comms())


def test_a_bridge_registers_itself(tmp_path):
    br, _st, _ = _bridge(tmp_path, "lane-a")
    asyncio.run(br.register_agent())
    assert br.agent_identity == "lane-a"
    assert [a.identity for a in ac.live_agents()] == ["lane-a"]


def test_two_bridges_under_different_accounts_are_both_visible(tmp_path):
    """Acceptance §8.1, end to end through the bridge."""
    a, _, _ = _bridge(tmp_path, "lane-a", sid="sa", account="acct-a")
    b, _, _ = _bridge(tmp_path, "lane-b", sid="sb", account="acct-b")
    asyncio.run(a.register_agent())
    asyncio.run(b.register_agent())
    assert sorted(x.identity for x in ac.live_agents()) == ["lane-a", "lane-b"]


def test_a_message_arrives_as_a_queued_prompt(tmp_path):
    """Acceptance §8.2. Queued, not injected: that is what makes 'at turn
    boundary, not mid-turn' true, since a queued prompt is only ever drained
    between turns."""
    a, _, _ = _bridge(tmp_path, "lane-a", sid="sa", account="acct-a")
    b, sb, _ = _bridge(tmp_path, "lane-b", sid="sb", account="acct-b")
    asyncio.run(a.register_agent())
    asyncio.run(b.register_agent())
    ac.send_message("stop at your next clean point", frm="lane-a", to="lane-b")
    _poll(b)
    assert len(sb.queued_prompts) == 1
    assert "stop at your next clean point" in sb.queued_prompts[0]
    assert "lane-a" in sb.queued_prompts[0], "the sender is not named"


def test_the_sender_is_not_delivered_its_own_broadcast(tmp_path):
    a, sa, _ = _bridge(tmp_path, "lane-a", sid="sa", account="acct-a")
    b, _sb, _ = _bridge(tmp_path, "lane-b", sid="sb", account="acct-b")
    asyncio.run(a.register_agent())
    asyncio.run(b.register_agent())
    ac.send_message("tree is moving", frm="lane-a", scope_kind="repo",
                    scope_value=a._agent_repo)
    _poll(a)
    assert list(sa.queued_prompts) == []


def test_a_halt_arrives_and_is_not_re_announced(tmp_path):
    """Acceptance §8.3 (soft half). Re-announcing every poll is the noise §6
    says agents learn to skip."""
    a, _, _ = _bridge(tmp_path, "lane-a", sid="sa", account="acct-a")
    b, sb, _ = _bridge(tmp_path, "lane-b", sid="sb", account="acct-b")
    asyncio.run(a.register_agent())
    asyncio.run(b.register_agent())
    ac.raise_halt("migrating the tree", by="lane-a", scope_kind="repo",
                  scope_value=b._agent_repo)
    _poll(b)
    assert len(sb.queued_prompts) == 1
    assert "HALT" in sb.queued_prompts[0]
    assert "migrating the tree" in sb.queued_prompts[0]
    _poll(b)
    assert len(sb.queued_prompts) == 1, "the halt was announced twice"


def test_a_delivered_halt_is_still_in_force(tmp_path):
    """The soft layer must not consume the hard one, or the first agent told
    would silently un-halt everybody else."""
    a, _, _ = _bridge(tmp_path, "lane-a", sid="sa", account="acct-a")
    asyncio.run(a.register_agent())
    ac.raise_halt("x", by="op", scope_kind="repo", scope_value=a._agent_repo)
    _poll(a)
    assert ac.halt_in_force(repo=a._agent_repo) is True


def test_deregistering_leaves_the_registry(tmp_path):
    br, _, _ = _bridge(tmp_path, "lane-a")
    asyncio.run(br.register_agent())
    asyncio.run(br.deregister_agent())
    assert ac.live_agents() == []
    assert br.agent_identity is None


def test_an_ambiguous_identity_is_reported_and_the_session_still_runs(tmp_path):
    """A refusal must not be fatal: the session runs, it is simply not
    addressable, and the user is told why."""
    dead = time.time() - 10 * ac.AGENT_TTL
    ac.register("lane-a", cwd=str(tmp_path), session_id="old-a", now=dead)
    ac.register("lane-b", cwd=str(tmp_path), session_id="old-b", now=dead)
    cfg = parse_args(["--cwd", str(tmp_path)])
    st = init_state_from_config(cfg)
    st.session_id = "brand-new"
    sent: list[dict] = []

    async def bcast(m):
        sent.append(m)

    br = SDKBridge(config=cfg, state=st, broadcaster=bcast)
    asyncio.run(br.register_agent())
    assert br.agent_identity is None, "guessed instead of refusing"
    warned = [m for m in sent if m.get("subtype") == "warning"]
    assert warned, "refused silently"
    body = warned[0]["data"]["message"]
    assert "lane-a" in body and "lane-b" in body
    assert "running normally" in body, "reads like a crash rather than a refusal"


def test_polling_without_an_identity_is_a_no_op(tmp_path):
    """Everything degrades to nothing when the registry is unavailable or the
    identity was refused -- a coordination convenience must never be why a
    session stops working."""
    cfg = parse_args(["--cwd", str(tmp_path)])
    st = init_state_from_config(cfg)
    br = SDKBridge(config=cfg, state=st, broadcaster=lambda m: None)
    br.agent_identity = None
    asyncio.run(br.poll_agent_comms())          # must not raise
    assert list(st.queued_prompts) == []


def test_an_unreachable_registry_does_not_break_the_session(tmp_path, monkeypatch):
    br, st, _ = _bridge(tmp_path, "lane-a")
    asyncio.run(br.register_agent())

    def boom(*a, **k):
        raise OSError("registry is gone")

    monkeypatch.setattr(ac, "connect", boom)
    _poll(br)                                    # must not raise
    assert list(st.queued_prompts) == []
