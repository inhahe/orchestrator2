"""Regression tests for ``sdk_bridge.cancel_and_join``.

Background: the codebase used to shut helper tasks down with

    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass

which cannot distinguish the *awaited* task's ``CancelledError`` from one aimed at
the awaiting coroutine itself.  Swallowing the latter means a cancelled coroutine
keeps running; under an anyio task group (every Starlette request / WebSocket
handler runs in one) the group re-delivers cancellation on every event-loop
iteration forever, the loop never sleeps, and the process pins one CPU core at
100%.  That was observed in the wild for 60+ CPU-hours (see known-issues.md).

So the contract under test is exactly two rules:
  1. the awaitee's cancellation (and any exception it died of) is absorbed;
  2. a cancellation aimed at *us* still propagates.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdk_bridge import cancel_and_join  # noqa: E402


def test_absorbs_awaitee_cancellation() -> None:
    """Rule 1: cancelling a plain sleeper must not raise out of the helper."""

    async def main() -> None:
        task = asyncio.create_task(asyncio.sleep(30))
        await asyncio.sleep(0)                       # let it start
        await cancel_and_join(task, "sleeper")       # must not raise
        assert task.done() and task.cancelled()

    asyncio.run(main())


def test_absorbs_awaitee_exception() -> None:
    """Rule 1, other half: a task that dies of a real error is also absorbed
    (its exception is retrieved, so asyncio emits no 'never retrieved' warning)."""

    async def boom() -> None:
        raise RuntimeError("kaboom")

    async def main() -> None:
        task = asyncio.create_task(boom())
        await asyncio.sleep(0)
        await cancel_and_join(task, "boom")          # must not raise
        assert task.done() and not task.cancelled()
        assert isinstance(task.exception(), RuntimeError)

    asyncio.run(main())


def test_propagates_our_own_cancellation() -> None:
    """Rule 2 — the whole point.  While we are inside cancel_and_join waiting on a
    task that refuses to die, a cancellation aimed at *us* must come out as
    CancelledError, not be swallowed."""

    swallowed = False

    async def stubborn() -> None:
        # Ignores cancellation for a while (~0.5s: long enough that the outer cancel
        # lands while we are still inside the helper's wait, short enough that
        # asyncio.run's own shutdown doesn't sit here for ten seconds).
        for _ in range(50):
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                pass

    async def victim() -> None:
        nonlocal swallowed
        task = asyncio.create_task(stubborn())
        await asyncio.sleep(0)
        await cancel_and_join(task, "stubborn")
        swallowed = True                              # must never be reached
        task.cancel()

    async def main() -> None:
        v = asyncio.create_task(victim())
        await asyncio.sleep(0.05)                     # let it get into the wait
        v.cancel()
        with pytest.raises(asyncio.CancelledError):
            await v
        assert not swallowed, (
            "cancel_and_join swallowed the caller's own cancellation — this is the "
            "100%-CPU anyio spin bug"
        )

    asyncio.run(main())


def test_naive_pattern_would_have_swallowed_it() -> None:
    """Guard the guard: prove the test above actually discriminates, i.e. that the
    old naive pattern really does swallow the caller's cancellation.  If this ever
    starts failing, asyncio semantics changed and the test above proves less than
    it claims."""

    swallowed = False

    async def stubborn() -> None:
        for _ in range(50):
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                pass

    async def victim_naive() -> None:
        nonlocal swallowed
        task = asyncio.create_task(stubborn())
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task                                # the trap
        except (asyncio.CancelledError, Exception):
            pass
        swallowed = True
        task.cancel()

    async def main() -> None:
        v = asyncio.create_task(victim_naive())
        await asyncio.sleep(0.05)
        v.cancel()
        try:
            await v
        except asyncio.CancelledError:
            pass
        assert swallowed, "expected the naive pattern to swallow our cancellation"

    asyncio.run(main())
