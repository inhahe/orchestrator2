"""Whose queue is it?  Persisted pending prompts across sessions.

The pending-prompt queue is mirrored to disk so typed-but-not-yet-run prompts
survive a server restart.  The subtlety that made this dangerous is that a
restored prompt is not merely *displayed*: ``SDKBridge.connect()`` ends by
calling ``_poke_for_queued_prompt()``, so the worker pops it and **sends it as
a real turn** with no user action at all.

Reported 2026-09-03: a second session started in a directory with
``--no-continue`` immediately re-ran a prompt the user had given a previous
session there, and had to be aborted.  The queue was keyed by *cwd alone* and
restored unconditionally, so:

* a brand-new session inherited whatever an older one had left queued;
* two sessions open on the same directory wrote to one file and clobbered
  each other;
* nothing ever expired -- an 18-day-old queue file was found still on disk,
  armed, in ``.../queues/D--visual-studio-projects-forward-raytracer.json``.

The rule now is that **a queue belongs to a session, not to a directory**:
the file is keyed by session id, only restored into that same session, and
only while it is fresh.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import session as sess_mod                                   # noqa: E402
from session import (                                        # noqa: E402
    QUEUE_MAX_AGE_S,
    load_persisted_queue,
    queue_file_for_cwd,
    save_persisted_queue,
)

CWD = r"D:\some\project"
SID_A = "11111111-aaaa-4444-8888-000000000001"
SID_B = "22222222-bbbb-4444-8888-000000000002"


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path, monkeypatch):
    """Point the queue store at a temp dir so tests never touch ~/.claude."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    yield


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

def test_a_session_gets_its_own_queue_back():
    """The feature has to keep working -- this is the whole point of it."""
    save_persisted_queue(CWD, ["finish the parser"], SID_A)
    assert load_persisted_queue(CWD, SID_A) == ["finish the parser"]


def test_an_empty_queue_removes_the_file_rather_than_leaving_an_empty_one():
    save_persisted_queue(CWD, ["x"], SID_A)
    assert queue_file_for_cwd(CWD, SID_A).exists()
    save_persisted_queue(CWD, [], SID_A)
    assert not queue_file_for_cwd(CWD, SID_A).exists()


def test_order_is_preserved():
    save_persisted_queue(CWD, ["first", "second", "third"], SID_A)
    assert load_persisted_queue(CWD, SID_A) == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# ...and nobody else's
# ---------------------------------------------------------------------------

def test_a_different_session_in_the_same_directory_gets_nothing():
    """The reported bug, at the storage layer."""
    save_persisted_queue(CWD, ["an old prompt"], SID_A)
    assert load_persisted_queue(CWD, SID_B) == []


def test_a_brand_new_session_does_not_inherit_an_older_ones_queue():
    """``--no-continue`` seeds no session id at all."""
    save_persisted_queue(CWD, ["an old prompt"], SID_A)
    assert load_persisted_queue(CWD, None) == []


def test_two_sessions_in_one_directory_do_not_clobber_each_other():
    """The hub hosts several sessions at once; cwd-only keying meant the last
    writer won and the other session's queue silently vanished."""
    save_persisted_queue(CWD, ["A work"], SID_A)
    save_persisted_queue(CWD, ["B work"], SID_B)
    assert load_persisted_queue(CWD, SID_A) == ["A work"]
    assert load_persisted_queue(CWD, SID_B) == ["B work"]


def test_the_id_inside_the_file_is_checked_too():
    """Belt and braces: a file that somehow lands in the wrong slot -- a reused
    session id, a hand-edited or copied file -- must still be refused."""
    path = queue_file_for_cwd(CWD, SID_A)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cwd": CWD, "session_id": SID_B,
                                "queue": ["not yours"],
                                "saved_at": time.time()}), encoding="utf-8")
    assert load_persisted_queue(CWD, SID_A) == []


def test_an_id_less_file_is_refused_even_when_one_exists():
    """The loader's own guard, tested against a file the saver would now never
    produce -- an older build, a hand-copied file, a half-finished write.

    This is deliberately not routed through save_persisted_queue(): the saver
    refuses id-less queues, so using it here would test nothing.  The reader is
    the half that does the damage (a restored prompt is *sent*), so it has to
    hold on input it did not create.
    """
    path = queue_file_for_cwd(CWD, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cwd": CWD, "session_id": None,
                                "queue": ["left by an older build"],
                                "saved_at": time.time()}), encoding="utf-8")
    assert path.exists(), "test planted nothing"
    assert load_persisted_queue(CWD, None) == []


def test_a_legacy_cwd_only_file_is_never_restored():
    """Exactly the shape found armed on disk after 18 days.  It must be inert
    without anyone having to go and delete it."""
    legacy = sess_mod._orch2_state_dir() / "queues" / (
        sess_mod._sanitize_cwd(CWD) + ".json")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"cwd": CWD, "queue": ["18 days old"],
                                  "saved_at": time.time()}), encoding="utf-8")
    assert load_persisted_queue(CWD, SID_A) == []
    assert load_persisted_queue(CWD, None) == []


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------

def _age_the_queue(seconds: float) -> None:
    """Backdate the stored queue by *seconds*."""
    path = queue_file_for_cwd(CWD, SID_A)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["saved_at"] = time.time() - seconds
    path.write_text(json.dumps(data), encoding="utf-8")


# Deliberately absolute, NOT expressed in terms of QUEUE_MAX_AGE_S: a test that
# derives its own timestamps from the constant it is checking passes for *any*
# value of that constant, including a 1-second cap that throws the queue away
# on every ordinary restart.  A mutation sweep caught exactly that.
def test_a_day_and_a_half_old_queue_is_not_restored():
    """A queue survives a restart, not a week."""
    save_persisted_queue(CWD, ["ancient"], SID_A)
    _age_the_queue(36 * 3600)
    assert load_persisted_queue(CWD, SID_A) == []


@pytest.mark.parametrize("age_s,label", [
    (30, "half a minute (a crash-restart)"),
    (5 * 60, "five minutes (a deliberate restart)"),
    (2 * 3600, "two hours (a long lunch)"),
])
def test_a_queue_from_a_realistic_restart_is_still_restored(age_s, label):
    """The cap must not be so eager that an ordinary restart loses work.
    These are the gaps a real restart actually spans."""
    save_persisted_queue(CWD, ["recent"], SID_A)
    _age_the_queue(age_s)
    assert load_persisted_queue(CWD, SID_A) == ["recent"], label


def test_a_file_with_no_timestamp_is_refused():
    """Undated means unbounded age; the file predates this format."""
    path = queue_file_for_cwd(CWD, SID_A)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cwd": CWD, "session_id": SID_A,
                                "queue": ["undated"]}), encoding="utf-8")
    assert load_persisted_queue(CWD, SID_A) == []


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("blob", ['{"queue": ', "[]", "null", "", "not json"])
def test_a_damaged_file_is_ignored_not_raised(blob):
    """Persistence must never break queue operations."""
    path = queue_file_for_cwd(CWD, SID_A)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(blob, encoding="utf-8")
    assert load_persisted_queue(CWD, SID_A) == []


def test_non_string_entries_are_dropped():
    path = queue_file_for_cwd(CWD, SID_A)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cwd": CWD, "session_id": SID_A,
                                "queue": ["ok", 5, None, {"a": 1}, "fine"],
                                "saved_at": time.time()}), encoding="utf-8")
    assert load_persisted_queue(CWD, SID_A) == ["ok", "fine"]


def test_missing_file_is_empty_not_an_error():
    assert load_persisted_queue(CWD, SID_A) == []


def test_the_id_is_path_sanitised():
    """A session id reaches the filesystem; it must not be able to escape the
    queues directory."""
    path = queue_file_for_cwd(CWD, "../../etc/passwd")
    assert path.parent == sess_mod._orch2_state_dir() / "queues"
    assert ".." not in path.name


# ---------------------------------------------------------------------------
# The server-side gate
#
# The storage rules above make the *wrong* queue unreachable.  These pin the
# other half: that the server asks for the right one, at the right moment.
# The original bug was purely a call-site bug -- _attach_queue_persistence ran
# before the session id was seeded and with no way to decline -- so storage
# tests alone would not have caught it.
# ---------------------------------------------------------------------------

import server                                                # noqa: E402
from state import PersistentDeque                            # noqa: E402


class FakeState:
    """Just the two attributes _attach_queue_persistence touches."""

    def __init__(self, session_id=None):
        self.session_id = session_id
        self.queued_prompts = PersistentDeque()


def test_a_seeded_session_loads_its_own_queue():
    save_persisted_queue(CWD, ["mine"], SID_A)
    st = FakeState(SID_A)
    assert server._attach_queue_persistence(st, CWD) == 1
    assert list(st.queued_prompts) == ["mine"]


def test_an_unseeded_session_reads_nothing_of_an_older_ones():
    """The exact reported shape: a fresh session, an older session's file
    present in the same directory.

    Note there is no flag involved.  An earlier version of this fix put a
    ``restore=`` gate on each call site; a mutation sweep showed the safety did
    not actually depend on it -- the loader already refuses -- so the flag was
    removed rather than kept as unverified ceremony."""
    save_persisted_queue(CWD, ["previously given"], SID_A)
    st = FakeState(None)
    assert server._attach_queue_persistence(st, CWD) == 0
    assert list(st.queued_prompts) == []


def test_a_fresh_session_still_wires_saving_for_later():
    """Restoring nothing must not mean persisting nothing -- once the session
    has an identity, its own queue has to survive a restart."""
    st = FakeState(None)
    server._attach_queue_persistence(st, CWD)
    st.queued_prompts.append("typed before the session had an id")
    # Assert on the file rather than through load_persisted_queue(): the
    # loader has its own id-less guard, which would report "nothing there"
    # whether or not anything was written.  Two guards that only ever mask
    # each other are two guards nothing verifies.
    assert not queue_file_for_cwd(CWD, None).exists(), (
        "wrote a queue for a session with no identity to restore it into")
    st.session_id = SID_A
    st.queued_prompts.append("typed after")
    assert load_persisted_queue(CWD, SID_A) == [
        "typed before the session had an id", "typed after"]


def test_a_restore_failure_does_not_stop_the_session_starting():
    """Persistence must never break queue operations, including at startup."""
    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    orig = server.load_persisted_queue
    server.load_persisted_queue = boom
    try:
        st = FakeState(SID_A)
        assert server._attach_queue_persistence(st, CWD) == 0
        assert list(st.queued_prompts) == []
    finally:
        server.load_persisted_queue = orig


def test_every_call_site_seeds_the_session_first():
    """The bug was ordering: the attach ran before the session id existed, so
    it could only ever look up the id-less slot -- and restored it into a
    session that was explicitly meant to be fresh.

    Pins the ordering textually, because the three call sites live in async
    startup paths that a unit test cannot cheaply drive.
    """
    src = (Path(__file__).resolve().parent.parent / "server.py").read_text(
        encoding="utf-8")
    calls = [i for i, line in enumerate(src.splitlines())
             if "_attach_queue_persistence(" in line and "def " not in line]
    assert len(calls) == 3, f"call sites moved; found {len(calls)}"
    lines = src.splitlines()
    for i in calls:
        window = "\n".join(lines[max(0, i - 40):i])
        assert "session_id" in window or "seed_id" in window, (
            f"_attach_queue_persistence at line {i + 1} is not preceded by "
            f"session seeding -- it would restore the id-less queue")
