"""`/btw` answered in a fork of the live conversation.

Asked 2026-09-18: "I think /btw is supposed to create a new conversation
instance with the same context but copied so it doesn't interfere with the
original, that runs as the current turn is running. Is that what it's
programmed to do?"

It was not. Both handlers made it an ordinary prompt in the main conversation,
and the implementation comment admitted it -- *"Full /btw implementation would
run in a separate context."* -- while the README promised the separate context
anyway.

The mechanism was a first-class SDK option the whole time::

    fork_session: bool = False
    \"\"\"When true, resumed sessions fork to a new session ID rather than
    continuing the previous one.\"\"\"

So ``resume=<current sid>`` + ``fork_session=True`` on a second client is all of
it: same context, new session id, original untouched, concurrent with the live
turn because it is a separate CLI process.

These tests pin the parts that would quietly ruin it: forking the *wrong*
thing, letting the fork *change* things, letting it resume the work it can see,
and letting its throwaway transcript pile up.

Offline: the options builder and prompt shaping are pure; nothing starts a CLI.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btw import (                                          # noqa: E402
    BTW_DISALLOWED_TOOLS,
    btw_prompt,
    build_btw_options,
    can_fork,
)


class FakeOptions:
    """Records what it was constructed with."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


class Cfg:
    cwd = r"D:\proj"
    config_dir = r"C:\Users\x\.claude-account-b"
    disable_prompt_cache = False


class St:
    session_id = "1aa74fb0-ca4e-42cb-80c3-ef7302fee0a4"
    model = "claude-opus-5"


def _opts(**over):
    cfg, st = Cfg(), St()
    for k, v in over.items():
        target = st if hasattr(st, k) else cfg
        setattr(target, k, v)
    return build_btw_options(FakeOptions, config=cfg, state=st).kwargs


# --------------------------------------------------------------------------
# It forks, rather than joining or copying
# --------------------------------------------------------------------------

def test_it_forks_the_live_session():
    k = _opts()

    assert k["resume"] == St.session_id
    assert k["fork_session"] is True


def test_forking_is_what_protects_the_original():
    """Without ``fork_session`` the same ``resume`` would make the aside write
    into the live conversation -- the exact interference the feature exists to
    avoid, and indistinguishable from the old behaviour except worse, because
    it would land mid-turn."""
    k = _opts()

    assert k["fork_session"] is True, (
        "resume without fork_session appends the aside to the real session"
    )


def test_it_inherits_the_account():
    """The config dir decides *whose* sessions are visible. Getting it wrong
    forks nothing at all, or somebody else's conversation."""
    k = _opts()

    assert k["env"]["CLAUDE_CONFIG_DIR"] == Cfg.config_dir


def test_it_inherits_the_working_directory_and_model():
    k = _opts()

    assert k["cwd"] == Cfg.cwd
    assert k["model"] == St.model


def test_nothing_to_fork_is_reported_rather_than_guessed():
    """A session with no id has no transcript. The caller falls back to the old
    behaviour instead of failing -- asking in the main context beats refusing."""

    class NoSid:
        session_id = None

    assert can_fork(NoSid()) is False
    assert can_fork(St()) is True


# --------------------------------------------------------------------------
# The fork must not change anything
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tool", ["Bash", "Write", "Edit", "NotebookEdit"])
def test_the_fork_cannot_modify_anything(tool):
    """Two agents in one working tree at the same time is the hazard. The main
    turn may be mid-edit on the very files an aside would touch, and neither
    would know. ``Bash`` is disallowed despite being useful read-only: it is
    the one tool that can write anything at all."""
    k = _opts()

    assert tool in k["disallowed_tools"]


def test_reading_still_works():
    """Answering "which file did you mean?" is the point; a fork that cannot
    read is a fork that can only guess."""
    k = _opts()

    for tool in ("Read", "Grep", "Glob"):
        assert tool not in k["disallowed_tools"]


def test_the_fork_never_stops_to_ask():
    """Nobody is watching a fork. A permission prompt it cannot deliver would
    hang the aside forever -- and with the mutating tools already gone there is
    nothing dangerous left to approve."""
    k = _opts()

    assert k["permission_mode"] == "bypassPermissions"


def test_the_fork_does_not_resume_an_interrupted_turn():
    """The main session opts into finishing interrupted turns. A fork doing
    that would pick up the work in its history -- with its tools disabled, so
    it would fail confusingly instead of answering."""
    k = _opts()

    assert k["env"]["CLAUDE_CODE_RESUME_INTERRUPTED_TURN"] == "0"


def test_the_prompt_cache_workaround_still_applies():
    k = _opts(disable_prompt_cache=True)

    assert k["env"]["DISABLE_PROMPT_CACHING"] == "1"


# --------------------------------------------------------------------------
# What the fork is told
# --------------------------------------------------------------------------

def test_the_question_survives_the_preamble():
    assert "why did you pick sqlite" in btw_prompt("why did you pick sqlite")


def test_it_is_told_not_to_continue_the_work_it_can_see():
    """A fork of a session mid-task reads its transcript, concludes there is a
    job in progress, and starts doing it. That is the single most likely way
    for this feature to be worse than useless."""
    text = btw_prompt("q").lower()

    assert "do not" in text
    assert "continue" in text or "resume" in text


def test_it_is_told_the_real_session_is_still_running():
    text = btw_prompt("q").lower()

    assert "side question" in text
    assert "fork" in text


def test_it_is_told_its_edits_are_disabled():
    """Otherwise it spends the aside discovering that by trial."""
    text = btw_prompt("q").lower()

    assert "disabled" in text or "disallowed" in text


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------

def test_the_server_routes_btw_to_the_fork_before_the_worker():
    """The worker is busy being the turn. A /btw that reaches the event queue
    is a /btw that waits for the turn to end -- the reported bug."""
    import inspect
    import server

    src = inspect.getsource(server)
    assert 'if kind == "btw" and await bridge.run_btw(payload):' in src


def test_the_old_queued_path_survives_as_the_fallback():
    """A session with nothing to fork still has to answer somehow."""
    import inspect
    from sdk_bridge import SDKBridge

    src = inspect.getsource(SDKBridge._between_turns)
    assert "btw_prompts" in src


def test_only_one_fork_at_a_time():
    """Each fork is a CLI process and a full transcript load, on a runtime that
    already recycles the CLI for leaking memory."""
    import inspect
    from sdk_bridge import SDKBridge

    src = inspect.getsource(SDKBridge.run_btw)
    assert "self._btw_task is not None and not self._btw_task.done()" in src


def test_the_throwaway_transcript_is_discarded():
    """A fork is a real session on disk; without this every aside leaves a stub
    conversation in /resume and the lobby forever."""
    import inspect
    from sdk_bridge import SDKBridge

    src = inspect.getsource(SDKBridge._discard_fork_transcript)
    assert "path.unlink()" in src


def test_the_discard_refuses_to_touch_the_live_session():
    """Deleting the wrong transcript destroys a conversation."""
    import inspect
    from sdk_bridge import SDKBridge

    src = inspect.getsource(SDKBridge._discard_fork_transcript)
    assert "fork_sid == self.state.session_id" in src
