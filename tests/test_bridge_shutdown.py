"""Shutdown must kill the CLI subprocess — under cancellation, and for real.

A surviving ``claude.exe`` is not a stray handle.  It is a live agent, still
resumed on its session, still able to edit files and commit, and invisible to
the hub that spawned it: its runtime has already been dropped from
``runtimes``, so nothing lists it and the launch-time duplicate check cannot
see it.  A hub up for one day accumulated 19 of them, several resuming the
*same* session file — two agents on one conversation, which is precisely the
hazard :mod:`proc_guard` exists to prevent.

The bug that caused it is covered in ``test_multisession.py`` (the idle-timer
task cancelled itself out of its own teardown).  These tests cover the two
guarantees added so the same class of bug cannot cost a subprocess again:

1. ``stop()`` is shielded — a cancellation aimed at the caller does not skip
   the end of the shutdown sequence, which is where the CLI actually dies.
2. ``disconnect()`` *verifies* the CLI is gone instead of trusting the SDK to
   have killed it, and kills it itself when it isn't.

These use a real child process rather than a mocked ``psutil``: the thing under
test is "did the process actually die", and a fake that reports death is a fake
that cannot fail the way production did.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdk_bridge import SDKBridge  # noqa: E402


def make_bridge() -> SDKBridge:
    """An SDKBridge with no SDK behind it — enough for the shutdown path."""
    cfg = SimpleNamespace(cwd=".", permission_mode="default", config_dir=None)
    state = SimpleNamespace(auth_error=False, background_tasks={},
                            completed_panel_bg={}, session_id=None)

    async def broadcaster(_msg):
        pass

    return SDKBridge(config=cfg, state=state, broadcaster=broadcaster)


class FakeClient:
    """An SDK client whose ``disconnect()`` does *not* kill the subprocess.

    That is the real, observed failure mode: the SDK's teardown is a best
    effort that the bridge used to treat as a guarantee, wrapped in a bare
    ``except Exception: pass`` so a failure left no trace at all.
    """

    def __init__(self, pid: int, *, raises: bool = False) -> None:
        self._transport = SimpleNamespace(_process=SimpleNamespace(pid=pid))
        self.disconnect_called = False
        self._raises = raises

    async def disconnect(self) -> None:
        self.disconnect_called = True
        if self._raises:
            raise RuntimeError("transport already closed")


class StubbornWorker:
    """A worker task that ignores cancellation until it is explicitly released.

    It earns its place twice.  *During* a test it guarantees ``stop()`` is still
    parked in ``cancel_and_join`` when the cancellation lands — without that the
    shutdown races to completion first and the test passes against the
    unshielded code too, which is the shape of a test that proves nothing.

    *After* a test it must become killable again: a task that ignores
    cancellation forever also wedges the final ``gather`` inside
    ``asyncio.run``, and a hung test run reports nothing at all — not even a
    failure.
    """

    def __init__(self) -> None:
        self._released = False
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                if self._released:
                    raise
                # else: refuse to die, like the wedged worker being modelled

    async def release(self) -> None:
        self._released = True
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


@pytest.fixture
def victim():
    """A real, harmless child process standing in for ``claude.exe``.

    Killed by pid — and only this pid — however the test ends.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        yield proc
    finally:
        try:
            psutil.Process(proc.pid).kill()
        except psutil.Error:
            pass
        proc.wait(timeout=10)


async def died(pid: int, timeout: float = 5.0) -> bool:
    """Did *pid* go away within *timeout*?

    A kill is not instantaneous — on Windows ``TerminateProcess`` returns before
    the process object is gone — so asserting on ``pid_exists`` the instant
    ``disconnect()`` returns tests the scheduler, not the shutdown.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if not psutil.pid_exists(pid):
            return True
        await asyncio.sleep(0.05)
    return not psutil.pid_exists(pid)


def test_disconnect_kills_a_cli_the_sdk_left_running(victim):
    """The backstop: SDK said it disconnected, process is still there."""
    async def go():
        br = make_bridge()
        br.client = FakeClient(victim.pid)
        assert psutil.pid_exists(victim.pid)
        await br.disconnect()
        assert br.client is None
        assert await died(victim.pid), (
            "claude.exe survived disconnect() — the bridge trusted the SDK's "
            "teardown instead of checking it"
        )
    asyncio.run(go())


def test_disconnect_kills_the_cli_even_when_the_sdk_raises(victim):
    """A failing ``client.disconnect()`` must not cost us the subprocess.

    This is the path that used to be ``except Exception: pass`` — the one
    combination that leaks silently *and* explains nothing afterwards.
    """
    async def go():
        br = make_bridge()
        client = FakeClient(victim.pid, raises=True)
        br.client = client
        await br.disconnect()          # must not raise
        assert client.disconnect_called
        assert await died(victim.pid)
    asyncio.run(go())


def test_stop_finishes_the_shutdown_even_if_the_caller_is_cancelled(victim):
    """One cancellation, arriving at the first await.

    The expensive part of shutdown (killing the CLI) is at the *end* of the
    sequence, so a ``CancelledError`` arriving at the first await skips exactly
    the part that matters.  ``stop()`` must still re-raise to its caller — the
    cancellation is real — while completing the teardown regardless.

    This much is carried by the ``finally`` around the worker join, and the
    shield is what covers the harder case in the next test.
    """
    async def go():
        import sdk_bridge
        br = make_bridge()
        br.client = FakeClient(victim.pid)

        # A worker that ignores cancellation, so stop() is *guaranteed* to
        # still be suspended at its first await when the cancel lands.
        # Without this the shutdown finishes first and the test proves nothing
        # — it passed against the unshielded code for exactly that reason.
        worker = StubbornWorker()
        br._worker_task = worker.task
        await asyncio.sleep(0)

        original = sdk_bridge.CANCEL_JOIN_TIMEOUT
        sdk_bridge.CANCEL_JOIN_TIMEOUT = 1.0
        try:
            stopper = asyncio.create_task(br.stop())
            await asyncio.sleep(0.05)      # now parked in the join
            assert psutil.pid_exists(victim.pid), "shutdown finished too early"
            stopper.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stopper              # the caller still sees it

            gone = await died(victim.pid, timeout=10.0)
        finally:
            sdk_bridge.CANCEL_JOIN_TIMEOUT = original
            await worker.release()

        assert gone, (
            "cancelling stop()'s caller abandoned the teardown and leaked the "
            "CLI subprocess"
        )
    asyncio.run(go())


def test_stop_survives_a_caller_that_keeps_cancelling(victim):
    """The shield, specifically.

    A ``finally`` protects a shutdown from *one* cancellation: the error has
    already been delivered, so the awaits in the cleanup path run normally.  It
    protects it from no more than one.  A caller that cancels again — a
    ``wait_for`` retrying, a task group tearing its children down, a supervisor
    that has run out of patience — lands the second ``CancelledError`` inside
    ``disconnect()`` itself, between the snapshot and the kill, and that is once
    more a live ``claude.exe`` with nothing left holding a reference to it.

    ``asyncio.shield`` is what makes that impossible: the cancellation reaches
    the caller immediately (it is real, and pretending otherwise would hang
    whoever is shutting us down), while the teardown continues on its own task
    where no further cancel can reach it.
    """
    async def go():
        import sdk_bridge
        br = make_bridge()
        br.client = FakeClient(victim.pid)

        worker = StubbornWorker()
        br._worker_task = worker.task
        await asyncio.sleep(0)

        original = sdk_bridge.CANCEL_JOIN_TIMEOUT
        sdk_bridge.CANCEL_JOIN_TIMEOUT = 1.0
        try:
            stopper = asyncio.create_task(br.stop())
            await asyncio.sleep(0.05)      # parked in the worker join
            assert psutil.pid_exists(victim.pid), "shutdown finished too early"

            # Cancel relentlessly for as long as the caller is still there.
            # Shielded, the first one ends `stopper` and the rest are no-ops.
            # Unshielded, one of them lands inside disconnect().
            for _ in range(300):
                if stopper.done():
                    break
                stopper.cancel()
                await asyncio.sleep(0.005)
            with pytest.raises(asyncio.CancelledError):
                await stopper

            gone = await died(victim.pid, timeout=10.0)
        finally:
            sdk_bridge.CANCEL_JOIN_TIMEOUT = original
            await worker.release()

        assert gone, (
            "a repeated cancellation reached inside the teardown and left the "
            "CLI subprocess running — stop() is not shielded"
        )
    asyncio.run(go())


def test_stop_still_disconnects_when_the_worker_will_not_die(victim):
    """A worker that ignores cancellation must not cost us the subprocess either.

    ``cancel_and_join`` is bounded for this reason; the ``finally`` around it
    makes ``disconnect()`` unconditional.
    """
    async def go():
        import sdk_bridge
        br = make_bridge()
        br.client = FakeClient(victim.pid)

        worker = StubbornWorker()
        br._worker_task = worker.task
        await asyncio.sleep(0)

        original = sdk_bridge.CANCEL_JOIN_TIMEOUT
        sdk_bridge.CANCEL_JOIN_TIMEOUT = 0.2      # keep the test quick
        try:
            await asyncio.wait_for(br.stop(), timeout=15)
        finally:
            sdk_bridge.CANCEL_JOIN_TIMEOUT = original
            await worker.release()

        assert await died(victim.pid), (
            "a wedged worker task blocked shutdown and the CLI outlived it"
        )
    asyncio.run(go())
