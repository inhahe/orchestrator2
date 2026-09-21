"""The autonomous wakeup loop must be stoppable -- by the agent and by you.

Reported 2026-09-06, relayed from a SlateOS lane:

    "No cron job exists and I've stopped the dynamic loop twice, so these
    wakeups are coming from outside my control -- you may need to end the
    /loop on your end."

It was right on both counts, and there were two independent causes.

**1. ``ScheduleWakeup(stop=true)`` armed a wakeup instead of ending one.**
``_maybe_arm_wakeup`` read ``delaySeconds`` and ``prompt`` and never looked at
``stop``.  The tool's documented way out is "call this tool with ``stop: true``
(omit every other field)" -- so with every other field omitted, the delay fell
back to its 60 s default and the empty prompt resolved to the autonomous-loop
text.  **Asking to stop armed a fresh 60 s loop.**  Stopping it twice armed it
twice, which is exactly what the report describes.

**2. The mid-turn deferral re-armed itself forever.**  When a wakeup fired
while a turn was running it called ``_arm_wakeup(WAKEUP_MIN_DELAY, prompt)``
unconditionally, so a session that stayed busy re-armed every 60 s
indefinitely.  The log shows the pair, once a minute::

    wakeup fired mid-turn - deferring 60s (turn still running)
    wakeup armed: delay=60s prompt='Autonomous-loop wakeup (scheduled by you)...'

A deferral exists so a wakeup landing mid-turn is not *lost*; it is not a
licence to outlive the loop it belongs to.  A working session is not a stalled
one, so it is now bounded.

**3. Nobody could see or stop it.**  The loop lives outside the conversation --
orchestrator2 runs the timer because the CLI ignores ``ScheduleWakeup`` in
streaming mode -- so the agent could not end it and the operator had no command
either.  Hence ``/loop``.  A background process that injects prompts into
someone's conversation has to be inspectable by the person whose conversation
it is.
"""

from __future__ import annotations

import asyncio
import os
import time
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from commands import classify                                  # noqa: E402
from config import (                                           # noqa: E402
    SLASH_COMMANDS, WAKEUP_MAX_DEFERS, WAKEUP_MAX_DELAY,
    WAKEUP_MIN_DELAY, WAKEUP_RESOLVED_PROMPT, parse_args,
)
from sdk_bridge import SDKBridge                               # noqa: E402
from state import (                                            # noqa: E402
    init_state_from_config,
    state_to_status_dict,
)


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH2_AGENT_DB", str(tmp_path / "agents.db"))
    yield


def _bridge(argv=()):
    cfg = parse_args(list(argv))
    st = init_state_from_config(cfg)
    sent: list[dict] = []

    async def bcast(m):
        sent.append(m)

    br = SDKBridge(config=cfg, state=st, broadcaster=bcast)
    return br, st, sent


@pytest.fixture
def bridge():
    br, st, sent = _bridge()
    yield br, st, sent
    # Teardown runs outside any event loop, and the timer task belongs to the
    # (now closed) loop the test used, so cancelling it can raise. Nothing here
    # outlives the test either way.
    try:
        br._cancel_wakeup()
    except RuntimeError:
        pass



async def _defer_once(br):
    """One mid-turn deferral, inside a live loop."""
    await br._wakeup_timer(0.0, "p")
    return br.loop_status()


def _in_loop(fn):
    """Run *fn* with a live event loop.

    ``_arm_wakeup`` schedules an ``asyncio`` task, so every call that arms (or
    re-arms) a wakeup needs a running loop.  Each test does its whole sequence
    inside one loop so a timer created in one is not cancelled from another.
    """
    async def go():
        return fn()
    return asyncio.run(go())


# ---------------------------------------------------------------------------
# 1. stop:true must stop
# ---------------------------------------------------------------------------

def test_scheduling_arms_a_wakeup(bridge):
    br, _st, _ = bridge
    _in_loop(lambda: br._maybe_arm_wakeup(
        "ScheduleWakeup", {"delaySeconds": 600, "prompt": "keep going"}))
    assert br.loop_status()["armed"] is True


def test_stop_true_ends_the_loop(bridge):
    """The reported bug. Previously this armed a fresh 60s wakeup."""
    br, _st, _ = bridge
    _in_loop(lambda: br._maybe_arm_wakeup("ScheduleWakeup", {"delaySeconds": 600, "prompt": "go"}))
    _in_loop(lambda: br._maybe_arm_wakeup("ScheduleWakeup", {"stop": True}))
    assert br.loop_status()["armed"] is False, (
        "stop=true left a wakeup armed -- the agent cannot end its own loop")


def test_stop_true_with_every_other_field_omitted(bridge):
    """The tool says to omit every other field, which is precisely the shape
    that used to fall through to the 60-second default."""
    br, _st, _ = bridge
    _in_loop(lambda: br._maybe_arm_wakeup("ScheduleWakeup", {"delaySeconds": 600, "prompt": "go"}))
    _in_loop(lambda: br._maybe_arm_wakeup("ScheduleWakeup", {"stop": True}))
    assert br.loop_status()["armed"] is False


def test_stopping_twice_does_not_arm_anything(bridge):
    """The report said the loop was stopped *twice*. Under the old code that
    armed it twice."""
    br, _st, _ = bridge
    _in_loop(lambda: br._maybe_arm_wakeup("ScheduleWakeup", {"delaySeconds": 600, "prompt": "go"}))
    _in_loop(lambda: br._maybe_arm_wakeup("ScheduleWakeup", {"stop": True}))
    _in_loop(lambda: br._maybe_arm_wakeup("ScheduleWakeup", {"stop": True}))
    assert br.loop_status()["armed"] is False


def test_stop_is_remembered_so_the_ui_can_say_stopped(bridge):
    """'Nothing armed' is what an operator sees both after stopping it and when
    it never existed; those are different situations."""
    br, _st, _ = bridge
    assert br.loop_status()["stopped"] is False
    _in_loop(lambda: br._maybe_arm_wakeup("ScheduleWakeup", {"stop": True}))
    assert br.loop_status()["stopped"] is True


def test_scheduling_again_after_a_stop_clears_the_stopped_flag(bridge):
    br, _st, _ = bridge
    _in_loop(lambda: br._maybe_arm_wakeup("ScheduleWakeup", {"stop": True}))
    _in_loop(lambda: br._maybe_arm_wakeup("ScheduleWakeup", {"delaySeconds": 120, "prompt": "x"}))
    st = br.loop_status()
    assert st["armed"] is True and st["stopped"] is False


@pytest.mark.parametrize("falsy", [False, 0, "", None])
def test_a_falsy_stop_still_schedules(bridge, falsy):
    """``stop: false`` is a normal schedule, not a stop."""
    br, _st, _ = bridge
    _in_loop(lambda: br._maybe_arm_wakeup(
        "ScheduleWakeup", {"stop": falsy, "delaySeconds": 300, "prompt": "x"}))
    assert br.loop_status()["armed"] is True


def test_a_disabled_wakeup_ignores_stop_too(tmp_path):
    """--no-wakeup means the whole mechanism is off; stop must not resurrect
    any bookkeeping."""
    br, _st, _ = _bridge(["--no-wakeup"])
    _in_loop(lambda: br._maybe_arm_wakeup("ScheduleWakeup", {"stop": True}))
    assert br.loop_status()["armed"] is False
    assert br.loop_status()["enabled"] is False


# ---------------------------------------------------------------------------
# 2. the mid-turn deferral is bounded
# ---------------------------------------------------------------------------

def test_a_deferral_is_bounded(bridge):
    """Unbounded, a busy session re-armed itself every 60s forever.

    The whole sequence runs in one event loop: each deferral cancels the
    previous timer and creates a new one, which cannot span loops.
    """
    br, st, _ = bridge
    st.busy = True

    async def go():
        # Exactly one call past the budget. Going further would be testing a
        # fiction: in production a dropped wakeup means no timer remains, so
        # _wakeup_timer is never entered again -- only this test can call it.
        for _ in range(WAKEUP_MAX_DEFERS + 1):
            await br._wakeup_timer(0.0, "p")
        return br.loop_status()

    status = asyncio.run(go())
    assert status["armed"] is False, (
        "the wakeup is still re-arming itself while the session is busy")


def test_a_dropped_loop_says_it_was_dropped(bridge):
    """Asked 2026-09-21 why a working session still showed a loop countdown.
    It is correct -- the model arms the wakeup mid-turn, via ScheduleWakeup --
    but it exposed the case next door: when the turn outlasts the loop's whole
    cadence the wakeup is *dropped*, and until now the only trace was a log
    line. The countdown simply vanished from the status bar.

    A loop that stops without saying it stopped is precisely the failure this
    week was spent removing from the idle teardown; reproducing it inside the
    loop's own code would be indefensible."""
    br, st, sent = bridge
    st.busy = True

    async def go():
        for _ in range(WAKEUP_MAX_DEFERS + 1):
            await br._wakeup_timer(0.0, "p")

    asyncio.run(go())
    notices = [m for m in sent if m.get("type") == "system_msg"]
    text = " ".join((m.get("data") or {}).get("message", "") for m in notices)

    assert "dropped" in text.lower(), (
        "the loop ended and the session was told nothing"
    )
    assert "/loop" in text, "it does not say how to start it again"


def test_a_failed_drop_notice_does_not_escape_the_timer():
    """The announce is the last thing the drop path does, so the timer state is
    already clean by then -- what a raising broadcaster would cost is an
    unhandled exception escaping a fire-and-forget task, where it becomes an
    asyncio warning nobody reads rather than anything actionable."""
    cfg = parse_args([])
    st = init_state_from_config(cfg)

    async def bcast(m):
        raise RuntimeError("socket went away")

    br = SDKBridge(config=cfg, state=st, broadcaster=bcast)
    st.busy = True

    async def go():
        for _ in range(WAKEUP_MAX_DEFERS + 1):
            await br._wakeup_timer(0.0, "p")

    asyncio.run(go())          # must not raise

    assert br.loop_status()["armed"] is False


def test_a_deferral_below_the_bound_says_nothing(bridge):
    """A wakeup merely pushed back is not news -- the status bar already shows
    the deferral count. Announcing each one would make ten messages out of a
    situation that resolves itself."""
    br, st, sent = bridge
    st.busy = True

    asyncio.run(_defer_once(br))
    notices = [m for m in sent if m.get("type") == "system_msg"]

    assert not notices, notices


def test_a_deferral_below_the_bound_keeps_the_wakeup(bridge):
    """The bound must not throw away a wakeup that merely landed mid-turn --
    that is the case the deferral exists for."""
    br, st, _ = bridge
    st.busy = True
    status = asyncio.run(_defer_once(br))
    assert status["armed"] is True
    assert status["defers"] == 1


def test_deferrals_do_not_reset_themselves(bridge):
    """Re-arming from the deferral is the *same* wakeup pushed back; resetting
    the counter there would make the bound unreachable."""
    br, st, _ = bridge
    st.busy = True

    async def go():
        for _ in range(3):
            await br._wakeup_timer(0.0, "p")
        return br.loop_status()

    assert asyncio.run(go())["defers"] == 3


def test_a_fresh_schedule_resets_the_defer_count(bridge):
    """A new loop starts with a full budget."""
    br, st, _ = bridge
    st.busy = True

    async def go():
        await br._wakeup_timer(0.0, "p")
        first = br.loop_status()["defers"]
        br._maybe_arm_wakeup("ScheduleWakeup",
                             {"delaySeconds": 300, "prompt": "x"})
        return first, br.loop_status()["defers"]

    first, after = asyncio.run(go())
    assert first == 1 and after == 0


# ---------------------------------------------------------------------------
# 3. /loop -- the operator surface
# ---------------------------------------------------------------------------

def test_the_command_parses():
    assert classify("/loop") == ("loop", "")
    assert classify("/loop off") == ("loop", "off")
    assert classify("/loop 300") == ("loop", "300")


def test_the_command_is_tab_completable():
    assert "/loop" in SLASH_COMMANDS


def test_stop_loop_cancels_and_reports(bridge):
    br, _st, _ = bridge
    _in_loop(lambda: br._arm_wakeup(600, "p"))
    assert br.stop_loop() is True
    assert br.loop_status()["armed"] is False
    assert br.stop_loop() is False, "reported stopping something twice"


def test_stop_loop_is_explicit_even_when_nothing_was_armed(bridge):
    br, _st, _ = bridge
    br.stop_loop()
    assert br.loop_status()["stopped"] is True


def test_start_loop_clamps_like_the_tool(bridge):
    br, _st, _ = bridge
    assert _in_loop(lambda: br.start_loop(1)) == WAKEUP_MIN_DELAY
    assert _in_loop(lambda: br.start_loop(999999)) == WAKEUP_MAX_DELAY
    assert _in_loop(lambda: br.start_loop(300)) == 300


def test_start_loop_defaults_to_the_autonomous_prompt(bridge, monkeypatch):
    br, _st, _ = bridge
    seen = {}
    monkeypatch.setattr(br, "_arm_wakeup",
                        lambda d, p, **k: seen.update(delay=d, prompt=p))
    br.start_loop(120)
    assert seen["prompt"] == WAKEUP_RESOLVED_PROMPT


def test_status_reports_the_time_remaining(bridge):
    br, _st, _ = bridge
    _in_loop(lambda: br._arm_wakeup(600, "p"))
    st = br.loop_status()
    assert st["armed"] is True
    assert 500 < st["seconds_left"] <= 600


def test_status_on_a_quiet_session_is_not_armed(bridge):
    br, _st, _ = bridge
    st = br.loop_status()
    assert st["armed"] is False and st["seconds_left"] is None


# ---------------------------------------------------------------------------
# The status bar can see it
# ---------------------------------------------------------------------------
#
# The loop injects prompts from outside the conversation.  `/loop` made it
# *askable*; the status bar makes it *visible*, which is the difference between
# noticing a scheduled wakeup and having to suspect one.

def test_arming_publishes_a_wall_clock_deadline(bridge):
    """Wall-clock, not the bridge's monotonic deadline: it crosses to a
    browser, which can only compare it against its own clock."""
    br, st, _ = bridge
    before = time.time()
    _in_loop(lambda: br._arm_wakeup(600, "p"))
    assert st.wakeup_at is not None
    assert before + 590 <= st.wakeup_at <= time.time() + 600


def test_a_quiet_session_publishes_no_deadline(bridge):
    br, st, _ = bridge
    assert st.wakeup_at is None


def test_cancelling_clears_the_deadline(bridge):
    """A countdown that outlives its wakeup is worse than none -- it says
    something is going to happen that is not."""
    br, st, _ = bridge
    _in_loop(lambda: br._arm_wakeup(600, "p"))
    _in_loop(br._cancel_wakeup)
    assert st.wakeup_at is None


def test_loop_off_clears_the_deadline(bridge):
    br, st, _ = bridge
    _in_loop(lambda: br._arm_wakeup(600, "p"))
    _in_loop(br.stop_loop)
    assert st.wakeup_at is None


def test_firing_clears_the_deadline(bridge):
    """It fired; there is no longer a wakeup pending, and the bar must not go
    on counting down to a moment that has passed."""
    br, st, _ = bridge

    async def go():
        br._arm_wakeup(600, "p")
        await br._wakeup_timer(0.0, "p")     # not busy -> fires

    asyncio.run(go())
    assert st.wakeup_at is None


def test_a_deferral_republishes_the_new_deadline(bridge):
    """The wakeup moved, so the countdown must move with it."""
    br, st, _ = bridge

    async def go():
        br.state.busy = True
        br._arm_wakeup(600, "p")
        first = st.wakeup_at
        await br._wakeup_timer(0.0, "p")     # busy -> defers, re-arms shorter
        return first, st.wakeup_at

    first, second = asyncio.run(go())
    assert second is not None and second < first, (first, second)
    assert st.wakeup_defers == 1


def test_a_dropped_wakeup_clears_the_deadline(bridge):
    """Past the deferral cap the wakeup is dropped, not fired. Leaving the
    countdown up would promise a prompt that is never coming."""
    br, st, _ = bridge

    async def go():
        br.state.busy = True
        br._arm_wakeup(600, "p")
        for _ in range(WAKEUP_MAX_DEFERS + 1):
            await br._wakeup_timer(0.0, "p")

    asyncio.run(go())
    assert st.wakeup_at is None
    assert st.wakeup_defers == 0


def test_the_status_dict_carries_the_deadline(bridge):
    br, st, _ = bridge
    _in_loop(lambda: br._arm_wakeup(600, "p"))
    d = state_to_status_dict(st, br.config)
    assert d["wakeup_at"] == st.wakeup_at
    assert d["wakeup_defers"] == 0


def _run_loop_cmd_ch(br, payload):
    """Run ``/loop <payload>``; return the text sent and *how* it was sent.

    The channel matters as much as the text: a change to what the session will
    do next has to reach every viewer, while an answer to one tab's question
    should not interrupt the others.
    """
    import server
    sent: list[dict] = []
    channels: list[str] = []

    class WS:
        pass

    async def fake_send_to(_ws, m):
        channels.append("send_to")
        sent.append(m)

    async def fake_broadcast(m):
        channels.append("broadcast")
        sent.append(m)

    orig_send, orig_bcast = server.send_to, server.broadcast
    server.send_to, server.broadcast = fake_send_to, fake_broadcast
    try:
        asyncio.run(server._handle_loop(WS(), br, payload))
    finally:
        server.send_to, server.broadcast = orig_send, orig_bcast
    text = " | ".join(m.get("data", {}).get("message", "") for m in sent)
    return text, channels


def _run_loop_cmd(br, payload):
    return _run_loop_cmd_ch(br, payload)[0]


def test_loop_shows_status(bridge):
    br, _st, _ = bridge
    _in_loop(lambda: br._arm_wakeup(600, "p"))
    out = _run_loop_cmd(br, "")
    assert "armed" in out and "fires in" in out
    assert "/loop off" in out, "does not say how to stop it"


def test_loop_off_stops_it(bridge):
    br, _st, _ = bridge
    _in_loop(lambda: br._arm_wakeup(600, "p"))
    out = _run_loop_cmd(br, "off")
    assert br.loop_status()["armed"] is False
    assert "stopped" in out.lower()


@pytest.mark.parametrize("word", ["off", "stop", "cancel", "end", "0"])
def test_the_obvious_words_all_stop_it(bridge, word):
    """An operator trying to stop a runaway loop should not have to guess the
    verb."""
    br, _st, _ = bridge
    _in_loop(lambda: br._arm_wakeup(600, "p"))
    _run_loop_cmd(br, word)
    assert br.loop_status()["armed"] is False


def test_zero_stops_rather_than_arming_the_minimum(bridge):
    """``/loop 0`` reads as "no loop", not as a duration.

    Taken as a duration it would be clamped up to the 60s floor -- so the one
    input that most plainly means *stop* would arm the fastest loop available.
    """
    br, _st, _ = bridge
    _in_loop(lambda: br._arm_wakeup(600, "p"))
    out = _run_loop_cmd(br, "0")
    assert br.loop_status()["armed"] is False
    assert "stopped" in out.lower()
    assert "armed" not in out.lower()


def test_loop_with_a_number_arms_it(bridge):
    br, _st, _ = bridge
    _run_loop_cmd(br, "300")
    st = br.loop_status()
    assert st["armed"] is True and 290 < st["seconds_left"] <= 300


def test_loop_says_when_it_clamped(bridge):
    br, _st, _ = bridge
    out = _run_loop_cmd(br, "5")
    assert "clamp" in out.lower(), "silently changed what the operator asked for"


def test_loop_rejects_nonsense_with_usage(bridge):
    br, _st, _ = bridge
    out = _run_loop_cmd(br, "sideways")
    assert "usage" in out.lower()
    assert br.loop_status()["armed"] is False


def test_status_is_answered_to_the_asker_alone(bridge):
    """A question is not an event.  Answering it to every tab would interrupt
    people who did not ask."""
    br, _st, _ = bridge
    _in_loop(lambda: br._arm_wakeup(600, "p"))
    _out, channels = _run_loop_cmd_ch(br, "")
    assert channels == ["send_to"]


def test_arming_is_broadcast_not_whispered(bridge):
    """Arming changes what the session will do next, so it reaches every
    viewer -- the same reason stopping does."""
    br, _st, _ = bridge
    _out, channels = _run_loop_cmd_ch(br, "on")
    assert "broadcast" in channels
    assert br.loop_status()["armed"] is True


def test_status_distinguishes_stopped_from_never_armed(bridge):
    """Both are "not armed", but only one of them is a decision.

    Without the distinction an operator who typed ``/loop off`` cannot tell
    whether it took effect, which is exactly the doubt the command exists to
    remove.
    """
    br, _st, _ = bridge
    fresh = _run_loop_cmd(br, "status")
    assert "stopped" not in fresh.lower()

    _in_loop(lambda: br._arm_wakeup(600, "p"))
    _run_loop_cmd(br, "off")
    after = _run_loop_cmd(br, "status")
    assert "stopped" in after.lower()


def test_status_says_so_when_the_loop_is_disabled_outright(bridge):
    """``--no-wakeup`` is not "not armed" -- no ``/loop`` argument will arm it,
    so saying "not armed" would invite the operator to keep trying."""
    import dataclasses
    br, _st, _ = bridge
    br.config = dataclasses.replace(br.config, wakeup_enabled=False)
    out = _run_loop_cmd(br, "status")
    assert "disabled" in out.lower()


def test_loop_off_is_broadcast_not_whispered(bridge):
    """Every viewer of the session should see that the loop was stopped -- it
    changes what the session will do next."""
    import server
    br, _st, _ = bridge
    _in_loop(lambda: br._arm_wakeup(600, "p"))
    seen: list[str] = []

    async def fake_broadcast(m):
        seen.append("broadcast")

    async def fake_send_to(_ws, m):
        seen.append("send_to")

    orig_send, orig_bcast = server.send_to, server.broadcast
    server.send_to, server.broadcast = fake_send_to, fake_broadcast
    try:
        asyncio.run(server._handle_loop(object(), br, "off"))
    finally:
        server.send_to, server.broadcast = orig_send, orig_bcast
    assert "broadcast" in seen
