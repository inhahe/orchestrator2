"""CLI argument parsing, constants, and runtime configuration.

Single source of truth for all startup flags and compile-time defaults.
The Config dataclass is read-only after parse_args() returns — runtime
mutations (from slash commands like /effort, /model) flow through State
in state.py.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

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
    "/help", "/history", "/status", "/debug", "/cost", "/cwd", "/clear", "/cls",
    "/interrupt", "/i", "/compact", "/effort", "/thinking", "/model",
    "/connect", "/reconnect", "/resume", "/rename", "/export", "/models",
    "/btw", "/graphify", "/autocompact", "/max-context", "/bell",
    "/collapse", "/collapse-threshold",
    "/queue", "/quit", "/exit",
    "/quit!", "/exit!",
]

# Known models — (id, description).  Used in --help and /model.
KNOWN_MODELS = [
    ("claude-opus-4-6", "Opus 4.6 — 1M context, max capability"),
    ("claude-sonnet-4-6", "Sonnet 4.6 — 200k context, fast"),
    ("claude-haiku-3-5", "Haiku 3.5 — 200k context, fastest"),
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
    inline_all_tools: bool = True
    show_tasks: str = "compact"        # "off"|"compact"|"full"|"full+output"
    show_edits: str = "compact"        # "off"|"compact"|"full"
    ascii_only: bool = False
    collapse_tools: bool = True        # auto-collapse consecutive tool/thinking blocks
    collapse_threshold: int = 3        # tools shown before collapsing
    max_dom_messages: int = 2000       # 0 = never trim old messages from the DOM

    # Panels
    panel_delay: float = 0.0
    panel_grace: float = 10.0

    # Bell
    bell_on: str = DEFAULT_BELL_EVENTS

    # Misc
    append_system_prompt: str | None = None
    mcp_config: str | None = None
    auto_reconnect: bool = False
    debug: bool = False
    log_file: str | None = None         # path to a persistent log file

    # Server
    port: int = DEFAULT_PORT
    open_browser: bool = False
    detach: bool = False               # re-launch headless and exit terminal
    auto_shutdown: bool = False        # shut down when all browser tabs close
    config_dir: str | None = None      # CLAUDE_CONFIG_DIR override


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
    _model_names = ", ".join(m for m, _ in KNOWN_MODELS)
    ap.add_argument(
        "--model",
        default=None,
        help=f"Model to use. Available: {_model_names}.",
    )
    ap.add_argument(
        "--list-models",
        action="store_true",
        help="List available models and exit.",
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
        help="Auto-collapse consecutive tool/thinking blocks.",
    )
    ap.add_argument(
        "--collapse-threshold",
        type=int, default=3, metavar="N",
        help="Number of blocks to show before collapsing (default: 3).",
    )
    ap.add_argument(
        "--max-dom-messages",
        type=int, default=2000, metavar="N",
        help="Max DOM elements in the chat backscroll before trimming old ones. 0 = never trim. Default: 2000.",
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

    # -- Resilience --
    ap.add_argument(
        "--auto-reconnect",
        action="store_true",
        help="Reconnect and auto-continue on CLI crash.",
    )

    # -- Config directory --
    ap.add_argument(
        "--config-dir",
        default=None,
        metavar="PATH",
        help=(
            "Override CLAUDE_CONFIG_DIR (where sessions and credentials are stored). "
            "Use this to run an instance under a different Claude account."
        ),
    )

    # -- Debug --
    ap.add_argument(
        "--debug",
        action="store_true",
        help="Print extra diagnostic messages.",
    )
    ap.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help="Write all log output to PATH (appends). Useful for diagnosing crashes when the terminal closes.",
    )

    # -- Server --
    ap.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"HTTP server port. Default: {DEFAULT_PORT}. Auto-selects a free port if in use.",
    )
    ap.add_argument(
        "--open",
        action="store_true",
        help="Open the browser automatically after starting.",
    )
    ap.add_argument(
        "--detach",
        action="store_true",
        help=(
            "Launch the server in the background and exit the terminal. "
            "Implies --open."
        ),
    )
    ap.add_argument(
        "--auto-shutdown",
        action="store_true",
        help="Shut down when all browser tabs close (auto-set by --detach).",
    )

    args = ap.parse_args(argv)

    # --list-models: print available models and exit.
    if args.list_models:
        print("Available models:")
        for model_id, desc in KNOWN_MODELS:
            print(f"  {model_id:25s} {desc}")
        raise SystemExit(0)

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
        cwd=str(Path(args.cwd).resolve()),
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
        api_stall_limit=args.api_stall_limit,
        api_stall_window=args.api_stall_window,
        status_url=args.status_url,
        show_thinking=args.show_thinking,
        show_full_commands=args.show_full_commands,
        show_tool_output=args.show_tool_output,
        show_tool_everything=args.show_tool_everything,
        show_tasks=args.show_tasks,
        show_edits=args.show_edits,
        ascii_only=args.ascii_only,
        collapse_tools=args.collapse_tools,
        collapse_threshold=max(1, args.collapse_threshold),
        max_dom_messages=max(0, args.max_dom_messages),
        bell_on=args.bell_on,
        append_system_prompt=args.append_system_prompt,
        mcp_config=args.mcp_config,
        auto_reconnect=args.auto_reconnect,
        debug=args.debug,
        log_file=args.log_file,
        port=args.port,
        open_browser=args.open or args.detach,
        detach=args.detach,
        auto_shutdown=args.auto_shutdown or args.open or args.detach,
        config_dir=args.config_dir,
    )
