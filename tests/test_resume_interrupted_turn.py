"""Finishing a turn the CLI reports as interrupted.

Reported 2026-09-06: three restarted SlateOS sessions each showed a prompt the
user never typed -- *"Continue from where you left off."* -- answered by
*"No response requested."*, and the interrupted work was not picked back up.

It is not orchestrator2's text and never was (``grep`` finds it nowhere in this
repo). It comes from the **CLI's own** conversation recovery
(``src/utils/conversationRecovery.ts`` in the Claude Code source): when a
resumed session's last turn was cut off mid-flight, it appends a *synthetic*
user message with ``isMeta: true``, plus an assistant sentinel so the
transcript stays API-valid if nothing acts on it. The transcript records prove
the provenance -- ``entrypoint: "sdk-py"``, ``isMeta: true``, bracketed by
``queue-operation`` entries.

In an interactive terminal the user is offered the choice, and accepting makes
the CLI delete that pair and re-enqueue it as a real prompt. Non-interactively
there is no chooser, so the pair just *stays*: a phantom prompt, a refusal to
answer it, and abandoned work. ``CLAUDE_CODE_RESUME_INTERRUPTED_TURN`` is the
supported opt-in for exactly this path (``src/cli/print.ts``), so orchestrator2
sets it: of the three possible behaviours -- finish the turn, or leave the
noise and abandon the work -- only one has any upside.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import parse_args                        # noqa: E402
from sdk_bridge import SDKBridge                      # noqa: E402
from state import init_state_from_config              # noqa: E402

ENV_VAR = "CLAUDE_CODE_RESUME_INTERRUPTED_TURN"


def _env(argv=()):
    """The env orchestrator2 hands the CLI subprocess."""
    cfg = parse_args(list(argv))
    state = init_state_from_config(cfg)

    async def bcast(_msg):
        pass

    br = SDKBridge(config=cfg, state=state, broadcaster=bcast)
    opts = br._make_options()
    env = getattr(opts, "env", None)
    if env is None and isinstance(opts, dict):
        env = opts.get("env")
    return env or {}


# ---------------------------------------------------------------------------
# The fix
# ---------------------------------------------------------------------------

def test_the_interrupted_turn_is_resumed_by_default():
    """Without this the CLI leaves a prompt the user never typed, answers it
    with 'No response requested.', and drops the work on the floor."""
    assert _env().get(ENV_VAR) == "1"


def test_it_can_be_turned_off():
    """Opening an interrupted session without it immediately acting is a
    legitimate thing to want."""
    assert ENV_VAR not in _env(["--no-resume-interrupted-turn"])


def test_the_flag_defaults_on():
    assert parse_args([]).resume_interrupted_turn is True
    assert parse_args(["--no-resume-interrupted-turn"]).resume_interrupted_turn is False


# ---------------------------------------------------------------------------
# ...without disturbing the rest of the environment
# ---------------------------------------------------------------------------

def test_the_other_env_vars_survive():
    """``env`` is merged onto the inherited environment by the SDK, so adding
    one var must not displace the others."""
    env = _env()
    assert env.get("CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS") == "1"


def test_prompt_cache_workaround_still_composes():
    env = _env(["--disable-prompt-cache"])
    assert env.get("DISABLE_PROMPT_CACHING") == "1"
    assert env.get(ENV_VAR) == "1"


def test_a_cross_account_session_still_gets_its_config_dir(tmp_path):
    env = _env(["--config-dir", str(tmp_path)])
    assert env.get("CLAUDE_CONFIG_DIR") == str(tmp_path)
    assert env.get(ENV_VAR) == "1"


@pytest.mark.parametrize("argv", [
    [],
    ["--no-resume-interrupted-turn"],
    ["--disable-prompt-cache"],
])
def test_the_env_is_always_a_flat_string_map(argv):
    """A non-string value here fails deep inside subprocess spawning, where the
    error names neither the variable nor this file."""
    for k, v in _env(argv).items():
        assert isinstance(k, str) and isinstance(v, str), (k, v)


def test_a_config_without_the_attribute_still_resumes():
    """``_make_options`` reads this with ``getattr(..., True)``, matching how it
    reads every other optional config attr, because ``SDKBridge`` is routinely
    constructed against hand-built config objects (tests, and older pickled or
    partially-populated Configs) that predate the field.

    A mutation sweep showed the fallback was unverified: ``parse_args`` always
    sets the attribute, so flipping the default to ``False`` changed nothing any
    test could see -- while silently disabling the feature for exactly those
    hand-built configs. Pin the fallback rather than delete it, since the
    absent-attribute case is real.
    """
    from types import SimpleNamespace

    cfg = parse_args([])
    bare = SimpleNamespace(**{k: v for k, v in vars(cfg).items()
                              if k != "resume_interrupted_turn"})
    assert not hasattr(bare, "resume_interrupted_turn")

    state = init_state_from_config(cfg)

    async def bcast(_msg):
        pass

    br = SDKBridge(config=bare, state=state, broadcaster=bcast)
    opts = br._make_options()
    env = getattr(opts, "env", None) or (opts.get("env") if isinstance(opts, dict) else {})
    assert env.get(ENV_VAR) == "1", (
        "a config lacking the field fell back to NOT resuming")
