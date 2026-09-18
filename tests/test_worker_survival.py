"""The worker task must never die silently — and nothing may cancel it off-task.

From a report: *"in the same session, i manually entered the prompt again while
it was idle since it didn't send, and it didn't do anything. it showed the prompt
coming from me, but stayed idle."*

This is the *second* failure in that session, and a different bug from the
queued-prompt one in ``test_queue_poke``. Here the prompt was routed correctly —
``busy`` and ``connecting`` were both False, so it went to ``event_queue`` via
``_enqueue_prompt`` — and simply was never consumed. A ``sys.remote_exec`` probe
of the live hub found the runtime perfectly healthy in every observable respect::

    "rid": "s5", "busy": false, "connecting": false, "viewers": 1,
    "stop_event": false, "event_queue_size": 3,
    "worker_repr": "<Task cancelled name='sdk-worker' ...>"

The worker task was **cancelled**, while ``stop_event`` — which every real
shutdown sets first — was clear.

The cause is a cross-task anyio cancel scope. ``claude_agent_sdk``'s
``SubprocessCLITransport.connect()`` does::

    self._stderr_task_group = anyio.create_task_group()
    await self._stderr_task_group.__aenter__()

…in whichever task called it (the worker), and its ``close()`` does::

    with suppress(Exception):
        self._stderr_task_group.cancel_scope.cancel()
        await self._stderr_task_group.__aexit__(None, None, None)

…in whichever task calls ``disconnect()``. anyio delivers a scope's
cancellation to the task that *entered* it, so a disconnect from any other task
cancels the worker — and ``suppress(Exception)`` swallows the resulting "cancel
scope exited in a different task" ``RuntimeError``, so the reconnect reports
success. The offending caller was ``api_session_launch``'s
``asyncio.create_task(existing.bridge.reconnect())``.

Three tests below, one per layer of the fix:

* the trigger is gone — the launcher queues ``("connect", "")`` for the worker;
* a foreign teardown is loud rather than silent, so a future offender is
  visible in the log the first time it happens;
* and if one slips through anyway, the worker restarts instead of vanishing.

The third matters most. The queue-poke bug and this one share one property:
every component looked healthy, so nothing in the UI or the log said anything
was wrong. Recovery is what turns "the session is dead forever and nobody knows"
into "one logged blip".
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import suppress

import anyio
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server                                  # noqa: E402
from config import Config                      # noqa: E402
from sdk_bridge import SDKBridge               # noqa: E402
from state import State                        # noqa: E402


def _bridge() -> tuple[SDKBridge, list[dict]]:
    sent: list[dict] = []

    async def broadcaster(msg):
        sent.append(msg)

    return SDKBridge(Config(), State(), broadcaster), sent


def _drain(br: SDKBridge) -> list[tuple[str, str]]:
    out = []
    while not br.event_queue.empty():
        out.append(br.event_queue.get_nowait())
    return out


# ---------------------------------------------------------------------------
# 0. The upstream behaviour this whole file exists because of.
# ---------------------------------------------------------------------------

def test_anyio_scope_cancelled_from_foreign_task_kills_the_host():
    """Pin the SDK/anyio behaviour that caused the outage.

    Not a test of our code — a guard on the assumption underneath the fix. If a
    future anyio or SDK release stops cancelling the host task here, this fails
    and tells us the workaround can be reconsidered. If it keeps passing, the
    routing rule in ``_warn_if_foreign_task`` stays mandatory.
    """
    async def go():
        box: dict = {}
        parked = asyncio.Event()

        async def _idle_child():
            await asyncio.sleep(3600)

        async def host():
            tg = anyio.create_task_group()
            await tg.__aenter__()              # exactly what transport.connect does
            tg.start_soon(_idle_child)
            box["tg"] = tg
            parked.set()
            await asyncio.Queue().get()        # the worker's park point

        host_task = asyncio.create_task(host())
        await parked.wait()
        await asyncio.sleep(0.05)

        # exactly what transport.close() does, from a *different* task:
        with suppress(Exception):
            box["tg"].cancel_scope.cancel()
            await box["tg"].__aexit__(None, None, None)
        await asyncio.sleep(0.05)

        assert host_task.cancelled(), (
            "anyio no longer cancels a cancel scope's host task from a foreign "
            "task — re-evaluate the worker-task routing rule"
        )

    asyncio.run(go())


# ---------------------------------------------------------------------------
# 1. The trigger: the launcher must not reconnect off-worker.
# ---------------------------------------------------------------------------

def test_session_launch_reuse_queues_connect_instead_of_reconnecting():
    """``api_session_launch`` reusing a live session must go through the queue.

    This is the exact call that killed the worker in the report: a second
    ``launch`` for a session already hosted here, carrying a ``--model`` the
    running bridge doesn't have.
    """
    async def go():
        br, _ = _bridge()
        br.state.session_id = "sess-abc"
        br.state.model = "sonnet"

        rt = type("RT", (), {})()
        rt.rid = "s5"
        rt.state = br.state
        rt.bridge = br
        rt.config = br.config

        reconnected = []
        br.reconnect = lambda: reconnected.append(1)   # must never be called

        saved_runtimes = dict(server.runtimes)
        saved_config = server.config
        server.runtimes.clear()
        server.runtimes["s5"] = rt
        server.config = br.config
        try:
            res = await server.api_session_launch(
                {"resume": "sess-abc", "model": "opus"},
            )
        finally:
            server.runtimes.clear()
            server.runtimes.update(saved_runtimes)
            server.config = saved_config

        assert res["reused"] is True
        assert br.state.model == "opus"
        assert not reconnected, "reconnect must not be driven from a foreign task"
        assert ("connect", "") in _drain(br), (
            "the reconnect must be handed to the worker via event_queue"
        )

    asyncio.run(go())


# ---------------------------------------------------------------------------
# 2. The detector: a foreign teardown is never silent again.
# ---------------------------------------------------------------------------

def test_disconnect_off_worker_logs_an_error(caplog):
    """The failure mode was *silence*. Any future offender must announce itself."""
    async def go():
        br, _ = _bridge()
        started = asyncio.Event()

        async def fake_worker():
            started.set()
            await asyncio.sleep(3600)

        br._worker_task = asyncio.create_task(fake_worker(), name="sdk-worker")
        await started.wait()
        try:
            with caplog.at_level(logging.ERROR, logger="sdk_bridge"):
                await br.disconnect()          # from the test's task, not the worker
        finally:
            br._worker_task.cancel()
            await asyncio.wait({br._worker_task})

        assert any("not the worker task" in r.getMessage() for r in caplog.records)

    asyncio.run(go())


def test_disconnect_on_worker_task_is_silent(caplog):
    """The legitimate path (``/connect`` handled inside the worker) stays quiet.

    A guard that cries wolf on the normal case gets ignored, which would cost us
    the one signal this class of bug produces.
    """
    async def go():
        br, _ = _bridge()
        done = asyncio.Event()

        async def worker():
            with caplog.at_level(logging.ERROR, logger="sdk_bridge"):
                await br.disconnect()
            done.set()

        br._worker_task = asyncio.create_task(worker(), name="sdk-worker")
        await asyncio.wait_for(done.wait(), timeout=5)
        assert not [r for r in caplog.records
                    if "not the worker task" in r.getMessage()]

    asyncio.run(go())


def test_shutdown_does_not_trip_the_guard(caplog):
    """``_shutdown`` disconnects off-worker on purpose — after joining the worker.

    It is the one caller allowed to, because by then there is no live worker to
    cancel.  The guard keys on ``_worker_task.done()`` precisely so this stays
    clean.
    """
    async def go():
        br, _ = _bridge()
        started = asyncio.Event()

        async def fake_worker():
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                raise

        br._worker_task = asyncio.create_task(fake_worker(), name="sdk-worker")
        await started.wait()
        with caplog.at_level(logging.ERROR, logger="sdk_bridge"):
            await br.stop()

        assert br.stop_event.is_set()
        assert not [r for r in caplog.records
                    if "not the worker task" in r.getMessage()]

    asyncio.run(go())


# ---------------------------------------------------------------------------
# 3. The safety net: an unexplained cancellation must not end the session.
# ---------------------------------------------------------------------------

def test_worker_restarts_when_cancelled_without_stop_event(caplog):
    """Cancel the worker the way the SDK did, then check the queue still drains.

    The user-visible contract: after the kill, a prompt put on ``event_queue``
    is still picked up. Before the fix it sat there forever behind an "idle"
    status bar.
    """
    async def go():
        br, _ = _bridge()
        seen: list[str] = []
        running = asyncio.Event()

        async def fake_loop(*, skip_connect: bool = False):
            seen.append(f"start(skip_connect={skip_connect})")
            running.set()
            while True:
                kind, payload = await br.event_queue.get()
                seen.append(f"{kind}:{payload}")

        br.worker_loop = fake_loop
        with caplog.at_level(logging.ERROR, logger="sdk_bridge"):
            br._spawn_worker(skip_connect=False)
            await asyncio.wait_for(running.wait(), timeout=5)

            first = br._worker_task
            running.clear()
            first.cancel()                      # stop_event deliberately NOT set
            await asyncio.wait_for(running.wait(), timeout=5)

            br.event_queue.put_nowait(("message", "hello again"))
            await asyncio.sleep(0.05)

        try:
            # It ends *cleanly*, not cancelled: it absorbed the cancellation
            # (uncancel + return) so the replacement isn't born cancelling.
            assert first.done() and not first.cancelled()
            assert br._worker_task is not first, "the worker must be replaced"
            assert not br._worker_task.done()
            assert "message:hello again" in seen, (
                "a restarted worker must consume the queue the dead one abandoned"
            )
            assert any("stop_event clear" in r.getMessage()
                       for r in caplog.records), "the restart must be logged"
        finally:
            br.stop_event.set()
            br._worker_task.cancel()
            await asyncio.wait({br._worker_task})

    asyncio.run(go())


def test_restart_skips_connect_when_the_client_is_still_live():
    """Don't fork a second ``claude.exe`` onto the session on restart.

    A foreign reconnect leaves the SDK connection *up* — it only killed the
    worker.  Re-running the connect phase would open a duplicate CLI on the same
    session, which is the exact hazard ``proc_guard`` exists to stop: two live
    agents editing the same tree.
    """
    async def go():
        br, _ = _bridge()
        br.client = object()                    # a live connection
        calls: list[bool] = []
        running = asyncio.Event()

        async def fake_loop(*, skip_connect: bool = False):
            calls.append(skip_connect)
            running.set()
            await asyncio.sleep(3600)

        br.worker_loop = fake_loop
        br._spawn_worker(skip_connect=False)
        await asyncio.wait_for(running.wait(), timeout=5)

        running.clear()
        br._worker_task.cancel()
        await asyncio.wait_for(running.wait(), timeout=5)

        try:
            assert calls == [False, True]
        finally:
            br.stop_event.set()
            br._worker_task.cancel()
            await asyncio.wait({br._worker_task})

    asyncio.run(go())


def test_restart_clears_stale_busy_and_connecting_flags():
    """A worker killed mid-reconnect leaves ``connecting`` True forever.

    That flag is what routes typed prompts to ``queued_prompts`` instead of the
    event queue, so a stuck True turns *every* later prompt invisible — the
    queue-poke bug's symptom, arriving by this new route.  The restart must
    reset both flags.
    """
    async def go():
        br, _ = _bridge()
        running = asyncio.Event()

        async def fake_loop(*, skip_connect: bool = False):
            running.set()
            await asyncio.sleep(3600)

        br.worker_loop = fake_loop
        br._spawn_worker(skip_connect=False)
        await asyncio.wait_for(running.wait(), timeout=5)

        br.state.busy = True
        br.state.connecting = True
        running.clear()
        br._worker_task.cancel()
        await asyncio.wait_for(running.wait(), timeout=5)

        try:
            assert br.state.busy is False
            assert br.state.connecting is False
        finally:
            br.stop_event.set()
            br._worker_task.cancel()
            await asyncio.wait({br._worker_task})

    asyncio.run(go())


def test_stop_event_cancellation_still_ends_the_worker():
    """A real shutdown must still stop. The restart only covers the *unexplained* case."""
    async def go():
        br, _ = _bridge()
        starts: list[int] = []
        running = asyncio.Event()

        async def fake_loop(*, skip_connect: bool = False):
            starts.append(1)
            running.set()
            await asyncio.sleep(3600)

        br.worker_loop = fake_loop
        br._spawn_worker(skip_connect=False)
        await asyncio.wait_for(running.wait(), timeout=5)

        br.stop_event.set()
        task = br._worker_task
        task.cancel()
        await asyncio.wait({task})

        assert task.cancelled()
        assert br._worker_task is task, "no restart after a legitimate shutdown"
        assert len(starts) == 1

    asyncio.run(go())


def test_end_to_end_foreign_reconnect_no_longer_strands_a_prompt():
    """The whole report, reproduced: kill the worker the SDK's way, then type.

    ``_stderr_task_group`` is entered inside the worker and cancelled from a
    foreign task, mimicking ``transport.close()``.  With the safety net the
    prompt that follows is consumed; without it, this is the outage.
    """
    async def go():
        br, _ = _bridge()
        seen: list[str] = []
        entered = asyncio.Event()
        running = asyncio.Event()
        box: dict = {}

        async def _idle_child():
            await asyncio.sleep(3600)

        async def fake_loop(*, skip_connect: bool = False):
            if not skip_connect:
                tg = anyio.create_task_group()
                await tg.__aenter__()
                tg.start_soon(_idle_child)
                box["tg"] = tg
                entered.set()
            running.set()
            while True:
                kind, payload = await br.event_queue.get()
                seen.append(f"{kind}:{payload}")

        br.worker_loop = fake_loop
        br._spawn_worker(skip_connect=False)
        await asyncio.wait_for(entered.wait(), timeout=5)
        await asyncio.wait_for(running.wait(), timeout=5)

        running.clear()
        with suppress(Exception):                     # exactly transport.close()
            box["tg"].cancel_scope.cancel()
            await box["tg"].__aexit__(None, None, None)

        await asyncio.wait_for(running.wait(), timeout=5)
        assert not br.stop_event.is_set()

        br.event_queue.put_nowait(
            ("message", "i tried release.bat and it says the version hasn't changed"),
        )
        await asyncio.sleep(0.05)

        try:
            assert any("release.bat" in s for s in seen), (
                "the prompt after a foreign SDK disconnect must still be consumed"
            )
        finally:
            br.stop_event.set()
            br._worker_task.cancel()
            await asyncio.wait({br._worker_task})

    asyncio.run(go())
