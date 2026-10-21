"""Suite-wide guards.

**No test touches the real agent registry.**  ``agent_comms.db_path()`` is
machine-wide (``%LOCALAPPDATA%/orchestrator2/agents.db``) and shared by every
hub on the machine, and a bridge reaches it from ``connect()`` (the remembered
session name) and from ``register_agent``.  Tests that only meant to exercise
a connect were creating tables in it -- found 2026-09-22 -- and two identities
registered by one process seven seconds apart, never heard from again, are
what made every session opened in this directory refuse an identity.  Tests
that want a registry get their own via ``ORCH2_AGENT_DB``; this makes that the
default rather than something each file has to remember.

**No test inherits a name or a compaction from the developer's shell**
(``ORCH2_AGENT_NAME``, ``CLAUDE_CODE_SESSION_NAME``,
``config.CLI_COMPACTION_VARS``): ``parse_args`` consumes the first, and a
session running this suite from its Bash tool carries the others.

**No test inherits another's account.**  ``account.adopt_from_environment``
makes the process's account module state and takes ``CLAUDE_CONFIG_DIR`` out
of the environment; each test starts un-adopted, with the variable as the
suite found it, so the environment is the account, as for a tool.

**No test writes the hub's files into a real account.**  That account is
whichever one the shell running the suite is on, and a test whose bridge is
told a session id saved that session's cost under its ``orchestrator2``
folder, where the hub would read it back as a real session's -- and a
wakeup filed there would be armed by the real hub, as a session waiting on a
usage limit would be reopened by it.  A test that names an account of its own
(a ``tmp_path``) still gets that one.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import account  # noqa: E402
import session  # noqa: E402
from config import CLI_COMPACTION_VARS  # noqa: E402
import limit_resume  # noqa: E402
import wakeup_store  # noqa: E402


@pytest.fixture(autouse=True)
def _private_agent_registry(monkeypatch, tmp_path_factory):
    monkeypatch.setenv(
        "ORCH2_AGENT_DB",
        str(tmp_path_factory.mktemp("agent-registry") / "agents.db"))
    monkeypatch.delenv("ORCH2_AGENT_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_NAME", raising=False)
    for var in CLI_COMPACTION_VARS:
        monkeypatch.delenv(var, raising=False)


_SUITE_ACCOUNT = os.environ.get("CLAUDE_CONFIG_DIR")


@pytest.fixture(autouse=True)
def _unadopted_account(monkeypatch):
    monkeypatch.setattr(account, "_adopted", False)
    monkeypatch.setattr(account, "_hub", None)
    # Registered with monkeypatch either way, so it is put back after a test
    # whose main() popped it.
    if _SUITE_ACCOUNT is None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "placeholder")
        monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    else:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", _SUITE_ACCOUNT)


#: The user's home, read before any test patches ``Path.home``.
_HOME = Path.home().resolve()


def _is_real_account(path: Path) -> bool:
    """Whether *path* is one of this user's accounts (``~/.claude*``)."""
    try:
        path = path.resolve()
    except OSError:
        return False
    return path.parent == _HOME and path.name.startswith(".claude")


@pytest.fixture(autouse=True)
def _no_hub_state_in_a_real_account(monkeypatch, tmp_path_factory):
    spare: list[Path] = []

    def spare_dir() -> Path:
        if not spare:
            spare.append(tmp_path_factory.mktemp("hub-state"))
        return spare[0]

    # Queues, background tasks, costs and compaction.
    real_state = session._orch2_state_dir

    def state_dir(config_dir: str | None = None) -> Path:
        d = real_state(config_dir)
        return spare_dir() if _is_real_account(d.parent) else d

    monkeypatch.setattr(session, "_orch2_state_dir", state_dir)

    # Wakeups, which a real hub would arm again at its start.
    real_wakeups = wakeup_store._state_dir

    def wakeups_dir() -> Path:
        d = real_wakeups()
        return spare_dir() / "wakeups" if _is_real_account(d.parent.parent) else d

    monkeypatch.setattr(wakeup_store, "_state_dir", wakeups_dir)

    # Sessions waiting on a usage limit, which a real hub would reopen.
    real_limits = limit_resume._state_dir

    def limits_dir() -> Path:
        d = real_limits()
        return (spare_dir() / "limit-resumes"
                if _is_real_account(d.parent.parent) else d)

    monkeypatch.setattr(limit_resume, "_state_dir", limits_dir)


@pytest.fixture
def hub_answers(monkeypatch):
    """Make a launch's POST to a running hub get *answer*: call the fixture
    with the JSON object the hub would send back."""
    import json
    import urllib.request

    def answer_with(answer: dict) -> None:
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(answer).encode("utf-8")

        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda req, timeout=None: _Resp())

    return answer_with


@pytest.fixture
def hub():
    """A hub with one session, its default (``hubkit.one_session_hub``).
    Some test files define a ``hub`` of their own, which takes precedence."""
    import hubkit
    with hubkit.one_session_hub() as rt:
        yield rt
