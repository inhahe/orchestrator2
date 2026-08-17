"""Theme configuration — named color tokens with file persistence.

Every color used in the UI is a named token.  Defaults are built-in;
users override individual tokens in ``theme.conf`` (next to server.py)
or via the ``/settings`` web page.

Token names match CSS custom property names (with ``--`` prefix stripped).
The theme engine generates a CSS ``:root`` override block injected into
the HTML ``<head>``.

File format (theme.conf)::

    # comment
    bg = #1a1a2e
    accent = #818cf8
    tool-name = #60a5fa

Blank lines and ``#``-comments are ignored.  Tokens are case-insensitive.
Hex colours must be 3, 4, 6, or 8 hex digits (with ``#`` prefix).
"""

from __future__ import annotations

import re
from collections import OrderedDict
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Token registry — (name, default, group, description)
# ---------------------------------------------------------------------------

# Each tuple: (token_name, default_hex, group, human description)
# Groups are used by the settings UI for organisation.
_TOKEN_DEFS: list[tuple[str, str, str, str]] = [
    # ── Backgrounds ──────────────────────────────────────────────
    ("bg",              "#1a1a2e", "Backgrounds",  "Page / main background"),
    ("bg-panel",        "#16213e", "Backgrounds",  "Sidebar panel background"),
    ("bg-chat",         "#0f0f23", "Backgrounds",  "Chat area background"),
    ("bg-input",        "#1a1a2e", "Backgrounds",  "Input bar background"),
    ("bg-msg-user",     "#1b2838", "Backgrounds",  "User message background"),
    ("bg-msg-assistant","transparent","Backgrounds","Assistant message background"),
    ("bg-tool",         "#111827", "Backgrounds",  "Tool / command block background"),
    ("bg-tool-hover",   "#1e293b", "Backgrounds",  "Tool block hover background"),
    ("bg-modal",        "#0f172a", "Backgrounds",  "Modal overlay background"),
    ("bg-status",       "#0d1117", "Backgrounds",  "Status bar background"),
    ("bg-code",         "#0a0a1a", "Backgrounds",  "Code / pre block background"),

    # ── Text ─────────────────────────────────────────────────────
    ("text",            "#e2e8f0", "Text",         "Primary text"),
    ("text-dim",        "#bcbcbc", "Text",         "Secondary / dim text (38;5;250)"),
    ("text-bright",     "#f1f5f9", "Text",         "Bright / emphasized text"),
    ("text-muted",      "#808080", "Text",         "Muted / disabled text (38;5;244)"),

    # ── Accent ───────────────────────────────────────────────────
    ("accent",          "#11a8cd", "Accent",       "Primary accent (ansicyan)"),
    ("accent-dim",      "#0e7faa", "Accent",       "Darker accent"),

    # ── Status palette (ANSI terminal colours) ───────────────────
    ("green",           "#0dbc79", "Status",       "Success / complete (ansigreen)"),
    ("green-dim",       "#098658", "Status",       "Darker green"),
    ("red",             "#cd3131", "Status",       "Error / failed (ansired)"),
    ("red-dim",         "#a31515", "Status",       "Darker red"),
    ("yellow",          "#e5e510", "Status",       "Working / in-progress (ansiyellow)"),
    ("yellow-dim",      "#b5a000", "Status",       "Darker yellow"),
    ("cyan",            "#11a8cd", "Status",       "Waiting status (ansicyan)"),
    ("blue",            "#3b8eea", "Status",       "Tool name text (bold blue)"),
    ("purple",          "#bc3fbc", "Status",       "Thinking / magenta (ansimagenta)"),

    # ── Borders ──────────────────────────────────────────────────
    ("border",          "#334155", "Borders",      "Standard border"),
    ("border-light",    "#475569", "Borders",      "Light border (hover, scrollbar)"),

    # ── Semantic overrides ───────────────────────────────────────
    # These default to another token (specified as $ref).
    # The settings UI shows them separately so users can diverge.
    ("user-label",      "$cyan",       "Messages",  "\"You\" label text"),
    ("user-border",     "$cyan",       "Messages",  "User message left border"),
    ("assistant-text",  "$text",       "Messages",  "Assistant message text"),
    ("tool-name",       "$blue",       "Tools",     "Tool name in headers"),
    ("tool-summary",    "$text-muted", "Tools",     "Tool summary / description"),
    ("tool-duration",   "$text-muted", "Tools",     "Tool elapsed time"),
    ("thinking-color",  "$purple",     "Tools",     "Thinking block header"),
    ("system-text",     "$purple",     "Messages",  "System message text"),
    ("system-warning",  "$yellow",     "Messages",  "Warning system message"),
    ("system-error",    "$red",        "Messages",  "Error system message"),
    ("system-done",     "$green",      "Messages",  "Done system message"),
    ("system-waiting",  "$cyan",       "Messages",  "Waiting system message"),
    ("turn-marker",     "$text-muted", "Messages",  "Turn boundary text"),
    ("send-btn-bg",     "$accent",     "Input",     "Send button background"),
    ("interrupt-btn-bg","$red-dim",    "Input",     "Interrupt button background"),
    ("panel-title",     "$text-muted", "Panels",    "Panel header text"),
    ("panel-count-bg",  "$border",     "Panels",    "Panel count badge background"),
    ("panel-count-active-bg","$accent-dim","Panels", "Active count badge background"),
    ("icon-running",    "$yellow",     "Icons",     "Running tool/task icon"),
    ("icon-complete",   "$green",      "Icons",     "Completed tool/task icon"),
    ("icon-error",      "$red",        "Icons",     "Error tool/task icon"),
    ("indicator-idle",  "#666666",     "Icons",     "Idle status dot"),
    ("indicator-working","$green",     "Icons",     "Working status dot"),
    ("indicator-waiting","$cyan",      "Icons",     "Waiting status dot"),
    ("indicator-done",  "$green",      "Icons",     "Done status dot"),
    ("indicator-error", "$red",        "Icons",     "Error status dot"),
    ("indicator-connecting","$yellow", "Icons",     "Connecting status dot"),
    ("indicator-bg-wait","$purple",    "Icons",     "Background-wait status dot"),
    ("indicator-compacting","$yellow", "Icons",     "Compacting status dot"),
    ("modal-backdrop",  "rgba(0,0,0,0.6)","Backgrounds","Modal dim overlay"),
    ("scrollbar-thumb", "$border",     "Borders",   "Scrollbar thumb"),
    ("scrollbar-hover", "$border-light","Borders",  "Scrollbar thumb hover"),
    ("todo-pending",    "$text-muted", "Panels",    "Pending todo text"),
    ("todo-in-progress","$yellow",     "Panels",    "In-progress todo icon"),
    ("todo-completed",  "$green",      "Panels",    "Completed todo icon"),
    ("todo-done-text",  "#666666",     "Panels",    "Completed todo text"),

    # ── Diff viewer (UltraCompare-style) ────────────────────────
    ("diff-changed-bg",   "rgba(0, 70, 70, 0.15)",     "Diff", "Modified row tint (subtle teal)"),
    ("diff-changed-char", "$red",                       "Diff", "Differing text foreground (red)"),
    ("diff-changed-text", "#bcbcbc",                   "Diff", "Unchanged text on modified rows"),
    ("diff-del-bg",       "rgba(0, 70, 70, 0.15)",     "Diff", "Deleted row tint"),
    ("diff-del-char",     "$red",                       "Diff", "Deleted text foreground (red)"),
    ("diff-ins-bg",       "rgba(0, 70, 70, 0.15)",     "Diff", "Inserted row tint"),
    ("diff-ins-char",     "$red",                       "Diff", "Inserted text foreground (red)"),
    ("diff-gap-bg",       "rgba(80, 80, 80, 0.08)",    "Diff", "Gap placeholder background"),
]

# Build lookup structures.
TOKEN_NAMES: list[str] = [t[0] for t in _TOKEN_DEFS]
_DEFAULTS: OrderedDict[str, str] = OrderedDict((t[0], t[1]) for t in _TOKEN_DEFS)
_GROUPS: OrderedDict[str, str] = OrderedDict((t[0], t[2]) for t in _TOKEN_DEFS)
_DESCS: OrderedDict[str, str] = OrderedDict((t[0], t[3]) for t in _TOKEN_DEFS)

# Hex colour regex (supports 3, 4, 6, 8 digit hex).
_HEX_RE = re.compile(r'^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$')
# Also accept rgba(...) for backdrop.
_RGBA_RE = re.compile(r'^rgba?\(.+\)$')
# Token reference.
_REF_RE = re.compile(r'^\$([a-z][a-z0-9-]*)$')
# "transparent" keyword.
_TRANSPARENT = "transparent"

# Default config file path (next to this module).
_DEFAULT_CONF_PATH = Path(__file__).parent / "theme.conf"


# ---------------------------------------------------------------------------
# Theme class
# ---------------------------------------------------------------------------

class Theme:
    """Loaded theme: defaults + user overrides, resolved references."""

    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        # Raw values (may contain $refs).
        self._raw: OrderedDict[str, str] = OrderedDict(_DEFAULTS)
        if overrides:
            for k, v in overrides.items():
                k = k.lower().strip()
                if k in self._raw:
                    self._raw[k] = v.strip()

        # Resolved values (no $refs).
        self._resolved: OrderedDict[str, str] = OrderedDict()
        self._resolve_all()

    def _resolve_all(self) -> None:
        """Expand all $ref tokens to their final values."""
        self._resolved.clear()
        for name in self._raw:
            self._resolved[name] = self._resolve(name, set())

    def _resolve(self, name: str, seen: set[str]) -> str:
        """Resolve a single token, detecting circular refs."""
        if name in seen:
            return _DEFAULTS.get(name, "#ff00ff")  # fallback: magenta
        seen.add(name)
        val = self._raw.get(name, _DEFAULTS.get(name, "#ff00ff"))
        m = _REF_RE.match(val)
        if m:
            ref = m.group(1)
            if ref in self._raw:
                return self._resolve(ref, seen)
            return _DEFAULTS.get(ref, val)
        return val

    def get(self, name: str) -> str:
        """Return the resolved colour value for *name*."""
        return self._resolved.get(name, _DEFAULTS.get(name, "#ff00ff"))

    def get_raw(self, name: str) -> str:
        """Return the raw (possibly $ref) value for *name*."""
        return self._raw.get(name, _DEFAULTS.get(name, ""))

    def is_default(self, name: str) -> bool:
        """True if this token still has its built-in default."""
        return self._raw.get(name) == _DEFAULTS.get(name)

    def set_token(self, name: str, value: str) -> bool:
        """Set a token value.  Returns True if valid and accepted."""
        name = name.lower().strip()
        value = value.strip()
        if name not in self._raw:
            return False
        if not _is_valid_value(value):
            return False
        self._raw[name] = value
        self._resolve_all()
        return True

    def reset_token(self, name: str) -> None:
        """Reset a token to its built-in default."""
        name = name.lower().strip()
        if name in _DEFAULTS:
            self._raw[name] = _DEFAULTS[name]
            self._resolve_all()

    def reset_all(self) -> None:
        """Reset every token to defaults."""
        self._raw = OrderedDict(_DEFAULTS)
        self._resolve_all()

    # ------------------------------------------------------------------
    # CSS generation
    # ------------------------------------------------------------------

    def to_css_root(self) -> str:
        """Generate a CSS ``:root { ... }`` block with all resolved tokens."""
        lines = [":root {"]
        for name, value in self._resolved.items():
            lines.append(f"  --{name}: {value};")
        lines.append("}")
        return "\n".join(lines)

    def to_css_overrides(self) -> str:
        """Generate CSS only for tokens that differ from the stylesheet defaults.

        This is injected as a ``<style>`` tag to override the built-in
        ``styles.css`` values.
        """
        diffs: list[str] = []
        for name, resolved in self._resolved.items():
            # Check if the resolved value differs from the hardcoded CSS default.
            css_default = _CSS_DEFAULTS.get(name)
            if css_default is not None and resolved != css_default:
                diffs.append(f"  --{name}: {resolved};")
            elif css_default is None:
                # New semantic token not in the base CSS — always emit.
                diffs.append(f"  --{name}: {resolved};")
        if not diffs:
            return ""
        return ":root {\n" + "\n".join(diffs) + "\n}"

    # ------------------------------------------------------------------
    # JSON (for the settings API / frontend)
    # ------------------------------------------------------------------

    def to_dict(self) -> list[dict[str, Any]]:
        """Structured list of all tokens for the settings UI."""
        result = []
        for name in TOKEN_NAMES:
            result.append({
                "name": name,
                "value": self.get(name),
                "raw": self.get_raw(name),
                "default": _DEFAULTS[name],
                "is_default": self.is_default(name),
                "group": _GROUPS.get(name, "Other"),
                "description": _DESCS.get(name, ""),
            })
        return result

    def groups(self) -> list[dict[str, Any]]:
        """Tokens grouped by category, for the settings UI."""
        groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        for entry in self.to_dict():
            g = entry["group"]
            if g not in groups:
                groups[g] = []
            groups[g].append(entry)
        return [{"group": g, "tokens": tokens} for g, tokens in groups.items()]


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def load_theme(path: Path | None = None) -> Theme:
    """Load a theme from *path* (defaults to ``theme.conf`` next to server.py).

    Missing file → all defaults.  Parse errors are logged and skipped.
    """
    if path is None:
        path = _DEFAULT_CONF_PATH
    overrides: dict[str, str] = {}
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
            overrides = _parse_conf(text)
        except Exception:
            pass  # Use defaults on error.
    return Theme(overrides)


def save_theme(theme: Theme, path: Path | None = None) -> None:
    """Write only the non-default tokens to *path*."""
    if path is None:
        path = _DEFAULT_CONF_PATH
    lines: list[str] = [
        "# orchestrator2 theme — edit colours here or use /settings in the UI.",
        "# Format: token = #hexcolor   (or $other-token to reference another token)",
        "# Delete a line to revert that token to its default.",
        "",
    ]
    current_group: str | None = None
    for name in TOKEN_NAMES:
        if theme.is_default(name):
            continue
        group = _GROUPS.get(name, "Other")
        if group != current_group:
            current_group = group
            lines.append(f"# {group}")
        raw = theme.get_raw(name)
        desc = _DESCS.get(name, "")
        if desc:
            lines.append(f"{name} = {raw}    # {desc}")
        else:
            lines.append(f"{name} = {raw}")
    if lines[-1] != "":
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _parse_conf(text: str) -> dict[str, str]:
    """Parse a theme.conf text into {name: value} overrides."""
    result: dict[str, str] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip inline comment.
        if "  #" in line:
            line = line[:line.index("  #")].strip()
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip().lower()
        value = value.strip()
        if name and value and name in _DEFAULTS:
            if _is_valid_value(value):
                result[name] = value
    return result


# ---------------------------------------------------------------------------
# Saved palettes
# ---------------------------------------------------------------------------

_PALETTES_PATH = Path(__file__).parent / "theme-palettes.conf"


def load_saved_colors() -> list[str]:
    """Load user's saved colour swatches from ``theme-palettes.conf``."""
    if not _PALETTES_PATH.exists():
        return []
    colors: list[str] = []
    try:
        for line in _PALETTES_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and _HEX_RE.match(line):
                if line not in colors:
                    colors.append(line)
    except Exception:
        pass
    return colors


def save_color(color: str) -> bool:
    """Append a colour to the saved-palettes file.  Returns True on success."""
    color = color.strip()
    if not _HEX_RE.match(color):
        return False
    existing = load_saved_colors()
    if color in existing:
        return True  # Already saved.
    existing.append(color)
    try:
        _PALETTES_PATH.write_text(
            "\n".join(existing) + "\n", encoding="utf-8",
        )
    except Exception:
        return False
    return True


def remove_saved_color(color: str) -> bool:
    """Remove a colour from the saved-palettes file."""
    color = color.strip()
    existing = load_saved_colors()
    if color not in existing:
        return False
    existing.remove(color)
    try:
        _PALETTES_PATH.write_text(
            "\n".join(existing) + "\n", encoding="utf-8",
        )
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _is_valid_value(v: str) -> bool:
    """Check if *v* is a valid token value."""
    v = v.strip()
    if v == _TRANSPARENT:
        return True
    if _HEX_RE.match(v):
        return True
    if _RGBA_RE.match(v):
        return True
    if _REF_RE.match(v):
        ref = _REF_RE.match(v).group(1)
        return ref in _DEFAULTS
    return False


# ---------------------------------------------------------------------------
# CSS default map — for generating minimal overrides
# ---------------------------------------------------------------------------
# These are the values that styles.css hardcodes.  Only tokens that differ
# from these need to be emitted in the override block.
_CSS_DEFAULTS: dict[str, str] = {
    "bg":              "#1a1a2e",
    "bg-panel":        "#16213e",
    "bg-chat":         "#0f0f23",
    "bg-input":        "#1a1a2e",
    "bg-msg-user":     "#1b2838",
    "bg-msg-assistant":"transparent",
    "bg-tool":         "#111827",
    "bg-tool-hover":   "#1e293b",
    "bg-modal":        "#0f172a",
    "bg-status":       "#0d1117",
    "bg-code":         "#0a0a1a",
    "text":            "#e2e8f0",
    "text-dim":        "#bcbcbc",
    "text-bright":     "#f1f5f9",
    "text-muted":      "#808080",
    "accent":          "#11a8cd",
    "accent-dim":      "#0e7faa",
    "green":           "#0dbc79",
    "green-dim":       "#098658",
    "red":             "#cd3131",
    "red-dim":         "#a31515",
    "yellow":          "#e5e510",
    "yellow-dim":      "#b5a000",
    "cyan":            "#11a8cd",
    "blue":            "#3b8eea",
    "purple":          "#bc3fbc",
    "border":          "#334155",
    "border-light":    "#475569",
    "diff-changed-bg":   "rgba(0, 70, 70, 0.15)",
    "diff-changed-char": "#cd3131",
    "diff-changed-text": "#bcbcbc",
    "diff-del-bg":       "rgba(0, 70, 70, 0.15)",
    "diff-del-char":     "#cd3131",
    "diff-ins-bg":       "rgba(0, 70, 70, 0.15)",
    "diff-ins-char":     "#cd3131",
    "diff-gap-bg":       "rgba(80, 80, 80, 0.08)",
}


# ---------------------------------------------------------------------------
# Built-in preset themes
# ---------------------------------------------------------------------------

PRESET_THEMES: dict[str, dict[str, str]] = {
    "default": {},  # all defaults

    "solarized-dark": {
        "bg":            "#002b36",
        "bg-panel":      "#073642",
        "bg-chat":       "#001e26",
        "bg-input":      "#002b36",
        "bg-msg-user":   "#073642",
        "bg-tool":       "#073642",
        "bg-tool-hover": "#0a4f5c",
        "bg-modal":      "#001e26",
        "bg-status":     "#001219",
        "bg-code":       "#00141c",
        "text":          "#839496",
        "text-dim":      "#657b83",
        "text-bright":   "#fdf6e3",
        "text-muted":    "#586e75",
        "accent":        "#268bd2",
        "accent-dim":    "#2176b0",
        "green":         "#859900",
        "green-dim":     "#6b7d00",
        "red":           "#dc322f",
        "red-dim":       "#b5201e",
        "yellow":        "#b58900",
        "yellow-dim":    "#936e00",
        "cyan":          "#2aa198",
        "blue":          "#268bd2",
        "purple":        "#6c71c4",
        "border":        "#073642",
        "border-light":  "#586e75",
    },

    "monokai": {
        "bg":            "#272822",
        "bg-panel":      "#1e1f1c",
        "bg-chat":       "#1c1d19",
        "bg-input":      "#272822",
        "bg-msg-user":   "#3e3d32",
        "bg-tool":       "#3e3d32",
        "bg-tool-hover": "#49483e",
        "bg-modal":      "#1c1d19",
        "bg-status":     "#1a1a16",
        "bg-code":       "#171812",
        "text":          "#f8f8f2",
        "text-dim":      "#a6a69c",
        "text-bright":   "#ffffff",
        "text-muted":    "#75715e",
        "accent":        "#66d9ef",
        "accent-dim":    "#52b5c9",
        "green":         "#a6e22e",
        "green-dim":     "#86b822",
        "red":           "#f92672",
        "red-dim":       "#c91e5a",
        "yellow":        "#e6db74",
        "yellow-dim":    "#c4b854",
        "cyan":          "#66d9ef",
        "blue":          "#66d9ef",
        "purple":        "#ae81ff",
        "border":        "#49483e",
        "border-light":  "#75715e",
    },

    "github-dark": {
        "bg":            "#0d1117",
        "bg-panel":      "#161b22",
        "bg-chat":       "#010409",
        "bg-input":      "#0d1117",
        "bg-msg-user":   "#161b22",
        "bg-tool":       "#161b22",
        "bg-tool-hover": "#21262d",
        "bg-modal":      "#010409",
        "bg-status":     "#010409",
        "bg-code":       "#0d1117",
        "text":          "#c9d1d9",
        "text-dim":      "#8b949e",
        "text-bright":   "#f0f6fc",
        "text-muted":    "#484f58",
        "accent":        "#58a6ff",
        "accent-dim":    "#388bfd",
        "green":         "#3fb950",
        "green-dim":     "#238636",
        "red":           "#f85149",
        "red-dim":       "#da3633",
        "yellow":        "#d29922",
        "yellow-dim":    "#bb8009",
        "cyan":          "#39d2e0",
        "blue":          "#58a6ff",
        "purple":        "#bc8cff",
        "border":        "#30363d",
        "border-light":  "#484f58",
    },

    "light": {
        "bg":            "#ffffff",
        "bg-panel":      "#f6f8fa",
        "bg-chat":       "#ffffff",
        "bg-input":      "#ffffff",
        "bg-msg-user":   "#f0f4f8",
        "bg-msg-assistant":"transparent",
        "bg-tool":       "#f6f8fa",
        "bg-tool-hover": "#eaeef2",
        "bg-modal":      "#ffffff",
        "bg-status":     "#f6f8fa",
        "bg-code":       "#f0f2f5",
        "text":          "#1f2328",
        "text-dim":      "#57606a",
        "text-bright":   "#000000",
        "text-muted":    "#8b949e",
        "accent":        "#0969da",
        "accent-dim":    "#0550ae",
        "green":         "#1a7f37",
        "green-dim":     "#116329",
        "red":           "#cf222e",
        "red-dim":       "#a40e26",
        "yellow":        "#9a6700",
        "yellow-dim":    "#7d5200",
        "cyan":          "#0598bc",
        "blue":          "#0969da",
        "purple":        "#8250df",
        "border":        "#d0d7de",
        "border-light":  "#afb8c1",
        "modal-backdrop":"rgba(0,0,0,0.3)",
    },
}
