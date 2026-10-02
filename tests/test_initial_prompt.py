"""A launch's --initial-prompt goes to the session that launch opened: once,
shown, and in a turn of its own.

Reported 2026-09-27, after starting the Slate OS lanes with
``--initial-prompt "Continue. If any background processes were running they
may be gone."``: "in some of the sessions i saw only my initial prompt sent,
in some i didn't and saw only this sent: [the lost-background-work notice]
... and when i restarted this session, it automatically started working again
and i have no idea why. i didn't start it with any --initial-prompt."

From the hub log and the lanes' transcripts:

* The first lane launch started the hub, so the hub's own Config carried its
  prompt, and ``_create_runtime`` cloned that Config for every session opened
  afterwards.  That included this orchestrator2 session, opened with no
  --initial-prompt at all, which was sent "Continue..." at 16:46:35.
* The lanes that *joined* the hub never handed theirs over
  (``_hub_launch_kwargs`` had no initial_prompt).  They got the hub's by
  inheritance, which happened to be the same text.
* The prompt went straight to ``run_turn``, which echoes nothing.  A tab
  attached before the CLI wrote it to the transcript never showed it.
* A lane that came back with lost background tasks started a turn of its own
  as soon as its CLI was up, reporting them stopped.  The prompt, sent 30-100
  ms after connecting, went into that turn ("run_turn ended on a turn that
  was not ours").  In one lane the CLI later merged it with the lost-work
  notice into a single message.
* ``/resume`` and ``/cwd`` rebuilt the primary session from the hub's Config,
  and a restart re-ran the launch command, so either re-sent the prompt.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import time
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock  # noqa: E402

import sdk_bridge                                        # noqa: E402
import server                                            # noqa: E402
from config import parse_args                            # noqa: E402
from sdk_bridge import SDKBridge                         # noqa: E402
from state import State, init_state_from_config          # noqa: E402

PROMPT = "Continue. If any background processes were running they may be gone."
SETTLE = 0.2


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(sdk_bridge, "TURN_END_SETTLE_S", SETTLE)
    monkeypatch.setenv("ORCH2_AGENT_DB", str(tmp_path / "agents.db"))
    monkeypatch.delenv("ORCH2_AGENT_NAME", raising=False)


# --------------------------------------------------------------------------
# The bridge: queued, shown, and kept out of the CLI's own turns
# --------------------------------------------------------------------------

class _CLI:
    """A ClaudeSDKClient that connects at once, then streams ``script`` --
    (delay, message) pairs -- by itself, as a CLI starting its own turn does."""

    script: list = []
    on_connect = None

    def __init__(self, options=None):
        self.options = options

    async def connect(self):
        if _CLI.on_connect is not None:
            _CLI.on_connect()

    async def disconnect(self):
        pass

    async def receive_messages(self):
        for delay, msg in list(_CLI.script):
            await asyncio.sleep(delay)
            yield msg
        while True:
            await asyncio.sleep(3600)
            yield None      # pragma: no cover


@pytest.fixture
def cli(monkeypatch):
    _CLI.script = []
    _CLI.on_connect = None
    monkeypatch.setattr(sdk_bridge, "ClaudeSDKClient", _CLI)
    return _CLI


def _bridge(tmp_path, argv=()):
    cfg = dataclasses.replace(parse_args(list(argv)), cwd=str(tmp_path))
    st = init_state_from_config(cfg)
    sent: list[dict] = []

    async def bc(m):
        sent.append(m)

    return SDKBridge(config=cfg, state=st, broadcaster=bc), st, sent


def _run_worker(br, *, until, timeout=5.0, skip_connect=False):
    """Run the real worker_loop and the real connect() against the fake CLI
    until ``until(asked)``; run_turn records what it is asked, and when
    (``time.monotonic()``)."""
    asked: list[str] = []
    at: list[float] = []

    async def fake_run_turn(prompt):
        asked.append(prompt)
        at.append(time.monotonic())
        return "", False

    br.run_turn = fake_run_turn

    async def go():
        task = asyncio.ensure_future(br.worker_loop(skip_connect=skip_connect))
        deadline = time.monotonic() + timeout
        try:
            while not until(asked) and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
        finally:
            br.stop_event.set()
            br.event_queue.put_nowait(("quit", ""))
            try:
                await asyncio.wait_for(task, 3)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
            if br._dispatcher_task is not None:
                br._dispatcher_task.cancel()
                await asyncio.gather(br._dispatcher_task, return_exceptions=True)

    asyncio.run(go())
    return asked, at


def test_it_waits_in_the_queue_while_the_session_connects(cli, tmp_path):
    """On the panel from the start, like a prompt typed during the connect."""
    br, st, _sent = _bridge(tmp_path, ["--initial-prompt", PROMPT])
    during: list[list[str]] = []
    cli.on_connect = lambda: during.append(list(st.queued_prompts))

    asked, _ = _run_worker(br, until=lambda a: a)

    assert during == [[PROMPT]]
    assert asked == [PROMPT]


def test_it_is_shown_when_it_goes_out(cli, tmp_path):
    """It used to go straight to run_turn, which shows nothing."""
    br, _st, sent = _bridge(tmp_path, ["--initial-prompt", PROMPT])

    _run_worker(br, until=lambda a: a)

    assert [m for m in sent if m.get("type") == "user_message"] == [
        {"type": "user_message", "content": PROMPT}]


def test_it_waits_out_a_turn_the_cli_starts_as_it_connects(cli, tmp_path):
    """The report's lanes: the CLI reports the lost tasks in a turn of its
    own the moment it is up.  The prompt must get a turn after that one, not
    be folded into it."""
    cli.script = [
        (0.02, AssistantMessage(content=[TextBlock("the tasks were stopped")],
                                model="claude-x")),
        (0.5, ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                            is_error=False, num_turns=1, session_id="sid-1",
                            origin={"kind": "task-notification"})),
    ]
    br, _st, _sent = _bridge(tmp_path, ["--initial-prompt", PROMPT])
    connected: list[float] = []
    cli.on_connect = lambda: connected.append(time.monotonic())

    asked, at = _run_worker(br, until=lambda a: a)

    assert asked == [PROMPT]
    # The CLI's turn ends 0.52 s after the connect; the prompt must follow it.
    took = at[0] - connected[0]
    assert took >= 0.52 + SETTLE * 0.9, (
        f"sent {took:.2f}s after connecting: into the CLI's own turn")


def test_without_one_it_goes_out_once_the_connect_has_settled(cli, tmp_path):
    br, _st, _sent = _bridge(tmp_path, ["--initial-prompt", PROMPT])
    connected: list[float] = []
    cli.on_connect = lambda: connected.append(time.monotonic())

    asked, at = _run_worker(br, until=lambda a: a)

    assert asked == [PROMPT]
    # From the connect, not from the worker's start: how long the connect
    # itself takes under load is not the thing being measured.
    assert at[0] - connected[0] >= SETTLE * 0.9, "sent before the connect settled"


def test_a_connect_starts_the_settle(cli, tmp_path):
    """Every connect, not only the first: a reconnect orphans running tasks
    too, and the new CLI reports them in a turn of its own."""
    br, _st, _sent = _bridge(tmp_path)

    async def go():
        before = time.monotonic()
        await br.connect()
        br._dispatcher_task.cancel()
        await asyncio.gather(br._dispatcher_task, return_exceptions=True)
        return before

    before = asyncio.run(go())

    assert br._turn_ended_at is not None and br._turn_ended_at >= before


def test_it_is_sent_once(cli, tmp_path):
    br, _st, _sent = _bridge(tmp_path, ["--initial-prompt", PROMPT])

    asked, _ = _run_worker(br, until=lambda a: len(a) >= 2, timeout=3.0)

    assert asked == [PROMPT]


def test_a_queue_restored_from_disk_does_not_get_it_twice(cli, tmp_path):
    """A launch that never finished connecting left it in the saved queue;
    the same launch run again must not queue it a second time."""
    br, st, _sent = _bridge(tmp_path, ["--initial-prompt", PROMPT])
    st.queued_prompts.append(PROMPT)

    asked, _ = _run_worker(br, until=lambda a: len(a) >= 2, timeout=3.0)

    assert asked == [PROMPT]


def test_a_restarted_worker_does_not_send_it_again(cli, tmp_path):
    br, st, _sent = _bridge(tmp_path, ["--initial-prompt", PROMPT])

    asked, _ = _run_worker(br, until=lambda a: a, timeout=0.6, skip_connect=True)

    assert asked == [] and list(st.queued_prompts) == []


def test_a_prompt_left_from_an_earlier_run_goes_before_it(cli, tmp_path):
    """In the order they were queued: the restored one was typed first."""
    br, st, _sent = _bridge(tmp_path, ["--initial-prompt", PROMPT])
    st.queued_prompts.append("typed before the hub went down")

    asked, _ = _run_worker(br, until=lambda a: len(a) >= 2)

    assert asked == ["typed before the hub went down", PROMPT]


def test_a_restarted_worker_still_sends_what_is_queued(cli, tmp_path):
    """No connect, so no poke: only the worker's own first pop sends it."""
    br, st, _sent = _bridge(tmp_path)
    st.queued_prompts.append("typed while the worker was down")
    # The poke that queuing it fired went to the worker that died.
    while not br.event_queue.empty():
        br.event_queue.get_nowait()

    asked, _ = _run_worker(br, until=lambda a: a, timeout=5.0, skip_connect=True)

    assert asked == ["typed while the worker was down"]


def test_a_prompt_typed_during_the_connect_comes_after_it(cli, tmp_path):
    br, st, _sent = _bridge(tmp_path, ["--initial-prompt", PROMPT])
    cli.on_connect = lambda: st.queued_prompts.append("typed while it connected")

    asked, _ = _run_worker(br, until=lambda a: len(a) >= 2)

    assert asked == [PROMPT, "typed while it connected"]


def test_a_session_without_one_sends_nothing_by_itself(cli, tmp_path):
    br, st, sent = _bridge(tmp_path)

    asked, _ = _run_worker(br, until=lambda a: a, timeout=0.6)

    assert asked == [] and list(st.queued_prompts) == []
    assert not [m for m in sent if m.get("type") == "user_message"]


# --------------------------------------------------------------------------
# The hub: the prompt belongs to the launch that gave it
# --------------------------------------------------------------------------

def _config_of_new_runtime(monkeypatch, tmp_path, hub_argv, **kw):
    """The Config _create_runtime builds, caught before any bridge starts."""
    monkeypatch.setattr(server, "config", parse_args(hub_argv))
    seen = {}

    def stop(cfg):
        seen["cfg"] = cfg
        raise RuntimeError("stop here")

    monkeypatch.setattr(server, "init_state_from_config", stop)
    with pytest.raises(RuntimeError):
        asyncio.run(server._create_runtime(cwd=str(tmp_path), no_continue=True, **kw))
    return seen["cfg"]


def test_a_session_the_hub_opens_does_not_get_the_hubs_prompt(monkeypatch, tmp_path):
    """The report: this session was opened with no --initial-prompt and was
    sent the lanes' "Continue..." as soon as it connected."""
    cfg = _config_of_new_runtime(monkeypatch, tmp_path, ["--initial-prompt", PROMPT])

    assert cfg.initial_prompt is None


def test_a_session_opened_with_one_gets_its_own(monkeypatch, tmp_path):
    cfg = _config_of_new_runtime(monkeypatch, tmp_path, ["--initial-prompt", PROMPT],
                                 initial_prompt="look at lane B")

    assert cfg.initial_prompt == "look at lane B"


def test_a_blank_one_is_none(monkeypatch, tmp_path):
    cfg = _config_of_new_runtime(monkeypatch, tmp_path, [], initial_prompt="  \n ")

    assert cfg.initial_prompt is None


def test_nor_the_hubs_session_note(monkeypatch, tmp_path):
    """The other one-shot input, the same fault: a note meant for the moved
    session was prepended to every session the hub opened later."""
    cfg = _config_of_new_runtime(monkeypatch, tmp_path, ["--session-note", "you moved"])

    assert cfg.session_note is None


def test_a_session_opened_with_a_note_still_gets_it(monkeypatch, tmp_path):
    cfg = _config_of_new_runtime(monkeypatch, tmp_path, [], session_note="you moved")

    assert cfg.session_note == "you moved"


def test_the_hand_over_includes_it():
    for flag in ("--initial-prompt", "-p"):
        kw = server._hub_launch_kwargs(parse_args([flag, PROMPT]), None)
        assert kw["initial_prompt"] == PROMPT, flag


def test_the_hand_over_puts_it_on_the_wire(monkeypatch):
    import urllib.request
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true, "rid": "R9"}'

    def fake_urlopen(req, timeout=None):
        seen.update(json.loads(req.data.decode("utf-8")))
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    server._launch_into_hub(8787, **server._hub_launch_kwargs(
        parse_args(["--cwd", "C:\\w", "--initial-prompt", PROMPT]), None))

    assert seen["initial_prompt"] == PROMPT


def _launch_new(monkeypatch, tmp_path, body):
    got = {}

    class _RT:
        rid = "R7"

    async def fake_create_runtime(**kw):
        got.update(kw)
        return _RT()

    monkeypatch.setattr(server, "_create_runtime", fake_create_runtime)
    monkeypatch.setattr(server, "config", parse_args([]))
    asyncio.run(server.api_session_launch(
        {"cwd": str(tmp_path), "no_continue": True, **body}))
    return got


def test_the_hub_opens_the_session_with_it(monkeypatch, tmp_path):
    got = _launch_new(monkeypatch, tmp_path, {"initial_prompt": PROMPT})

    assert got["initial_prompt"] == PROMPT


@pytest.mark.parametrize("value", [None, "", "   ", 42, ["x"]])
def test_a_launch_without_a_usable_one_passes_none(monkeypatch, tmp_path, value):
    got = _launch_new(monkeypatch, tmp_path, {"initial_prompt": value})

    assert got["initial_prompt"] is None


def _reuse(monkeypatch, body, *, queued=()):
    """Launch at a session the hub already has open."""
    st = State()
    st.session_id = "sid-live"
    st.queued_prompts.extend(queued)
    cfg = parse_args([])
    br = types.SimpleNamespace(event_queue=asyncio.Queue(), config=cfg)
    rt = types.SimpleNamespace(rid="R1", state=st, config=cfg, bridge=br)
    monkeypatch.setattr(server, "runtimes", {"R1": rt})
    monkeypatch.setattr(server, "config", parse_args([]))
    res = asyncio.run(server.api_session_launch(
        {"cwd": "C:\\w", "resume": "sid-live", **body}))
    return res, rt


def test_a_session_that_is_already_open_is_sent_it(monkeypatch):
    """Queued like a prompt typed there: sent when the session is next free,
    shown then."""
    res, rt = _reuse(monkeypatch, {"initial_prompt": PROMPT})

    assert res["reused"] is True
    assert list(rt.state.queued_prompts) == [PROMPT]


def test_the_same_launch_again_does_not_queue_it_twice(monkeypatch):
    _res, rt = _reuse(monkeypatch, {"initial_prompt": PROMPT}, queued=[PROMPT])

    assert list(rt.state.queued_prompts) == [PROMPT]


def test_an_open_session_is_sent_nothing_by_a_launch_without_one(monkeypatch):
    _res, rt = _reuse(monkeypatch, {})

    assert list(rt.state.queued_prompts) == []


class _FakeBridge:
    """Stands in for SDKBridge inside _reconfigure: records its config."""

    made: list = []

    def __init__(self, config, state, broadcaster):
        self.config = config
        self._initial_resume_id = None
        _FakeBridge.made.append(self)

    async def start(self):
        pass

    async def stop(self):
        pass


def _reconfigure(monkeypatch, tmp_path, *, picking):
    _FakeBridge.made = []
    monkeypatch.setattr(sdk_bridge, "SDKBridge", _FakeBridge)
    monkeypatch.setattr(server, "_attach_queue_persistence", lambda st, cwd: 0)
    monkeypatch.setattr(server, "config", parse_args(
        ["--initial-prompt", PROMPT, "--session-note", "you moved"]))
    monkeypatch.setattr(server, "state", State())
    monkeypatch.setattr(server, "bridge", None)
    monkeypatch.setattr(server, "_default_runtime", None)
    monkeypatch.setattr(server, "_picker_mode", picking)
    monkeypatch.chdir(tmp_path)
    ok, err = asyncio.run(server._reconfigure(cwd=str(tmp_path)))
    assert ok, err
    return _FakeBridge.made[-1].config


def test_moving_the_primary_session_does_not_send_it_again(monkeypatch, tmp_path):
    """/cwd and /resume rebuilt it from the hub's Config, prompt and all."""
    cfg = _reconfigure(monkeypatch, tmp_path, picking=False)

    assert cfg.initial_prompt is None and cfg.session_note is None


def test_the_session_picked_in_the_picker_does_get_it(monkeypatch, tmp_path):
    """`--resume` with no id opens the picker; the session picked there is
    the one this launch opened."""
    cfg = _reconfigure(monkeypatch, tmp_path, picking=True)

    assert cfg.initial_prompt == PROMPT and cfg.session_note == "you moved"


def test_a_restart_does_not_send_it_again(monkeypatch):
    """A restart is not the launch again."""
    import subprocess
    seen = {}

    def fake_popen(args, **kw):
        seen["argv"] = list(args)
        raise OSError("not really")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sys, "argv", [
        "server.py", "--initial-prompt", PROMPT, "-p", "again", "-pglued",
        "--initial-prompt=joined", "--session-note", "you moved",
        "--session-note=joined", "--model", "claude-opus-5-5", "--open"])
    monkeypatch.setattr(server, "config", parse_args([]))
    st = State()
    st.session_id = "sid-1"
    monkeypatch.setattr(server, "state", st)

    asyncio.run(server.api_restart())

    argv = seen["argv"]
    for gone in (PROMPT, "again", "-pglued", "--initial-prompt", "-p",
                 "--initial-prompt=joined", "--session-note", "you moved",
                 "--session-note=joined"):
        assert gone not in argv, (gone, argv)
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
