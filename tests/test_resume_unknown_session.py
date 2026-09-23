"""A resume that cannot happen must say so -- not quietly open a blank session.

Reported 2026-09-22: "i did --resume with a nonexistent session name, and now
it won't let me /rename the session"::

    SDK connection failed (attempt 1/10: ... Provided value "OS A" is not a
    UUID and does not match any session title. (exit code: 1)), retrying in 2s
    Rename failed: session OS A not found on disk
    SDK connected.
    Rename failed: session OS A not found on disk

"SDK connected" is the lie. The log shows what it connected *to*::

    20:54:56  SDK connect failed (attempt 1): ... "OS A" ...
    20:54:58  connect: resume=None            <- the retry dropped the resume
    20:55:00  agent identity: os-2 (created)  <- and minted a new identity
    20:55:00  SDK connected after 1 retries

The requested resume id was "one-time use", cleared the moment the options
were built -- so a first attempt that *failed* had already spent it. The retry
loop calls a bare ``connect()``, an explicit resume implies ``no_continue``,
and the retry therefore started a brand-new empty session. Meanwhile
``state.session_id`` stayed "OS A" until that session's first turn, so
``/rename`` looked for a session called "OS A" on disk.

**The typo was the harmless trigger.** The mechanism applied to every
lobby-opened session: any one whose first connect attempt failed for a
transient reason was silently swapped for a blank one. Proven offline before
this was written::

    attempt 1 resume : 43ee6a50-real
    retry     resume : None | continue_conversation = False

``reconnect()`` never had the problem because it passes ``resume_id=sid``
explicitly; only the first-connect retry loop relied on the one-time id.

A second, separate defect fell out of the same investigation: ``/clear``
starts a deliberately new session, but ``expected_resume_sid`` was never reset
on that path, so its first turn reported "Expected to resume X but SDK started
Y -- prior context may not be loaded" -- a false alarm on exactly the one
occasion a new session is the point.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sdk_bridge                                         # noqa: E402
from config import parse_args                             # noqa: E402
from sdk_bridge import SDKBridge, _is_unknown_session_error  # noqa: E402
from session import describe_resumable_sessions           # noqa: E402
from state import init_state_from_config                  # noqa: E402

# The CLI's actual words, from the report.
UNKNOWN = (
    'Claude Code returned an error result: Error: --resume requires a valid '
    'session ID or session title when used with --print. Usage: claude -p '
    '--resume <session-id|title>. Provided value "OS A" is not a UUID and does '
    'not match any session title. (exit code: 1)'
)


def _bridge(resume="43ee6a50-real", **over):
    """A bridge set up exactly the way _create_runtime sets one up."""
    cfg = dataclasses.replace(parse_args([]), resume=resume,
                              no_continue=bool(resume), **over)
    st = init_state_from_config(cfg)
    sent: list[dict] = []

    async def bcast(msg):
        sent.append(msg)

    br = SDKBridge(config=cfg, state=st, broadcaster=bcast)
    br._initial_resume_id = resume
    return br, st, sent


class _FailingClient:
    """A ClaudeSDKClient whose connect fails with *error*, recording options."""

    seen: list = []
    error: Exception = RuntimeError("transient")

    def __init__(self, options=None):
        _FailingClient.seen.append(options)

    async def connect(self):
        raise _FailingClient.error

    async def disconnect(self):
        pass


@pytest.fixture
def failing(monkeypatch):
    _FailingClient.seen = []
    _FailingClient.error = RuntimeError("transient")
    monkeypatch.setattr(sdk_bridge, "ClaudeSDKClient", _FailingClient)
    return _FailingClient


def _run_connect_loop(br, timeout=10.0):
    """Drive the real worker_loop's connect phase until it parks; count tries."""
    attempts: list[str] = []
    real_connect = br.connect

    async def counting_connect(*a, **k):
        attempts.append("try")
        return await real_connect(*a, **k)

    br.connect = counting_connect

    async def go():
        task = asyncio.ensure_future(br.worker_loop())
        for _ in range(400):
            await asyncio.sleep(0.01)
            if attempts and br.event_queue.qsize() == 0 and not br.state.connecting:
                break
        br.event_queue.put_nowait(("quit", ""))
        await asyncio.wait_for(task, timeout)

    asyncio.run(go())
    return attempts


# --------------------------------------------------------------------------
# The general hazard: a failed first attempt must not spend the resume
# --------------------------------------------------------------------------

def test_a_failed_first_connect_keeps_the_requested_resume(failing):
    br, _st, _sent = _bridge()

    with pytest.raises(Exception):
        asyncio.run(br.connect())

    assert br._initial_resume_id == "43ee6a50-real", (
        "a connect that failed spent the resume, so the retry will open a "
        "blank session instead"
    )


def test_the_retry_asks_for_the_same_session_not_a_blank_one(failing):
    """The mechanism the report exposed, on a *real* session id: this is what
    turned a transient first-connect failure into a silently swapped
    conversation for any lobby-opened session."""
    br, _st, _sent = _bridge()

    with pytest.raises(Exception):
        asyncio.run(br.connect())
    retry = br._make_options()

    # ``resume`` is the whole question. (``continue_conversation`` is False on
    # every resume -- it is the dataclass default and ``resume`` takes
    # precedence -- so it cannot tell a resume from a fresh session.)
    assert getattr(retry, "resume", None) == "43ee6a50-real"


def test_every_attempt_in_the_real_loop_resumes_the_same_session(failing):
    """Driven through the real worker_loop rather than inferred: each spawn the
    loop makes must carry the requested resume. (Two attempts are enough; the
    second is the one that used to be blank.)"""
    br, _st, _sent = _bridge()
    calls = {"n": 0}

    async def fail_then_stop(self):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError(UNKNOWN)    # settled -> the loop parks
        raise RuntimeError("transient")

    failing.connect = fail_then_stop
    _run_connect_loop(br, timeout=15.0)

    resumes = [getattr(o, "resume", None) for o in failing.seen]
    assert len(resumes) >= 2, resumes
    assert all(r == "43ee6a50-real" for r in resumes), resumes


# --------------------------------------------------------------------------
# A session that does not exist is settled, not transient
# --------------------------------------------------------------------------

def test_the_cli_text_for_a_missing_session_is_recognised():
    assert _is_unknown_session_error(UNKNOWN)


@pytest.mark.parametrize("text", [
    "TimeoutError: connect timed out after 180s",
    "Claude Code returned an error result: API Error: 529 overloaded",
    "[WinError 267] The directory name is invalid",
    "",
])
def test_other_failures_are_not_mistaken_for_it(text):
    """Only a missing session is settled. Classifying a timeout as one would
    give up on a session that the next attempt would have opened."""
    assert not _is_unknown_session_error(text)


def test_an_unknown_session_is_tried_once_not_ten_times(failing):
    """A session that does not exist will not start existing on the tenth try.
    With the resume now kept across retries, this is also what stops the loop
    retrying a typo for several minutes of backoff."""
    failing.error = RuntimeError(UNKNOWN)
    br, _st, _sent = _bridge(resume="OS A")

    attempts = _run_connect_loop(br)

    assert len(attempts) == 1, f"tried a nonexistent session {len(attempts)} times"


def test_the_bogus_id_does_not_survive_as_the_session_id(failing):
    """The reported symptom: /rename looked for "OS A" on disk because the
    session id still said so. Nothing was resumed, so nothing may claim it."""
    failing.error = RuntimeError(UNKNOWN)
    br, st, _sent = _bridge(resume="OS A")
    st.session_id = "OS A"                   # what the early history seed does

    _run_connect_loop(br)

    assert st.session_id != "OS A"


def test_the_message_names_what_was_asked_for(failing):
    failing.error = RuntimeError(UNKNOWN)
    br, _st, sent = _bridge(resume="OS A")

    _run_connect_loop(br)
    errors = [(m.get("data") or {}).get("message", "") for m in sent
              if m.get("type") == "system_msg" and m.get("subtype") == "error"]

    assert any("'OS A'" in e for e in errors), errors
    assert not any("after 10 attempts" in e for e in errors), (
        "a settled failure was reported as if it had been retried"
    )


# --------------------------------------------------------------------------
# Saying what *can* be resumed
# --------------------------------------------------------------------------

def _make_session(root, cwd, sid, title=None):
    from session import _sanitize_cwd
    from pathlib import Path
    project = root / "projects" / _sanitize_cwd(str(Path(cwd).resolve(strict=False)))
    project.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "user", "sessionId": sid, "cwd": cwd,
              "message": {"role": "user", "content": "hi"}}]
    if title:
        lines.append({"type": "custom-title", "customTitle": title, "sessionId": sid})
    (project / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def test_the_listing_names_the_real_titles(tmp_path):
    """The reported directory's sessions were titled 'E OSb' and 'E OSc', and
    nothing told the user so."""
    cwd = str(tmp_path / "os")
    os.makedirs(cwd)
    _make_session(tmp_path, cwd, "27db9c83-aaaa", "E OSb")
    _make_session(tmp_path, cwd, "1aa74fb0-bbbb", "E OSc")

    text = describe_resumable_sessions(cwd, str(tmp_path))

    assert "'E OSb'" in text and "'E OSc'" in text, text
    assert "27db9c83" in text


def test_untitled_sessions_are_counted_not_dropped(tmp_path):
    cwd = str(tmp_path / "os")
    os.makedirs(cwd)
    _make_session(tmp_path, cwd, "aaaa1111-x", "Named")
    _make_session(tmp_path, cwd, "bbbb2222-y")
    _make_session(tmp_path, cwd, "cccc3333-z")

    text = describe_resumable_sessions(cwd, str(tmp_path))

    assert "2 untitled" in text, text


def test_the_listing_prefers_the_newest_sessions(tmp_path):
    """The listing is capped, so order decides what the user is shown. The
    session they meant is far likelier to be recent than eight months old."""
    import time
    from session import _sanitize_cwd
    from pathlib import Path
    cwd = str(tmp_path / "os")
    os.makedirs(cwd)
    project = tmp_path / "projects" / _sanitize_cwd(str(Path(cwd).resolve(strict=False)))
    now = time.time()
    for i in range(8):
        _make_session(tmp_path, cwd, f"sess{i}-0000", f"S{i}")
        os.utime(project / f"sess{i}-0000.jsonl", (now - (8 - i) * 60,) * 2)

    text = describe_resumable_sessions(cwd, str(tmp_path))

    assert "'S7'" in text, f"the newest session was cut: {text}"
    assert "'S0'" not in text, f"the oldest was shown instead of a newer one: {text}"


def test_an_empty_directory_says_so(tmp_path):
    assert "no sessions" in describe_resumable_sessions(
        str(tmp_path / "nothing"), str(tmp_path)).lower()


def test_the_error_the_user_sees_lists_what_they_could_have_meant(
        failing, tmp_path, monkeypatch):
    """End to end: the listing must reach the message, not just exist as a
    helper. "Not found" alone is what left the user guessing at titles."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    cwd = tmp_path / "os"
    cwd.mkdir()
    _make_session(tmp_path, str(cwd), "27db9c83-aaaa", "E OSb")
    failing.error = RuntimeError(UNKNOWN)
    br, _st, sent = _bridge(resume="OS A", cwd=str(cwd))

    _run_connect_loop(br)
    errors = " ".join((m.get("data") or {}).get("message", "") for m in sent
                      if m.get("type") == "system_msg")

    assert "'E OSb'" in errors, errors


# --------------------------------------------------------------------------
# /clear: a deliberately new session is not a failed resume
# --------------------------------------------------------------------------

def test_a_fresh_connect_forgets_the_previous_expected_resume():
    """After /clear the next session is new on purpose. A stale expectation
    made its first turn report "prior context may not be loaded"."""
    br, st, _sent = _bridge(resume=None)
    st.session_id = None
    st.expected_resume_sid = "43ee6a50-old"

    br._make_options()

    assert st.expected_resume_sid is None


def test_a_real_resume_still_sets_the_expectation():
    """The check itself is worth having -- only its false alarm was wrong."""
    br, st, _sent = _bridge()

    br._make_options()

    assert st.expected_resume_sid == "43ee6a50-real"
