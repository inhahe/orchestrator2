"""CLI argument parsing, constants, and runtime configuration.

Single source of truth for all startup flags and compile-time defaults.
The Config dataclass is read-only after parse_args() returns — runtime
mutations (from slash commands like /effort, /model) flow through State
in state.py.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EFFORT_LEVELS = ("low", "medium", "high", "max")
# "auto" means "don't pass effort — let the model pick its default"
# (typically 'high' for Opus/Sonnet 4.6).
EFFORT_CHOICES = ("auto",) + EFFORT_LEVELS

CONTINUE_PROMPT = (
    'If you need input from me before continuing, pause and include the '
    'literal token "[WAITING]" in your reply. If you are finished with '
    'all your tasks, include the literal token "[DONE]" instead. '
    'Otherwise, continue working.'
)
WAITING_SENTINEL = "[WAITING]"
DONE_SENTINEL = "[DONE]"

DEFAULT_COMPACT_THRESHOLD = 160_000
DEFAULT_COMPACT_THRESHOLD_1M = 950_000

CONTINUE_RESPONSE_DELAY_SECONDS = 2.0
CONTINUE_BURST_LIMIT = 3
CONTINUE_BURST_WINDOW_SECONDS = 180.0

# Tools whose output is typically short and can be shown inline without
# drowning the user's scrollback.
SHORT_OUTPUT_TOOLS = frozenset({
    "Edit", "Write", "NotebookEdit", "TodoWrite", "KillShell",
})

# All recognised slash commands — used for frontend tab-completion.
SLASH_COMMANDS = [
    "/help", "/status", "/debug", "/cost", "/cwd", "/clear", "/cls",
    "/interrupt", "/i", "/compact", "/effort", "/thinking", "/model",
    "/connect", "/reconnect", "/rename", "/export",
    "/tools", "/tasks", "/bg", "/background", "/show", "/btw",
    "/autocompact", "/max-context", "/bell",
    "/queue", "/todos", "/plan", "/quit", "/exit",
    "/quit!", "/exit!",
]

# Terminal bell event system.
BELL_EVENT_NAMES = frozenset({
    "turn-done", "waiting", "done", "stalled",
    "api-stall", "api-ok", "interrupt", "bg-done", "requires-action",
    "rate-hit", "rate-reset",
})
# These events are deferred while bg tasks are still running — the bell
# fires from bg-completion when the last task finishes.
BELL_DEFER_WHEN_BG_RUNNING = frozenset({
    "turn-done", "waiting", "done", "stalled",
})

DEFAULT_BELL_EVENTS = "waiting,done,stalled,api-stall,requires-action,rate-hit,rate-reset"

# Subscription rate-limit bucket types.
SUBSCRIPTION_RL_TYPES = frozenset(
    {"five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"}
)

RL_TYPE_LABEL = {
    "five_hour": "5h",
    "seven_day": "7d",
    "seven_day_opus": "7d opus",
    "seven_day_sonnet": "7d sonnet",
}

# Status markers — (unicode, ascii) pairs.
MARKERS: dict[str, tuple[str, str]] = {
    "start":        ("▶", ">"),
    "completed":    ("✓", "v"),
    "failed":       ("✗", "x"),
    "stopped":      ("◼", "!"),
    "cancelled":    ("◼", "!"),
    "check":        ("✓", "v"),
    "arrow_result": ("→", ">"),
    "arrow_cur":    ("→", ">"),
    "bullet":       ("·", "."),
    "unknown":      ("•", "*"),
}

# Sentinel for the --resume interactive picker.
_PICKER_SENTINEL = "<picker>"

# Sentinel pushed on turn_msg_queue when the dispatcher crashes.
DISPATCHER_DEAD = object()

# Default server port.
DEFAULT_PORT = 8420

# ---------------------------------------------------------------------------
# Model / context-window helpers
# ---------------------------------------------------------------------------

def model_context_window(model: str | None) -> int | None:
    """Best-effort context-window size (tokens) for the given model id.

    Returns None when the window can't be determined (unknown model).
    Explicit 1M-context variants → 1M.  Opus 4+ → 1M.  Other known
    Claude models → 200k.  Unknown → None.
    """
    if not model:
        return None
    m = model.lower()
    if "[1m]" in m or "-1m" in m or m.endswith("1m"):
        return 1_000_000
    if "opus" in m and ("4" in m or "5" in m or "6" in m):
        return 1_000_000
    if "claude" in m or "sonnet" in m or "haiku" in m:
        return 200_000
    return None


def default_compact_at(model: str | None) -> int:
    """Pick a sensible auto-compact trigger based on the selected model."""
    window = model_context_window(model)
    if window is not None and window >= 1_000_000:
        return DEFAULT_COMPACT_THRESHOLD_1M
    return DEFAULT_COMPACT_THRESHOLD


# ---------------------------------------------------------------------------
# Bell-event parsing
# ---------------------------------------------------------------------------

def _parse_bell_spec(spec: str) -> str | dict[str, bool]:
    """Parse a bell-event specification string.

    Returns ``"all"`` or ``"none"`` for those shortcuts, otherwise a dict
    mapping event names to True (enabled) / False (disabled).
    """
    spec = spec.strip().lower()
    if spec in ("all", "none"):
        return spec
    result: dict[str, bool] = {}
    for token in spec.replace(",", " ").split():
        token = token.strip()
        if not token:
            continue
        if token.endswith(" off") or token.endswith("-off"):
            name = token.rsplit(" ", 1)[0] if " " in token else token[:-4]
            name = name.rstrip("-")
        elif token.endswith(" on") or token.endswith("-on"):
            name = token.rsplit(" ", 1)[0] if " " in token else token[:-3]
            name = name.rstrip("-")
            result[name] = True
            continue
        else:
            name = token
            result[name] = True
            continue
        # Reached only for -off / off suffix.
        result[name] = False
    return result


def parse_bell_events(spec: str) -> set[str]:
    """Full-replacement parse (for --bell-on startup flag).

    Returns the final set of enabled events.
    """
    result = _parse_bell_spec(spec)
    if result == "all":
        return set(BELL_EVENT_NAMES)
    if result == "none":
        return set()
    assert isinstance(result, dict)
    return {k for k, v in result.items() if v}


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    """Immutable configuration parsed from CLI arguments."""

    # Session
    initial_prompt: str | None = None
    no_continue: bool = False
    resume: str | None = None          # None, _PICKER_SENTINEL, or session UUID/name
    no_replay: bool = False
    cwd: str = "."

    # Model & effort
    model: str | None = None
    effort: str | None = None          # None means "auto"
    no_thinking: bool = False

    # Tools & permissions
    permission_mode: str = "bypassPermissions"
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)

    # Context / compaction
    compact_at: int = DEFAULT_COMPACT_THRESHOLD
    no_compact: bool = True            # True = orchestrator auto-compact disabled
    compact_cooldown_turns: int = 3
    max_context_tokens: int = 0        # 0 = disabled
    auto_compact: bool = False

    # Auto-continue
    auto_continue: bool = False
    continue_prompt: str | None = None
    continue_response_delay: float = CONTINUE_RESPONSE_DELAY_SECONDS
    continue_burst_limit: int = CONTINUE_BURST_LIMIT
    continue_burst_window: float = CONTINUE_BURST_WINDOW_SECONDS

    # API stall
    api_stall_limit: int = 5
    api_stall_window: float = 60.0
    status_url: str = "https://status.claude.com/api/v2/summary.json"
    status_poll_interval: float = 30.0
    no_status_poll: bool = False

    # Display
    show_thinking: bool = False
    show_full_commands: bool = False
    show_tool_output: bool = False
    show_tool_everything: bool = False
    inline_all_tools: bool = False
    show_tasks: str = "compact"        # "off"|"compact"|"full"|"full+output"
    show_edits: str = "compact"        # "off"|"compact"|"full"
    ascii_only: bool = False
    collapse_tools: bool = True        # auto-collapse long tool runs

    # Panels
    tasks_panel: bool = False
    bg_panel: bool = True
    todos_panel: bool = False
    panel_delay: float = 0.0
    panel_grace: float = 10.0

    # Bell
    bell_on: str = DEFAULT_BELL_EVENTS

    # Misc
    append_system_prompt: str | None = None
    mcp_config: str | None = None
    auto_reconnect: bool = False
    debug: bool = False

    # Server
    port: int = DEFAULT_PORT
    open_browser: bool = False


def config_to_dict(config: Config) -> dict:
    """Serializable snapshot for sending to the frontend as initial state."""
    import dataclasses
    d = dataclasses.asdict(config)
    # Convert non-serializable defaults.
    d["allowed_tools"] = list(config.allowed_tools)
    d["disallowed_tools"] = list(config.disallowed_tools)
    return d


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> Config:
    """Parse CLI flags and return an immutable Config."""

    ap = argparse.ArgumentParser(
        description="GUI-based Claude orchestrator with a web interface."
    )

    # -- Session --
    ap.add_argument(
        "--initial-prompt", "-p",
        default=None,
        help="First message to send.",
    )
    ap.add_argument(
        "--no-continue",
        action="store_true",
        help="Start a fresh session instead of resuming the most recent one in cwd.",
    )
    ap.add_argument(
        "--no-replay",
        action="store_true",
        help=(
            "When resuming, do NOT replay prior user/assistant messages into "
            "the backscroll."
        ),
    )
    ap.add_argument(
        "--resume",
        nargs="?",
        const=_PICKER_SENTINEL,
        default=None,
        metavar="SESSION_ID",
        help=(
            "Resume a specific session by id. Pass --resume with no value to "
            "open an interactive picker."
        ),
    )

    # -- Model & effort --
    ap.add_argument(
        "--effort",
        choices=list(EFFORT_CHOICES),
        default=None,
        help=(
            "Thinking effort level. 'auto' (or omit) means don't pass an "
            "effort parameter; the model uses its own default."
        ),
    )
    ap.add_argument(
        "--model",
        default=None,
        help='e.g. "claude-opus-4-6", "claude-sonnet-4-6".',
    )
    ap.add_argument(
        "--cwd",
        default=".",
        help="Working directory Claude operates in.",
    )
    ap.add_argument(
        "--no-thinking",
        action="store_true",
        help="Disable extended thinking entirely at the API level.",
    )

    # -- Context / compaction --
    ap.add_argument(
        "--compact-at",
        type=int,
        default=None,
        help=(
            f"Force /compact when context tokens exceed this. Default derived "
            f"from model: {DEFAULT_COMPACT_THRESHOLD_1M} for 1M-context, "
            f"{DEFAULT_COMPACT_THRESHOLD} otherwise."
        ),
    )
    ap.add_argument(
        "--auto-compact",
        dest="no_compact",
        action="store_false",
        help="Enable the orchestrator's auto-compact check (off by default).",
    )
    ap.add_argument(
        "--no-compact",
        dest="no_compact",
        action="store_true",
        default=True,
        help="(Default.) Disable the orchestrator's auto-compact entirely.",
    )
    ap.add_argument(
        "--compact-cooldown-turns",
        type=int,
        default=3,
        help="After auto-compact, skip compact check for this many turns. Default 3.",
    )
    ap.add_argument(
        "--max-context-tokens",
        type=int,
        default=0,
        help=(
            "Cap context window at ~N tokens via rolling-window trim. "
            "0 = disabled."
        ),
    )

    # -- Auto-continue --
    ap.add_argument(
        "--auto-continue",
        action="store_true",
        help="Enable autonomous continue after each turn.",
    )
    ap.add_argument(
        "--continue-prompt",
        default=None,
        metavar="TEXT",
        help="Override the auto-continue prompt text.",
    )
    ap.add_argument(
        "--continue-response-delay",
        type=float,
        default=CONTINUE_RESPONSE_DELAY_SECONDS,
        help=f"Seconds to wait before auto-continuing. Default {CONTINUE_RESPONSE_DELAY_SECONDS}.",
    )
    ap.add_argument(
        "--continue-burst-limit",
        type=int,
        default=CONTINUE_BURST_LIMIT,
        metavar="N",
        help=f"Safety brake: treat as [WAITING] after N fast turns. Default {CONTINUE_BURST_LIMIT}.",
    )
    ap.add_argument(
        "--continue-burst-window",
        type=float,
        default=CONTINUE_BURST_WINDOW_SECONDS,
        metavar="SECONDS",
        help=f"Time window for burst-limit. Default {CONTINUE_BURST_WINDOW_SECONDS}.",
    )

    # -- Tools & permissions --
    ap.add_argument(
        "--permission-mode",
        choices=["bypassPermissions", "acceptEdits", "default", "plan"],
        default="bypassPermissions",
        help="Tool permission mode. Default: bypassPermissions.",
    )
    ap.add_argument(
        "--allowed-tool",
        action="append",
        default=[],
        metavar="NAME",
        help="Restrict to these tools (repeatable). Omit to allow all.",
    )
    ap.add_argument(
        "--disallowed-tool",
        action="append",
        default=[],
        metavar="NAME",
        help="Block specific tools (repeatable).",
    )

    # -- MCP & system prompt --
    ap.add_argument(
        "--append-system-prompt",
        default=None,
        metavar="TEXT",
        help="Extra instructions appended to the system prompt.",
    )
    ap.add_argument(
        "--mcp-config",
        default=None,
        metavar="PATH",
        help="Path to an MCP servers JSON file.",
    )

    # -- Display --
    ap.add_argument(
        "--show-thinking",
        action="store_true",
        help="Print full thinking blocks (default: collapsed snippet).",
    )
    ap.add_argument(
        "--show-full-commands",
        action="store_true",
        help="Display Bash command body inline.",
    )
    ap.add_argument(
        "--show-tool-output",
        action="store_true",
        help="Print full tool result content inline.",
    )
    ap.add_argument(
        "--show-tool-everything",
        action="store_true",
        help="Convenience: --show-full-commands AND --show-tool-output.",
    )
    ap.add_argument(
        "--inline-all-tools",
        action="store_true",
        help="Render every tool inline with [#N] tags.",
    )
    ap.add_argument(
        "--show-tasks",
        choices=("off", "compact", "full", "full+output"),
        default="compact",
        help="Non-Bash tool activity display. Default: compact.",
    )
    ap.add_argument(
        "--show-edits",
        choices=("off", "compact", "full"),
        default="compact",
        help="Edit tool call display. Default: compact.",
    )
    ap.add_argument(
        "--ascii-only",
        action="store_true",
        help="Use ASCII markers instead of Unicode.",
    )

    ap.add_argument(
        "--collapse-tools",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Auto-collapse long runs of consecutive tool calls.",
    )

    # -- Panels --
    ap.add_argument(
        "--tasks-panel",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Show live tasks panel in toolbar.",
    )
    ap.add_argument(
        "--bg-panel",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Show background-tasks panel. Default: on.",
    )
    ap.add_argument(
        "--todos-panel",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Show TodoWrite plan panel.",
    )
    ap.add_argument(
        "--panel-delay",
        type=float,
        default=0.0,
        metavar="SECS",
        help="Seconds before a tool appears in panels. Default: 0.",
    )
    ap.add_argument(
        "--panel-grace",
        type=float,
        default=10.0,
        metavar="SECS",
        help="Minimum seconds a completed task stays in panel. Default: 10.",
    )

    # -- Bell --
    ap.add_argument(
        "--bell-on",
        default=DEFAULT_BELL_EVENTS,
        metavar="EVENTS",
        help=(
            "Comma-separated bell events. "
            f"Valid: {', '.join(sorted(BELL_EVENT_NAMES))}. "
            "Shortcuts: all, none."
        ),
    )

    # -- API stall --
    ap.add_argument(
        "--api-stall-limit",
        type=int,
        default=5,
        help="Enter API-stall after N retries within window. Default: 5. 0 = disable.",
    )
    ap.add_argument(
        "--api-stall-window",
        type=float,
        default=60.0,
        help="Sliding window (seconds) for stall-limit. Default: 60.",
    )
    ap.add_argument(
        "--status-url",
        default="https://status.claude.com/api/v2/summary.json",
        help="Anthropic Statuspage.io summary feed.",
    )
    ap.add_argument(
        "--status-poll-interval",
        type=float,
        default=30.0,
        help="Status feed poll interval (seconds). Default: 30.",
    )
    ap.add_argument(
        "--no-status-poll",
        action="store_true",
        help="Don't poll the status page when API-stalled.",
    )

    # -- Resilience --
    ap.add_argument(
        "--auto-reconnect",
        action="store_true",
        help="Reconnect and auto-continue on CLI crash.",
    )

    # -- Debug --
    ap.add_argument(
        "--debug",
        action="store_true",
        help="Print extra diagnostic messages.",
    )

    # -- Server --
    ap.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"HTTP server port. Default: {DEFAULT_PORT}.",
    )
    ap.add_argument(
        "--open",
        action="store_true",
        help="Open the browser automatically after starting.",
    )

    args = ap.parse_args(argv)

    # --show-tool-everything is a convenience that flips both detail flags.
    if args.show_tool_everything:
        args.show_full_commands = True
        args.show_tool_output = True

    # Fill compact-at from the model when the user didn't specify.
    compact_at = args.compact_at
    if compact_at is None:
        compact_at = default_compact_at(args.model)

    return Config(
        initial_prompt=args.initial_prompt,
        no_continue=args.no_continue,
        resume=args.resume,
        no_replay=args.no_replay,
        cwd=args.cwd,
        model=args.model,
        effort=args.effort,
        no_thinking=args.no_thinking,
        permission_mode=args.permission_mode,
        allowed_tools=list(args.allowed_tool),
        disallowed_tools=list(args.disallowed_tool),
        compact_at=compact_at,
        no_compact=args.no_compact,
        auto_compact=not args.no_compact,
        compact_cooldown_turns=args.compact_cooldown_turns,
        max_context_tokens=args.max_context_tokens,
        auto_continue=args.auto_continue,
        continue_prompt=args.continue_prompt,
        continue_response_delay=args.continue_response_delay,
        continue_burst_limit=args.continue_burst_limit,
        continue_burst_window=args.continue_burst_window,
        api_stall_limit=args.api_stall_limit,
        api_stall_window=args.api_stall_window,
        status_url=args.status_url,
        status_poll_interval=args.status_poll_interval,
        no_status_poll=args.no_status_poll,
        show_thinking=args.show_thinking,
        show_full_commands=args.show_full_commands,
        show_tool_output=args.show_tool_output,
        show_tool_everything=args.show_tool_everything,
        inline_all_tools=args.inline_all_tools,
        show_tasks=args.show_tasks,
        show_edits=args.show_edits,
        ascii_only=args.ascii_only,
        collapse_tools=args.collapse_tools,
        tasks_panel=args.tasks_panel,
        bg_panel=args.bg_panel,
        todos_panel=args.todos_panel,
        panel_delay=args.panel_delay,
        panel_grace=args.panel_grace,
        bell_on=args.bell_on,
        append_system_prompt=args.append_system_prompt,
        mcp_config=args.mcp_config,
        auto_reconnect=args.auto_reconnect,
        debug=args.debug,
        port=args.port,
        open_browser=args.open,
    )
