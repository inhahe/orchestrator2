"""A session running in another hub is running, not "recent".

Reported 2026-09-03: the sessions view listed OSb, OSc and Good Photons under
**recents** while all three were actively running.

``_session_list_payload`` built its running list from ``runtimes.values()`` --
this *process's* registry. The hub reuses one server per account/port, so a
machine running several accounts has several hub processes; a session live in
any of the others is absent from ``runtimes``, falls through to the on-disk
scan, and gets listed as merely recent. Confirmed on the reporter's machine:
all three were held by foreign ``claude`` processes under hubs listening on
ports 63978, 51842 and 51843, while this hub had 8420.

``proc_guard.map_foreign_session_holders()`` answers "who else is holding a
session" in **one** process walk (the pre-existing helper scanned per session
id: a cold walk measured 13 s here, 0.4 s warm, so forty of them per 2-second
lobby tick was never viable). Sessions it finds are promoted into ``running``
with ``foreign: True`` -- they are real, they are alive, and this window simply
cannot drive them.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402

#: The real probe, captured before the autouse fixture stubs it out.
#: Tests of the probe itself call this; everything else gets the stub.
REAL_PROBE = server._probe_hub_running


class _Holder(SimpleNamespace):
    """Stands in for proc_guard.ForeignHolder."""


def _holder(pid=999, port=51842, started="2026-09-03 01:00:00"):
    return _Holder(holder_pid=pid, started=started, server_pid=pid - 1, port=port)


def _disk(sid, title="A Session", cwd="D:/proj", mtime=100):
    return {"session_id": sid, "title": title, "cwd": cwd, "account": None,
            "mtime": mtime, "age": "2m ago", "first_user_msg": "hi"}


#: What each peer hub answers on ``/api/running``, keyed by port.  ``_payload``
#: installs this; a port that is absent stands for a hub that could not be
#: reached, which is the default and the more interesting case.
PEERS: dict = {}


def _peer(sid, *, busy=False, viewers=0, title=None, last_activity=None,
          cwd=None, idle_deadline=None):
    """One entry as a peer hub's ``SessionRuntime.meta()`` would return it."""
    return {"rid": "s1", "session_id": sid, "title": title, "cwd": cwd,
            "busy": busy, "viewers": viewers, "created_at": 1.0,
            "last_activity": last_activity, "idle_deadline": idle_deadline,
            "account": None}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """No real process scans, no real disk scans, no real HTTP, empty registry."""
    monkeypatch.setattr(server, "runtimes", {})
    server._holders_cache = None
    server._hub_probe_cache.clear()
    PEERS.clear()
    # Never let a test reach the network: an unstubbed probe would try
    # 127.0.0.1 on whatever port the fixture invented.
    monkeypatch.setattr(server, "_probe_hub_running",
                        lambda port: PEERS.get(port))
    yield
    server._holders_cache = None
    server._hub_probe_cache.clear()
    PEERS.clear()


def _payload(recent, holders, peers=None):
    server._holders_cache = (float("inf"), holders)   # bypass the real scan
    PEERS.clear()
    PEERS.update(peers or {})

    async def go():
        loop = asyncio.get_running_loop()
        orig = loop.run_in_executor

        async def fake_exec(_ex, fn, *a):
            if fn is server._recent_disk_sessions:
                return recent
            return fn(*a)

        loop.run_in_executor = fake_exec          # type: ignore[assignment]
        try:
            return await server._session_list_payload()
        finally:
            loop.run_in_executor = orig           # type: ignore[assignment]

    return asyncio.run(go())


# ---------------------------------------------------------------------------
# The reported bug
# ---------------------------------------------------------------------------

def test_a_session_held_by_another_hub_is_listed_as_running():
    msg = _payload([_disk("sid-osb", "OSb")], {"sid-osb": _holder()})
    assert [m["title"] for m in msg["running"]] == ["OSb"]
    assert msg["recent"] == [], "still shown as merely recent"


def test_it_is_flagged_as_somebody_elses():
    """It must not masquerade as a local runtime: there is nothing here to
    attach to, and the card's close button would stop a process this window
    does not own."""
    msg = _payload([_disk("sid-osb", "OSb")], {"sid-osb": _holder(port=51842)})
    card = msg["running"][0]
    assert card["foreign"] is True
    assert card["rid"] is None
    assert card["port"] == 51842
    assert card["holder_pid"] == 999


def test_several_at_once():
    """The report named three."""
    recent = [_disk("a", "OSb"), _disk("b", "OSc"), _disk("c", "Good Photons")]
    holders = {"a": _holder(port=51842), "b": _holder(port=51843),
               "c": _holder(port=63978)}
    msg = _payload(recent, holders)
    assert sorted(m["title"] for m in msg["running"]) == [
        "Good Photons", "OSb", "OSc"]
    assert msg["recent"] == []


def test_a_genuinely_idle_session_stays_recent():
    """The promotion must be evidence-based, or "recent" becomes meaningless."""
    msg = _payload([_disk("sid-old", "Old Thing")], {"sid-other": _holder()})
    assert msg["running"] == []
    assert [s["title"] for s in msg["recent"]] == ["Old Thing"]


def test_no_holders_changes_nothing():
    msg = _payload([_disk("x"), _disk("y")], {})
    assert msg["running"] == []
    assert len(msg["recent"]) == 2


# ---------------------------------------------------------------------------
# Say what you know, and only what you know
# ---------------------------------------------------------------------------
#
# A process scan can see *that* something holds a session and *where* it runs.
# It cannot see whether a turn is in flight or how many tabs are watching, and
# those were filled in as ``busy: False`` / ``viewers: 0`` -- rendered by the
# lobby as an idle dot and a viewer count, i.e. placeholders presented as
# measurements. A session hammering away in another window looked idle.
#
# The hub that owns the session does know, and now answers on ``/api/running``.
# So: ask, and state facts; when you cannot ask, say so.

def test_the_owning_hub_is_asked_what_the_session_is_doing():
    msg = _payload([_disk("sid-a", "OSb")], {"sid-a": _holder(port=51842)},
                   peers={51842: {"sid-a": _peer("sid-a", busy=True, viewers=2)}})
    card = msg["running"][0]
    assert card["busy"] is True, "reported an actively-working session as idle"
    assert card["viewers"] == 2
    assert card["live_known"] is True


def test_a_peer_that_says_idle_is_believed():
    """The point is not "always busy" -- it is that the answer comes from
    something that knows."""
    msg = _payload([_disk("sid-a")], {"sid-a": _holder(port=51842)},
                   peers={51842: {"sid-a": _peer("sid-a", busy=False, viewers=0)}})
    card = msg["running"][0]
    assert card["busy"] is False
    assert card["viewers"] == 0
    assert card["live_known"] is True


def test_an_unreachable_hub_yields_unknown_not_idle():
    """``None``, not ``False``. This is the bug: a hub that cannot be reached
    is not evidence that its session is idle, and rendering it as the idle dot
    asserted exactly that."""
    msg = _payload([_disk("sid-a")], {"sid-a": _holder(port=51842)}, peers={})
    card = msg["running"][0]
    assert card["busy"] is None
    assert card["viewers"] is None
    assert card["live_known"] is False


def test_a_holder_that_is_not_a_hub_yields_unknown():
    """A bare ``claude --resume`` in a terminal has no port and no API. There
    is nothing to ask, and that is a permanent condition, not a failure."""
    msg = _payload([_disk("sid-a")], {"sid-a": _holder(port=None)})
    card = msg["running"][0]
    assert card["busy"] is None and card["live_known"] is False


def test_a_peer_that_answers_without_this_session_is_still_unknown():
    """The scan and the peer's registry can disagree for a moment (a session
    closing). "It answered, but not about this session" is ignorance about
    *this* session, not a report that it is idle."""
    msg = _payload([_disk("sid-a")], {"sid-a": _holder(port=51842)},
                   peers={51842: {"sid-other": _peer("sid-other", busy=True)}})
    card = msg["running"][0]
    assert card["busy"] is None and card["live_known"] is False


def test_the_live_title_wins_over_the_disk_one():
    """A rename lands in the owning hub's memory before it lands in the
    JSONL the scan reads."""
    msg = _payload([_disk("sid-a", "Old Name")], {"sid-a": _holder(port=51842)},
                   peers={51842: {"sid-a": _peer("sid-a", title="Renamed")}})
    assert msg["running"][0]["title"] == "Renamed"


def test_the_disk_title_is_used_when_the_peer_has_none():
    """An unnamed session in the peer's registry must not blank out a title
    the disk scan resolved."""
    msg = _payload([_disk("sid-a", "From Disk")], {"sid-a": _holder(port=51842)},
                   peers={51842: {"sid-a": _peer("sid-a", title=None)}})
    assert msg["running"][0]["title"] == "From Disk"


def test_the_live_activity_time_wins_over_the_file_mtime():
    """Both are wall-clock epochs, so they are comparable -- and the peer's is
    the moment of the last *turn*, not of the last flush to disk."""
    msg = _payload([_disk("sid-a", mtime=100)], {"sid-a": _holder(port=51842)},
                   peers={51842: {"sid-a": _peer("sid-a", last_activity=900)}})
    assert msg["running"][0]["last_activity"] == 900


def test_one_request_per_hub_not_per_session():
    """Three sessions in one peer hub is one question."""
    asked: list = []

    def counting(port):
        asked.append(port)
        return {f"sid-{i}": _peer(f"sid-{i}") for i in "abc"}

    server._probe_hub_running = counting
    recent = [_disk("sid-a"), _disk("sid-b"), _disk("sid-c")]
    holders = {s: _holder(port=51842) for s in ("sid-a", "sid-b", "sid-c")}
    msg = _payload(recent, holders)
    assert asked == [51842], f"asked {len(asked)} times for one hub"
    assert len(msg["running"]) == 3


def test_each_distinct_hub_is_asked_once():
    asked: list = []

    def counting(port):
        asked.append(port)
        return {}

    server._probe_hub_running = counting
    holders = {"sid-a": _holder(port=51842), "sid-b": _holder(port=63978),
               "sid-c": _holder(port=51842)}
    _payload([_disk("sid-a"), _disk("sid-b"), _disk("sid-c")], holders)
    assert sorted(asked) == [51842, 63978]


def test_a_holder_with_no_port_is_not_probed():
    asked: list = []

    def counting(port):
        asked.append(port)
        return {}

    server._probe_hub_running = counting
    _payload([_disk("sid-a")], {"sid-a": _holder(port=None)})
    assert asked == [], "tried to ask a process that has no API"


# ---------------------------------------------------------------------------
# The probe's own caching
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(monkeypatch, answer, *, fail=False):
    """Stub ``urlopen``; return the list recording ``(url, timeout)`` per call.

    The timeout is recorded, not just accepted: a fake that swallows it makes
    "the probe has a deadline" unverifiable, and that deadline is the only
    thing standing between one wedged peer and a stalled lobby.
    """
    import urllib.request
    calls: list[tuple] = []

    def fake(url, timeout=None):
        calls.append((url, timeout))
        if fail:
            raise OSError("connection refused")
        return _FakeResp(answer)

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return calls


def test_the_probe_parses_a_peers_registry_into_a_by_id_map(monkeypatch):
    calls = _fake_urlopen(monkeypatch, {"running": [
        _peer("sid-a", busy=True), _peer("sid-b")]})
    got = REAL_PROBE(51842)
    assert set(got) == {"sid-a", "sid-b"}
    assert got["sid-a"]["busy"] is True
    assert [c[0] for c in calls] == ["http://127.0.0.1:51842/api/running"], calls


def test_the_probe_asks_loopback_only(monkeypatch):
    """It must never leave the machine: peer hubs are local by definition, and
    loopback is what lets it skip the external-auth middleware."""
    calls = _fake_urlopen(monkeypatch, {"running": []})
    REAL_PROBE(51842)
    assert calls[0][0].startswith("http://127.0.0.1:")


def test_an_empty_answer_is_knowledge_not_ignorance(monkeypatch):
    """``{}`` means "asked, it has no sessions"; ``None`` means "could not
    ask". Collapsing them would make an idle hub indistinguishable from a dead
    one."""
    _fake_urlopen(monkeypatch, {"running": []})
    assert REAL_PROBE(51842) == {}


def test_a_failed_probe_returns_none(monkeypatch):
    _fake_urlopen(monkeypatch, None, fail=True)
    assert REAL_PROBE(51842) is None


def test_a_successful_probe_is_cached(monkeypatch):
    calls = _fake_urlopen(monkeypatch, {"running": [_peer("sid-a")]})
    REAL_PROBE(51842)
    REAL_PROBE(51842)
    assert len(calls) == 1, "asked twice inside the TTL"


def test_the_success_cache_is_short_lived(monkeypatch):
    """"Is it working right now" is the fast-moving thing the probe exists to
    answer, so its cache must expire on the order of a lobby tick."""
    calls = _fake_urlopen(monkeypatch, {"running": [_peer("sid-a")]})
    REAL_PROBE(51842)
    server._hub_probe_cache[51842] = (
        time.monotonic() - server._HUB_PROBE_TTL - 0.01,
        server._hub_probe_cache[51842][1])
    REAL_PROBE(51842)
    assert len(calls) == 2, "a stale busy flag was served past the TTL"


def test_a_failed_probe_backs_off_far_longer_than_a_good_one(monkeypatch):
    """One wedged peer must not add its timeout to every 2-second lobby tick
    for everyone -- that would make the lobby slow *because* something is
    wrong, which is exactly when it needs to be responsive."""
    assert server._HUB_PROBE_FAIL_TTL >= 10 * server._HUB_PROBE_TTL
    calls = _fake_urlopen(monkeypatch, None, fail=True)
    REAL_PROBE(51842)
    # Past the *success* TTL but well inside the failure backoff.
    server._hub_probe_cache[51842] = (
        time.monotonic() - server._HUB_PROBE_TTL - 0.01, None)
    REAL_PROBE(51842)
    assert len(calls) == 1, "retried a dead hub on the very next tick"


def test_the_backoff_does_expire(monkeypatch):
    """A hub that comes back must be noticed, or the card is stuck on
    "unknown" until the server restarts."""
    calls = _fake_urlopen(monkeypatch, None, fail=True)
    REAL_PROBE(51842)
    server._hub_probe_cache[51842] = (
        time.monotonic() - server._HUB_PROBE_FAIL_TTL - 0.01, None)
    REAL_PROBE(51842)
    assert len(calls) == 2, "a recovered hub would never be re-asked"


def test_the_probe_cannot_outlast_the_tick_it_runs_on(monkeypatch):
    """A bounded deadline, actually handed to urlopen.

    Without it a peer that accepts the connection and then never answers holds
    the executor thread open indefinitely, and the session list -- for every
    tab on this hub -- waits behind it. The failure mode is the lobby going
    dark precisely when something is wrong with a peer, which is when it is
    most needed.
    """
    calls = _fake_urlopen(monkeypatch, {"running": []})
    REAL_PROBE(51842)
    assert calls[0][1] is not None, "probe issued with no deadline at all"
    assert calls[0][1] == server._HUB_PROBE_TIMEOUT
    assert server._HUB_PROBE_TIMEOUT < 2.0, "longer than the lobby tick"


# ---------------------------------------------------------------------------
# Not stealing our own
# ---------------------------------------------------------------------------

def test_our_own_live_session_is_never_relabelled_foreign(monkeypatch):
    """``proc_guard`` already excludes our own process tree, but a *cached*
    holder map outlives the scan that produced it. A stale entry must not be
    able to turn one of our own runtimes into someone else's."""
    rt = SimpleNamespace(
        state=SimpleNamespace(session_id="sid-mine", session_title="Mine",
                              busy=False),
        meta=lambda: {"rid": "s1", "session_id": "sid-mine", "title": "Mine",
                      "last_activity": 5},
    )
    monkeypatch.setattr(server, "runtimes", {"s1": rt})
    msg = _payload([_disk("sid-mine", "Mine")], {"sid-mine": _holder()})
    assert len(msg["running"]) == 1
    assert msg["running"][0]["rid"] == "s1"
    assert not msg["running"][0].get("foreign")


def test_running_stays_sorted_by_activity():
    recent = [_disk("a", "Older", mtime=10), _disk("b", "Newer", mtime=900)]
    msg = _payload(recent, {"a": _holder(), "b": _holder()})
    assert [m["title"] for m in msg["running"]] == ["Newer", "Older"]


# ---------------------------------------------------------------------------
# The scan itself
# ---------------------------------------------------------------------------

def test_the_scan_is_cached():
    """A cold process walk measured 13 s; the lobby ticks every 2 s."""
    calls = []

    import proc_guard
    real = proc_guard.map_foreign_session_holders
    proc_guard.map_foreign_session_holders = lambda: (calls.append(1) or {})
    server._holders_cache = None
    try:
        server._foreign_holders()
        server._foreign_holders()
        server._foreign_holders()
    finally:
        proc_guard.map_foreign_session_holders = real
    assert len(calls) == 1, f"scanned {len(calls)} times instead of caching"


def test_a_failing_scan_does_not_break_the_lobby():
    """A best-effort detector must never be why the session list won't render."""
    import proc_guard
    real = proc_guard.map_foreign_session_holders

    def boom():
        raise RuntimeError("psutil exploded")

    proc_guard.map_foreign_session_holders = boom
    server._holders_cache = None
    try:
        assert server._foreign_holders() == {}
    finally:
        proc_guard.map_foreign_session_holders = real


def test_the_map_matches_the_single_id_lookup():
    """The map replaces N per-id scans, so it must agree with them. Runs
    against whatever is really on this machine -- including nothing.

    Deliberately tolerant of a holder that exits between the two scans: that is
    not a disagreement, it is a process that stopped, and it was observed for
    real while developing this (two sessions' CLIs turned over mid-investigation).
    A test that treats normal process turnover as a failure is a test that will
    cry wolf. So the assertion is the one that actually matters: *while the
    holder is still alive*, both routes name the same pid.
    """
    import psutil
    import proc_guard
    m = proc_guard.map_foreign_session_holders()
    checked = 0
    for sid, holder in list(m.items())[:3]:
        one = proc_guard.find_foreign_session_holder(sid)
        if one is None:
            assert not psutil.pid_exists(holder.holder_pid), (
                f"map claims {sid[:8]} is held by live pid "
                f"{holder.holder_pid}, per-id lookup disagrees")
            continue
        assert one.holder_pid == holder.holder_pid
        checked += 1
    # Nothing to compare is a legitimate outcome (no other hub running).
    assert checked >= 0
