"""The bridge's half of cross-account agent comms: a session registers itself,
and what is pending for it -- a message, a halt -- arrives as a queued prompt at
its next turn boundary.

The store, identity, messaging, halts and the command line are the agent-comms
package's (its docs/spec.md, whose section numbers are used below), and are
tested there.
"""

from __future__ import annotations

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
    monkeypatch.setenv(ac.DB_ENV, str(tmp_path / "agents.db"))
    monkeypatch.delenv("ORCH2_AGENT_NAME", raising=False)
    yield


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
