"""The name other Claude sessions address this one by.

Reported 2026-09-22, from a SlateOS lane session::

    i did `/rename Lane A` and then sent `test` and it said: ... I checked
    again and the session is still named os-f5, so I still don't know which
    lane this is.

The model was right. SlateOS's ``scripts/which-lane.py`` reads the lane from
the session's *addressable name* -- the first line of ``ListAgents`` -- and our
``/rename`` set only the *title*. Those are different things held by different
parties:

* the **title** is a ``custom-title`` record in the transcript, which we write;
* the **addressable name** is held by the running **CLI**, which never learns
  about a record we append.

Measured against CLI 2.1.280 before any code was written::

    nothing set                          -> nameprobe-qqmoa2tw-ab
    CLAUDE_CODE_SESSION_NAME=Lane Test   -> Lane Test
    our custom-title record, then resume -> nameprobe-qqmoa2tw-0d
    an agent-name record, then resume    -> nameprobe-qqmoa2tw-1b
    forwarded "/rename Lane Live"        -> Lane Live   (live, no reconnect;
                                            also as a brand-new CLI's first input)

So the name exists only at runtime: set by the CLI's own ``/rename`` or by the
environment at startup, and restored by nothing on disk. Hence two halves:
pass the name on **every** connect, and hand ``/rename`` to the live CLI.

How the CLI answers ``/rename`` (measured, no model involved)::

    SystemMessage session_state_changed running
    SystemMessage init
    AssistantMessage model='<synthetic>' "Session renamed to: Lane Probe"
    ResultMessage success cost=0                  -- about 0.1 s in all
    SystemMessage session_state_changed idle      -- just *after* the result

That is why it is sent as a short exchange and not as a turn or a queued
prompt -- see ``SDKBridge._run_pending_cli_command`` and the tests below.

Why the name is never ``--agent-name``: that flag, like ``ORCH2_AGENT_NAME``,
is set once per *hub*, and every session the hub opens inherits it -- so it
would give every session in the hub the same address.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_agent_sdk import (                          # noqa: E402
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

import commands                                        # noqa: E402
from config import DISPATCHER_DEAD, parse_args         # noqa: E402
from sdk_bridge import SDKBridge                        # noqa: E402
from state import State, init_state_from_config         # noqa: E402

ENV = "CLAUDE_CODE_SESSION_NAME"
SID = "5e55-0000-rename"


def _bridge(argv=(), **state_over):
    cfg = parse_args(list(argv))
    st = init_state_from_config(cfg)
    for k, v in state_over.items():
        setattr(st, k, v)

    async def bc(m):
        pass

    return SDKBridge(config=cfg, state=st, broadcaster=bc), st


@pytest.fixture(autouse=True)
def _no_inherited_names(monkeypatch):
    """Names in the developer's shell must not leak into tests."""
    monkeypatch.delenv("ORCH2_AGENT_NAME", raising=False)
    monkeypatch.delenv(ENV, raising=False)


# --------------------------------------------------------------------------
# Passing the name to the CLI
# --------------------------------------------------------------------------

def test_a_person_chosen_title_becomes_the_addressable_name():
    br, _st = _bridge(human_title="Lane A")

    assert br._make_options().env.get(ENV) == "Lane A"


def test_a_rename_made_before_the_session_existed_names_the_cli_it_starts():
    """`/rename` before the first turn only has ``pending_rename`` to go on."""
    br, _st = _bridge(pending_rename="Lane A")

    assert br._make_options().env.get(ENV) == "Lane A"


def test_the_pending_rename_is_the_newer_choice():
    br, _st = _bridge(human_title="Old name", pending_rename="Lane A")

    assert br._make_options().env.get(ENV) == "Lane A"


def test_no_name_leaves_the_cli_to_choose():
    """Unset must mean *absent* -- not an empty string the CLI might take as
    the name."""
    br, _st = _bridge()

    assert ENV not in br._make_options().env


def test_a_blank_title_is_treated_as_no_name():
    br, _st = _bridge(human_title="   ")

    assert ENV not in br._make_options().env


def test_the_name_rides_on_every_connect_not_just_the_first():
    """Nothing on disk restores it -- measured -- so a reconnect or a hub
    restart that did not pass it again would quietly revert to an auto name."""
    br, _st = _bridge(human_title="Lane A")

    first = br._make_options().env.get(ENV)
    second = br._make_options(resume_id="some-session").env.get(ENV)

    assert first == second == "Lane A"


def test_a_sessions_own_agent_name_is_its_address():
    """``--agent-name`` names the session its launch opens, for both
    registries -- the agent-comms identity and what ListAgents shows agree --
    and it outranks the title.  That it is the session's *own* (the hub never
    passes its name on to other sessions) is tests/test_agent_name_per_session.py."""
    br, _st = _bridge(["--agent-name", "Lane-D"], human_title="OS D",
                      pending_rename="Something")

    assert br._make_options().env.get(ENV) == "Lane-D"


def test_the_bridge_never_takes_a_name_from_the_environment(monkeypatch):
    """The hub's environment is every session's environment, so a name read
    from it would be every session's name.  ORCH2_AGENT_NAME is read once, by
    the launch it was set for (config.py), and never here."""
    br, _st = _bridge()
    monkeypatch.setenv("ORCH2_AGENT_NAME", "Lane C")

    assert ENV not in br._make_options().env


def _cli_env(opts) -> dict:
    """What the CLI is actually started with: the SDK layers options.env over
    this process's whole environment (subprocess_cli.py, ``process_env =
    {**inherited_env, ..., **self._options.env, ...}``)."""
    return {**os.environ, **opts.env}


def test_a_name_this_process_inherited_is_not_passed_on(monkeypatch):
    """A hub started from inside a named session -- an agent launching it
    from its Bash tool -- carries that session's name in its environment,
    and options.env can add a variable but never remove one."""
    monkeypatch.setenv(ENV, "Lane Z")
    br, _st = _bridge()

    assert ENV not in _cli_env(br._make_options())


def test_an_inherited_name_does_not_beat_the_sessions_own(monkeypatch):
    monkeypatch.setenv(ENV, "Lane Z")
    br, _st = _bridge(human_title="Lane A")

    assert _cli_env(br._make_options())[ENV] == "Lane A"


# --------------------------------------------------------------------------
# Only a name a person chose
# --------------------------------------------------------------------------

def _session_file(tmp_path, sid, records):
    from session import _sanitize_cwd
    project = tmp_path / "projects" / _sanitize_cwd(str(tmp_path / "work"))
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{sid}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                    encoding="utf-8")
    return path


def test_an_ai_summary_is_never_used_as_the_name(tmp_path):
    """The CLI withholds names "not chosen by a human" from exactly this role.
    A peer called "Fixing the parser bug in lexer.rs" is useless."""
    from session import read_human_title
    sid = "aaaa1111-0000"
    _session_file(tmp_path, sid, [
        {"type": "user", "sessionId": sid, "message": {"role": "user", "content": "x"}},
        {"type": "ai-title", "aiTitle": "Fixing the parser bug", "sessionId": sid},
    ])

    assert read_human_title(sid, str(tmp_path)) is None


def test_a_custom_title_is_used(tmp_path):
    from session import read_human_title
    sid = "bbbb2222-0000"
    _session_file(tmp_path, sid, [
        {"type": "user", "sessionId": sid, "message": {"role": "user", "content": "x"}},
        {"type": "ai-title", "aiTitle": "Some summary", "sessionId": sid},
        {"type": "custom-title", "customTitle": "Lane A", "sessionId": sid},
    ])

    assert read_human_title(sid, str(tmp_path)) == "Lane A"


def test_the_name_is_the_title_the_user_sees(tmp_path, monkeypatch):
    """A rename pin overrides a stale custom-title record the CLI wrote back;
    the tab shows the pinned title, so that is the name, too."""
    import session
    from session import read_human_title
    sid = "cccc3333-0000"
    path = _session_file(tmp_path, sid, [
        {"type": "user", "sessionId": sid, "message": {"role": "user", "content": "x"}},
        {"type": "custom-title", "customTitle": "Old", "sessionId": sid},
    ])
    monkeypatch.setattr(session, "_load_rename_pins", lambda: {
        str(path): {"title": "Lane A", "stale": ["Old"]}})

    assert read_human_title(sid, str(tmp_path)) == "Lane A"


def test_a_missing_session_has_no_human_title(tmp_path):
    from session import read_human_title

    assert read_human_title("does-not-exist", str(tmp_path)) is None


# --------------------------------------------------------------------------
# Whose name: the session the connect lands on
# --------------------------------------------------------------------------

class _Stop(Exception):
    pass


def _capture_connects(monkeypatch, titles, reads=None, fail_reads=0):
    """Stub the SDK client (record the env each connect would start the CLI
    with) and the disk read of the title (``titles`` maps sid -> title)."""
    import sdk_bridge
    envs: list[dict] = []
    failures = {"left": fail_reads}

    def fake_read(sid, cfg=None):
        if reads is not None:
            reads.append(sid)
        if failures["left"]:
            failures["left"] -= 1
            raise OSError("transcript locked")
        return titles.get(sid)

    class _Client:
        def __init__(self, options=None):
            envs.append(dict(options.env))

        async def connect(self):
            raise _Stop()

        async def disconnect(self):
            pass

    monkeypatch.setattr(sdk_bridge, "read_human_title", fake_read)
    monkeypatch.setattr(sdk_bridge, "ClaudeSDKClient", _Client)
    return envs


def _connect(br, resume_id=None):
    with pytest.raises(Exception):
        asyncio.run(br.connect(resume_id=resume_id))


def test_connect_loads_the_title_from_disk(monkeypatch):
    """After a hub restart the name is only on disk; connect must read it
    before building the options, or the restarted CLI comes up nameless."""
    envs = _capture_connects(monkeypatch, {"sid-x": "Lane A"})
    br, _st = _bridge()

    _connect(br, "sid-x")

    assert envs[-1].get(ENV) == "Lane A"


def test_a_hub_restart_names_the_cli_from_the_session_it_resumes(monkeypatch):
    """A runtime restored by the hub resumes through ``_initial_resume_id``,
    not a ``resume_id`` argument."""
    envs = _capture_connects(monkeypatch, {"sid-x": "Lane A"})
    br, _st = _bridge()
    br._initial_resume_id = "sid-x"

    _connect(br)

    assert envs[-1].get(ENV) == "Lane A"


def test_a_reconnect_to_the_same_session_does_not_reread_it(monkeypatch):
    """/model, /effort and friends reconnect the same session; its title is
    already known, and reading it means reading the transcript."""
    reads: list[str] = []
    envs = _capture_connects(monkeypatch, {"sid-x": "Lane A"}, reads)
    br, _st = _bridge()

    _connect(br, "sid-x")
    _connect(br, "sid-x")

    assert reads == ["sid-x"]
    assert envs[-1].get(ENV) == "Lane A"


def test_a_rename_in_this_runtime_is_not_undone_by_a_reconnect(monkeypatch):
    """The disk read is skipped for the session /rename just named; what it
    set must be what the next CLI starts with."""
    envs = _capture_connects(monkeypatch, {"sid-x": "Stale"})
    br, st = _bridge()
    st.session_id = "sid-x"
    monkeypatch.setattr(commands, "write_session_title", lambda *a, **k: None)
    commands.try_immediate_command("rename", "Lane A", st, parse_args([]))

    _connect(br, "sid-x")

    assert envs[-1].get(ENV) == "Lane A"


def test_another_session_does_not_inherit_the_name(monkeypatch):
    """One tab, two sessions: each answers to its own title, or to none."""
    envs = _capture_connects(monkeypatch, {"sid-a": "Lane A"})
    br, _st = _bridge()

    _connect(br, "sid-a")
    _connect(br, "sid-b")

    assert envs[0].get(ENV) == "Lane A"
    assert ENV not in envs[1]


def test_a_new_session_starts_unnamed(monkeypatch):
    """/clear: the connect at its bottom lands on no session at all."""
    envs = _capture_connects(monkeypatch, {"sid-a": "Lane A"})
    br, st = _bridge()
    _connect(br, "sid-a")
    st.session_id = None

    _connect(br)

    assert ENV not in envs[-1]


def test_a_failed_read_is_tried_again_on_the_next_connect(monkeypatch):
    """A locked transcript must cost one connect's name, not the session's."""
    envs = _capture_connects(monkeypatch, {"sid-x": "Lane A"}, fail_reads=1)
    br, _st = _bridge()

    _connect(br, "sid-x")
    _connect(br, "sid-x")

    assert ENV not in envs[0]
    assert envs[1].get(ENV) == "Lane A"


# --------------------------------------------------------------------------
# /rename
# --------------------------------------------------------------------------

def _rename(state, title, monkeypatch):
    monkeypatch.setattr(commands, "write_session_title", lambda *a, **k: None)
    return commands.try_immediate_command("rename", title, state, parse_args([]))


def _text(res) -> str:
    return " ".join((m.get("data") or {}).get("message", "") for m in res.messages)


def test_rename_records_the_title_as_human_chosen(monkeypatch):
    st = State()
    st.session_id = "sid-1"

    _rename(st, "Lane A", monkeypatch)

    assert (st.human_title, st.human_title_sid) == ("Lane A", "sid-1")


def test_rename_forwards_itself_to_the_live_cli(monkeypatch):
    """The CLI renames itself live when handed its own /rename -- measured --
    which avoids a reconnect that would reload the whole transcript."""
    st = State()
    st.session_id = "sid-1"

    res = _rename(st, "Lane A", monkeypatch)

    assert res.forward_to_sdk is True
    assert res.forward_payload == "/rename Lane A"


def test_rename_says_the_name_is_now_addressable(monkeypatch):
    """The confusion reported was exactly that a title and an address are
    different things. Say which one was set."""
    st = State()
    st.session_id = "sid-1"

    res = _rename(st, "Lane A", monkeypatch)

    assert "address" in _text(res).lower()


def test_rename_during_a_turn_says_when_it_takes_effect(monkeypatch):
    """The live rename waits for the turn; saying "now" would be the same
    misunderstanding the report was about."""
    st = State()
    st.session_id = "sid-1"
    st.busy = True

    res = _rename(st, "Lane A", monkeypatch)

    assert "once the current turn ends" in _text(res)


def test_rename_before_the_session_exists_still_names_it(monkeypatch):
    """No session id yet: the title is pending, and the CLI that is already
    running is told anyway -- its own /rename as its first input names it and
    starts the session (measured)."""
    st = State()
    st.session_id = None

    res = _rename(st, "Lane A", monkeypatch)

    assert st.pending_rename == "Lane A"
    assert res.forward_payload == "/rename Lane A"


# --------------------------------------------------------------------------
# Handing it to the bridge
# --------------------------------------------------------------------------

class _FakeRT:
    def __init__(self, busy=False, connected=True, blocked=None):
        self.bridge, self.state = _bridge()
        self.state.busy = busy
        self.state.connect_blocked_msg = blocked
        self.bridge.client = object() if connected else None
        self.rid = "R1"


def _forward(rt):
    import server
    asyncio.run(server._forward_cli_command(rt, "/rename Lane A"))


def test_an_idle_session_gets_it_straight_away():
    rt = _FakeRT()

    _forward(rt)

    assert rt.bridge._pending_cli_command == "/rename Lane A"
    assert rt.bridge.event_queue.get_nowait() == ("cli-command", "")


def test_a_busy_session_does_not_get_it_as_a_queued_prompt():
    """Queued prompts are echoed as "You: ..." when sent, and "merge all"
    would glue the next real prompt onto the rename -- which the CLI would
    then take as part of the title."""
    rt = _FakeRT(busy=True)

    _forward(rt)

    assert not rt.state.queued_prompts
    assert rt.bridge._pending_cli_command == "/rename Lane A"


def test_no_connected_cli_means_nothing_to_tell():
    """The name is passed at the next connect anyway."""
    rt = _FakeRT(connected=False)

    _forward(rt)

    assert rt.bridge._pending_cli_command is None
    assert rt.bridge.event_queue.empty()


def test_a_session_blocked_on_a_duplicate_is_left_alone():
    rt = _FakeRT(blocked="already open elsewhere")

    _forward(rt)

    assert rt.bridge._pending_cli_command is None


def test_the_newest_rename_wins():
    br, _st = _bridge()

    br.request_cli_command("/rename Lane A")
    br.request_cli_command("/rename Lane B")

    assert br._pending_cli_command == "/rename Lane B"


# --------------------------------------------------------------------------
# The exchange: sent the way a prompt is, but not a turn
# --------------------------------------------------------------------------

def _state_msg(s):
    return SystemMessage(subtype="session_state_changed",
                         data={"type": "system", "subtype": "session_state_changed",
                               "state": s, "session_id": SID})


def _init_msg(sid=SID):
    return SystemMessage(subtype="init",
                         data={"type": "system", "subtype": "init", "session_id": sid})


def _result_msg():
    return ResultMessage(subtype="success", duration_ms=90, duration_api_ms=0,
                         is_error=False, num_turns=0, session_id=SID,
                         total_cost_usd=0)


def _as_measured(text):
    """What CLI 2.1.280 sends back for /rename (see the module docstring)."""
    title = text.split(" ", 1)[1]
    return [
        _state_msg("running"),
        _init_msg(),
        AssistantMessage(content=[TextBlock(f"Session renamed to: {title}")],
                         model="<synthetic>"),
        _result_msg(),
        _state_msg("idle"),
    ]


class _CLI:
    """A connected CLI, delivered through the real dispatcher's routing rule:
    to ``turn_msg_queue`` while ``turn_active`` is set, to the ghost-turn path
    otherwise (see ``_message_dispatcher``).  One message per loop step, so
    the rule is applied at delivery time, as it is for real."""

    def __init__(self, br, script=_as_measured, burst=False):
        self.br = br
        self.script = script
        # burst: everything arrives before the consumer runs once -- output
        # that is already queued by the time the first message is looked at.
        self.burst = burst
        self.sent: list[str] = []
        self.feeds: list[asyncio.Task] = []

    async def query(self, text):
        self.sent.append(text)
        self.feeds.append(asyncio.create_task(self._feed(self.script(text))))

    async def _feed(self, msgs):
        for m in msgs:
            if not self.burst:
                await asyncio.sleep(0)
            if self.br.turn_active.is_set():
                await self.br.turn_msg_queue.put(m)
            else:
                await self.br._handle_async_message(m)

    async def settle(self):
        for t in self.feeds:
            await t


def _live(script=_as_measured, burst=False):
    cfg = parse_args([])
    st = init_state_from_config(cfg)
    sent: list[dict] = []

    async def bc(m):
        sent.append(m)

    br = SDKBridge(config=cfg, state=st, broadcaster=bc)
    cli = _CLI(br, script, burst)
    br.client = cli
    return br, st, cli, sent


def _run(br, cli):
    async def go():
        await br._run_pending_cli_command()
        await cli.settle()
    asyncio.run(go())


def test_an_idle_cli_is_renamed():
    br, _st, cli, _sent = _live()
    br.request_cli_command("/rename Lane A")

    _run(br, cli)

    assert cli.sent == ["/rename Lane A"]
    assert br._pending_cli_command is None


def test_it_is_not_a_turn():
    """No turn markers, no turn booked, no "You:" echo: nothing the user did
    reached the model."""
    br, st, cli, sent = _live()
    turns = st.turns
    br.request_cli_command("/rename Lane A")

    _run(br, cli)

    kinds = {m.get("type") for m in sent}
    assert not kinds & {"turn_start", "turn_end", "user_message"}, kinds
    assert st.turns == turns


def test_the_cli_reply_is_not_rendered_as_the_assistant():
    """/rename already answered the user; "Session renamed to: ..." arriving as
    an assistant turn would be a ghost reply to nothing."""
    br, _st, cli, sent = _live()
    br.request_cli_command("/rename Lane A")

    _run(br, cli)

    assert "Session renamed" not in json.dumps(sent)
    assert not br.state.busy


def test_it_does_not_cancel_a_scheduled_loop_wakeup():
    """run_turn cancels the model's wakeup, because a turn starting means its
    plan was superseded.  A rename supersedes nothing -- and the loops here run
    unattended, so a cancelled one is noticed hours later, if at all."""
    br, _st, cli, _sent = _live()
    br.request_cli_command("/rename Lane A")

    async def go():
        br._wakeup_task = asyncio.create_task(asyncio.sleep(60))
        await br._run_pending_cli_command()
        await cli.settle()
        cancelled = br._wakeup_task.cancelled() or br._wakeup_task.done()
        br._wakeup_task.cancel()
        return cancelled

    assert asyncio.run(go()) is False


def test_the_stream_is_released_afterwards():
    """Left set, ``turn_active`` would route the next ghost turn's output into
    a queue nobody reads."""
    br, _st, cli, _sent = _live()
    br.request_cli_command("/rename Lane A")

    _run(br, cli)

    assert not br.turn_active.is_set()


def test_the_reply_is_read_while_the_stream_is_claimed():
    """If the stream were not claimed, the reply would be a ghost turn."""
    br, st, cli, _sent = _live()
    br.request_cli_command("/rename Lane A")
    seen = []
    orig = br.client.query

    async def query(text):
        seen.append(br.turn_active.is_set())
        await orig(text)

    br.client.query = query

    _run(br, cli)

    assert seen == [True]


def test_a_brand_new_session_learns_its_id_from_the_exchange(monkeypatch):
    """The pending path: /rename is the CLI's first input, so the init that
    creates the session arrives here, and is handled as a turn's would be."""
    import sdk_bridge
    monkeypatch.setattr(sdk_bridge, "write_session_title", lambda *a, **k: None)
    br, st, cli, _sent = _live()
    st.session_id = None
    br.request_cli_command("/rename Lane A")

    _run(br, cli)

    assert st.session_id == SID


@pytest.mark.parametrize("why", ["busy", "connecting", "turn", "dead", "no-client",
                                 "cli-running"])
def test_it_waits_when_the_cli_is_not_free(why):
    """Never into a running turn -- the CLI can absorb a prompt sent there and
    drop it (``absorbed_mid_turn``) -- nor a ghost turn, a connect, a dead
    process, or a turn the CLI has announced but not yet shown output for."""
    br, st, cli, _sent = _live()
    if why == "busy":
        st.busy = True
    elif why == "connecting":
        st.connecting = True
    elif why == "turn":
        br.turn_active.set()
    elif why == "dead":
        br._transport_dead = True
    elif why == "no-client":
        br.client = None
    elif why == "cli-running":
        br._cli_state = "running"
    br.request_cli_command("/rename Lane A")

    _run(br, cli)

    assert cli.sent == []
    assert br._pending_cli_command == "/rename Lane A", "dropped instead of kept"


def _model_turn(text):
    return [
        _state_msg("running"),
        _init_msg(),
        AssistantMessage(content=[TextBlock("OK")], model="claude-opus-5"),
        _result_msg(),
        _state_msg("idle"),
    ]


def test_a_rename_after_a_real_turn_is_not_held_up():
    """Found by the end-to-end run against CLI 2.1.280, not by the tests: the
    CLI's "idle" arrives in the same instant as the result (measured), so the
    dispatcher routes it into the turn's queue -- which the next turn start
    throws away unread.  Waiting for it alone left every rename made after a
    real turn pending indefinitely."""
    br, _st, cli, _sent = _live(_model_turn, burst=True)

    async def go():
        await br.run_turn("Reply with just the word OK.")
        await cli.settle()
        cli.script = _as_measured
        br.request_cli_command("/rename Lane A")
        await br._run_pending_cli_command()
        await cli.settle()

    asyncio.run(go())

    assert cli.sent == ["Reply with just the word OK.", "/rename Lane A"]


def test_a_second_rename_is_not_held_up_by_the_first():
    """The exchange's own "idle" lands the same way."""
    br, _st, cli, _sent = _live(burst=True)

    async def go():
        br.request_cli_command("/rename Lane A")
        await br._run_pending_cli_command()
        await cli.settle()
        br.request_cli_command("/rename Lane B")
        await br._run_pending_cli_command()
        await cli.settle()

    asyncio.run(go())

    assert cli.sent == ["/rename Lane A", "/rename Lane B"]


def test_the_cli_saying_it_started_a_turn_is_recorded():
    """A background task's notification starts a turn seconds before its
    first output flips ``state.busy``; "running" is the only early signal."""
    br, _st, _cli, _sent = _live()

    asyncio.run(br._handle_async_message(_state_msg("running")))

    assert br._cli_state == "running"


def test_a_turn_that_got_there_first_is_rendered_not_swallowed():
    """The model's answer to a background task, arriving in the instant we
    looked idle, belongs on screen -- all of it, not only the first message."""
    def model_first(text):
        return [
            AssistantMessage(content=[TextBlock("The build finished.")],
                             model="claude-opus-5"),
            AssistantMessage(content=[TextBlock("All 312 tests pass.")],
                             model="claude-opus-5"),
        ]

    br, _st, cli, sent = _live(model_first)
    br.request_cli_command("/rename Lane A")

    _run(br, cli)

    rendered = json.dumps(sent)
    assert "The build finished." in rendered
    assert "All 312 tests pass." in rendered
    assert not br.turn_active.is_set()


def test_output_already_queued_behind_it_is_rendered_too():
    """Once the stream is released the dispatcher sends the rest of the turn
    to the ghost path itself -- but whatever it had already queued for us
    would otherwise sit there until the next turn start throws it away."""
    def model_burst(text):
        return [
            AssistantMessage(content=[TextBlock("The build finished.")],
                             model="claude-opus-5"),
            AssistantMessage(content=[TextBlock("All 312 tests pass.")],
                             model="claude-opus-5"),
        ]

    br, _st, cli, sent = _live(model_burst, burst=True)
    br.request_cli_command("/rename Lane A")

    _run(br, cli)

    assert "All 312 tests pass." in json.dumps(sent)
    assert br.turn_msg_queue.empty()


def test_a_tool_call_is_the_model_too():
    def model_tool(text):
        return [AssistantMessage(
            content=[ToolUseBlock(id="t1", name="Bash", input={"command": "ls"})],
            model="claude-opus-5")]

    br, st, cli, _sent = _live(model_tool)
    br.request_cli_command("/rename Lane A")

    _run(br, cli)

    assert st.busy, "the model's turn was not handed to the ghost-turn path"


def test_a_cli_that_never_answers_releases_the_stream():
    br, _st, cli, _sent = _live(lambda text: [])
    br.CLI_COMMAND_TIMEOUT_S = 0.05
    br.request_cli_command("/rename Lane A")

    _run(br, cli)

    assert not br.turn_active.is_set()


def test_a_dead_cli_during_the_exchange_is_recovered():
    """With the stream claimed, the dispatcher leaves recovery to the turn in
    progress.  There is none -- so the exchange must ask for it."""
    br, _st, cli, _sent = _live(lambda text: [DISPATCHER_DEAD])
    br.request_cli_command("/rename Lane A")

    _run(br, cli)

    from sdk_bridge import _CONNECT_TRANSPORT_DEAD
    assert ("connect", _CONNECT_TRANSPORT_DEAD) in list(br.event_queue._queue)


# --------------------------------------------------------------------------
# When it runs: the worker's idle points
# --------------------------------------------------------------------------

def test_the_parked_worker_runs_it_and_keeps_waiting():
    br, st, cli, _sent = _live()

    async def go():
        br.request_cli_command("/rename Lane A")
        br.event_queue.put_nowait(("message", "hello"))
        prompt = await asyncio.wait_for(br._await_next_prompt(), 2)
        await cli.settle()
        return prompt

    assert asyncio.run(go()) == "hello"
    assert cli.sent == ["/rename Lane A"]


def test_a_rename_typed_during_a_turn_runs_when_it_ends():
    """And before the next queued prompt starts a turn of its own."""
    br, st, cli, sent = _live()
    st.queued_prompts.append("next")
    br.request_cli_command("/rename Lane A")

    async def go():
        prompt = await asyncio.wait_for(br._between_turns("", False), 2)
        await cli.settle()
        return prompt

    assert asyncio.run(go()) == "next"
    assert cli.sent == ["/rename Lane A"]


def test_the_clis_idle_wakes_a_worker_that_found_it_still_running():
    """"idle" arrives just after the result, so the end-of-turn attempt can
    see "running" and decline; the "idle" itself must then wake the worker."""
    br, _st, _cli, _sent = _live()
    br._cli_state = "running"
    br.request_cli_command("/rename Lane A")
    while not br.event_queue.empty():
        br.event_queue.get_nowait()

    asyncio.run(br._handle_async_message(_state_msg("idle")))

    assert br._cli_state == "idle"
    assert br.event_queue.get_nowait() == ("cli-command", "")


def test_the_end_of_a_ghost_turn_wakes_the_worker_for_it():
    """The parked worker declined it while the ghost turn held the CLI."""
    br, _st, _cli, _sent = _live()

    async def go():
        await br._begin_ghost_turn_if_needed()
        br.request_cli_command("/rename Lane A")
        while not br.event_queue.empty():
            br.event_queue.get_nowait()
        await br._end_ghost_turn("success")

    asyncio.run(go())

    assert ("cli-command", "") in list(br.event_queue._queue)


def test_clear_drops_a_waiting_rename():
    """It named the session being wiped; run afterwards it would name the new
    one."""
    br, _st, _cli, _sent = _live()

    async def nothing(*a, **k):
        pass

    br.disconnect = nothing
    br.connect = nothing
    br.request_cli_command("/rename Lane A")

    asyncio.run(br._clear_context())

    assert br._pending_cli_command is None


# --------------------------------------------------------------------------
# Through the real WS router
# --------------------------------------------------------------------------

class _WS:
    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))


@pytest.fixture
def hub(monkeypatch):
    import server
    from session_runtime import SessionRuntime
    cfg = parse_args([])
    st = State()
    st.session_id = "sid-live"
    rt = SessionRuntime(config=cfg, state=st, rid="R1")

    async def bc(m):
        pass

    rt.bridge = SDKBridge(config=cfg, state=st, broadcaster=bc)
    rt.bridge.client = object()
    saved = (dict(server.runtimes), server._default_runtime, server.config,
             server.state, server.bridge, dict(server._ws_runtime))
    server.runtimes.clear()
    server.runtimes[rt.rid] = rt
    server._default_runtime = rt
    server.config = cfg
    server.state = st
    server.bridge = rt.bridge
    monkeypatch.setattr(commands, "write_session_title", lambda *a, **k: None)
    yield rt
    server.runtimes.clear()
    server.runtimes.update(saved[0])
    server._default_runtime = saved[1]
    server.config = saved[2]
    server.state = saved[3]
    server.bridge = saved[4]
    server._ws_runtime.clear()
    server._ws_runtime.update(saved[5])


def _slash_rename(ws):
    import server
    return server._handle_ws_message(ws, {"type": "message", "text": "/rename Lane A"})


def test_the_router_hands_rename_to_the_bridge(hub):
    ws = _WS()

    asyncio.run(_slash_rename(ws))

    assert hub.bridge._pending_cli_command == "/rename Lane A"


def test_the_router_never_queues_rename_as_a_prompt(hub):
    """The generic forward (`_enqueue_prompt`) queues when busy -- echoed on
    send, mergeable into the next prompt -- and sends straight into the CLI
    when idle, as a turn."""
    hub.state.busy = True
    ws = _WS()

    asyncio.run(_slash_rename(ws))

    assert not hub.state.queued_prompts
    assert ("message", "/rename Lane A") not in list(hub.bridge.event_queue._queue)


def test_the_router_does_not_echo_it_as_something_said_to_the_model(hub):
    """The user typed our /rename, which answered already. Echoing the forward
    as "You: /rename Lane A" would read as a message to the model."""
    ws = _WS()

    asyncio.run(_slash_rename(ws))

    assert not [m for m in ws.sent if m.get("type") == "user_message"]
