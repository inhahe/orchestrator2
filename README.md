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

## Running Multiple Instances

Just run `orch.bat` (or `python server.py`) from each project directory. If port 8420 is already in use, the server automatically picks a free port:

```
C:\project-a> orch
orchestrator2 launched on http://localhost:8420

C:\project-b> orch
Port 8420 in use — using 52314 instead.
orchestrator2 launched on http://localhost:52314
```

To use a different Claude account, pass `--config-dir` to point at an alternate `CLAUDE_CONFIG_DIR`:

```bash
orch --config-dir "C:\Users\me\.claude-alt"
```

## Features

- **Live WebSocket UI** -- real-time streaming of assistant messages, tool calls, and results
- **Account display** -- the status bar shows the signed-in account (the email address from `<config-dir>/.claude.json`, falling back to the config-dir name when no email is stored). Hover it to see the display name, organization, plan type, role, and the active `CLAUDE_CONFIG_DIR` — handy when running multiple accounts/config dirs side by side
- **Side-by-side diff viewer** -- UltraCompare-style edit comparisons with configurable colors
- **Two-layer activity collapse** -- every tool call, tool result, and thinking block renders as its own one-line collapsed row with a summary (path, command, pattern, item count, etc.); click any row to expand the full details (diff, output, command body, thinking text). Once Claude speaks again after a run of activity, the whole run between the two messages auto-collapses into a single grouped summary line ("N tools") that can also be expanded to reveal the individual rows underneath
- **Anchored collapse (no lurch)** -- when a run of tools auto-collapses while you're following at the bottom, the text already on screen stays put instead of jerking upward. The reclaimed vertical space is held open as a temporary gap that the next streamed lines fill from the top down; once real content reaches the bottom, normal bottom-following resumes (scrolling up at any time releases the gap)
- **Sidebar panels** -- live views of active tools, background tasks, pending queue, and TodoWrite plan. Click a tool or background task to expand its full detail (the Bash command line, or the tool's input) inline within the panel; click again to collapse. Expanded rows stay open across live updates
- **Draggable sidebar** -- resize handle with width persisted across sessions
- **Auto-compact** -- automatic context compaction when token usage gets high
- **Readable post-compact summary** -- after a compact, the harness-injected summary that becomes the new conversation's starting context is shown as a collapsed box you can click to expand and read in full (Claude Code hides this prompt from the user)
- **Background tasks** -- track and inspect agent-spawned background work
- **Autonomous-loop heartbeat** -- honours the model's `ScheduleWakeup` tool calls: when the agent schedules a self-paced wake-up (e.g. a 60s autonomous-loop tick), the orchestrator re-injects the scheduled prompt as a fresh turn after the requested delay, even while a background task is still running. Disable with `--no-wakeup`
- **Pending queue** -- type messages while Claude is busy; they queue and execute in order
- **Queue management** -- edit or delete queued prompts from the sidebar
- **Prompt history** -- Ctrl+Up/Down to recall previous prompts (persisted across sessions)
- **Permission dialogs** -- allow/deny tool execution from the browser
- **AskUserQuestion fallback** -- Claude's interactive multiple-choice tool (`AskUserQuestion`) has no picker widget in the web UI, so instead of silently failing, its questions and options are surfaced as a chat message and Claude is told to continue the exchange in plain text — you just type your answer
- **Session resume** -- automatically continues the most recent session for the working directory
- **Session picker** -- interactive session selection with `--resume`
- **Switch projects** -- change working directory and session with `/cwd <path>`
- **Multi-tab support** -- multiple browser tabs receive synchronized updates
- **Bell notifications** -- configurable audible alerts for key events
- **API stall detection** -- monitors retry patterns and polls Anthropic's status page
- **API-error loop breaker** -- a turn that ends in an inline API error (e.g. `API Error: 400 …`) is now marked `error` in the turn marker instead of the misleading `success`, and the status bar shows `api error`. When the *same* error repeats two turns in a row — the classic "poisoned history" case where a bad block deep in the resumed conversation (e.g. an unsupported image) is re-sent every turn and never clears — the orchestrator posts a one-time recovery hint (start a fresh session, or trim the conversation) and pauses auto-continue so it won't keep hammering the failing request
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
| `--collapse-tools` / `--no-collapse-tools` | on | Auto-collapse activity between messages |

### Bell Notifications

| Flag | Default | Description |
|------|---------|-------------|
| `--bell EVENTS` | `waiting,done,stalled,api-stall,requires-action,rate-hit,rate-reset` | Comma- or space-separated bell events to ring on. Shortcuts: `all`, `none`. (`--bell-on` is accepted as an alias.) |

Valid events: `turn-done`, `waiting`, `done`, `stalled`, `api-stall`, `api-ok`, `interrupt`, `bg-done`, `requires-action`, `rate-hit`, `rate-reset`

At runtime, use the `/bell` slash command to view or change the bell events without restarting:

- `/bell` — show current settings and usage
- `/bell <e1> <e2>...` — REPLACE: ring on **only** these events (e.g. `/bell done` silences everything except `done`)
- `/bell +<event>` — add an event to the current set
- `/bell -<event>` — remove an event from the current set
- `/bell all` — ring on every event
- `/bell none` — disable the bell entirely

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
| `--no-wakeup` | off | Disable honouring the model's `ScheduleWakeup` tool calls (autonomous-loop heartbeat) |

### Server

| Flag | Default | Description |
|------|---------|-------------|
| `--port N` | 8420 | HTTP server port (auto-selects a free port if in use) |
| `--open` | off | Open browser automatically on startup |
| `--detach` | off | Launch the server in the background and exit the terminal (implies `--open`) |
| `--config-dir PATH` | -- | Override `CLAUDE_CONFIG_DIR` (session/credential storage). Use to run under a different Claude account |
| `--debug` | off | Print extra diagnostic messages |

## Slash Commands

Type these in the input box. Commands starting with `/` are processed by the orchestrator, not sent to Claude.

### Session

| Command | Description |
|---------|-------------|
| `/clear` | Start a fresh session (wipes context) |
| `/cls` | Clear the chat output area |
| `/history [N]` | Clear output and replay session history (last N records; default 2000) |
| `/cwd [path]` | Show current working directory, or switch to a new one and reconnect |
| `/rename [name]` | Set a custom session title |
| `/export [path]` | Save conversation as markdown |
| `/connect` | Reconnect to the SDK |
| `/resume [id\|title]` | Resume a specific session, or open the session picker |
| `/quit`, `/exit`, `/q` | Graceful exit |
| `/quit!`, `/exit!`, `/q!` | Force exit |
| `/interrupt`, `/i` | Stop the current turn |

### Info

| Command | Description |
|---------|-------------|
| `/help` | Show all commands |
| `/status` | Session info, cost, and usage |
| `/debug` | Internal state diagnostics |

### Model & Behavior

| Command | Description |
|---------|-------------|
| `/model` | List available models and show current |
| `/model <name>` | Switch to a different model |
| `/models` | Alias for `/model` |
| `/effort [level]` | Show or set effort (`auto`/`low`/`medium`/`high`/`max`) |
| `/thinking [on\|off]` | Toggle extended thinking |
| `/btw <question>` | Side question in separate context |
| `/graphify [path] [flags]` | Build a knowledge graph ([graphify](https://github.com/safishamsi/graphify)) |

### Display

| Command | Description |
|---------|-------------|
| `/collapse [on\|off]` | Toggle activity collapsing (tools, thinking, turn markers between messages) |
| `/autocompact [on\|off\|N]` | Control auto-compact |
| `/max-context [off\|N]` | Cap context tokens |
| `/bell` | Show current bell events and usage |
| `/bell <events>` | Ring on ONLY these events (replaces current set) |
| `/bell all` / `/bell none` | Enable every event / disable the bell |
| `/bell +<event>` / `/bell -<event>` | Add or remove a single event from the current set |
| `/queue [N\|send\|drop N\|clear]` | Manage queued prompts |

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
