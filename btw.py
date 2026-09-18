"""`/btw` — a side question asked in a *fork* of the live conversation.

Reported 2026-09-18: "I did a /btw and it didn't send it and show me the result
until after the turn ended. That seems to defeat the purpose of a /btw?" — and,
asked what it was meant to be: "I think /btw is supposed to create a new
conversation instance with the same context but copied so it doesn't interfere
with the original, that runs as the current turn is running."

It never did any of that. Both handlers turned it into an ordinary prompt in the
main conversation, and the implementation comment said as much: *"Full /btw
implementation would run in a separate context."* The README promised the
separate context anyway.

The mechanism turns out to be a first-class SDK feature rather than anything
clever::

    fork_session: bool = False
    \"\"\"When true, resumed sessions fork to a new session ID rather than
    continuing the previous one.\"\"\"

So ``resume=<current sid>`` + ``fork_session=True`` on a second client is the
whole thing: same context up to the fork point, a **new** session id, the
original transcript untouched, and genuinely concurrent with the live turn
because it is a separate CLI process. (An earlier plan here was to copy the
transcript with ``copy_session.py`` and call the result a workaround. That was
written without checking whether the SDK could fork natively, which it can.)

**The fork does not get to change anything.** Two agents in one working tree,
running at the same time, is a genuinely bad idea: the main turn may be
mid-edit on the very files a side question would touch, and neither would know.
So the fork runs with the mutating tools disallowed — see
:data:`BTW_DISALLOWED_TOOLS`. ``Bash`` is in that list despite being useful
read-only (``git log``, ``ls``) because it is the one tool that can write
anything at all, and "the aside quietly rebuilt something while the real turn
was working" is not a failure anyone would enjoy debugging.
"""

from __future__ import annotations

from typing import Any

# Tools the fork may not use.  Everything that can modify the working tree, the
# session, or spawn something that can.  Read/Grep/Glob stay: answering "which
# file did you mean?" is the point of the feature.
BTW_DISALLOWED_TOOLS = [
    "Bash",
    "Write",
    "Edit",
    "NotebookEdit",
    "Agent",
    "Task",
    "KillShell",
    "TaskStop",
]

# Prepended to the question so the fork knows what it is and does not try to
# resume the work it can see in its own history.  Without it, a fork of a
# session mid-task reads its transcript, concludes there is a job in progress,
# and starts doing it -- in a context whose edits are disallowed, so it would
# fail confusingly rather than answer.
BTW_PREAMBLE = (
    "[orchestrator2 /btw] This is a SIDE QUESTION, asked in a throwaway fork of "
    "the conversation above. The real session is still running and is doing the "
    "actual work; nothing you do here affects it, and this fork is discarded "
    "once you answer.\n\n"
    "Do NOT continue, resume, redo or 'help with' the task in the history. Do "
    "not edit anything (the tools for it are disabled). Just answer the "
    "question below, using the conversation above as context, and keep it "
    "brief.\n\n"
    "Question: "
)


def build_btw_options(options_cls: Any, *, config: Any, state: Any,
                      stderr_cb: Any = None) -> Any:
    """Build ``ClaudeAgentOptions`` for the forked side conversation.

    Deliberately **not** routed through ``SDKBridge._make_options``: that method
    has side effects on session state (``expected_resume_sid``,
    ``_unprompted_resume_pending``) which exist to describe the *main*
    connection, and a fork quietly rewriting them would make the next real
    reconnect misreport itself.

    What it does inherit is everything that decides *which account and which
    conversation* this is: the cwd, the config dir, and the model. Getting the
    config dir wrong would fork someone else's session, or nothing at all.
    """
    kwargs: dict[str, Any] = {
        "cwd": config.cwd,
        "setting_sources": ["user", "project", "local"],
        "max_buffer_size": 10 * 1024 * 1024,
        "resume": state.session_id,
        "fork_session": True,
        "disallowed_tools": list(BTW_DISALLOWED_TOOLS),
        # The fork must never stop to ask a human: nobody is watching it, and a
        # prompt it cannot deliver would hang the aside forever.  With the
        # mutating tools already disallowed there is nothing dangerous left to
        # approve.
        "permission_mode": "bypassPermissions",
        "env": {},
    }
    if stderr_cb is not None:
        kwargs["stderr"] = stderr_cb
    if getattr(config, "config_dir", None):
        kwargs["env"]["CLAUDE_CONFIG_DIR"] = config.config_dir
    if getattr(config, "disable_prompt_cache", False):
        kwargs["env"]["DISABLE_PROMPT_CACHING"] = "1"
    # A fork inherits the interrupted-turn recovery flag from nothing -- and
    # must not have it.  Resuming into "finish what you were doing" is exactly
    # what a side question must not do.
    kwargs["env"]["CLAUDE_CODE_RESUME_INTERRUPTED_TURN"] = "0"
    if getattr(state, "model", None):
        kwargs["model"] = state.model
    return options_cls(**kwargs)


def btw_prompt(question: str) -> str:
    """The text actually sent to the fork."""
    return BTW_PREAMBLE + question.strip()


def can_fork(state: Any) -> bool:
    """Whether there is a conversation to fork.

    A session with no id has no transcript yet, so there is nothing to copy and
    nothing the aside could know that the main session does not.  The caller
    falls back to the old behaviour (an ordinary queued prompt) rather than
    failing, because "you cannot ask that yet" would be a worse answer than
    asking it in the main context.
    """
    return bool(getattr(state, "session_id", None))
