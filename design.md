# orchestrator2 — design

Living record of what orchestrator2 *is* and how it's put together. Update it in
the same change whenever a feature or the architecture moves.

Companion docs:

- `README.md` — user-facing: setup, CLI flags, slash commands, feature list.
- `known-issues.md` — unsolved bugs, tech debt, and a log of notable fixes.
- `roadmap.md` / `multisession-plan.md` — forward-looking notes.

---

## 1. What it is

A web front-end for the Claude Agent SDK. A single Python server (Starlette +
uvicorn) drives the same bundled `claude` CLI that Claude Code uses — sharing
its session store, login and config dir — and exposes it in a browser: live
chat, streaming tool calls, side-by-side diffs, a status bar, tool/background
panels, and a session lobby.

It is a **multi-session hub**: one server process hosts many concurrent
sessions, and any number of browser tabs (local or on the LAN) can attach to
any of them.

Run with `python server.py` from the directory you want Claude to work in.
Everything is in-process — no database, no build step; the frontend is plain
ES modules served straight from `static/`.

---

## 2. Process & runtime model

```
                        ┌──────────────── server.py (Starlette/uvicorn) ─────────────┐
                        │                                                             │
  browser tab ══WS══════╪══▶ websocket_endpoint ──▶ _handle_ws_message                │
  browser tab ══WS══════╪══▶      │                    ├─▶ immediate cmd (sync)       │
  browser tab ══WS══════╪══▶      │                    ├─▶ lobby msg (attach/open/…)  │
                        │         │                    └─▶ enqueue → bridge           │
                        │         ▼                                                   │
                        │   ws_channel.ClientChannel  (one writer task per socket)    │
                        │         ▲                                                   │
                        │         │ broadcast / send_to / lobby_broadcast             │
                        │   ┌─────┴──────────────────────────────────────────┐        │
                        │   │ runtimes: dict[rid, SessionRuntime]            │        │
                        │   │   each: config · state · SDKBridge · clients   │        │
                        │   └─────┬──────────────────────────────────────────┘        │
                        └─────────┼───────────────────────────────────────────────────┘
                                  ▼
                          SDKBridge.worker_loop  ──▶ ClaudeSDKClient ──▶ `claude` CLI
```

Three layers, deliberately separated:

1. **Transport** (`server.py`, `ws_channel.py`) — HTTP routes, WebSocket
   lifecycle, auth, fan-out. Knows nothing about turns.
2. **Session** (`session_runtime.py`, `state.py`, `config.py`) — one live
   conversation's identity, configuration and mutable state.
3. **Agent** (`sdk_bridge.py`, `tool_manager.py`, `commands.py`) — the turn
   loop, SDK message handling, and command semantics.

### Key invariants

- **All fan-out is non-blocking.** Nothing on the event loop ever `await`s a
  socket write directly (see §5). A wedged client cannot stall a turn, the
  receive loop, or another tab.
- **Broadcast is per-runtime.** `rt.broadcast()` reaches only the tabs viewing
  that session. Cross-session leakage is a bug.
- **State lives on the server.** The browser is a renderer. A tab can be closed
  and reopened at any point and gets a full replay (`history`) plus a status
  snapshot; nothing is lost.
- **Immediate commands are synchronous.** `/help`, `/cls`, `/status` etc. must
  return without touching the SDK or awaiting I/O, so they work mid-turn.
- **The SDK connection is owned by the worker task.** `bridge.connect()`,
  `disconnect()` and `reconnect()` may only be called *from* `worker_loop`'s
  task. Anything else — a route handler, a timer, an `asyncio.create_task` —
  must push an event onto `bridge.event_queue` (`("connect", "")` for a
  reconnect) and let the worker do it. This is not style: the SDK's subprocess
  transport enters an anyio task group inside whoever calls `connect()` and
  cancels its scope inside whoever calls `disconnect()`, and anyio delivers that
  cancellation to the task that *entered* the scope. A disconnect from any other
  task therefore **kills the worker**, silently — the SDK's `suppress(Exception)`
  eats the "different task" `RuntimeError` and the reconnect reports success.
  `SDKBridge._warn_if_foreign_task()` logs a stack if this is ever violated, and
  `_spawn_worker()` restarts a worker cancelled with `stop_event` clear. See
  known-issues.md, "A silently-cancelled worker made a session ignore every
  prompt". The one exemption is `_shutdown()`, which disconnects only after it
  has joined the worker.

---

## 3. Files

| File | Role |
|---|---|
| `server.py` | Starlette app, routes, WebSocket endpoint, startup/shutdown lifespan, hub registry (`runtimes`), lobby protocol, `_send_initial_state`, restart/shutdown, session picker |
| `session_runtime.py` | `SessionRuntime` — one live session: `rid`, `config`, `state`, `bridge`, `clients`, `meta()` for the lobby, `broadcast()`, idle timer |
| `ws_channel.py` | Per-socket outbound queue + writer task; snapshot coalescing |
| `sdk_bridge.py` | `SDKBridge`: SDK connection, `worker_loop`, `_message_dispatcher`, turn management, auto-continue |
| `state.py` | `State` dataclass + `state_to_status_dict()` / `state_to_panels_dict()` serialisers, rate-limit helpers |
| `config.py` | `Config` dataclass, `parse_args()`, constants (`CONTINUE_PROMPT`, `MARKERS`, …) |
| `commands.py` | `classify(line)` → `(kind, payload)`, immediate-command dispatch, completions |
| `session.py` | Session discovery, JSONL parsing, `render_session_history`, titles, trim, export |
| `tool_manager.py` | Tool-use / tool-result / thinking rendering helpers, registries |
| `theme.py` | CSS-variable theme system, `load_theme()` |
| `auth.py` | LAN-vs-external classification, external password, HTTP Basic + WS auth |
| `proc_guard.py` | Job object tying `claude.exe` children to the server's lifetime; duplicate-session detection |
| `copy_session.py` | Account/session discovery + the JSONL copy-and-rewrite. Imports nothing heavier than the stdlib on purpose — see §8 |
| `copy_session_tui.py` | The full-screen `textual` wizards (`--resume` / `--copy` picker, standalone copy wizard). The only module that needs `textual` |
| `static/` | Frontend ES modules — see §7 |
| `tests/` | Pytest suite (offline; no real SDK) |
| `tools/` | Diagnostics run by hand, not part of the suite: `measure_tab_cpu.py`, `probe_mutations.py`, `probe_animations.py`, `profile_idle_tab.py` (against a live hub; see §7 "An idle tab must cost nothing"), `probe_lobby_card.py` (renders the lobby's cards standalone and measures the title box — jsdom does no layout; §8), `probe_mobile_sidebar.py` (loads the real page at a phone viewport against a live hub and measures whether the panels are reachable; §8), `mutate.py` (the mutation sweep; §10), `agents.py` (cross-account agent CLI; §9a). The Playwright ones need the D: interpreter |

---

## 4. Session lifecycle

### Startup

`server.py` lifespan → `parse_args()` → `init_state_from_config()` → seed
`state.session_id` from `find_most_recent_session_for_cwd()` → build
`_default_runtime` → `SDKBridge.start()` → `worker_loop` as a background task.

**`fastapi`/`uvicorn` are imported inside `main()`, not at module top**, so a
`--detach` parent — or a launch that only joins a hub — never pays the ~1.5 s to
import a stack it will not serve with. The cost of that is a
`ModuleNotFoundError` arriving hundreds of lines into startup with nothing in it
to say *which* interpreter lacked the package, which is the whole question when
a Windows Python and a WSL `python3` can both run this file.
`_require_dependencies()` closes that: it `find_spec`s every required
distribution (locating without importing, so the lazy imports stay lazy) and
prints all the missing ones at once, `sys.executable`, and the exact
`pip install -r requirements.txt` line to run. It is called **after every path
that exits without serving and before the splash pre-server starts** — earlier
would reject hub joins that need none of those packages, later would leave a
splash listening on a bound socket with a traceback behind it.

### Central-hub reuse

A launch probes `/api/whoami`. If a hub for the **same account
(`CLAUDE_CONFIG_DIR`) and port** is already up, it POSTs `/api/session/launch`
and opens the browser at `/?rid=<rid>` instead of starting a second server.
`--standalone` opts out. If 8420 is held by something that isn't such a hub, it
binds an auto-picked free port.

**The port is the hub's identity, which is how you get a second one.** Since
the probe targets `config.port`, `--standalone --port N` starts an independent
hub on N and any later `--port N` launch *joins it* rather than the default
hub on 8420 — so a second hub with several sessions in it needs no new
mechanism, just a port. (From inside that hub's lobby, **+ New session** with a
working directory does the same thing without touching the command line.)

**Accounts are shareable across a hub; path namespaces are not.** Reuse hands
over nothing but absolute paths — `cwd`, `config_dir`, the resume id's project
directory — so it is only sound between processes that resolve a path to the
same place. `_path_namespace()` names that: `win32`, `wsl:<distro>`, or the bare
platform. WSL2 shares `localhost` with Windows, so without the check a
`server.py` run inside WSL joins the *Windows* hub and posts it
`cwd=/mnt/d/mypage/inhahe.com`, which `Path.resolve()` does not reject but
anchors to the current drive — inventing `D:\mnt\d\mypage\inhahe.com`. WSL
distributions are separated for the same reason: they share `/mnt/d` but not
`/home`. `_probe_hub` declines a foreign-namespace hub and the launch starts its
own server; a hub that reports no `namespace` predates the check and is still
joined, so an upgrade doesn't split same-OS relaunches onto a second port.

**`_create_runtime` refuses a working directory it cannot see**, raising
`NotADirectoryError` naming both the requested and resolved forms. It is the
single funnel for every way a session is born — the launch API and the lobby's
*open*/*new* — so it also covers a plain typo and the old-hub window above.
`/api/session/launch` turns that into `{"ok": false, "error": …}`, and the
launcher prints the reason before falling back to its own server.

### Connecting

`connect()` wraps the SDK handshake in `asyncio.wait_for(..., CONNECT_TIMEOUT)`.
**That ceiling is a backstop against a hang, not a performance budget**, and it
is sized from measurement: over 580 logged connects the handshake ran p50 5.3 s,
p90 20.4 s, p99 38.2 s, max 44.7 s — against what was then a 45 s limit, which
timed out 5.5% of attempts. Successes running continuously into the ceiling with
no gap is the signature of a cutoff slicing a live distribution, and the retries
confirmed it: every timed-out connect succeeded on the next attempt in ~11–22 s,
against a warm file cache after the first spawn had paid to read the bundled
`claude.exe` past Windows Defender. A short ceiling therefore made startup
*slower* — it bought a teardown, a backoff and a second spawn to reach a
handshake that was nearly done — and hub startup compounds it by spawning
several CLIs at once. `CONNECT_TIMEOUT` is **180 s** (~4.7× p99) — the floor of
the budget below; a genuinely hung handshake is still torn down, since the
alternative is a UI stuck on "connecting…" with no way out but a restart.
`connect()` logs the handshake cost on success as well as failure, so the
distribution stays observable.

**The 180 s was not the deadline that fired.** `ClaudeSDKClient.connect()` has
its own, smaller one: it waits on the `initialize` control request for
`max(int(os.environ.get("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT", "60000"))/1000, 60)`
seconds — read from *this process's* `os.environ`, never from `options.env` — so
out of the box the SDK abandons the handshake at 60 s and ours never runs. The
symptom is a session that retries forever with `Control request timeout:
initialize`, each failure a fixed 65.3 s after its spawn.
`_raise_sdk_initialize_ceiling()` sets that variable at import to just above
`CONNECT_TIMEOUT_MAX`, so it exists only to never be the deadline that fires. An
already-larger value is left alone.

**The budget is sized from the session file, because the handshake is.** A
resume makes the CLI re-read and re-parse the whole JSONL, so the cost is
O(size) — measured at ~115 s/GB: 348 MB → ~58 s, 452 MB → ~58 s, 1293 MB →
**148 s**. Against a 60 s ceiling that last one was not slow, it was impossible,
and no number of retries could have changed that. `connect_timeout_for(bytes)`
returns `CONNECT_TIMEOUT + 360 s/GB`, capped at `CONNECT_TIMEOUT_MAX` (1800 s) —
~3× the measured slope, because the downside is asymmetric (too long costs a
slow failure, too short costs the session entirely). A fresh session, or one
whose file can't be found, gets the flat 180 s: nothing to re-read, nothing to
wait for. The size comes from `_resume_jsonl_size()`, which resolves through
`find_session_dir(sid, config_dir)` — **account-scoped**, since in a hub the
three sessions sharing a cwd routinely live under three different
`.claude-account-*` trees, and searching the hub's own account would hand the
largest session the smallest budget.

**Large resumes are serialised.** Two whole-file reads at once don't go twice as
fast; they halve each other and push both towards their deadline. In the report
that prompted this, all three CLIs spawned within 0.1 s and the two that
finished landed at 58.5 s and 58.3 s — near-identical, and both a hair under the
ceiling they were racing. A process-wide `asyncio.Lock` (created lazily, so it
binds to the running loop) is taken only by sessions at or above
`HEAVY_CONNECT_BYTES` (128 MB); the disk does the same total work either way, so
queueing strictly helps and small connects stay concurrent. The connect clock
starts *after* the lock — charging queue time to the budget would time sessions
out for being second — while `state.connect_started_at` is set *before* it, so
a queued session's "connecting (Ns)" timer still runs rather than looking frozen.

Connect failures name their cause. `str(asyncio.TimeoutError())` is the empty
string, so interpolating the exception produced "SDK connection failed" with
nothing after the colon; `worker_loop` computes a `_why` (timeout + duration, or
the exception's class name when it too has no text) and threads it through all
three user-visible messages. The duration reported is the budget that actually
applied, not the constant — "timed out after 180 s" on a session granted 613 s
would send the user looking for a deadline that never elapsed.
`tests/test_connect_timeout.py`.

**Not every connect failure is transient, and the loop's optimism is expensive
when it's wrong.** Ten attempts with backoff, then — if the user has queued a
prompt — retry forever, which is precisely what a user does when a session won't
start. So failures that a retry cannot cure short-circuit both: `connect()`
raises `UnusableCwd` before spawning anything when `config.cwd` isn't a
directory, and `_fatal` skips straight to the give-up branch with a message
naming the path and the one action that helps. Without this a missing directory
respawned `claude.exe` every 30 s for nine minutes, reporting only "[WinError
267] The directory name is invalid" — an error that doesn't contain the
directory, because the SDK never sees a name it could print.
`tests/test_cwd_namespace.py`.

### Tab attach

`_attach_ws(ws, rt)` detaches from any previous runtime (arming that one's idle
timer if it's now viewer-less), binds the socket, then `_send_initial_state()`
sends, in order: `attached` → `status_update` → `completion_list` →
`clear_screen` → `history`.

### Teardown

A runtime with zero viewers is torn down after `--session-idle-timeout`
(default 300 s; `0` disables). `_default_runtime` is exempt — the hub always
keeps its primary session.

**"Zero viewers" is a proxy for "nobody cares about this session", and it was
wrong in two directions.** Both fixed 2026-09-14; `tests/test_idle_teardown.py`
(22) and the `idle` target (`server.py` × 12) pin them.

*A session that is working is not idle.* The timer checked `rt.clients` and
nothing else, so a turn started and then left to run — close the tab, lock the
phone — was killed mid-edit five minutes later, taking its background tasks
with it. That is the opposite of what a server-side agent is for: you close the
tab *because* it keeps working. `_idle_teardown_after` now re-arms instead of
tearing down when `state.busy` or `background_tasks` is non-empty, and reaps on
the first pass after the session goes quiet. Deliberately unbounded, unlike the
wakeup deferral (§6): a wakeup that keeps landing mid-turn has already failed
at its job, whereas a session that keeps working is succeeding at its, and a
genuinely wedged one is still closable from the lobby.

*A sleeping phone is not a viewer leaving.* Phones suspend their browser within
a minute or two of the screen locking, well inside the 5-minute default, so the
session its only viewer is still using was reaped — and `app.js` reconnects on
`visibilitychange`, so the user unlocks the phone and watches the tab reconnect
to nothing. The socket's `User-Agent` is read **at accept** into `_mobile_ws`
(not asked of the page: when the socket dies the page is already suspended),
and `_maybe_start_idle_timer` takes its timeout from the *last viewer to leave*
— `--mobile-idle-timeout`, default **0 = never**. `_MOBILE_UA_RE` mirrors
`lobby.js`'s `_isMobile()` regex deliberately; the two decide the same thing
about the same client and disagreeing would be worse than either being wrong.
Shared blind spot: iPadOS Safari reports a desktop UA, so an iPad is treated as
a desktop.

Known limitation, stated rather than papered over: a **laptop** suspending has
the same shape and is not covered. Generalising to "any abnormal close" would
catch it, but 1006 is common enough (a crashed tab, flaky wifi) that almost
everything would become exempt — so the narrower, stable signal was chosen.

The runtime remembers whether its countdown is the mobile one (`idle_mobile`),
so a deferral re-arms with the grace it was armed with rather than quietly
dropping to the short clock.

`_teardown_runtime` pops the runtime from `runtimes`, cancels its idle timer,
then `await rt.bridge.stop()` — and that last step is the one that actually
kills `claude.exe`, so everything here exists to guarantee it runs.

**`_cancel_idle_timer` never cancels the calling task.** `rt.idle_timer` *is*
the task running `_idle_teardown_after` → `_teardown_runtime` → the cancel, so
an unconditional `timer.cancel()` fires a `CancelledError` into the teardown it
is part of, landing at the next await: `bridge.stop()`. It therefore skipped
the shutdown entirely and left the CLI alive — while the runtime had *already*
been popped, so nothing listed the orphan and the launch-time duplicate check
could not see it, and relaunching the session started a second agent on the
same conversation. `CancelledError` is not an `Exception`, so the handler in
`_teardown_runtime` never saw it and a task that ends *cancelled* raises no
asyncio warning: the leak was completely silent for three weeks (19 live
subprocesses; see `known-issues.md`). The fix compares against
`asyncio.current_task()`.

Three guarantees now stand behind that, so no future variant can cost a
subprocess:

- **`SDKBridge.stop()` is shielded** (`asyncio.shield(self._shutdown())`). A
  cancellation aimed at the caller is re-raised to it, but the teardown
  continues on its own task. A `finally` alone would survive only the *first*
  cancel; a caller that retries lands the second one inside `disconnect()`.
- **`disconnect()` is unconditional and verifies the kill.** It runs in a
  `finally` around the worker join (a wedged worker must not also cost the
  subprocess), and it snapshots the CLI's pid + `create_time` *before*
  disconnecting, then reaps it if it is still alive rather than trusting the
  SDK. An SDK failure is logged, not swallowed.
- **`cancel_and_join` is bounded** by `CANCEL_JOIN_TIMEOUT` (10 s) and on
  expiry logs the stuck task's own stack via `traceback.format_stack(
  task.get_coro().cr_frame)` — a parked coroutine appears in no thread dump, so
  `py-spy` shows only an idle loop.

Covered by `tests/test_bridge_shutdown.py` (real child processes, not a mocked
`psutil` — a fake that reports death cannot fail the way production did) and
`test_multisession.py::test_teardown_from_its_own_idle_timer_still_stops_the_bridge`.

---

## 5. Fan-out: `ws_channel`

Every socket gets a `ClientChannel` holding a `deque` and a dedicated writer
task. `send()` enqueues and returns immediately; only the writer task awaits
`ws.send_text`.

**Snapshot coalescing.** `status_update`, `panel_update`, `queue_update` and
`session_list` are full-state snapshots, so a newer one supersedes any still
queued: `send()` replaces the pending message *in place* (preserving relative
ordering) rather than appending. A slow client therefore never accumulates a
stale backlog of status frames ahead of real content.

Content messages (`assistant_text`, `tool_use`, `history`, …) are never
coalesced — they're append-only and ordering matters.

Shutdown calls `ws_channel.drain_all(timeout)` so the goodbye actually lands.

*Why:* before this, fan-out `await`ed each socket inline. One phone on flaky
Wi-Fi applying TCP backpressure would stall the whole event loop, which is what
made `/help` and `/cls` appear to hang for minutes mid-turn.

---

## 6. Turn flow (`sdk_bridge.py`)

`worker_loop` pulls from the event queue and runs turns. `_message_dispatcher`
receives SDK messages and routes them either into the in-turn queue or, between
turns, to the async handler (background-task completions, notifications).

Message types handled: `SystemMessage` (init, api_retry, task_notification,
compact_boundary), `AssistantMessage` (`TextBlock`, `ToolUseBlock`,
`ThinkingBlock`), `ResultMessage`, `UserMessage` (replay).

**The worker task is supervised (`_spawn_worker`).** `start()` doesn't create
the task directly; `_spawn_worker(skip_connect=…)` does, and it treats a
`CancelledError` arriving while `stop_event` is *clear* as a fault rather than a
shutdown: it logs an error with a stack, clears `busy`/`connecting`, and creates
a **replacement task**. It has to be a new task — anyio re-delivers a scope's
cancellation via `call_soon` to every task still inside it, so a worker that
merely swallowed the cancellation would be re-cancelled at its next `await`
forever. The replacement is passed `skip_connect=self.client is not None`, which
`worker_loop` uses to skip both the connect phase (a second `claude.exe` on one
session is exactly what `proc_guard` exists to prevent) and
`config.initial_prompt` (already delivered). This exists because a worker that
dies has no symptom: the runtime, state, bridge, sockets and status bar all stay
healthy while prompts pile up unread in `event_queue`.

**A dead CLI subprocess is recovered from unconditionally.** When `claude.exe`
exits, `bridge.client` points at a corpse: every later prompt in that session
fails identically forever, and between failures the status bar reads an ordinary
"idle", so nothing on screen distinguishes a healthy session from a dead one.
That is not a preference, so it is not gated on `--auto-reconnect` (which
defaults off and now means only "reconnect after *any* failed turn", its
original "auto-continue" half having been removed in 2026-08-14).

Two detectors, because the CLI can die at either end of the pipe and which one
fires depends only on what we were doing:

| | Where | Fires when |
|---|---|---|
| read side | `_message_dispatcher` — `receive_messages()` raises with the exit code | any time, **including while parked** |
| write side | `run_turn` — `client.query()` raises `CLIConnectionError` synchronously | only on the next prompt |

Both funnel into `_note_transport_death()`, which sets `_transport_dead` and —
if no turn is live — pushes `("connect", "transport-dead")` onto `event_queue`.
It cannot reconnect itself: the dispatcher is not the worker task, and the SDK
transport's anyio scope belongs to whoever entered it (see §2). The read-side
detector is the important one; it is the only signal available while the session
is idle, and discarding it is what let a session sit dead for **5h21m** and
survive a restart (`known-issues.md`).

`_recover_dead_transport()` does the reconnect on the worker task, announces
both the attempt and the outcome, and is **bounded** by
`TRANSPORT_DEATH_MAX_RECOVERIES` (3) within `TRANSPORT_DEATH_WINDOW` (600 s) —
a CLI that aborts on startup would otherwise be respawned forever, which is
worse than the zombie because it burns the machine too. On giving up it names
`/connect` as the way back, and `/connect` clears the budget. `_transport_dead`
is cleared by a *successful* `connect()` only, so a failed reconnect leaves the
session still known-dead rather than restarting the budget on the next crash.
`_is_transport_death()` matches on the message text, not just
`CLIConnectionError`, so an ordinary turn error never costs a healthy CLI.
`tests/test_dead_cli_recovery.py`.

**The dying CLI's stderr is drained before the transport is closed.** Why
`claude.exe` aborts is only ever explained on its stderr — the exit code is
Bun's panic handler, and the panic report carries a `bun.report` URL that
decodes to a stack trace. The SDK loses that report by default: it raises
`ProcessError` as soon as stdout closes and `wait()` returns non-zero (with the
placeholder "Check stderr output for details", *without* draining stderr), and
`close()` then cancels the stderr reader's task group. Recovering promptly makes
this strictly worse, since it calls `close()` sooner.

So `_on_sdk_stderr` keeps a `STDERR_TAIL_LINES`-deep ring buffer with an arrival
timestamp, and `_recover_dead_transport()` awaits `_drain_stderr()` and logs
`_log_stderr_tail()` **before** anything that could close the transport —
including the give-up path, which is the run of crashes most worth explaining.
The drain ends after `STDERR_DRAIN_QUIET` with no new line, restarting that
window per line so a multi-line report isn't truncated, and is capped by
`STDERR_DRAIN_MAX` so evidence-gathering can never postpone the reconnect. The
quiet period is measured from the later of the last line and the drain's start,
so an *empty* tail still waits one window: an empty tail is the lost-report case
itself (the dump sitting unread in the pipe), not proof of a silent CLI.
`connect()` clears the buffer so a fresh subprocess is never logged with its
predecessor's dying words.

**`run_turn`'s `try` opens *before* the session is marked working.** Everything
that claims the session — `state.busy = True`, `state.turn_started_at`,
`turn_active.set()`, the `turn_start` broadcast — and the `client.query()` that
follows must all sit inside the `try`, because the `finally` is the only thing
that guarantees they are undone. The rule is not academic: those statements used
to sit one line *above* the `try`, and `client.query()` on a dead CLI raises
`CLIConnectionError` **synchronously on the caller's stack**
(`subprocess_cli.write()`), so the single failure mode that occurs before the
turn reads a message was the single failure mode the guard missed. A session hit
by it read "working" for 16 hours, and — worse — kept `turn_active` set, so
`_message_dispatcher` filed every later SDK message into a `turn_msg_queue` with
no reader. Cleanup must not become suppression either: the `finally` re-raises,
because `worker_loop` needs the exception to show "Turn failed: …" and to
reconnect. The `finally` also captures `state.turn_started_at` into `_started_at`
*before* clearing it — both synthetic `turn_end` markers it can emit need the
start time, and reading it afterwards made every abnormal turn report `0:00`.
`tests/test_turn_start_failure.py`.

**Auto-continue — removed (2026-08-14).** The web orchestrator has no
auto-continue loop. Every completed turn returns control to the user in
`_between_turns`:

- Background tasks running → park in `_await_next_prompt()` until they drain.
- Otherwise → ring the `turn-done` bell and wait for the user.

It had been dead long before removal: the loop sat behind a `config.auto_continue`
flag that defaulted to `False` and that no flag, command, or assignment ever set,
and this codebase never had a `[WAITING]` / `[DONE]` sentinel scan or burst
counter at all. Deleted with it: `CONTINUE_PROMPT`, `WAITING_SENTINEL` /
`DONE_SENTINEL`, the `continue_response_delay` / `continue_burst_*` knobs,
`state.continue_prompt`, `_is_auto_turn`, and the `"waiting"` / `"done"` /
`"burst"` status-bar branches. `needs_user_attention` is now only ever `None` or
`"api-error"`.

**The bell, and being able to attribute one.** `ring_bell()` records
`state.pending_bell` only if the event is in `state.bell_events` (from
`--bell`); `_flush_bell()` broadcasts it as a `bell` WS message, which every
tab attached to that runtime plays. Ring sites: `turn-done` (a turn that
completed — *not* an interrupted one, which returns early and rings
`interrupt`), `bg-done` (see below), `requires-action`, `interrupt`,
and `rate-hit` (edge-triggered on the status *transitioning* to `rejected`).

**`bg-done` is deferred, not immediate.** `in_bg_wait()` (not `busy`, not
`connecting`, not rate-limited) is necessary but not sufficient: the CLI feeds a
finished background task's `<task-notification>` straight to the model, which
resumes streaming seconds later, so the completion instant is exactly when a
working session *looks* parked. Measured: all nine `bg-done` rings in the log
were followed by a `ghost turn begin` in the same session 2.3–5.0 s later.
`_announce_bg_completion` therefore calls `_arm_bg_done_bell()`, which starts a
`BG_DONE_BELL_GRACE` (10 s) timer and rings only if the session is *still* in
`in_bg_wait()` when it fires. `_cancel_bg_done_bell()` withdraws the ring
wherever `busy` flips True — `run_turn` and `_begin_ghost_turn_if_needed` — so a
resumption that both starts *and* ends inside the grace still silences it. One
pending timer at a time, so a burst of completions is one bell.

Both the ring and the flush log — including *suppressed* rings — with the
event, session id and title. Before that, a bell left no trace anywhere, and in
a hub hosting several sessions a bell from a background tab is indistinguishable
by ear from one for the session you're looking at, so "why did it just ring?"
could not be answered even in principle. Grep `bell:` in the log.

The `bell` message carries `session_id`/`session_title` for the same reason.
And `--bell` is forwarded in the `/api/session/launch` payload: a launch that
finds a hub already running never builds its own `Config`, so **any flag not in
that payload is silently dropped** and the session inherits the hub process's
value. `bell_on` joins `model`/`effort`/`config_dir` there, and is applied to an
already-live runtime too (no reconnect needed — `bell_events` is read at ring
time).

One piece was deliberately **kept**: `session.py`'s prefix match on the old
prompt text ("If you need input from me before continuing…"). History replay
still needs it — old transcripts, and any from the single-file
`Python Agent/orchestrator.py` which does implement auto-continue, contain that
text and would otherwise render as ordinary user bubbles. See `known-issues.md`.

**The pending-prompt queue.** A prompt typed while `state.busy` (or
`connecting`) is appended to `state.queued_prompts` by `server.py` instead of
going on the event queue, so it shows up in the queue panel. The bridge drains
it at four points — `_between_turns`, the interrupt branch, and the `wakeup`
and config-command branches of `_await_next_prompt` — and **all four go
through `_pop_queued_prompt()`**. Doing it by hand at each site is what caused
the "it sent the prompt but it was still in the queue" bug: the panel renders
from a *snapshot* (`queue_update`, or the `panels` blob on `status_update`), so
a pop that doesn't publish a new snapshot leaves the row on screen until the
2 s status ticker overwrites it. The helper does the three things that must
always happen together: dequeue, echo the prompt to the transcript, publish a
fresh `queue_update`. Anything mutating `queued_prompts` from the bridge owes
the frontend a snapshot; the server-side REST endpoints have their own twin,
`_broadcast_queue_update`.

**Attaching to a session costs a bounded amount, however long the session is.**
Three things used to make opening a large session take ~42 s to show anything:

- `_tail_read_jsonl` derived its average line size *from the file size*, which
  made the "tail" exactly half the file at any size (724 MB of a 1.4 GB
  transcript). The estimate is now a lower bound capped by `MAX_TAIL_BYTES`
  (32 MB); measured 1448 MB -> 3.0 s where the *smallest* of the three files
  previously took 4.9 s.
- the history read raced the CLI's own resume read of the same file. The CLI
  reads already queue against each other; the history read now joins that same
  queue, instead of adding a second unserialised reader per session (a
  three-session launch had ~4 GB in flight).
- rendering is the other half of the wait, so the newest `HISTORY_FIRST_SLICE`
  messages are sent as `history` and the remainder as `history_prepend`, which
  `chat.js` renders into a detached container and inserts above, re-anchoring
  `scrollTop` by exactly the height added.

The cost of that boundedness: a `TodoWrite` further back than the cap is not
found, so a very stale plan seeds nothing rather than seeding something wrong.

**The plan panel is seeded from the transcript on attach.**
`state.current_todos` is otherwise written only by a *live* `TodoWrite`, so a
restarted session showed a blank panel while the CLI — which rebuilds its own
todo state from the same transcript — still knew the plan.
`last_todos_from_records()` returns the list as of the most recent `TodoWrite`,
handed back as a fourth element of `render_session_history()` so it rides on
the read that already happens (600 MB transcripts take ~40 s; a second pass for
a side panel is not worth it). Applied only when `current_todos` is empty, so a
live write is never clobbered.

`TodoWrite` is the **only** writer and each call is a full replace — every item
carries its own `pending`/`in_progress`/`completed` status, and the panel's
strikethrough *is* that status. There is no separate completion tool
(`TaskOutput`/`TaskStop` are the background-task system), so the last write is
authoritative and the list is seeded verbatim; `/api/todos/clear` already
exists for dropping finished items.

**A prompt echo skips one viewer, never the broadcast.** The frontend renders a
prompt optimistically when it isn't busy at send time and reports that as
`client_echoed`; `_enqueue_prompt` turns it into `echoed_by=<socket>` and
passes it to `rt.broadcast(..., exclude=…)`. It used to be a boolean that
suppressed the send outright, which read "the sender drew it" as "everyone has
it": with a session open in two tabs the prompt appeared only where it was
typed, while both tabs showed the reply to it. The boolean and the socket are
deliberately **one** parameter — "echoed, but by whom?" is the state that made
the bug expressible, so it is not representable.

**The queue is persisted, and it belongs to a *session*, not to a directory.**
`_attach_queue_persistence()` mirrors `queued_prompts` to
`<config-dir>/orchestrator2/queues/<cwd>__<session-id>.json` so typed-but-not-yet-run
prompts survive a server restart. The subtlety that made this dangerous is that
a restored prompt is **not merely shown — it is sent**: `connect()` finishes by
calling `_poke_for_queued_prompt()`, so the worker pops it and runs it as a real
turn with no user action. Handing that to the wrong session means silently
re-running old work, which is exactly what happened when the file was keyed by
cwd alone and restored unconditionally (see `known-issues.md`, "A new session
inherited an old session's queued prompt"). Three rules now hold:

- **Keyed by session id**, so two sessions open on the same directory — routine,
  the hub hosts several — cannot overwrite each other's queue.
- **Restored only into that same session**: the id in the filename *and* the id
  recorded inside must match, and the queue must be under `QUEUE_MAX_AGE_S`
  (24 h). A queue exists to survive a restart, not a fortnight.
- **No session id, no queue — enforced in the loader, not at the call sites.**
  `load_persisted_queue()` returns nothing for `session_id is None`, and
  `save_persisted_queue()` writes nothing. A `--no-continue` session has no id
  by definition, so it cannot inherit anything, and it cannot leave a
  directory-shaped crumb for the next one either.

  The call sites therefore need no flag; they need only run **after** the
  session id is seeded, which is the ordering the original bug got wrong and
  which `test_every_call_site_seeds_the_session_first` pins textually. An
  earlier draft of this fix did add a `restore=` gate to each caller — the
  mutation sweep showed the safety did not depend on it, so it was removed
  rather than kept as unverified ceremony (§10's rule: a surviving mutation is
  a fact about the code).

Saving is wired for every session including a fresh one, and reads
`st.session_id` at save time, so a session starts persisting the moment it has
an identity to restore into.

**There is exactly one idle wait, and a config command must drain the queue
before returning to it.** `/model`, `/effort`, `/thinking`, `/connect` and
`/clear` all take effect by reconnecting (they're fixed at connect time), and
`connect()` holds `state.connecting` True for the seconds that takes — which is
*precisely* the condition that makes `server.py` route a typed prompt into
`queued_prompts`. So a prompt typed during a model switch is always queued, and
an idle wait that applies the command and goes straight back to blocking on
`event_queue.get()` strands it: idle status bar, prompt visible in the panel,
nothing left to poke the worker. Hence the drain in `_apply_idle_config_command`'s
caller. Checking after the await is race-free — `connect()` clears `connecting`
in a `finally` before `reconnect()` returns, so a prompt is either already
queued (and popped) or routes to the event queue we're about to block on.

`worker_loop` used to hand-roll a *second* idle wait for the first prompt, and
every divergence from `_await_next_prompt` was a latent bug that fired only when
the user happened to be parked at that one: it dropped `/compact`, `/btw` and
wakeups outright, and had this same stranding hole. It now calls
`_await_next_prompt()`, so idle behaviour can't depend on which wait point you
are standing in. The between-turns path was never affected — `_between_turns`
reconnects at the top and drains further down — which is why the same keystrokes
worked or didn't purely on whether a turn happened to be ending.

**Transcript echo happens at enqueue time.** Every prompt that runs has to show
up as a "you:" message, and exactly one producer echoes on its own: the
browser's optimistic echo in `app.js send()`, which fires only when it believes
the session is idle and reports which way it went via `client_echoed` on the
wire. Everything else that starts a turn — the queue panel's send arrow
(`/api/queue/send`), `/queue send`, `/graphify`, any command forwarding a
payload — is a REST call or a server-side synthesis that echoes nowhere. So
`server.py`'s **`_enqueue_prompt(rt, prompt, *, client_echoed)`** is the single
choke point: it echoes unless the client already did, then puts the message on
the event queue. Prompts that go via `queued_prompts` instead are echoed by
`_pop_queued_prompt` (above); between them every path is covered exactly once.

Echoing at *enqueue* rather than where the bridge consumes the prompt is
deliberate: the event queue carries plain `(kind, payload)` tuples with nowhere
to hang an echo flag. The old design used a single `State` slot
(`initial_prompt_client_echoed`) as a stand-in, which only ever described the
last message the WebSocket handler saw — wrong whenever two prompts were in
flight, and never set at all by the REST producers. That field is gone.

**An interrupt drains the queue.** Ctrl-C followed by a queued prompt is the
"stop — do this instead" gesture, so `_between_turns` sends it rather than
parking. This used to depend on a race: `interrupted` is only True when
`run_turn` spots the interrupt before the SDK's `ResultMessage`, and on a short
turn the `ResultMessage` wins — so the same keypress either drained the queue
(via the normal path) or stranded it forever (nothing pokes the worker after an
interrupt). The interrupt branch still returns immediately after the drain: an
interrupted turn must never reach the auto-continue or compaction logic. Note
the decision order inside `_between_turns` — `interrupted` is checked well
before `if state.background_tasks:` (the bg-wait park), so background work never
holds a queued prompt back.

`interrupt()` has two mutually exclusive pokes, chosen on `turn_active`:

- **Live turn** (`turn_active` set, i.e. inside `run_turn`) → `INTERRUPT_SENTINEL`
  on `turn_msg_queue`, so the loop ticks even when the SDK isn't streaming.
- **Parked** (idle or bg-wait) → `("wakeup", "interrupt")` on the event queue.
  `turn_active` is set *only* by `run_turn`, so while a **ghost turn** streams
  (a background task producing output) `state.busy` is True — server.py routes
  typed prompts into `queued_prompts` — yet the worker is parked and
  `_between_turns` never runs. Without the wakeup, Ctrl-C there did nothing at
  all. The wakeup branch drains the queue; with an empty queue it falls through
  to the auto-resume check, which ignores an `"interrupt"` payload, so it's a
  no-op rather than a spontaneous turn.

**…but the next turn must wait for the wind-down.** Interrupting a *ghost* turn
creates a hazard the live-turn path doesn't have: the CLI answers
`client.interrupt()` with a `[Request interrupted by user]` UserMessage and a
terminating `ResultMessage`, and with no `run_turn` running there is nobody to
absorb them. The parked-worker poke makes the worker drain and start a turn in
a millisecond or two, so that terminator lands *inside the new turn* — which
ends ~40 ms after starting, on a result that was never its own. The prompt is
consumed, the session goes idle, and the user has to send it again.

So `interrupt()` clears `_interrupt_settled` when — and only when — it
interrupts a stream nobody is consuming (`state.busy` and not `turn_active`),
issues `client.interrupt()` *before* poking the worker, and `run_turn` waits on
that event (5 s cap) **before setting `turn_active`**. Ordering is the whole
point: while it waits, the dispatcher still routes to `_handle_async_message`,
which closes the ghost turn properly and sets the event. Waiting also means the
prompt only ever reaches a quiesced CLI — swallowing the stray result inside the
message loop instead would leave us guessing whether the `query()` we'd already
sent had been discarded along with the interrupt. Interrupting an *idle*
session doesn't clear the event (no stream, no terminator, so waiting would
stall every later turn), and `run_turn`'s `finally` re-sets it unconditionally
so a stale clear can never outlive a turn.

**Output continuing after an interrupt is a background task, not a runaway —
and we render it.** From a report: *"often when i hit ctrl-c to interrupt a
turn, it says it's interrupted but then it keeps working and outputting text,
and i have to hit ctrl-c once or maybe twice more to get it to stop."* The log
shows `run_turn` doing everything right — it drained the wind-down and exited on
the CLI's terminating `ResultMessage` at `23:05:11,153` — and then the SDK
streaming again at `23:05:22,033` (a `ThinkingBlock`) with no `query()` from us
in between, which `_begin_ghost_turn_if_needed()` rendered as a ghost turn.

The session JSONL identifies the cause exactly: a **background Bash task
completed at `23:05:15`** and enqueued a `<task-notification>`, which the CLI
fed to the model. The mechanism is in the CLI source — `drainCommandQueue`
creates its `abortController` **per dequeued command** (`print.ts:2133`), and
the SDK `interrupt` control request only calls `abortController.abort()`; it
never clears the command queue, so anything dequeued afterwards runs with a
fresh, un-aborted controller. `print.ts:2010-2013` states that feeding
notifications to the model this way *"matches TUI behavior where
useQueueProcessor always feeds notifications to the model regardless of
coordinator mode"* — so **interactive Claude Code does the same thing**.

Consequences for us:

- **The delay is the task's own runtime**, not interrupt latency. Across 20
  logged interrupts, 3 were followed by more output, at 2.1 s / 8.5 s / 10.9 s
  — each matching when its background task finished.
- **We must not suppress it.** The CLI writes that output to the session JSONL
  either way, so hiding it live would make the transcript disagree with itself
  on reload — a background task's result would silently vanish and then
  reappear. `_begin_ghost_turn_if_needed()` therefore returns `False` only when
  a live `run_turn` already owns the stream.
- **We must not auto-re-assert the interrupt.** Firing extra control requests
  at a legitimately woken model would cancel work the user can see, and the
  next dequeued command would get a fresh controller anyway.

What the SDK genuinely lacks is interactive mode's *additional* gestures: Esc
pops queued commands, and `ctrl+x ctrl+k` (`handleKillAgents`) calls
`clearCommandQueue()` and kills running agents. A deliberate "stop background
tasks too" control would be the correct way to give the user that, rather than
inferring it from an ordinary interrupt.

**A completed background task must always be visible — `_announce_bg_completion()`.**
The reason the wake-up above looks causeless is that the row explaining it can
go missing. `task_notification` and `task_updated` both route through
`_announce_bg_completion()`, which broadcasts `bg_complete` (rendered as a
"Background task completed" row by `chat.js:_addBgComplete`). Both call sites
used to be `if entry: <broadcast>` with no `else`, and `complete_bg_task()`
returns `None` for **two different reasons**: a duplicate (the same completion
arriving as both a notification and a patch) *and* a task that was never
registered. The second happens for real — the CLI keeps background tasks
running across a `--continue`/`--resume`, so a bridge that attached afterwards
never received their `task_started`, and their completion was dropped with no
broadcast and no log line at all.

So the helper distinguishes them: a duplicate stays silent (and logs that it
did), while an unregistered task is rendered from whatever the notification
itself carries (`task_id`, `status`, `summary`, `output`; `name`/`seq`/`command`
are `None`). The bg-wait bell and the `bg-all-done` wakeup stay gated on a
*known* entry — an unregistered task was never in `background_tasks`, so "no
tasks left" would be vacuously true and would inject a wakeup prompt for work
nobody was tracking. `task_started`, `task_notification` and `task_updated` now
all log, so a completion with no matching start line identifies this case
directly.

**The server no longer announces an interrupt it can't vouch for.** The other
half of *"it says it's interrupted but then it keeps working"* was literal:
`server.py` broadcast `"Turn interrupted."` the instant Ctrl-C arrived, before
the control request had even been sent. For a live turn `run_turn` already owns
the reporting — `"Interrupting…"` when it notices, then the visible
`turn_end` *interrupted* marker deferred to the point the CLI actually stops —
so `_do_interrupt()` stays quiet there and speaks only when no `run_turn` will
(a ghost turn, or an idle session), and says `"Interrupting…"` rather than
claiming it's already done. It samples `turn_active` *before* awaiting
`bridge.interrupt()`, since `run_turn` can exit during that await.

**A compact-closed turn's `turn_end` is deferred — and must be flushed before
the next prompt is echoed.** When a turn ends because the CLI auto-compacted,
`run_turn` doesn't broadcast `turn_end`; it parks it in
`state.pending_compact_turn_end` and arms a 10 s timer, because a post-compact
*ghost turn* may continue the same logical turn and should own the marker
(`_claim_pending_compact_turn_end`). Three things can emit it: that ghost-turn
claim, the timer, or `run_turn`'s own flush at the **start** of the next turn.

That third backstop is the one that reads wrong. By the time the next turn
starts, its prompt has already been echoed, so the transcript shows
`you: …` followed by `Turn 102 completed (0:26:41) — success` — the previous
turn appearing to finish after the prompt that follows it. So both echo sites —
`_pop_queued_prompt()` and `server.py`'s `_enqueue_prompt()` — call
**`SDKBridge.flush_pending_turn_end()`** first. It's a pure reordering:
`run_turn` would have flushed the same marker milliseconds later, and flushing
with nothing pending is a no-op. A *held-back* pop (the queue head is being
edited) doesn't flush — nothing is being echoed, so there's nothing to get ahead
of, and the ghost-turn claim keeps its chance.

Queued prompts hit this every time (they start the next turn well inside the
10 s grace, so they always lose the race to it); hand-typed ones only when the
user types within 10 s, which is why it looked intermittent. For a prompt the
browser echoed optimistically the server still flushes, but can't get ahead of
an echo that already happened client-side.

**The pending-prompt queue pokes the worker — from the container, not the
callers.** A prompt typed while `state.busy` or `state.connecting` is routed by
`server.py` to `state.queued_prompts` (the visible queue panel) instead of the
worker's `event_queue`. The worker, meanwhile, is parked on
`event_queue.get()`. Nothing connects the two but hand-placed drain
checkpoints — and *every* "I typed a prompt and it just sat there" report in
this project's history has been the same shape: a transition that queues a
prompt and then ends without reaching one.

The instances, in order: a prompt typed during a `/model` reconnect (fixed by
draining in `_await_next_prompt` after a config command); a prompt typed during
a ghost turn that ended on `bg-all-done` rather than `queue-edit-done` (fixed by
draining on that wakeup too); and a prompt typed during the reconnect
`api_session_launch` fires when a second launch reuses a live session to apply
`--model`/`--effort`. That last one is an `asyncio.create_task(bridge.reconnect())`
from the *server*, with nothing awaiting it and no worker checkpoint anywhere
after it — measured at **33 s** of `connecting` in one real case, which is a
generous window in which to type. No care at the call site could have prevented
it: the call site is in `server.py` and the park point is in `sdk_bridge.py`.

Each of those fixes closed one instance and left the class open, so the poke is
now wired to the **container**. `PersistentDeque` grew a listener list
(`add_listener`, additive — `on_change` is a single slot already owned by disk
persistence), and `SDKBridge.__init__` subscribes `_poke_for_queued_prompt`,
which puts `("wakeup", QUEUE_POKE)` on the event queue whenever the deque
changes and is left non-empty. Every writer — present, and any added later —
gets it without knowing it exists.

Two rules keep the poke from causing its own bugs:

- **It is declined while `state.connecting`.** `connect()` assigns
  `self.client` *before* awaiting the handshake, so acting on the poke would
  hand a prompt to `run_turn` and call `query()` on a client that is non-None
  and unusable at the same time. Declining is only safe because `connect()`
  re-pokes on the way out — that re-poke is the half that fixes reconnects
  nobody awaits, and it lives inside `connect()` precisely because that is the
  one place every reconnect, whoever launched it, must pass through.
- **It is the lowest-priority signal.** Everything else on the event queue is
  something the user explicitly asked for, so a poke that finds other events
  pending steps aside — otherwise a `/model` typed just after a prompt was
  queued had its reconnect deferred until *after* the prompt had run on the old
  model, and `/btw` (whose entire purpose is to jump the queue) lost to the
  queue head it is meant to jump. The same promise is kept on the other path:
  a `/btw` typed *during* a turn is drained by `_between_turns`, which pushes
  it to the **front** of `queued_prompts` (in typed order, so two asides do not
  come out reversed) rather than appending it behind everything already
  pending. It cannot be answered any sooner than that — a turn owns the single
  CLI — but "the moment this turn ends" is the whole of what `/btw` can mean.
  It remains an ordinary prompt in the *main* context; the "separate context"
  the help used to promise would need a second, forked CLI. See
  known-issues.md. It **re-queues** rather than drops, because
  dropping would strand the prompt for any pending event whose handler doesn't
  itself drain — the same bug, reintroduced by its own fix. `QUEUE_POKE_DEFERRED`
  bounds the yielding to one hop, so a queue full of pokes still terminates.

`tests/test_queue_poke.py` (14 tests) pins all of it, including the reported
sequence end to end against a fake SDK client with a deliberately slow
`connect()` — a zero-cost connect cannot reproduce the bug at all.

**A compaction is long, silent, and now says so.** An auto-compaction is one
summarisation call over the entire context, and it happens *inside* a turn.
Measured on a real run: `durationMs: 140989` — 141 seconds, 167,897 tokens in,
5,941 out, and no model output whatsoever for the duration. The status bar said
`working` the whole time with nothing being printed, which is indistinguishable
from a wedged session. That is what produced the report *"it said what it had to
say and then scheduled a wakeup, but the client shows still 'working'"*: the
status was not stuck, it was **accurate** — the turn genuinely had not ended.

The CLI already tells us. `services/compact/compact.ts` calls
`setSDKStatus('compacting')` before the call and `setSDKStatus(null)` after,
which arrives as `{type: 'system', subtype: 'status', status: …}` →
`SystemMessage(subtype='status')`. We were dropping it on the floor.
`_handle_system_message` now records it in `state.cli_status` /
`cli_status_started_at` and pushes a status update; `state_to_status_dict` ranks
`compacting` **above `busy`** — both are true at once, and `compacting` is the
one that explains the silence — and times it, because during a two-minute silent
stretch the only interesting question is how long it has been going on. A
compaction cannot span a turn boundary, so `run_turn` clears the flag
unconditionally at its start: a CLI that dies mid-compaction never sends the
clearing status, and without that reset the bar would read `compacting` for the
rest of the session. The label carries through `theme.py`
(`indicator-compacting`), `styles.css`, `status.js` (which runs its own 1 s
local timer for it, as for `working`/`connecting`) and `app.js`, where it counts
as busy — a prompt typed during a compaction queues exactly as it would during
any other part of a turn.

**A wakeup that fires mid-turn is deferred, not injected.** The second half of
the same report. ScheduleWakeup is a *tool*, so the assistant message carrying
it has `stop_reason: "tool_use"` and the harness must make another API call —
the turn cannot end there. In the observed run that next step tripped the
compaction above, and the post-compaction continuation prompt restarted the work,
so the turn ran on well past the scheduled time. `_wakeup_timer` then fired into
that live turn and injected `"Resume work now…"` as a user message the model had
to obey, mid-thought.

`_wakeup_timer` now checks `state.busy` first. A running turn means the loop is
not stalled, so it needs no nudge. It **re-arms** for `WAKEUP_MIN_DELAY` rather
than dropping — a turn that ends without the model re-arming would otherwise
strand the loop for good, with no pending wakeup and a model that considers its
ScheduleWakeup call already made. The re-arm is **bounded** by
`WAKEUP_MAX_DEFERS` (10); see "A loop that could not be stopped" below for why
an unbounded one was a bug rather than a courtesy. That re-arm is why
`_cancel_wakeup` grew the
same self-cancel guard as `server._cancel_idle_timer` (§4): `_arm_wakeup` begins
by cancelling the pending timer, which at that moment *is* the calling task.
An unconditional `.cancel()` there would mark the running task cancelled and
silently truncate everything after the re-arm — the exact shape that cost this
hub 19 live CLI subprocesses, and left no trace when it did.

`tests/test_compacting_status.py` pins both halves (13 tests, each verified
against a mutant: the ranking removed, the change-guard removed, the stale-clear
removed, the deferral removed, the deferral turned into a drop, and the
self-cancel guard removed).

**A loop that could not be stopped — by the agent or the operator.** An agent
reported: *"No cron job exists and I've stopped the dynamic loop twice, so these
wakeups are coming from outside my control — you may need to end the /loop on
your end."* It was right on both counts, and neither party had a way out.

The premise is that the CLI does not act on `ScheduleWakeup` in streaming mode,
so orchestrator2 runs the timer itself: it intercepts the tool call, arms an
`asyncio` timer, and later injects the prompt as a user message. That makes the
loop a piece of *hub* state sitting outside the conversation — the agent sees
only its own tool call, and the operator saw nothing at all. Two bugs followed
from that arrangement, both in `_maybe_arm_wakeup` / `_wakeup_timer`:

- **`stop: true` armed a loop instead of ending one.** The tool documents that
  stopping means calling it with `stop: true` and *every other field omitted*.
  The handler read `delaySeconds` and `prompt` and never read `stop`, so the
  omitted delay fell to its 60 s default and the omitted prompt resolved to
  `WAKEUP_RESOLVED_PROMPT`. Asking to stop armed a fresh 60 s wakeup. The agent
  stopping "twice" armed it twice — its own escape hatch was the accelerator.
- **The mid-turn deferral re-armed forever.** The deferral above re-armed
  unconditionally, so a session that stayed busy re-armed every 60 s with no
  limit. `orchestrator2.log` carried the matching pair (`wakeup fired mid-turn —
  deferring 60s` / `wakeup armed: delay=60s`) on repeat.

Both are fixed, and the second fix has a shape worth stating: a deferral is a
*courtesy to a working session*, not a promise to deliver. After
`WAKEUP_MAX_DEFERS` (10) consecutive deferrals the wakeup is **dropped** and
logged, because ten straight busy checks mean the session is working, which is
the very condition the loop exists to restore. `_arm_wakeup` takes
`_keep_defers=True` on the deferral path so re-arming does not reset the counter
it is supposed to be bounded by — without that the bound is unreachable, which
is the mutation `loop`/#5 pins.

The surfacing half is **`/loop`** (`server._handle_loop`, `commands.classify`,
`sdk_bridge.loop_status`/`stop_loop`/`start_loop`):

| `/loop` | show whether one is armed, when it fires, how many times it has been deferred, and how to stop it |
| `/loop off` (also `stop`, `cancel`, `end`, `0`) | cancel it, and record that it was cancelled |
| `/loop on` | arm one at `WAKEUP_DEFAULT_DELAY` |
| `/loop <seconds>` | arm one, clamped to the tool's documented 60–3600 s and **saying so** when it clamps |

Two details are load-bearing rather than cosmetic. `0` is a stop word, not a
duration — read as a duration it would clamp *up* to the 60 s floor, so the
plainest way to ask for no loop would arm the fastest one available. And
`stop_loop` sets `_loop_stopped`, which `/loop` reports as `not armed
(stopped)`: an operator who just typed `/loop off` needs to distinguish a loop
they ended from one that was never there, since removing exactly that doubt is
the command's purpose. Stopping and arming **broadcast** (they change what the
session will do next, so every viewer must see it); a status query answers only
the tab that asked.

`tests/test_loop_control.py` (39 tests) and two mutation targets, `loop`
(`sdk_bridge.py`, 11 mutations) and `loop-server` (`server.py`, 8), pin all of
it — including both original bugs restored verbatim. The `loop-server` sweep
was worth its own run: five of its eight mutations survived the first pass,
each a real gap (`/loop 0` arming, the audience of every reply, the
stopped/never-armed distinction, and the `--no-wakeup` message), and the tests
that close them were written from the survivors.

**Thinking blocks.** Models newer than `claude-opus-4-6` return *encrypted*
reasoning: the `thinking` block carries a `signature` but an empty `thinking`
string, and no `thinking_delta` ever streams. Those blocks are still surfaced —
rendered as `💭 Thinking (hidden by model)`, non-expandable — in the live turn
(both dispatcher and `run_turn` paths), in history replay (`session.py`), and in
the tool-detail view (`tool_manager.py`), so the three stay consistent.

*Why it's empty, and whether it can be recovered* (checked against the Claude
Code source and measured, 2026-08-14): this is an API-side policy, not an SDK
artefact and not a missing option on our side. The raw chain of thought is
withheld and the signature becomes a short handle — 764–792 B for
`opus-4-8`/`opus-5` versus ~4200 B for `opus-4-6`, whose text *does* arrive — so
the model can resume its own reasoning without the client ever seeing it.
Measured across 51 session files: ~101 000 thinking blocks on `opus-4-8`,
`opus-5`, `sonnet-5` and `fable-5`, none with text; only `opus-4-5`/`opus-4-6`
have any. Two live checks against CLI 2.1.228 both came back empty: plain
`claude -p`, and `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1 MAX_THINKING_TOKENS=4000`
(so it isn't the adaptive-vs-budget thinking switch either).

The readable path that does exist upstream is a *summary*, not the CoT:
Claude Code sends the `redact-thinking-2026-02-12` beta to skip an API-side
summariser, but only for **interactive** sessions and only when settings.json
doesn't set `showThinkingSummaries: true` (`src/utils/betas.ts`). Print/SDK
sessions — ours — never send that header and still get empty blocks, so there is
no header we're failing to ask for.

What *is* reliably available is the fact and duration of thinking: the block
still arrives, and the TUI drives its `thinking…` / `thought for Ns` spinner
purely off `content_block_start` (`src/utils/messages.ts`), never off content.
If we ever want parity with what a TUI user actually sees, that's the signal to
use — not the text.

---

## 6e. Finishing a turn the CLI reports as interrupted

When the CLI resumes a session whose last turn was cut off mid-flight, its
conversation-recovery layer appends a **synthetic** user message —
`"Continue from where you left off."`, flagged `isMeta` — plus an assistant
sentinel so the transcript stays API-valid if nothing acts on it.

Interactively the user is offered the choice, and accepting makes the CLI
delete that pair and re-enqueue it as a real prompt. **Non-interactively there
is no chooser**, so without intervention the pair simply stays: a prompt the
user never typed, answered by a refusal to do anything, with the interrupted
work quietly abandoned.

`_make_options()` therefore sets **`CLAUDE_CODE_RESUME_INTERRUPTED_TURN=1`**,
the supported opt-in for this path — remove the pair, re-enqueue it once, carry
on. It defaults on because the alternative is not "do nothing", it is "leave
the noise *and* drop the work". `--no-resume-interrupted-turn` opts out for
anyone who wants to open an interrupted session without it acting immediately.

**The transcript is left verbatim.** An earlier version of this fix also
filtered the pair out of the replay; it was reverted on request, and rightly —
this codebase has burned enough time on failures that were invisible, and
hiding records makes the next diagnosis harder. The pair stays; the env var
means it now gets acted on rather than abandoned.

**Counter-intuitive when diagnosing:** the pair is the *last* thing in the
transcript, so it reads as a parting shot from the dying process. It is the
*first* thing the new one wrote — the recovery layer runs during session load,
and the pair lands ~0.7 s before connect completes. Confirming that needs the
transcript's UTC stamps aligned against `orchestrator2.log`'s local ones; the
four-hour offset is enough to make a resume-time write look like a
shutdown-time one. See `known-issues.md` for the measured timeline.

**Recovering the turn is announced.** Acting on the pair means a session can
start producing output with nobody having typed anything — open it from the
lobby and it is simply *working*. Reported 2026-09-16 as "it was apparently
still doing a turn, even though the tab had been closed for a long time". The
recovery was correct; the silence was not. `_begin_ghost_turn_if_needed()` now
emits a one-time `system_msg` — *"Continuing a turn that was interrupted before
this session was last closed. Nothing was sent from here…"* — and names the
interrupt button, because the work may be days old and no longer wanted.

The signal is `_unprompted_resume_pending`, a single flag with three
touchpoints: `_make_options()` arms it when the connect carries `resume=`,
`run_turn()` clears it the moment a prompt goes out, and the ghost-turn path
consumes it. So it is true in exactly one window — *connected with resume, have
not asked for anything yet* — and a stream arriving in that window has no other
possible author. That distinction matters because the **common** ghost turn is
a background task's `<task-notification>` waking the model mid-session, and
telling that user their turn had been interrupted would be a lie repeated
several times a session. A reconnect re-arms it, since transport death
mid-turn produces the same surprise and owes the same explanation. The
`ghost turn begin` log line now carries `resumed_interrupted_turn=` so the two
kinds stay distinguishable in forensics.

Tests: `tests/test_resumed_turn_notice.py`; mutation target `resumenotice`.

### Background tasks that never end

A background task leaves the panel when the CLI says it finished. When the CLI
never says so the row stays forever, and **every hub process accumulates them**
— measured across the log, 12 of 5,577 started in one, 7 of 13,074 in another,
2 within the first 277 of a freshly restarted one. Roughly one task in a
thousand, permanent and cumulative, which is why the panel is reliably wrong by
a row or two even though the per-task rate is tiny. *An event rate is the wrong
statistic for an error that never self-heals.*

**The rows are not stale bookkeeping — they are real hung processes.** Reported
2026-09-16 against a session the model considered finished; the task was still
there in the OS:

```
pid 34752  started 16:27:15  cpu_time=0.0s  status=running
    grep -rliE "[d]:.github" --include=*.bat ... .
```

Zero CPU in twenty minutes, output file frozen at 50 bytes since three seconds
before the task was even registered. The CLI sent no completion because there
was none; the model had read the partial output file directly and moved on. The
panel, the CLI and the model were each telling the truth about different
things, and the disagreement was an abandoned hung process nobody could see.

`bg_stall.py` therefore does **not** decide a task is finished — it never
removes a row or invents a completion. It decides whether a task is still
*doing* anything, which is answerable from two signals:

- **output** — the CLI streams task stdout to a path we can derive
  (`%TEMP%/claude/<sanitised cwd>/<session>/tasks/<task id>.output`); a
  size/mtime that has not moved is a task producing nothing;
- **CPU** — the task's process tree, summed. The descendants matter: what we
  match is the task's *shell*, and a shell sleeps while a child compiles or
  runs tests, so reading it alone reports a flat zero for a task working hard.
- **Disk I/O** — read/write *operation counts* for the same tree. CPU misses a
  whole class of work: copying a large file, walking a huge tree or pulling
  over the network burns almost no CPU and may write nothing to the task's own
  output file for minutes. Counted in operations rather than bytes, because a
  task re-reading the same cached page makes no byte progress but is
  unmistakably alive. This is also what separates *doing* I/O from being
  *blocked* on it — the reported grep was the latter, and its counters were as
  still as its CPU.

The process is found among the CLI's descendants by matching the recorded
command, **normalised on both sides** — the CLI wraps it in shell-snapshot
sourcing and `eval 'cd "..." && <command>'`, and those layers strip quotes,
flip slashes and flatten newlines. When several processes match they are the
task's own wrapper chain, and the *outermost* is taken: the activity sample
sums a subtree, so the ancestor covers every layer exactly once.

All three of those facts came from probing live tasks rather than fixtures. A
fixture-only suite had the shell at fault (single process, so the descendant
sum looked unnecessary), the match passing (hand-written command lines are not
wrapped), and the outermost-vs-innermost choice backwards.

Only when **all three** have been still for `STALL_AFTER_S` (300 s, the same
bar as the idle grace) is a task `STALLED`. If the process cannot be identified the
CPU half is unprovable, so the worst said is `QUIET`, which annotates and
changes nothing — "I lost track of it" is a statement about us, not the task.
Guessing the other way would reintroduce the failure this project keeps
re-learning: a session that *is* working, reaped because it looked idle.

**What STALLED changes is holds, not rows.** Six places let background work
defer something — idle teardown, a deferred `/model`, its flush, the
`bg-all-done` wakeup, the rolling context trim, and bg-wait parking. All six go
through `SDKBridge._active_bg_tasks()`, and a test pins the count so a seventh
reading the raw registry can't quietly appear. This matters most for the idle
teardown, whose deferral is deliberately *unbounded*: "a session that keeps
working is succeeding at its job" is right, and exactly why a task burning no
CPU must not count — it would pin the runtime until the hub died.

The row itself stays, annotated with what was observed (`no output or CPU for
20m`) rather than a verdict (`hung`) — the first is checkable by the reader,
the second is a conclusion we cannot reach for a task that may be blocked on
something about to arrive. Rows we can identify a process for get a kill button
(`POST /api/bg/kill`), which verifies the recorded `create_time` before killing
so a recycled pid can't be shot. **Nothing kills on a timer**: the panel reports
and the person reading it decides.

The 2 s status ticker drives the probe, rate-limited to `BG_PROBE_INTERVAL_S`
(15 s) and run in a thread — the Windows process-tree walk is not free, and it
shares an event loop with token streaming.

Tests: `tests/test_bg_stall.py`; mutation targets `bgstall`, `bgstall-wire`.

### `/btw` — a forked side conversation

`/btw <question>` asks something **while** the session works. A turn owns the
one main CLI, so the answer cannot come from it; instead a second
`ClaudeSDKClient` is connected with `resume=<current sid>` and
`fork_session=True`, which the SDK documents as *"resumed sessions fork to a
new session ID rather than continuing the previous one"*. Same context, new
session id, original transcript untouched, and genuinely concurrent — a
separate CLI process the live turn never learns about.

Verified end-to-end against a throwaway session rather than trusting the flag:
the fork got a new id, answered from context (it recalled a codeword planted in
the base conversation), and the base transcript was **byte-identical** before
and after.

It runs off the worker entirely (`SDKBridge.run_btw` → an `asyncio.Task`), so
nothing about it touches the turn in flight. `server.py` routes `btw` straight
there; it falls back to the old event-queue path only when there is nothing to
fork (a session with no id yet), where it behaves as it always did — an
ordinary prompt at the front of the queue.

**The fork may not change anything.** Two agents in one working tree at the same
time is the hazard: the live turn may be mid-edit on the very files an aside
would touch, and neither would know. So `BTW_DISALLOWED_TOOLS` removes the
mutating set — including `Bash`, despite its read-only uses, because it is the
one tool that can write anything at all. Read/Grep/Glob stay, since answering
"which file did you mean?" is the point. With nothing dangerous left to
approve, it runs `bypassPermissions`: nobody is watching a fork, and a
permission prompt it cannot deliver would hang the aside forever.

**That block is by tool name, and is not airtight.** `disallowed_tools` names
the built-ins; MCP tools loaded from the user's own settings (`setting_sources`
is inherited so the fork sees the same project context) are not covered, and a
write-capable MCP server would still be reachable. Restricting to a whitelist
would not help: `allowed_tools` is a permission *auto-approve* list in this
CLI, not a restriction. So the accurate claim is "cannot edit files or run
Bash", not "read-only".

**It is told what it is.** `BTW_PREAMBLE` says the real session is still
running and that it must not continue, resume or redo the task it can see. A
fork of a session mid-task otherwise reads its own transcript, concludes there
is a job in progress, and starts doing it — in a context whose edits are
disabled, so it would fail confusingly instead of answering. It also carries
`CLAUDE_CODE_RESUME_INTERRUPTED_TURN=0`, because the main session's opt-in to
finishing interrupted turns is the last thing an aside should inherit.

**A fork is not a holder.** The forked CLI is launched with
`--resume <the same session id>`, which is exactly what
`proc_guard.find_foreign_claude_for_session` scans for to detect "something
else is already driving this conversation" — and that scan is what *stops a
session opening*, so a false positive is not cosmetic. Our own tree is
excluded, so a hub never trips its own guard; but another hub scanning during
the seconds an aside lives would have seen the session as held elsewhere. Both
scanners (the duplicate check and the holder map the lobby reads) now skip
processes carrying `--fork-session`, via `_is_session_fork`. It is matched as
an *argument*, never as a substring of the joined line — same rule the
`--resume` pair match follows, so a session whose prompt happens to discuss the
flag is not waved through. The flag name is pinned against the SDK source
(`cmd.append("--fork-session")`) rather than assumed, because a filter watching
for the wrong spelling fails silently and looks like it works.

**Housekeeping.** A fork is a real session on disk, so its transcript is deleted
once it answers — otherwise every aside leaves a stub conversation in `/resume`
and the lobby forever. The delete refuses to touch anything whose id matches
the live session's. Only one `/btw` runs at a time: each is a CLI process and a
full transcript load, on a runtime that already recycles the CLI for leaking
memory.

**In the UI** it renders inline, where it was asked, but visually apart — an
accented block labelled `/btw`, with the question and the streamed answer, and
a note that the session itself never saw it. Folding a fork's reply into the
transcript unmarked would be worse than not having the feature: you would be
reading answers to a question the session never received. It is live-only —
nothing of it is in this session's history, so there is nothing to replay.

Tests: `tests/test_btw_fork.py`; mutation target `btwfork`.

### A prompt sent during a ghost turn

A ghost turn sets `state.busy` but **not** `turn_active`, which means the
worker is parked in `_await_next_prompt()` — looking idle — while the CLI is
mid-answer. That gap swallowed a prompt (reported 2026-09-16: *"it showed it
sent as a 'You:' message, but it just kept working and never answered me"*).

Routing was never the problem. `server.py` checks `state.busy` and correctly
queued the prompt. The queue poke then woke the parked worker, which popped it
— echoing it as `You:` — and started a turn on top of the live stream. That
turn consumed the **ghost turn's** terminating ResultMessage (logged with the
ghost turn's 1159 s elapsed, not its own) and exited "normally" having answered
nothing.

The prompt itself was **discarded by the CLI**, not merely delayed: its
transcript shows `queue-operation enqueue` at 12:59:14.189Z and
`queue-operation remove` with `reason: "absorbed_mid_turn"` at 13:06:17.594Z,
and no `user` record for it anywhere. A prompt handed to a CLI that is already
streaming can be absorbed into the running turn and dropped — which is why the
fix is to not send it, rather than to send it and wait.

`_await_interrupt_settled` already guarded this exact race when an *interrupt*
caused it. Nothing guarded it when a background task did.

The fix is one decline, in the `wakeup` branch of `_await_next_prompt()`,
alongside the identical `state.connecting` one: **do not pop a queued prompt
while `state.busy`**. At that park point a real `run_turn` cannot be active —
the worker would be inside it, not parked — so `state.busy` there means
precisely "a ghost turn is streaming". Declining leaves the prompt in the queue
pane, visibly pending, instead of echoing it into the transcript and losing it.

Declining is safe because `_end_ghost_turn()` **already** poked the worker with
`queue-edit-done` for exactly this case; that poke was written for ghost turns
that stream briefly and works unchanged for one that runs nineteen minutes. The
missing half was never the resume — it was the decline.

`run_turn` also waits on `_await_ghost_settled()` (a `_ghost_settled` event,
cleared on ghost-turn begin, set on end) next to the interrupt guard, covering
every other way in. Its timeout is 1800 s rather than the interrupt guard's 5 s
because a ghost turn is real work and starting early *is* the bug; it is finite
only so a terminator that never lands cannot wedge the session, and that case
logs loudly.

Tests: `tests/test_prompt_during_ghost_turn.py`; mutation target `ghostqueue`.

### Background work that died with the session

Recovering the turn is only half of it. If the session was running background
tasks when it was cut off, the model resumes believing they are still out
there — waiting for `<task-notification>`s that nothing can send and holding
`TaskOutput` handles that no longer resolve. Reported 2026-09-16: *"the agent
has no idea that any background tasks it had been running were aborted."*

`State.background_tasks` mirrors the CLI's registry and dies with the process,
which left three holes. `_orphan_bg_tasks()` had told the **browser** about
orphaned tasks since 2026-08 and never told the **model**; a *teardown* told
nobody at all, being wired only to the reconnect paths; and after a teardown we
did not even know, because `session.py` persisted the prompt queue and nothing
else.

So the live set is now mirrored to `<config-dir>/orchestrator2/bgtasks/`, keyed
by cwd **and** session id, written on every task start and finish
(`_save_bg_tasks`). `connect()` reads it back once for the session being
resumed (`_report_lost_bg_tasks`), announces it, and erases it — reported
once, never again. `_orphan_bg_tasks()` erases it too, so a loss announced live
is not re-announced at the next resume.

**Three save sites, and no clear site.** The record exists to outlive the
process that wrote it, so the paths that wipe the in-memory registry on the way
out — `_teardown_runtime`, `_clear_context` — must *not* save. That inversion
is the whole mechanism, and `tests/test_lost_bg_tasks.py` pins the call-site
count so a fourth save cannot quietly appear.

**It says "unknown", never "aborted".** Asked whether the registry could even be
trusted, the log says yes — 50,886 starts against 50,841 completions, ~4
unexplained — but *"running when we last heard"* is still not *"failed"*: at
04:01:34,445 on 2026-09-16 a teardown began and two tasks logged **completed**
1 s and 2 s into it. So `describe_lost_bg_tasks()` names the tasks and how long
each had been running, says the results are gone and the outcome is unknown,
and tells the model to check for effects before re-running and not to wait.
Claiming an outcome we cannot know is the one thing that would make the notice
worth ignoring.

**It arrives as a queued prompt, at the front.** Going through
`state.queued_prompts.appendleft()` buys the whole delivery for free: the
deque's `on_change` persists it, its listener list pokes the worker (so it is
sent immediately when nothing is running) and pushes the queue panel, so it is
visible in the left pane like any other queued prompt. `appendleft` rather than
`append` because it is *context for whatever the session does next*, not a task
of its own — appended, a prompt queued earlier would be answered by a model
still believing its background work was alive. Merging it with your own next
prompt is deliberately not automatic; `merge all` in the queue pane already
does that on request.

**`/clear` is the one silent orphan path.** It wipes the conversation, so a
model that has just lost all memory of *starting* those tasks cannot act on
being told they were lost, and the notice would be the first thing in its
brand-new context. The browser is still told. Every other caller -- a
reconnect, a CLI that died for good, a resume -- leaves a model that still
remembers the work, so all of them tell it.

Tests: `tests/test_lost_bg_tasks.py`; mutation targets `lostbg`,
`lostbg-session`.

## 6a. The model list

`/model` runs on the event loop, so it can only ever read a cache — it must
never block on a network call. `config.py` holds that cache
(`_model_cache`, `_model_cache_at`), filled from Anthropic `/v1/models` using
`ANTHROPIC_API_KEY` when set, otherwise the Claude Code OAuth token from
`<config-dir>/.credentials.json`.

`server.py::_model_cache_loop()` keeps it warm: retry with exponential backoff
(30 s → 10 min) while fetching fails, then re-fetch every `MODEL_CACHE_TTL`
(1 h). Both halves matter — without retry, one transient failure at startup
pinned the process to the stale hardcoded `KNOWN_MODELS` for its whole
lifetime; without the TTL, a hub left up for days never saw a newly released
model.

`KNOWN_MODELS` is the offline fallback. When it's what's being served,
`/model` sets `live: false` and the UI says so — a *silent* fallback is
indistinguishable from "that model doesn't exist". Note that `/model <id>`
never validates the id against the list, so any model can be selected by name
regardless.

Caveat: the cache is process-global, but credentials are per config dir.
`main()` pins `CLAUDE_CONFIG_DIR` for the process, so the list reflects the
*hub's* account even for a runtime opened under a different one.

## 6b. Subprocess ownership (`proc_guard.py`)

The SDK spawns `claude.exe` as a child process, and Windows does **not** tear
down a process tree when the parent dies. Left alone, every server that gets
killed leaks a live agent that keeps editing files and committing — invisible
to the lobby, because it belongs to no `SessionRuntime`.

Three nets:

- **Job object.** `lifespan` calls `install_process_reaper()` before anything
  spawns, putting the serving process in a job with
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. Children inherit membership, so the
  kernel reaps the whole tree however the server dies — including `taskkill
  /F`, where no shutdown code runs. That "no shutdown code runs" case is the
  entire reason this is a job object and not an `atexit` handler.
- **Duplicate-session guard.** `connect()` refuses (`DuplicateSessionError`)
  when another `claude` process outside our tree is already resuming the same
  session id, since two agents would share one session JSONL and one working
  tree. Overridable with `--allow-duplicate-session`.
- **Per-session descendant reap.** `SDKBridge.disconnect()` snapshots the
  CLI's descendants *before* stopping it, then kills any survivors. This
  covers the gap the job object cannot: when only `claude.exe` dies and the
  server lives on, the job object won't collect anything until the server
  itself exits.
- **The CLI itself is verified dead.** `snapshot_process(pid)` pins the CLI's
  own pid + `create_time` alongside its descendants, and after
  `client.disconnect()` returns, a survivor is killed directly (and logged at
  WARNING). The SDK's teardown is a best effort — `wait(5) → terminate() →
  wait(5) → kill()` — that this used to treat as a guarantee, inside a bare
  `except Exception: pass`.

Note the blind spot the duplicate-session guard has *by construction*: it
excludes `_own_tree_pids()`, so it can never catch an orphan we created
ourselves — a leaked CLI is a child of our own hub. That is why the teardown
path (§4) has to be airtight rather than relying on the guard to notice
afterwards.

### Why the reap is a pid list and not a nested job

The obvious design — give each `claude.exe` its own nested job — does not
work here, and the reason is worth recording so nobody retries it. **Job
membership is inherited only at process creation.** Assigning an
already-running process to a job does not retroactively pull in the children
it has already spawned (measured: assign-then-spawn reaps the grandchild,
spawn-then-assign leaves it running). Since the SDK spawns `claude.exe` and
`claude.exe` spawns its MCP servers during its own init, we could only ever
assign *after* the processes we care about already exist.

An explicit list is sufficient precisely because of the division of labour:
the only scenario where no cleanup code can run is the server dying, and the
job object already covers that. Whenever a *session* is torn down, the server
is by definition alive and able to run code.

Snapshot ordering and pid identity both matter. The snapshot must happen
while the CLI is alive, because orphaning destroys the parent links needed to
walk the tree; and each entry stores a creation time, because Windows recycles
pids and the gap between snapshot and reap is exactly when the CLI's pid
becomes available for reuse. Killing on a bare pid could shoot an unrelated
process — a worse bug than the leak.

`_cli_pid()` reads SDK-private attributes (`client._transport._process.pid`)
and fails soft to `None`. Because a silent `None` would quietly restore the
leak, `test_sdk_still_exposes_the_cli_process_we_reach_for` asserts those
attributes still exist.

**Invariant:** any child that must outlive the server — currently only the ⟳
Restart replacement — has to pass `proc_guard.breakaway_flags()` in its
`creationflags`. Forgetting it means the restart kills its own replacement.
The job sets `JOB_OBJECT_LIMIT_BREAKAWAY_OK` to make that possible;
`breakaway_flags()` returns 0 when no reaper is installed, so it's always safe
to add.

The `--detach` parent and the picker console intentionally do *not* install a
reaper: the parent exits immediately and would otherwise take the real server
down with it.

## 6c. CLI memory recycling (`SDKBridge._maybe_recycle_cli`)

Anthropic's bundled `claude.exe` leaks **committed** memory per turn. Measured
2026-09-01 on a host running five sessions:

| | commit charged | in RAM | in pagefile | nowhere |
|---|---|---|---|---|
| system | 254.60 GiB of a 255.95 GiB limit | 49.29 | 20.60 | **184.71** |

| image | procs | private | working set | ratio |
|---|---|---|---|---|
| `claude` | 5 | **173.57 GiB** | 12.21 GiB | **14.2x** |
| `chrome` | 45 | 7.26 GiB | 6.12 GiB | 1.19x |
| `python` (incl. this server) | 20 | 1.34 GiB | 0.40 GiB | 3.3x |

Sampling those processes across a 25 s idle gap gave flat-to-negative deltas
for the idle ones and **+21.85 MiB for the one taking a turn**, which places
the growth in per-turn work inside the CLI — not in this orchestrator (its
parent `python.exe` held 0.55 GiB after 88.9 h) and not in the browser.

Two things about *commit* make this worse than an ordinary leak, and both are
why the gauge reads `PrivateUsage` rather than working set:

- **It is invisible to RSS.** 184.71 GiB of the charge was reserved and never
  touched, so it lived in neither RAM nor the pagefile. A monitor watching
  working set would have shown 12 GiB and called the machine healthy.
- **Windows does not overcommit.** The charge is deducted from a system-wide
  limit at `VirtualAlloc(MEM_COMMIT)` whether or not anything is stored, so it
  starves *unrelated* programs. Observed: a 6.95 GiB render allocation refused
  on a box with 20.97 GiB of RAM free.

We can't fix it upstream, but a process's leak is bounded by its lifetime, so
we end the lifetime: `reconnect()` — disconnect, reap, `--resume <session>`.
That is the identical path `/model` and `/effort` take on every use, so the
session id, transcript, cwd and context all survive; the replacement starts at
its baseline.

**The whole design is in *when* it is allowed to fire.** A recycle at the wrong
moment is not a slow session, it is lost work:

- **No turn in flight** — the in-flight tool result would be discarded. Given
  by the call site: the check sits at the very bottom of `_between_turns()`,
  after the interrupt, `/compact`, queued-prompt and background-task branches
  have all had their chance to return. It is the one quiescent point in the
  loop.
- **No background tasks** — the CLI's task registry is in-memory
  (`AppStateStore`: `tasks: {}`) and `--resume` does not rehydrate it, so a
  recycle orphans the bookkeeping for anything still running: no completion
  notification, no `TaskOutput`, no `Monitor`. The OS processes themselves
  survive (they are in no job object, and the CLI's `gracefulShutdown` reaps no
  children), so this costs tracking rather than work — but the leak is slow
  (0.08–0.65 GiB/h), so waiting it out is free by comparison.
- **On the worker task** — a `disconnect()` from any other task cancels the
  worker through the SDK transport's anyio cancel scope, and the SDK's
  `suppress(Exception)` eats the evidence. See `_warn_if_foreign_task`. Hence
  `/recycle now` is an event-queue kind (`recycle`) the worker drains, never
  something a WebSocket handler does inline.
- **Growth above the baseline** (`cli_recycle_min_reclaim`, 1 GiB). `connect()`
  records `state.cli_mem_baseline` right after the resume is read, so it
  includes the transcript. Without this, a session whose *honest* fresh-CLI
  cost already exceeds the limit — a 1.29 GB JSONL will do it — would recycle
  on every single turn and never once get under it, paying a full multi-minute
  `connect_timeout_for()` resume each time to reclaim nothing.
- **Cooldown** (`--cli-recycle-cooldown`, 600 s) as a hard backstop on the same
  cost.

A failed recycle is caught and reported, never raised: this is awaited from
`_between_turns`, so an escaping exception would end the worker and leave the
session reading "idle" while every later prompt lands in a queue nobody reads.

Surface area: `--cli-recycle-at GIB` (default 8, 0 disables),
`--no-cli-recycle`, `--cli-recycle-cooldown SECS`; `/recycle` reports,
`/recycle <GiB>|off|on` retargets at runtime (via `state.cli_recycle_at`,
shadowing the flag the same way `_max_context_tokens` does), and `/recycle now`
forces one — skipping the threshold and the cooldown, which pace an *automatic*
policy, but **not** the safety conditions, which protect work in flight. The
status bar shows a `cli` field, hidden until the process passes half the
threshold so it stays silent in the common case.

## 6d. A reconnect is not free while background tasks are running

The bullet above — "no background tasks" — was, for a while, `_maybe_recycle_cli`'s
*private* virtue. There are eight callers of `reconnect()` and it was the only
one that checked. `/model`, `/effort`, `/thinking`, `/connect`, the
transport-death recovery, the `auto_reconnect`-after-a-failed-turn path and the
context trim all cleared a live `state.background_tasks` and recorded it with a
`log.info` nobody reads.

The lost `TaskOutput` handle was the smaller half. The larger half:
`_between_turns()` parks the session in bg-wait **only** while
`state.background_tasks` is non-empty, and leaves that park on the
`bg-all-done` wakeup `_complete_bg_task` sends when the dict drains. Clearing
the dict from underneath it skips the park entirely — so an autonomous session
waiting on background work dropped straight to idle, was never woken, and said
nothing, while the processes it had spawned carried on unattended.

So the knowledge moved down into `reconnect()`:

- **`_orphan_bg_tasks(why)`** clears the registry *and broadcasts a warning
  naming the tasks* (first five, then "and N more"). Losing the bookkeeping is
  sometimes unavoidable; losing it silently never is. A task we failed to parse
  falls back to `task <id>` rather than an empty string — the unparseable one is
  exactly the one worth naming.
- **`_reconnect_or_defer(reason)`** is what the *discretionary* commands call.
  `/model`, `/effort` and `/thinking` want a new CLI, but not at the price of
  orphaning running work, so with tasks in flight the request is recorded in
  `_deferred_reconnect` and the user is told — including that `/connect`
  overrides. This is the same trade `_maybe_recycle_cli` already makes.
- **`_flush_deferred_reconnect()`** applies it at the two points the session can
  become quiescent: the top of `_between_turns()` (tasks drained during a turn —
  placed *above* the queued-prompt branch, so the next prompt runs under the new
  settings rather than one turn late) and the wakeup branch of
  `_await_next_prompt()` (`bg-all-done`, which is the common case, since a
  parked session has no turn boundary coming).
- **Any `reconnect()` clears `_deferred_reconnect`.** The new CLI is built from
  current state, so a pending switch has already applied; leaving the flag set
  would reconnect twice.

What is deliberately *not* deferred:

- **`/connect`** — the user overruling us on purpose, having been told the cost.
  It sets `reconnect_forced` in the `_between_turns` drain.
- **A dead transport** (`_CONNECT_TRANSPORT_DEAD`) — there is no CLI left
  holding the registry, so waiting protects nothing. Goes to
  `_recover_dead_transport`, which is bounded and announced.
- **The context trim** is *skipped*, not deferred: `trim_session()` mints a new
  session id before the reconnect would happen, so postponing the reconnect
  would leave `state.session_id` pointing at a session the live CLI is not
  resumed on. Declining keeps the two in agreement and costs nothing — a session
  parked in bg-wait is not accumulating context, and the check runs again after
  the next turn.
- **`/clear`** wipes the registry along with everything else, which is what it
  means — but it now names what went with it, and drops any deferred reconnect
  (its own `connect()` applies the settings).

Pinned by `tests/test_reconnect_bg_tasks.py` (37 tests) and the `reconnect`
target of `tools/mutate.py` — 20 mutations, each undoing one part of the above,
all caught.

## 7. Frontend (`static/`)

Plain ES modules, no bundler. One WebSocket, opened by `app.js`, which owns
reconnection and dispatches every inbound message type to a module:

| Module | Responsibility |
|---|---|
| `app.js` | WS connect/reconnect, message router, URL/`?rid=` handling, global keys |
| `chat.js` | Message stream: text, tool calls, thinking blocks, results |
| `status.js` | Top status bar from `status_update` |
| `panels.js` | Live tool / background-task / todo panels from `panel_update` |
| `lobby.js` | `☰ Sessions` overlay (§8) |
| `switch.js` | `/move` — copy this session to another account and/or directory. Module/file/CSS keep the old `switch` name; see §8 |
| `diff.js` | Side-by-side edit diffs |
| `commands.js` | Slash-command input + completions |
| `util.js` | Shared helpers |

Styling: `styles.css` holds layout/structure; `theme.py` generates the CSS
variables it consumes, so themes never require CSS edits.

### A sent prompt is tracked by id, not by "did anything come back"

Each prompt the tab echoes optimistically carries a `prompt_id`, and the server
answers with `prompt_ack {prompt_id, disposition}` — `queued`, `enqueued`,
`immediate` or `rejected` — sent to that socket alone. The tab's 8s watchdog
does not clear on the ack; it *reads* it at the deadline and asks whether the
session's state is consistent with it. Only `enqueued` obliges a turn to start,
and only its violation escalates ("the server accepted your prompt but this
session never started running it"). No ack at all keeps the original meaning:
half-open socket or dead server → close and reconnect.

The ack is emitted once, by a wrapper around `_dispatch_ws_message` that
defaults to `immediate` for any branch that didn't disposition the prompt. A
missing ack does not fail safe — it closes a healthy socket — so totality is
enforced structurally rather than per-branch.

A session parked unable to connect (`state.connect_blocked_msg` set — currently
only a refused duplicate-session resume) is a deliberate `rejected`: enqueuing
would strand the prompt on an `event_queue` the parked worker isn't draining as
a turn, which is exactly the `enqueued`+idle case the watchdog escalates. So
`_enqueue_prompt` rejects it, re-surfacing the real reason, and the worker parks
in `_wait_for_reconnect_trigger()` (alive, so `/connect` can recover once the
other holder is gone) rather than exiting. `_send_initial_state` replays the
reason to a tab that attaches after the original broadcast. See known-issues.md.

*Why:* the watchdog used to clear on *any* inbound message, which the 30s forced
status heartbeat disarmed for free. A session whose worker had been silently
cancelled kept the tab on "idle" indefinitely with no complaint. See
known-issues.md.

### An idle tab must cost nothing

The status ticker fires every 2s per runtime, whether or not anything changed.
That looks free and isn't: each delivery wakes every viewing tab for a socket
read, a JSON parse, four panel renders and — if any of them touches the DOM —
a style recalc, layout and repaint of a document holding **tens of thousands of
elements** (a replayed session is ~48 DOM elements per message, so the default
`max_dom_messages: 2000` caps a tab at ~95k). Measured against the real UI with
Playwright, a session doing *nothing* cost **~2.7% of a CPU core per open tab,
forever**; with four tabs open orchestrator2 was the heaviest thing in the
browser. Main-thread JS was only 0.02% of that — the cost was almost entirely
the repaint pipeline, which is why a JS profile alone made the tab look idle.

Two independent rules keep it near zero, and both are load-bearing:

1. **The server doesn't send unchanged snapshots.** `_tick_runtime()` serialises
   status+panels, compares against `rt.last_status_sig`, and broadcasts only on
   a difference — plus a forced resend every `_STATUS_HEARTBEAT_SECONDS` (30s)
   so a wedged ticker is still distinguishable from a quiet one. Safe because
   `state_to_status_dict()` has no per-tick clock fields when idle: the elapsed
   "working (0:00:04)" label only ticks while `busy`, and `status.js` runs its
   own 1s local timer for that anyway. The bell flush deliberately sits
   *outside* the suppression.
2. **Every renderer is idempotent.** Panels and status bar compare before they
   write. The subtle traps, all of which were real:
   - a guard that `return`s *after* the write (the empty-state `innerHTML` in
     `_renderTools`/`_renderBg` was rewritten every tick);
   - no guard at all (`_renderTodos` rebuilt its whole list, re-ran a
     `querySelector` and re-attached a listener every 2s forever — the single
     largest offender);
   - `textContent = x` with an identical `x`, which still replaces the text
     node (hence `_setText`/`_setHTML` in `panels.js`, `_set` in `status.js`);
   - `classList.add`/`remove` of a token already in / not in the set, which
     re-serialises and re-sets the whole `class` attribute. `classList.toggle`
     with an explicit force argument does *not*, so prefer it. `Commands.setBusy`
     hit this and was, after everything else was fixed, still repainting the
     whole document every 2s on its own.

The regression tests are `tests/test_status_ticker.py`. The end-to-end check is
`tools/probe_mutations.py`, which watches a live tab with a `MutationObserver`:
**an idle tab must report zero DOM mutations.** That assertion is far easier to
reason about than a CPU number, and it's what caught the `setBusy` write.

> Measurement notes, because two earlier attempts here were wrong. Chrome's
> *renderer* CPU is what matters and `Performance.getMetrics` can't see it —
> `TaskDuration` covers only the main thread, so paint/raster/composite are
> invisible. Do not leave the CDP sampling profiler running while measuring
> process CPU: its sampling thread lives inside the renderer and made an idle
> tab read 8.9% instead of 1.7%. And never measure "no WebSocket" by *closing*
> the socket — that trips `app.js`'s reconnect and replays the entire history.
> `tools/measure_tab_cpu.py` handles all three pitfalls.

### Nothing that must keep working may be gated on an animation frame

A **hidden** window — minimised, fully covered by another window, or a
background tab — gets **zero** `requestAnimationFrame` callbacks, and its
`setTimeout` is clamped to one second, then to one *minute* once Chrome's
"intensive throttling" starts five minutes in. WebSocket delivery is **not**
throttled. So messages keep arriving and keep being appended while everything
scheduled on a frame is frozen — and any *"already scheduled, skip"* latch
guarding an rAF stays latched for the entire time the window is hidden.

`_maybeTrimOldMessages` was exactly such a latch: `_trimPending` went true, its
frame never ran, `max_dom_messages` silently stopped being enforced, and the
list grew without bound. Meanwhile every append still did
`scrollTop = scrollHeight`, which forces a synchronous layout of the whole
list — O(nodes) — so the append rate decayed as the list grew and **stayed**
decayed after the window was shown, because the DOM was still enormous. That is
the reported *"it only adds a few lines per second"*. History replay was a
second route to the same symptom: it yields with `setTimeout(_renderBatch, 0)`
between 50-message batches, and 1s-per-50 is a drip-feed.

It also explains the *"sometimes it's already up to date"*: a window that is
merely **unfocused but still visible** keeps getting frames, so none of this
happens. Focus is not the relevant fact; `document.hidden` is.

The rule, and how `chat.js` holds it:

- **`_onNextFrame(fn)`** runs `fn` immediately when `_hidden()`, otherwise on a
  frame. `_maybeTrimOldMessages` uses it, so the cap holds while hidden — which
  is precisely when the list grows fastest and with nobody watching.
- **`_scrollToBottom()`** returns early while hidden, setting
  `_scrollPendingOnShow`. One catch-up scroll on `visibilitychange` settles the
  whole backlog instead of one forced layout per message.
- **`visibilitychange`** also cancels the two collapse gaps
  (`_cancelGap`/`_cancelShortGap`). Those exist to hold a *visible* reading
  position steady, and `_maybeTrimOldMessages` returns early while either is
  open — a gap left open across a hide is the unbounded-growth bug by another
  route.
- **`_reclaimStrandedTrim()`**, also on the hide side, closes the *other* door
  into the same bug: `_onNextFrame` only helps when the cap is crossed while
  already hidden. Cross it while **visible** and the trim is correctly parked on
  a frame — then hiding the window strands that frame, and `_trimPending` is
  latched exactly as before. So hiding runs any pending trim synchronously.
  (Mutation testing found this; the tests and the original fix had both missed
  it.) The show side needs no counterpart: a trim that survives to the next
  `_maybeTrimOldMessages` is re-armed there anyway.
- **`_trimNow()` is idempotent on purpose** — it re-reads the child list and
  re-checks the cap. That is what makes the frame orphaned by
  `_reclaimStrandedTrim` harmless when it finally fires, and it is why no
  `cancelAnimationFrame` bookkeeping exists here: a `_trimFrame` id would be
  code no test could distinguish from its absence. Without the re-check a late
  frame would compute a removal count against a list already under the cap and
  delete *live* messages.
- **`_replayHistory`** wraps its batch in `do { … } while (idx < n && _hidden())`
  — a **loop**, not a recursive call, so a large history doesn't nest a stack
  frame per batch. It still yields normally while visible, which is the only
  case where responsiveness is worth anything.
- **The trim removes with one `Range.deleteContents()`**, not `remove`
  successive `removeChild` calls.
- **`_scheduleStreamRender` deliberately stays a bare rAF** and must not be
  "fixed" to match. It re-renders the *entire* accumulated `_streamingText`, so
  the one frame that runs on becoming visible produces fully caught-up text,
  whereas running it eagerly would re-render the whole markdown body once per
  delta for nobody. `_flushStreaming` cancels it and renders synchronously, so a
  turn ending while hidden still lands its text.

The same reasoning applies to the **transport**, not just the rendering, and
that half is the more damaging one. `app.js` runs its reconnect backoff on a
`setTimeout`, so a hidden window retries on the throttled clock: the 20-attempt
budget that spans ~7.5 minutes while visible burns down over ~20 minutes of
1/minute ticks with nobody watching, and then latches auto-reconnect off
**permanently** (`_serverShutdown`), leaving a dead transcript that only a typed
`/connect` can revive. A laptop suspend/resume drops the socket while hidden
essentially by definition, so this is the ordinary case, not an exotic one.

`_onVisibilityChange` in `app.js` therefore reconnects on show whenever the
socket is not up — which also short-circuits an ordinary backoff that still has
up to 30s to run, since the user is present and waiting out a timer helps
nobody. Two distinctions make it safe:

- **An OPEN socket returns early**, because `reconnect()` treats that case as a
  *server-side* SDK-bridge reconnect and sends `/connect`. Firing that on every
  tab focus would be a command the user never typed. (A CONNECTING socket needs
  no check — `reconnect()` already refuses to open a second one.)
- **`_retriesExhausted` is tracked separately from `_serverShutdown`.** Both a
  deliberate `server_shutdown` and a `server_restart` latch `_serverShutdown`,
  and neither is ours to overrule — the first would just fail noisily, the
  second would race the Lobby reload-poll that is already running. Only our own
  patience running out is reversible. The flag is cleared in `reconnect()`; left
  stale it would make the *next* genuine shutdown look retryable.

Tested in `tests/hidden_window.test.js` (16 tests) and
`tests/reconnect_on_show.test.js` (11 tests), which drive the real `chat.js` and
the real `app.js` in jsdom — the first with `document.hidden` **tied to** rAF
suppression (a harness that let frames run while claiming to be hidden would
test nothing), the second against a fake WebSocket and a manual clock so the
backoff is steppable. `pytest tests/` picks up every `tests/*.test.js`
automatically via `tests/test_frontend_js.py` (skipped if `node` or
`node_modules/` is absent; `npm install` provides jsdom, the repo's only JS
dependency and a test-only one).

Every mechanism above is pinned by a **mutation sweep**, `tools/mutate.py`
(targets `chat` and `app`; the `reconnect` target of section 6d rides the same
tool, which is why it runs pytest as well as node): each of 23 mutations undoes
one part of the fix, and all 23 are caught. That
sweep is the reason this section can claim the tests verify the fix rather than
merely accompany it. It has repeatedly been the thing that found the real bug —
a vacuous gap test, a missing second `visibilitychange`, the stranded-visible-latch
hole above, a focus-triggered `/connect` nobody asked for, and a three-valued
`_shutdownCause` whose extra values no branch could ever read (replaced by the
boolean the logic actually used). **A mutation that survives is a fact about the
code, not a gap in the tests to paper over:** twice now the right answer was to
delete the unverifiable code rather than write a test that could not distinguish
its presence from its absence.

### Display options are backend state, not frontend state

Anything that changes how the stream *looks* (`collapse_tools`,
`show_thinking`, `collapse_threshold`, `max_dom_messages`) lives in `State`,
ships inside `state_to_status_dict()`, and is applied by `status.js` calling a
setter on the owning module. That gives one source of truth for every tab and
makes each option settable three ways — CLI flag, slash command, and (where
one exists) a UI control — without any of them drifting apart. An immediate
command returning `state_updates` triggers a full `status_update` broadcast, so
a change made in one tab lands in all of them.

`collapse_tools` additionally lets `localStorage` win over the backend, because
it has a checkbox and a per-browser preference should stick. `show_thinking`
has no checkbox, so the backend is authoritative and there's no override.

**Ordering constraint:** `_send_initial_state()` must send `status_update`
*before* `history`. Replayed thinking blocks are rendered by the same
`_addThinking()` as live ones, so if history arrived first it would render
against a stale default and `--show-thinking` would appear not to work on
resume.

> `--show-thinking` spent a long time as dead config — parsed, stored on
> `State`, read by nothing — precisely because the `State → status dict →
> `status.js` → `chat.js`` chain was never completed. `tests/test_show_thinking.py`
> asserts every hop, including grepping the frontend for the setter call.

---

## 8. The lobby

The `☰ Sessions` overlay browses every session on the hub: those running live
(cwd, viewer count, busy dot) and recent on-disk ones, scanned across **all**
Claude accounts on the machine and tagged with their account so a session
started under a different `CLAUDE_CONFIG_DIR` still reopens correctly.

**Wire protocol** — client → server: `list`, `lobby_watch {on}`, `detach`,
`attach {rid}`, `open {session_id, cwd, account}`, `new {cwd}`, `close {rid}`,
`move_list`, `move_do {config_dir, new_name, cwd}`. Server → client:
`session_list {running, recent, recent_pending}`, `attached {session}`,
`session_closed {rid, message}`, `lobby_notice {message}`,
`session_elsewhere {session_id, holder_pid, started, port, cwd, account}`.

While the overlay is open the socket joins `_lobby_watchers` and gets live
`session_list` pushes.

**Opening a session already live in another hub.** A machine often runs several
hubs (one per `--cwd`). Before `open` forks a runtime to resume a session, it
asks `proc_guard.find_foreign_session_holder(sid)` whether another `claude` is
already resuming it. If so, resuming here would only hit `DuplicateSessionError`
and park unable to run (§ the connect-blocked path), so the server sends
`session_elsewhere` instead of creating the doomed runtime. When the holder's
parent is one of our servers, the payload carries that hub's listening `port`;
`lobby.js` offers to go there (a same-tab nav to `http://host:<port>/?open=<sid>`,
which attaches to that hub's existing runtime). When the holder isn't a
focusable hub (a `claude --resume` in a terminal, an orphan), `port` is `None`
and the UI explains rather than offering a dead link. `--allow-duplicate-session`
bypasses the check. Note this guards the **lobby** `open` path; a duplicate
reached via `POST /api/session/launch` (a second launcher) still parks with the
connect-blocked message rather than redirecting — a possible follow-up.

### `/move` — a session's destination is an account *and* a directory

The command began as `/move`: "copy this conversation into another Claude account and
carry on here". Copying it into another **project directory** is the same
operation along a second axis, so it is the same command: pick an account
(the current one included), then a directory (the current one included), then
a name. Leaving either alone gives you each single-axis move; changing both
does them together. `move.js` drives it, `server._do_move` performs it.

**It was called `/switch` until the directory axis existed**, because "switch"
named the account and nothing else — and for most commands the one-line entry
in `/help` is the only documentation anyone reads. The rename went all the way
through: the module (`Move`), its file, its CSS classes, the `move_*` wire
messages and the `movecopy`/`moveserver`/`moveui`/`movecmd` mutation targets.
No alias was kept.

That was a deliberate reversal. The first pass kept `/switch` working and left
the internals alone, on the grounds that renaming them would churn `server.py`,
`app.js`, the stylesheet, three test files and every mutation anchor for no
user-visible gain — and wrote *this paragraph* to explain the mismatch.
Needing a paragraph to explain why a name is wrong is the argument for fixing
the name: the explanation is a permanent tax on every future reader, while the
churn is paid once and is fully covered by the suite.

The directory half is the harder one, because a session's directory is not
merely where it runs — it is encoded in three places that have to agree:

1. **The project slug** the JSONL lives under, `<cfg>/projects/<slug>/`. The
   CLI derives it from its own cwd, so a copy filed under the *old* slug is
   invisible to `--resume` from the new directory. A move therefore files the
   copy under `_sanitize_cwd(dest_cwd)`. Staying put deliberately keeps
   `src_dir.name` instead: a session located by the cwd-sniffing fallback can
   live under a slug that no longer matches its cwd, and recomputing would
   relocate it as a side effect of an *account* switch nobody asked to be a
   move.
2. **The `cwd` field on every record.** `sniff_session_cwd` reads it to decide
   which directory a session belongs to, and the hub's "switch cwd to match the
   session's recorded cwd" step on resume would otherwise drag the session
   straight back where it came from.
3. **`gitBranch`**, which named a branch in a tree the copy has now left.
   Recomputed from the destination (`git_branch_for`), `""` when it isn't a
   repo — and `""` is a real answer, not "leave it alone".

So `copy_session_file` grew `new_cwd=` / `new_branch=`, and one rule that
matters more than any of them: **only the top-level fields are rewritten.**
Paths inside `message` and `toolUseResult` are a record of what happened — that
a Read really did read a file under the old path is a *fact*, and rewriting it
would falsify the transcript rather than relocate it. The rewrite also applies
to *every* record rather than only those matching the old value, because a
session that used `/cwd` mid-flight has records from two directories while the
copy lands in exactly one project dir; leaving the odd one out would make which
directory "wins" depend on record order, since `sniff_session_cwd` reads only
the first few.

Two smaller decisions, both load-bearing: a destination that does not exist is
**refused** rather than created (a typo and a genuinely new directory look
identical from here), and the directory field is free text with the directories
Claude already knows about offered as one-click fills — the suggestion list is
a convenience, not the set of legal destinations, since moving a session
somewhere Claude has never run is exactly the interesting case.

Pinned by `tests/test_move_directory.py` (29), `tests/move_overlay.test.js`
(16, jsdom) and four mutation targets: `switchcopy` (`copy_session.py` × 8),
`switchserver` (`server.py` × 15), `switchui` (`static/move.js` × 13) and
`movecmd` (`static/commands.js` × 5, how the command name is matched).
`switchserver` found three weak tests on its first run — a slug test whose
fixture made both implementations agree, a spelling test whose input
`Path.resolve()` had already canonicalised before the comparison it was meant
to exercise, and a dedupe test whose newest entry was also its last — all three
rewritten from the survivors. The browser half is worth testing separately
because a missing `cwd` field in `move_do` is, server-side, indistinguishable
from a deliberate "stay where you are".

#### `copy_session.py` no longer imports `textual`

Its first section has been headed *"Discovery / parsing (no TUI dependency —
importable & testable on its own)"* for as long as it has existed, and a
module-scope `from textual import work` further down made that false. The cost
was invisible until the hub moved to an interpreter without `textual`
installed: `_recent_disk_sessions` imports `discover_claude_dirs` inside a
broad `except Exception`, so the **lobby silently narrowed to a single
account**, and `/move` — which has no such guard — failed outright.

The TUI now lives in `copy_session_tui.py`; `copy_session.main()` imports it
only when the wizard is actually going to run, and reports the missing package
by name rather than raising. A missing TUI dependency should cost you the TUI
and nothing else. `textual` is in `requirements.txt` with that scope written
next to it.

**Every session is reachable as a URL, so the browser can open it in a tab.**
The cards were `<div>`s with click handlers, and a click handler has no "open
in new tab" — long-press offers nothing on a phone, and neither does
ctrl/middle-click on a desktop. The in-place switch was therefore the *only*
route to a session on mobile, so **one tab was all you could have** (reported
2026-09-14). The card and recent-item titles, and the ☰ Sessions control, are
now anchors carrying the URL the click handler would navigate to
(`?rid=` for a local runtime, `?open=&cwd=&account=` otherwise). The delegated
handlers `preventDefault()` a *plain* click so the existing in-place path is
unchanged, and deliberately keep their hands off a modified one
(`_plainClick`: ctrl / cmd / shift / alt / middle) — swallowing those would
take back the very capability the anchors exist to provide.

Handing the job to the browser rather than inventing a control: the affordance
already exists on every platform, is the one users already know, and works in
contexts a page-level button cannot (long-press, drag to a bookmark bar,
copy-link). A one-line hint in the lobby makes it discoverable on mobile, where
nothing else advertises it.

**The side panels on a phone.** Below 768px the sidebar becomes a fixed
overlay behind a toggle in the status bar, with a backdrop that closes it.
That mechanism was always correct — measured end to end with
`tools/probe_mobile_sidebar.py`: the toggle is on screen, tapping it puts a
350×915 sidebar in front, all four panels render. What was wrong was that
nobody could tell (reported 2026-09-14, "i can't see any of the side panes
including queued prompts"):

* it was an **unlabelled 30×25 cog** sitting next to a labelled "☰ Sessions",
  which is how it read — as decoration. It now says **"⚙ Panels"** and is a
  real touch target. Every other control in that media-query block had already
  been sized for a finger; this one was simply missed;
* **nothing gave a reason to tap it.** The status bar announces `bg wait (1)`
  out loud but has never mentioned queued prompts, so the one panel with
  something in it was the one panel with no outward sign. The toggle now
  carries the queue count when it is non-zero — the queue only, because a
  single number standing for several different things is a number you cannot
  act on, and hidden at zero, because a badge that is always there stops being
  a signal.

The investigation is worth recording because two plausible theories were wrong
and the probe killed both: the status bar does **not** overflow on a phone (the
mobile rules hide the labels and separators, so `scrollWidth == clientWidth`)
so the toggle cannot scroll out of reach; and a bare `/` landing in the lobby
rather than a session is *deliberate* — the hub's front door — not a failure
to attach.

**A tab's name is a claim, and it has to be given up.** `onAttached` stamps
`window.name = 'orch2-sess-<sid>'` so other tabs raise this one rather than
duplicating it. Two rules keep that honest, both learned from a tab that could
not reopen its own session (2026-09-16): `_focusOrOpen` treats `w === window`
as "navigate here", because browsing-context lookup by name resolves the
current context first and focusing the already-focused tab is a silent no-op;
and `showNotice` relinquishes the name, because a notice forces the lobby in
front, so the tab is demonstrably not showing that session any more.

**Desktop vs mobile.** On desktop each session gets its own tab, keyed by a
stable window name (`orch2-sess-<sid>`), so picking a session focuses its
existing tab rather than reloading it. Mobile browsers can't do either half of
that — `window.focus()` is a no-op on another tab, and `window.open('', name)`
won't find a tab opened outside this browsing-context group. So `lobby.js`
detects mobile (`navigator.userAgentData.mobile`, UA-regex fallback) and
switches the **current** tab in place over the existing WebSocket
(`attach` / `open` / `new`), with a busy notice and a 12 s watchdog for
feedback. `app.js` rewrites the URL to `?rid=<rid>` on `attached` so a refresh
returns to the same session instead of forking it.

Because the mobile overlay stays open across the switch and covers the chat,
every failure path in the lobby handler reports through `_lobby_error()`, which
sends a chat `system_msg` **and** a visible `lobby_notice` banner.

The lobby header also carries **⟳ Restart server** (spawns a replacement
process in its own console window, resumes the primary session, reloads the
tab) and **⏛ Shut down server**.

### A session running in another hub is running, not "recent"

`running` is built from `runtimes.values()` — **this process's** registry. The
hub reuses one server per account/port, so a machine driving several accounts
has several hub processes, and a session live in any of the others is absent
from `runtimes`. It fell through to the on-disk scan and was listed under
*recent* while actively working (reported 2026-09-03: three sessions, held by
hubs on ports 63978/51842/51843 while this hub had 8420).

`proc_guard.map_foreign_session_holders()` answers "who else holds a session"
in **one** `process_iter` walk. The pre-existing `find_foreign_session_holder()`
scans per session id, which is right for the one-shot open path and hopeless
for a list: a cold walk measured 13 s on this machine (0.4 s warm), so forty of
them per 2-second lobby tick was never viable. Parent lookups (which give the
hub's port, at the cost of a `net_connections` call) are cached per holder pid.

`_session_list_payload` promotes matching disk sessions into `running` with
`foreign: True`, plus `port`/`holder_pid`/`started`. Two guards: the scan is
cached for `_HOLDERS_CACHE_TTL` (8 s), and anything in this process's own
`runtimes` is excluded even if it appears in the map — `proc_guard` already
skips our own process tree, but a *cached* map outlives the scan that made it
and must never be able to relabel one of our own sessions as somebody else's.

In the UI the card is dashed and dimmed with an `other window :<port>` tag, and
has **no ×** — this window can neither drive nor close it. Clicking routes
through the same `?open=<sid>` path a "recent" row uses (shared
`_openSessionById`, so the two cannot drift), which the server answers with
`session_elsewhere`. That mattered: the click handler bails on `!rid`, and a
foreign card has no rid, so without an explicit branch the card would silently
do nothing — the same dead end the feature exists to remove.

**`rid: None` is load-bearing, and it bit twice.** The click handler above was
the first time. The second was `m.rid === _currentRid`, which decides the
"current" badge: `_currentRid` is also `null` in a tab that hasn't attached to
a session — a standalone `?lobby=1` tab — so `null === null` labelled every
foreign card **current**, the exact opposite of what the same card said two
badges later. It is now `!!m.rid && m.rid === _currentRid && !m.foreign`, one
value used for both the badge and the highlight so they cannot disagree. The
`!m.foreign` clause is redundant against today's payload and survived its
mutation; it was kept and tested rather than trimmed, because rids are per-hub
and sequential, so two hubs on one machine routinely mint the same `s3` and the
day a foreign card gains a rid the bug returns against a session in another
process entirely.

**A card's title outranks its annotations.** `.lobby-card-title` was `flex: 1`
— a *zero* basis — beside badges at `flex: 0 0 auto`, so the badges took their
natural width and the title got the remainder. Next to `other window :63978`
in a ~275px card that was almost none, and "Good Photons" rendered as "Goo…".
The title is now `flex: 1 1 auto; min-width: 0` and carries a `title=` tooltip,
and the foreign tag left the header entirely — the dashed border and dimming
already signal "not yours" pre-attentively, so the tag is detail rather than
identity. The header holds only the dot, the title, the "current" badge and
the ×.

The tag did not go into the meta row either. It is a bordered pill, not a field
in that row's `a · b · c` run, and at ~120px it pushed the run past the card
width and wrapped it — stranding a separator at the end of the first line with
nothing after it. It gets its own `.lobby-card-tags` row between the cwd and
the meta line, the same shape the countdown badge already uses, emitted only
when there is a tag to put in it.

### A hub says when it is running code that has since changed

Python is read once at import, so a server goes on executing whatever was on
disk when it started — for as long as it lives, and silently. That is normally
harmless and occasionally very expensive: a background-task bug was reported
against a hub whose process had started **2026-09-01 22:59:30**, one day before
that exact bug was fixed. The decisive evidence was a log line
(`cleared N stale bg task(s) on reconnect`) that exists nowhere in the current
source — it could only have come from a process still running the pre-fix
version. Six days of a fixed bug still happening, with the fix on disk unread.

`snapshot_sources()` records the mtime of every loaded **project** `.py` once
`_deferred_bridge_startup` has finished importing — deliberately there and not
in `lifespan`, because that function is what imports `sdk_bridge`, and a
baseline taken before it would never notice a change to the file holding most
of the logic. It is in the `finally`, so a hub whose bridge failed to start
still gets a baseline; that hub is still running code, and is the case where
someone is most likely to be editing and restarting.

`stale_sources()` re-stats and reports what has moved forward. Two exclusions
keep it from crying wolf, and both are pinned:

- **Only project files.** A `pip install` does not make *this* hub stale, and
  third-party files change for reasons the operator did not cause.
- **Only `.py`.** JS and CSS are re-read per request, so a browser refresh
  picks them up. Telling someone to restart the server for a CSS edit teaches
  them to ignore the notice, which costs more than the notice ever saves.

A module imported *after* the snapshot is baselined on first sighting rather
than flagged: a genuinely late import is running current code, so calling it
stale would be exactly backwards.

It rides `session_list` — including the fast landing payload, before the disk
scan, because "this hub is running old code" is what you want *before* you
start reading its session list for clues — and `lobby.js` renders it as a
warning band directly above the lobby's notice bar, next to ⟳ Restart, which
is the entire remedy. Not dismissable: it is a fact about the code you are
looking at, and it stops being true the moment you act on it.

Pinned by `tests/test_stale_sources.py` (16) and the `stale` mutation target
(`server.py` × 15), plus the banner half in `lobby_running_card.test.js`. The
sweep's one survivor is worth recording: removing the *call site* of
`snapshot_sources` was invisible, because every test established its own
baseline. Without it the first check baselines whatever it finds and nothing is
ever stale — the feature silently absent rather than broken, which is the same
shape as the bug it exists to catch.

### Ask the hub that owns it, and say so when you can't

A process scan can see *that* something holds a session and *where* it runs.
It cannot see whether a turn is in flight, how many tabs are watching, or what
the session has since been renamed to. `_foreign_running_entry` filled those in
with `busy: False` / `viewers: 0`, and the lobby rendered them as an idle dot
and a viewer count — **placeholders presented as measurements**. A session
hammering away in another window looked idle, which is the opposite of what the
foreign card exists to tell you.

The hub that owns the session does know, and it already has the answer in
memory. `GET /api/running` returns this hub's `rt.meta()` list; peers call it.
(`/api/status` is not a substitute — it reports the *primary* session, not the
registry.) `_probe_foreign_hubs` asks **one request per peer hub**, not per
session, and merges the answer in: real `busy`, `viewers`, `last_activity`, and
a live `title` that beats the disk one because a rename reaches the owning
hub's memory before it reaches the JSONL.

Three guards, because the thing being asked may itself be the thing that's
wrong:

- **Loopback only** (`127.0.0.1`), which is also what lets it skip the
  external-auth middleware — no credentials, and it can never leave the machine.
- **A bounded deadline** (`_HUB_PROBE_TIMEOUT`, 1 s, well under the 2 s lobby
  tick). A peer that accepts the connection and never answers would otherwise
  hold the executor thread and stall the session list for every tab here.
- **A failure backoff** (`_HUB_PROBE_FAIL_TTL`, 30 s, against a 2 s success
  TTL). Without it one wedged peer adds its timeout to *every* lobby tick — the
  lobby made slow by exactly the condition it is trying to report. The two TTLs
  differ because they answer questions of different volatility: *who owns what*
  changes when a session starts or stops, *is it working* changes by the
  minute, and *is this hub dead* changes rarely.

When the answer can't be had — no port at all (a bare `claude --resume` in a
terminal has no API), the hub didn't answer, or it answered without this
session (a closing-session race) — `busy` and `viewers` are **`None`**, and
`live_known` is false. `None` is not `False`: the card renders a third dot
state, a hollow ring, whose tooltip distinguishes *"the hub on port N didn't
answer"* from *"that isn't an orchestrator2 hub, so there is nothing to ask"*.
An empty answer (`{}`) stays distinct from no answer (`None`) for the same
reason — an idle hub and a dead one are not the same fact.

The meta row is assembled from a list and joined with separators, so an absent
part — a foreign session's missing rid, or an unknowable viewer count — takes
its separator with it instead of leaving a dangling `·`. Note the viewer check
is `typeof m.viewers === 'number'`, not `m.viewers || …`: `0` is falsy, and
"nobody is watching" and "we have no idea" mean opposite things.

Pinned by `tests/lobby_running_card.test.js` (28), the `lobbycard` mutation
target (`static/lobby.js` × 23), and on the server side
`tests/test_foreign_running_sessions.py` (30) with the `foreign` target
(`server.py` × 25). jsdom does no layout, so the markup tests
cannot tell you whether the title actually *fits* — which was the entire
complaint. `tools/probe_lobby_card.py` renders the real cards with the real
CSS in Chromium and measures the title box; `--legacy` undoes the three fixes
in memory to show the measurement reproduces the report (title box **40px**
for text needing 91px, i.e. "Goo…", against **237px** after).

### Closing one session (the card's ×)

Each running card carries a **×** that stops *that* session and leaves the hub
and its other sessions running (`close {rid}` → `close_runtime()`, also exposed
as `POST /api/session/close {rid}` for scripts). This exists because closing a
browser tab deliberately does **not** stop a session — the server keeps it (and
its `claude.exe`) alive so you can come back — and until now the only ways to
stop one were the idle timer, killing the whole hub, or hunting for a PID.

Three things separate it from the idle teardown it shares plumbing with:

* **It applies to the primary session.** `_teardown_runtime` refuses
  `_default_runtime` on the automatic path (an idle primary must survive the
  night), so the close passes `force=True`. Refusing there would make the
  button useless in the commonest case — a hub launched in one folder hosting
  exactly one session. The primary is **demoted** instead: `_default_runtime`
  and its two legacy module-level shadows (`state`, `bridge`) are set to
  `None`, which every consumer of them already guards for, and the hub carries
  on with no primary.
* **It can fire while tabs are watching.** `_evacuate_viewers()` sends each
  viewer `clear_screen` + `session_closed` and then `_enter_lobby()`, *before*
  `bridge.stop()` — stopping a CLI takes seconds, and a tab still showing (and
  accepting typing into) a session being killed is the worse failure. `app.js`
  drops the stale `?rid=` from the URL so a refresh lands in the lobby cleanly.
* **`_maybe_start_idle_timer` skips unregistered runtimes.** `_enter_lobby`
  arms the idle timer of the runtime each tab is *leaving* — during a close
  that is the runtime being torn down, which would leave a live task holding a
  dead runtime.

The session's JSONL is untouched, so a closed session reopens from **Recent**.

---

## 8a. Session titles (`session.py`)

A session's display name comes from `custom-title` (a user rename) / `ai-title`
(an auto summary) records appended to its JSONL. Two things make this harder
than "read the last record".

### Reading: the incremental title index

A rename done early, followed by heavy use, buries the `custom-title` deep in a
file that can be hundreds of MB, so head/tail peeking is wrong — correctness
needs a full scan. Doing that for every session on every lobby refresh froze
the UI for seconds. Since JSONLs are append-only, `_resolve_title_raw` scans
each file once, records `(size, custom, ai)` in `~/.orchestrator2_title_index.json`,
and thereafter rescans only the bytes appended since — which is exactly where a
new rename lands. The index is a pure cache: deleting it costs one rescan.

### Writing: renames are pinned, because we are not the only writer

**We cannot win an append race against the CLI, so we don't try.**

Claude Code holds the title in memory (`currentSessionTitle`) and re-appends it
at EOF from `reAppendSessionMetadata()` on **every compaction** and again on
**resume**. It absorbs a rename written by anyone else only if that record is
still inside the last **64 KiB** of the file when it next looks
(`readFileTailSync` / `LITE_READ_BUF_SIZE`) — and the resume path stamps with
`skipTitleRefresh`, so it doesn't look at all. On a busy session 64 KiB is
seconds wide, so a `/rename` usually scrolls out of the window before the CLI
reads it; the CLI then re-stamps the *old* title at EOF forever, and "last
record wins" hands it the vote. Measured on a real 845 MB session: **933
`custom-title 'OS'` records against 46 `'OSc'`**.

So `write_session_title` records the intent on our side as a **pin** in
`~/.orchestrator2_renames.json` — `{title, stale[], at}` keyed by JSONL path:

* `title` — what the user asked for.
* `stale` — every title we have previously seen or set for that file,
  accumulated across a chain of renames (A → B → C keeps both A and B).

`_apply_rename_pin` overlays it on the raw scan result: a JSONL title in
`stale` (or absent) means the CLI is re-stamping something the user already
replaced, so the pin wins. A title we have **never** seen is a genuine newer
intent — someone typed `/rename` inside the CLI — so we yield to it and delete
the pin. Known limitation: renaming *back* to a previously-used title from
outside orchestrator2 is indistinguishable from a stale re-stamp and gets
overridden; the escape hatch is to rename from orchestrator2.

Unlike the title index, the pin file is the **only** record of a user decision,
so it is written through immediately rather than batched behind a dirty flag.

Two smaller rules fall out of the same interop:

* The manual-append fallback serialises with `separators=(",", ":")`, byte-identical
  to the CLI's `JSON.stringify`. Its external-writer check is
  `line.startsWith('{"type":"custom-title"')` — whitespace-sensitive — so a space
  after the colon would defeat it outright.
* The SDK `rename_session` path is preferred when the target account matches the
  process env, because it goes *through* the running CLI and updates that
  process's in-memory title, so its next compaction stamp agrees with us. We pin
  anyway: that process gets replaced on resume and loses the memory.

---

## 8b. Session-file integrity (`session.py`)

**A session JSONL is a tree, and we read it as a list.** Every record carries a
`parentUuid`; the CLI rebuilds the model's context by walking that link
*backwards from the last message*, so what the model can see is one root-to-leaf
chain. We render history by walking the file *top to bottom*, so the browser
shows every record in it. While the chain is intact the two agree and the
distinction never surfaces. When it breaks they diverge **silently, and in the
worst direction**: the transcript still looks complete, so nothing appears
wrong, while the model has actually lost everything before the break.

That is not hypothetical. A run of NUL bytes in the middle of a session file
stops the CLI's reader there, so every resume afterwards re-attached to the last
record it could parse and grew a **new branch** off it. Three weeks later one of
those files had 27 sibling branches hanging off a single July-24 node — with a
browser transcript that showed all of it.

**We were causing the holes.** The first explanation here was "a lost write,
one machine-level event"; it was wrong. All three holes in the 1.29 GB SlateOS
session sat *immediately before* a 93-byte `custom-title` record — the only
thing this process ever appends to a session file — and each was at an idle
boundary where a CLI had been shut down. A CLI killed mid-write leaves the file
size extended past its valid data, so the tail reads back as zeros; our
`O_APPEND` then wrote *after* the zeros and moved them from the end of the file
into the middle of it, where they do permanent damage.

`heal_tail_before_append()` closes that, and is called on **both** title-write
paths. Before appending it drops a trailing NUL run and a trailing half-written
record; both are at EOF so nothing can reference them, and a *complete* last
line is never touched. Note this is **not** fixed by routing the write through
the SDK's `rename_session` — that helper is `os.open(O_WRONLY | O_APPEND)` +
`os.write` of the same 93 bytes, so it lands in exactly the same place.

**`total − chain` is not the damage, and reading it that way is the trap.** A
compaction re-roots the conversation: the `compact_boundary` record is written
with `parentUuid: null`, a brand-new root, so everything before it is off-chain
*by design*. These files reach hundreds of roots — 361 of the raytracer's 362
are compact boundaries, the 362nd being the session's original first prompt — so
a perfectly healthy long session also shows a tiny chain and an enormous
"stranded" count. The first version of this code reported that artifact as loss:
it called a 99.7% catastrophe on a session that had abandoned 1.4%, and would
have raised a 318,916-record false alarm on a session that was fine. The honest
measure is **work abandoned at a fork** — sibling subtrees hanging off a node on
the live chain, which is what a re-attaching resume actually leaves behind.

Compaction is also what *heals* this: once a session compacts, new work chains
from the fresh root and the corrupt region stops mattering. So the question is
never "does this file contain a hole" but "does the chain the model is on right
now fork away from abandoned work" — which is why `check_session_integrity`
requires both a hole *and* a non-zero abandoned count before it says anything.

The failure is therefore a **resume-point** bug, not data loss. Barely anything
was destroyed: each abandoned branch is just the opening records of a session
that then compacted, and its work lives on in the segments that follow. What
actually hurt was that every resume opened with three-week-old context instead
of continuing from the newest segment.

So `check_session_integrity()` runs on **every connect**, not at startup only.
Every one of `/model`, `/effort`, `/thinking`, `/connect` and `/clear` takes
effect by tearing down and re-resuming the CLI, and *a resume is exactly the
moment a broken chain costs the model its history* — which is why the reports
that led to this all arrived as "I changed the model and it forgot everything".

The scan is index-backed for the same reason the title index is: these files
reach hundreds of MB and corruption, like everything else, lives in the
append-only prefix, so a byte range once known clean is never read again.
`find_nul_holes()` does a chunked raw-byte scan from the last clean offset and
returns the start offset of **every** NUL run it finds, not just the first — the
SlateOS file had three, and a scanner that stopped at one reported the file as
half-fixed forever. Runs that straddle a chunk boundary are stitched back
together rather than counted twice. Only if the scan hits does
`analyze_session_chain()` walk the tree.

Findings are cached in `~/.orchestrator2_integrity_index.json`, keyed by a
`(size, mtime_ns)` fingerprint, and a cached verdict — positive *or* negative —
is only reused while that fingerprint is unchanged. Size alone is not enough:
`--relink` repairs a file by rewriting one `parentUuid` in place, and both uuids
are 36 characters, so the file is fixed without changing length by a single
byte. Under a size-only key the tool would have kept warning about a session it
had already repaired. When the file has only *grown* the previously-scanned
prefix is still trusted and the known holes are carried forward, so the
incremental scan stays incremental; anything else forces a full re-walk. Like
the title index it is a pure cache — deleting it costs a rescan, nothing more.

`analyze_session_chain()` reports `chain` / `stranded` / `rooted`, the
`truncated_at` timestamp of the branch point, and the branch count. Two parsing
rules matter and both are defensive:

* A record's own uuid is the **last** one on its line (the CLI writes
  `parentUuid` in the head and `uuid` at the tail), so a payload that quotes a
  uuid cannot hijack a record's identity.
* The leaf is the last non-sidechain `user`/`assistant` record — a subagent
  transcript or a trailing `custom-title` at EOF must not be mistaken for the
  end of the conversation. (The trailing `custom-title` case is real and
  observed; the other two are guards.)

`SDKBridge._report_session_integrity()` broadcasts the finding **once per
bridge**, not once per reconnect: the condition is permanent and cannot be
fixed mid-session, so repeating it on every `/model` switch would just bury the
transcript. It runs in a thread off the connect path and swallows its own
exceptions — it is a diagnostic and must never be able to take a session down.

The related guard that catches the *other* failure mode — the SDK silently
starting a fresh session instead of resuming the one we asked for — compares
`state.expected_resume_sid` against the id in the init message. This used to be
gated on `first_init` as well, but `init_seen` is set once and never cleared, so
after the very first init it could never fire again: disabled for precisely the
reconnect case that matters most. `_make_options()` sets
`expected_resume_sid` fresh on every connect, so checking it every time is
correct.

### Repairing a damaged session — `tools/repair_session_jsonl.py`

The detector only reports; the repair is a deliberately separate offline tool,
run against a session the CLI does **not** have open. It has two modes, both
of which take a backup, write a temp file, verify it, and only then
`os.replace()`.

Default mode fixes *lines*: it drops NUL runs and unparseable fragments. That
is enough when the damage is confined to bytes.

`--relink` fixes the *chain*, which is what actually cost history. The CLI
reconstructs a conversation by electing a leaf — the newest non-sidechain
terminal record, ties going to the first one in the file — and walking
`parentUuid` back to a root (verified against `sessionStorage.ts`:
`loadTranscriptFile` / `findLatestMessage` / `buildConversationChain`). The
failure mode is a **misattached tail**: a handful of records written after the
newest compact boundary but hanging off an ancient node. Being newest they win
the leaf election, so every resume opens five-week-old context while the
current segment sits unread. `--relink` finds the newest `compact_boundary`,
takes its component as the live segment, and re-parents each stray head onto
the segment's newest leaf, chaining multiple strays in write order. On the real
SlateOS file that was **one** `parentUuid`, out of 453,845 lines.

What it deliberately does **not** do is splice the older abandoned branches
back in. They are not loss — each is the opening of a session that then
compacted, and its work survives in the segments that follow. Chaining all 15
would push ~3,700 stale records into the live context, and the first thing the
CLI would do is compact them away, taking the current state with them.

The byte patcher is surgical: it edits `parentUuid` in place and re-parses the
result, requiring that every other field is byte-identical, so a `parentUuid`
nested inside a `toolUseResult` payload cannot be mistaken for the record's own
field. If no occurrence passes that check it raises rather than guess. `--apply`
is opt-in; without it the tool prints the plan and touches nothing.

---

## 9. Auth & networking

The server binds `0.0.0.0`, so any device on the LAN can reach it. `auth.py`
classifies each connection as LAN or external; external clients require a
password (HTTP Basic, which sets a cookie the WebSocket then presents; a
`?password=` query param also works). LAN clients pass without credentials.

### External access is off until you turn it on, with a password

`_resolve_external_auth()` reads **two independent knobs** and returns an
`ExternalAuthPolicy(enabled, password, warn)`:

| switch | password | result |
|---|---|---|
| off (**default**) | — | refused, silently — this is what the user asked for |
| on | set | allowed |
| on | missing/blank | **refused, with a warning** |

The switch is `--external-access on|off` / `ORCH2_EXTERNAL_ACCESS`; the secret
is `--external-password` / `ORCH2_EXTERNAL_PASSWORD`. A whitespace-only value
is not a password (an empty env var and a fat-fingered flag both land there).

**There is no built-in default password.** There used to be —
`DEFAULT_EXTERNAL_PASSWORD = "uncommon11"`, applied automatically whenever the
flag and env var were unset — which meant every install was reachable from the
internet with a secret published in the source. A password nobody chose is not
a password.

The third row is the point of splitting the knobs. "On with no password" has to
be *reachable and refused*: failing open would be indefensible, and failing
silently would leave the operator with remote access that doesn't work and no
clue why. So it warns (startup log, `/status`, and the body served to the
refused client) and stays shut.

Discoverability is deliberate, because a refusal the user can't act on is just
a wall: `EXTERNAL_HOWTO` is defined once and quoted in all three places, so the
instructions can't drift apart.

### Only a wrong password is a failed attempt

The brute-force throttle counts failures *globally* (per-IP would be defeated
by a botnet). It used to increment on **any** request without valid
credentials, which is not the same thing as a guess: one ordinary first visit
fires a document request, a dozen static assets and — via `app.js`'s 20-try
reconnect loop — a stream of WebSocket upgrades, all credential-less. Five of
those armed the lockout, so the user was told *"Too many failed attempts. Try
again in 9s"* while typing the correct password for the first time, and a
reload only appeared to fix it because by then the auth cookie existed.

Now a failure is recorded only when a credential was **presented and wrong**
(`if auth or pw_param is not None`). A request carrying none gets a plain 401
challenge and costs nothing. The protection is unchanged — a real guess always
presents something.

A **valid cookie is also honoured during a lockout**. It proves a past success
rather than a guess, so accepting it gives an attacker nothing, and it removes
the one real bite of a global counter: a stranger guessing from anywhere could
otherwise lock the owner out of their own remote session for up to five
minutes.

---

## 9a. Cross-account agent comms (`agent_comms.py`, `tools/agents.py`)

Implements `specs/agent-comms-spec.md`. Several Claude sessions work one
machine and one repository at once and **cannot address each other**, because
the session registry is scoped per `CLAUDE_CONFIG_DIR` and they run under
different accounts. That scoping *is* the bug, so the store here is
deliberately machine-wide and outside any config directory:
`%LOCALAPPDATA%/orchestrator2/agents.db`, SQLite in WAL mode because concurrent
writers from different accounts are a hard requirement.

**Identity is assigned, not derived.** Every derivation breaks in some
topology — by account when two agents share one, by cwd when two share a
directory, by pid when a session restarts — so it is a name, like a hostname,
set with `--agent-name` / `ORCH2_AGENT_NAME` and stable across restarts.
`resolve_identity()` implements the spec's table, and the row that matters is
the refusal: a fresh session in a directory with several registered identities
**refuses and lists them** rather than guessing. Adopting the wrong one
inherits another agent's message queue and halt state, and neither agent can
detect it — the wrong one is told to stop and the right one never is. A
refusal is not fatal: the session runs, it is simply not addressable, and says
so.

**Delivery is the hard part, not transport.** A file-based scheme already
existed and still failed: a message sat in the addressee's own working tree for
two days and it still lost a 7783-second job. So pending traffic is appended to
`state.queued_prompts` by `SDKBridge.poll_agent_comms()`, driven from the
status ticker. Queuing rather than injecting is what makes the spec's "at turn
boundary, not mid-turn" true for free — a queued prompt is drained by
`_between_turns` at a turn's end and by `_await_next_prompt` when idle, shows
in the queue panel, and can never land in the middle of a half-finished edit.
The ticker (not just `_between_turns`) drives it because an *idle* agent is at
a turn boundary too; delivering only between turns would leave a halt
undelivered until the agent happened to do something.

**Halt is not a message.** Informational input gets deprioritised by an agent
mid-task, so the enforcement primitive is shared state with two layers: soft,
announced once per agent as a queued prompt; and hard, via

```
python tools/agents.py check-halt --scope repo    # 0 = clear, non-zero = halted
```

which an expensive operation calls before starting. The system never tries to
classify which processes are long-running — the operation about to spend three
hours is the one that knows, and it alone knows where its safe abort points
are. It is cooperative: something that does not check cannot be stopped, and
that limit is deliberate.

Two asymmetries are load-bearing. Registrations and messages **expire** (a
crashed session listed forever would make identity adoption unsafe); a halt
**never** does, because one that timed out would clear itself in the middle of
the maintenance it protects. And announcing a halt to an agent must not consume
it: the soft layer is per-agent, the hard layer stays in force until lifted
explicitly.

`repo` scope is keyed on git's **common** directory, not the toplevel, so all
worktrees of one repository share a halt — which is the motivating case, three
lanes in three worktrees.

### Surfacing: how an agent finds any of this

`specs/agent-comms-addendum-surfacing.md` answers a question the spec did not,
and its two halves pull opposite ways.

**Messaging gets no new tool and no documentation.** The description already
shipped to every agent says `ListAgents` lists "other local Claude sessions on
this machine" — the capability was advertised, only the implementation was
account-scoped. Inspection (not assumption) found the mechanism: the CLI
advertises itself in `<CLAUDE_CONFIG_DIR>/sessions/<pid>.json` plus a
`<pid>.<hash>.key`, then peers talk over a **machine-global named pipe**
recorded in that file. The transport was never account-scoped; only the
directory listing was. `mirror_peer_registries()` copies each *live* session's
advertisement (and its key) into every other config directory, driven from the
ticker.

The sharpened reason this matters: **an empty `ListAgents` is
indistinguishable from "this feature does not exist here."** The agent that hit
this got one negative answer, never tried `SendMessage` at all, concluded the
capability was absent and spent an hour building a file-based workaround. No
quality of tool description compensates — by then the agent has its answer.

Verified by observation, which the addendum insists on over reasoning about
descriptions: `ListAgents` went from **1 peer to 4**, and a `SendMessage` from
an account-c session was found in the **recipient's own transcript** under
account-b — queued at `22:14:01.677Z`, processed at `22:14:07.372Z`, i.e. its
next turn boundary.

Two invariants keep the mirror safe. Only *live* sessions are copied
(`_pid_is_a_live_claude` requires both that the pid exists **and** that it is a
claude process, since pids are recycled), and the reaper removes only files
recorded in its own manifest — after a source disappears a copy is
indistinguishable from an original by inspection, and deleting a real
advertisement would unregister somebody's working session, the one outcome
worse than the bug.

**Halt gets exactly one tool**, in `agent_tools.py`, exposed as an in-process
MCP server (`agent-halt`) merged into `mcp_servers` alongside any the user
configured. Checking deliberately stays a CLI: it must work from a bare shell
with no client running, or every script wanting to be well-behaved would have
to boot an agent to ask permission. The descriptions state the *occasion*
("use this when you need every agent quiescent — a migration, a shared-file
repair"), because one that only says "raises a halt" gets read and not acted on.

Only one line about this belongs in an instruction file — the one a tool
description cannot carry, about *when* — and it lives in the projects-level
`CLAUDE.md`.

### The five improvements from a day of real use

`agent-comms-improvements-checklist.md` was written after four sessions used
the registry for an afternoon. Each item is a defect that cost that day
something, and the fixes are shaped by what it cost:

1. **Attributes in the listing.** Name, kind and start time were all it gave,
   so one agent was identifiable only because it messaged first and another
   was found by elimination on a timestamp — the message sent to it framed
   defensively ("ignore this if you are not lane B"). Worse, two sessions
   claimed the same lane and made the same one-line fix, one on a superseded
   tree, and nothing could detect it. Registrations now carry free-form
   `labels` (`--agent-label k=v`, repeatable), indexed by cwd, and
   `agents.py list` prints them. The registry **never interprets** them —
   "lane" is the project's word. `list` also states a same-directory collision
   outright rather than leaving it to be spotted in a column: that is the case
   that cost the most and the one least likely to be noticed.
2. **Tell a copied session that it moved.** See §8, `/move`: the copy's
   absolute paths still *resolve*, so nothing reveals the move. A one-shot note
   is prepended to the copy's next prompt (`SDKBridge._with_session_note`) —
   to the model, not the browser, because the model is the one holding stale
   paths.
3. **Assignable, stable identity.** Already the design: `resolve_identity`'s
   §3.3 table, with the load-bearing refusal row.
4. **Delivery state.** A send returned success and nothing happened for
   minutes; the recipients had not taken a turn since restarting. From the
   sender's side that is indistinguishable from *read and deprioritised*, and
   **the two call for opposite responses — wait, versus escalate.**
   `message_state()` separates `queued` / `delivered` / `expired` and records
   `target_live` **at send time**, because the sender asks "was this ever going
   to arrive?" later, when the return value is long gone. There is deliberately
   no state beyond `delivered`: delivery is observable, comprehension is not,
   and a field claiming it would be the unverifiable signal item 5 warns about.
5. **Relayed authority is a non-goal, and stays one.** An agent relayed an
   operator instruction and the peer **correctly refused**; its reasoning is
   the reasoning — *waiting costs a delay until the operator types one line,
   acting on a mistaken relay works against a direct instruction and neither of
   us finds out for a while.* Every delivered message now carries that framing,
   so the refusal does not depend on the receiving agent being unusually
   careful. **No mechanism will be built that appears to solve it**: an
   "operator-originated" flag would not be verifiable either, and would be
   worse than nothing because it would look like authority. A test asserts
   `send_message` has grown no such parameter.

**Delivered traffic must be *visible*, not merely queued.** Peer messages are
appended to `state.queued_prompts`, whose container notifies persistence and
the worker poke -- and, until 2026-09-07, nothing that told the *browser*. So a
message was queued, drained at the next turn boundary and acted on while never
appearing in the pane it sat in; `SDKBridge._broadcast_queue`, written for
exactly this, had zero callers. The panel push is now a second
`add_listener` beside the poke, for the reason `PersistentDeque`'s own
docstring gives about the poke: hooking the container means a writer added
tomorrow gets it for free and cannot reintroduce the bug. It schedules rather
than awaits (`_fire` is synchronous and may run with no loop) and coalesces a
burst into one broadcast.

Pinned by `tests/test_agent_comms.py` (70) and the `agentcomms` (16) /
`agentcli` (4) targets of `tools/mutate.py`; the surfacing half by
`tests/test_agent_comms_surfacing.py` (27) and the `mirror` (8) /
`agenttools` (8) targets; the checklist and the panel push by
`tests/test_agent_comms_checklist.py` (30) with the `checklist` (9) and
`queuepanel` (5) targets.
`tools/agents.py --self-test`
carries 31 fixtures of its own, asserting **both directions** of every rule:
a checker with only positives passes for one that reports everything, and one
with only negatives for one that reports nothing.

## 10. Testing

`tests/` runs fully offline — no `claude` process, no network. Notable
coverage: hub registry / idle teardown / lobby protocol
(`test_multisession.py`), fan-out and coalescing (`test_ws_channel.py`),
external auth (`test_external_auth.py`), API-error and rate-limit detection,
session titles, cancellation, shutdown/subprocess reaping with *real* child
processes (`test_bridge_shutdown.py`), CLI-status surfacing and wakeup deferral
(`test_compacting_status.py`), pending-prompt delivery (`test_queue_poke.py`,
`test_queue_drain.py`, `test_idle_config_reconnect_queue.py`), worker-task
ownership and restart-after-foreign-cancel (`test_worker_survival.py`),
prompt-ack dispositions (`test_prompt_ack.py`), cross-account agent
identity/registry/halt (`test_agent_comms.py`, section 9a), what a reconnect does to
running background tasks (`test_reconnect_bg_tasks.py`, section 6d), and whose
queued prompts a starting session may adopt (`test_queue_persistence.py`,
section 6), which sessions the lobby calls running
(`test_foreign_running_sessions.py`, section 8), external-access policy and the
auth throttle (`test_external_access_policy.py`, section 9), that the
self-paced wakeup loop can be stopped from either side
(`test_loop_control.py`, section 6), what a session carries with it when
`/move` moves it to another directory (`test_move_directory.py` +
`move_overlay.test.js`, section 8), and that a
mistyped launch flag leaves a trace instead of
evaporating (`test_launch_errors.py`, section 10a).

Both interpreters need the dependencies in `requirements.txt`. `textual` is
listed there but only the session-picker TUI uses it; `test_move_directory.py`
runs a subprocess with `textual` blocked to prove the rest of the code still
works without it, because it once silently did not.

**Convention:** pytest-asyncio is *not* installed. Async tests define an inner
`async def go(): …` and call `asyncio.run(go())`.

Run with the interpreter that has the SDK installed.  **Use the C: one**:
D: is a spinning disk and C: is NVMe, so the same suite takes ~4 min there
against ~7 min on D:, and `import server` alone costs 0.4 s instead of 12 s
cold.  Both interpreters are Python 3.14 with the same dependencies; see
known-issues.md, "Launching was slow because the interpreter was on a hard
disk".

```
C:/Users/inhah/AppData/Local/Python/pythoncore-3.14-64/python.exe -m pytest tests/ -q
```

---

## 10a. Startup failures must be visible

`orch2.bat` launches the server via `start /MIN` + `tray_minimizer`, which
gives the process a real console and then hides its window. Anything written
to stderr before logging is configured therefore goes nowhere a human will
look — and argparse errors are exactly that: `parse_args()` runs before
`--log-file` has been read, and exits 2.

So `main()` parses through **`_parse_args_or_report()`**, which tees argparse's
stderr into a buffer and, on a non-zero exit, calls `_report_launch_failure()`:
append the message plus the full command line to `launch-error.log` beside
`server.py`, and pop a dialog **only** when `_console_is_hidden()`.

`_console_is_hidden()` is deliberately narrower than `not _console_is_visible()`
(which the picker uses for a different question — "can a TUI be seen here?").
It requires a console to *exist* and be hidden. "No console at all" — a piped
run, a test, CI — is not a human at a launcher, and a modal dialog there hangs
the process until someone clicks it. That is not hypothetical: the first
version of this used the looser test and wedged a piped run until it was
killed. `_show_error_dialog()` uses `MessageBoxTimeoutW` for the same reason,
and `ORCH2_NO_DIALOG=1` disables it outright for automation.

Why this earns a section: the silent failure did not merely hide itself, it
*misattributed* a real bug. A typo'd `--noresume` evaporated, the user
reasonably assumed the session they were then looking at was the one they had
just launched, and a stale-queue injection (§6, `known-issues.md`) got
explained away as a flag doing something. An invisible failure is worse than a
loud one because it gets credited to whatever happened next.

## 11. Operational notes

- Any Python edit needs a `python server.py` restart (or the lobby's ⟳ Restart).
- Any `static/` edit needs a browser hard refresh — these files are cached, and
  a stale frontend against a new backend is a recurring source of confusion
  (see `known-issues.md`, "Frontend/backend version skew").
