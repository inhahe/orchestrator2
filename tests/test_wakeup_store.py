"""Scheduled wakeups that outlive the hub.

Reported 2026-09-20: two autonomous-loop sessions had stopped days earlier and
said nothing. Half of that was the idle timer reaping a session that was merely
*waiting* (fixed in ``server._idle_teardown_after``); the other half is that
``state.wakeup_at`` lived only in memory, so a hub restart cancelled every
schedule just as quietly.

Asked which a user would expect, the answer was "i guess my loops are mostly
unattended" — which settles it. Restoring a schedule only when someone next
*opens* the session would be safe and nearly useless, because the entire point
of those loops is running while nobody watches. So the hub resurrects them.

**That makes this the most dangerous decision in the codebase**, and these
tests are mostly about the restraints rather than the feature. A wakeup carries
a prompt that gets *sent as a real turn*, unattended. Persisting the prompt
queue once caused exactly that shape of harm: a brand-new session inherited an
older one's queued prompt and sent it immediately, with no user action, from an
18-day-old file still waiting to do it again.

The interesting rule is the **lateness budget**: a wakeup may fire late by at
most its own cadence (``due_at - armed_at``, bounded), because a loop that
ticks every twenty minutes has no business firing eight hours late on a plan
that has gone stale. Past that it is reported paused, not run.
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wakeup_store                                        # noqa: E402
from wakeup_store import (                                 # noqa: E402
    ARM,
    DISCARD,
    FIRE,
    MAX_LATE_S,
    MIN_LATE_S,
    PAUSED,
    clear_wakeup,
    iter_pending_wakeups,
    lateness_budget,
    plan_restore,
    save_wakeup,
    wakeup_file_for,
)

CWD = r"D:\proj"
SID = "1aa74fb0-ca4e-42cb-80c3-ef7302fee0a4"
NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def cfgdir(tmp_path, monkeypatch):
    """Keep every test's records out of the real config dir."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    return tmp_path


def _rec(**over):
    rec = {
        "cwd": CWD,
        "session_id": SID,
        "config_dir": None,
        "armed_at": NOW - 1200.0,     # a 20-minute loop
        "due_at": NOW,
        "prompt": "Autonomous-loop wakeup (scheduled by you). Resume work now.",
        "saved_at": NOW - 1200.0,
    }
    rec.update(over)
    return rec


# --------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------

def test_a_wakeup_still_in_the_future_is_re_armed_for_what_is_left():
    plan = plan_restore(_rec(due_at=NOW + 300.0), now=NOW)

    assert plan.action == ARM
    assert abs(plan.delay - 300.0) < 1.0


def test_a_slightly_late_wakeup_still_fires():
    """The common case this feature exists for: you restart the hub to pick up
    a fix, and thirty seconds later the loop carries on."""
    plan = plan_restore(_rec(due_at=NOW - 30.0), now=NOW)

    assert plan.action == FIRE


def test_a_badly_late_wakeup_is_paused_not_run():
    """Eight hours late, on a plan made before you went to bed. Firing that
    unattended is the outcome the whole path is written to avoid."""
    plan = plan_restore(_rec(due_at=NOW - 8 * 3600.0), now=NOW)

    assert plan.action == PAUSED


def test_the_lateness_budget_is_the_loops_own_cadence():
    """Derived, not configured: a fast loop tolerates little lateness and a
    slow one tolerates more, with no knob to get wrong."""
    fast = _rec(armed_at=NOW - 300.0, due_at=NOW)        # 5-minute loop
    slow = _rec(armed_at=NOW - 2700.0, due_at=NOW)       # 45-minute loop
    epic = _rec(armed_at=NOW - 6 * 3600.0, due_at=NOW)   # 6-hour loop

    assert lateness_budget(fast) == 300.0
    assert lateness_budget(slow) == 2700.0
    assert lateness_budget(epic) == MAX_LATE_S           # capped at an hour


def test_a_fast_loop_is_not_declared_stale_by_a_brief_restart():
    """A 30-second loop would otherwise have a 30-second budget, and a restart
    takes longer than that."""
    rec = _rec(armed_at=NOW - 30.0, due_at=NOW)

    assert lateness_budget(rec) == MIN_LATE_S
    assert plan_restore(_rec(armed_at=NOW - 30.0, due_at=NOW - 45.0),
                        now=NOW).action == FIRE


def test_a_very_slow_loop_cannot_fire_hours_late():
    """Six-hour cadence, six hours late, is still a stale plan."""
    rec = _rec(armed_at=NOW - 6 * 3600.0, due_at=NOW - 2 * 3600.0)

    assert lateness_budget(rec) == MAX_LATE_S
    assert plan_restore(rec, now=NOW).action == PAUSED


def test_the_budget_survives_a_nonsense_interval():
    """``armed_at`` after ``due_at`` means a corrupt or hand-edited record; it
    must not produce a negative budget that fires everything."""
    rec = _rec(armed_at=NOW + 500.0, due_at=NOW)

    assert lateness_budget(rec) == MIN_LATE_S


# --------------------------------------------------------------------------
# What is refused
# --------------------------------------------------------------------------

def test_a_stale_record_is_discarded():
    """A record older than the freshness cap is litter, not a schedule — the
    18-day-old queue file that re-ran a prompt is why this gate exists."""
    plan = plan_restore(_rec(saved_at=NOW - 48 * 3600.0), now=NOW)

    assert plan.action == DISCARD


@pytest.mark.parametrize("bad", [
    {"session_id": ""},
    {"prompt": ""},
    {"prompt": "   "},
    {"due_at": "soon"},
    {"saved_at": None},
    {"cwd": ""},
])
def test_an_unusable_record_is_discarded(bad):
    assert plan_restore(_rec(**bad), now=NOW).action == DISCARD


def test_a_non_dict_is_discarded():
    for junk in (None, [], "wakeup", 42):
        assert plan_restore(junk, now=NOW).action == DISCARD


# --------------------------------------------------------------------------
# On disk
# --------------------------------------------------------------------------

def test_a_wakeup_round_trips():
    save_wakeup(CWD, SID, due_at=NOW + 600, prompt="do the thing",
                armed_at=NOW)

    got = list(iter_pending_wakeups())

    assert [r["session_id"] for r in got] == [SID]
    assert got[0]["prompt"] == "do the thing"


def test_clearing_removes_it():
    """Every exit from the armed state erases the record. One that outlives
    its wakeup is a turn waiting to be run twice."""
    save_wakeup(CWD, SID, due_at=NOW + 600, prompt="p", armed_at=NOW)
    clear_wakeup(CWD, SID)

    assert list(iter_pending_wakeups()) == []


def test_clearing_something_that_was_never_saved_is_harmless():
    clear_wakeup(CWD, SID)


def test_a_session_with_no_id_is_never_saved():
    """Nothing to restore *into*, so the record could only ever be litter."""
    save_wakeup(CWD, None, due_at=NOW + 600, prompt="p", armed_at=NOW)

    assert list(iter_pending_wakeups()) == []


def test_an_empty_prompt_is_never_saved():
    """A wakeup with no prompt would wake a session to say nothing."""
    save_wakeup(CWD, SID, due_at=NOW + 600, prompt="", armed_at=NOW)

    assert list(iter_pending_wakeups()) == []


def test_a_record_in_the_wrong_slot_is_ignored():
    """The filename is derived from cwd+session id, so a record whose recorded
    id does not match its own slot has been moved or tampered with. Restoring
    it would run a turn in the wrong session — exactly the failure the prompt
    queue was re-keyed to prevent."""
    save_wakeup(CWD, SID, due_at=NOW + 600, prompt="p", armed_at=NOW)
    path = wakeup_file_for(CWD, SID)
    rec = json.loads(path.read_text(encoding="utf-8"))
    rec["session_id"] = "someone-elses-session"
    path.write_text(json.dumps(rec), encoding="utf-8")

    assert list(iter_pending_wakeups()) == []


def test_unreadable_records_do_not_stop_the_scan():
    """One corrupt file must not cost every other loop its schedule."""
    save_wakeup(CWD, SID, due_at=NOW + 600, prompt="good", armed_at=NOW)
    junk = wakeup_file_for(CWD, "aaaa-bbbb")
    junk.write_text("{not json", encoding="utf-8")

    assert [r["prompt"] for r in iter_pending_wakeups()] == ["good"]


def test_records_come_back_in_due_order():
    """The resurrection cap means order decides who comes back; soonest-due is
    the only defensible priority."""
    save_wakeup(CWD, "sid-late", due_at=NOW + 9000, prompt="late", armed_at=NOW)
    save_wakeup(CWD, "sid-soon", due_at=NOW + 10, prompt="soon", armed_at=NOW)

    assert [r["prompt"] for r in iter_pending_wakeups()] == ["soon", "late"]


def test_no_records_is_not_an_error():
    assert list(iter_pending_wakeups()) == []


# --------------------------------------------------------------------------
# The restraints the hub adds
# --------------------------------------------------------------------------

class _FakeBridge:
    def __init__(self):
        self.armed = []

    def _arm_wakeup(self, delay, prompt):
        self.armed.append((delay, prompt))


class _FakeRT:
    def __init__(self, rid="s9"):
        self.rid = rid
        self.bridge = _FakeBridge()
        self.sent = []

    async def broadcast(self, msg):
        self.sent.append(msg)


def _revive(monkeypatch, *, live=()):
    """Drive the real resurrection with fake runtimes.  Returns (created, rts)."""
    import asyncio
    import server

    class _Live:
        def __init__(self, sid):
            self.state = type("S", (), {"session_id": sid})()

    monkeypatch.setattr(server, "runtimes",
                        {f"live{i}": _Live(sid) for i, sid in enumerate(live)})
    created, rts = [], []

    async def _fake_create(**kw):
        created.append(kw)
        rt = _FakeRT(rid=f"s{len(rts)}")
        rts.append(rt)
        return rt

    monkeypatch.setattr(server, "_create_runtime", _fake_create)

    async def go():
        await server._resurrect_scheduled_wakeups()
        await asyncio.sleep(0.05)      # let the announce task run

    asyncio.run(go())
    return created, rts


def test_one_boot_revives_at_most_the_cap(monkeypatch):
    """Each revival is a CLI process and a transcript load, on a machine that
    has hit its Windows commit limit before.  Without a bound, a hub that had
    eight loops running would try to bring all eight back at once."""
    import server

    for i in range(wakeup_store.MAX_RESURRECT + 3):
        save_wakeup(CWD, f"sid-{i:02d}", due_at=time.time() + 300 + i,
                    prompt="Resume work now", armed_at=time.time())

    created, _rts = _revive(monkeypatch)

    assert len(created) == wakeup_store.MAX_RESURRECT


def test_the_soonest_due_loops_are_the_ones_revived(monkeypatch):
    """The cap makes order matter: it must not be whatever the filesystem
    listed first."""
    for i in range(wakeup_store.MAX_RESURRECT + 3):
        save_wakeup(CWD, f"sid-{i:02d}", due_at=time.time() + 9000 - i * 100,
                    prompt="Resume work now", armed_at=time.time())

    created, _rts = _revive(monkeypatch)
    revived = {c["resume"] for c in created}
    last = f"sid-{wakeup_store.MAX_RESURRECT + 2:02d}"

    assert last in revived, "the soonest-due loop was not among those revived"


def test_a_session_already_running_is_left_alone(monkeypatch):
    """Resurrecting it again would put two bridges on one conversation — the
    duplicate-session hazard proc_guard exists to prevent, self-inflicted."""
    save_wakeup(CWD, SID, due_at=time.time() + 300, prompt="Resume work now",
                armed_at=time.time())

    created, _rts = _revive(monkeypatch, live=(SID,))

    assert created == [], "a live session was resurrected on top of itself"


def test_an_overdue_wakeup_is_re_armed_with_the_settle_delay(monkeypatch):
    """Firing the instant the process comes up means a turn starts before any
    tab has reconnected to see it."""
    import server

    save_wakeup(CWD, SID, due_at=time.time() - 30, prompt="Resume work now",
                armed_at=time.time() - 1230)

    _created, rts = _revive(monkeypatch)

    assert len(rts) == 1
    delay, _prompt = rts[0].bridge.armed[0]
    assert delay == server.WAKEUP_RESTORE_SETTLE_S


def test_a_future_wakeup_is_re_armed_for_its_remaining_time(monkeypatch):
    save_wakeup(CWD, SID, due_at=time.time() + 600, prompt="Resume work now",
                armed_at=time.time())

    _created, rts = _revive(monkeypatch)

    delay, prompt = rts[0].bridge.armed[0]
    assert 500 < delay <= 600
    assert prompt == "Resume work now"


def test_a_revived_session_says_why_it_woke_up(monkeypatch):
    """A turn that starts by itself, in a session the user did not open, with
    no explanation, is the shape of every "why is it doing that?" report this
    project has collected."""
    save_wakeup(CWD, SID, due_at=time.time() + 300, prompt="Resume work now",
                armed_at=time.time())

    _created, rts = _revive(monkeypatch)
    notices = [m for m in rts[0].sent if m.get("type") == "system_msg"]

    assert notices, "the session was reopened and never said why"
    text = (notices[0].get("data") or {}).get("message", "").lower()
    assert "nobody typed anything" in text
    assert "restart" in text


def test_a_paused_record_is_left_on_disk_and_not_run(monkeypatch):
    """Deleting it would lose the schedule entirely; running it is the stale
    plan this whole path exists to refuse. It must do neither."""
    import asyncio
    import server

    save_wakeup(CWD, SID, due_at=time.time() - 8 * 3600,
                prompt="Autonomous-loop wakeup", armed_at=time.time() - 8.5 * 3600)
    monkeypatch.setattr(server, "runtimes", {})
    created = []

    async def _no_create(**kw):
        created.append(kw)
        raise AssertionError("a stale wakeup must not reopen a session")

    monkeypatch.setattr(server, "_create_runtime", _no_create)

    asyncio.run(server._resurrect_scheduled_wakeups())

    assert created == []
    assert list(iter_pending_wakeups()), "the schedule was thrown away"


def test_a_discarded_record_is_removed(monkeypatch):
    """The other half: junk past the freshness cap is deleted, so it cannot sit
    there for eighteen days waiting to run a turn."""
    import asyncio
    import server

    save_wakeup(CWD, SID, due_at=time.time() + 600, prompt="p",
                armed_at=time.time())
    path = wakeup_file_for(CWD, SID)
    rec = json.loads(path.read_text(encoding="utf-8"))
    rec["saved_at"] = time.time() - 72 * 3600
    path.write_text(json.dumps(rec), encoding="utf-8")
    monkeypatch.setattr(server, "runtimes", {})

    asyncio.run(server._resurrect_scheduled_wakeups())

    assert list(iter_pending_wakeups()) == []


def test_a_due_wakeup_does_reopen_its_session(monkeypatch):
    """The control: without it, every test above passes on a function that
    never resurrects anything."""
    import asyncio
    import server

    save_wakeup(CWD, SID, due_at=time.time() + 300, prompt="Resume work now",
                armed_at=time.time())
    monkeypatch.setattr(server, "runtimes", {})
    created = []

    class _RT:
        rid = "s9"
        bridge = None

        async def broadcast(self, msg):
            pass

    async def _fake_create(**kw):
        created.append(kw)
        return _RT()

    monkeypatch.setattr(server, "_create_runtime", _fake_create)

    asyncio.run(server._resurrect_scheduled_wakeups())

    assert len(created) == 1
    assert created[0]["resume"] == SID
    assert created[0]["cwd"] == CWD


def test_a_revived_session_says_why_it_woke_up():
    """A turn that starts by itself, in a session the user did not open, with
    no explanation, is the shape of every "why is it doing that?" report this
    project has collected."""
    import inspect
    import server

    src = inspect.getsource(server._rearm_restored_wakeup)
    assert "system_msg" in src
    assert "nobody typed anything" in src


def test_an_overdue_wakeup_waits_for_the_hub_to_settle():
    """Firing the instant the process comes up means a turn starts before any
    tab has reconnected to see it."""
    import server

    assert server.WAKEUP_RESTORE_SETTLE_S >= 5.0
