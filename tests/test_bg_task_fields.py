"""Background-task fields must match the SDK's actual message shape.

Found while investigating a report that ``TaskOutput`` events were being
"read the wrong way".  They weren't — ``TaskOutput`` is a genuine tool the
model calls to poll a background task, so rendering it under tools is right,
and the tasks in question really were still running.  But the dig turned up a
neighbouring bug: the ``task_started`` handler read ``msg.name``, and
``TaskStartedMessage`` has no such field — it's ``description``.  Every
background task was therefore registered with an empty name, and the bg panel
showed unlabelled rows.

That class of bug is invisible at runtime (``getattr`` default swallows it),
so these tests pin the field names against the real SDK.
"""

from __future__ import annotations

import dataclasses
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from state import State                                  # noqa: E402
from tool_manager import complete_bg_task, register_bg_task  # noqa: E402

sdk_types = pytest.importorskip("claude_agent_sdk.types")


def _fields(cls) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)}


def test_task_started_carries_description_not_name():
    """The exact mismatch that produced blank task names."""
    fields = _fields(sdk_types.TaskStartedMessage)
    assert "description" in fields
    assert "name" not in fields, (
        "SDK grew a 'name' field — re-check the task_started handler, which "
        "now prefers 'description'")


def test_task_started_handler_reads_the_field_that_exists():
    """Mirror of the handler's extraction, against a real SDK message."""
    msg = sdk_types.TaskStartedMessage(
        subtype="task_started", data={}, task_id="bmhmsjzig",
        description="run the full corpus suite", uuid="u", session_id="s",
        tool_use_id="toolu_1", task_type="local_bash",
    )
    name = (getattr(msg, "description", None)
            or getattr(msg, "name", None) or "")
    assert name == "run the full corpus suite", \
        "task name is blank — the bg panel would show an unlabelled row"


def test_task_notification_fields_are_what_the_handler_reads():
    fields = _fields(sdk_types.TaskNotificationMessage)
    for f in ("task_id", "status", "output_file", "summary"):
        assert f in fields, f"handler reads msg.{f}, SDK no longer provides it"


def test_registered_task_keeps_its_name_through_completion():
    state = State()
    register_bg_task(state, "t1", "local_bash", "run the full corpus suite",
                     tool_use_id="toolu_1")
    assert state.background_tasks["t1"]["name"] == "run the full corpus suite"

    entry = complete_bg_task(state, "t1", "completed", summary="done")
    assert entry is not None
    assert entry["name"] == "run the full corpus suite"
    assert "t1" not in state.background_tasks, "completed task still listed"


def test_completion_is_idempotent():
    """A task must not be resurrected or double-reported.

    Completion can arrive as ``task_notification`` *or* as a ``task_updated``
    patch, and the handler runs both paths — so the dedupe matters.
    """
    state = State()
    register_bg_task(state, "t1", "local_bash", "x")
    assert complete_bg_task(state, "t1", "completed") is not None
    assert complete_bg_task(state, "t1", "completed") is None


def test_completing_an_unknown_task_is_a_noop():
    state = State()
    assert complete_bg_task(state, "never-registered", "completed") is None
