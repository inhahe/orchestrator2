# Multi-Session Hub — Implementation Plan

Turning orchestrator2 from a **single-session server** (one process = one Claude
session, chosen at startup) into a **multi-session hub**: a LAN-reachable server
that hosts several live sessions at once, shows a **lobby** to newly-connected
tabs, lets each tab pick a session to open, and keeps all tabs viewing the same
session in live sync.

## Confirmed design decisions

1. **Session source (lobby contents):** *live + openable disk.* The lobby lists
   sessions currently running live (top) plus recent on-disk sessions you can
   open. Opening a disk session spins up a live bridge for it. Plus a "New
   session" button.
2. **Concurrency:** *truly concurrent.* The server hosts several live
   `SDKBridge` instances at once. Tabs on session A sync together; session B
   runs independently.
3. **Idle lifecycle:** *keep running with an idle timeout.* A session with zero
   viewers keeps running for a while, then is torn down. The server itself stays
   up as long as it has any session or recent activity.

## Already true today (no work needed)

- **LAN access** — the server already binds `0.0.0.0` (uvicorn + splash
  pre-server). Reachable at `http://<host-ip>:<port>` (default **8420**,
  auto-selects a free port if taken). A Windows Firewall inbound allow-rule for
  the port may be needed.
- **Live multi-tab sync** — `broadcast()` already pushes every update to all
  connected tabs. The new work makes this **per-session** instead of
  server-wide.

## Scope / constraints

- **Single account per server.** Each `SDKBridge` inherits one
  `CLAUDE_CONFIG_DIR`; the lobby lists that account's sessions. Cross-account
  stays the job of `--copy`. Documented limitation.
- **Resume is cwd-scoped.** Each runtime must run in its own session's `cwd`, so
  every `SessionRuntime` carries its own `config` clone (cwd + resume), not the
  shared global.

## Target architecture

### `SessionRuntime` (new) — one per live session

- `rid` — internal id (a fresh session has no Claude `session_id` until its
  first result arrives; `rid` is stable from creation).
- `config` — clone of the base config with this session's `cwd` + `resume`.
- `state: State` — its own state object.
- `bridge: SDKBridge` — its own SDK connection + `worker_loop`, constructed with
  `broadcaster=runtime.broadcast`.
- `clients: set[WebSocket]` — only the tabs currently viewing *this* session.
- `ticker_task`, `idle_timer`, and metadata (title, cwd, created_at,
  last_activity, busy).
- `broadcast(msg)` → only this runtime's clients.

### Global registry (replaces the single `bridge`/`state`)

- `runtimes: dict[str, SessionRuntime]` keyed by `rid`.
- `lobby_clients: set[WebSocket]` — tabs at the lobby, not attached.
- `lobby_broadcast(msg)` → pushes the session list to lobby tabs.

## WebSocket protocol additions

- **On connect** → tab lands in the lobby; server sends `session_list`:
  - *running* sessions (rid, title, cwd, busy, #viewers, last activity)
  - *recent on-disk* sessions (id, title, cwd, mtime) for the current account
- **`attach {rid}`** → join a running session's client set; send its history +
  status (existing `_send_initial_state`, scoped to the runtime).
- **`open {session_id, cwd}`** → spin up a runtime from a disk session (clone
  config with that cwd + resume), then attach.
- **`new {cwd}`** → fresh empty session runtime, then attach.
- **`detach` / back-to-lobby** → return the tab to the lobby, resend
  `session_list`.
- All existing messages (`message`, `interrupt`, slash commands, queue ops)
  route to the ws's **attached** runtime instead of the global one.
- Lobby list re-broadcasts whenever a runtime is created/removed or its
  title/busy/activity changes.

## Lifecycle

- Runtime with zero viewers → start an **idle timer** (new
  `--session-idle-timeout`, default ~5 min); on expiry, disconnect its bridge
  and drop it from the registry, then update the lobby.
- Server auto-shutdown reworked: exit only when there are **no runtimes** and no
  tabs for the grace period (today's per-server "all tabs closed → shutdown"
  logic moves up a level).

## Startup

Keep the one-command workflow: if `--resume`/continue resolves a session,
pre-create one runtime for it so there's something to attach to; otherwise start
with an empty lobby + "New session". The `--resume`/`--copy` picker is unchanged
and just seeds that first runtime.

## Frontend (`static/`)

- New **lobby view**: cards for running sessions (title, cwd, viewer count, busy
  dot) + recent-disk list + a **New session** button.
- On attach, switch to the existing chat UI; add a **"Sessions"** back button to
  the toolbar.
- WS client handles `session_list`; sends `attach`/`open`/`new`/`detach`; tracks
  attachment state (one socket per tab, logical attachment — no reconnect
  needed).

---

## Phases (buildable + committable at each checkpoint)

### Phase 1 — Backend session registry (no visible behavior change)

- Add `SessionRuntime` class + `runtimes` registry + `lobby_clients`.
- Refactor `broadcast()` → per-runtime; add `lobby_broadcast()`.
- Thread the ws's attached runtime through `_handle_ws_message`,
  `_send_initial_state`, the ticker, and the queue/command handlers (replace the
  global `bridge`/`state` reads).
- **Compatibility shim:** auto-attach every connecting tab to a single default
  runtime (seeded from the existing startup resume/continue path), so the app
  behaves exactly as today while the plumbing changes underneath.
- Goal: identical UX, but the internals are now per-runtime. Commit.

### Phase 2 — Lobby protocol + multi-runtime

- Implement `session_list` / `attach` / `open` / `new` / `detach`.
- On-demand runtime spin-up from disk sessions (config clone with cwd + resume).
- Per-runtime tickers; idle-timeout teardown; registry lifecycle.
- Rework server auto-shutdown to the registry level.
- Live lobby updates on runtime create/remove/title/busy changes.
- Commit.

### Phase 3 — Frontend lobby UI

- Lobby screen: running-session cards + recent-disk list + "New session".
- Attach/detach flow; "Sessions" back button in the toolbar.
- WS wiring for the new message types; attachment-state tracking.
- Commit.

### Phase 4 — Polish + docs

- Live lobby refinements (viewer counts, activity, busy dots).
- Docs: README (lobby + LAN usage), roadmap entry, `CLAUDE.md` architecture
  notes for the new modules/flows.
- Windows Firewall `netsh` note for LAN access.
- Tests for registry lifecycle, attach/detach, idle teardown.
- Commit.

## Progress

- [x] Phase 1 — backend registry (default-runtime shim; behavior unchanged)
- [x] Phase 2 — lobby protocol + multi-runtime (backend); frontend still Phase 3
- [x] Central-hub reuse — a launch joins a running hub (same account/port) as a
  new session instead of starting a second server (`/api/whoami`,
  `/api/session/launch`, `?rid=` ws attach, `--standalone` opt-out). Extends
  Phase 2; frontend still opens the launched session via `?rid`.
- [x] Phase 3 — frontend lobby UI (lobby overlay: running cards + recent list +
  New session; "Sessions" toolbar button; WS wiring for `session_list` /
  `attached` receive and `list` / `attach` / `open` / `new` send; attachment
  tracked via the `attached` message. Overlay approach — the tab stays attached
  while browsing, so no blank-lobby state.)
- [x] Phase 4 — polish + docs (live lobby push via `lobby_watch`/`_lobby_watchers`
  so open overlays refresh in real time; `tests/test_multisession.py` covers
  registry lifecycle, idle teardown, lobby watchers; README LAN + Windows
  Firewall `netsh` note; `CLAUDE.md` hub architecture notes.)
