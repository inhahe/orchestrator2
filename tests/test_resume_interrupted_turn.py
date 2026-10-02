"""What happens to a turn that was cut off, when its session comes back.

2026-09-06: three restarted SlateOS sessions each showed a prompt the user
never typed -- *"Continue from where you left off."* -- answered by *"No
response requested."*, and the interrupted work was not picked back up.

It is not orchestrator2's text and never was.  It comes from the **CLI's own**
conversation recovery (``src/utils/conversationRecovery.ts``): when a resumed
session's last turn was cut off mid-flight, it appends a *synthetic* user
message with ``isMeta: true``, plus an assistant sentinel so the transcript
stays API-valid if nothing acts on it.  ``CLAUDE_CODE_RESUME_INTERRUPTED_TURN``
makes the CLI act on it instead -- remove the pair, run the turn -- and from
then on orchestrator2 set it on every connect.

2026-09-29: *"sessions would often start working automatically when i resumed
them, when i don't think they had had anything scheduled, and without showing
me the prompt that caused it to start working again."*  Asked what a resumed
session should do, the answer was: wait for me.

So it is now set only where finishing the turn is what anyone would expect:

* a **recovery** -- a turn was running here when the CLI was replaced: it
  died under the turn, or a stuck turn is reconnected.  (``/model`` and a CLI
  recycle reconnect between turns, so a turn left waiting stays waiting);
* a ``/move``, which cut the turn itself;
* an open under ``--resume-interrupted-turn``.

Every other connect sets it to ``"0"``, and the tab says the turn was cut off
instead (tests/test_cut_off_turn.py).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server                                         # noqa: E402
from config import parse_args                        # noqa: E402
from sdk_bridge import SDKBridge                      # noqa: E402
from state import init_state_from_config              # noqa: E402

ENV_VAR = "CLAUDE_CODE_RESUME_INTERRUPTED_TURN"
SID = "1aa74fb0-ca4e-42cb-80c3-ef7302fee0a4"


def _bridge(argv=(), config=None):
    cfg = parse_args(list(argv))
    state = init_state_from_config(cfg)

    async def bcast(_msg):
        pass

    return SDKBridge(config=config or cfg, state=state, broadcaster=bcast)


def _env_of(opts):
    env = getattr(opts, "env", None)
    if env is None and isinstance(opts, dict):
        env = opts.get("env")
    return env or {}


def _env(argv=(), *, resume_id=SID, recovering=False):
    """The env orchestrator2 hands the CLI subprocess."""
    br = _bridge(argv)
    return _env_of(br._make_options(resume_id=resume_id, recovering=recovering))


# ---------------------------------------------------------------------------
# Opening a session leaves a cut-off turn waiting
# ---------------------------------------------------------------------------

def test_opening_a_session_does_not_finish_its_cut_off_turn():
    """The report: a session opened from the lobby, with --resume, or after a
    restart started working with nobody having asked it to."""
    assert _env().get(ENV_VAR) == "0"


def test_it_is_turned_off_not_left_out(monkeypatch):
    """The CLI reads it as a boolean, and the SDK lays ``env`` over the hub's
    own environment -- which it can add to but not remove from.  Left out, an
    inherited "1" would turn it back on."""
    monkeypatch.setenv(ENV_VAR, "1")

    assert _env().get(ENV_VAR) == "0"


def test_a_fresh_session_is_not_affected():
    """Nothing to resume, nothing cut off."""
    assert _env(["--no-continue"], resume_id=None).get(ENV_VAR) == "0"


# ---------------------------------------------------------------------------
# ...while a recovery finishes it
# ---------------------------------------------------------------------------

def test_a_recovery_finishes_it():
    """This runtime's own session, whose CLI died mid-turn under it: the turn
    is one the user was watching run, and nobody opened anything."""
    assert _env(recovering=True).get(ENV_VAR) == "1"


def _reconnect(br):
    """reconnect(), recording what it asked connect() for."""
    br.state.session_id = SID
    seen: list[dict] = []

    async def no_disconnect():
        pass

    async def fake_connect(resume_id=None, *, recovering=False):
        seen.append({"resume_id": resume_id, "recovering": recovering})

    br.disconnect = no_disconnect
    br.connect = fake_connect
    asyncio.run(br.reconnect())
    return seen[-1]


def test_a_cli_that_died_mid_turn_is_a_recovery():
    """The turn was running here when the CLI went; the new one finishes it.
    The turn has unwound by the time the reconnect runs, so what counts is
    what was running when the death was noticed."""
    br = _bridge()
    br.turn_active.set()
    br._note_transport_death("exit code 3")
    br.turn_active.clear()               # run_turn unwinds

    assert _reconnect(br) == {"resume_id": SID, "recovering": True}


def test_so_is_one_that_died_under_a_ghost_turn():
    br = _bridge()
    br.state.busy = True
    br._note_transport_death("exit code 3")
    br.state.busy = False

    assert _reconnect(br)["recovering"] is True


def test_so_is_reconnecting_a_turn_that_is_stuck():
    """/connect on a session still marked working."""
    br = _bridge()
    br.state.busy = True

    assert _reconnect(br)["recovering"] is True


def test_a_reconnect_between_turns_is_not():
    """/model, /effort, a CLI recycle: they wait for a quiet moment.  A turn
    left waiting when the session was opened must not be finished by a model
    switch."""
    assert _reconnect(_bridge())["recovering"] is False


def test_nor_is_a_cli_that_died_while_parked():
    br = _bridge()
    br._note_transport_death("exit code 3")

    assert _reconnect(br)["recovering"] is False


def test_a_death_is_forgotten_once_recovered():
    br = _bridge()
    br.turn_active.set()
    br._note_transport_death("exit code 3")
    br.turn_active.clear()
    _reconnect(br)
    br._transport_dead = False           # the new CLI connected

    assert _reconnect(br)["recovering"] is False


def test_only_reconnect_can_recover():
    """The worker's first connect (and its retries) opens the session."""
    src = inspect.getsource(SDKBridge)
    calls = [line.strip() for line in src.splitlines()
             if "self.connect(" in line and "def " not in line]
    recovering = [c for c in calls if "recovering=" in c]

    assert recovering == ["await self.connect(resume_id=sid, recovering=recovering)"], calls


def test_the_flag_brings_the_old_behaviour_back():
    assert _env(["--resume-interrupted-turn"]).get(ENV_VAR) == "1"


def test_the_flags():
    assert parse_args([]).resume_interrupted_turn is False
    assert parse_args(["--resume-interrupted-turn"]).resume_interrupted_turn is True
    assert parse_args(["--no-resume-interrupted-turn"]).resume_interrupted_turn is False


def test_a_config_built_by_hand_waits_too():
    from config import Config

    assert Config().resume_interrupted_turn is False


def test_a_config_without_the_attribute_leaves_it_waiting():
    """``_make_options`` reads this with ``getattr``, as it reads every other
    optional config attr, because ``SDKBridge`` is routinely built against
    hand-built config objects that predate the field.  Their fallback is the
    default: waiting."""
    from types import SimpleNamespace

    cfg = parse_args([])
    bare = SimpleNamespace(**{k: v for k, v in vars(cfg).items()
                              if k != "resume_interrupted_turn"})
    assert not hasattr(bare, "resume_interrupted_turn")

    env = _env_of(_bridge(config=bare)._make_options(resume_id=SID))

    assert env.get(ENV_VAR) == "0"


# ---------------------------------------------------------------------------
# The notice that explains a finished turn is armed only when one is finished
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv,recovering,armed", [
    ([], False, False),                              # an open: the turn waits
    ([], True, True),                                # a recovery: it runs
    (["--resume-interrupted-turn"], False, True),    # opted back in
])
def test_the_ghost_turn_notice_follows_it(argv, recovering, armed):
    br = _bridge(argv)
    br._make_options(resume_id=SID, recovering=recovering)

    assert br._unprompted_resume_pending is armed
    assert br._finishing_cut_off_turn is armed


# ---------------------------------------------------------------------------
# ...without disturbing the rest of the environment
# ---------------------------------------------------------------------------

def test_the_other_env_vars_survive():
    """``env`` is merged onto the inherited environment by the SDK, so adding
    one var must not displace the others."""
    env = _env()
    assert env.get("CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS") == "1"


def test_prompt_cache_workaround_still_composes():
    env = _env(["--disable-prompt-cache"], recovering=True)
    assert env.get("DISABLE_PROMPT_CACHING") == "1"
    assert env.get(ENV_VAR) == "1"


def test_a_cross_account_session_still_gets_its_config_dir(tmp_path):
    env = _env(["--config-dir", str(tmp_path)])
    assert env.get("CLAUDE_CONFIG_DIR") == str(tmp_path)
    assert env.get(ENV_VAR) == "0"


@pytest.mark.parametrize("argv", [
    [],
    ["--no-resume-interrupted-turn"],
    ["--resume-interrupted-turn"],
    ["--disable-prompt-cache"],
])
def test_the_env_is_always_a_flat_string_map(argv):
    """A non-string value here fails deep inside subprocess spawning, where the
    error names neither the variable nor this file."""
    for k, v in _env(argv).items():
        assert isinstance(k, str) and isinstance(v, str), (k, v)


# ---------------------------------------------------------------------------
# The hub: which sessions are opened finishing it
# ---------------------------------------------------------------------------

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


def test_a_session_the_hub_opens_waits(monkeypatch, tmp_path):
    cfg = _config_of_new_runtime(monkeypatch, tmp_path, [])

    assert cfg.resume_interrupted_turn is False


def test_a_hub_started_with_the_flag_opens_them_all_finishing(monkeypatch, tmp_path):
    """A preference for the hub, not a one-shot input like --initial-prompt."""
    cfg = _config_of_new_runtime(monkeypatch, tmp_path, ["--resume-interrupted-turn"])

    assert cfg.resume_interrupted_turn is True


@pytest.mark.parametrize("hub_argv", [[], ["--resume-interrupted-turn"]])
@pytest.mark.parametrize("asked", [True, False])
def test_a_launch_says_for_its_own_session(monkeypatch, tmp_path, hub_argv, asked):
    cfg = _config_of_new_runtime(monkeypatch, tmp_path, hub_argv,
                                 resume_interrupted_turn=asked)

    assert cfg.resume_interrupted_turn is asked


def test_move_finishes_the_turn_it_cut():
    """The move stops the original mid-turn if it was in one; the copy -- and
    the original, reopened when the move fails -- carries on with it."""
    src = inspect.getsource(server._do_move)

    assert src.count("resume_interrupted_turn=True") == 2, (
        "the copy and the reopened original must both finish the cut turn")


def test_the_hand_over_includes_it():
    kw = server._hub_launch_kwargs(parse_args(["--resume-interrupted-turn"]), None)

    assert kw["resume_interrupted_turn"] is True
    assert server._hub_launch_kwargs(parse_args([]), None)["resume_interrupted_turn"] is False


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
        parse_args(["--cwd", "C:\\w", "--resume-interrupted-turn"]), None))

    assert seen["resume_interrupted_turn"] is True


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


def test_the_hub_opens_the_session_as_the_launch_asked(monkeypatch, tmp_path):
    assert _launch_new(monkeypatch, tmp_path,
                       {"resume_interrupted_turn": True})["resume_interrupted_turn"] is True


@pytest.mark.parametrize("value", [None, False, "yes", 1, "true"])
def test_anything_but_true_leaves_it_waiting(monkeypatch, tmp_path, value):
    """An older launcher sends nothing; nothing but a JSON true turns it on."""
    body = {} if value is None else {"resume_interrupted_turn": value}

    assert _launch_new(monkeypatch, tmp_path, body)["resume_interrupted_turn"] is False
