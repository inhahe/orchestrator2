# orchestrator2

A web-based orchestrator for the Claude Agent SDK. Wraps Claude Code's agentic capabilities in a multi-file async Python server with a browser UI featuring live status, tool panels, side-by-side diffs, background task tracking, and a fully configurable color theme system.

## How to Set Up & Use This

**1. Prerequisites**

- **Python 3.11+**
- **Claude Code CLI** — install it from <https://claude.com/claude-code> (the `claude` command must be on your `PATH`). orchestrator2 drives the same CLI the Claude Code app uses, and shares its session store and login.

**2. Install dependencies**

```bash
pip install -r requirements.txt
```

**3. Sign in to your Claude account**

You need to be logged in to Claude. orchestrator2 checks this automatically on startup and, if you're not signed in, opens the normal Claude login window for you. You can also do it yourself any time:

```bash
claude auth login          # from a terminal, or
/login                     # from inside orchestrator2 (then /connect)
```

**4. Run it**

From the project directory you want Claude to work in:

```bash
python server.py --cwd "%cd%" --open
```

- `--cwd "%cd%"` — the working directory Claude operates in (defaults to the current directory).
- `--open` — open the browser automatically. Otherwise browse to the printed URL (default `http://localhost:8420`; it auto-picks a free port if 8420 is taken).

The server starts serving and the browser opens **immediately**; the status bar shows **`connecting…`** while the Claude SDK finishes loading in the background, then flips to `idle` when it's ready to take your first message.

**5. Use it**

- Type a message and press **Enter** to send (**Shift+Enter** for a newline).
- Type while Claude is busy to **queue** follow-up prompts (they run in order).
- The **status bar** (above the input) shows state, **config dir**, account, session, **working directory (full path)**, turns, model, effort, context usage, and rate limits.
- The **sidebar panels** show active tools, background tasks, the pending queue, and the current plan/todos.
- **Slash commands** start with `/` — type `/help` for the full list. Common ones: `/status`, `/cwd <path>` (switch project), `/model`, `/effort`, `/resume`, `/rename`, `/switch` (move this session to another account), `/login`, `/clear`, `/interrupt`.

Sessions are stored the same way Claude Code stores them — under `<config-dir>/projects/<cwd>/` — so a conversation is interchangeable with `claude --continue` / `claude --resume` **as long as both use the same `CLAUDE_CONFIG_DIR`** (account). See [Choosing a Claude account](#choosing-a-claude-account).

## Quick Start

```bash
pip install -r requirements.txt
python server.py --cwd "%cd%" --open
```

## Running Multiple Sessions (central hub)

The server is a **central hub**: the first launch starts it on port 8420, and
every later launch from another directory *joins that hub* as a new session
rather than starting a second server. The launch opens a browser tab attached
to its own session, and all tabs viewing the same session stay in live sync.

```
C:\project-a> orch
orchestrator2 starting on http://localhost:8420

C:\project-b> orch
Joined running orchestrator2 hub on port 8420 (session s2).
```

Click **☰ Sessions** in the status bar to open the **lobby** — a browser for
every session on the hub. It lists the sessions running live (with their
working directory, viewer count and a busy dot) plus recent on-disk sessions
you can reopen, and has a **New session** button (optionally pointed at a
different working directory). Picking a running session attaches this tab to
it; opening a recent one spins up a fresh live session for it. The lobby
overlay closes as soon as the session loads; the tab stays attached to
whatever it's viewing. Recent on-disk sessions are scanned across **every**
Claude account on the machine (each is tagged with its account), so a session
started under a different `CLAUDE_CONFIG_DIR` still shows up and reopens under
the right account.

On a desktop browser each session gets its **own tab** — picking one focuses
that tab, or opens a new one. On **mobile** (where a page can't raise another
tab to the foreground) the lobby instead switches the *current* tab to the
session you picked, updating the address bar to `?rid=<rid>` so a refresh
returns to the same session.

The lobby header also has **⟳ Restart server** and **⏛ Shut down server**
buttons. Restart spawns a fresh server process (picking up code changes) that
resumes the primary session and reloads your tab once it's serving; the
replacement gets its **own console window** so the `claude` subprocess draws
into it (no stray window) and any startup failure is visible rather than
silent.

A launch only reuses a hub with the **same account** (`CLAUDE_CONFIG_DIR`) on
the same port. Notes:

- **`--standalone`** forces a separate, independent server instead of joining
  the hub (it binds 8420, or auto-picks a free port if that's taken).
- If port 8420 is occupied by something that *isn't* an orchestrator2 hub for
  this account, the launch starts its own server on an auto-selected free port.
- A session with zero viewers is torn down after `--session-idle-timeout`
  seconds (default 300; `0` disables); the hub itself keeps running.

### Accessing the hub from other devices (LAN)

The server binds `0.0.0.0`, so it's reachable from other machines on your
network at `http://<host-ip>:8420` (find `<host-ip>` with `ipconfig`). Every
tab — local or remote — lands on the same hub and can browse the lobby, so a
laptop or phone on the same Wi-Fi can watch or drive any session live.

Windows Firewall blocks the inbound port by default. To allow it, run once in
an **elevated** PowerShell/Command Prompt:

```powershell
netsh advfirewall firewall add rule name="orchestrator2" ^
  dir=in action=allow protocol=TCP localport=8420
```

Remove it later with:

```powershell
netsh advfirewall firewall delete rule name="orchestrator2"
```

Use `localport=<port>` to match a non-default `--port`.

**Authentication.** Connections from LAN/loopback addresses (`10.x`, `172.16–31.x`,
`192.168.x`, `127.x`, IPv6 link-local) pass through with no credentials — the
LAN workflow stays frictionless. Connections from any other (public) address
require a password, entered in the browser's sign-in prompt (leave the username
blank; only the password is checked). The default password is `uncommon11`; set
your own with `--external-password <pw>` or the `ORCH2_EXTERNAL_PASSWORD`
environment variable, or pass `--external-password ""` to block all external
access outright. After the page authenticates, an `HttpOnly` cookie carries the
credential to the WebSocket automatically (browsers don't resend Basic-Auth
headers on socket upgrades), so live streaming works from outside the LAN too.

Wrong-password attempts are rate-limited with a single **global** counter (not
per source IP — per-IP throttling would let a botnet get a fresh budget per
address): the first five failures are free, after which the hub is locked out
for an exponentially growing window (2 s, doubling each further failure up to
5 min) during which every external request is refused with `429` before the
password is even checked — so the whole hub allows only a handful of guesses per
escalating window no matter how many machines are trying. A single correct
password clears the counter. (LAN/loopback always bypasses the throttle, so a
lockout only affects remote access, and the 5-min cap bounds it.) Password and
cookie-token comparisons run in constant time (`hmac.compare_digest`) so the
secret can't be recovered by response-timing analysis.

### Choosing a Claude account

orchestrator2 uses the config dir given by `CLAUDE_CONFIG_DIR` (or `~/.claude` when that's unset), the same as the `claude` CLI. Sessions live under `<config-dir>/projects/<cwd>/`, so `claude --continue` / `--resume` only share a conversation with orchestrator2 when both are pointed at the **same** config dir. To pin a specific account regardless of the environment, pass `--config-dir`:

```bash
orch --config-dir "C:\Users\me\.claude-alt"
```

Switch accounts at runtime with `/logout` then `/login` (then `/connect` to reconnect). The active account is shown in the status bar's **account** field (hover for the config dir).

## Features

- **Live WebSocket UI** -- real-time streaming of assistant messages, tool calls, and results
- **Config-dir display** -- a dedicated **config** field (just before **account**) shows the active `CLAUDE_CONFIG_DIR` this session uses, as its trailing folder name (e.g. `.claude-account-b`); hover for the full path. Makes it obvious which config dir / account store a session is on when running several side by side
- **Account display** -- the status bar shows the signed-in account (the email address from `<config-dir>/.claude.json`, falling back to the config-dir name when no email is stored). Hover it to see the display name, organization, plan type, role, and the active `CLAUDE_CONFIG_DIR` — handy when running multiple accounts/config dirs side by side
- **Working-directory display** -- the status bar shows the full working-directory path Claude is operating in
- **Auto-login** -- checks your Claude sign-in on startup and opens the standard Claude login window if you're not authenticated (the same flow Claude Code uses). Sign in / out at runtime with `/login` and `/logout`
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
- **Session picker** -- interactive full-screen terminal session selection with `--resume` (no id); `--copy` copies a session in from another account first
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
| `--disable-prompt-cache` | off | Turn off Claude prompt caching in the CLI (sets `DISABLE_PROMPT_CACHING`). Workaround for the bundled CLI's `ttl='1h' cache_control must not come after ttl='5m'` API 400 on long resumed sessions; costs cache savings, so leave off unless you hit that error |
| `--resume [SESSION_ID]` | -- | Resume a specific session by ID, or omit the ID to open a full-screen terminal picker (grouped by project) before the server starts |
| `--copy` | off | Open a full-screen terminal wizard to copy a session between Claude accounts: pick source account + session, destination account, and a name. If copied into the current account it asks whether to open it now. Whenever nothing gets opened (cancelled, declined, or a cross-account copy) it then asks what to open — the current directory's most-recent session (the default), pick another, or a fresh empty session |
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
| `--mcp-config PATH_OR_JSON` | -- | MCP servers config — a JSON file path or inline JSON string. Lets the orchestrator act as an MCP client. Use the `/mcp` command at runtime to inspect and manage the configured servers |

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
| `--bell EVENTS` | `turn-done,bg-done,requires-action,rate-hit` | Comma- or space-separated bell events to ring on. Shortcuts: `all`, `none`. (`--bell-on` is accepted as an alias.) |

Valid events: `turn-done`, `interrupt`, `bg-done`, `requires-action`, `rate-hit`

`bg-done` rings only when a background task finishes **while the session is
parked waiting on it** (the "bg wait" status) — not when the model spawned the
task mid-turn and kept working, since a routine mid-turn completion isn't worth
alerting you about.

The flag accepts the same three forms as the `/bell` command:

- **Replace** — bare event names ring on *only* those events, given as separate words or one quoted string: `--bell turn-done bg-done` or `--bell "turn-done bg-done"`
- **Modify** — `+`/`-` prefixed tokens start from the defaults, then add or remove: `--bell +interrupt` (defaults **plus** `interrupt`), `--bell=-bg-done` (defaults **minus** `bg-done`; use the `=` form for a leading `-` so it isn't parsed as a flag)
- **Shortcuts** — `--bell all` (every event) or `--bell none` (disable the bell)

At runtime, use the `/bell` slash command to view or change the bell events without restarting:

- `/bell` — show current settings and usage
- `/bell <e1> <e2>...` — REPLACE: ring on **only** these events (e.g. `/bell turn-done` silences everything except `turn-done`)
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
| `--auto-shutdown` | off | Shut the server down when all browser tabs close (auto-set by `--open`/`--detach`) |
| `--no-auto-shutdown` | off | Never auto-shut-down when tabs close, even under `--open`/`--detach`; the server runs until stopped explicitly |
| `--standalone` | off | Start a separate server instead of joining a running hub on the same port/account |
| `--session-idle-timeout SECS` | 300 | Seconds a session with zero viewers lingers before teardown (`0` disables) |
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
| `/switch` | Copy this session into another Claude account and continue it in the same window (opens an account picker, then asks for a new session name) |
| `/export [path]` | Save conversation as markdown |
| `/login [force]` | Show sign-in status, or launch the Claude login flow. Automatically re-authenticates when the last turn failed with a 401; `/login force` re-authenticates even when it thinks you're already signed in (use this if you hit auth errors but it insists you're logged in). After you finish signing in the browser, it confirms the signed-in account by email and updates the status bar's **account** field, then prompts you to `/connect`. |
| `/logout` | Sign the active account out |
| `/connect` | Reconnect. If the browser↔server WebSocket has dropped (e.g. server restart), revives it client-side even after auto-reconnect has given up; if the socket is alive, reconnects the SDK bridge. Prompts typed while disconnected are queued and sent once reconnected. |
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
| `/model` | List available models and show current. The list is fetched live from the Anthropic API and refreshed hourly; if it can't be reached, a built-in fallback list is shown and labelled as such |
| `/model <name>` | Switch to a different model. Any model id works, whether or not it appears in the list |
| `/models` | Alias for `/model` |
| `/effort [level]` | Show or set effort (`auto`/`low`/`medium`/`high`/`max`) |
| `/thinking [on\|off]` | Toggle extended thinking |
| `/btw <question>` | Side question in separate context |
| `/graphify [path] [flags]` | Build a knowledge graph ([graphify](https://github.com/safishamsi/graphify)) |
| `/graphify explain <node>` | Explain a graph node + neighbors (runs directly, no LLM turn) |
| `/graphify path <A> <B>` | Shortest path between two graph nodes (quote names with spaces) |
| `/graphify diagnose` | Report multigraph edge-collapse risk in the graph |

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
| `/mcp` | List configured MCP servers with connection status and tool counts |
| `/mcp tools [server]` | List the tools each MCP server provides (optionally one server) |
| `/mcp reconnect <server>` | Reconnect a failed / disconnected server (also re-triggers auth) |
| `/mcp enable <server>` / `/mcp disable <server>` | Enable (reconnect) or disable (disconnect) a server |
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
