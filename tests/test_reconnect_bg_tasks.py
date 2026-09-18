"""What a reconnect does to running background tasks.

Every reconnect kills the ``claude.exe`` subprocess and starts a new one with
``--resume <session>``.  The transcript survives that; the CLI's **task
registry does not** — it is in-memory (``AppStateStore``'s ``tasks: {}``) and
``--resume`` rehydrates the conversation, not the running tasks.  So a
reconnect while a background task is in flight loses the completion
notification and the ``TaskOutput`` handle.  The OS processes themselves keep
running (they are in no job object, and the CLI's shutdown path reaps no
children), which makes it a bookkeeping loss rather than a work loss — but a
silent one, and that is the part that bit.

``_maybe_recycle_cli`` knew all this and guarded itself.  Nothing else did.
There are eight callers of ``reconnect()``; the recycle was the only one that
checked, so ``/model``, ``/effort``, ``/thinking``, ``/connect``, the
transport-death recovery, the ``auto_reconnect``-after-a-failed-turn path and
the context trim all cleared a live ``background_tasks`` dict with a
``log.info`` nobody reads.

The consequence was worse than a lost handle.  ``_between_turns`` parks the
session in bg-wait *only* when ``state.background_tasks`` is non-empty, and it
leaves that park on the ``bg-all-done`` wakeup that ``_complete_bg_task``
sends when the dict drains.  Clear the dict out from under it and the park
never happens: an autonomous session waiting on background work drops straight
to idle, is never woken, and says nothing about it — while the processes it
spawned carry on unattended.

So the knowledge moved down into ``reconnect()`` itself, and the discretionary
callers learned to wait:

* **Any** reconnect that orphans tasks now names them to the user.
* ``/model``, ``/effort`` and ``/thinking`` are *deferred* — recorded and
  applied the moment the tasks drain (:meth:`SDKBridge._reconnect_or_defer`).
* ``/connect`` and a dead transport are **not** deferrable: there is nothing
  left to protect in the second case, and the first is the user overruling us
  on purpose, having been told the price.
* The context trim is skipped outright rather than deferred, because
  ``trim_session`` mints a new session id before the reconnect would happen.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sdk_bridge                                   # noqa: E402
from sdk_bridge import SDKBridge, _CONNECT_TRANSPORT_DEAD  # noqa: E402
from config import parse_args                       # noqa: E402
from state import init_state_from_config            # noqa: E402


class IdleClient:
    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass


def _bridge(argv=()):
    """A bridge whose reconnect is real but whose transport is not.

    ``disconnect``/``connect`` are stubbed so ``reconnect()`` runs its own body
    — the orphan warning is what we are testing, and stubbing ``reconnect``
    itself would test nothing.
    """
    cfg = parse_args(list(argv))
    state = init_state_from_config(cfg)
    sent: list[dict] = []

    async def bcast(msg: dict) -> None:
        sent.append(msg)

    br = SDKBridge(config=cfg, state=state, broadcaster=bcast)
    br.client = IdleClient()

    connects: list[str | None] = []

    async def fake_disconnect():
        pass

    async def fake_connect(resume_id=None):
        connects.append(resume_id)

    br.disconnect = fake_disconnect
    br.connect = fake_connect
    return br, state, sent, connects


def _messages(sent: list[dict]) -> str:
    return " | ".join(m.get("data", {}).get("message", "")
                      for m in sent if m.get("type") == "system_msg")


def _warnings(sent: list[dict]) -> str:
    return " | ".join(m.get("data", {}).get("message", "")
                      for m in sent
                      if m.get("type") == "system_msg"
                      and m.get("subtype") == "warning")


def _bg(state, **tasks):
    for tid, name in tasks.items():
        state.background_tasks[tid] = {"name": name, "seq": 1}


# ---------------------------------------------------------------------------
# The loss is announced
# ---------------------------------------------------------------------------

def test_orphaning_a_background_task_tells_the_user():
    """It used to be a ``log.info``, which is indistinguishable from silence."""
    br, state, sent, _ = _bridge()
    _bg(state, t1="render the report")
    asyncio.run(br.reconnect())
    assert "render the report" in _warnings(sent)


def test_the_warning_counts_them():
    br, state, sent, _ = _bridge()
    _bg(state, t1="alpha", t2="beta", t3="gamma")
    asyncio.run(br.reconnect())
    text = _warnings(sent)
    assert "3 background tasks" in text
    for name in ("alpha", "beta", "gamma"):
        assert name in text


def test_one_task_is_not_reported_in_the_plural():
    br, state, sent, _ = _bridge()
    _bg(state, t1="alpha")
    asyncio.run(br.reconnect())
    text = _warnings(sent)
    assert "1 background task orphaned" in text
    assert "tasks orphaned" not in text


def test_a_long_list_is_summarised_rather_than_dumped():
    """A status line is not a place for forty task names."""
    br, state, sent, _ = _bridge()
    for i in range(9):
        state.background_tasks[f"t{i}"] = {"name": f"job{i}"}
    asyncio.run(br.reconnect())
    text = _warnings(sent)
    assert "9 background tasks" in text
    assert "and 4 more" in text
    assert "job8" not in text, "the whole list was dumped after all"


def test_a_task_with_no_name_is_still_named():
    """A task we failed to parse is exactly the one worth telling the user
    about; falling back to an empty string would report ``, ,`` and lose it."""
    br, state, sent, _ = _bridge()
    state.background_tasks["abc123"] = {}
    state.background_tasks["def456"] = {"command": "npm run build"}
    asyncio.run(br.reconnect())
    text = _warnings(sent)
    assert "abc123" in text
    assert "npm run build" in text


def test_a_reconnect_with_nothing_running_says_nothing_about_tasks():
    """The warning has to stay rare enough to be read."""
    br, state, sent, _ = _bridge()
    asyncio.run(br.reconnect())
    assert _warnings(sent) == ""


def test_the_registry_is_actually_cleared():
    """Leaving the entries would park _between_turns forever on a wakeup that
    the dead CLI can no longer send."""
    br, state, sent, _ = _bridge()
    _bg(state, t1="alpha")
    state.completed_panel_bg["old"] = {"name": "done"}
    asyncio.run(br.reconnect())
    assert state.background_tasks == {}
    assert state.completed_panel_bg == {}


def test_the_reconnect_still_resumes_the_same_session():
    """The warning must not have displaced the thing reconnect() is for."""
    br, state, sent, connects = _bridge()
    state.session_id = "sess-abc"
    _bg(state, t1="alpha")
    asyncio.run(br.reconnect())
    assert connects == ["sess-abc"]


# ---------------------------------------------------------------------------
# Discretionary reconnects wait
# ---------------------------------------------------------------------------

def test_a_model_switch_waits_for_a_running_task():
    br, state, sent, connects = _bridge()
    _bg(state, t1="render the report")
    applied = asyncio.run(br._reconnect_or_defer("model → opus"))
    assert applied is False
    assert connects == [], "reconnected anyway"
    assert state.background_tasks, "the task was orphaned by a deferred switch"
    assert br._deferred_reconnect == "model → opus"


def test_the_user_is_told_it_is_waiting_and_how_to_override():
    """Silently not applying a /model is its own bug."""
    br, state, sent, _ = _bridge()
    _bg(state, t1="alpha")
    asyncio.run(br._reconnect_or_defer("model → opus"))
    text = _messages(sent)
    assert "model → opus" in text
    assert "/connect" in text, "no way out was offered"


def test_a_model_switch_with_nothing_running_applies_at_once():
    br, state, sent, connects = _bridge()
    state.session_id = "sess-abc"
    applied = asyncio.run(br._reconnect_or_defer("model → opus"))
    assert applied is True
    assert connects == ["sess-abc"]
    assert br._deferred_reconnect is None


# ---------------------------------------------------------------------------
# ...and land when the tasks drain
# ---------------------------------------------------------------------------

def test_the_deferred_switch_lands_once_the_tasks_finish():
    br, state, sent, connects = _bridge()
    _bg(state, t1="alpha")
    asyncio.run(br._reconnect_or_defer("model → opus"))
    state.background_tasks.clear()
    assert asyncio.run(br._flush_deferred_reconnect()) is True
    assert len(connects) == 1
    assert "applying model → opus" in _messages(sent)


def test_the_flush_does_nothing_while_the_tasks_are_still_running():
    br, state, sent, connects = _bridge()
    _bg(state, t1="alpha")
    asyncio.run(br._reconnect_or_defer("model → opus"))
    assert asyncio.run(br._flush_deferred_reconnect()) is False
    assert connects == []
    assert br._deferred_reconnect == "model → opus", "the request was dropped"


def test_the_flush_does_nothing_when_nothing_was_deferred():
    """Otherwise every quiescent point would reconnect."""
    br, state, sent, connects = _bridge()
    assert asyncio.run(br._flush_deferred_reconnect()) is False
    assert connects == []


def test_the_flush_waits_for_a_turn_to_end():
    br, state, sent, connects = _bridge()
    br._deferred_reconnect = "model → opus"
    br.turn_active.set()
    assert asyncio.run(br._flush_deferred_reconnect()) is False
    assert connects == []


def test_the_flush_does_not_reconnect_a_connection_in_progress():
    br, state, sent, connects = _bridge()
    br._deferred_reconnect = "model → opus"
    state.connecting = True
    assert asyncio.run(br._flush_deferred_reconnect()) is False
    assert connects == []


def test_any_reconnect_satisfies_a_deferred_one():
    """The new CLI is built from current state, so the pending switch has
    already applied; leaving the flag set would reconnect twice."""
    br, state, sent, connects = _bridge()
    br._deferred_reconnect = "model → opus"
    asyncio.run(br.reconnect())
    assert br._deferred_reconnect is None
    assert asyncio.run(br._flush_deferred_reconnect()) is False
    assert len(connects) == 1


# ---------------------------------------------------------------------------
# Where it plugs into the turn loop
# ---------------------------------------------------------------------------

_PARKED = "<parked>"


def _looped(argv=()):
    br, state, sent, connects = _bridge(argv)

    async def fake_await_next():
        return _PARKED

    async def fake_recycle(*, force: bool = False):
        return False

    br._await_next_prompt = fake_await_next
    br._maybe_recycle_cli = fake_recycle
    return br, state, sent, connects


def test_a_model_command_between_turns_is_deferred_not_applied():
    br, state, sent, connects = _looped()
    _bg(state, t1="alpha")
    br.event_queue.put_nowait(("model", "opus"))
    asyncio.run(br._between_turns("done", False))
    assert connects == []
    assert br._deferred_reconnect == "model → opus"


def test_the_session_still_parks_in_bg_wait_after_a_model_switch():
    """The bug in one assertion.

    ``_between_turns`` parks only while ``background_tasks`` is non-empty, and
    is woken by the ``bg-all-done`` that fires when it empties.  The old code
    emptied it *itself* during the reconnect, so the park was skipped, the
    wakeup never came, and an autonomous session went quietly idle with its
    background work still running.
    """
    br, state, sent, connects = _looped()
    _bg(state, t1="alpha")
    br.event_queue.put_nowait(("model", "opus"))
    result = asyncio.run(br._between_turns("done", False))
    assert result is _PARKED, "dropped out of bg-wait"
    assert state.background_tasks, "the task the session is waiting for vanished"


def test_an_explicit_connect_is_not_deferred():
    """The user asked for a reconnect knowing what it costs."""
    br, state, sent, connects = _looped()
    state.session_id = "sess-abc"
    _bg(state, t1="alpha")
    br.event_queue.put_nowait(("connect", ""))
    asyncio.run(br._between_turns("done", False))
    assert connects == ["sess-abc"]
    assert br._deferred_reconnect is None
    assert "alpha" in _warnings(sent), "orphaned without saying so"


def test_a_dead_transport_is_not_deferred():
    """There is no CLI left holding the registry, so waiting protects nothing."""
    br, state, sent, connects = _looped()
    _bg(state, t1="alpha")
    recovered: list[str] = []

    async def fake_recover(why: str) -> bool:
        recovered.append(why)
        return True

    br._recover_dead_transport = fake_recover
    br.event_queue.put_nowait(("connect", _CONNECT_TRANSPORT_DEAD))
    asyncio.run(br._between_turns("done", False))
    assert recovered, "a dead transport waited for a task it can no longer see"
    assert br._deferred_reconnect is None


def test_giving_up_on_a_dead_cli_still_orphans_and_announces():
    """The give-up branch is the only path through _recover_dead_transport that
    does *not* call reconnect(), so it is the only one that would leave the
    registry populated against a process that no longer exists — and then park
    in bg-wait for a wakeup nothing can send."""
    br, state, sent, connects = _bridge()
    _bg(state, t1="alpha")
    br._may_auto_reconnect = lambda: False

    async def no_stderr():
        pass

    br._drain_stderr = no_stderr
    br._log_stderr_tail = lambda why: None
    assert asyncio.run(br._recover_dead_transport("CLI exited")) is False
    assert connects == [], "reconnected after giving up"
    assert state.background_tasks == {}
    assert "alpha" in _warnings(sent)


def test_a_deferred_switch_lands_at_the_next_turn_boundary():
    """A task that drained mid-turn frees the switch as of that turn's end."""
    br, state, sent, connects = _looped()
    br._deferred_reconnect = "model → opus"
    asyncio.run(br._between_turns("done", False))
    assert len(connects) == 1
    assert br._deferred_reconnect is None


def test_the_deferred_switch_lands_before_the_next_queued_prompt_runs():
    """Applying it after would run one whole turn on the old model — which is
    the thing the user typed /model to stop."""
    br, state, sent, connects = _looped()
    br._deferred_reconnect = "model → opus"
    state.queued_prompts.append("next thing")
    assert asyncio.run(br._between_turns("done", False)) == "next thing"
    assert len(connects) == 1, "the prompt ran under the old settings"


def test_the_bg_all_done_wakeup_applies_the_deferred_switch():
    """The common case: the switch was typed while parked, so no turn boundary
    is coming.  ``bg-all-done`` is the only signal that the wait is over."""
    br, state, sent, connects = _bridge()
    br._deferred_reconnect = "model → opus"
    br.event_queue.put_nowait(("wakeup", "bg-all-done"))
    br.event_queue.put_nowait(("quit", ""))
    assert asyncio.run(br._await_next_prompt()) is None
    assert len(connects) == 1
    assert br._deferred_reconnect is None


def test_the_idle_model_command_defers_too():
    """``_apply_idle_config_command`` is the *more* dangerous site: it is
    reached from ``_await_next_prompt`` while the session is parked in bg-wait,
    so orphaning there strands the park directly."""
    br, state, sent, connects = _bridge()
    _bg(state, t1="alpha")
    assert asyncio.run(br._apply_idle_config_command("model", "opus")) is True
    assert connects == []
    assert state.background_tasks
    assert br._deferred_reconnect == "model → opus"


@pytest.mark.parametrize("kind,payload,reason", [
    ("model", "opus", "model → opus"),
    ("effort", "high", "effort → high"),
    ("thinking", "on", "thinking → on"),
])
def test_every_discretionary_command_defers(kind, payload, reason):
    """One of the three having been missed is how this class of bug persists."""
    br, state, sent, connects = _bridge()
    _bg(state, t1="alpha")
    assert asyncio.run(br._apply_idle_config_command(kind, payload)) is True
    assert connects == []
    assert br._deferred_reconnect == reason


@pytest.mark.parametrize("kind,payload", [
    ("model", "opus"), ("effort", "high"), ("thinking", "on"),
])
def test_every_discretionary_command_still_applies_when_idle(kind, payload):
    """Deferring must not have turned into never applying."""
    br, state, sent, connects = _bridge()
    state.session_id = "sess-abc"
    asyncio.run(br._apply_idle_config_command(kind, payload))
    assert connects == ["sess-abc"]


def test_an_idle_connect_overrules_the_wait():
    br, state, sent, connects = _bridge()
    state.session_id = "sess-abc"
    _bg(state, t1="alpha")
    assert asyncio.run(br._apply_idle_config_command("connect", "")) is True
    assert connects == ["sess-abc"]
    assert "alpha" in _warnings(sent)


# ---------------------------------------------------------------------------
# The context trim
# ---------------------------------------------------------------------------

def _trim_bridge(monkeypatch, bg: bool):
    br, state, sent, connects = _looped(["--max-context-tokens", "100"])
    state.session_id = "sess-old"
    state.context_tokens = 500
    if bg:
        _bg(state, t1="alpha")
    trims: list[str] = []

    def fake_trim(sid, proj, max_ctx):
        trims.append(sid)
        return "sess-new"

    monkeypatch.setattr(sdk_bridge, "trim_session", fake_trim)
    return br, state, sent, connects, trims


def test_the_context_trim_is_skipped_while_tasks_run(monkeypatch):
    """It cannot be *deferred*: trim_session mints the new session id before
    the reconnect would happen, so a postponed reconnect would leave
    state.session_id pointing at a session the live CLI is not resumed on."""
    br, state, sent, connects, trims = _trim_bridge(monkeypatch, bg=True)
    asyncio.run(br._between_turns("done", False))
    assert trims == [], "trimmed under a running task"
    assert state.session_id == "sess-old"
    assert connects == []
    assert state.background_tasks


def test_the_context_trim_still_happens_when_nothing_is_running(monkeypatch):
    """The skip must not have disabled the rolling window outright."""
    br, state, sent, connects, trims = _trim_bridge(monkeypatch, bg=False)
    asyncio.run(br._between_turns("done", False))
    assert trims == ["sess-old"]
    assert state.session_id == "sess-new"
    assert connects == ["sess-new"]


# ---------------------------------------------------------------------------
# /clear
# ---------------------------------------------------------------------------

def test_clear_names_what_it_discarded():
    """``/clear`` wipes the registry along with everything else — which is what
    it means — but the bulk ``background_tasks.clear()`` says nothing."""
    br, state, sent, connects = _bridge()
    _bg(state, t1="render the report")
    asyncio.run(br._clear_context())
    assert "render the report" in _warnings(sent)
    assert state.background_tasks == {}


def test_a_deferred_switch_does_not_survive_a_clear():
    """The connect at the bottom of _clear_context already applies it; leaving
    the flag set would reconnect a second time on the next wakeup."""
    br, state, sent, connects = _bridge()
    br._deferred_reconnect = "model → opus"
    asyncio.run(br._clear_context())
    assert br._deferred_reconnect is None
