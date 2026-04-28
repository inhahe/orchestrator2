"""Shared mutable state and status serialisation helpers.

The State dataclass is the single mutable truth for the running session.
Helper functions convert it into JSON-serialisable dicts for the frontend
(status bar, panel data).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import (
    BELL_DEFER_WHEN_BG_RUNNING,
    CONTINUE_PROMPT,
    MARKERS,
    RL_TYPE_LABEL,
    SUBSCRIPTION_RL_TYPES,
    Config,
    model_context_window,
    parse_bell_events,
)

# ---------------------------------------------------------------------------
# Formatting helpers (pure functions, no side-effects)
# ---------------------------------------------------------------------------

def fmt_duration(seconds: float) -> str:
    """Compact duration: ``4.2s``, ``1m 23s``, ``1h 4m 5s``, ``18h 3m``."""
    if seconds < 60:
        return f"{seconds:.1f}s"
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


def ring_bell(state: State, event: str) -> None:
    """Record a bell event.

    Turn-completion events are deferred while bg tasks run.  The web
    frontend receives a ``bell`` WS message instead of ``\\a``.
    """
    if event in BELL_DEFER_WHEN_BG_RUNNING and state.background_tasks:
        state.pending_bell = event
        return
    if event in state.bell_events:
        state.pending_bell = event


# ---------------------------------------------------------------------------
# State dataclass
# ---------------------------------------------------------------------------

@dataclass
class State:
    """Mutable session state shared across all backend components."""

    # Session identity
    session_id: str | None = None
    session_title: str | None = None
    init_seen: bool = False
    expected_resume_sid: str | None = None

    # Auto-continue prompt text (runtime-mutable via /continue-prompt)
    continue_prompt: str = CONTINUE_PROMPT

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

    # Turn/compact state
    last_result_subtype: str | None = None
    last_compact_trigger: str | None = None
    compact_during_last_turn: bool = False
    last_compact_turn: int | None = None
    needs_user_attention: str | None = None  # "waiting"/"burst"/"done"/"api-error"/None
    recent_turn_ends: deque[float] = field(default_factory=deque)
    turn_started_at: float | None = None

    # Active (foreground) tool calls
    active_tools: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Queued user prompts (typed while busy)
    queued_prompts: deque[str] = field(default_factory=deque)

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

    # API stall tracking
    api_retry_times: deque[float] = field(
        default_factory=lambda: deque(maxlen=100)
    )
    api_status_indicator: str | None = None
    api_status_description: str | None = None
    api_stall_source: str | None = None
    api_stall_saw_bad: bool = False

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

    # Display settings (runtime-mutable)
    collapse_tools: bool = True
    collapse_threshold: int = 3
    inline_all_tools: bool = False
    show_edits: str = "compact"
    show_thinking: bool = False
    thinking_enabled: bool = True
    show_tasks_panel: bool = False
    show_bg_panel: bool = True
    show_todos_panel: bool = False
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
        continue_prompt=config.continue_prompt or CONTINUE_PROMPT,
        effort=initial_effort,
        model=config.model,
        is_subscription=sub,
        subscription_plan=detect_subscription_plan() if sub else None,
        collapse_tools=config.collapse_tools,
        collapse_threshold=config.collapse_threshold,
        inline_all_tools=config.inline_all_tools,
        show_edits=config.show_edits,
        show_thinking=config.show_thinking,
        thinking_enabled=not config.no_thinking,
        show_tasks_panel=config.tasks_panel,
        show_bg_panel=config.bg_panel,
        show_todos_panel=config.todos_panel,
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
    elif state.needs_user_attention == "waiting":
        busy_label = "waiting"
        busy_class = "waiting"
    elif state.needs_user_attention == "done":
        busy_label = "done"
        busy_class = "done"
    elif state.needs_user_attention == "burst":
        busy_label = "api error"
        busy_class = "error"
    elif state.needs_user_attention == "api-error":
        label = "api error"
        if state.api_status_description:
            desc = state.api_status_description[:40]
            label = f"api error ({desc})"
        busy_label = label
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
                    entry["reset_in"] = fmt_duration(float(delta))
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
        "busy_label": busy_label,
        "busy_class": busy_class,
        "turns": state.turns,
        "plan_field": plan_field,
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
        "show_tasks_panel": state.show_tasks_panel,
        "show_bg_panel": state.show_bg_panel,
        "show_todos_panel": state.show_todos_panel,
    }
