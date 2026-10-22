"""A halt nobody is left to lift must be recognisable as one: orchestrator2's half.

Nothing lifts a halt but an explicit act (spec §6.4).  So a halt whose raiser
has gone -- finished, or moved to another machine along with the project it
protected -- stays up, and every new session on the machine is told to stop,
until somebody notices.  The prompt that says how old the halt is, and that
its raiser is gone, is agent-comms' (``agent_comms.halt_prompt``, tested
there).  What is left here: that the prompt a session queues carries it, and
that the lobby shows **every halt in force**, with a **Lift** button.
"""

from __future__ import annotations

import asyncio
import json
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
GONE = "not registered as running on this machine"


def _bridge_and_poll(tmp_path):
    """A registered bridge for ``lane-b``, and a function that polls it once.
    Returns ``(state, poll)``."""
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

    def poll():
        br._agent_poll_at = 0.0
        asyncio.run(br.poll_agent_comms())
    poll.bridge = br
    return st, poll


def test_a_delivered_halt_prompt_carries_the_raisers_absence(tmp_path):
    """End to end through the bridge's poll: the registry's answer reaches the
    prompt the session queues."""
    st, poll = _bridge_and_poll(tmp_path)
    ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine",
                  now=time.time() - 4 * DAY)
    poll()
    assert len(st.queued_prompts) == 1
    text = st.queued_prompts[0]
    assert "4 days ago" in text, text
    assert GONE in text, text


def test_a_conversation_told_elsewhere_is_not_told_again(tmp_path):
    """agent-comms' hook told the session, in a terminal, before orchestrator2
    resumed it: one conversation, one telling."""
    st, poll = _bridge_and_poll(tmp_path)
    hid = ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine")
    ac.mark_halt_delivered(hid, ac.session_key("sb"))      # as the hook records it
    poll()
    assert list(st.queued_prompts) == []


def test_a_halt_the_bridge_told_is_recorded_under_the_conversation(tmp_path):
    """So the session, resumed in a terminal, is not told again by the hook."""
    st, poll = _bridge_and_poll(tmp_path)
    ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine")
    poll()
    assert len(st.queued_prompts) == 1
    assert ac.pending_halt_for(ac.session_key("sb")) is None, "the hook would tell it again"


def _clear(br):
    """/clear, with no CLI to stop or start."""
    async def nothing(*_a, **_k):
        pass
    br.disconnect = nothing
    br.connect = nothing
    asyncio.run(br._clear_context())


def test_a_cleared_conversation_is_told_again(tmp_path):
    """A /clear leaves a context that has never seen the halt."""
    st, poll = _bridge_and_poll(tmp_path)
    ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine")
    poll()
    st.queued_prompts.clear()
    _clear(poll.bridge)
    poll()
    assert len(st.queued_prompts) == 1, "the new context was never told"


def test_a_conversation_cleared_before_its_first_id_is_told_again(tmp_path):
    """Told while new, under a key of its own, then cleared before any turn:
    the conversation after it must not inherit that key."""
    st, poll = _bridge_and_poll(tmp_path)
    st.session_id = None
    ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine")
    poll()
    st.queued_prompts.clear()
    _clear(poll.bridge)
    poll()
    assert len(st.queued_prompts) == 1, "the new conversation took the old one's key"


def test_a_conversation_told_before_its_first_session_id_keeps_it(tmp_path):
    """Told while new and idle, under a key of its own; its first turn gives it
    a session id, under which it must count as told."""
    st, poll = _bridge_and_poll(tmp_path)
    st.session_id = None
    ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine")
    poll()
    assert len(st.queued_prompts) == 1
    st.queued_prompts.clear()
    poll.bridge._take_session_id("sb-first")
    poll()
    assert list(st.queued_prompts) == [], "told again once it had an id"
    assert ac.pending_halt_for(ac.session_key("sb-first")) is None


@pytest.mark.parametrize("ends", ["clear", "another CLI session"])
def test_a_halt_queued_for_a_conversation_that_ended_is_not_told_twice(tmp_path, ends):
    """Queued, not yet delivered, when the conversation ended: delivered to
    the next one, which is then told under its own key, it would be twice."""
    st, poll = _bridge_and_poll(tmp_path)
    ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine")
    st.queued_prompts.append("carry on with the build")
    poll()
    assert len(st.queued_prompts) == 2
    if ends == "clear":
        _clear(poll.bridge)
    else:
        poll.bridge._take_session_id("sb-other")
    poll()
    halts = [p for p in st.queued_prompts if p.startswith(ac.HALT_PROMPT_PREFIX)]
    assert len(halts) == 1, st.queued_prompts
    assert "carry on with the build" in st.queued_prompts, "a prompt the operator queued went too"


def test_a_poll_that_comes_back_after_the_conversation_ended_queues_no_halt(tmp_path):
    """Asked for one conversation, answered after a /clear: its halt is not
    the next one's."""
    st, poll = _bridge_and_poll(tmp_path)
    br = poll.bridge
    ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine")
    real = br._collect_agent_comms

    def cleared_meanwhile(key, carries):
        out = real(key, carries)
        br._conversation_epoch += 1         # what a /clear does while it is out
        return out
    br._collect_agent_comms = cleared_meanwhile
    poll()
    assert list(st.queued_prompts) == []


def test_a_carry_is_made_at_the_next_tick_not_the_next_poll(tmp_path):
    """Waiting out the poll interval, a restart in it would lose the carry."""
    st, poll = _bridge_and_poll(tmp_path)
    br = poll.bridge
    st.session_id = None
    ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine")
    poll()
    br._take_session_id("sb-first")
    br._agent_poll_at = time.monotonic()                # it has just polled
    asyncio.run(br.poll_agent_comms())
    assert ac.pending_halt_for(ac.session_key("sb-first")) is None


def test_a_trimmed_conversation_is_not_told_again(tmp_path, monkeypatch):
    """A context trim starts a new transcript for the same conversation: run
    as the bridge runs it, between turns."""
    import dataclasses

    import sdk_bridge
    st, poll = _bridge_and_poll(tmp_path)
    br = poll.bridge
    ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine")
    poll()
    st.queued_prompts.clear()

    async def nothing(*_a, **_k):
        return None
    monkeypatch.setattr(sdk_bridge, "trim_session", lambda sid, proj, max_ctx: "sb-trimmed")
    br.config = dataclasses.replace(br.config, max_context_tokens=100)
    st.context_tokens = 500
    br.reconnect = nothing
    br._maybe_recycle_cli = nothing
    br._await_next_prompt = nothing
    asyncio.run(br._between_turns("done", False))
    assert st.session_id == "sb-trimmed", "no trim ran"
    poll()
    assert list(st.queued_prompts) == []


def test_a_new_cli_session_is_told_again(tmp_path):
    """The CLI started another session than the one the bridge ran: a new
    context, which has not been told."""
    st, poll = _bridge_and_poll(tmp_path)
    ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine")
    poll()
    st.queued_prompts.clear()
    poll.bridge._take_session_id("sb-other")
    poll()
    assert len(st.queued_prompts) == 1


def test_a_failed_raiser_lookup_leaves_the_halt_in_the_banner(monkeypatch):
    """The banner exists to show the halts: an unknown raiser is said as
    unknown, not taken as a reason to show none."""
    ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine")

    def boom(*_a, **_k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(ac, "raiser_running", boom)
    halts = server._halts_payload()
    assert [h["reason"] for h in halts] == ["PC migration"]
    assert halts[0]["raiser_running"] is None


def test_a_halt_this_agent_raised_is_not_told_to_it(tmp_path):
    st, poll = _bridge_and_poll(tmp_path)
    ac.raise_halt("moving the tree", by="lane-b", scope_kind="machine")
    poll()
    assert list(st.queued_prompts) == []


def test_a_message_that_fails_does_not_lose_the_halt_told_before_it(tmp_path, monkeypatch):
    """The halt is recorded as told when it is taken: dropped with the poll,
    it would never reach the session."""
    st, poll = _bridge_and_poll(tmp_path)
    ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine")

    def locked(*_a, **_k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(ac, "pending_messages", locked)
    poll()
    assert len(st.queued_prompts) == 1 and "PC migration" in st.queued_prompts[0]


def test_the_halt_server_finds_the_agent_at_the_next_tick_after_a_new_id(tmp_path):
    """The registration carries the session id, by which agent-comms' server
    finds whose halt it raises: waiting for the next heartbeat would sign one
    raised meanwhile unknown-agent."""
    st, poll = _bridge_and_poll(tmp_path)
    poll()                                                 # a heartbeat, now
    poll.bridge._take_session_id("sb-after-clear")
    poll()
    assert ac.identity_for_session("sb-after-clear") == "lane-b"


def test_a_failed_raiser_lookup_still_tells_the_halt(tmp_path, monkeypatch):
    st, poll = _bridge_and_poll(tmp_path)
    ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine")

    def boom(*_a, **_k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(ac, "raiser_running", boom)
    poll()
    assert len(st.queued_prompts) == 1, "the halt was never told"
    assert "PC migration" in st.queued_prompts[0]
    assert GONE not in st.queued_prompts[0], "claimed what it could not know"


def test_a_prompt_that_fails_to_build_leaves_the_halt_to_tell(tmp_path,
                                                               monkeypatch):
    """Marking a halt delivered is what stops it being offered again, so it
    must come after everything that can fail."""
    st, poll = _bridge_and_poll(tmp_path)
    ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine")

    def boom(*_a, **_k):
        raise OSError("localtime")
    monkeypatch.setattr(ac, "halt_prompt", boom)
    poll()
    assert list(st.queued_prompts) == []
    assert ac.pending_halt_for(ac.session_key("sb")) is not None, \
        "marked delivered without being told: it is lost for good"


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
    f = ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine",
                      now=time.time() - 4 * DAY)
    halts = server._halts_payload()
    assert sorted((h["id"], h["raised_by"], h["raiser_running"])
                  for h in halts) == [(a, "lane-a", True), (f, "Lane-F", False)]
    migration = next(h for h in halts if h["id"] == f)
    assert migration["reason"] == "PC migration"
    assert migration["scope_kind"] == "machine"
    assert migration["age"] == "4 days ago", "the lobby would use its own clock"


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
    """Send the lobby's ``lift_halt``; return (handled, payloads built,
    payloads broadcast to the lobbies)."""
    built: list[dict] = []
    broadcast: list[dict] = []

    async def payload(include_recent=True):
        built.append({"type": "session_list", "n": len(built)})
        return built[-1]

    async def to_lobbies(msg):
        broadcast.append(msg)

    monkeypatch.setattr(server, "_session_list_payload", payload)
    monkeypatch.setattr(server, "lobby_broadcast", to_lobbies)
    handled = asyncio.run(server._handle_lobby_message(
        ws, {"type": "lift_halt", "id": raw_id}))
    return handled, built, broadcast


def test_lift_from_the_lobby_lifts_that_halt_and_no_other(monkeypatch):
    keep = ac.raise_halt("repair", by="lane-a", scope_kind="machine")
    gone = ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine")
    ws = _Ws()
    handled, built, broadcast = _lift(monkeypatch, ws, gone)
    assert handled is True, "not consumed: it would reach the chat"
    assert [h.id for h in ac.halts_in_force()] == [keep]
    assert broadcast == built, "the other lobbies were not shown the change"
    assert built[0] in ws.sent, "the tab that clicked was not shown the change"
    assert len(built) == 1, "built the session list twice"


def test_lift_from_the_lobby_refuses_a_bad_id(monkeypatch):
    ac.raise_halt("PC migration", by="Lane-F", scope_kind="machine")
    ws = _Ws()
    handled, _, _ = _lift(monkeypatch, ws, "everything")
    assert handled is True
    assert ac.halt_in_force() is True
    assert any(m.get("type") == "lobby_notice" for m in ws.sent), ws.sent
