"""The plan panel survives a restart.

Asked 2026-09-06: do todo lists survive a session/hub restart, and if not,
should they?

They did not. ``state.current_todos`` was written only by a *live* ``TodoWrite``
tool call, so after a restart the panel sat empty until the agent happened to
rewrite its list. The asymmetry that made this worth fixing: the CLI rebuilds
its *own* todo state from the transcript on resume, so Claude still knew the
plan -- only the user's view of it was missing.

**There is one writer, and each write is a full replace.** Verified against
three live transcripts: their tails contain ``TodoWrite`` and nothing else that
touches todos (``TaskOutput``/``TaskStop`` belong to the background-task
system). Every write carries the entire list, each item tagged
``pending`` / ``in_progress`` / ``completed``; the strikethrough in the panel is
that ``completed`` status being rendered, not a separate "mark it done" call.
So the last write is authoritative on its own -- including which items are
struck out -- and nothing further needs scanning.

Seeded verbatim, completed items included, because that is exactly what the
panel showed before the restart, and ``/api/todos/clear`` already exists for
anyone who wants them gone.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from session import last_todos_from_records, render_session_history  # noqa: E402


def _todo_write(*items):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "TodoWrite", "id": "t1",
         "input": {"todos": [
             {"content": c, "status": s, "activeForm": c} for c, s in items]}},
    ]}}


def _other_tool(name):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": name, "id": "x", "input": {"foo": 1}},
    ]}}


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def test_no_todo_write_means_no_todos():
    assert last_todos_from_records([_other_tool("Bash")]) == []


def test_the_plan_is_recovered():
    got = last_todos_from_records([_todo_write(("write the parser", "pending"))])
    assert [t["content"] for t in got] == ["write the parser"]


def test_the_last_write_wins():
    """Each write is a full replace, so an earlier one must not leak through."""
    got = last_todos_from_records([
        _todo_write(("old plan", "pending")),
        _todo_write(("new plan", "in_progress"), ("also new", "pending")),
    ])
    assert [t["content"] for t in got] == ["new plan", "also new"]


def test_completed_items_are_kept_with_their_status():
    """The strikethrough is this status being rendered. Keeping it reproduces
    exactly what the panel showed before the restart."""
    got = last_todos_from_records([_todo_write(
        ("done thing", "completed"),
        ("doing thing", "in_progress"),
        ("later thing", "pending"),
    )])
    assert [t["status"] for t in got] == ["completed", "in_progress", "pending"]


def test_an_all_completed_plan_is_still_recovered():
    """Not filtered away: 'everything is done' is information too, and the
    existing /api/todos/clear is how a user asks for them to go."""
    got = last_todos_from_records([_todo_write(("a", "completed"), ("b", "completed"))])
    assert len(got) == 2


def test_other_tools_never_supply_todos():
    """TaskOutput/TaskStop are the background-task system, not the plan."""
    for name in ("TaskOutput", "TaskStop", "Bash", "Edit"):
        assert last_todos_from_records([_other_tool(name)]) == []


def test_the_tool_name_is_what_decides_not_the_payload_shape():
    """A different tool carrying a ``todos``-shaped argument must not be
    mistaken for the plan. Matching on shape alone would make the panel
    hostage to any tool that happens to use that parameter name."""
    rec = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Agent", "id": "a1",
         "input": {"todos": [{"content": "not the plan", "status": "pending"}]}},
    ]}}
    assert last_todos_from_records([rec]) == []


def test_a_todo_write_still_wins_after_an_impostor():
    rec_bad = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Agent", "id": "a1",
         "input": {"todos": [{"content": "not the plan", "status": "pending"}]}},
    ]}}
    got = last_todos_from_records([_todo_write(("real plan", "pending")), rec_bad])
    assert [t["content"] for t in got] == ["real plan"]


@pytest.mark.parametrize("payload", [5, 3.5, True, {"a": 1}])
def test_a_non_iterable_todos_payload_cannot_crash_the_read(payload):
    """``for t in 5`` raises TypeError, and this runs inside the session load —
    a side panel must never be why a transcript fails to open."""
    rec = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "TodoWrite", "input": {"todos": payload}},
    ]}}
    assert last_todos_from_records([rec]) == []


def test_a_later_non_todo_tool_does_not_erase_the_plan():
    got = last_todos_from_records([
        _todo_write(("keep me", "pending")),
        _other_tool("Bash"),
    ])
    assert [t["content"] for t in got] == ["keep me"]


@pytest.mark.parametrize("bad", [
    {"type": "assistant", "message": None},
    {"type": "assistant", "message": {"content": "not a list"}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use",
                                                   "name": "TodoWrite"}]}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use",
                                                   "name": "TodoWrite",
                                                   "input": {"todos": "nope"}}]}},
    {"no_message_key": True},
])
def test_malformed_records_do_not_raise(bad):
    """A side panel must never be the reason a session fails to load."""
    assert last_todos_from_records([bad]) == []


def test_non_dict_items_are_dropped():
    rec = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "TodoWrite",
         "input": {"todos": [{"content": "ok", "status": "pending"}, "junk", None]}},
    ]}}
    got = last_todos_from_records([rec])
    assert [t["content"] for t in got] == ["ok"]


# ---------------------------------------------------------------------------
# ...delivered by the reader that already walks the transcript
# ---------------------------------------------------------------------------

def _transcript(tmp_path, *records):
    import json
    p = Path(tmp_path) / "s.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return p


def test_render_session_history_returns_the_todos(tmp_path):
    """It rides along on the existing read: the transcript can be 600 MB and
    take ~40 s to walk, so a second pass just for a side panel would cost more
    than the panel is worth."""
    p = _transcript(tmp_path,
                    {"type": "user", "message": {"role": "user", "content": "hi"}},
                    _todo_write(("seeded", "in_progress")))
    count, messages, orphans, todos = render_session_history(p)
    assert [t["content"] for t in todos] == ["seeded"]
    assert any(m.get("content") == "hi" for m in messages), "history still renders"


def test_render_session_history_with_no_todos(tmp_path):
    p = _transcript(tmp_path,
                    {"type": "user", "message": {"role": "user", "content": "hi"}})
    _c, _m, _o, todos = render_session_history(p)
    assert todos == []


def test_a_missing_transcript_returns_the_four_tuple(tmp_path):
    got = render_session_history(Path(tmp_path) / "does-not-exist.jsonl")
    assert len(got) == 4
    assert got[3] == []


def test_the_unreadable_error_path_returns_the_four_tuple(tmp_path, monkeypatch):
    """The OSError branch, driven directly.

    Neither a missing file nor a directory reaches it -- ``_tail_read_jsonl``
    absorbs both -- so the branch is provoked at its own boundary. A mutation
    sweep is why this exists: the first version of this test *named* the error
    path, never reached it, and therefore pinned nothing. A wrong arity there
    surfaces in the caller as an unpacking error naming neither this function
    nor the real problem.
    """
    import session as S

    def boom(*_a, **_k):
        raise OSError("disk went away")

    monkeypatch.setattr(S, "_tail_read_jsonl", boom)
    got = S.render_session_history(Path(tmp_path) / "s.jsonl")
    assert len(got) == 4, "the error path returns a different shape"
    assert got[3] == []
    assert any(m.get("subtype") == "error" for m in got[1]), (
        "the error path was not taken, so this pins nothing")
