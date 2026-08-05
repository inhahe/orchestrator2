"""CLI argument parsing, constants, and runtime configuration.

Single source of truth for all startup flags and compile-time defaults.
The Config dataclass is read-only after parse_args() returns — runtime
mutations (from slash commands like /effort, /model) flow through State
in state.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

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

# ---------------------------------------------------------------------------
# ScheduleWakeup / autonomous-loop heartbeat
# ---------------------------------------------------------------------------
#
# The model drives self-paced autonomous loops by calling the ``ScheduleWakeup``
# tool: "resume in N seconds with this prompt".  In Claude Code's interactive
# REPL the harness implements that timer.  But orchestrator2 talks to the CLI in
# streaming mode, where the CLI never re-injects a scheduled prompt on its own —
# so ScheduleWakeup is a silent no-op and the autonomous loop stalls (most
# visibly while waiting on a background task, when little context is produced and
# even compaction-driven continuations don't fire).  We therefore honour
# ScheduleWakeup ourselves: intercept the tool call, arm a timer, and re-inject
# the prompt as a fresh turn when it fires — even while background tasks run.
WAKEUP_MIN_DELAY = 60.0
WAKEUP_MAX_DELAY = 3600.0
WAKEUP_DEFAULT_DELAY = 60.0

# Prompts the model passes verbatim to ScheduleWakeup for an autonomous loop with
# no user task.  The runtime is expected to "resolve" these sentinels back to the
# autonomous-loop instructions at fire time; since orchestrator2 owns the timer,
# we resolve them to a concrete continue instruction (the model already carries
# the full autonomous-loop policy from its CLAUDE.md).
WAKEUP_SENTINELS = ("<<autonomous-loop-dynamic>>", "<<autonomous-loop>>")
WAKEUP_RESOLVED_PROMPT = (
    "Autonomous-loop wakeup (scheduled by you). Resume work now: first check "
    "on any in-flight background task, then continue the next task per your "
    "project's CLAUDE.md / roadmap. If — and only if — you are genuinely "
    "blocked on a human decision and no other task is unblocked, stop and do "
    "NOT schedule another wakeup; otherwise keep the loop alive by calling "
    "ScheduleWakeup again at the end of this turn."
)

# Tools whose output is typically short and can be shown inline without
# drowning the user's scrollback.
SHORT_OUTPUT_TOOLS = frozenset({
    "Edit", "Write", "NotebookEdit", "TodoWrite", "KillShell",
})

# All recognised slash commands — used for frontend tab-completion.
SLASH_COMMANDS = [
    "/help", "/history", "/status", "/debug", "/cost", "/cwd", "/clear", "/cls",
    "/interrupt", "/i", "/compact", "/effort", "/thinking", "/model",
    "/login", "/logout",
    "/connect", "/reconnect", "/resume", "/rename", "/switch", "/export", "/models",
    "/btw", "/graphify", "/autocompact", "/max-context", "/bell", "/mcp",
    "/collapse", "/collapse-threshold",
    "/queue", "/quit", "/exit",
    "/quit!", "/exit!",
]

# Fallback model list — (id, description).  Used only when the live list
# from the Anthropic API can't be fetched (see fetch_available_models).
# This goes stale as new models ship, so the live list is preferred; the
# cache is refreshed periodically (see _model_cache / MODEL_CACHE_TTL) so a
# transient fetch failure no longer pins the process to this list.
KNOWN_MODELS = [
    ("claude-opus-5", "Opus 5 — 1M context, max capability"),
    ("claude-sonnet-5", "Sonnet 5 — 200k context"),
    ("claude-fable-5", "Fable 5 — 200k context"),
    ("claude-opus-4-8", "Opus 4.8 — 1M context"),
    ("claude-opus-4-7", "Opus 4.7 — 1M context"),
    ("claude-opus-4-6", "Opus 4.6 — 1M context"),
    ("claude-sonnet-4-6", "Sonnet 4.6 — 200k context, fast"),
    ("claude-haiku-4-5-20251001", "Haiku 4.5 — 200k context, fastest"),
]

# Terminal bell event system.
#
# Only events that are actually rung somewhere in the program are listed.
# (The auto-continue loop is not wired up in the web orchestrator, so its
# 'done'/'waiting'/'stalled' events never fire and were removed, along with
# 'api-ok'/'rate-reset' which never had a ring site.  'api-stall' was removed
# too: it only existed to halt that dead auto-continue loop.)
BELL_EVENT_NAMES = frozenset({
    "turn-done", "bg-done", "requires-action",
    "interrupt", "rate-hit",
})

DEFAULT_BELL_EVENTS = "turn-done,bg-done,requires-action,rate-hit"

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

# Sentinel pushed on turn_msg_queue by ``SDKBridge.interrupt()`` so the
# run_turn loop's ``await turn_msg_queue.get()`` wakes up immediately
# even when the SDK isn't streaming.  Without this, an interrupt can
# leave ``state.busy=True`` forever because ``client.interrupt()``
# alone doesn't guarantee a follow-up SDK message and the loop is
# blocked waiting on the queue.
INTERRUPT_SENTINEL = object()

# Default server port.
DEFAULT_PORT = 8420

# Password required for connections from outside the LAN when neither
# ``--external-password`` nor ``ORCH2_EXTERNAL_PASSWORD`` is set.  LAN/loopback
# clients never need it.  Pass ``--external-password ""`` to disable external
# access entirely.
DEFAULT_EXTERNAL_PASSWORD = "uncommon11"

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
# Live model list (Anthropic /v1/models) with hardcoded fallback
# ---------------------------------------------------------------------------

# Cache of the live model list.  /model runs on the event loop, so it must
# never block on a network call — it reads this cache (kept warm by the
# server's model-cache loop) via get_known_models().  None until first fetch.
_model_cache: list[tuple[str, str]] | None = None
# time.monotonic() of the last *successful* fetch (0.0 = never).
_model_cache_at: float = 0.0

# How long a fetched model list is considered fresh.  Re-fetching matters:
# a hub can stay up for days, and a model released in the meantime would
# otherwise never appear in /model without a restart.
MODEL_CACHE_TTL = 3600.0


def _read_oauth_token() -> str | None:
    """Read the Claude Code OAuth access token from credentials, if present."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    path = Path(base if base else Path.home() / ".claude") / ".credentials.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if isinstance(oauth, dict):
        tok = oauth.get("accessToken")
        if isinstance(tok, str) and tok:
            return tok
    return None


def _fmt_window(n: int) -> str:
    if n >= 1_000_000:
        return f"{n // 1_000_000}M"
    if n >= 1000:
        return f"{n // 1000}k"
    return str(n)


def fetch_available_models(timeout: float = 6.0) -> list[tuple[str, str]] | None:
    """Fetch the live model list from the Anthropic API (blocking).

    Returns a list of ``(model_id, description)`` tuples in the order the API
    returns them (newest first), or ``None`` when the list can't be fetched
    (no credentials, network error, unexpected response).  On success the
    result is cached for later non-blocking reads via ``get_known_models``.

    Auth uses ``ANTHROPIC_API_KEY`` when set, otherwise the Claude Code OAuth
    token (with the oauth beta header the subscription API expects).

    This blocks on the network, so off the event loop only (startup warm or
    the synchronous --list-models path).
    """
    global _model_cache, _model_cache_at
    headers = {"anthropic-version": "2023-06-01"}
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if api_key:
        headers["x-api-key"] = api_key
    else:
        token = _read_oauth_token()
        if not token:
            log.warning("model list: no credentials (no ANTHROPIC_API_KEY and "
                        "no OAuth token in %s) — using the fallback list",
                        os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")
            return None
        headers["Authorization"] = f"Bearer {token}"
        headers["anthropic-beta"] = "oauth-2025-04-20"
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/models?limit=100", headers=headers,
    )
    # Failures are logged, not silent: a silent failure here is invisible and
    # shows up much later as "a model I know exists is missing from /model".
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as exc:
        log.warning("model list: HTTP %s from /v1/models — using the fallback "
                    "list%s", exc.code,
                    " (token expired? re-login with /login)"
                    if exc.code in (401, 403) else "")
        return None
    except Exception as exc:
        log.warning("model list: fetch failed (%r) — using the fallback list", exc)
        return None
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        log.warning("model list: unexpected /v1/models response shape — "
                    "using the fallback list")
        return None
    out: list[tuple[str, str]] = []
    for m in data:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if not isinstance(mid, str) or not mid:
            continue
        name = m.get("display_name") or mid
        window = model_context_window(mid)
        desc = f"{name} — {_fmt_window(window)} context" if window else str(name)
        out.append((mid, desc))
    if not out:
        log.warning("model list: /v1/models returned no usable models — "
                    "using the fallback list")
        return None
    _model_cache = out
    _model_cache_at = time.monotonic()
    log.info("model list: cached %d models from /v1/models", len(out))
    return out


def model_cache_is_stale() -> bool:
    """True when the live model list has never loaded or has aged out."""
    if _model_cache is None:
        return True
    return (time.monotonic() - _model_cache_at) > MODEL_CACHE_TTL


def get_known_models() -> list[tuple[str, str]]:
    """Best available model list for display — non-blocking, event-loop safe.

    Returns the cached live list when present, otherwise the hardcoded
    ``KNOWN_MODELS`` fallback.
    """
    return _model_cache if _model_cache else KNOWN_MODELS


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
    """Parse the --bell / --bell-on startup spec into the enabled-event set.

    Mirrors the ``/bell`` slash-command semantics so the CLI flag behaves the
    same way as the runtime command:

    * ``all`` / ``*``            -> every known event
    * ``none`` / ``off``         -> no events (bell disabled)
    * bare names (``turn-done bg-done``) -> REPLACE: only those events
    * ``+``/``-`` prefixed tokens -> MODIFY the default set (``+`` adds,
      ``-`` removes).  A bare name mixed in with prefixed tokens is treated
      as an add.  This is what ``--bell +turn-done`` relies on.

    Unknown event names are silently ignored.
    """
    p = (spec or "").strip().lower()
    if not p:
        return {e for e in DEFAULT_BELL_EVENTS.split(",") if e in BELL_EVENT_NAMES}
    if p in ("all", "*"):
        return set(BELL_EVENT_NAMES)
    if p in ("none", "off", "disable"):
        return set()

    tokens = [t for t in p.replace(",", " ").split() if t]
    has_mod_prefix = any(t.startswith(("+", "-")) for t in tokens)

    if has_mod_prefix:
        # MODIFY mode: start from the defaults, then apply add/remove tokens.
        result = {e for e in DEFAULT_BELL_EVENTS.split(",") if e in BELL_EVENT_NAMES}
        for tok in tokens:
            if tok.startswith("-"):
                name, op = tok[1:], "remove"
            elif tok.startswith("+"):
                name, op = tok[1:], "add"
            else:
                name, op = tok, "add"
            if name not in BELL_EVENT_NAMES:
                continue
            if op == "add":
                result.add(name)
            else:
                result.discard(name)
        return result

    # REPLACE mode: only the explicitly-listed valid events stay on.
    return {tok for tok in tokens if tok in BELL_EVENT_NAMES}


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
    copy: bool = False                 # --copy: TUI to copy a session from another account, then resume it
    no_replay: bool = False
    cwd: str = "."
    # Workaround for a bundled-CLI prompt-cache bug: when the 1h-vs-5m
    # cache_control TTL flips mid-session (e.g. rate-limit overage toggles
    # eligibility) the request is rejected with "API Error: 400 ... a ttl='1h'
    # cache_control block must not come after a ttl='5m' cache_control block".
    # Setting DISABLE_PROMPT_CACHING in the CLI env stops it emitting any
    # cache_control blocks, sidestepping the ordering error (at the cost of
    # prompt-cache savings).  Opt-in.
    disable_prompt_cache: bool = False

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

    # ScheduleWakeup / autonomous-loop heartbeat.  When True (default), the
    # bridge honours the model's ScheduleWakeup tool calls by re-injecting the
    # scheduled prompt as a fresh turn after the requested delay — including
    # while background tasks are still running.
    wakeup_enabled: bool = True

    # API status polling
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
    session_idle_timeout: int = 300    # secs a viewer-less session lingers before teardown
    standalone: bool = False           # don't reuse a running hub; force a separate server
    external_password: str | None = None  # non-LAN password; None = unspecified (→ env var, else DEFAULT_EXTERNAL_PASSWORD); "" = block all external
    config_dir: str | None = None      # CLAUDE_CONFIG_DIR override
    skip_auto_login: bool = False      # internal: child skips the login check
    wait_port: bool = False            # internal: retry binding --port while an old instance releases it (restart)


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
        "--disable-prompt-cache",
        action="store_true",
        help=(
            "Disable Claude prompt caching in the CLI (sets DISABLE_PROMPT_CACHING). "
            "Workaround for the bundled CLI's 'ttl=1h cache_control must not come "
            "after ttl=5m' API 400 that can hit long resumed sessions. Costs cache "
            "savings; leave off unless you hit that error."
        ),
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
            "open an interactive picker (a full-screen TUI in the terminal, "
            "or the in-browser picker when not run from a terminal)."
        ),
    )
    ap.add_argument(
        "--copy",
        action="store_true",
        help=(
            "Open a full-screen TUI to copy a session from another Claude "
            "account (.claude directory) into the current one, then resume it. "
            "The destination is the account of the current CLAUDE_CONFIG_DIR."
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
        help="Model id to use (e.g. claude-opus-4-8). "
             "Run --list-models for the current list.",
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
        metavar="PATH_OR_JSON",
        help="MCP servers config: a path to a JSON file or an inline JSON "
        "string (lets the orchestrator act as an MCP client).",
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
        "--bell", "--bell-on",
        dest="bell_on",
        nargs="+",
        default=DEFAULT_BELL_EVENTS,
        metavar="EVENT",
        help=(
            "Bell events to ring on, as separate words or one comma-/space-"
            "separated string (e.g. '--bell turn-done bg-done' or "
            "'--bell \"turn-done,bg-done\"'). "
            f"Valid: {', '.join(sorted(BELL_EVENT_NAMES))}. "
            "Bare names REPLACE the set; '+event'/'-event' add to / remove "
            "from the defaults (e.g. '+turn-done'). "
            "Shortcuts: 'all' enables every event, 'none' disables the bell. "
            "(--bell-on is accepted as an alias for backward compatibility.)"
        ),
    )

    # -- API status --
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
    ap.add_argument(
        "--no-wakeup",
        dest="wakeup_enabled",
        action="store_false",
        default=True,
        help=(
            "Disable honouring the model's ScheduleWakeup tool calls. By "
            "default the orchestrator re-injects a scheduled prompt as a fresh "
            "turn after the requested delay (keeps autonomous loops alive, "
            "including while background tasks run)."
        ),
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
    ap.add_argument(
        "--no-auto-shutdown",
        action="store_true",
        help=(
            "Never auto-shut-down when all tabs close, even under --open / "
            "--detach (which normally imply --auto-shutdown). The server keeps "
            "running until stopped explicitly."
        ),
    )
    ap.add_argument(
        "--session-idle-timeout",
        type=int,
        default=300,
        metavar="SECS",
        help=(
            "Seconds a session with zero viewers keeps running before it is "
            "torn down. Default: 300 (5 min). 0 disables idle teardown."
        ),
    )
    ap.add_argument(
        "--standalone",
        action="store_true",
        help=(
            "Start a separate server instead of joining an already-running "
            "hub on the same port/account. By default a launch reuses a "
            "running hub, adding its session there."
        ),
    )
    ap.add_argument(
        "--external-password",
        default=None,
        help=(
            "Password for non-LAN access.  Connections from private/loopback "
            "IPs are always allowed; connections from public IPs require HTTP "
            "Basic Auth with this password (leave the username blank).  When "
            "omitted it falls back to the ORCH2_EXTERNAL_PASSWORD env var, then "
            "to the built-in default 'uncommon11'.  Pass an empty string "
            "(--external-password \"\") to block all external access."
        ),
    )
    ap.add_argument(
        "--skip-auto-login",
        action="store_true",
        help=argparse.SUPPRESS,  # internal: --detach child, parent already checked
    )
    ap.add_argument(
        "--wait-port",
        action="store_true",
        help=argparse.SUPPRESS,  # internal: restart child waits for the old instance to free the port
    )

    args = ap.parse_args(argv)

    # --list-models: print available models and exit.  Prefer the live list
    # from the API; fall back to the hardcoded set if it can't be fetched.
    if args.list_models:
        models = fetch_available_models() or KNOWN_MODELS
        print("Available models:")
        for model_id, desc in models:
            print(f"  {model_id:28s} {desc}")
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
        copy=args.copy,
        no_replay=args.no_replay,
        disable_prompt_cache=args.disable_prompt_cache,
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
        bell_on=(
            " ".join(args.bell_on)
            if isinstance(args.bell_on, list)
            else args.bell_on
        ),
        append_system_prompt=args.append_system_prompt,
        mcp_config=args.mcp_config,
        auto_reconnect=args.auto_reconnect,
        wakeup_enabled=args.wakeup_enabled,
        debug=args.debug,
        log_file=args.log_file,
        port=args.port,
        open_browser=args.open or args.detach,
        detach=args.detach,
        auto_shutdown=(
            not args.no_auto_shutdown
            and (args.auto_shutdown or args.open or args.detach)
        ),
        session_idle_timeout=args.session_idle_timeout,
        standalone=args.standalone,
        external_password=args.external_password,
        config_dir=args.config_dir,
        skip_auto_login=args.skip_auto_login,
        wait_port=args.wait_port,
    )
