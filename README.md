# orchestrator2

A web-based orchestrator for the Claude Agent SDK. Wraps Claude Code's agentic capabilities in a multi-file async Python server with a browser UI featuring live status, tool panels, side-by-side diffs, background task tracking, and a fully configurable color theme system.

## Quick Start

```bash
pip install -r requirements.txt
python server.py
```

Opens at `http://localhost:8420`. Add `--open` to launch the browser automatically.

To run from a different directory (e.g. via a launcher script):

```bash
python server.py --cwd "%cd%" --open
```

## Features

- **Live WebSocket UI** -- real-time streaming of assistant messages, tool calls, and results
- **Side-by-side diff viewer** -- UltraCompare-style edit comparisons with configurable colors
- **Tool collapse** -- consecutive tool calls auto-collapse behind a toggle (configurable threshold, persisted across sessions)
- **Sidebar panels** -- live views of active tools, background tasks, pending queue, and TodoWrite plan
- **Draggable sidebar** -- resize handle with width persisted across sessions
- **Auto-compact** -- automatic context compaction when token usage gets high
- **Background tasks** -- track and inspect agent-spawned background work
- **Pending queue** -- type messages while Claude is busy; they queue and execute in order
- **Queue management** -- edit or delete queued prompts from the sidebar
- **Permission dialogs** -- allow/deny tool execution from the browser
- **Session resume** -- automatically continues the most recent session for the working directory
- **Session picker** -- interactive session selection with `--resume`
- **Switch projects** -- change working directory and session with `/cwd <path>`
- **Multi-tab support** -- multiple browser tabs receive synchronized updates
- **Bell notifications** -- configurable audible alerts for key events
- **API stall detection** -- monitors retry patterns and polls Anthropic's status page
- **Theme system** -- 70+ color tokens configurable via file, presets, or the settings page
- **Export** -- save conversations as markdown

## CLI Options

### Session

| Flag | Default | Description |
|------|---------|-------------|
| `--initial-prompt`, `-p` | -- | First message to send on startup |
| `--no-continue` | off | Start a fresh session instead of resuming the most recent one |
| `--no-replay` | off | When resuming, don't replay prior messages into backscroll |
| `--resume [SESSION_ID]` | -- | Resume a specific session by ID, or omit the ID for a picker |
| `--cwd PATH` | `.` | Working directory Claude operates in |

### Model & Effort

| Flag | Default | Description |
|------|---------|-------------|
| `--model NAME` | auto | Model to use (see `--list-models` for available models) |
| `--list-models` | -- | List available models and exit |
| `--effort LEVEL` | auto | Thinking effort: `auto`, `low`, `medium`, `high`, `max` |
| `--no-thinking` | off | Disable extended thinking entirely |

### Context & Compaction

| Flag | Default | Description |
|------|---------|-------------|
| `--auto-compact` | off | Enable auto-compact when context gets large |
| `--no-compact` | on | Disable auto-compact (default) |
| `--compact-at N` | auto | Token threshold for auto-compact (default: 950k for 1M-context models, 160k otherwise) |
| `--compact-cooldown-turns N` | 3 | Skip compact check for N turns after compacting |
| `--max-context-tokens N` | 0 | Cap context via rolling-window trim (0 = disabled) |

### Tools & Permissions

| Flag | Default | Description |
|------|---------|-------------|
| `--permission-mode` | `bypassPermissions` | Permission mode: `bypassPermissions`, `acceptEdits`, `default`, `plan` |
| `--allowed-tool TOOL` | all | Restrict to specific tools (repeatable) |
| `--disallowed-tool TOOL` | none | Block specific tools (repeatable) |

### MCP & System Prompt

| Flag | Default | Description |
|------|---------|-------------|
| `--append-system-prompt TEXT` | -- | Extra instructions appended to the system prompt |
| `--mcp-config PATH` | -- | Path to an MCP servers JSON file |

### Display

| Flag | Default | Description |
|------|---------|-------------|
| `--show-thinking` | off | Print full thinking blocks (default: collapsed snippet) |
| `--show-full-commands` | off | Display Bash command body inline |
| `--show-tool-output` | off | Print full tool result content inline |
| `--show-tool-everything` | off | Shorthand for `--show-full-commands` + `--show-tool-output` |
| `--show-tasks` | `compact` | Non-Bash tool display: `off`, `compact`, `full`, `full+output` |
| `--show-edits` | `compact` | Edit tool display: `off`, `compact`, `full` |
| `--ascii-only` | off | Use ASCII markers instead of Unicode |
| `--collapse-tools` / `--no-collapse-tools` | on | Auto-collapse consecutive tool calls |
| `--collapse-threshold N` | 3 | Number of tools shown before collapsing |

### Bell Notifications

| Flag | Default | Description |
|------|---------|-------------|
| `--bell-on EVENTS` | `waiting,done,stalled,api-stall,requires-action,rate-hit,rate-reset` | Comma-separated bell events. Shortcuts: `all`, `none` |

Valid events: `turn-done`, `waiting`, `done`, `stalled`, `api-stall`, `api-ok`, `interrupt`, `bg-done`, `requires-action`, `rate-hit`, `rate-reset`

### API Stall Detection

| Flag | Default | Description |
|------|---------|-------------|
| `--api-stall-limit N` | 5 | Enter stall state after N retries in window (0 = disable) |
| `--api-stall-window N` | 60.0 | Sliding window (seconds) for stall detection |
| `--status-url URL` | Anthropic statuspage | Status feed URL to poll during stalls |

### Resilience

| Flag | Default | Description |
|------|---------|-------------|
| `--auto-reconnect` | off | Reconnect and auto-continue on CLI crash |

### Server

| Flag | Default | Description |
|------|---------|-------------|
| `--port N` | 8420 | HTTP server port |
| `--open` | off | Open browser automatically on startup |
| `--debug` | off | Print extra diagnostic messages |

## Slash Commands

Type these in the input box. Commands starting with `/` are processed by the orchestrator, not sent to Claude.

### Session

| Command | Description |
|---------|-------------|
| `/clear` | Start a fresh session (wipes context) |
| `/cls` | Clear the chat output area |
| `/cwd [path]` | Show current working directory, or switch to a new one and reconnect |
| `/rename [name]` | Set a custom session title |
| `/export [path]` | Save conversation as markdown |
| `/connect` | Reconnect to the SDK |
| `/quit`, `/exit`, `/q` | Graceful exit |
| `/quit!`, `/exit!`, `/q!` | Force exit |
| `/interrupt`, `/i` | Stop the current turn |

### Info

| Command | Description |
|---------|-------------|
| `/help` | Show all commands |
| `/status` | Session info, cost, and usage |
| `/debug` | Internal state diagnostics |
| `/tools` | List active tools and background tasks |
| `/tasks` | List non-Bash tools this turn |
| `/bg` | List background tasks |
| `/show tN\|bN\|kN` | Inspect tool, background task, or thinking block by tag |
| `/todos` | Show Claude's TodoWrite plan |

### Model & Behavior

| Command | Description |
|---------|-------------|
| `/model` | List available models and show current |
| `/model <name>` | Switch to a different model |
| `/models` | Alias for `/model` |
| `/effort [level]` | Show or set effort (`auto`/`low`/`medium`/`high`/`max`) |
| `/thinking [on\|off]` | Toggle extended thinking |
| `/btw <question>` | Side question in separate context |

### Display

| Command | Description |
|---------|-------------|
| `/collapse [on\|off]` | Toggle tool collapsing |
| `/collapse-threshold N` | Set number of tools before collapsing |
| `/autocompact [on\|off\|N]` | Control auto-compact |
| `/max-context [off\|N]` | Cap context tokens |
| `/bell [all\|none\|EVENTS]` | Configure bell notifications |
| `/queue [N\|drop N\|clear]` | Manage queued prompts |

## Theme System

All UI colors are defined as named tokens. Override them via a `theme.conf` file placed next to `server.py`, or use the built-in settings page.

### Theme file format

Create `theme.conf` in the orchestrator2 directory:

```ini
# Custom colors
bg = #1e1e2e
accent = #cba6f7
green = #a6e3a1
red = #f38ba8

# Tokens can reference other tokens
user-label = $cyan
tool-name = $blue
```

- Lines starting with `#` are comments
- Format: `token-name = value`
- Values: hex colors (`#rgb`, `#rrggbb`, `#rrggbbaa`), `rgba(...)`, `transparent`, or `$token-name` references
- Only non-default tokens need to be listed

### Built-in presets

- `default` -- dark theme with ANSI terminal colors
- `solarized-dark`
- `monokai`
- `github-dark`
- `light`

### Color tokens

There are 70+ configurable tokens organized into groups:

**Backgrounds** -- `bg`, `bg-panel`, `bg-chat`, `bg-input`, `bg-msg-user`, `bg-msg-assistant`, `bg-tool`, `bg-tool-hover`, `bg-modal`, `bg-status`, `bg-code`

**Text** -- `text`, `text-dim`, `text-bright`, `text-muted`

**Palette (ANSI)** -- `green`, `green-dim`, `red`, `red-dim`, `yellow`, `yellow-dim`, `cyan`, `blue`, `purple`, `accent`, `accent-dim`

**Borders** -- `border`, `border-light`

**Messages** -- `user-label`, `user-border`, `assistant-text`, `system-text`, `system-warning`, `system-error`, `system-done`, `system-waiting`, `turn-marker`

**Tools** -- `tool-name`, `tool-summary`, `tool-duration`, `thinking-color`

**Panels** -- `panel-title`, `panel-count-bg`, `panel-count-active-bg`, `todo-pending`, `todo-in-progress`, `todo-completed`, `todo-done-text`

**Status indicators** -- `indicator-idle`, `indicator-working`, `indicator-waiting`, `indicator-done`, `indicator-error`, `indicator-connecting`, `indicator-bg-wait`

**Icons** -- `icon-running`, `icon-complete`, `icon-error`

**Diff viewer** -- `diff-changed-bg`, `diff-changed-char`, `diff-changed-text`, `diff-del-bg`, `diff-del-char`, `diff-ins-bg`, `diff-ins-char`, `diff-gap-bg`

**Input** -- `send-btn-bg`, `interrupt-btn-bg`

**Modal / scrollbar** -- `modal-backdrop`, `scrollbar-thumb`, `scrollbar-hover`

## Architecture

```
server.py          FastAPI app, WebSocket, startup/shutdown
sdk_bridge.py      SDK connection, worker loop, message dispatcher, auto-continue
state.py           Mutable session state, status/panel serializers
config.py          Config dataclass, argparse, constants
commands.py        Slash command parsing and dispatch
session.py         Session discovery, JSONL parsing, history replay, export
tool_manager.py    Tool-use/result rendering helpers
theme.py           Color token system, theme file I/O, CSS generation

static/
  index.html       Page structure
  styles.css       Base styles and CSS variables
  app.js           WebSocket manager, sidebar resize, modal
  chat.js          Message rendering, tool collapse, diff viewer
  status.js        Status bar updates
  panels.js        Sidebar panel rendering
  diff.js          Side-by-side diff computation
```

### Key flows

1. **Startup**: `server.py` lifespan -> `parse_args()` -> `init_state_from_config()` -> find/resume session -> `SDKBridge.start()` -> `worker_loop`
2. **Browser connects**: sends `status_update`, `completion_list`, then session `history`
3. **User message**: browser -> WebSocket -> event queue -> `worker_loop` -> SDK turn -> streamed results back via WebSocket
4. **Auto-continue**: after each turn, checks for `[DONE]`/`[WAITING]` sentinels and burst limits before sending the next continue prompt

## License

MIT
