"""The one agent-facing tool the comms addendum asks for: raise / lift a halt.

``specs/agent-comms-addendum-surfacing.md`` §2 draws the line precisely:

    | action            | surface                         |
    |-------------------|---------------------------------|
    | raise / lift halt | agent-facing tool               |
    | check a halt      | CLI with an exit-code contract  |

and §1 is equally firm about what *not* to build: messaging needs no new tool,
because ``ListAgents``/``SendMessage`` already advertise "other local Claude
sessions on this machine".  Widening the registry scope (see
``agent_comms.mirror_peer_registries``) makes that shipped description true;
adding a second messaging tool would be the wrong fix, and needing to *document*
that agents can message each other would be evidence the scope fix was
incomplete.

Checking stays in ``tools/agents.py`` and must remain usable with no agent and
no client running -- a build script, a CI step or a human at a prompt should all
be able to ask.  If checking were only reachable through an agent, every script
that wanted to be well-behaved would have to boot one to ask permission, which
defeats the point.

**On the descriptions below.**  The addendum asks for the *occasion*, not just
the action: "an agent reads a description at the moment it is choosing an
action", and one that only says "raises a halt" gets read and not acted on.  So
each says what situation calls for it.
"""

from __future__ import annotations

import os
from typing import Any

import agent_comms as ac

RAISE_DESCRIPTION = (
    "Ask every other Claude session on this machine to stop taking new work, "
    "and make long-running operations refuse to start.\n\n"
    "Use this when you need every agent quiescent before doing something that "
    "cannot be done while others are writing: migrating or moving the "
    "repository, repairing a shared file, rewriting history, a drive or "
    "toolchain migration, anything with a shared lock. Prefer it to filing a "
    "request or asking the operator to relay a message -- those are read late "
    "or not at all, which is the exact failure this exists to prevent.\n\n"
    "It is deliberate and rare. It stays in force until you lift it explicitly "
    "(never on a timer, so it cannot expire in the middle of the maintenance "
    "it is protecting), so lift it as soon as the reason has passed. Other "
    "agents are told once, at their next turn boundary; scripts that call "
    "`agents.py check-halt` refuse to start while it is up."
)

LIFT_DESCRIPTION = (
    "Lift a halt you or the operator raised, letting other sessions resume "
    "and long-running operations start again.\n\n"
    "Use this the moment the reason for the halt has passed -- a halt is never "
    "lifted automatically, so one that is forgotten silently blocks every "
    "agent and every expensive job on the machine until someone notices."
)


def _scope_args(args: dict[str, Any], cwd: str) -> tuple[str, str]:
    scope = (args.get("scope") or "repo").strip().lower()
    if scope not in ("repo", "machine"):
        raise ValueError(f"scope must be 'repo' or 'machine', not {scope!r}")
    return scope, (ac.repo_key(cwd) if scope == "repo" else "")


def build_halt_tools(*, identity_getter, cwd_getter):
    """Return the SDK tool objects, or ``None`` when the SDK cannot host them.

    *identity_getter* / *cwd_getter* are callables rather than values because a
    bridge resolves its identity after this is built, and a session can change
    directory; capturing either by value would pin the tool to a stale answer.
    """
    try:
        from claude_agent_sdk import tool
    except Exception:
        return None

    @tool("RaiseHalt", RAISE_DESCRIPTION, {
        "reason": str,
        "scope": str,
    })
    async def raise_halt_tool(args: dict[str, Any]) -> dict[str, Any]:
        reason = (args.get("reason") or "").strip()
        if not reason:
            return {"content": [{"type": "text", "text":
                    "Refused: a halt needs a reason. Every other agent is "
                    "about to be told to stop, and 'no reason given' is not "
                    "something they can act on."}], "isError": True}
        cwd = cwd_getter() or os.getcwd()
        try:
            scope, value = _scope_args(args, cwd)
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Refused: {e}"}],
                    "isError": True}
        by = identity_getter() or "unknown-agent"
        hid = ac.raise_halt(reason, by=by, scope_kind=scope, scope_value=value)
        others = [a.identity for a in ac.live_agents(
            **({"repo": value} if scope == "repo" else {}))
            if a.identity != by]
        return {"content": [{"type": "text", "text": (
            f"Halt {hid} raised ({scope} scope) by {by}: {reason}\n"
            f"Agents in scope right now: "
            f"{', '.join(others) if others else '(none registered)'}\n"
            f"They are told once, at their next turn boundary. Any operation "
            f"that calls `agents.py check-halt` now refuses to start.\n"
            f"This does NOT expire — lift it with LiftHalt when the reason has "
            f"passed."
        )}]}

    @tool("LiftHalt", LIFT_DESCRIPTION, {"scope": str})
    async def lift_halt_tool(args: dict[str, Any]) -> dict[str, Any]:
        cwd = cwd_getter() or os.getcwd()
        try:
            scope, value = _scope_args(args, cwd)
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Refused: {e}"}],
                    "isError": True}
        n = ac.lift_halt(scope_kind=scope, scope_value=value)
        return {"content": [{"type": "text", "text": (
            f"{n} halt(s) lifted ({scope} scope)." if n else
            f"No halt was in force for {scope} scope."
        )}]}

    return [raise_halt_tool, lift_halt_tool]


def build_server(*, identity_getter, cwd_getter):
    """An in-process MCP server exposing the halt tools, or None."""
    tools = build_halt_tools(identity_getter=identity_getter,
                            cwd_getter=cwd_getter)
    if not tools:
        return None
    try:
        from claude_agent_sdk import create_sdk_mcp_server
        return create_sdk_mcp_server(name="agent-halt", version="1.0.0",
                                     tools=tools)
    except Exception:
        return None
