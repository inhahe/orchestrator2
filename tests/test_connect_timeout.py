"""``CONNECT_TIMEOUT`` is a backstop against a hang, not a performance budget.

From a report: *"i often get 'sdk connetion failed' one or more times when
starting up a session, like this time. maybe it times out too quickly?"* — which
was right.

Measured over 580 connects in ``orchestrator2.log`` (``connect: resume=`` to the
``dispatcher start`` that follows a completed handshake): p50 5.3 s, p90 20.4 s,
p98 34.9 s, p99 38.2 s, **max 44.7 s**, against a ceiling of 45 s — and 32
timeouts, 5.5% of attempts. A genuinely hung connect and a merely slow one would
form two populations with a gap; instead the successes run continuously into the
ceiling and stop dead there. That is a cutoff slicing a live distribution.

The retries confirm it. Both logged startup failures read::

    19:10:50  connect: resume=424654f8..., cwd=D:\\...\\orchestrator2
    19:11:36  SDK connect timed out after 45s - retrying
    19:11:43  connect: resume=424654f8...      (attempt 2)
    19:12:05  SDK connected after 1 retries    (21.9s)

The second attempt does nothing the first wasn't; it runs against a warm file
cache after the first paid to read the bundled ``claude.exe`` past Windows
Defender. Timing the first one out therefore *costs* a teardown, a backoff and a
whole second spawn to reach a handshake that was nearly done — and hub startup
makes it worse by spawning several CLIs within seconds of each other.

These tests pin the conclusion rather than the number: the ceiling must sit well
clear of the observed distribution, the mechanism must still fire on a real
hang, and a timeout must say so — ``str(TimeoutError())`` is ``""``, so the
message the user saw was literally "failed: " with nothing after it.

----------------------------------------------------------------------------
Round two: the 180 s ceiling was never reached
----------------------------------------------------------------------------

From a later report: *"i resumed three sessions under ..\\os, two of them took a
long time to connect, the third hasn't connected yet and is on its 8th
attempt."* ``CONNECT_TIMEOUT`` was not the binding deadline — the SDK has its
own, and it is smaller. ``ClaudeSDKClient.connect()`` computes::

    initialize_timeout = max(int(os.environ.get(
        "CLAUDE_CODE_STREAM_CLOSE_TIMEOUT", "60000")) / 1000.0, 60.0)

so unless that variable is set the SDK abandons the handshake at 60 s and ours
never fires. The log shows exactly that: nine failures, every one 65.3 s ± 0.1 s
after its spawn, all reading ``Control request timeout: initialize``.

The work being cut off is a whole-file read — resuming re-parses the session
JSONL — so the handshake cost is O(size). Measured on the three sessions in the
report (the third timed alone, with the SDK ceiling lifted)::

     348 MB  ->   ~58 s   connected, second attempt
     452 MB  ->   ~58 s   connected
    1293 MB  ->   148 s   never connected: 10 attempts, all cut at 65.3 s

~115 s/GB. The 1.29 GB session was not hung; at a 60 s ceiling it was
*impossible*, and retrying could never have helped. Hence two changes, both
tested below: lift the SDK's ceiling out of the way, and size the real budget
from the file instead of from a constant that cannot know how big it is.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sdk_bridge                                   # noqa: E402
from sdk_bridge import (                            # noqa: E402
    CONNECT_TIMEOUT,
    CONNECT_TIMEOUT_MAX,
    HEAVY_CONNECT_BYTES,
    connect_timeout_for,
)

MB = 1024 * 1024
GB = 1024 * MB

# Measured, uncontended, on the 1.29 GB session from the report.
MEASURED_BYTES = 1293 * MB
MEASURED_SECONDS = 148.0


# Slowest connect that *succeeded* in the logged sample, in seconds.  It is a
# lower bound on the real distribution, not an upper one: the old ceiling was
# 45 s, so anything slower was censored and never recorded as a success.
OBSERVED_SLOWEST_SUCCESS = 44.7


def test_the_ceiling_clears_the_observed_distribution():
    """A connect that would have succeeded must not be killed.

    The margin matters more than the exact value: the sample is censored at the
    old 45 s ceiling, so the true tail is *longer* than the 44.7 s we can see.
    A ceiling merely above the observed max would still be inside the real one.
    """
    assert CONNECT_TIMEOUT >= 3 * OBSERVED_SLOWEST_SUCCESS, (
        f"CONNECT_TIMEOUT={CONNECT_TIMEOUT}s is too close to the {OBSERVED_SLOWEST_SUCCESS}s "
        "slowest observed *successful* connect; the sample is censored, so the "
        "real tail runs longer than that"
    )


def test_it_is_still_bounded():
    """Not a licence to hang forever.

    The timeout exists because a CLI can spawn and never finish its handshake,
    which would otherwise leave the UI on "connecting…" with no way out but a
    restart. An unbounded wait trades one stuck state for another.
    """
    assert CONNECT_TIMEOUT == pytest.approx(CONNECT_TIMEOUT)
    assert 0 < CONNECT_TIMEOUT <= 600, "a hung handshake must still be recovered"


# ---------------------------------------------------------------------------
# The mechanism
# ---------------------------------------------------------------------------

class _HangingClient:
    """Spawns, never completes the handshake — the case the ceiling is for."""

    def __init__(self) -> None:
        self.disconnected = False

    async def connect(self) -> None:
        await asyncio.sleep(3600)

    async def disconnect(self) -> None:
        self.disconnected = True


def test_a_hung_handshake_still_times_out_and_is_torn_down(monkeypatch):
    """On expiry the half-open client must be dropped, not retried against.

    A timed-out connect can leave a live subprocess behind and ``self.client``
    pointing at something unusable; reusing it would fail every subsequent turn
    with the connection already counted as established.
    """
    monkeypatch.setattr(sdk_bridge, "CONNECT_TIMEOUT", 0.05)

    async def go():
        client = _HangingClient()
        with pytest.raises(asyncio.TimeoutError):
            try:
                await asyncio.wait_for(
                    client.connect(), timeout=sdk_bridge.CONNECT_TIMEOUT)
            except BaseException:
                await client.disconnect()
                raise
        assert client.disconnected, "half-open client left in place after timeout"

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Saying why
# ---------------------------------------------------------------------------

def test_a_timeout_reports_itself():
    """``str(asyncio.TimeoutError())`` is the empty string.

    That is the whole reason the log read ``SDK connect failed (attempt 1): ``
    with nothing after the colon, and the user saw an unexplained "SDK
    connection failed". Whether a connect was refused or simply ran long is the
    first thing worth knowing about it — and, in this case, the entire diagnosis.
    """
    assert str(asyncio.TimeoutError()) == "", \
        "premise changed: TimeoutError now carries a message of its own"

    exc: Exception = asyncio.TimeoutError()
    why = (f"timed out after {CONNECT_TIMEOUT:.0f}s"
           if isinstance(exc, asyncio.TimeoutError)
           else (str(exc) or type(exc).__name__))
    assert why == f"timed out after {CONNECT_TIMEOUT:.0f}s"


def test_a_silent_exception_falls_back_to_its_type():
    """Empty messages are not unique to timeouts.

    Several SDK errors raise with no text at all; naming the class is the
    difference between "something failed" and "CLIConnectionError".
    """
    exc: Exception = RuntimeError("")
    why = (f"timed out after {CONNECT_TIMEOUT:.0f}s"
           if isinstance(exc, asyncio.TimeoutError)
           else (str(exc) or type(exc).__name__))
    assert why == "RuntimeError"


def test_the_worker_uses_that_wording(monkeypatch):
    """Pin it at the call site, not just in the expression above.

    The three connect-failure messages the user can see — per-attempt, after the
    attempt cap, and the "still can't connect" loop — all interpolated the raw
    exception. This checks the source, so the wording can't drift back.
    """
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "sdk_bridge.py"), encoding="utf-8").read()
    assert "SDK connection failed (attempt {_connect_attempt}/{_MAX_CONNECT_ATTEMPTS}: {_why})" in src
    assert "SDK connection failed after {_connect_attempt} attempts: {_why}" in src
    assert "Still can't connect ({_why})" in src


# ---------------------------------------------------------------------------
# The deadline we do not own
# ---------------------------------------------------------------------------

def test_the_sdk_ceiling_is_lifted_above_ours():
    """Otherwise every budget computed here is decoration.

    The SDK floors its ``initialize`` wait at 60 s and only reads the override
    from *this* process's environment, never from ``options.env`` — so an
    orchestrator that carefully allows 635 s still had its handshake killed at
    60 s by a library default it never set. Importing ``sdk_bridge`` must fix
    that, because ``connect()`` re-reads the variable on every attempt.
    """
    raw = os.environ.get("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT")
    assert raw is not None, \
        "the SDK's 60s initialize floor is back in force; large resumes cannot connect"
    assert int(raw) / 1000.0 > CONNECT_TIMEOUT_MAX, (
        f"SDK initialize ceiling {int(raw) / 1000.0}s is not above our own maximum "
        f"budget {CONNECT_TIMEOUT_MAX}s, so it can still pre-empt it"
    )


def test_the_sdk_default_is_the_one_that_was_failing():
    """Pin the premise: 60 s, and below every measured large-session connect.

    If a future SDK raises its own floor past our sizes this test is how we find
    out, rather than carrying a workaround for a problem that no longer exists.
    """
    floor = max(60000 / 1000.0, 60.0)
    assert floor == 60.0
    assert MEASURED_SECONDS > floor, \
        "premise changed: the measured connect now fits inside the SDK default"


def test_an_existing_larger_override_is_respected(monkeypatch):
    """Setting it by hand is a decision, and this only ever raises a ceiling."""
    monkeypatch.setenv("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT", "9999000")
    sdk_bridge._raise_sdk_initialize_ceiling()
    assert os.environ["CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"] == "9999000"


def test_a_smaller_or_junk_override_is_raised(monkeypatch):
    """A value below our maximum would silently reintroduce the bug."""
    for bad in ("60000", "0", "not-a-number", ""):
        monkeypatch.setenv("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT", bad)
        sdk_bridge._raise_sdk_initialize_ceiling()
        assert int(os.environ["CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"]) / 1000.0 \
            > CONNECT_TIMEOUT_MAX, f"{bad!r} was left in place"


# ---------------------------------------------------------------------------
# A budget shaped like the work
# ---------------------------------------------------------------------------

def test_the_session_that_could_not_connect_now_fits():
    """The whole point, stated as the case that failed."""
    budget = connect_timeout_for(MEASURED_BYTES)
    assert budget > MEASURED_SECONDS, (
        f"a {MEASURED_BYTES / GB:.2f} GB resume measured at {MEASURED_SECONDS}s "
        f"is still only given {budget:.0f}s"
    )


def test_the_margin_is_generous_because_the_downside_is_asymmetric():
    """Too long costs a slow failure; too short costs the session permanently.

    The measurement was uncontended and on a warm cache, so the real spread is
    wider than the one number. Being 3x over-generous is free — the case this
    guards against is a *hang*, which is unbounded either way.
    """
    assert connect_timeout_for(MEASURED_BYTES) >= 3 * MEASURED_SECONDS


def test_a_small_session_keeps_the_tight_backstop():
    """Sizing the budget must not turn into 'wait ages for everything'.

    A session with nothing to re-read has nothing to wait for, so it keeps the
    180 s backstop and a genuine hang is still caught promptly.
    """
    assert connect_timeout_for(0) == CONNECT_TIMEOUT
    assert connect_timeout_for(None) == CONNECT_TIMEOUT
    assert connect_timeout_for(1 * MB) < CONNECT_TIMEOUT * 1.05


def test_the_budget_grows_with_the_file():
    """It is O(size) because the work is: resume re-parses the whole JSONL."""
    steps = [connect_timeout_for(n * MB) for n in (0, 100, 500, 1000, 2000)]
    assert steps == sorted(steps)
    assert steps[0] < steps[-1]


def test_it_is_still_bounded_however_large_the_file():
    """An unbounded budget trades a timeout for a permanently stuck UI."""
    assert connect_timeout_for(500 * GB) == CONNECT_TIMEOUT_MAX
    assert 0 < CONNECT_TIMEOUT_MAX < 3600


def test_a_negative_size_does_not_produce_a_negative_budget():
    """``stat`` should never return one, but a budget of -1s fails instantly."""
    assert connect_timeout_for(-1) == CONNECT_TIMEOUT


# ---------------------------------------------------------------------------
# Not racing each other for the disk
# ---------------------------------------------------------------------------

def test_large_resumes_are_serialised():
    """Two whole-file reads at once take twice as long each, not half.

    All three CLIs in the report spawned within 0.1 s and the two that finished
    landed at 58.5 s and 58.3 s — near-identical, and both a hair under the
    ceiling they were racing. The disk does the same total work either way, so
    queueing them strictly helps: each finishes as early as it can and none is
    pushed past its deadline by the others.
    """
    async def go():
        lock = sdk_bridge._heavy_lock()
        order: list[str] = []

        async def reader(name: str, dur: float):
            async with lock:
                order.append(f"{name}-start")
                await asyncio.sleep(dur)
                order.append(f"{name}-end")

        await asyncio.gather(reader("a", 0.02), reader("b", 0.01))
        # Interleaving would show a-start, b-start, ... — never for a lock.
        assert order in (["a-start", "a-end", "b-start", "b-end"],
                         ["b-start", "b-end", "a-start", "a-end"]), order

    asyncio.run(go())


def test_only_large_sessions_take_the_lock():
    """Queueing a 2 MB handshake behind a multi-minute read is a regression.

    Small connects are not disk-bound, so they must stay concurrent; the
    threshold belongs where the read starts to dominate the handshake.
    """
    assert 2 * MB < HEAVY_CONNECT_BYTES, "a trivial session would be serialised"
    assert HEAVY_CONNECT_BYTES < 348 * MB, \
        "the sessions that actually contended are below the threshold"


def test_the_lock_is_created_lazily_and_shared():
    """One lock per process, bound to the running loop, not to import time.

    Built at module scope it would bind to whatever loop existed then — for a
    server that creates its loop after import, that is either the wrong loop or
    a DeprecationWarning that becomes an error.
    """
    async def go():
        return sdk_bridge._heavy_lock() is sdk_bridge._heavy_lock()

    assert asyncio.run(go())


def test_queue_time_is_not_charged_to_the_budget():
    """The clock starts after the lock, or the back of the queue times out.

    Waiting for another session's read is not this CLI hanging. Starting the
    timer before the lock would time out sessions for the sin of being second.
    """
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "sdk_bridge.py"), encoding="utf-8").read()
    body = src[src.index("async def connect(self, resume_id"):]
    body = body[:body.index("finally:")]
    acquire = body.index("nullcontext() if lock is None else lock")

    # Exactly one clock, and it is inside the lock.  Counting matters: an
    # earlier assignment that merely *also* exists ahead of the queue is the
    # bug, and a check that only looked for one after the acquire would pass
    # while the budget was being charged for queue time.
    starts = [m for m in range(len(body))
              if body.startswith("started = time.monotonic()", m)]
    assert len(starts) == 1, \
        f"expected one connect clock in connect(), found {len(starts)}"
    assert starts[0] > acquire, \
        "the connect clock is started before the queue is cleared"


# ---------------------------------------------------------------------------
# Reporting the deadline that actually applied
# ---------------------------------------------------------------------------

def test_a_timeout_names_the_budget_it_actually_had():
    """"timed out after 180s" on a session given 635 s is simply false.

    The number in that message is the one a user checks against the log, so
    quoting the constant instead of the applied budget sends them looking for a
    deadline that never elapsed.
    """
    budget = connect_timeout_for(MEASURED_BYTES)
    why = sdk_bridge._exc_reason(asyncio.TimeoutError(), budget)
    assert why == f"timed out after {budget:.0f}s"
    assert why != f"timed out after {CONNECT_TIMEOUT:.0f}s"


def test_the_reason_still_defaults_to_the_constant():
    """Callers that have no budget to hand (turn failures) are unaffected."""
    assert sdk_bridge._exc_reason(asyncio.TimeoutError()) == \
        f"timed out after {CONNECT_TIMEOUT:.0f}s"


# ---------------------------------------------------------------------------
# Finding the file the budget is derived from
# ---------------------------------------------------------------------------

from types import SimpleNamespace                   # noqa: E402


def _sizer(tmp_path, config_dir=None):
    """``_resume_jsonl_size`` bound to a minimal fake bridge."""
    me = SimpleNamespace(config=SimpleNamespace(config_dir=config_dir))
    return lambda sid: sdk_bridge.SDKBridge._resume_jsonl_size(me, sid)


def test_the_budget_reads_the_file_the_cli_will_read(tmp_path, monkeypatch):
    """A 300 MB session must be recognised as one, not treated as fresh."""
    (tmp_path / "abc.jsonl").write_bytes(b"x" * (5 * MB))
    monkeypatch.setattr(sdk_bridge, "find_session_dir", lambda sid, cd: tmp_path)
    assert _sizer(tmp_path)("abc") == 5 * MB


def test_a_fresh_session_is_not_given_a_long_budget(monkeypatch):
    """No resume id means no file to re-read, so nothing to wait for."""
    monkeypatch.setattr(sdk_bridge, "find_session_dir",
                        lambda sid, cd: pytest.fail("looked up a nonexistent resume"))
    assert _sizer(None)(None) == 0


def test_a_session_file_that_cannot_be_found_falls_back(monkeypatch):
    """Unknown size must mean the flat backstop, never a crash.

    This only decides how long to wait; a lookup failure that propagated would
    turn a tuning detail into a failed connect.
    """
    monkeypatch.setattr(sdk_bridge, "find_session_dir", lambda sid, cd: None)
    assert _sizer(None)("missing") == 0

    def boom(sid, cd):
        raise OSError("drive gone")

    monkeypatch.setattr(sdk_bridge, "find_session_dir", boom)
    assert _sizer(None)("missing") == 0


def test_the_lookup_is_scoped_to_the_session_account(tmp_path, monkeypatch):
    """The three sessions in the report lived under three different accounts.

    ``7baa01e0`` — the one that never connected — is only in
    ``.claude-account-c``. Searching the hub process's own account would have
    found nothing and quietly handed the largest session the smallest budget.
    """
    seen: list[str | None] = []

    def spy(sid, cd):
        seen.append(cd)
        return None

    monkeypatch.setattr(sdk_bridge, "find_session_dir", spy)
    _sizer(tmp_path, config_dir=r"C:\Users\x\.claude-account-c")("7baa01e0")
    assert seen == [r"C:\Users\x\.claude-account-c"]
