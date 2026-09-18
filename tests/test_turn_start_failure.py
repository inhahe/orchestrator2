"""A turn that dies before its first message must still end.

From a report: *"i got 'Turn failed: Cannot write to terminated process (exit
code: 2147483651)' in session OSa, that may have been due to a full drive, but
the problem is that after that it still said 'working' for 16 hours."*

Exit code 2147483651 is 0x80000003, ``STATUS_BREAKPOINT`` — the bundled CLI
aborting, consistent with the drive filling up underneath it. That part is not
ours. What is ours is the state left behind::

    03:09:52,548  run_turn enter: turns=234 ...
    03:09:52,550  run_turn failed: Cannot write to terminated process

Two milliseconds, and **no ``run_turn finally`` line between them**. That block
exists precisely to guarantee this can't happen — its own comment says "ensures
state.busy can never get stuck True" — but ``run_turn`` set ``state.busy``,
``state.turn_started_at`` and ``turn_active`` and then called
``client.query()`` *above* the ``try:``, and ``query()`` on a dead subprocess
raises ``CLIConnectionError`` synchronously. So the one failure that happens
before the turn has read a single message was the one failure the guard didn't
cover.

The visible symptom was the status bar, but ``turn_active`` is the worse half:
while it is set the dispatcher routes every SDK message to ``turn_msg_queue``,
which no longer has a reader. The session is not merely mislabelled, it is deaf.

These tests drive ``run_turn`` with a client that raises where the real one did,
and assert the session is left in the same state a clean turn would leave it.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_agent_sdk import ResultMessage          # noqa: E402
from config import parse_args                       # noqa: E402
from sdk_bridge import SDKBridge                    # noqa: E402
from state import fmt_duration, init_state_from_config   # noqa: E402


class DeadCLIClient:
    """``query()`` fails exactly as the SDK's transport does on a dead process.

    ``subprocess_cli.write()`` raises ``CLIConnectionError`` rather than
    returning an error, so the failure surfaces on the *caller's* stack — which
    is why it landed outside the guard.
    """

    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc or ConnectionError(
            "Cannot write to terminated process (exit code: 2147483651)")
        self.queries = 0

    async def query(self, prompt: str) -> None:
        self.queries += 1
        raise self.exc

    async def interrupt(self) -> None:  # pragma: no cover - unused
        pass


def _bridge(client=None):
    cfg = parse_args([])
    state = init_state_from_config(cfg)
    sent: list[dict] = []

    async def bcast(msg: dict) -> None:
        sent.append(msg)

    br = SDKBridge(config=cfg, state=state, broadcaster=bcast)
    br.client = client if client is not None else DeadCLIClient()
    return br, state, sent


def _kinds(sent: list[dict]) -> list[str]:
    return [m.get("type") for m in sent]


# ---------------------------------------------------------------------------
# The 16-hour "working"
# ---------------------------------------------------------------------------

def test_a_dead_cli_does_not_leave_the_session_working():
    """The reported symptom, directly.

    ``busy`` drives the status bar and, through ``in_bg_wait()``, the bg-done
    bell; ``turn_started_at`` drives the elapsed counter that read 16 hours.
    """
    br, state, _ = _bridge()

    async def go():
        with pytest.raises(Exception):
            await br.run_turn("do the thing")

    asyncio.run(go())

    assert state.busy is False, "session stuck 'working' after a failed turn"
    assert state.turn_started_at is None, "elapsed timer left running"


def test_the_dispatcher_is_not_left_routing_into_a_dead_queue():
    """``turn_active`` set with no ``run_turn`` reading is worse than a bad label.

    ``_message_dispatcher`` checks this flag to decide between
    ``turn_msg_queue`` and ``_handle_async_message``. Left set, every subsequent
    SDK message — including the ones a ghost turn would otherwise render — is
    filed into a queue nobody reads, and the session goes quiet for good.
    """
    br, state, _ = _bridge()

    async def go():
        with pytest.raises(Exception):
            await br.run_turn("do the thing")

    asyncio.run(go())

    assert br.turn_active.is_set() is False
    assert br.turn_msg_queue.empty()


def test_the_failure_still_propagates_to_the_worker():
    """Cleanup must not become suppression.

    ``worker_loop`` catches this to show "Turn failed: …" and to reconnect when
    ``auto_reconnect`` is on. A ``finally`` that swallowed the exception would
    trade a stuck status bar for a silently skipped prompt — and skip the
    reconnect that gets the session working again.
    """
    br, state, _ = _bridge()

    async def go():
        with pytest.raises(Exception) as ei:
            await br.run_turn("do the thing")
        assert "terminated process" in str(ei.value)

    asyncio.run(go())


def test_the_user_is_told_the_turn_ended():
    """A turn that started on screen has to visibly finish.

    ``turn_start`` is broadcast before ``query()``, so the transcript already
    shows a turn in progress. The ``finally``'s synthetic ``turn_end`` plus
    warning is what closes it; without reaching the guard the turn just hung
    there open.
    """
    br, state, sent = _bridge()

    async def go():
        with pytest.raises(Exception):
            await br.run_turn("do the thing")

    asyncio.run(go())

    kinds = _kinds(sent)
    assert "turn_start" in kinds
    assert "turn_end" in kinds
    assert kinds.index("turn_start") < kinds.index("turn_end")
    warnings = [m for m in sent if m.get("subtype") == "warning"]
    assert any("without a result message" in m["data"]["message"] for m in warnings)


def test_a_later_turn_is_unaffected():
    """Recovery, not just tidiness.

    The point of clearing the flags is that the *next* prompt runs normally.
    This is the assertion that would have caught the bug from the user's side:
    after the CLI came back, OSa should have been able to take a prompt.
    """
    client = DeadCLIClient()
    br, state, sent = _bridge(client)

    async def go():
        with pytest.raises(Exception):
            await br.run_turn("first")

        # The CLI is replaced by a healthy one, as a reconnect would do.
        class LiveClient:
            async def query(self, prompt: str) -> None:
                br.turn_msg_queue.put_nowait(ResultMessage(
                    subtype="success", duration_ms=1, duration_api_ms=1,
                    is_error=False, num_turns=1, session_id="sid-1"))

            async def interrupt(self) -> None:  # pragma: no cover
                pass

        br.client = LiveClient()
        await br.run_turn("second")

    asyncio.run(go())

    assert state.busy is False
    assert br.turn_active.is_set() is False


# ---------------------------------------------------------------------------
# Duration reporting
# ---------------------------------------------------------------------------

def test_an_abnormal_turn_reports_a_real_duration():
    """The synthetic ``turn_end`` read ``0:00`` for every failure.

    ``finally`` nulls ``state.turn_started_at`` and then computed the duration
    from it, so ``(None or now) - now`` was always zero. Harmless-looking, but
    it erases the one clue that says whether a turn died instantly (a dead CLI,
    as here) or after twenty minutes of work.
    """
    br, state, sent = _bridge()

    async def go():
        async def slow_query(prompt: str) -> None:
            # Longer than a second: ``fmt_duration`` truncates, so a shorter
            # turn would format identically to zero and prove nothing.
            await asyncio.sleep(1.1)
            raise ConnectionError("Cannot write to terminated process")

        br.client.query = slow_query
        with pytest.raises(Exception):
            await br.run_turn("do the thing")

    asyncio.run(go())

    ends = [m for m in sent if m.get("type") == "turn_end"]
    assert ends, "no turn_end reported"
    assert ends[-1]["duration"] != fmt_duration(0.0), \
        "duration read after the finally cleared turn_started_at"
