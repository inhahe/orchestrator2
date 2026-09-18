"""A CLI that exits must not leave the session permanently unusable.

From a report: *"in my 'OSb' session, I got 'Turn ended without a result
message (CLIConnectionError: Cannot write to terminated process (exit code:
2147483651))' … i closed the window, ran orchestrator2 in that directory again,
told claude to continue, and got the same api error again."*

The log has the whole sequence. The CLI died at 15:03:25 and **we saw it**::

    15:03:25  Fatal error in message reader: Command failed with exit code 2147483651
    15:03:25  dispatcher crashed: gen=1 Command failed with exit code 2147483651
    15:03:25  run_turn failed: SDK dispatcher died mid-turn

and then did nothing, because recovery sat behind ``--auto-reconnect``, which
defaults off. The runtime stayed dead for **5h21m** — status bar reading a
perfectly ordinary "idle" the whole time — until the user typed "Continue" at
20:24:48 and it failed on the first write. Restarting couldn't help: the hub was
still up, so the relaunch reattached to the same runtime holding the same dead
client, and the retry failed identically 36 seconds later.

The distinction these tests pin is between *a turn that failed* and *a session
that can no longer run any turn at all*. The second is not a preference to be
configured; ``self.client`` points at a corpse and no prompt will ever succeed
again. What does need bounding is the opposite case — a CLI that dies on every
connect — which is why recovery has a budget rather than being a retry loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sdk_bridge                                   # noqa: E402
from sdk_bridge import (                            # noqa: E402
    SDKBridge,
    TRANSPORT_DEATH_MAX_RECOVERIES,
    _exc_reason,
    _is_transport_death,
)
from config import parse_args                       # noqa: E402
from state import init_state_from_config            # noqa: E402


# The exact text the SDK's transport raises, from the reported traceback:
# claude_agent_sdk/_internal/transport/subprocess_cli.py:513.
DEAD_WRITE = "Cannot write to terminated process (exit code: 2147483651)"
# ...and the read side, from _internal/query.py's message reader.
DEAD_READ = ("Command failed with exit code 2147483651 (exit code: 2147483651)\n"
             "Error output: Check stderr output for details")


class DeadCLIClient:
    """``query()`` fails the way a write to an exited process does."""

    def __init__(self) -> None:
        self.queries = 0

    async def query(self, prompt: str) -> None:
        self.queries += 1
        raise ConnectionError(DEAD_WRITE)

    async def interrupt(self) -> None:  # pragma: no cover - unused
        pass


def _bridge(client=None, argv=()):
    cfg = parse_args(list(argv))
    state = init_state_from_config(cfg)
    sent: list[dict] = []

    async def bcast(msg: dict) -> None:
        sent.append(msg)

    br = SDKBridge(config=cfg, state=state, broadcaster=bcast)
    br.client = client if client is not None else DeadCLIClient()
    return br, state, sent


def _messages(sent: list[dict]) -> list[str]:
    return [m.get("data", {}).get("message", "")
            for m in sent if m.get("type") == "system_msg"]


# ---------------------------------------------------------------------------
# Telling a dead CLI apart from a bad turn
# ---------------------------------------------------------------------------

def test_both_halves_of_the_pipe_are_recognised():
    """The CLI can die at either end, and only one detector fires each time.

    Which one depends purely on what we happened to be doing: parked, the read
    side raises out of ``receive_messages()``; mid-prompt, the write side raises
    out of ``query()``. Missing either leaves the zombie undetected in exactly
    the situation the other cannot see.
    """
    assert _is_transport_death(ConnectionError(DEAD_WRITE))
    assert _is_transport_death(RuntimeError(DEAD_READ))


def test_an_ordinary_turn_failure_is_not_a_death():
    """Reconnecting on *any* failure would discard a healthy CLI.

    A bug in our own rendering, a bad tool result, a cancelled task — none of
    those mean the subprocess is gone, and tearing the connection down for them
    would turn a recoverable turn error into a lost CLI and a fresh 5–45 s
    handshake. That is what ``--auto-reconnect`` is for, opt-in.
    """
    assert not _is_transport_death(RuntimeError("tool render blew up"))
    assert not _is_transport_death(ValueError("bad block"))
    assert not _is_transport_death(KeyError("session_id"))


def test_a_connection_not_yet_established_is_not_a_death():
    """``CLIConnectionError`` also covers "not connected", which is transient."""
    assert not _is_transport_death(ConnectionError("Not connected. Call connect() first."))


def test_the_reason_is_one_line_and_never_empty():
    """It goes in the transcript, where the SDK's two-line form would wrap.

    And ``str(TimeoutError())`` is ``""`` — the reason the reported failure read
    "SDK connection failed" with nothing after it.
    """
    assert _exc_reason(RuntimeError(DEAD_READ)) == \
        "Command failed with exit code 2147483651 (exit code: 2147483651)"
    assert _exc_reason(RuntimeError("")) == "RuntimeError"
    assert _exc_reason(asyncio.TimeoutError()) != ""


# ---------------------------------------------------------------------------
# The write side: a prompt into a dead CLI
# ---------------------------------------------------------------------------

def test_a_failed_write_marks_the_transport_dead():
    """``run_turn`` must classify, not just propagate.

    Without the flag ``worker_loop`` cannot tell this from any other turn
    failure, which is precisely why it fell through to "await next input" and
    left the session pointing at a corpse.
    """
    br, state, _ = _bridge()

    async def go():
        with pytest.raises(Exception):
            await br.run_turn("do the thing")

    asyncio.run(go())
    assert br._transport_dead is True


def test_a_failed_write_mid_turn_does_not_also_queue_a_connect():
    """``run_turn`` is about to raise and ``worker_loop`` will recover inline.

    Queueing a connect as well would reconnect twice — once from the handler and
    once when the next idle wait drained the event queue — costing a second
    handshake and a second `claude.exe` spawn for one death.
    """
    br, state, _ = _bridge()

    async def go():
        with pytest.raises(Exception):
            await br.run_turn("do the thing")

    asyncio.run(go())
    assert br.event_queue.empty()


# ---------------------------------------------------------------------------
# The read side: the dispatcher notices while nobody is asking
# ---------------------------------------------------------------------------

class DeadReaderClient:
    """``receive_messages()`` raises the way the SDK does on a CLI exit."""

    async def receive_messages(self):
        raise RuntimeError(DEAD_READ)
        yield  # pragma: no cover - makes this an async generator

    async def connect(self) -> None:      # pragma: no cover - unused
        pass

    async def disconnect(self) -> None:   # pragma: no cover - unused
        pass


def test_the_dispatcher_reports_the_death_it_witnesses():
    """The signal that existed at 15:03:25 and was thrown away.

    ``receive_messages()`` raises carrying the CLI's exit code, and this is the
    *only* detector that fires while the session is parked — the state the
    reported session was in for the next five hours. Everything downstream is
    moot if this doesn't happen.
    """
    br, _, _ = _bridge(DeadReaderClient())

    async def go():
        br._dispatcher_gen = 4
        await br._message_dispatcher(gen=4)

    asyncio.run(go())

    assert br._transport_dead is True, \
        "the dispatcher watched the CLI die and said nothing"
    assert br.event_queue.get_nowait() == ("connect", sdk_bridge._CONNECT_TRANSPORT_DEAD)


def test_an_orphan_dispatcher_dying_is_not_this_clients_death():
    """A stale dispatcher from a previous generation says nothing about now.

    ``connect()`` cancels the old dispatcher, and a reconnect that raced one
    would otherwise be told its brand-new CLI is dead — reconnecting again, and
    again, off the corpse of a process we deliberately replaced.
    """
    br, _, _ = _bridge(DeadReaderClient())

    async def go():
        br._dispatcher_gen = 9        # bridge has moved on
        await br._message_dispatcher(gen=4)

    asyncio.run(go())

    assert br._transport_dead is False
    assert br.event_queue.empty()


def test_a_death_while_parked_asks_the_worker_to_reconnect():
    """This is the signal that was available at 15:03 and thrown away.

    It is also the *only* one that fires while the session is idle, so without
    it recovery cannot begin until the user types something — which in the
    report was five hours later.
    """
    br, _, _ = _bridge()

    async def go():
        br._note_transport_death("CLI exited")

    asyncio.run(go())

    assert br._transport_dead is True
    assert br.event_queue.get_nowait() == ("connect", sdk_bridge._CONNECT_TRANSPORT_DEAD)


def test_the_request_is_not_duplicated():
    """Both detectors can fire for one death; that must not mean two connects."""
    br, _, _ = _bridge()

    async def go():
        br._note_transport_death("CLI exited")
        br._note_transport_death("Cannot write to terminated process")

    asyncio.run(go())

    assert br.event_queue.qsize() == 1


def test_a_reconnect_is_not_requested_during_shutdown():
    """Teardown kills the CLI on purpose; reviving it would leak a subprocess."""
    br, _, _ = _bridge()

    async def go():
        br.stop_event.set()
        br._note_transport_death("CLI exited")

    asyncio.run(go())
    assert br.event_queue.empty()


# ---------------------------------------------------------------------------
# Recovery, and its limit
# ---------------------------------------------------------------------------

def test_recovery_reconnects_and_says_so():
    """The user has to learn the session is alive again.

    Silence here is what made the reporter close the window: after "Turn
    failed", the status bar returns to an ordinary-looking "idle" whether the
    session is healthy or dead, so there is nothing on screen to distinguish
    "try again" from "this will never work".
    """
    br, state, sent = _bridge()
    calls: list[int] = []

    async def fake_reconnect():
        calls.append(1)

    br.reconnect = fake_reconnect

    async def go():
        assert await br._recover_dead_transport("CLI exited") is True

    asyncio.run(go())

    assert calls == [1]
    msgs = " | ".join(_messages(sent))
    assert "Reconnecting" in msgs
    assert "Reconnected" in msgs


def test_a_cli_that_dies_every_time_is_given_up_on():
    """Unconditional recovery must not become an unbounded respawn loop.

    Each attempt spawns a fresh `claude.exe`; a CLI that aborts on startup would
    otherwise be restarted forever, which is a worse failure than the zombie
    because it burns the machine as well as the session.
    """
    br, state, sent = _bridge()

    async def fake_reconnect():
        pass

    br.reconnect = fake_reconnect

    async def go():
        results = []
        for _ in range(TRANSPORT_DEATH_MAX_RECOVERIES + 2):
            results.append(await br._recover_dead_transport("CLI exited"))
        return results

    results = asyncio.run(go())

    assert results[:TRANSPORT_DEATH_MAX_RECOVERIES] == \
        [True] * TRANSPORT_DEATH_MAX_RECOVERIES
    assert results[TRANSPORT_DEATH_MAX_RECOVERIES:] == [False, False]

    msgs = " | ".join(_messages(sent))
    assert "Gave up" in msgs
    assert "/connect" in msgs, "the way out must be named"


def test_giving_up_is_scoped_to_a_window(monkeypatch):
    """A CLI that dies twice a day is not the failure the budget is for.

    Without ageing, the third crash in a *week* would be refused as if it were
    the third in a minute, and the session would be dead for no reason.
    """
    br, state, sent = _bridge()
    monkeypatch.setattr(sdk_bridge, "TRANSPORT_DEATH_WINDOW", 0.05)

    async def fake_reconnect():
        pass

    br.reconnect = fake_reconnect

    async def go():
        for _ in range(TRANSPORT_DEATH_MAX_RECOVERIES):
            assert await br._recover_dead_transport("CLI exited") is True
        await asyncio.sleep(0.1)          # the window passes
        assert await br._recover_dead_transport("CLI exited") is True

    asyncio.run(go())


def test_a_failed_reconnect_is_reported_not_swallowed():
    """The old code did ``except Exception: pass``.

    A reconnect that throws leaves the session exactly as dead as before, so
    saying nothing means the user is back to a normal-looking idle bar with no
    reason to suspect anything.
    """
    br, state, sent = _bridge()

    async def fake_reconnect():
        raise RuntimeError("spawn failed")

    br.reconnect = fake_reconnect

    async def go():
        assert await br._recover_dead_transport("CLI exited") is False

    asyncio.run(go())

    msgs = " | ".join(_messages(sent))
    assert "spawn failed" in msgs
    assert "/connect" in msgs


def test_a_manual_connect_clears_the_budget():
    """``/connect`` is the documented way back after we give up.

    If it didn't reset the count it would be refused by the very limit whose
    error message recommends it.
    """
    br, state, sent = _bridge()
    calls: list[str] = []

    async def fake_reconnect():
        calls.append("reconnect")

    br.reconnect = fake_reconnect

    async def go():
        for _ in range(TRANSPORT_DEATH_MAX_RECOVERIES + 1):
            await br._recover_dead_transport("CLI exited")
        assert br._transport_death_times
        # The user runs /connect.
        await br._apply_idle_config_command("connect", "")
        assert br._transport_death_times == []
        # ...and recovery works again afterwards.
        assert await br._recover_dead_transport("CLI exited") is True

    asyncio.run(go())
    assert "reconnect" in calls


# ---------------------------------------------------------------------------
# End to end through the worker's failure handler
# ---------------------------------------------------------------------------

def test_a_dead_cli_is_recovered_without_auto_reconnect():
    """The report, reduced to one assertion.

    ``--auto-reconnect`` defaults off and the reporter did not pass it, so this
    is the difference between a session that comes back and one that answers
    every prompt with the same error until the hub is killed.
    """
    br, state, sent = _bridge()
    assert br.config.auto_reconnect is False, "premise: the default is off"

    recovered: list[str] = []

    async def fake_recover(why):
        recovered.append(why)
        return True

    br._recover_dead_transport = fake_recover

    async def go():
        try:
            await br.run_turn("Continue")
        except Exception as exc:
            # The real handler, as worker_loop calls it.
            await br._handle_turn_failure(exc)

    asyncio.run(go())

    assert recovered, "a dead CLI was left in place with auto_reconnect off"
    assert "terminated process" in recovered[0]
    assert any("Turn failed" in m for m in _messages(sent)), \
        "the failure must still be reported, not silently recovered"


def test_an_ordinary_failure_does_not_reconnect():
    """Recovery is for a corpse, not for every turn that raised.

    Tearing the connection down after, say, a rendering bug would cost a
    healthy CLI and a fresh handshake for nothing. That behaviour is what
    ``--auto-reconnect`` opts into.
    """
    br, state, sent = _bridge()
    calls: list[str] = []

    async def fake_reconnect():
        calls.append("reconnect")

    async def fake_recover(why):
        calls.append("recover")
        return True

    br.reconnect = fake_reconnect
    br._recover_dead_transport = fake_recover

    async def go():
        await br._handle_turn_failure(RuntimeError("tool render blew up"))

    asyncio.run(go())

    assert calls == [], "a healthy CLI was torn down over an ordinary error"
    assert any("tool render blew up" in m for m in _messages(sent))


def test_auto_reconnect_still_widens_it_to_ordinary_failures():
    """The flag keeps its meaning for the case it is actually about."""
    br, state, sent = _bridge(argv=["--auto-reconnect"])
    assert br.config.auto_reconnect is True
    calls: list[str] = []

    async def fake_reconnect():
        calls.append("reconnect")

    br.reconnect = fake_reconnect

    async def go():
        await br._handle_turn_failure(RuntimeError("tool render blew up"))

    asyncio.run(go())
    assert calls == ["reconnect"]


class LiveClient:
    """A CLI that connects and then simply stays up."""

    def __init__(self, options=None) -> None:
        self.options = options

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive_messages(self):
        while True:                       # pragma: no cover - parked
            await asyncio.sleep(3600)
            yield None


def test_a_successful_connect_clears_the_death_flag(monkeypatch):
    """Otherwise the session is dead forever from the first crash.

    ``_transport_dead`` gates recovery *and* suppresses duplicate requests. Left
    set after a good connect, the next genuine death would be silently ignored
    as "already known" — one crash would cost every future recovery.
    """
    monkeypatch.setattr(sdk_bridge, "ClaudeSDKClient",
                        lambda options=None: LiveClient(options))
    br, state, _ = _bridge()

    async def go():
        br._note_transport_death("CLI exited")
        assert br._transport_dead is True
        await br.connect()
        assert br._transport_dead is False, \
            "a live CLI is still reported dead; recovery is now a one-shot"
        await sdk_bridge.cancel_and_join(br._dispatcher_task, "test dispatcher")

    asyncio.run(go())


def test_the_connect_flag_is_cleared_only_by_a_real_connect():
    """A failed reconnect must leave the session still known-dead.

    Clearing it optimistically in ``reconnect()`` would make the *next*
    detection look like a first death, restarting the budget every time and
    turning the give-up limit into an infinite loop.
    """
    br, state, _ = _bridge()

    async def go():
        br._note_transport_death("CLI exited")
        assert br._transport_dead is True

        async def failing_reconnect():
            raise RuntimeError("nope")

        br.reconnect = failing_reconnect
        await br._recover_dead_transport("CLI exited")
        assert br._transport_dead is True, \
            "a failed reconnect reported the transport as live again"

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Keeping the dead CLI's crash report
# ---------------------------------------------------------------------------
#
# Recovering fixes the session; it does not explain the crash. The explanation
# exists — exit code 2147483651 is 0x80000003, which for a Bun standalone
# executable like ``claude.exe`` is its panic handler, and one earlier death did
# get its report into our log::
#
#     [cli stderr] oh no: Bun has crashed. This indicates a bug in Bun, not your code.
#     [cli stderr] https://bun.report/1.3.13/e_11b55f1dmgggEuhogC2qj71Co1/...
#
# The 2026-08-25 death logged *nothing*, though the callback was wired the whole
# time. The SDK raises ``ProcessError`` as soon as stdout closes and ``wait()``
# returns non-zero — with the placeholder "Check stderr output for details" and
# without draining stderr — and its ``close()`` then cancels the stderr reader's
# task group, discarding every line it hadn't reached. Recovering *tightens*
# that race, because it calls ``close()`` sooner than a human would; without the
# drain below, the fix above would destroy the evidence for the bug it recovers
# from.

def test_a_crash_report_still_in_the_pipe_is_not_cut_off():
    """The drain must wait even when nothing has arrived yet.

    This is the exact shape of the lost report: at the moment we detect the
    death our tail is empty — not because the CLI was silent, but because its
    several-KB panic dump is sitting unread in the pipe. Returning immediately
    on an empty tail would reproduce the original bug inside its own fix.
    """
    br, state, _ = _bridge()

    async def go():
        async def late_panic():
            # The SDK's reader task getting its turn, just after we start.
            await asyncio.sleep(0.05)
            for line in ("oh no: Bun has crashed. This indicates a bug in Bun,"
                         " not your code.",
                         "https://bun.report/1.3.13/e_11b55f1dmgggEuhogC2qj71Co1/"):
                br._on_sdk_stderr(line)

        task = asyncio.ensure_future(late_panic())
        await br._drain_stderr()
        # Snapshot *at the moment the drain returns*. Reading the tail after
        # awaiting the writer would pass even if the drain returned instantly,
        # which is the whole failure being tested for.
        seen = list(br._stderr_tail)
        await task
        return seen

    tail = asyncio.run(go())

    assert any("Bun has crashed" in ln for ln in tail), \
        "the drain returned before the crash report was read"
    assert any("bun.report" in ln for ln in tail), \
        "the report URL — the only actionable part — was cut off"


def test_lines_arriving_during_the_drain_extend_it():
    """A report arrives as a burst of lines, not all at once.

    A fixed sleep would truncate it mid-dump; the window has to restart on each
    line so the whole report is either read or hits the hard cap.
    """
    br, state, _ = _bridge()

    async def go():
        async def dribble():
            for i in range(6):
                await asyncio.sleep(sdk_bridge.STDERR_DRAIN_QUIET * 0.4)
                br._on_sdk_stderr(f"panic line {i}")

        task = asyncio.ensure_future(dribble())
        await br._drain_stderr()
        seen = list(br._stderr_tail)   # see the note in the test above
        await task
        return seen

    tail = asyncio.run(go())
    assert len(tail) == 6, \
        f"drain stopped mid-report after {len(tail)} of 6 lines: {tail}"


def test_the_drain_cannot_park_the_recovery():
    """A CLI spewing stderr forever must not postpone the reconnect forever.

    The session is unusable until we reconnect, so evidence-gathering is
    strictly subordinate to it: the extension above is capped.
    """
    br, state, _ = _bridge()

    async def go():
        stop = asyncio.Event()

        async def firehose():
            while not stop.is_set():
                br._on_sdk_stderr("still talking")
                await asyncio.sleep(0.01)

        task = asyncio.ensure_future(firehose())
        started = time.monotonic()
        # wait_for, not a bare await: without the cap the drain never returns
        # at all, and a test that hangs forever reports nothing.
        timed_out = False
        try:
            await asyncio.wait_for(br._drain_stderr(),
                                   sdk_bridge.STDERR_DRAIN_MAX + 1.0)
        except (asyncio.TimeoutError, TimeoutError):
            timed_out = True
        elapsed = time.monotonic() - started
        stop.set()
        await task
        return timed_out, elapsed

    timed_out, elapsed = asyncio.run(go())
    assert not timed_out, \
        "drain never returned against a continuous stderr stream"
    assert elapsed < sdk_bridge.STDERR_DRAIN_MAX + 1.0, \
        f"drain ran {elapsed:.1f}s against a continuous stderr stream"


def test_the_tail_is_collected_before_the_transport_is_closed():
    """Ordering is the entire fix: ``close()`` cancels the stderr reader.

    Draining after ``disconnect()`` would be draining a stream that has already
    been cancelled — which is what the SDK does today, and why the report was
    lost. This pins the drain to *before* the reconnect.
    """
    br, state, _ = _bridge()
    order: list[str] = []

    async def fake_reconnect():
        order.append("reconnect")

    async def fake_drain():
        order.append("drain")

    br.reconnect = fake_reconnect
    br._drain_stderr = fake_drain

    async def go():
        await br._recover_dead_transport("CLI exited")

    asyncio.run(go())
    assert order == ["drain", "reconnect"], \
        f"stderr was drained after the transport closed: {order}"


def test_giving_up_still_collects_the_crash_report():
    """The run of deaths that exhausts the budget is the one worth explaining.

    Collecting evidence only on the recovering path would mean the repeated
    crashes — the case where something is genuinely wrong with the CLI — are
    exactly the ones that go unrecorded.
    """
    br, state, _ = _bridge()
    drains: list[int] = []

    async def fake_drain():
        drains.append(1)

    async def fake_reconnect():
        pass

    br._drain_stderr = fake_drain
    br.reconnect = fake_reconnect

    async def go():
        for _ in range(TRANSPORT_DEATH_MAX_RECOVERIES + 1):
            await br._recover_dead_transport("CLI exited")

    asyncio.run(go())
    assert len(drains) == TRANSPORT_DEATH_MAX_RECOVERIES + 1, \
        "the death that ran out of budget was not post-mortemed"


def test_the_tail_is_logged_as_one_record():
    """Twenty separate log lines don't survive being found six hours later.

    The point of the tail is that the *death* line carries the explanation, so
    reading it needs no timestamp correlation against a 365k-line file.
    """
    br, state, _ = _bridge()
    br._on_sdk_stderr("oh no: Bun has crashed.")
    br._on_sdk_stderr("https://bun.report/1.3.13/abc/")

    with caplog_at_error() as records:
        br._log_stderr_tail("CLI exited")

    blob = "\n".join(records)
    assert "Bun has crashed" in blob and "bun.report" in blob, \
        "the post-mortem doesn't contain the crash report"
    # One *record* holding the whole report — not merely one record per line,
    # which is what log rotation and grep-by-timestamp lose.
    assert any("Bun has crashed" in r and "bun.report" in r for r in records), \
        f"the tail was split across {len(records)} records instead of one block"


def test_silence_is_recorded_as_silence():
    """"No stderr" and "we never looked" must not read the same in the log.

    If a future crash really does say nothing, that narrows the cause; leaving
    the death line bare would make it indistinguishable from this bug recurring.
    """
    br, state, _ = _bridge()

    with caplog_at_error() as records:
        br._log_stderr_tail("CLI exited")

    blob = "\n".join(records)
    assert "nothing" in blob.lower(), \
        "a silent crash left no record that it was silent"


class caplog_at_error:
    """Minimal log capture — pytest's ``caplog`` fixture can't be nested here."""

    def __init__(self) -> None:
        self.records: list[str] = []

    def __enter__(self) -> list[str]:
        outer = self

        class _H(logging.Handler):
            def emit(self, record):
                outer.records.append(record.getMessage())

        self._h = _H()
        self._log = sdk_bridge.log
        self._log.addHandler(self._h)
        return self.records

    def __exit__(self, *exc) -> None:
        self._log.removeHandler(self._h)


def test_a_new_cli_does_not_inherit_the_old_ones_dying_words(monkeypatch):
    """Otherwise the next death reports the *previous* process's crash.

    The tail is evidence about one subprocess. Carried across a reconnect it
    becomes actively misleading — a healthy CLI that later dies quietly would be
    logged with the panic report of the one it replaced.
    """
    monkeypatch.setattr(sdk_bridge, "ClaudeSDKClient",
                        lambda options=None: LiveClient(options))
    br, state, _ = _bridge()
    br._on_sdk_stderr("oh no: Bun has crashed.")
    assert br._stderr_tail

    async def go():
        await br.connect()
        await sdk_bridge.cancel_and_join(br._dispatcher_task, "test dispatcher")

    asyncio.run(go())
    assert not br._stderr_tail, \
        "the new subprocess inherited the corpse's stderr"
