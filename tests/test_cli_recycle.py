"""Recycling the bundled ``claude.exe`` to bound its per-turn memory leak.

Measured 2026-09-01 on the host running five orchestrator sessions::

    commit charged   254.60 GiB      of a 255.95 GiB limit
      in RAM          49.29 GiB
      in pagefile     20.60 GiB
      NOWHERE        184.71 GiB      <- reserved, never touched

    by image        procs   private     working set    ratio
      claude            5   173.57 GiB    12.21 GiB     14.2x
      chrome           45     7.26 GiB     6.12 GiB      1.19x
      python           20     1.34 GiB     0.40 GiB      3.3x

Sampling the same processes across a 25 s idle gap gave flat-to-negative deltas
for the idle ones and **+21.85 MiB for the one taking a turn**, which places the
growth in per-turn work inside Anthropic's CLI — not in this orchestrator (the
parent python.exe held 0.55 GiB after 88.9 h) and not in the browser.

Windows does not overcommit, so that charge is deducted from a *system-wide*
limit even though nothing is stored: it is what refused a 6.95 GiB render
allocation on a box with 20.97 GiB of RAM free.

We cannot fix the leak upstream, but a process's leak is bounded by its
lifetime, so we end the lifetime: disconnect, then reconnect with
``--resume <session>``.  That is the same path ``/model`` and ``/effort`` take
on every use, so the conversation, session id, history and cwd all survive.

What these tests pin is **when** that is allowed to happen, because a recycle at
the wrong moment is not a slow session, it is lost work:

* mid-turn, the in-flight tool result is discarded;
* with a background task running, the CLI's task registry
  (``AppStateStore``'s ``tasks: {}``) is in-memory and ``--resume`` does not
  rehydrate it, so the completion notification and the ``TaskOutput`` handle are
  gone (the OS processes survive — they are in no job object — so this costs
  bookkeeping rather than the work itself, but it is still a loss);
* every turn, on a session whose *honest* size already exceeds the limit, it
  would pay a full multi-minute resume to reclaim nothing.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import commands                                     # noqa: E402
import proc_guard                                   # noqa: E402
import sdk_bridge                                   # noqa: E402
from sdk_bridge import SDKBridge                    # noqa: E402
from config import SLASH_COMMANDS, parse_args       # noqa: E402
from state import init_state_from_config, state_to_status_dict  # noqa: E402

GIB = 1024 ** 3
STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "static")


class IdleClient:
    """A connected CLI that does nothing — enough to look alive."""

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass


def _bridge(argv=(), *, mem=None):
    cfg = parse_args(list(argv))
    state = init_state_from_config(cfg)
    sent: list[dict] = []

    async def bcast(msg: dict) -> None:
        sent.append(msg)

    br = SDKBridge(config=cfg, state=state, broadcaster=bcast)
    br.client = IdleClient()

    reconnects: list[int] = []

    async def fake_reconnect():
        reconnects.append(1)

    br.reconnect = fake_reconnect

    if mem is not None:
        async def fake_measure():
            state.cli_mem = mem
            return mem

        br._measure_cli_memory = fake_measure

    return br, state, sent, reconnects


def _messages(sent: list[dict]) -> str:
    return " | ".join(m.get("data", {}).get("message", "")
                      for m in sent if m.get("type") == "system_msg")


# ---------------------------------------------------------------------------
# The measurement itself
# ---------------------------------------------------------------------------

def test_the_counter_read_is_the_one_that_shows_the_leak():
    """Working set cannot see this leak; private/commit is the only witness.

    The claude processes held 14.2x more private bytes than working set, and
    the excess was in neither RAM nor the pagefile. A gauge reading RSS would
    have shown 2.4 GiB and reported the machine healthy while it was 1.35 GiB
    from refusing every allocation on it.
    """
    val = proc_guard.private_bytes(os.getpid())
    assert isinstance(val, int) and val > 0

    if sys.platform == "win32":
        import psutil
        mi = psutil.Process(os.getpid()).memory_info()
        assert val == mi.private, "not reading PrivateUsage"


def test_an_unreadable_process_measures_none_not_zero():
    """Zero would read as "the CLI uses no memory" and silence the gauge.

    Worse for the policy: it is also below every threshold, so a probe failure
    would look exactly like a healthy CLI forever.
    """
    assert proc_guard.private_bytes(0) is None
    assert proc_guard.private_bytes(-1) is None
    # A pid that cannot exist on any platform we run on.
    assert proc_guard.private_bytes(2 ** 31 - 1) is None


def test_a_probe_failure_never_escapes_into_the_turn(monkeypatch):
    """This runs at the end of every turn; it must not be able to break one."""
    br, state, _, _ = _bridge()

    def boom(pid):
        raise RuntimeError("psutil exploded")

    monkeypatch.setattr(proc_guard, "private_bytes", boom)
    br._cli_pid = lambda: 1234

    assert asyncio.run(br._measure_cli_memory()) is None


def test_no_pid_means_no_measurement():
    """``_cli_pid`` reaches through SDK-private attributes and may return None."""
    br, state, _, _ = _bridge()
    br._cli_pid = lambda: None
    assert asyncio.run(br._measure_cli_memory()) is None
    assert state.cli_mem is None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_recycling_is_on_by_default():
    """The leak is upstream and unconditional, so the workaround is too.

    An opt-in flag would mean the machine keeps filling for every user who
    hasn't read the release notes — which is the whole population.
    """
    cfg = parse_args([])
    assert cfg.cli_recycle_at == 8.0
    assert cfg.cli_recycle_cooldown == 600


@pytest.mark.parametrize("argv,expected", [
    (["--no-cli-recycle"], 0.0),
    (["--cli-recycle-at", "0"], 0.0),
    (["--cli-recycle-at", "12.5"], 12.5),
    (["--cli-recycle-at", "-3"], 0.0),          # clamped, not negative-enabled
])
def test_the_flag_sets_the_threshold(argv, expected):
    assert parse_args(argv).cli_recycle_at == expected


def test_disabled_means_never():
    """``--no-cli-recycle`` has to survive a CLI far over any threshold."""
    br, state, sent, reconnects = _bridge(["--no-cli-recycle"], mem=99 * GIB)
    assert asyncio.run(br._maybe_recycle_cli()) is False
    assert reconnects == []


def test_the_runtime_override_wins_over_the_flag():
    """``/recycle 20`` must beat ``--cli-recycle-at 8`` for the rest of the run.

    Same shape as ``_max_context_tokens`` / ``_compact_at``: the flag is the
    startup default, the command is the live value.
    """
    br, state, _, _ = _bridge(["--cli-recycle-at", "8"])
    assert br._cli_recycle_limit() == 8.0
    state.cli_recycle_at = 20.0
    assert br._cli_recycle_limit() == 20.0
    state.cli_recycle_at = 0.0
    assert br._cli_recycle_limit() == 0.0
    state.cli_recycle_at = None                  # back to the flag
    assert br._cli_recycle_limit() == 8.0


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------

def test_a_small_cli_is_left_alone():
    br, state, sent, reconnects = _bridge(["--cli-recycle-at", "8"], mem=2 * GIB)
    state.cli_mem_baseline = 1 * GIB
    assert asyncio.run(br._maybe_recycle_cli()) is False
    assert reconnects == []


def test_a_leaked_cli_is_replaced_and_the_user_is_told():
    """The session visibly reconnects, so silence here reads as a hang.

    A resume can take minutes on a large transcript; without a message the tab
    just sits in "reconnecting" with no stated cause.
    """
    br, state, sent, reconnects = _bridge(["--cli-recycle-at", "8"], mem=9 * GIB)
    state.cli_mem_baseline = 2 * GIB

    assert asyncio.run(br._maybe_recycle_cli()) is True
    assert reconnects == [1]
    msg = _messages(sent)
    assert "Recycling" in msg
    assert "9.0 GiB" in msg, "the number that triggered it is not shown"
    assert "conversation is unaffected" in msg, \
        "a surprise reconnect with no reassurance reads as data loss"
    assert state.cli_recycles == 1
    assert state.cli_recycled_at


def test_an_honestly_large_session_does_not_recycle_every_turn():
    """The thrash guard, and the reason the baseline is recorded at all.

    A 1.29 GB transcript can put a *fresh* CLI over any threshold worth setting.
    Without this check every single turn would end in a full resume — minutes of
    ``connect_timeout_for()`` budget — to reclaim nothing at all, because the
    replacement lands at the same size.
    """
    br, state, sent, reconnects = _bridge(["--cli-recycle-at", "8"], mem=9 * GIB)
    state.cli_mem_baseline = int(8.5 * GIB)      # only 0.5 GiB of growth

    assert asyncio.run(br._maybe_recycle_cli()) is False
    assert reconnects == []


def test_an_unknown_baseline_does_not_block_the_first_recycle():
    """A probe that failed at connect must not disable the policy outright."""
    br, state, sent, reconnects = _bridge(["--cli-recycle-at", "8"], mem=40 * GIB)
    state.cli_mem_baseline = None

    assert asyncio.run(br._maybe_recycle_cli()) is True
    assert reconnects == [1]


def test_an_unmeasurable_cli_is_never_recycled_on_a_guess():
    """No reading is not a reason to throw away a working subprocess."""
    br, state, sent, reconnects = _bridge(["--cli-recycle-at", "8"])

    async def no_reading():
        return None

    br._measure_cli_memory = no_reading
    assert asyncio.run(br._maybe_recycle_cli()) is False
    assert reconnects == []


def test_the_cooldown_bounds_the_cost():
    """Resuming is not free, so the fix must not become the expense.

    ``connect_timeout_for()`` budgets minutes for a multi-hundred-MB session;
    a policy that fired on consecutive turns would spend more time reconnecting
    than answering.
    """
    br, state, sent, reconnects = _bridge(
        ["--cli-recycle-at", "8", "--cli-recycle-cooldown", "600"], mem=40 * GIB)
    state.cli_mem_baseline = 2 * GIB

    async def go():
        assert await br._maybe_recycle_cli() is True
        # Still huge (the stub never shrinks), but far too soon.
        assert await br._maybe_recycle_cli() is False

    asyncio.run(go())
    assert reconnects == [1]


def test_the_cooldown_expires():
    br, state, sent, reconnects = _bridge(
        ["--cli-recycle-at", "8", "--cli-recycle-cooldown", "0"], mem=40 * GIB)
    state.cli_mem_baseline = 2 * GIB

    async def go():
        assert await br._maybe_recycle_cli() is True
        assert await br._maybe_recycle_cli() is True

    asyncio.run(go())
    assert reconnects == [1, 1]


# ---------------------------------------------------------------------------
# The safety conditions — the part that protects work in flight
# ---------------------------------------------------------------------------

def test_a_running_background_task_defers_the_recycle():
    """The single most important condition here.

    The CLI's background-task registry is built empty on startup
    (``AppStateStore``: ``tasks: {}``) and ``--resume`` does not rehydrate it,
    so a recycle silently orphans everything still running: no completion
    notification, no ``TaskOutput``, no ``Monitor``. The leak is slow — 0.08 to
    0.65 GiB/h — so waiting costs nothing next to that.
    """
    br, state, sent, reconnects = _bridge(["--cli-recycle-at", "8"], mem=99 * GIB)
    state.cli_mem_baseline = 1 * GIB
    state.background_tasks["t1"] = {"name": "render", "status": "running"}

    assert asyncio.run(br._maybe_recycle_cli()) is False
    assert reconnects == []


def test_a_turn_in_flight_defers_the_recycle():
    """Mid-turn the in-flight tool result would be discarded outright."""
    br, state, sent, reconnects = _bridge(["--cli-recycle-at", "8"], mem=99 * GIB)
    state.cli_mem_baseline = 1 * GIB
    br.turn_active.set()

    assert asyncio.run(br._maybe_recycle_cli()) is False
    assert reconnects == []


def test_a_queued_prompt_defers_the_recycle():
    """The user is mid-thought; a multi-minute resume is not the answer."""
    br, state, sent, reconnects = _bridge(["--cli-recycle-at", "8"], mem=99 * GIB)
    state.cli_mem_baseline = 1 * GIB
    state.queued_prompts.append("carry on")

    assert asyncio.run(br._maybe_recycle_cli()) is False
    assert reconnects == []


def test_a_dead_or_absent_transport_is_not_recycled():
    """There is nothing to reclaim, and the dead-CLI path owns this case.

    Racing it would mean two reconnects for one death, and would restart the
    give-up budget that stops a CLI which crashes on every connect.
    """
    br, state, sent, reconnects = _bridge(["--cli-recycle-at", "8"], mem=99 * GIB)
    state.cli_mem_baseline = 1 * GIB

    br._transport_dead = True
    assert asyncio.run(br._maybe_recycle_cli()) is False
    br._transport_dead = False

    br.client = None
    assert asyncio.run(br._maybe_recycle_cli()) is False
    br.client = IdleClient()

    state.connecting = True
    assert asyncio.run(br._maybe_recycle_cli()) is False
    state.connecting = False

    br.stop_event.set()
    assert asyncio.run(br._maybe_recycle_cli()) is False

    assert reconnects == []


def test_a_failed_recycle_does_not_kill_the_worker():
    """This is awaited from ``_between_turns``, which the worker runs.

    An exception escaping here would propagate out of the turn loop and end the
    worker — leaving the session "idle" while every later prompt lands in an
    event queue nobody reads. That failure mode is documented in
    ``_warn_if_foreign_task`` and is exactly what must not be reintroduced by a
    memory optimisation.
    """
    br, state, sent, reconnects = _bridge(["--cli-recycle-at", "8"], mem=40 * GIB)
    state.cli_mem_baseline = 1 * GIB

    async def failing_reconnect():
        raise RuntimeError("spawn failed")

    br.reconnect = failing_reconnect

    assert asyncio.run(br._maybe_recycle_cli()) is False
    msg = _messages(sent)
    assert "recycle failed" in msg.lower()
    assert "/connect" in msg, "the way out must be named"


# ---------------------------------------------------------------------------
# /recycle
# ---------------------------------------------------------------------------

def test_the_command_is_tab_completable():
    assert "/recycle" in SLASH_COMMANDS


def test_only_the_form_that_needs_the_sdk_is_queued():
    """Reporting and threshold changes answer instantly; ``now`` needs the worker.

    Routing the settings forms through the event queue would make them silently
    wait for the current turn to finish.
    """
    assert commands.classify("/recycle") == ("recycle-show", "")
    assert commands.classify("/recycle 12") == ("recycle-show", "12")
    assert commands.classify("/recycle off") == ("recycle-show", "off")
    assert commands.classify("/recycle now") == ("recycle", "")


def test_recycle_now_runs_on_the_worker_not_the_caller():
    """A disconnect off the worker task cancels the worker, silently.

    The SDK's transport enters its anyio cancel scope in whoever calls
    ``connect()`` — always the worker — and cancels it in whoever calls
    ``disconnect()``. The resulting "cancel scope exited in a different task"
    is swallowed by the SDK's ``suppress(Exception)``, so the session reports
    success and then ignores every prompt. Hence: ``recycle`` is an event-queue
    kind the worker handles, never something a WebSocket handler does itself.
    """
    br, state, sent, reconnects = _bridge(["--cli-recycle-at", "8"], mem=1 * GIB)

    async def go():
        # The idle path, as worker_loop / _await_next_prompt calls it.
        return await br._apply_idle_config_command("recycle", "")

    assert asyncio.run(go()) is True, "the worker doesn't handle the kind"
    assert reconnects == [1], "forced recycle didn't happen"


def test_forcing_ignores_the_threshold_and_the_cooldown():
    """The user asked. Both of those exist to pace an automatic policy."""
    br, state, sent, reconnects = _bridge(
        ["--no-cli-recycle", "--cli-recycle-cooldown", "3600"], mem=1 * GIB)

    async def go():
        assert await br._maybe_recycle_cli(force=True) is True
        assert await br._maybe_recycle_cli(force=True) is True

    asyncio.run(go())
    assert reconnects == [1, 1]


def test_forcing_does_not_ignore_the_safety_conditions():
    """Those protect work in flight, which is not the user's to waive by typo.

    And it says why, because a ``/recycle now`` that did nothing and said
    nothing is indistinguishable from a broken command.
    """
    br, state, sent, reconnects = _bridge(mem=99 * GIB)
    state.background_tasks["t1"] = {"name": "render"}

    assert asyncio.run(br._maybe_recycle_cli(force=True)) is False
    assert reconnects == []
    assert "Not recycling" in _messages(sent)


def test_the_report_names_the_numbers_that_drive_the_policy():
    cfg = parse_args(["--cli-recycle-at", "8"])
    state = init_state_from_config(cfg)
    state.cli_mem = 9 * GIB
    state.cli_mem_baseline = 2 * GIB
    state.cli_recycles = 3

    out = commands.try_immediate_command("recycle-show", "", state, cfg)
    text = out.messages[0]["data"]["message"]
    assert "8 GiB" in text            # the limit
    assert "9.00 GiB" in text         # where it is now
    assert "2.00 GiB" in text         # the baseline it is measured against
    assert "3 recycle" in text


@pytest.mark.parametrize("arg,expected", [
    ("off", 0.0), ("none", 0.0), ("0", 0.0),
    ("12", 12.0), ("12gib", 12.0), ("12gb", 12.0), ("9.5", 9.5),
])
def test_the_threshold_can_be_changed_live(arg, expected):
    cfg = parse_args([])
    state = init_state_from_config(cfg)
    commands.try_immediate_command("recycle-show", arg, state, cfg)
    assert state.cli_recycle_at == expected


def test_on_returns_to_the_startup_default():
    cfg = parse_args(["--cli-recycle-at", "6"])
    state = init_state_from_config(cfg)
    commands.try_immediate_command("recycle-show", "off", state, cfg)
    assert state.cli_recycle_at == 0.0
    commands.try_immediate_command("recycle-show", "on", state, cfg)
    assert state.cli_recycle_at is None
    assert state_to_status_dict(state, cfg)["cli_recycle_at"] == 6.0


def test_nonsense_is_rejected_rather_than_silently_disabling_it():
    """``/recycle nope`` must not read as ``/recycle 0``."""
    cfg = parse_args([])
    state = init_state_from_config(cfg)
    out = commands.try_immediate_command("recycle-show", "nope", state, cfg)
    assert state.cli_recycle_at is None
    assert out.messages[0]["subtype"] == "error"


# ---------------------------------------------------------------------------
# Where it runs in the turn loop
# ---------------------------------------------------------------------------

def _instrumented(argv=()):
    br, state, sent, reconnects = _bridge(argv)
    calls: list[bool] = []

    async def fake_recycle(*, force: bool = False):
        calls.append(force)
        return False

    async def fake_await_next():
        return None

    br._maybe_recycle_cli = fake_recycle
    br._await_next_prompt = fake_await_next
    return br, state, sent, calls


def test_the_check_runs_at_the_quiescent_end_of_a_turn():
    """The one moment in the loop where a CLI swap costs nothing."""
    br, state, sent, calls = _instrumented()
    asyncio.run(br._between_turns("done", False))
    assert calls == [False]


def test_the_check_is_skipped_while_a_background_task_runs():
    """``_between_turns`` returns to the idle wait before reaching it.

    Belt and braces with the guard inside the policy: this pins the *call site*
    as well, so a future reordering of the branches can't quietly move the
    recycle in front of the bg-task wait.
    """
    br, state, sent, calls = _instrumented()
    state.background_tasks["t1"] = {"name": "render"}
    asyncio.run(br._between_turns("done", False))
    assert calls == []


def test_the_check_is_skipped_when_a_prompt_is_waiting():
    br, state, sent, calls = _instrumented()
    state.queued_prompts.append("next thing")
    assert asyncio.run(br._between_turns("done", False)) == "next thing"
    assert calls == []


def test_an_interrupted_turn_does_not_recycle():
    """The user hit stop to say something, not to wait out a resume."""
    br, state, sent, calls = _instrumented()
    asyncio.run(br._between_turns("partial", True))
    assert calls == []


def test_recycle_now_issued_mid_turn_is_honoured_at_the_boundary():
    """It is queued while busy, so the drain has to remember it."""
    br, state, sent, calls = _instrumented()
    br.event_queue.put_nowait(("recycle", ""))
    asyncio.run(br._between_turns("done", False))
    assert calls == [True], "a /recycle now issued during a turn was dropped"


def test_recycle_now_still_waits_for_a_background_task():
    """Queued-then-forced must not smuggle a recycle past the safety gate."""
    br, state, sent, calls = _instrumented()
    state.background_tasks["t1"] = {"name": "render"}
    br.event_queue.put_nowait(("recycle", ""))
    asyncio.run(br._between_turns("done", False))
    assert calls == []


# ---------------------------------------------------------------------------
# The baseline is re-taken on every connect
# ---------------------------------------------------------------------------

class LiveClient:
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


def test_connect_records_a_fresh_baseline(monkeypatch):
    """Otherwise the replacement is measured against the corpse's baseline.

    Keeping the old number would make the growth check compare a brand-new
    process against the size the *leaked* one started at — so the first turn
    after a recycle could trigger another one.
    """
    monkeypatch.setattr(sdk_bridge, "ClaudeSDKClient",
                        lambda options=None: LiveClient(options))
    br, state, _, _ = _bridge()
    state.cli_mem_baseline = 40 * GIB          # the process we just replaced

    async def fresh():
        state.cli_mem = 2 * GIB
        return 2 * GIB

    br._measure_cli_memory = fresh

    async def go():
        await br.connect()
        await sdk_bridge.cancel_and_join(br._dispatcher_task, "test dispatcher")

    asyncio.run(go())
    assert state.cli_mem_baseline == 2 * GIB, \
        "the new CLI inherited the leaked one's baseline"


# ---------------------------------------------------------------------------
# It reaches the UI
# ---------------------------------------------------------------------------

def test_the_status_payload_always_carries_the_keys():
    """status.js checks for null, so the keys must never be omitted."""
    cfg = parse_args([])
    status = state_to_status_dict(init_state_from_config(cfg), cfg)
    for key in ("cli_mem", "cli_mem_baseline", "cli_recycles", "cli_recycle_at"):
        assert key in status
    assert status["cli_mem"] is None, "unmeasured must not serialise as 0"
    assert status["cli_recycle_at"] == 8.0


def test_the_frontend_consumes_them():
    """The hop that turns a backend field into dead config: nothing reads it."""
    with open(os.path.join(STATIC, "status.js"), encoding="utf-8") as fh:
        status_js = fh.read()
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as fh:
        html = fh.read()

    assert "status.cli_mem" in status_js, "status.js ignores cli_mem"
    assert "cli_recycle_at" in status_js, "the threshold is never read"
    assert 'id="status-climem"' in html, "there is no element to render into"
