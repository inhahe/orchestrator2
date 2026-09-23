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

**No test inherits a name from the developer's shell** (``ORCH2_AGENT_NAME``,
``CLAUDE_CODE_SESSION_NAME``): ``parse_args`` consumes the first, and a
session running this suite from its Bash tool carries the second.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _private_agent_registry(monkeypatch, tmp_path_factory):
    monkeypatch.setenv(
        "ORCH2_AGENT_DB",
        str(tmp_path_factory.mktemp("agent-registry") / "agents.db"))
    monkeypatch.delenv("ORCH2_AGENT_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_NAME", raising=False)
