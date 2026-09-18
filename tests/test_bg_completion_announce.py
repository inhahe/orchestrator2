"""A background task's completion must never vanish silently.

From a report, after an interrupt was followed by the model waking up and
talking again: "why wouldn't it say 'background task x completed'? there's
already a mechanism that shows when a background task completes."

There is — ``task_notification`` -> ``bg_complete`` -> a chat row.  But both
callers were shaped ``if entry: <broadcast>`` with no ``else``, and
``complete_bg_task()`` returns ``None`` for a task that was never registered,
not just for a duplicate.  The CLI keeps background tasks running across a
``--continue``/``--resume``, so a bridge that attached afterwards never sees
their ``task_started`` — and their completion was then dropped with no
broadcast *and* no log line.  The model wakes up out of an idle session with no
visible cause, and nothing in the log can afterwards say whether the
notification arrived at all.

Offline: a real ``SDKBridge`` with a list broadcaster; no SDK subprocess.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import parse_args  # noqa: E402
from sdk_bridge import SDKBridge  # noqa: E402
from state import init_state_from_config  # noqa: E402
from tool_manager import register_bg_task  # noqa: E402


def _bridge():
    cfg = parse_args([])
    state = init_state_from_config(cfg)
    sent: list[dict] = []

    async def bcast(msg: dict) -> None:
        sent.append(msg)

    return SDKBridge(config=cfg, state=state, broadcaster=bcast), state, sent


def _completions(sent: list[dict]) -> list[dict]:
    return [m for m in sent if m.get("type") == "bg_complete"]


def _announce(br, task_id, *, status="completed", summary=None,
              output=None, source="task_notification"):
    asyncio.run(br._announce_bg_completion(
        task_id=task_id, status=status, summary=summary,
        output=output, source=source,
    ))


def test_a_registered_task_reports_with_its_name():
    br, state, sent = _bridge()
    register_bg_task(state, "t1", "local_bash", "Find light area usage in scenes")

    _announce(br, "t1", summary="completed (exit code 0)", output="3 hits")

    done = _completions(sent)
    assert len(done) == 1
    assert done[0]["name"] == "Find light area usage in scenes"
    assert done[0]["status"] == "completed"
    assert done[0]["output"] == "3 hits"


def test_a_task_we_never_saw_start_is_still_reported():
    """The bug: a bg task surviving a --continue/--resume completes, and the
    UI said nothing at all — so the model resuming looked causeless."""
    br, _state, sent = _bridge()

    _announce(br, "bgl8jatjt",
              summary='Background command "Find light area usage" completed '
                      '(exit code 0)',
              output="exit 0")

    done = _completions(sent)
    assert len(done) == 1, \
        "completion of an unregistered task was dropped silently"
    assert done[0]["task_id"] == "bgl8jatjt"
    assert done[0]["status"] == "completed"
    # Nothing to look them up from, but the notification's own fields survive.
    assert done[0]["name"] is None and done[0]["seq"] is None
    assert "Find light area usage" in done[0]["summary"]


def test_an_unknown_task_does_not_trigger_a_bg_all_done_wakeup():
    """``background_tasks`` was already empty, so "no tasks left" is vacuously
    true — firing the wakeup would inject a prompt for work nobody tracked."""
    br, _state, _sent = _bridge()

    _announce(br, "never-registered")

    assert br.event_queue.qsize() == 0, "spurious wakeup queued"


def test_the_last_registered_task_finishing_still_wakes_the_loop():
    """The behaviour that must survive the fix."""
    br, state, _sent = _bridge()
    register_bg_task(state, "t1", "local_bash", "x")

    _announce(br, "t1")

    assert br.event_queue.qsize() == 1
    assert br.event_queue.get_nowait() == ("wakeup", "bg-all-done")


def test_a_duplicate_completion_stays_silent():
    """Completion arrives as task_notification *and* as a task_updated patch;
    reporting both would double-render the row."""
    br, state, sent = _bridge()
    register_bg_task(state, "t1", "local_bash", "x")

    _announce(br, "t1", source="task_notification")
    n_after_first = len(_completions(sent))
    _announce(br, "t1", source="task_updated")

    assert n_after_first == 1
    assert len(_completions(sent)) == 1, "duplicate completion rendered twice"


def test_a_failure_is_reported_with_its_status():
    br, state, sent = _bridge()
    register_bg_task(state, "t1", "local_bash", "x")

    _announce(br, "t1", status="failed", output="boom")

    done = _completions(sent)
    assert len(done) == 1 and done[0]["status"] == "failed"
