"""A resumed session that finishes its own interrupted turn must say so.

The report: "E OSc showed under recents, but when i loaded it, it was
apparently still doing a turn. even though it was under recents, and even
though the tab had been closed for a long time."

Nothing was wrong with the lobby.  The full chain, from orchestrator2.log::

    09-11 19:24:24  runtime s3 started (cwd=E:...os, resume=1aa74fb0-...)
    09-16 04:01:32  bg task bpj3iib57 started: during_turn=True     <- working
    09-16 04:01:34  runtime s3 idle for 300s - tearing down
    09-16 04:01:34  run_turn finally: normal_completion=False exit_exc=CancelledError
    09-16 04:01:39  reaped 7 orphaned MCP/tool process(es)
    09-16 06:23:00  runtime s13 started (resume=1aa74fb0-...)       <- user opens it
    09-16 06:23:30  ghost turn begin: SDK streaming without active run_turn

So the session really was not running when the lobby listed it under Recents
(its CLI starts one second *after* the runtime, so it is that runtime's own
child, not an orphan).  It came back to life on load because we set
``CLAUDE_CODE_RESUME_INTERRUPTED_TURN=1`` on every connect, which makes the
CLI pick up a turn that was cut off - here, cut off by the idle teardown that
killed a working runtime (fixed separately; that hub predated the fix).

Recovering the turn is the behaviour we want.  Doing it in total silence is
not: output appears with nobody having typed anything, and the only trace is a
server-log WARNING.  These tests pin the notice.

Offline: a real ``SDKBridge`` with a list broadcaster; no SDK subprocess.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    TextBlock,
)

from config import parse_args  # noqa: E402
from sdk_bridge import SDKBridge  # noqa: E402
from state import init_state_from_config  # noqa: E402


def _bridge(argv=()):
    cfg = parse_args(list(argv))
    state = init_state_from_config(cfg)
    sent: list[dict] = []

    async def bcast(msg: dict) -> None:
        sent.append(msg)

    br = SDKBridge(config=cfg, state=state, broadcaster=bcast)
    return br, state, sent


def _assistant(text: str = "...continuing") -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text)], model="claude-x")


def _notices(sent: list[dict]) -> list[str]:
    out = []
    for m in sent:
        if m.get("type") == "system_msg":
            out.append((m.get("data") or {}).get("message") or "")
    return out


def _resumed(br, sid: str = "1aa74fb0-ca4e-42cb-80c3-ef7302fee0a4"):
    """Connect the way a lobby click does: with an explicit session to resume."""
    br._make_options(resume_id=sid)


def _fresh(br, state):
    """Connect the way a brand-new session does: nothing to resume."""
    state.session_id = None
    br._initial_resume_id = None
    br._make_options()


# --------------------------------------------------------------------------
# The notice itself
# --------------------------------------------------------------------------

def test_a_resumed_session_that_starts_talking_on_its_own_says_why():
    """The reported case, end to end: resume, no prompt, SDK streams."""
    br, _state, sent = _bridge()
    _resumed(br)

    asyncio.run(br._begin_ghost_turn_if_needed())

    assert _notices(sent), (
        "a session resumed itself into a turn and told the user nothing; "
        "that silence is the whole defect"
    )


def test_the_notice_says_nobody_here_sent_it():
    """The user's confusion was authorship, not activity: they know it is
    working, they do not know why.  A notice that only said "resuming" would
    not answer the question that was actually asked."""
    br, _state, sent = _bridge()
    _resumed(br)

    asyncio.run(br._begin_ghost_turn_if_needed())
    text = " ".join(_notices(sent)).lower()

    assert "interrupt" in text, "must name what is being continued"
    assert "nothing was sent" in text, "must say the prompt did not come from here"


def test_the_notice_says_how_to_stop_it():
    """It is resuming work the user may no longer want - two days passed in the
    reported case."""
    br, _state, sent = _bridge()
    _resumed(br)

    asyncio.run(br._begin_ghost_turn_if_needed())
    text = " ".join(_notices(sent)).lower()

    assert "ctrl+c" in text or "interrupt button" in text


def test_the_notice_is_an_info_not_an_error():
    """The turn being recovered is the feature working.  Styling it as an error
    would train the user to distrust a correct recovery."""
    br, _state, sent = _bridge()
    _resumed(br)

    asyncio.run(br._begin_ghost_turn_if_needed())
    subs = [m.get("subtype") for m in sent if m.get("type") == "system_msg"]

    assert subs == ["info"], subs


# --------------------------------------------------------------------------
# When it must stay quiet
# --------------------------------------------------------------------------

def test_a_ghost_turn_after_a_real_prompt_is_not_announced():
    """The *common* ghost turn is a background task's notification waking the
    model mid-session.  Announcing "your turn was interrupted" there would be
    a lie told several times a session."""
    br, _state, sent = _bridge()
    _resumed(br)

    async def go():
        # A prompt went out: everything after this was asked for.
        br._unprompted_resume_pending = False
        await br._begin_ghost_turn_if_needed()

    asyncio.run(go())

    assert not _notices(sent), _notices(sent)


def test_run_turn_is_what_clears_it():
    """Pin the mechanism, not just the flag: sending a prompt is the event that
    makes later output attributable to the user."""
    src = inspect.getsource(SDKBridge.run_turn)
    head = src.split("_prompt_preview")[0]

    assert "_unprompted_resume_pending = False" in head, (
        "run_turn must disarm the notice *before* it can stream anything, "
        "or the first prompt of a resumed session gets labelled a recovery"
    )


def test_a_fresh_session_never_announces():
    """No resume, nothing to recover."""
    br, state, sent = _bridge(["--no-continue"])
    _fresh(br, state)

    asyncio.run(br._begin_ghost_turn_if_needed())

    assert not _notices(sent), _notices(sent)


def test_only_one_notice_per_connection():
    """A second ghost turn in the same connection has some other cause - by
    then the recovered turn has ended and a bg task is the likely trigger."""
    br, state, sent = _bridge()
    _resumed(br)

    async def go():
        await br._begin_ghost_turn_if_needed()
        # First ghost turn ends; the next stream is a different animal.
        state.busy = False
        await br._begin_ghost_turn_if_needed()

    asyncio.run(go())

    assert len(_notices(sent)) == 1, _notices(sent)


def test_a_reconnect_arms_it_again():
    """Transport death mid-turn reconnects with resume, and the CLI picks the
    turn up there too - same surprise, same explanation owed."""
    br, state, sent = _bridge()
    _resumed(br)

    async def go():
        await br._begin_ghost_turn_if_needed()
        state.busy = False
        _resumed(br)                      # reconnect
        await br._begin_ghost_turn_if_needed()

    asyncio.run(go())

    assert len(_notices(sent)) == 2, _notices(sent)


def test_an_active_run_turn_suppresses_it():
    """If a real turn owns the stream there is no ghost turn at all, so there
    is nothing to explain."""
    br, _state, sent = _bridge()
    _resumed(br)
    br.turn_active.set()

    rendered = asyncio.run(br._begin_ghost_turn_if_needed())

    assert rendered is False
    assert not _notices(sent), _notices(sent)


# --------------------------------------------------------------------------
# The notice must not become a new failure mode
# --------------------------------------------------------------------------

def test_the_ghost_turn_still_starts_when_the_notice_cannot_be_sent():
    """A broadcast raising (last socket closing as it lands) must not abort the
    turn bookkeeping - that would leave the UI idle while the SDK streams."""
    cfg = parse_args([])
    state = init_state_from_config(cfg)

    async def bcast(msg: dict) -> None:
        if msg.get("type") == "system_msg":
            raise RuntimeError("socket went away")

    br = SDKBridge(config=cfg, state=state, broadcaster=bcast)
    _resumed(br)

    rendered = asyncio.run(br._begin_ghost_turn_if_needed())

    assert rendered is True
    assert state.busy is True, "the turn must still be marked live"


def test_the_notice_lands_before_the_status_update():
    """Order matters for reading: the explanation should be on screen before
    the UI flips to "working", not after the output has started."""
    br, _state, sent = _bridge()
    _resumed(br)

    asyncio.run(br._begin_ghost_turn_if_needed())
    kinds = [m.get("type") for m in sent]

    assert kinds.index("system_msg") < kinds.index("status_update"), kinds


def test_the_recovered_turn_is_still_rendered():
    """The notice explains the output; it does not replace it."""
    br, state, sent = _bridge()
    _resumed(br)

    rendered = asyncio.run(br._begin_ghost_turn_if_needed())

    assert rendered is True, "the recovered turn's text must still be shown"
    assert state.busy is True


def test_the_log_records_which_kind_of_ghost_turn_it_was():
    """Both kinds log "ghost turn begin"; forensics on a report like this one
    needs them distinguishable a week later."""
    src = inspect.getsource(SDKBridge._begin_ghost_turn_if_needed)

    assert "resumed_interrupted_turn=%s" in src, (
        "the log line must say whether this ghost turn was a resume recovery"
    )


def test_the_env_var_that_causes_this_is_still_the_one_we_set():
    """The notice is only true while we opt into CLI turn recovery.  If that
    env var is ever dropped, this test fails and the notice comes out too."""
    src = inspect.getsource(SDKBridge)

    assert "CLAUDE_CODE_RESUME_INTERRUPTED_TURN" in src, (
        "notice claims the CLI resumes interrupted turns; nothing opts into it"
    )
