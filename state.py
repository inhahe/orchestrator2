"""Shared mutable state and status serialisation helpers.

The State dataclass is the single mutable truth for the running session.
Helper functions convert it into JSON-serialisable dicts for the frontend
(status bar, panel data).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import (
    MARKERS,
    RL_TYPE_LABEL,
    SUBSCRIPTION_RL_TYPES,
    Config,
    model_context_window,
    parse_bell_events,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Formatting helpers (pure functions, no side-effects)
# ---------------------------------------------------------------------------

def fmt_duration(seconds: float) -> str:
    """Colon-separated elapsed clock: ``0:0:04``, ``0:2:28``, ``1:4:05``."""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m}:{s:02d}"


def fmt_countdown(seconds: float) -> str:
    """Human-readable countdown: ``4s``, ``1m 23s``, ``1h 4m 5s``, ``18h 3m``.

    Used for time-until-an-event (e.g. rate-limit reset), which reads more
    naturally as ``2h 30m`` than as a colon clock.
    """
    if seconds < 60:
        return f"{int(seconds)}s"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h >= 1:
        return f"{h}h {m}m {s}s" if h < 10 else f"{h}h {m}m"
    return f"{m}m {s}s"


def fmt_tok(n: int) -> str:
    """Compact token count: ``42``, ``3k``, ``175k``, ``1M``, ``1.25M``."""
    if n >= 1_000_000:
        whole = n / 1_000_000
        return f"{whole:.0f}M" if whole >= 10 or whole == int(whole) else f"{whole:.2f}M"
    if n >= 1_000:
        return f"{n // 1000}k"
    return str(n)


def fmt_reset_time(ts: int) -> str:
    """Format a unix timestamp as compact local time.

    Same-day → ``H:MMam/pm``.  Other day → ``mmm DD H:MMam/pm``.
    """
    try:
        when = time.localtime(ts)
    except (OverflowError, OSError, ValueError):
        return str(ts)
    now = time.localtime()
    if when.tm_year == now.tm_year and when.tm_yday == now.tm_yday:
        return time.strftime("%I:%M%p", when).lstrip("0").lower()
    return time.strftime("%b %d %I:%M%p", when).replace(" 0", " ").lower()


def mark(key: str, *, ascii_only: bool = False) -> str:
    """Return a status marker, respecting *ascii_only* mode."""
    u, a = MARKERS.get(key, ("?", "?"))
    return a if ascii_only else u


def _get(obj: Any, key: str, default: int = 0) -> int:
    """Read an int key from a dict or SDK object.

    Returns *default* on a missing key **or** an explicit ``None`` value.
    The Anthropic usage payload sends ``null`` for cache fields when there's
    no cache activity, so ``obj.get(key, 0)`` can return ``None`` and the
    subsequent ``None + int`` would crash the turn.
    """
    if isinstance(obj, dict):
        val = obj.get(key, default)
    else:
        val = getattr(obj, key, default)
    return val if isinstance(val, (int, float)) else default


def extract_context_tokens(
    usage: dict[str, Any] | None,
    model_usage: dict[str, Any] | None,
) -> int:
    """Sum input + cache_read + cache_creation tokens.

    The SDK may provide data in ``usage`` (snake_case or camelCase) or in
    ``model_usage`` (keyed by model name, camelCase values).  Try all
    shapes and return the first non-zero total found.

    Handles both plain dicts and SDK Pydantic-style objects.
    """
    if usage is not None:
        # Try snake_case first (Anthropic API native).
        total = (
            _get(usage, "input_tokens")
            + _get(usage, "cache_read_input_tokens")
            + _get(usage, "cache_creation_input_tokens")
        )
        if total:
            return total
        # Try camelCase (CLI / SDK wrapper).
        total = (
            _get(usage, "inputTokens")
            + _get(usage, "cacheReadInputTokens")
            + _get(usage, "cacheCreationInputTokens")
        )
        if total:
            return total
    if model_usage is not None:
        values = (
            model_usage.values()
            if isinstance(model_usage, dict)
            else []
        )
        total = 0
        for mu in values:
            total += (
                _get(mu, "inputTokens")
                + _get(mu, "cacheReadInputTokens")
                + _get(mu, "cacheCreationInputTokens")
            )
        if total:
            return total
    return 0


def humanize_size(text: str) -> str:
    """Return a human-readable size hint like ``42 lines, 1.2k chars``."""
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    if lines == 0 and text:
        lines = 1
    chars = len(text)
    char_str = f"{chars / 1000:.1f}k chars" if chars >= 1000 else f"{chars} chars"
    return f"{lines} line{'s' if lines != 1 else ''}, {char_str}"


# ---------------------------------------------------------------------------
# Authentication & subscription detection
# ---------------------------------------------------------------------------

def detect_subscription() -> bool:
    """Best-effort subscription detection.

    Returns True unless an API-mode env var is set (ANTHROPIC_API_KEY,
    CLAUDE_CODE_USE_BEDROCK, CLAUDE_CODE_USE_VERTEX).
    """
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return False
    for v in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
        val = os.environ.get(v, "").strip().lower()
        if val and val not in ("0", "false", "no"):
            return False
    return True


def check_authentication() -> tuple[bool, str]:
    """Check whether Claude Code has credentials available.

    Returns ``(ok, reason)`` — ``ok=False`` means the CLI can't
    authenticate.
    """
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return True, "ANTHROPIC_API_KEY set"
    for v in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
        val = os.environ.get(v, "").strip().lower()
        if val and val not in ("0", "false", "no"):
            return True, f"{v} set"
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    path = Path(base if base else Path.home() / ".claude") / ".credentials.json"
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
            if isinstance(oauth, dict) and oauth.get("accessToken"):
                return True, f"OAuth credentials at {path}"
        except (OSError, ValueError) as e:
            return False, f"credentials file {path} exists but is unreadable: {e}"
    return False, (
        "no Claude Code credentials found — neither ANTHROPIC_API_KEY "
        "nor cloud-routing env vars are set, and there's no OAuth "
        f"token at {path}"
    )


def detect_subscription_plan() -> str | None:
    """Read the plan name from OAuth credentials, distinguishing Max 5x/20x."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    path = Path(base if base else Path.home() / ".claude") / ".credentials.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return None
    plan = oauth.get("subscriptionType")
    if not isinstance(plan, str) or not plan:
        return None
    tier = oauth.get("rateLimitTier")
    if isinstance(tier, str) and plan.lower() == "max":
        m = re.search(r"(\d+x)$", tier)
        if m:
            return f"{plan} {m.group(1)}"
    return plan


def config_dir_path(config_dir: str | None = None) -> Path:
    """Resolve a Claude config dir.

    An explicit ``config_dir`` (a cross-account runtime's dir) wins; otherwise
    fall back to the process env (``CLAUDE_CONFIG_DIR``) or ``~/.claude``.
    """
    base = config_dir or os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base) if base else Path.home() / ".claude"


def detect_account_info(config_dir: str | None = None) -> dict[str, Any]:
    """Read the signed-in account details from ``<config-dir>/.claude.json``.

    Returns a dict with whatever could be gathered (all keys optional):
    ``email``, ``display_name``, ``org_name``, ``org_type`` (e.g.
    ``claude_max``), ``org_role``, ``config_dir`` (always present).  The
    ``.claude.json`` file stores these under ``oauthAccount``.  Secrets (the
    OAuth token in ``.credentials.json``) are deliberately never read here.

    ``config_dir`` scopes the read to a specific account (a cross-account hub
    runtime's dir); without it the process env / default is used.
    """
    cfg = config_dir_path(config_dir)
    info: dict[str, Any] = {"config_dir": str(cfg)}
    path = cfg / ".claude.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return info
    oauth = data.get("oauthAccount") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return info
    email = oauth.get("emailAddress")
    if isinstance(email, str) and email:
        info["email"] = email
    for src, dst in (
        ("displayName", "display_name"),
        ("organizationName", "org_name"),
        ("organizationType", "org_type"),
        ("organizationRole", "org_role"),
    ):
        val = oauth.get(src)
        if isinstance(val, str) and val:
            info[dst] = val
    return info


# ---------------------------------------------------------------------------
# Rate-limit helpers
# ---------------------------------------------------------------------------

def apply_rate_limit_info(state: State, info: Any) -> None:
    """Update subscription flag + live utilisation from a rate_limit_info blob."""
    if info is None:
        return
    if isinstance(info, dict):
        rl_type = info.get("rate_limit_type")
        util = info.get("utilization")
        status = info.get("status")
        resets_at = info.get("resets_at")
    else:
        rl_type = getattr(info, "rate_limit_type", None)
        util = getattr(info, "utilization", None)
        status = getattr(info, "status", None)
        resets_at = getattr(info, "resets_at", None)
    if rl_type in SUBSCRIPTION_RL_TYPES:
        state.is_subscription = True
    if isinstance(util, (int, float)) and isinstance(rl_type, str):
        state.rate_limit_utils[rl_type] = float(util)
    if isinstance(status, str):
        if status == "rejected" and state.rate_limit_status != "rejected":
            state.rate_limit_reset_bell_fired = False
            ring_bell(state, "rate-hit")
        state.rate_limit_status = status
    if isinstance(resets_at, (int, float)):
        state.rate_limit_resets_at = int(resets_at)


def reset_rate_limit(state: State) -> bool:
    """Wipe *all* rate-limit state (status, reset time, live utilisation).

    Unlike the "stale rejection" clear (which only fires when the session is
    currently marked ``rejected``), this zeroes everything — including the
    per-window utilisation percentages that drive the status bar's usage
    display.  Used when the account itself changes (a completed ``/login`` to a
    different account, propagated to sibling sessions on the same config dir):
    the previous account's limits and usage numbers no longer describe this
    session, so nothing about them should linger in the toolbar.

    Returns True if anything actually changed, so callers can decide whether a
    status broadcast is worthwhile.
    """
    changed = bool(
        state.rate_limit_status is not None
        or state.rate_limit_resets_at is not None
        or state.rate_limit_reset_bell_fired
        or state.rate_limit_utils
    )
    state.rate_limit_status = None
    state.rate_limit_resets_at = None
    state.rate_limit_reset_bell_fired = False
    state.rate_limit_utils.clear()
    return changed


def ring_bell(state: State, event: str) -> None:
    """Record a bell event.

    Turn-completion events are deferred while bg tasks run.  The web
    frontend receives a ``bell`` WS message instead of ``\\a``.

    Every ring is logged — including the ones filtered out by
    ``state.bell_events``.  From a report: "i often hear it when a turn wasn't
    just completed and no task just finished and there was no rate-hit."  With
    several sessions live in one hub, a bell from *another* session's tab is
    indistinguishable by ear, and nothing anywhere recorded which event fired
    or which session it came from, so the question was unanswerable after the
    fact.  Grep ``bell:`` to attribute one.
    """
    if event not in state.bell_events:
        log.info(
            "bell: %s suppressed (not in --bell set %s) session=%s",
            event, sorted(state.bell_events) or "<empty>",
            (state.session_id or "?")[:12],
        )
        return
    if state.pending_bell and state.pending_bell != event:
        log.info(
            "bell: %s replaced unflushed %s session=%s",
            event, state.pending_bell, (state.session_id or "?")[:12],
        )
    log.info(
        "bell: %s rung session=%s title=%r busy=%s turns=%s",
        event, (state.session_id or "?")[:12], (state.session_title or "")[:40],
        state.busy, state.turns,
    )
    state.pending_bell = event


def in_bg_wait(state: State) -> bool:
    """True when the session is *parked* waiting on background task(s).

    Mirrors the status-bar ``bg-wait`` class: the model is not actively
    producing a turn (``busy``), not (re)connecting, and not rate-limited — it's
    idle-except-for-background-work.  Used to gate the ``bg-done`` bell so it
    only rings when the user is genuinely waiting on the task, not when the
    model spawned it mid-turn and kept right on working (in which case the
    completion is just routine progress, not something to alert about).
    """
    now_ts = int(time.time())
    rate_limited = bool(
        state.rate_limit_status == "rejected"
        and state.rate_limit_resets_at
        and state.rate_limit_resets_at > now_ts
    )
    return not (state.busy or state.connecting or rate_limited)


# ---------------------------------------------------------------------------
# Persistent deque — fires a callback after every mutation
# ---------------------------------------------------------------------------

class PersistentDeque(deque):
    """A ``deque`` that notifies subscribers after any mutation.

    Backs ``State.queued_prompts``.  Two independent things need to know when
    the pending-prompt queue changes, and they must not have to know about each
    other:

    * **persistence** — the queue is mirrored to disk on every change so
      typed-but-not-yet-run prompts survive a server restart (``on_change``,
      wired by ``server._attach_queue_persistence``);
    * **the worker** — a prompt appended while the worker is parked has to
      *poke* it, or it sits unsent forever (``add_listener``, wired by
      ``SDKBridge``).

    ``on_change`` is a single slot, which is why the second subscriber gets a
    list instead of fighting for it.  Hooking the **container** rather than the
    call sites is the point: `queued_prompts` has half a dozen writers spread
    across ``server.py`` and ``sdk_bridge.py``, and every "the prompt didn't
    send" bug in this project's history has been a writer that forgot to poke.
    A writer added tomorrow gets it for free and cannot reintroduce the bug.

    Callbacks take no arguments — they read the current contents from the deque
    itself — and their exceptions are swallowed: neither persistence nor a
    wake-up may break a queue operation.
    """

    on_change = None  # class default; set per-instance once persistence is wired

    def add_listener(self, cb) -> None:
        """Subscribe *cb* to every mutation.  Additive; never clobbers."""
        # Instance attribute created on first use — a class-level mutable
        # default would be shared by every session's queue in the hub.
        if "_listeners" not in self.__dict__:
            self._listeners = []
        self._listeners.append(cb)

    def _fire(self) -> None:
        cb = self.on_change
        if cb is not None:
            try:
                cb()
            except Exception:
                # Persistence must never break queue operations.
                pass
        for listener in self.__dict__.get("_listeners", ()):
            try:
                listener()
            except Exception:
                pass

    def append(self, x):            # type: ignore[override]
        super().append(x); self._fire()

    def appendleft(self, x):        # type: ignore[override]
        super().appendleft(x); self._fire()

    def pop(self):                  # type: ignore[override]
        v = super().pop(); self._fire(); return v

    def popleft(self):              # type: ignore[override]
        v = super().popleft(); self._fire(); return v

    def clear(self) -> None:        # type: ignore[override]
        super().clear(); self._fire()

    def extend(self, it):           # type: ignore[override]
        super().extend(it); self._fire()

    def extendleft(self, it):       # type: ignore[override]
        super().extendleft(it); self._fire()

    def insert(self, i, x):         # type: ignore[override]
        super().insert(i, x); self._fire()

    def remove(self, x):            # type: ignore[override]
        super().remove(x); self._fire()

    def rotate(self, n=1):          # type: ignore[override]
        super().rotate(n); self._fire()

    def __setitem__(self, i, x):
        super().__setitem__(i, x); self._fire()

    def __delitem__(self, i):
        super().__delitem__(i); self._fire()

    def __iadd__(self, other):
        r = super().__iadd__(other); self._fire(); return r


# ---------------------------------------------------------------------------
# State dataclass
# ---------------------------------------------------------------------------

@dataclass
class State:
    """Mutable session state shared across all backend components."""

    # Session identity
    session_id: str | None = None
    session_title: str | None = None
    pending_rename: str | None = None   # title to apply once session_id arrives
    init_seen: bool = False
    expected_resume_sid: str | None = None

    # Metrics
    context_tokens: int = 0
    total_cost_usd: float = 0.0
    turns: int = 0
    last_usage: dict[str, Any] = field(default_factory=dict)

    # Model / effort
    effort: str | None = None
    model: str | None = None
    active_model: str | None = None      # discovered from AssistantMessage.model

    # Busy flags
    busy: bool = False
    connecting: bool = False
    connect_started_at: float | None = None

    # What the *CLI* says it is doing, from ``system``/``status`` messages.
    # Today the only value is ``"compacting"`` (set when the CLI starts an
    # auto-compaction, cleared with ``None`` when it finishes).  Worth
    # surfacing because an auto-compaction is long and completely silent — one
    # observed run took 141 s during which the model emitted nothing at all,
    # so the status bar said "working" with no output and looked wedged.
    cli_status: str | None = None
    cli_status_started_at: float | None = None   # time.monotonic()

    # Turn/compact state
    last_result_subtype: str | None = None
    last_compact_trigger: str | None = None
    compact_during_last_turn: bool = False
    last_compact_turn: int | None = None
    # A compact-induced ResultMessage suppresses run_turn's turn_end on the
    # assumption that a post-compact ghost turn will own it.  When no ghost
    # turn follows (the turn genuinely ended at compaction), this holds the
    # deferred turn_end info {"subtype", "started_at"} so it can still be
    # emitted (by a timer, or the next turn start) instead of being lost.
    pending_compact_turn_end: dict[str, Any] | None = None
    needs_user_attention: str | None = None  # "api-error" or None
    # API-error loop detection.  When a turn ends with a non-retryable API
    # error (e.g. a 400 from a poisoned message deep in history that gets
    # resent on every resume), the same error repeats forever.  Track the
    # last error signature + a consecutive-repeat counter so the worker can
    # break the auto-continue loop and warn the user instead of hammering it.
    last_api_error_signature: str | None = None
    api_error_repeat_count: int = 0
    # Sticky flag: the last turn failed to authenticate (HTTP 401).  Unlike a
    # transient API error this won't clear on its own — the stored OAuth session
    # is dead — so `/login` uses it to force a re-auth instead of falsely
    # reporting "already signed in".  Cleared on the next successful turn.
    auth_error: bool = False
    recent_turn_ends: deque[float] = field(default_factory=deque)
    turn_started_at: float | None = None

    # Active (foreground) tool calls
    active_tools: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Queued user prompts (typed while busy).  A PersistentDeque so the queue
    # can be mirrored to disk on every change (survives a server restart); the
    # save callback is wired up by server.py once the cwd is known.
    queued_prompts: "PersistentDeque[str]" = field(default_factory=PersistentDeque)
    queue_editing_index: int | None = None  # index being edited in the UI

    # (There is deliberately no "was this prompt already echoed?" flag here.
    # There was one — a single slot that only described the last message the
    # WebSocket handler happened to see, so it was wrong whenever two prompts
    # were in flight and unset entirely for the REST producers.  Transcript
    # echoing now happens at enqueue time in server.py's ``_enqueue_prompt``,
    # where the producer is still known.)

    # Background tasks (bg shells + Task subagents)
    background_tasks: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Tool history (ring buffer)
    tool_history: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=200)
    )
    next_tool_seq: int = 1
    current_turn_tool_seqs: list[int] = field(default_factory=list)

    # Background task history
    current_turn_bg: dict[str, dict[str, Any]] = field(default_factory=dict)
    next_bg_seq: int = 1

    # Thinking history (ring buffer)
    thinking_history: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=200)
    )
    next_thinking_seq: int = 1

    # TodoWrite plan
    current_todos: list[dict[str, Any]] = field(default_factory=list)

    # Subscription / plan
    is_subscription: bool = False
    subscription_plan: str | None = None

    # Signed-in account (read once from <config-dir>/.claude.json).  Keys:
    # email, display_name, org_name, org_type, org_role, config_dir.
    account: dict[str, Any] = field(default_factory=dict)

    # Display settings (runtime-mutable)
    collapse_tools: bool = True
    collapse_threshold: int = 3
    max_dom_messages: int = 2000       # 0 = never trim
    inline_all_tools: bool = True
    show_edits: str = "compact"
    show_thinking: bool = False
    thinking_enabled: bool = True
    show_tasks: str = "compact"
    panel_delay: float = 0.0
    panel_grace: float = 10.0

    # Panel completed-task grace state
    completed_panel_tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    completed_panel_bg: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Bell events
    bell_events: set[str] = field(default_factory=set)
    pending_bell: str | None = None

    # Rate limits
    rate_limit_utils: dict[str, float] = field(default_factory=dict)
    rate_limit_status: str | None = None
    rate_limit_resets_at: int | None = None
    rate_limit_reset_bell_fired: bool = False

    # Web transport — connected client IDs (supports multiple browser tabs)
    connected_clients: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# State initialisation from Config
# ---------------------------------------------------------------------------

def init_state_from_config(config: Config) -> State:
    """Create a fresh State populated from parsed Config."""
    initial_effort = None if config.effort in (None, "auto") else config.effort
    sub = detect_subscription()
    return State(
        effort=initial_effort,
        model=config.model,
        is_subscription=sub,
        subscription_plan=detect_subscription_plan() if sub else None,
        account=detect_account_info(getattr(config, "config_dir", None)),
        collapse_tools=config.collapse_tools,
        collapse_threshold=config.collapse_threshold,
        max_dom_messages=config.max_dom_messages,
        inline_all_tools=config.inline_all_tools,
        show_edits=config.show_edits,
        show_thinking=config.show_thinking,
        thinking_enabled=not config.no_thinking,
        show_tasks=config.show_tasks,
        panel_delay=config.panel_delay,
        panel_grace=config.panel_grace,
        bell_events=parse_bell_events(config.bell_on),
    )


# ---------------------------------------------------------------------------
# Status serialisation (for the frontend status bar)
# ---------------------------------------------------------------------------

def state_to_status_dict(state: State, config: Config) -> dict[str, Any]:
    """Produce the JSON-serialisable blob that drives the frontend status bar.

    Mirrors the logic of _panel_session() in the original orchestrator.
    """
    now_ts = int(time.time())

    # --- Busy label + class ---
    rate_limited = (
        state.rate_limit_status == "rejected"
        and state.rate_limit_resets_at
        and state.rate_limit_resets_at > now_ts
    )
    if rate_limited:
        busy_label = "rate limited"
        busy_class = "error"
    elif state.connecting:
        if state.connect_started_at is not None:
            elapsed_s = time.monotonic() - state.connect_started_at
            busy_label = f"connecting ({int(elapsed_s)}s)"
        else:
            busy_label = "connecting"
        busy_class = "connecting"
    elif state.cli_status == "compacting":
        # Ranked above ``busy`` on purpose: a compaction happens *inside* a
        # turn, so busy is also true, and "compacting" is the more informative
        # of the two — it is the one that explains why nothing is being
        # printed.  Timed, because the interesting question during a silent
        # two-minute stretch is "how long has this been going on".
        if state.cli_status_started_at is not None:
            elapsed = fmt_duration(time.monotonic() - state.cli_status_started_at)
            busy_label = f"compacting ({elapsed})"
        else:
            busy_label = "compacting"
        busy_class = "compacting"
    elif state.busy:
        if state.turn_started_at is not None:
            elapsed = fmt_duration(time.monotonic() - state.turn_started_at)
            busy_label = f"working ({elapsed})"
        else:
            busy_label = "working"
        busy_class = "working"
    elif state.background_tasks:
        oldest = min(
            (t.get("started_at", time.monotonic())
             for t in state.background_tasks.values()),
            default=None,
        )
        if oldest is not None:
            elapsed = fmt_duration(time.monotonic() - oldest)
            busy_label = f"bg wait ({len(state.background_tasks)}) ({elapsed})"
        else:
            busy_label = f"bg wait ({len(state.background_tasks)})"
        busy_class = "bg-wait"
    elif state.auth_error:
        # The stored Claude login is dead (a turn 401'd, or the CLI reported an
        # auth failure at connect).  Surfaced prominently since nothing works
        # until the user runs /login.  Cleared on the next successful connect or
        # clean turn, so this label goes away on its own once re-authenticated.
        busy_label = "not authed"
        busy_class = "error"
    elif state.needs_user_attention == "api-error":
        busy_label = "api error"
        busy_class = "error"
    else:
        busy_label = "idle"
        busy_class = "idle"

    # --- Session ---
    session_title = state.session_title

    # --- Plan / cost ---
    rate_field: str | None = None
    rate_limits: dict[str, Any] = {}
    if state.is_subscription:
        plan_field = f"{state.subscription_plan or 'sub'}"
        is_hit = (
            state.rate_limit_status == "rejected"
            and state.rate_limit_resets_at
            and state.rate_limit_resets_at > now_ts
        )
        if is_hit:
            resets = state.rate_limit_resets_at or 0
            rate_field = f"rate-limit reset: {fmt_reset_time(resets)}"
        elif state.rate_limit_utils:
            order = list(RL_TYPE_LABEL.keys())
            seen = [t for t in order if t in state.rate_limit_utils]
            seen += [
                t for t in state.rate_limit_utils
                if t not in RL_TYPE_LABEL
            ]
            parts = [
                f"{state.rate_limit_utils[t] * 100:.0f}% / "
                f"{RL_TYPE_LABEL.get(t, t)}"
                for t in seen
            ]
            rate_field = ", ".join(parts)
            # Structured data for the frontend's rate-limit display.
            for t in seen:
                label = RL_TYPE_LABEL.get(t, t)
                pct = round(state.rate_limit_utils[t] * 100, 1)
                entry: dict[str, Any] = {
                    "label": label,
                    "percent_used": pct,
                }
                if is_hit and state.rate_limit_resets_at:
                    delta = max(0, state.rate_limit_resets_at - now_ts)
                    entry["reset_in"] = fmt_countdown(float(delta))
                rate_limits[label] = entry
    else:
        plan_field = f"${state.total_cost_usd:.4f}"

    # --- Model ---
    effective_model = state.model or state.active_model or ""
    short_model = (
        effective_model[len("claude-"):]
        if effective_model.startswith("claude-")
        else effective_model
    )
    model_display = short_model or "auto"

    # --- Effort / thinking ---
    effort_display = state.effort if state.effort else "auto"
    thinking_display = "on" if state.thinking_enabled else "off"

    # --- Context ---
    window = model_context_window(effective_model)
    window_str = fmt_tok(window) if window else "?"
    if state.context_tokens:
        resident = min(state.context_tokens, window) if window else state.context_tokens
        context_field = f"~{fmt_tok(resident)}/{window_str} tok"
    else:
        context_field = f"~?/{window_str} tok"

    # --- Todos compact ---
    todos = state.current_todos
    if todos:
        done = sum(1 for t in todos if t.get("status") == "completed")
        in_prog_label = ""
        for t in todos:
            if t.get("status") == "in_progress":
                label = (t.get("activeForm") or t.get("content") or "").strip()
                label = label.replace("\n", " ")
                if len(label) > 50:
                    label = label[:49] + "…"
                in_prog_label = label
                break
        todos_compact = f"{done}/{len(todos)}"
    else:
        todos_compact = None
        in_prog_label = ""

    # --- Assemble ---
    return {
        "session_id": state.session_id,
        "session_title": session_title,
        "cwd": config.cwd,
        "busy_label": busy_label,
        "busy_class": busy_class,
        "turns": state.turns,
        "plan_field": plan_field,
        "config_dir": str(config_dir_path(getattr(config, "config_dir", None))),
        "account": state.account,
        "is_subscription": state.is_subscription,
        "rate_field": rate_field,
        "rate_limits": rate_limits,
        "model": model_display,
        "effort": effort_display,
        "thinking": thinking_display,
        "context_field": context_field,
        "context_tokens": state.context_tokens,
        "todos_compact": todos_compact,
        "todos_in_progress": in_prog_label,
        "bg_count": len(state.background_tasks),
        "queued_count": len(state.queued_prompts),
        "collapse_tools": state.collapse_tools,
        "collapse_threshold": state.collapse_threshold,
        "max_dom_messages": state.max_dom_messages,
        # Display gate for --show-thinking: thinking blocks are always sent in
        # full, this only decides whether they start expanded.
        "show_thinking": state.show_thinking,
    }


# ---------------------------------------------------------------------------
# Panel serialisation (for the frontend side panels)
# ---------------------------------------------------------------------------

def state_to_panels_dict(state: State) -> dict[str, Any]:
    """Produce the JSON-serialisable blob for the three side panels."""

    # Active (foreground) tools — filter to Bash for the Bash Commands panel;
    # send all for the generic Tasks panel.
    active_tools_list = []
    for tool_id, info in state.active_tools.items():
        active_tools_list.append({
            "tool_use_id": tool_id,
            "seq": info.get("seq"),
            "name": info.get("name"),
            "input": info.get("input"),
            "started_at": info.get("started_at"),
            "is_background": info.get("is_background", False),
        })

    # Background tasks
    bg_tasks_list = []
    for task_id, info in state.background_tasks.items():
        bg_tasks_list.append({
            "task_id": task_id,
            "seq": info.get("seq"),
            "name": info.get("name"),
            "task_type": info.get("task_type"),
            "started_at": info.get("started_at"),
            "tool_use_id": info.get("tool_use_id"),
            "command": info.get("command"),
        })

    # Completed tools still in grace period
    completed_tools_list = []
    now = time.monotonic()
    expired = []
    for tool_id, info in state.completed_panel_tools.items():
        grace_end = info.get("grace_end", 0)
        if now > grace_end:
            expired.append(tool_id)
            continue
        completed_tools_list.append({
            "tool_use_id": tool_id,
            "seq": info.get("seq"),
            "name": info.get("name"),
            "input": info.get("input"),
            "duration": info.get("duration"),
            "is_error": info.get("is_error", False),
        })
    for tid in expired:
        state.completed_panel_tools.pop(tid, None)

    # Completed bg tasks still in grace period
    completed_bg_list = []
    expired_bg = []
    for task_id, info in state.completed_panel_bg.items():
        grace_end = info.get("grace_end", 0)
        if now > grace_end:
            expired_bg.append(task_id)
            continue
        completed_bg_list.append({
            "task_id": task_id,
            "seq": info.get("seq"),
            "name": info.get("name"),
            "status": info.get("status"),
            "duration": info.get("duration"),
            "command": info.get("command"),
        })
    for tid in expired_bg:
        state.completed_panel_bg.pop(tid, None)

    # Todos
    todos_list = []
    for t in state.current_todos:
        todos_list.append({
            "content": t.get("content", ""),
            "status": t.get("status", "pending"),
            "activeForm": t.get("activeForm"),
        })

    # Pending prompt queue
    queue_list = [
        {"index": i, "text": text}
        for i, text in enumerate(state.queued_prompts)
    ]

    return {
        "active_tools": active_tools_list,
        "background_tasks": bg_tasks_list,
        "completed_tools": completed_tools_list,
        "completed_bg": completed_bg_list,
        "todos": todos_list,
        "queued_prompts": queue_list,
    }
