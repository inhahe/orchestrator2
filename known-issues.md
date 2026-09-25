# Known issues / tech debt — orchestrator2

## `--resume <title>` into a running hub kept the title as the session id — FIXED (2026-09-24)

> "i tried '/rename OS A' and I got "Rename failed: session OS Lane A not found
> on disk". also, 'session' in the status bar shows 'OS Lane' even though the
> session should be OS Lane A"

```
19:22:19  runtime s3 started (cwd=E:\visual studio projects\os, resume=OS Lane A)
19:22:20  connect: resume=OS Lane A
19:22:20  [history] session_dir not found for OS Lane A
19:22:23  SDK connected in 3.0s
```

`orch2 --resume "OS Lane A"` joined the running hub, and the hub opened a
runtime with the *title* as its resume target. The CLI accepts a title for
`--resume` and resumed the right session — but orchestrator2 had seeded the
runtime's session id with the title, where it stayed until the first turn's
init: no transcript, the status bar showing the id's first eight characters
("OS Lane"), and `/rename` looking on disk for a session called "OS Lane A".
The reuse check could never match either, so launching it twice would have
opened it twice.

Only the hub's *own* startup resolved titles, and through a helper that fell
back to a substring match across every project — `--resume "Lane A"` could
have opened "OS Lane A". Both now use `session.resolve_session_ref`: the exact
title (any case) of one session in the launch directory, as the CLI itself
does. A title two sessions share opens neither, and says so rather than
reporting that no such session exists.

### And two sessions under one name

The same log showed the launch before it opening an empty session as
`Lane-A`, and this one taking `Lane-A` too. Nothing said so. When the empty
one was torn down for idleness, its `deregister` deleted the shared registry
entry, and the real lane session dropped out of the registry — no halts, not
addressable — while believing itself registered. Now a session only removes
its own entry, a heartbeat that finds its entry gone restores it under the
same name, and taking a name another live session holds is said in the
transcript. design.md §9a.

## `/rename` set the title but not the name other sessions address — FIXED (2026-09-22)

> "i did `/rename Lane A` and then sent `test` and it said: ... I checked again
> and the session is still named os-f5, so I still don't know which lane this
> is."

The model was right. SlateOS's `which-lane.py` reads the lane from the first
line of `ListAgents` — the session's **addressable name**, what `SendMessage`
targets — and that is held by the running CLI. Our `/rename` wrote a
`custom-title` record, which the running CLI never reads, and nothing on disk
restores the name on resume either: measured, neither our record nor the
`agent-name` record the CLI's own `/rename` writes survives a resume. The name
comes only from `CLAUDE_CODE_SESSION_NAME` at startup or the CLI's own
`/rename`, live.

So now both: every connect passes the person-chosen title as
`CLAUDE_CODE_SESSION_NAME`, and `/rename` is also handed to the live CLI. The
second half is sent as a short exchange at the CLI's next idle moment, **not**
as a turn (which would have cancelled a scheduled loop wakeup and rung the
turn-done bell) and not as a queued prompt (echoed on send, and mergeable into
the next prompt, which the CLI would take as part of the title). design.md §6,
*CLI-native commands*, and §8a.

### What the first version of the fix got wrong

It let `--agent-name` / `ORCH2_AGENT_NAME` win over the title, to make
SlateOS's documented `--agent-name "Lane A"` work. At the time both belonged
to the *hub*, and every session the hub opened inherited them — so a hub
started with `--agent-name orchestrator2`, which was advice given the same
day, would have named all six lanes "orchestrator2". It was taken out before
it shipped, and came back once the flag was made per session (next entry).

### Not handled: the rolling-window context trim

`--max-context-tokens` (off by default) trims into a **new** session id whose
file carries no `custom-title`, so after a trim the next connect finds no title
and the CLI comes up with an auto name. Rename again after a trim if it
matters.

## `--agent-name` belonged to the hub, not to a session — FIXED (2026-09-22)

Found while fixing the entry above. `Config.agent_name` was parsed once, for
the hub process, and `_create_runtime` builds every session from
`dataclasses.replace(config, …)` — so every session the hub later opened
inherited it, as they all read `ORCH2_AGENT_NAME` from the hub's environment.
`resolve_identity()` takes an explicit name at face value, so all of them
registered as that one identity: one inbox, one halt state, whichever session
polled first got the message. The other direction was dropped silently: a
launch that *joined* a running hub never handed `--agent-name` over, so it
registered nothing by that name.

> "yes, make --agent-name apply per session"

Now it names the session its launch opens and nothing else, including through
a running hub; it is that session's `ListAgents` name as well as its registry
identity; and it is remembered by session id, so the session keeps it when it
comes back from the lobby or a hub restart without the flag. That last part
needed its own table: the registry row an identity lives in is deleted on a
clean exit, so until now a named session closed normally and reopened without
the flag came back re-identified by its directory instead. design.md §9a.

`--agent-label` had the same fault — a hub started with `--agent-label
lane=b` put lane=b on every session it opened, which is worse than no label
in exactly the case labels exist for (telling lanes apart) — and got the same
fix: per session, handed over, remembered.

### Tests were writing to the real registry

Found while doing this: a test that only meant to exercise a connect reached
the machine's `agents.db`, which is how it came to have a `session_names`
table before this code ever ran in a hub. Nothing was written into it, but
the same path very probably explains the two identities `orchestrator2` and
`orchestrator2-2`: registered by one process seven seconds apart on
2026-09-06, no session id, never a heartbeat, nothing ever sent to them — and
the reason every session opened in this directory since has *refused* an
identity ("several agent identities are registered"). `tests/conftest.py` now
gives every test a private registry. The two rows are still in the real one.

### Two things it could not keep

* **`ORCH2_AGENT_NAME` no longer reaches a session's own processes.** The
  launch that reads it removes it, because left in place it reached *every*
  session's processes: `tools/agents.py` defaulting to one session's inbox in
  all of them, and a test hub an agent starts claiming that identity. It was
  never visible to a session opened in a running hub anyway.
* **Naming a session that is already open also retitles it.** The only live
  way to change the running CLI's name is its own `/rename`, which sets its
  title too. A reconnect would avoid that, at the cost of the session's
  background tasks.

## Upstream: Anthropic's bundled `claude.exe` leaks committed memory per turn — WORKED AROUND (2026-09-01)

**Not our bug, and not fixable here** — the leak is inside the `claude.exe` the
Agent SDK spawns. What is ours is that a long-lived orchestrator session keeps
that process alive for days, so the leak has time to take the machine down.
Recycling (design.md §6c) is a **workaround**; the entry stays open because the
underlying defect is still there and a future SDK/CLI release could change its
shape (or fix it, at which point the threshold should be revisited rather than
silently kept).

**Why it is worse than an ordinary leak.** It grows
`PROCESS_MEMORY_COUNTERS_EX.PrivateUsage` — *committed* memory, i.e. Task
Manager's "Commit size", psutil's `memory_info().private` — not the working set.
Two properties of Windows commit make that far more damaging than RSS growth:

1. **Commit is a promise, not storage.** A page is deducted from the system
   commit limit at `VirtualAlloc(MEM_COMMIT)` and does not reach RAM until
   touched, nor the pagefile until touched *and* evicted. So the leaked memory is
   counted **nowhere** you would think to look.
2. **Windows never overcommits.** The limit (RAM + pagefile) is a hard credit
   limit, so allocations start failing machine-wide while gigabytes of RAM sit
   free.

**Measured 2026-09-01.** System commit 254.60 GiB of a 255.95 GiB limit — in RAM
49.29, in pagefile 20.60, **NOWHERE 184.71**. By image:

| image | procs | private | working set | ratio |
|---|---|---|---|---|
| `claude` | 5 | 173.57 GiB | 12.21 GiB | **14.2x** |
| `chrome` | 45 | 7.26 GiB | 6.12 GiB | 1.19x |
| `python` (incl. this orchestrator) | 20 | 1.34 GiB | -- | -- |

The orchestrator's own parent process was 0.55 GiB private after 88.9 hours, so
the Python side is not implicated. A 25 s idle sample showed flat/negative
deltas for every idle `claude` process and **+21.85 MiB for the one taking a
turn** — hence "per turn", and hence the rate range of 0.08--0.65 GiB/h
depending on how hard a session is driven.

**The measurement above is against bundled CLI 2.1.105 and is now stale.** On
2026-09-02 the SDK was updated 0.1.59 → 0.2.151, which carries CLI
**2.1.105 → 2.1.258**. The leak may have changed shape or been fixed outright;
nobody has re-measured. **Do that before trusting the 8 GiB default** — as this
entry already said, a fixed leak means the threshold should be revisited rather
than silently kept. The `/recycle` command reports live private bytes and the
recycle count, so a session left running for a day answers it: if `cli_recycles`
stays 0 and `cli` stays near its baseline, the leak is gone.

**Diagnostic note for future investigation:** RSS/working-set monitoring cannot
see this leak *at all*. Anything that samples `memory_info().rss` will report a
healthy process right up to the moment the machine stops being able to allocate.
Use `private` (or `PrivateMemorySize64` in PowerShell).

**Cost of the workaround.** A recycle is a `disconnect()` + `connect(resume=…)`,
and the resume budget scales with the session JSONL — a 1.29 GB transcript takes
minutes to re-read. That is why recycling is gated on a cooldown *and* on the
process having grown at least `cli_recycle_min_reclaim` GiB past the baseline
sampled after connect: without the growth check, a session whose transcript is
honestly large would pay that resume cost every turn and reclaim nothing.

**Residual limitation.** The CLI's background-task registry is in-memory
(`AppStateStore.ts:470` `tasks: {}`) and is *not* rehydrated by `--resume`, so a
recycle loses the harness's view of any running background task. Actual OS
processes survive (no job object; `gracefulShutdown.ts` reaps no children) and
their outputs stay on disk under `%TEMP%\claude\<project>\<session-uuid>\tasks\`.
This is why `_maybe_recycle_cli()` refuses while `state.background_tasks` is
non-empty — including for a forced `/recycle now`.

## A failed resume silently became a blank session — FIXED (2026-09-22)

> "i did --resume with a nonexistent session name, and now it won't let me
> /rename the session"

```
SDK connection failed (attempt 1/10: ... Provided value "OS A" is not a UUID
and does not match any session title. (exit code: 1)), retrying in 2s…
Rename failed: session OS A not found on disk
SDK connected.
Rename failed: session OS A not found on disk
```

"SDK connected" is the misleading line. What it connected *to*:

```
20:54:56  SDK connect failed (attempt 1): ... "OS A" ...
20:54:58  connect: resume=None            <- the retry dropped the resume
20:55:00  agent identity: os-2 (created)  <- and minted a new identity
20:55:00  SDK connected after 1 retries
```

### The typo was the harmless trigger for a general hazard

The requested resume id was "one-time use" — cleared the moment
`_make_options` had built the options, so a first attempt that **failed** had
already spent it. The retry loop calls a bare `connect()`; `_create_runtime`
sets `no_continue = no_continue or bool(resume)`; so the retry took the
fresh-session branch. `reconnect()` never had the problem because it passes
`resume_id=sid` explicitly — only the first-connect retry relied on the
one-time id.

That applied to **every lobby-opened session**, not just typos: any whose
first connect attempt failed for a transient reason was silently swapped for a
blank one. Proven offline before writing a fix:

```
attempt 1 resume : 43ee6a50-real
retry     resume : None | continue_conversation = False
```

The resume id is now consumed only once a connect succeeds.

### A missing session is settled, not transient

With the resume kept across retries, a typo would have been retried ten times
over minutes of backoff — safe, but useless. The CLI's own wording for a
nonexistent target (`does not match any session title` / `requires a valid
session ID or session title`) is now classified like `UnusableCwd`: one
attempt, then a message that names what was asked for **and what exists**:

> There is no session called 'OS A' here — it is neither a session id nor any
> session's title. Sessions here: 'E OSc' (1aa74fb0). Open one of those from
> the session list, or start a new session.

Matched on the CLI's words rather than pre-validated: the CLI accepts titles as
well as ids, and reimplementing its resolution would eventually disagree with
it. If the wording changes, the match fails safe — back to retrying, which now
keeps the right id.

The bogus id is also cleared from `state.session_id`. That was the reported
symptom: nothing was resumed, but history, `/rename` and the persisted queue
all went on keying on "OS A" until the blank session's first turn.

### And a false alarm on `/clear`

`/clear` starts a new session on purpose, but `_make_options`' fresh branch
never reset `state.expected_resume_sid`. Its first turn therefore reported
*"Expected to resume X but SDK started Y — prior context may not be loaded"* —
the only other occurrence of that warning in the whole log was exactly this, on
the Lithic session on 2026-09-20.

**Verified:** `tests/test_resume_unknown_session.py` (18), driven through the
real `worker_loop` with a fake client rather than asserted against source, and
the `resumefail` (`sdk_bridge.py` × 8) and `resumelist` (`session.py` × 4)
mutation targets, 12/12; neighbouring targets re-swept 21/21; full suite 1,157
passed. One assertion in my first draft was wrong — `continue_conversation`
is False on *every* resume (dataclass default; `resume` takes precedence), so
it cannot distinguish a resume from a fresh session. `resume` is the whole
question.

## A new model was refused by the SDK's bundled CLI — FIXED (2026-09-22)

> "Claude Code 2.1.259 does not support this model; version 2.1.280 or newer
> is required." — "maybe we need to update the sdk?"

Reasonable guess, and **not sufficient**. The Agent SDK pins a CLI version and
ships that binary inside the wheel:

| | version |
|---|---|
| Installed SDK 0.2.152 bundles | 2.1.259 |
| **Newest SDK 0.2.157 bundles** | **2.1.277** ← still short |
| Opus 5.5 requires | 2.1.280 |
| Standalone `claude` on PATH | 2.1.232 (older still) |

Read out of the 0.2.157 *sdist* (`__cli_version__ = "2.1.277"`) rather than by
installing it — upgrading in place under a running hub is what left the
`~laude_agent_sdk` directories the last time, and the answer turned out to be
"upgrading would not have fixed it" anyway.

2.1.280 did exist: the release channel serves `/latest` (2.1.280 that day) and
`/stable` (2.1.267) as bare version strings, with a per-platform SHA-256 in
`/<version>/manifest.json`.

### The override was always there

`ClaudeAgentOptions.cli_path` takes priority over the bundled binary.
orchestrator2 had simply never set it — `grep cli_path sdk_bridge.py config.py`
returned nothing. So the fix is `--cli-path`, plus the same override on
`_create_runtime` and the launch API so a **single session** can run the newer
CLI while the rest stay on the bundled one. That split matters: the binary that
unblocks a new model is `latest`, not `stable`, and CLI-side behaviour has
burned this project repeatedly.

A path that does not exist falls back to the bundled CLI with a warning.
Failing the session over a typo would be much worse than running the model an
older CLI supports, and a *silent* fallback would present as "my new model is
still refused" with nothing to explain it.

### Verified both directions

```
cli 2.1.280 + opus-5-5 : ('ok', 'OK')
bundled     + opus-5-5 : ('ok', 'API Error: 400 Claude Code 2.1.259 ...')
```

The second line is the control. Without it the first proves only that a session
can start, not that the override is what made the model work. Note also that
the refusal arrives as *response text*, not an exception — the SDK does not
raise, which is why this presented as a chat message rather than a connect
failure.

Installed to `~/.claude-cli/2.1.280/claude.exe` (checksum-verified against the
manifest), deliberately **not** over the existing `~/.local/bin/claude`, so the
change is reversible by deleting one directory and unsetting one flag.

**Verified:** `tests/test_cli_path.py` (10) and the `clipath` (`sdk_bridge.py`
× 4) and `clipath-cfg` (`config.py` × 2) mutation targets, 6/6; full suite
1,139 passed.

**Still open:** the SDK itself is 0.2.152 and 0.2.157 is out. Worth taking, but
it buys nothing for this problem and wants a quiet moment — it rewrites
site-packages under a hub that has live sessions holding the old CLI open.

## `/model` answered from an hour-old cache — FIXED (2026-09-22)

> "/model currently doesn't show opus 5.5, so it must be using an old cached
> /model. i want /model to always read the current list when possible."

Correct diagnosis. `/model` never fetched — it rendered whatever
`_model_cache_loop` had last cached, and that loop runs hourly. Opus 5.5 went
live between 11:59 and 12:40; the hub's 11:59 refresh had cached eleven models,
and a fetch at 12:40 returned twelve:

```
claude-opus-5-5   Claude Opus 5.5 — 1M context    ← listed first
claude-fable-5-1  Claude Fable 5.1 — 200k context
claude-opus-5     Claude Opus 5 — 1M context
...
```

### "Must not block the loop" is not "must not fetch"

The design note said *"`/model` runs on the event loop, so it can only ever
read a cache — it must never block on a network call"*, and the second clause
was treated as implying the first. It does not. `_model_cache_loop` had been
doing `await asyncio.to_thread(fetch_available_models)` all along — the
off-loop pattern was already there, just not wired to the command.

`/model` with no argument now awaits a threaded refresh before rendering
(`MODEL_SHOW_FETCH_BUDGET`, 4 s). On timeout or error the previous list is
shown, labelled. The WS router already had precedent for this: `/mcp` sits a
few lines above, handled in the async context "because it can't be a
synchronous immediate command".

### The picker's own staleness signal was the thing that hid it

The UI has had a "couldn't reach the model API" banner for a while, shown when
`live === false`. But `live` meant `not model_cache_is_stale()` — *the cache
has not aged out in an hour* — which is a far weaker claim than *this is what
the API says now*, and was **True in precisely the case that misled**. A
41-minute-old list missing a released model reported itself live.

There are now three provenances — `live` (fetched within 60 s), `cache` (real
but old), `builtin` (the hardcoded fallback) — and the two failure states get
different warnings. "Showing the built-in list" is alarming and true for one of
them and simply wrong for the other; saying it anyway trains the reader to
ignore the banner.

`KNOWN_MODELS` itself was missing both `claude-opus-5-5` and
`claude-fable-5-1`, so a hub that lost API auth would have silently dropped
them from the picker. (That hub had 1,676 `HTTP 401 from /v1/models` lines
across the preceding days, so this was not hypothetical.) Both added, newest
first, and pinned by a test.

**Verified:** `tests/test_model_list.py` (24, up from 6) and the `modellive`
(`server.py` × 6) and `modelfresh` (`config.py` × 5) mutation targets, 11/11;
full suite 1,129 passed. The router tests drive the real `_handle_ws_message`
rather than grepping for the call — a source-level assertion survives the
mutation that deletes it, which is how four wakeup-restore tests passed while
the guard they checked had been replaced by `if False`.

Two defects the tests caught in the first implementation: the lock **serialised
but did not coalesce**, so three tabs running `/model` made three sequential
requests rather than one (fixed with a freshness check *inside* the lock); and
the hang test's own `asyncio.run` teardown joins executor threads, so a fixed
`sleep` in the fake was paid at teardown and hid what was being measured — the
fake now blocks on an event released once the measurement is taken. The second
is worth remembering beyond the test: a hung fetch thread will delay loop
shutdown, which matters little for a server that runs for days but would matter
for anything short-lived.

## Autonomous loops were reaped between iterations — FIXED (2026-09-20)

> "for two sessions i had going, OSc and OSa, they now say 'This tab's session
> is no longer running — the server was restarted (or the session was closed)
> since the tab was opened.', even though i never closed them."

Both halves of that banner were wrong, and the real cause was worse than
either.

**The hub had not restarted.** It was pid 21996, up since 2026-09-16. The two
"log file opened" entries from today are a second `server.py` launch detecting
the running hub and bowing out 360 ms later:

```
17:54:12,570 [pid 15336] --- log file opened ---
17:54:12,930 [pid 15336] --- server shut down (pid 15336) ---
```

**And the user had closed nothing.** Both sessions were reaped by the idle
timer, days earlier — and reaped *between loop iterations*:

```
22:25:10  wakeup armed: delay=1800s  'Autonomous-loop wakeup (scheduled by you)...'
22:26:45  runtime s5 idle for 300s — tearing down            <- 95 seconds later

01:30:15  wakeup armed: delay=1500s  'Autonomous-loop wakeup (scheduled by you)...'
01:31:50  runtime s6 idle for 300s but still running 1 background task(s) — deferring
01:36:48  bg task buj4ncnuw completed
01:36:50  runtime s6 idle for 300s — tearing down            <- 19 minutes still to run
```

For s6 the background task was the *only* thing holding it open; the moment it
finished, the runtime went.

### A session with work scheduled is waiting, not idle

`_idle_teardown_after` was taught in September that "a session that is working
is not idle" — it defers on `state.busy` and on running background tasks. **A
pending wakeup was never part of that**, and an autonomous loop between
iterations is idle by definition: waiting is the whole activity. Worse,
`state.wakeup_at` lives only in memory, so reaping cancels the loop outright
and leaves nothing to restore it from. The loops stopped days ago, silently.

The guard now also defers on a wakeup — but only one still in the *future*. A
stale past timestamp means the wakeup should already have fired, and deferring
on it would pin the runtime forever on a schedule nothing will honour.

*This is the third time this teardown has been found reaping something it
should not, and the second time my own fix for it was incomplete: the entry
above it cites an autonomous-loop session as the worst case and still only
covered the mid-turn half.*

### The banner was guessing

"The server was restarted (or the session was closed)" was a guess offered as
an explanation, and it sent the user looking for a restart that never happened.
Teardowns now record why, and the banner reads them:

> This tab's session (E OSc) was closed after 5 minutes with no tab connected
> at 2026-09-19 01:36. Its conversation is safe — reopen it from the list
> below to carry on.

The old wording survives only for a rid we genuinely cannot explain — one from
a previous hub, or a reboot that restored old tabs. The record is bounded at 32
entries: it is a courtesy for reconnecting tabs, not a log.

**Verified:** `tests/test_idle_teardown.py` (+10) and the `wakeupidle` mutation
target (`server.py` × 8), 8/8 after repair; `idle` re-swept 12/12; full suite
1,074 passed. Two survivors on the first sweep: one was a real gap (nothing
checked that the *idle* path passes its own reason, so "was closed" would have
read as an ordinary close and hidden the five-minute rule), and one was
equivalent — moving the record below `runtimes.pop` changes nothing, since
popping the registry entry does not touch `rt.state`. A comment claiming that
position was load-bearing has been corrected.

### The restart half — also fixed

A wakeup lived only in memory, so a hub restart cancelled every schedule just
as quietly as the reaper did. Asked which behaviour a user would expect, the
answer settled it: *"i guess my loops are mostly unattended."* Restoring a
schedule only when someone next **opens** the session would have been the safe,
queue-shaped design — and decoration, for a loop whose entire purpose is
running while nobody watches. So `wakeup_store.py` persists them and the hub
resurrects the sessions itself at startup.

**That is the most dangerous thing the hub does, and every restraint on it is
deliberate.** A wakeup carries a prompt that is *sent as a real turn*,
unattended, in a session the user did not open — the same shape as the harm the
persisted prompt queue once caused. So a record is session-scoped and must
match its own slot; it expires; only `MAX_RESURRECT` sessions come back per
boot (each is a CLI and a transcript load, on a machine that has exhausted its
commit limit before); a session already running is never revived on top of
itself; an overdue one waits `WAKEUP_RESTORE_SETTLE_S` so tabs can reconnect
before a turn starts; and the session announces why it woke up.

The interesting rule is the **lateness budget**: a wakeup may fire late by at
most its own cadence (`due_at - armed_at`, floored at 1 min and capped at 1 h).
A loop that ticks every twenty minutes has no business firing eight hours late
on a plan that has gone stale, so past that budget it is left *paused* — the
record stays on disk, and opening the session is what surfaces it. Derived
rather than configured, so a fast loop tolerates little lateness and a slow one
more, with no knob to get wrong.

**Verified:** `tests/test_wakeup_store.py` (34) and the `wakestore`
(`wakeup_store.py` × 12) and `wakerevive` (`server.py` × 5) mutation targets,
17/17 after repair. **Four of the five `wakerevive` mutants survived the first
sweep**, and all four for the same reason: the tests were source-greps. One
asserted the source contained `live_sids` — which it still did after the guard
became `if False:` — and another looked for `system_msg` in a function whose
broadcast had been redirected to nowhere. They are behavioural now: fake
runtimes, real resurrection, assertions on what was created, armed and sent.
The fifth survivor was equivalent (a negative-interval guard the `max()` floor
already covers) and was removed with a note.

## `/btw` waited behind the queue it exists to jump — FIXED (2026-09-18)

> "I did a /btw and it didn't send it and show me the result until after the
> turn ended. That seems to defeat the purpose of a /btw?"

Two separate things, and only one of them was fixable.

### The bug: it went to the back of the queue

A `/btw` typed *during* a turn lands on the event queue and is drained by
`_between_turns` when the turn ends. That branch did:

```python
elif kind == "btw":
    # Side question — for now, queue as regular message.
    state.queued_prompts.append(payload)
```

**Appended.** So an aside typed during a turn waited behind every prompt already
queued — while the *idle* path returns the `/btw` text immediately, ahead of the
queue. The same gesture made three seconds earlier was silently demoted, and
design.md had been asserting the opposite all along: "`/btw`, whose entire
purpose is to jump the queue". It now goes to the front.

It goes there *in the order typed*: pushing each aside as it arrives reverses
two typed in one turn, which the first draft of the fix did and a test caught
immediately.

### The part that is not a bug: it cannot answer mid-turn

**A turn owns the single CLI.** Nothing can be answered while one is running —
and shoving a prompt at a busy CLI is not merely late but unsafe: measured
2026-09-16, a prompt sent into a live stream was removed by the CLI itself with
`reason: "absorbed_mid_turn"` and never reached the model. So "the moment the
turn ends, ahead of everything else" is the best available behaviour, and it is
what `/btw` now does.

### The documentation was promising something that was never built

`/btw` was advertised as "side question in **separate context**" in both the
README and `/help`, and the implementation comment admitted the gap — *"for
now, queue as regular message. Full /btw implementation would run in a separate
context."* It has always been an ordinary prompt in the main conversation.

### What it is supposed to be — NOW BUILT (2026-09-18)

Asked to confirm: *"I think /btw is supposed to create a new conversation
instance with the same context but copied so it doesn't interfere with the
original, that runs as the current turn is running."* That is the intent, and
**the SDK supports it directly**:

```python
fork_session: bool = False
"""When true, resumed sessions fork to a new session ID rather than
continuing the previous one."""
```

So `resume=<current sid>, fork_session=True` on a second `ClaudeSDKClient` is
the whole mechanism: same context, new session id, original untouched, and
genuinely concurrent with the live turn because it is a separate CLI process.
No transcript copying, no synthetic bookkeeping.

An earlier note here proposed copying the session file with `copy_session.py`
and called a second CLI a workaround. That was wrong — it was written without
checking whether the SDK could fork natively, which it can. Corrected rather
than deleted, because the wrong version is the more instructive one: the
"expensive hack" framing came from only ever looking at our own code.

Built. `SDKBridge.run_btw` spawns it as an `asyncio.Task` so it never touches
the worker, and `server.py` routes `btw` straight there — reaching the event
queue at all is what made it wait for the turn.

**Verified end-to-end rather than on the flag's say-so**, and deliberately
against a throwaway session: had `fork_session` not behaved as documented, the
fork would have appended to a live conversation mid-turn. A base session was
created, told a codeword, then forked:

```
base session: b18aaa20…   base transcript bytes: 16124
fork session: cfc2fc45…   answer: TANGERINE
NEW SESSION ID: True
base transcript unchanged: True (16124 -> 16124)
HAS CONTEXT: True
```

New id, byte-identical original, and it answered from context.

The costs are real and unchanged: a CLI process per aside, a transcript load
before it can answer, and a full context's worth of tokens. Hence one at a
time.

**The foreign-holder hazard — also fixed.** The forked CLI is launched with
`--resume <the same session id>`, which is exactly the pattern
`proc_guard.find_foreign_claude_for_session` scans for, and that scan is what
stops a session opening. Our own tree is excluded so a hub never trips its own
guard, but another hub scanning during the seconds a fork lives would have seen
the session as held elsewhere. Both scanners — the duplicate check and the
holder map the lobby reads — now skip `--fork-session` processes.

Two things that test would have missed if written carelessly, and did not:
a **control** proving a plain foreign `--resume` is still detected (without it
the skip test passes because the harness matches nothing), and the mutation
sweep's survivor — nothing pinned that the flag is matched as an *argument*
rather than a substring of the joined line, so a session whose prompt discussed
`--fork-session` would have been waved through and the duplicate guard disabled
for a conversation something really was driving.

**Verified:** `tests/test_proc_guard.py` (+8) and the `forkskip` mutation target
(`proc_guard.py` × 5), 5/5 after that gap was closed. The flag name itself is
pinned against the SDK source, since a filter watching for the wrong spelling
fails silently and looks like it works.

**Verified:** `tests/test_queue_drain.py` (+3) and the `btw` mutation target
(`sdk_bridge.py` × 4), 4/4. One full-suite run showed
`test_frontend_js_suite[hidden_window.test.js]` timing out; the run took 586 s
against its usual ~85 s (a loaded machine), and the test passes on its own.
1,031 passed otherwise.

## Background tasks that never end, and the statistic that hid them — FIXED (2026-09-16)

> "it still happens all the time that i still see a background task in the pane
> but the model claims it's done with everything. for example, right now, the
> 'lithic' session is completely done, but background tasks shows 'Find every
> script that writes into D:\github'"

**My earlier answer to this was wrong, and wrong in an instructive way.** Asked
whether the task registry could be trusted, I measured 44 unmatched starts out
of 50,886 and reported ~0.1%. True, and beside the point: *a stuck row never
clears*, so an event rate of 0.1% is a panel that is permanently wrong. The
question was about standing state; I answered with a rate. Measured properly,
per hub process:

| hub pid | stuck | started |
|---|---|---|
| 104628 | 12 | 5,577 |
| 3640 | 7 | 13,074 |
| 128856 | 6 | 6,823 |
| 132168 | 3 | 1,427 |
| 21996 (fresh) | 2 | 277 |

**Twelve of twelve hub processes had them.** A freshly restarted hub had two
within its first 277 tasks.

The supporting claim was worse. I said "almost all the rest are accounted for
by an announced orphan, a teardown or a reconnect" — that classifier searched
the next 600 lines of a log where a dozen sessions interleave, so it matched
*something* nearly always. It was noise dressed as evidence, and it let me
dismiss exactly the population being asked about.

### The rows are real hung processes

The reported task was still there:

```
pid 34752  started 16:27:15  cpu_time=0.0s  status=running
    grep -rliE "[d]:.github" --include=*.bat ... .
```

Zero CPU in twenty minutes, output file frozen at 50 bytes since three seconds
*before* the task was registered. Its transcript record explains where it came
from: `"Command did not complete within its 420s timeout and was moved to the
background (ID: bnexix9sl)"` — a foreground Bash auto-promoted on timeout, not
a task the model chose to background. The model then read the partial output
file directly and moved on, which is why it considered itself done.

So the panel, the CLI and the model were each telling the truth about different
things. The panel was the *only* thing reporting an abandoned hung process.

### What was built

`bg_stall.py` never decides a task is finished — it decides whether one is
still *doing* anything, from three signals over the task's process tree:
output-file movement, CPU time, and disk-I/O operation counts. All three still
for 300 s ⇒ `STALLED`. I/O is there because CPU misses a whole class of work —
a copy, a tree walk, a network pull burns almost no CPU and may write nothing
to its own output file for minutes — and because it is what distinguishes
*doing* I/O from being *blocked* on it, which is what the reported grep was. Process unidentifiable ⇒ at worst
`QUIET`, which annotates and changes nothing, because "I lost track of it" is a
statement about us rather than the task.

`STALLED` changes *holds*, not rows. Six places let background work defer
something — idle teardown, a deferred `/model`, its flush, the `bg-all-done`
wakeup, the context trim, bg-wait parking — and all six now ask
`_active_bg_tasks()`. That matters most for the teardown, whose deferral was
made deliberately unbounded on the reasoning that "a session that keeps working
is succeeding at its job": a task burning no CPU isn't working, and would have
pinned the runtime until the hub died.

The row stays, annotated with the observation (`no output or CPU for 20m`), not
a verdict (`hung`) — the first is checkable, the second is a conclusion we
cannot reach about a task that may be blocked on something imminent. Rows with
an identified process get a kill button (`POST /api/bg/kill`), which re-checks
`create_time` before killing so a recycled pid cannot be shot. Nothing kills on
a timer.

**Verified:** `tests/test_bg_stall.py` (40), mutation targets `bgstall`
(`bg_stall.py` × 23) and `bgstall-wire` (`state.py` × 3), 26/26 after repair;
full suite 1,029 passed. The path derivation and process matching were then
checked against a *live* background task rather than only fixtures, which is
what caught the last defect: `process_cpu_seconds` measured the task's shell
alone, and a shell sleeps while its child does the work — so a task compiling
quietly would have read as zero CPU, zero output, and been declared stalled.
It now sums the process tree. Probing a live task again, after
adding the I/O signal, caught two more: the command match was defeated by the
CLI's wrapping (quotes stripped, slashes flipped, newlines flattened), so every
such task was unidentifiable and kept its gate holds forever; and the
wrapper-chain tie-break took the *innermost* match, left over from when only
the matched process was measured rather than its subtree. Two survivors on the first sweep: one aimed at a
*duplicated* copy of the quiet-clock computation (`stall_state` and `quiet_for`
each had one — now a single implementation), and one equivalent guard, deleted
with a note. The first sweep also caught a real bug in the first draft: I had
used `min` where the meaning is "the most recent evidence of activity", which
called a task stalled while its output file was still growing.

## A prompt sent during a ghost turn was echoed and then swallowed — FIXED (2026-09-16)

> "I sent this prompt to the Good Photons session [...] it showed it sent as a
> 'You:' message, but it just kept working and never answered me."

The log has all of it:

```
08:55:01  run_turn exit: normal (turns=62)          <- real turn ends
08:56:07  bg task bheyhqc8u completed
08:56:17  ghost turn begin: SDK streaming without active run_turn
08:59:14  run_turn start: ta.is_set=False queue_size=0 state.busy=True
          prompt='You said the 5x map bought 25%...'
09:18:33  run_turn ResultMessage: subtype=success elapsed=1159.1s
09:18:33  run_turn exit: normal (turns=63)
```

A background task woke the model into a **ghost turn** — a stream with no
`run_turn` consuming it. Ghost turns set `state.busy` but not `turn_active`, so
the worker was parked in `_await_next_prompt()`, looking idle while the CLI was
three minutes into an answer.

**Routing was correct.** `server.py` saw `state.busy` and queued the prompt. The
queue poke then woke the parked worker, which popped it — that pop is what
emits the `You:` echo — and started a turn on top of the live stream. That turn
consumed the *ghost* turn's terminating ResultMessage (note `elapsed=1159.1s`:
that is the ghost turn's duration measured from its own start, not this turn's)
and exited "normally" with `turns=63`, having answered nothing.

**The prompt was not merely delayed — the CLI discarded it.** Its own
transcript records the whole life of it:

```
12:59:14.189Z  queue-operation  enqueue  "You said the 5x map bought 25%..."
13:06:17.594Z  queue-operation  remove   reason: "absorbed_mid_turn"
```

(UTC; 08:59:14 and 09:06:17 local.) There is **no `user` record for it
anywhere in the session** — it never became a message the model could see. We
handed it to a CLI that was already mid-stream, and seven minutes later, when a
background task's notification landed, the CLI concluded the running turn had
absorbed it and removed it. So the question was never asked, and no amount of
waiting would have produced an answer.

`_await_interrupt_settled` already guarded this identical race when an
*interrupt* caused it, and its docstring describes the same mechanism
("the dispatcher handed the *previous* stream's terminator to the *new* turn").
Nothing guarded it when a background task caused it instead.

**The park point's own comment carried the bad assumption:** *"A queued user
prompt is always sendable once we're back to waiting."* True for a ghost turn
that streams briefly. False for one that runs nineteen minutes.

### The fix is a decline, not a resume

One check in the `wakeup` branch of `_await_next_prompt()`, next to the
identical `state.connecting` one: **don't pop while `state.busy`**. A real
`run_turn` can never be active at that park point — the worker would be inside
it — so `state.busy` there means exactly "a ghost turn is streaming". The
prompt stays in the queue pane, visibly pending, instead of being echoed into
the transcript and lost.

The resume half already existed: `_end_ghost_turn()` has poked the worker with
`queue-edit-done` all along, for precisely this case. It was written for ghost
turns that stream briefly and works unchanged for long ones. **An early draft
of this fix added a second poke there; the mutation sweep caught it as
redundant** — the new mutation removes the *existing* poke instead, which is
the line actually holding the contract up.

`run_turn` also waits on `_await_ghost_settled()` next to the interrupt guard,
covering every other path in. Timeout 1800 s, not the interrupt guard's 5 s,
because a ghost turn is real work and starting early is the bug; finite only so
a terminator that never lands can't wedge the session.

**Verified:** `tests/test_prompt_during_ghost_turn.py` (13) and the
`ghostqueue` mutation target (`sdk_bridge.py` × 8), 8/8. The one survivor on
the first sweep was the redundant poke described above — a test passing because
the *old* code was already correct, which is the useful kind of survivor. Full
suite 989 passed.

## A resumed session had no idea its background tasks were gone — FIXED (2026-09-16)

> "if the session is automatically continued, the agent has no idea that any
> background tasks it had been running were aborted. is there any way to tell
> it? i guess not because the resume was done by the cli?"

Half right. We can't get **in front of** the resumed turn — the CLI's
conversationRecovery re-enqueues it during session load, before our `connect()`
returns, so anything we send is behind it in the CLI's command queue. But the
reason the model was never told had nothing to do with the CLI. Three holes,
all ours:

1. **`_orphan_bg_tasks()` told the browser and not the model.** It has named
   orphaned tasks in the UI since 2026-08. The model — the thing actually
   waiting on those `TaskOutput` handles — was never in the loop, on any
   orphan path.
2. **A teardown told nobody.** `_orphan_bg_tasks()` is wired only to the
   reconnect paths; `_teardown_runtime` never calls it. There are **zero**
   orphan announcements in the entire 2026-09-16 log, including the 04:01
   teardown that started all of this.
3. **After a teardown we didn't even know.** `State.background_tasks` mirrors
   the CLI's registry in memory; `session.py` persisted the prompt queue and
   nothing else. A fresh runtime started empty, so there was nothing to report
   even if someone had asked for it.

### Could the record even be trusted?

Asked directly — *"i think that knowledge might often be incorrect, because
the client seems to often have bg tasks showing that were already finished"* —
and worth measuring before building on it, because a stale claim is worse than
no claim: the model *acts* on it.

Across 715,914 log lines: **50,886** tasks started, **50,841** completed. Of
the 44 unmatched, 4 had started within the hour (still running), and almost all
the rest are accounted for by an announced orphan, a teardown or a reconnect —
leaving about **4 in 50,886** unexplained. There is one registration site,
keyed by the CLI's own `task_id`, so there is no second key that could strand a
phantom row. Completed tasks do linger in the panel, deliberately, for
`panel_grace` = 10 s.

**The remembered behaviour was real, and is what produced those numbers.**
`_orphan_bg_tasks`'s own docstring records it: the loss "used to be a bare
`log.info` into a file nobody reads", so tasks orphaned by a reconnect stayed
in the panel forever, dead. 14 of the unmatched starts are that fix working.

### But "running when we last heard" is not "failed"

At 04:01:34,445 a teardown began; two background tasks logged **completed** at
04:01:35,327 and 04:01:36,182 — one and two seconds into it. (The bg log lines
carry no runtime id, so whether those two were OSc's can't be proven from the
log; the race is real either way.) So the record is a list of tasks whose
outcome is **unknown**, and the notice says exactly that: it names them, says
how long each had been running, says the results and notifications are gone,
and tells the model to check for effects before re-running and not to wait on
them. It never says "aborted".

### What was built

The live set is mirrored to `<config-dir>/orchestrator2/bgtasks/`, keyed by cwd
and session id, written on every start and finish. `connect()` reads it back
once for the session being resumed, announces it, and erases it.
`_orphan_bg_tasks()` erases it too, so a loss announced live isn't re-announced
later. The paths that clear the in-memory registry on shutdown deliberately do
**not** save — the record's whole job is to outlive them — and the test suite
pins the save-site count so a fourth one can't quietly appear.

`/clear` is deliberately exempt from telling the model: it wipes the
conversation, so the notice would land in a context that has no memory of
starting the work it describes. The browser is still told there too.

Delivery is a **queued prompt at the front of the queue**, as asked: visible in
the left pane, sent immediately if nothing is running. Front rather than back
because it is context for whatever the session does next — appended, a prompt
queued earlier would be answered by a model that still thought its background
work was alive. Merging it into your own next prompt is deliberately not
automatic; `merge all` already does that on request.

**Verified:** `tests/test_lost_bg_tasks.py` (31), mutation targets `lostbg`
(`sdk_bridge.py` × 8) and `lostbg-session` (`session.py` × 10), 18/18 after
repair; full suite 976 passed. Four survivors on the first sweep and three were the same shape — *a
test that passed for the wrong reason*. `test_another_sessions_record_is_never_read`
loaded with a different session id, which changes the **filename**, so it never
exercised the recorded-id gate at all; it now writes a mismatched id into the
right path, the way a reused slot would. The fourth was equivalent (an early-out
guarding a call that has the same gate inside) and was deleted with a note
rather than covered.

## A resumed session finished its old turn without telling anyone — FIXED (2026-09-16)

> "E OSc showed under recents, but when i loaded it, it was apparently still
> doing a turn. even though it was under recents, and even though the tab had
> been closed for a long time."

Two claims, and only one of them was a bug.

**The lobby was right.** The session genuinely was not running. `/api/running`
listed it as runtime `s13` created 06:23:00, and the only `claude.exe` holding
that session id (pid 52792, `--resume 1aa74fb0-…`) started 06:23:**01** — one
second *after* the runtime, with a parent chain leading back to the hub. That
is s13's own CLI, not a survivor of the old tab. Recents was correct.

*(An intermediate reading said otherwise: a `--resume` scan missed the flag
because the command lines were truncated at 230 characters, which briefly
looked like broken foreign-holder detection. Re-reading the full command lines
found `--resume <sid>` on all five live CLIs. `proc_guard` is fine.)*

**It really was mid-turn, and we started it.** The chain, from the log:

```
09-11 19:24:24  runtime s3 started (cwd=E:\...\os, resume=1aa74fb0-…)
09-16 04:01:32  bg task bpj3iib57 started: during_turn=True      <- working
09-16 04:01:34  runtime s3 idle for 300s — tearing down
09-16 04:01:34  run_turn finally: normal_completion=False exit_exc=CancelledError
09-16 04:01:39  reaped 7 orphaned MCP/tool process(es)
09-16 06:23:00  runtime s13 started (resume=1aa74fb0-…)          <- user opens it
09-16 06:23:30  ghost turn begin: SDK streaming without active run_turn
```

The turn was killed at 04:01 by the idle teardown of a *working* runtime — the
fourth occurrence of that bug, in a hub too old to have the fix. At 06:23 the
session was opened, and `CLAUDE_CODE_RESUME_INTERRUPTED_TURN=1` (which we set
on every connect, deliberately — see design.md §6) made the CLI pick the
unfinished turn back up. Thirty seconds later it was streaming.

**So the defect is the silence.** Recovering the turn is the behaviour we
want; doing it with no announcement means output appears with nobody having
typed anything, and the only trace is a `WARNING` in a log the user is not
reading. The session now says so, once, in the transcript: *"Continuing a turn
that was interrupted before this session was last closed. Nothing was sent
from here — the CLI picked the unfinished turn back up on resume. Use the
interrupt button (Ctrl+C) to stop it."*

The hard part is **not** saying it the rest of the time. Most ghost turns are
a background task's `<task-notification>` waking the model mid-session, and
announcing an interruption there would be a lie told several times a session.
The discriminator is `_unprompted_resume_pending`: armed at connect only when
the connect carries `resume=`, cleared the instant `run_turn` sends a prompt,
consumed by the ghost-turn path. True in exactly one window — *resumed, and we
have not asked for anything yet* — in which no other author is possible.

**Verified:** `tests/test_resumed_turn_notice.py` (15) and the `resumenotice`
mutation target (`sdk_bridge.py` × 9), all caught on the first sweep. The
mutations that matter are the two opposite failures: never announcing (the
reported bug) and always announcing (the lie). Both die.

## A tab that lost its session could never reopen it — FIXED (2026-09-16)

**Reported:** *"my E OSc session somehow turned into a sessions window. so i
clicked on 'E OSc' in the recents list to try to reload it. every time i click
on it, it does nothing."*

Both halves were one bug, and "every time" was the tell: nothing about the
situation changed by clicking again, because the click was a **self-focus**.

`onAttached` stamps a tab `window.name = 'orch2-sess-<sid>'` so other tabs can
raise it instead of opening a duplicate. When that session's runtime went away,
the server pushed a `lobby_notice`, and `showNotice` forces the lobby in front
— but left `_hasSession` true, so the landing path that resets the name never
ran. (`onSessionClosed` *does* reset it; that is a different route, and not the
one this took.) The tab was then a lobby wearing a session's name.

Then `_focusOrOpen(url, 'orch2-sess-<sid>')` ran `window.open('', name)`.
**Browsing-context lookup by name checks the current context first**, so the
tab got *itself* back — found it non-blank, and called `focus()` on the window
that already had focus. That was the click's entire effect.

Two fixes, because the two halves fail independently:

* `_focusOrOpen` now returns early when `w === window` and navigates here.
  Navigating is also the *right* answer: "focus the tab showing this session",
  when that tab is this one, means show it. This holds however the name went
  stale.
* `showNotice` relinquishes the per-session name. A notice forces the lobby in
  front, so whatever the tab was showing, it is not showing it now — and
  keeping the name makes this tab the target of every *other* tab's "focus the
  tab with this session", handing them a lobby. That is the same bug seen from
  outside, which the first fix does not address.

`E OSc`'s runtime was indeed gone: `/api/running` showed `E OSb`, `E OSa`,
`Good Photons` and `orchestrator2c`, and no OSc.

**Verified:** 4 tests and the `staletab` mutation target (`static/lobby.js`
× 4), all caught.

### The test passed for the wrong reason, twice

Worth recording, because both mistakes are invisible without a sweep:

1. **The harness had no page URL.** jsdom cannot resolve a relative href
   against `about:blank`, so assigning one *throws*, and a throw escaping
   mid-handler hid what the handler would have done next — masking the
   difference between fixed and mutant entirely. `makePage` now builds its
   JSDOM with a real `url`, where jsdom merely declines to navigate.
2. **`render()` reset the very name under test.** Rendering into a
   never-attached tab is itself a landing, which sets `window.name` to
   `orch2-lobby`; the test set the stale name *before* rendering, so by the
   time it clicked there was no stale name left. It is now applied after the
   render — matching the real sequence: attach, session dies, notice, name left
   behind.

Both were only exposed by the mutation surviving. The tests were green
throughout.

## The side panels were unreachable on mobile — in practice, not in code — FIXED (2026-09-14)

**Reported:** *"on mobile, i can't see any of the side panes including queued
prompts."*

The mechanism was **already correct**, which is why reading the CSS and the JS
got nowhere. `tools/probe_mobile_sidebar.py` (new) loads the real page at a
412×915 phone viewport against a live hub and measures: the toggle is on screen
at (8, 801), tapping it puts a 350×915 sidebar in front, all four panels
render. The panes were one tap away the whole time.

What was wrong was that nothing made that tap findable:

* **An unlabelled 30×25 cog next to a labelled "☰ Sessions".** Beside a
  labelled sibling, a bare glyph reads as decoration. It now says
  **"⚙ Panels"**, at 73×32. Notably the *same* media-query block already
  sizes `#send-btn`, `#input-box` and the lobby's buttons for a finger — the
  toggle was simply missed when that pass was done.
* **Nothing gave a reason to tap it.** The status bar says `bg wait (1)` out
  loud but has never mentioned queued prompts, so the one panel with something
  in it was the one panel with no outward sign. The toggle now carries the
  queue count when non-zero.

Two design choices inside that badge: it counts **the queue only**, not a sum
across panels, because a single number standing for several different things
is a number you cannot act on; and it is **hidden at zero**, because a badge
that is always present stops being a signal.

### Two plausible theories the probe killed

Worth recording, because both were confident guesses that reading the source
supported and measurement refused:

1. *"The status bar scrolls horizontally, so the toggle scrolls out of reach."*
   It does not overflow on a phone — the mobile rules hide the labels and
   separators, so `scrollWidth == clientWidth == 396`. The toggle is still at
   x=8 after scrolling to the far end.
2. *"A bare `/` never attaches, so the lobby covers everything."* The lobby
   does stay up on a bare `/` — **deliberately**, as the hub's front door
   (`websocket_endpoint`'s landing branch says so). With `?rid=` it attaches in
   under a second.

**Verified:** 8 tests in `tests/reconnect_on_show.test.js` (toggle label, badge
appears/clears/explains itself, open/close, backdrop-closes) and the
`panelstoggle` mutation target (`static/status.js` × 6), all caught.

## Only one orchestrator2 tab was possible on mobile — FIXED (2026-09-14)

**Reported:** *"on mobile (android chrome at least) i seem to only be able to
have one orchestrator2 tab open at a time. i go to sessions, it replaces the
current tab with sessions. i click on a session, it replaces the tab with that
session."*

Accurate, and the cause was structural rather than a bug in the mobile path.
The lobby's cards were `<div>`s with click handlers, and **a click handler has
no "open in new tab"**: long-press offers nothing, and neither does
ctrl/middle-click on a desktop. Combined with the (correct) mobile decision to
switch the current tab in place — a page cannot raise or focus another tab on
Android Chrome or iOS Safari, so opening one would strand the user on a
background tab — the in-place switch was the *only* route to a session. One
tab was all you could have.

**Fix: hand the job to the browser.** The card and recent-item titles, and the
☰ Sessions control, are now anchors carrying the URL the click handler would
navigate to (`?rid=` for a local runtime, `?open=&cwd=&account=` otherwise).
The delegated handlers `preventDefault()` a *plain* click, so every existing
flow is byte-for-byte unchanged, and deliberately **do not touch a modified
one** — ctrl, cmd, shift, alt or middle-click is the user asking the browser
for a new tab, and swallowing it would take back the capability the anchors
exist to provide.

Not a new control, because the platform already has one: it is the affordance
users know, it works in contexts a page-level button cannot (long-press, drag
to bookmarks, copy link), and it costs no UI. A one-line hint in the lobby
makes it discoverable on mobile, where nothing else advertises it.

**Verified:** 12 tests across `tests/lobby_running_card.test.js` and
`tests/reconnect_on_show.test.js`, and a new `newtab` mutation target
(`static/lobby.js` × 12), all caught — including "a modified click is
swallowed too, so no new tab ever opens" and one per modifier key, since
missing any single one silently removes the gesture for a whole platform.

Two things came out of the sweep:

* **`newtab` needs two test files.** The markup is checkable against the module
  alone in jsdom, but what a *click* does needs the whole app in the real page.
  `tools/mutate.py`'s node runner took one file; it now runs several and
  requires all to pass, the way the pytest runner already did.
* **Two `lobbycard` mutations went stale** the moment the title stopped being a
  `<span>`, and reported as `anchor not unique` rather than passing silently —
  which is the sweep catching a change to the code it pins. Re-anchored.

## A session was torn down for having no socket open, not for being idle — FIXED (2026-09-14)

**Reported:** *"i think if the only web connection to a session is via mobile,
it shouldn't have a session timeout for the web connection because phones often
go to sleep"*. Correct, and reading that path turned up a second, worse case in
the same three lines.

`_idle_teardown_after` tested exactly one thing: `not rt.clients`. "No socket is
open" was standing in for "nobody cares about this session", and it is a bad
proxy twice over.

### 1. A sleeping phone is not a viewer leaving

Phones suspend their browser a minute or two after the screen locks — well
inside the 300 s default. So the session whose *only* viewer is a phone gets
reaped while that viewer is still using it. Worse, the frontend already
reconnects on `visibilitychange`, so the user unlocks the phone and watches the
tab reconnect to a session that no longer exists.

The socket's `User-Agent` is recorded **at accept** into `_mobile_ws`, and
`_maybe_start_idle_timer` picks its timeout from the *last viewer to leave*:
`--mobile-idle-timeout`, default **0 = never**.

Read at accept rather than asked of the page on purpose: the fact is needed at
the moment the socket *dies*, which on a sleeping phone is exactly when the page
is no longer running to answer. `_MOBILE_UA_RE` mirrors `lobby.js`'s
`_isMobile()` regex deliberately — the two decide the same thing about the same
client, and disagreeing would be worse than either being wrong. Shared blind
spot: iPadOS Safari reports a desktop UA, so an iPad counts as a desktop.

**Known limitation, stated rather than papered over.** A *laptop* suspending has
the same shape and is not covered. Generalising to "any abnormal close" would
catch it, but 1006 is common enough — a crashed tab, flaky wifi — that almost
every session would become exempt, so the narrower and more stable signal won.

### 2. A session that is working is not idle

This one was not reported and is the more damaging of the two: **a turn left
running was killed mid-edit**, along with any background tasks it had spawned,
five minutes after the last tab closed. Closing the tab is precisely what you do
*because* a server-side agent keeps working without you.

**It happened four times, and the log records all four.** Asked afterwards
"which session was closed mid-write?", so the claim was checked rather than
left as a reading of the code. Across 686,310 log lines there are 206 idle
teardowns; exactly four are followed, in the *same process*, by a turn dying
of `CancelledError` within milliseconds:

| when | runtime | hub pid | gap |
|---|---|---|---|
| 2026-07-18 21:00:24 | `s6` | 8808 | +1 ms |
| 2026-08-15 18:36:44 | `s3` | 6760 | +3 ms |
| 2026-09-08 05:51:57 | `s4` | 132168 | +2 ms |
| 2026-09-16 04:01:34 | `s3` | 20276 | +1 ms |

The fourth landed **two days after the fix was written**, and is not a
regression: hub pid 20276 started 2026-09-11 19:24 and had never been
restarted, so it was still running pre-fix code. A fix on disk is not a fix in
the process -- which is the entire reason the lobby grew a staleness banner,
and that banner was not running in that hub either. It cost that session a
turn and 7 orphaned MCP processes, and it is what produced the resumed-turn
report below.

The last one is fully reconstructable and is the worst case the design allows:

```
04:52:45  run_turn start  bridge=78c0  'Autonomous-loop wakeup (scheduled by
                                        you). Resume work now: ...'
05:51:26  bg task bubqawmqp (Fix import and test) started  during_turn=True
05:51:57,730  runtime s4 idle for 300s - tearing down
05:51:57,732  run_turn finally: normal_completion=False exit_exc=CancelledError
05:51:57,733  run_turn cancelled - worker_loop exiting
05:51:57,733  dispatcher cancelled: gen=1 msgs=14824
05:52:02,906  reaped 13 orphaned MCP/tool process(es)
```

A **59-minute** turn, thirty-one seconds after it started a task called "Fix
import and test", killed for having no viewer. It was an *autonomous-loop*
session -- which is the shape most exposed to this, because working unattended
with no tab open is exactly what it is for, and "no tab open" was the entire
test.

`_idle_teardown_after` now re-arms when `state.busy` or `background_tasks` is
non-empty, and reaps on the first pass after the session goes quiet. Unbounded,
unlike the wakeup deferral (which *is* capped): a wakeup that keeps landing
mid-turn has already failed at its job, whereas a session that keeps working is
succeeding at its, and a genuinely wedged one is still closable from the lobby.

The runtime remembers whether its countdown is the mobile one (`idle_mobile`),
so a deferral re-arms with the grace it was armed with instead of quietly
dropping onto the short clock — which would have reaped the phone-viewed
session after all, just five minutes later.

**Verified:** `tests/test_idle_teardown.py` (22) and the `idle` mutation target
(`server.py` × 12), all caught.

Two survivors on the first sweep, and they are the usual two shapes. *The
production registration line was untested* — every test went through a helper
that **replicated** what `websocket_endpoint` does at accept, so deleting the
real line changed nothing observable; there is now a test that drives the
endpoint itself with a socket that disconnects immediately. And clearing
`rt.idle_mobile` in `_cancel_idle_timer` was **unreachable**: its only reader is
the deferral re-arm, which always follows an arm that just wrote it. Deleted
rather than covered, with a comment saying why.

## A peer message ran as a turn without ever appearing in the queue pane — FIXED (2026-09-07)

**Reported:** *"when one agent sends a message to another agent, does that show
up in the queue in the left pane? it doesn't seem to?"* It did not.

`poll_agent_comms` appends delivered peer traffic straight to
`state.queued_prompts`. That container notifies two subscribers — persistence
(`on_change`) and the worker poke (`add_listener`) — and **neither pushes
anything to the browser**. The queue panel is refreshed by `queue_update`
broadcasts issued *by the call sites*, and `server.py`'s five writers all do it;
the bridge's own writer did not.

`SDKBridge._broadcast_queue` — written for exactly this, and documented as "the
twin of server.py's `_broadcast_queue_update`" — had **zero callers**.

So a peer message was queued, drained at the next turn boundary and *acted on*,
while never appearing in the pane it was sitting in. That is the worst
arrangement of the three possibilities: the session does something the operator
cannot see waiting to happen. `poll_agent_comms`'s own docstring even claimed it
"is visible in the queue panel".

**Fix, and where it was put.** Not a call in `poll_agent_comms` — wired to the
**container**, as a second listener beside the worker poke:

```python
state.queued_prompts.add_listener(self._poke_for_queued_prompt)
state.queued_prompts.add_listener(self._schedule_queue_push)
```

`PersistentDeque`'s docstring already gives the reason, one level up: *"every
'the prompt didn't send' bug in this project's history has been a writer that
forgot to poke. A writer added tomorrow gets it for free and cannot reintroduce
the bug."* This was the same class of bug with "broadcast" in place of "poke",
so it belongs in the same place. `server.py`'s explicit pushes stay — they
cover runtimes whose bridge is not wired yet, and a duplicate full-state push is
harmless.

Two details: `_fire` is **synchronous** and can run with no event loop at all (a
test, a worker thread), so the listener *schedules* and silently does nothing
when there is no loop — a missing panel refresh must never break a queue
operation. And a latch coalesces a burst, so an agent-comms poll delivering
three messages costs one broadcast rather than three.

**Verified:** the before/after was measured rather than assumed — with the
listener removed, one message produced `queue_update` broadcasts: 0 while
`queued_prompts` held 1. Pinned by 5 tests in
`tests/test_agent_comms_checklist.py` and the `queuepanel` mutation target
(`sdk_bridge.py` × 5, all caught).

## A scheduled wakeup was invisible in the status bar — FIXED (2026-09-07)

Requested alongside the agent-comms checklist: *"i'd like a status indicator
added to the status bar that indicates whether a wakeup or loop is scheduled
and maybe in how many seconds"*.

`/loop` made the wakeup loop **askable**; the bar makes it **visible**, which
is the difference between noticing a scheduled wakeup and having to suspect
one. A `loop` field appears with a live countdown when one is armed and is
hidden the rest of the time — most sessions never use the loop, and a
permanent `loop --` would be noise in a bar that is already full.

Three details that are not decoration:

- **The deadline crosses as a wall-clock epoch**, not the bridge's
  `time.monotonic()` value. The browser can only compare it against its own
  clock; a monotonic number would render as an arbitrary duration.
- **The countdown ticks locally.** Status pushes are ~2 s and can stall while
  the SDK blocks the event loop, and a countdown that freezes is worse than
  none.
- **The deferral count is shown.** "Armed" on its own implies it will fire; a
  non-zero count means the wakeup keeps landing mid-turn and is being pushed
  back, and past `WAKEUP_MAX_DEFERS` it is dropped rather than fired.

Cleared on every exit from the armed state — cancel, `/loop off`, fire, and
the deferral-cap drop — because a countdown that outlives its wakeup promises
a prompt that is not coming. Each of those four has its own mutation.

**Verified:** `tests/test_loop_control.py` (47) with the `loop` target
(`sdk_bridge.py` × 17), and `tests/reconnect_on_show.test.js` (34) with a new
`statusloop` target (`static/status.js` × 8).

Two things came out of that sweep. The harness stubbed `setInterval` to a
no-op, so "does the countdown tick" was unaskable — it now records intervals
and fires them explicitly against a controllable `Date.now`, since firing them
from `advance()` would run a 1 s ticker six hundred thousand times under the
existing `advance(600000)`. And `Math.max(0, deadline - now)` **survived**:
`left < 1` already covers a past deadline, so the clamp was unreachable. It was
deleted rather than covered, per the sweep's own rule that a mutation nothing
can distinguish is a fact about the code.

## Six `if (window.X)` guards were permanently false, silently — FIXED (2026-09-07)

**Reported:** *"i did /move and it's stuck on 'loading accounts'."*

The server was fine — both halves of the reply build in under 0.1 s and return
correct data. The account list arrived and was thrown away by this, in
`app.js`:

```js
if (window.Switch) Switch.renderAccounts(msg);
```

Every frontend module is `const Switch = (() => {…})()` at the top level of a
classic script. A top-level `const` creates a binding in the global
**declarative** environment record; only `var` and function declarations become
properties of `window`. So `window.Switch` was `undefined` — always. The guard
reads as caution and means *never*.

`commands.js` calls `Switch.open()` directly, so the overlay opened and said
"Loading accounts…" — and then nothing, forever.

**There were six**, every one of them dead:

| guard | what never happened |
|---|---|
| `window.Switch` (×3) | account list never rendered; switch errors never surfaced; the overlay never closed after a successful switch |
| `window.Lobby && Lobby.onReconnect` | a reconnected lobby never resubscribed to live updates |
| `window.Lobby && Lobby.showReloadingAndPoll` | the restart-in-progress UI never ran |
| `window.App && typeof App.openModal` (chat.js) | **every modal** — `/help`, `/status`, `/debug` — took the "App is not available" fallback and rendered inline |

That last one had been degrading `/help` for as long as the guard existed, and
quietly enough to read as a design choice.

**Fix:** `typeof X !== 'undefined'`, which is the correct test for a possibly
absent *lexical* binding (it does not throw when the name was never declared,
which is exactly the case the guard was written for).

### Why no test caught it

The jsdom harness ended with:

```js
+ '\n;window.App = App; window.Lobby = Lobby; window.Chat = Chat;'
```

**The fixture assigned the very properties the guards looked for.** It
manufactured a world the browser never provides, and made six dead branches
look alive. It did not assign `window.Switch` at all, which is why that path
was never exercised even by accident.

Now `window.__mods = {…}` — a deliberately test-only name that production code
cannot satisfy by accident — plus a test asserting that **no** module is
reachable as a `window` property, so a future `if (window.Chat)` fails loudly
instead of silently.

**Verified:** 7 new tests in `tests/reconnect_on_show.test.js`, and mutations
that restore each original guard verbatim: `app` (13/13) and a new `chat-modal`
target (2/2). The latter's first run had a survivor worth keeping — the modal
test asserted the modal opened but not that the content was not *also* echoed
inline, so "do both" slipped past.

**Standing lesson:** a test fixture that reaches into the module namespace must
do it under a name production never looks for. Anything else can accidentally
satisfy a production lookup, and then the test proves the opposite of what it
claims.

## `/switch` renamed to `/move`, internals included (2026-09-07)

Once the command could change **directory** as well as account, "switch" named
only half of what it did — and for most commands the one-line entry in `/help`
is all the documentation anyone reads. It is now `/move` (`/move <path>`
prefills the directory).

**No alias, and the rename goes all the way through**: the module (`Move`),
`static/move.js`, every `.move-*` CSS class, the `move_*` wire messages,
`_do_move` / `_move_accounts_payload` / `_move_dirs_payload`,
`tests/move_overlay.test.js`, `tests/test_move_directory.py`, and the
`movecopy` / `moveserver` / `moveui` / `movecmd` mutation targets.

**This reversed a decision made an hour earlier.** The first pass kept
`/switch` as an alias and left the internals alone, reasoning that renaming
them would churn `server.py`, `app.js`, the stylesheet, three test files and
every mutation anchor for no user-visible gain — and wrote a paragraph in
design.md explaining the mismatch so it was "documented rather than merely
tolerated".

Two things were wrong with that. The alias was justified by *other people's*
muscle memory, and there are no other people (`"i don't think anybody else uses
orchestrator2"`). And needing a paragraph to explain why a name is wrong is the
argument for fixing the name, not for keeping it: the explanation is a
permanent tax on every future reader, while the churn is paid once. "It's a lot
of mechanical edits" is not a reason — especially with 864 tests and 26
mutation targets to catch a slip.

**The one real hazard, and how it was handled.** `switch-cwd` is *both* a CSS
class in the overlay and the command kind for `/cwd` in `commands.py` /
`server.py`. A blanket `s/switch/move/` would have silently broken `/cwd`. The
rename script therefore worked from explicit per-file identifier lists, never a
global substitution, and the two uses were confirmed to live in disjoint files
first. Prose uses of the word ("a `/model` switch", "the external-access
switch") were left alone for the same reason.

Verified by re-resolving **every anchor of all 26 mutation targets** after the
rename (0 broken), then the full suite.

## A bug fixed on Sep 2 was still happening on Sep 7, because the hub had never restarted — FIXED (2026-09-07)

**Reported:** *"isn't the client supposed to wake up the model any time a
background task completes? one just completed in my Good Photons session and
didn't wake it up, it happened right after i did a model change."*

Accurate in every detail, including the connection to the model change — and
the bug was `A /model switch silently orphaned running background tasks, and
dropped the session out of bg-wait`, **fixed 2026-09-02**, five days earlier.

### How it was pinned down

The log had the answer, in a line that cannot be produced by the current source:

```
2026-09-07 00:25:58,177 [pid 81672] [sdk_bridge] INFO: cleared 1 stale bg task(s) on reconnect
2026-09-07 00:26:03,280 [pid 81672] [sdk_bridge] INFO: connect: resume=8d532b0a…, cwd=D:\visual studio projects\forward raytracer
```

`grep "stale bg task" sdk_bridge.py` → nothing. That string survives only in
`known-issues.md`, quoted as the code the 2026-09-02 fix **deleted**. Then:

```
>>> psutil.Process(81672).create_time()
2026-09-01 22:59:30
```

The hub holding that session started the day *before* its own fix. Python is
read once at import, so it had been executing the pre-fix `reconnect()` for six
days: clearing `background_tasks` silently, so `_between_turns` never parked in
bg-wait and the `bg-all-done` wakeup — which counts the tasks it just erased —
could never fire.

Current behaviour, for contrast: `/model` goes through `_reconnect_or_defer`,
which **defers** the switch while background tasks are running and says so; a
reconnect that does happen calls `_orphan_bg_tasks`, which warns *the user*
rather than logging an INFO line nobody reads.

### The actual defect: nothing said the hub was stale

The session bug was already fixed. What was not fixed is that **a running hub
had no way to tell you it was running old code**, and no way for anyone else to
tell either without comparing process start times against a changelog. Six days
of a fixed bug still happening, with the fix on disk unread.

So: `snapshot_sources()` records the mtime of every loaded project `.py` once
`_deferred_bridge_startup` has finished importing (in the `finally`, so a hub
whose bridge failed to start still gets a baseline — that hub is still running
code, and is the case where someone is most likely to be editing). The lobby's
`session_list` carries the result and `lobby.js` renders a warning band above
the notice bar, next to ⟳ Restart:

> **This hub is running older code.** 14 loaded source files changed on disk
> since it started 6d ago. Restart to pick them up.
> `sdk_bridge.py, server.py, session.py, …`

Two exclusions keep it from crying wolf, and both are pinned by mutations:

- **Only project files.** A `pip install` does not make *this* hub stale.
- **Only `.py`.** JS and CSS are re-read per request, so a browser refresh
  picks them up. Telling someone to restart the server for a CSS edit trains
  them to ignore the notice, which costs more than the notice ever saves.

A module imported *after* the snapshot is baselined on first sighting rather
than flagged — a genuinely late import (`sdk_bridge` and friends load lazily)
is running current code, so calling it stale would be exactly backwards.

**Verified:** `tests/test_stale_sources.py` (16) with the `stale` mutation
target (`server.py` × 15), and the banner half in
`tests/lobby_running_card.test.js` (37) / `lobbycard` (`static/lobby.js` × 29).

The sweep's one survivor is worth recording: deleting the **call site** of
`snapshot_sources` was invisible, because every test established its own
baseline first. Without that call the first check baselines whatever it finds
and nothing is ever stale — the feature *silently absent* rather than broken,
which is precisely the shape of the bug it exists to catch. Now tested by
driving `_deferred_bridge_startup` with the heavy import forced to fail.

## A session working away in another window was drawn as idle — FIXED (2026-09-07)

**Not reported — opened from the entry below**, which noticed that `viewers: 0`
on a foreign card was a placeholder printed as a fact, and recorded that
`busy: False` was the same kind of lie and still being drawn as the idle dot.

`_foreign_running_entry` builds a running-card meta for a session held by
*another* hub process. A process scan (`proc_guard.map_foreign_session_holders`)
can see that something holds the session and where it runs. It cannot see
whether a turn is in flight, how many tabs are watching, or what the session has
since been renamed to. Those were filled in:

```python
"busy": False,        # another process owns the turn state
"viewers": 0,
```

and `lobby.js` did `m.busy ? 'busy' : 'idle'`, so **a session hammering away in
another window was drawn with the grey idle dot** — the precise opposite of what
the foreign card exists to tell you. The comment beside `busy` even said why it
was unknowable, and the value said `False` anyway.

### The fix: ask the hub that owns it

The owning hub knows, and already has the answer in memory. New endpoint
`GET /api/running` returns this hub's `rt.meta()` list; `_probe_foreign_hubs`
calls it on peers and merges the result — real `busy`, `viewers`,
`last_activity`, and a live `title` that beats the disk one (a rename reaches
the owning hub's memory well before it reaches the JSONL).

**One request per peer hub, not per session** — three sessions in one hub is one
question.

Three guards, because the thing being asked may itself be what's wrong:

| guard | value | why |
|---|---|---|
| loopback only | `127.0.0.1` | peers are local by definition; also what skips the external-auth middleware, so no credentials |
| deadline | `_HUB_PROBE_TIMEOUT` 1 s | a peer that accepts the connection and never answers would hold the executor thread and stall the session list for **every tab here** |
| failure backoff | `_HUB_PROBE_FAIL_TTL` 30 s vs a 2 s success TTL | otherwise one wedged peer adds its timeout to *every* lobby tick — the lobby made slow by the exact condition it is reporting |

### The other half: `None` is not `False`

When the answer can't be had — no port (a bare `claude --resume` in a terminal
has no API), the hub didn't answer, or it answered without this session (a
closing-session race) — `busy`/`viewers` are **`None`** and `live_known` is
false. The dot has a **third state**: a hollow ring, whose tooltip distinguishes
*"the hub on port N didn't answer"* from *"that isn't an orchestrator2 hub, so
there is nothing to ask"*.

Two distinctions that are easy to collapse and were deliberately kept:

- **`{}` vs `None` from the probe.** "Asked, it has no sessions" and "couldn't
  ask" are different facts; an idle hub is not a dead one.
- **`typeof m.viewers === 'number'`, not `m.viewers || …`.** `0` is falsy, so
  the usual idiom cannot tell "nobody is watching" from "we have no idea", and
  those mean opposite things.

**Verified:** `tests/test_foreign_running_sessions.py` (30) with the `foreign`
mutation target (`server.py` × 25), and `tests/lobby_running_card.test.js` (28)
with `lobbycard` (`static/lobby.js` × 23) — all caught.

One survived the first server sweep and is worth recording: *"the probe has no
timeout, so a hung peer hangs the session list."* The stub was
`def fake(url, timeout=None)` — it *accepted* the timeout and dropped it, so
nothing could observe whether one was passed. A fake that swallows the argument
under test makes the test vacuous. It now records `(url, timeout)` and asserts
the deadline is both present and the configured one.

## A lobby card claimed to be "current" and squeezed out its own title — FIXED (2026-09-06)

**Reported** with a screenshot: *"Good Photons is showing with its title mostly
blocked by other things on the same line. and why is 'current' there anyway,
when none of the other ones say 'current'?"* Two independent bugs in one card,
both on the **foreign** path (a session running in another hub process).

### 1. `null === null`

```js
if (m.rid === _currentRid) card.classList.add('current');
...
${m.rid === _currentRid ? '<span class="lobby-current-tag">current</span>' : ''}
```

`_foreign_running_entry()` sets `"rid": None` — there is nothing in *this* hub
to attach to. `_currentRid` is `null` in a tab that has not attached to a
session, which is exactly what a standalone `?lobby=1` tab is. So the
comparison was `null === null`, and **every foreign card in that tab labelled
itself current**.

Worth stating plainly because it is the opposite of the truth: a session
running in another hub is the one session this tab is definitely *not*
attached to — which the same card said two badges later with *"other window
:63978"*.

Now `const isCurrent = !!m.rid && m.rid === _currentRid && !m.foreign;`, used
for both the class and the badge so they cannot disagree.

The `!m.foreign` clause looks redundant today (a foreign rid is always null)
and duly **survived its mutation on the first sweep**. It was kept and given a
test rather than trimmed, because rids are per-hub and sequential (`s1`, `s2`,
…): two hubs on one machine routinely mint the same ones, so the day a foreign
card gains a rid, `s3 === s3` resurrects this bug against a session in a
different process entirely.

### 2. The title lost the space fight to its own annotations

```css
.lobby-card-title { flex: 1; }        /* = flex: 1 1 0% — a ZERO basis */
.lobby-foreign-tag { flex: 0 0 auto; } /* never shrinks */
```

With a zero basis the title got only what the badges left over, and the badges
never shrink. In a three-across grid a card is ~275px and *"other window
:63978"* is roughly half of it, so **"Good Photons" rendered as "Goo…"**.

Two changes:

- `flex: 1 1 auto; min-width: 0` — the title starts at its natural width and
  shrinks proportionally instead of last.
- The foreign marker left the header. A card's title is its identity and
  outranks every annotation on it; the dashed border and dimming already say
  "not yours" before any text is read, so the tag is explanatory detail. The
  header now holds only the status dot, the title, the "current" badge and the
  close button.

It did not go into the meta row: a bordered pill is not a field in that row's
`a · b · c` run, and at ~120px it pushed the run past the card width and
wrapped, stranding a separator at the end of the first line. It has its own
row, like the countdown badge.

The title also gained a `title=` tooltip, so an elided one is still readable.

### 3. Two things found in the same code path

- **A dangling separator.** The meta row emitted the rid unconditionally, so a
  foreign card (no rid) rendered `… · <empty> · .claude-account-b`. The row is
  now assembled from a list and joined, so an absent part takes its separator
  with it.
- **"0 viewers" was a fabrication.** `viewers: 0` in a foreign entry is a
  placeholder — another process owns the session, so we have no way to count
  its viewers — and the card printed it as fact. (`busy: false` was the same
  kind of placeholder, rendered as an idle dot. Both are fixed properly in the
  next entry, which was opened from here.)

**Verified:** `tests/lobby_running_card.test.js` (20, jsdom) and the
`lobbycard` mutation target (`static/lobby.js` × 16), all caught. Two survived
the first sweep — the `!m.foreign` clause above, and a "no dangling separator"
assertion that was equally satisfied by having *no* separators at all, so
`join('')` slipped past it.

jsdom does no layout, so none of that could confirm the title actually *fits*.
`tools/probe_lobby_card.py` renders the real cards with the real CSS in
Chromium and measures: **40px of title box for text needing 91px** with the
three fixes undone (`--legacy`), i.e. exactly the reported "Goo…", against
**237px, unclipped** after.

## Moving the interpreter to C: silently narrowed the lobby to one account, and broke `/move` — FIXED (2026-09-06)

**Found while adding the directory half of `/move`**, not reported — which is
the point of the entry. It had been live since the interpreter switch earlier
the same day and produced no error anywhere.

`copy_session.py` has had this section header for as long as it has existed:

```python
# ---------------------------------------------------------------------------
# Discovery / parsing (no TUI dependency — importable & testable on its own)
# ---------------------------------------------------------------------------
```

…and, 300 lines further down, a module-scope `from textual import work`. The
claim was false the whole time; it just never cost anything, because every
interpreter that ran the hub happened to have `textual` installed. `D:\python314`
did. The C: interpreter the hub moved to for the launch-speed fix did not.

**What that produced.** Two failures, neither of which announced itself:

1. **The lobby quietly showed one account.** `_recent_disk_sessions` does
   `from copy_session import discover_claude_dirs` inside a broad
   `except Exception`, whose fallback is *a list containing only the current
   config dir*. So the "scan every Claude account on this machine" feature
   degraded to "scan the one we're already using", logged a warning nobody was
   reading, and looked exactly like having no other sessions.
2. **`/move` failed outright.** `_move_accounts_payload` and `_do_move`
   import from `copy_session` with no guard at all.

Both are the same defect: an optional, cosmetic dependency was load-bearing for
code that has nothing to do with it.

**Fix.** The TUI moved to `copy_session_tui.py`. `copy_session.py` keeps the
discovery/parsing half plus a `main()` that imports the wizard *only when the
wizard is about to run*, and reports the missing package by name and how to
install it instead of raising:

```
The session picker needs the 'textual' package: No module named 'textual'
Install it with:  pip install textual
```

`textual` was also missing from `requirements.txt` entirely, which is how the
C: interpreter came to be set up without it — `pip install -r requirements.txt`
was run and did the right thing by its own lights. It is in there now, with its
scope written next to it.

**Verified:** `tests/test_move_directory.py` runs a subprocess with a
`MetaPathFinder` that raises on any `textual` import, and asserts (a) the pure
half imports and works anyway, and (b) `main()` exits 2 with a message naming
the package rather than a traceback. Note the first attempt at that probe used
the `find_module`/`load_module` finder protocol, **removed in Python 3.12** — it
was silently inert, textual imported normally, and the check "passed" by
launching the real wizard. Blockers written for this must implement `find_spec`.

**Standing lesson:** a comment asserting a module has no heavy dependency is
worth nothing unless something imports it that way. The test above is the
cheapest possible enforcement.

## The wakeup loop could not be stopped — by the agent or the operator — FIXED (2026-09-06)

**Reported**, by the agent trapped in it, relayed by the operator:

> "No cron job exists and I've stopped the dynamic loop twice, so these wakeups
> are coming from outside my control — you may need to end the /loop on your
> end. Still stopped for the migration: nothing uncommitted, nothing unpushed,
> no build processes running."

Every clause was accurate, including the diagnosis. The wakeups *were* coming
from outside its control, and there was no `/loop` to end on the operator's end
either. The agent had done the correct thing twice and been punished for it.

**Why the loop is hub state at all.** The CLI does not act on `ScheduleWakeup`
in streaming mode, so orchestrator2 implements it: `_maybe_arm_wakeup`
intercepts the tool call, arms an `asyncio` timer, and `_wakeup_timer` later
injects the prompt as a user message. The loop therefore lives *outside* the
conversation — invisible to the agent, which sees only its own tool call, and
until now invisible to the operator, who saw nothing.

### 1. `ScheduleWakeup(stop=true)` armed a wakeup instead of ending one

`_maybe_arm_wakeup` read `delaySeconds` and `prompt`. It never read `stop`. The
tool documents that stopping means calling it with `stop: true` and **every
other field omitted** — so on the stop call `delaySeconds` was absent and fell
to its 60 s default, and `prompt` was absent and resolved to
`WAKEUP_RESOLVED_PROMPT`. The request to stop was indistinguishable from a
request to arm the fastest loop the tool allows.

This is why "I've stopped the dynamic loop twice" made things worse rather than
better: each stop armed another 60 s wakeup. The one control the agent had was
wired backwards.

**Fix:** `stop` is read first and short-circuits — cancel, set `_loop_stopped`,
log, return. `bool(inp.get("stop"))` rather than `"stop" in inp`, so an explicit
`stop: false` still arms (pinned as its own mutation).

### 2. A wakeup that landed mid-turn re-armed itself forever

Deferring a wakeup that fires during a live turn is correct and deliberate
(design.md §"A wakeup that fires mid-turn is deferred"). But the re-arm was
unconditional:

```python
if self.state.busy:
    self._arm_wakeup(WAKEUP_MIN_DELAY, prompt)   # every 60s, forever
    return
```

A session that stayed busy — exactly the long autonomous runs the loop is for —
re-armed every 60 s with no limit. `orchestrator2.log` already carried the
evidence: `wakeup fired mid-turn — deferring 60s` followed by `wakeup armed:
delay=60s`, on repeat, for as long as the session worked.

**Fix:** the deferral is bounded by `WAKEUP_MAX_DEFERS` (10, in `config.py`).
After ten consecutive deferrals the wakeup is dropped with a log line saying
why — ten straight busy checks mean the session is working, which is the
condition the loop exists to restore, so there is nothing left to nudge. The
deferral path passes `_arm_wakeup(..., _keep_defers=True)`; without that the
re-arm resets the counter and the bound can never be reached, which is a
mutation in its own right.

### 3. Nobody could see it, so `/loop` — FIXED

A background process that injects prompts into someone's conversation has to be
inspectable by the person whose conversation it is. `/loop` reports armed /
next-fire / deferral count / how to stop; `/loop off` (also `stop`, `cancel`,
`end`, `0`) cancels; `/loop on` and `/loop <seconds>` arm one, clamped to the
tool's documented 60–3600 s and saying so when clamped.

Two decisions inside it are load-bearing:

- **`0` is a stop word, not a duration.** Read as a duration it clamps *up* to
  the 60 s floor, so the plainest possible way to ask for no loop would arm the
  fastest one available — the same inversion as bug 1, in the operator's UI.
- **A stopped loop reads differently from one that was never armed.** Both are
  "not armed", but only one is a decision, and an operator who just typed
  `/loop off` needs to see that it took. Removing that doubt is the whole point
  of the command.

Stopping and arming **broadcast**; a status query answers only the asking tab.

**Verified:** `tests/test_loop_control.py` (39) plus two mutation targets,
`loop` (`sdk_bridge.py` × 11) and `loop-server` (`server.py` × 8), both 100%
caught — including bugs 1 and 2 restored verbatim. The `loop-server` sweep
earned its keep: **five of eight mutations survived the first run**, each a real
gap (`/loop 0` arming a loop, the audience of every reply, the
stopped-vs-never-armed distinction, the `--no-wakeup` message), and the tests
that close them were written from those survivors rather than guessed at.

**Caveat, operational:** the fix is in `sdk_bridge.py` and `server.py`, so
already-running sessions keep the old behaviour until the hub is restarted with
`python server.py`. An agent reporting uncontrollable wakeups right now is still
running the broken code.

## Launching was slow because the interpreter lived on a hard disk — FIXED (2026-09-06)

**Reported:** "there's still a long time between launching orchestrator2 and
the tab opening. I've tried to have that settled many times... I was told it's
due to the antivirus scanning, but why would it scan something each time that
it's already scanned?"

**The scepticism was right — it was not antivirus.**

```
D:  disk 0  HDD  WDC WD2004FBYZ-01YCBB2      <- 7200rpm platter
C:  disk 1  SSD  Samsung SSD 980 PRO NVMe
```

`D:\python314` — the interpreter `orch2.bat` launched the server with, plus its
**92,240** site-package files — was on the spinning disk.

**Measured.** The same Python 3.14 exists on both drives (pymanager installed
the C: one), which makes a clean A/B:

| | `pass` | `import asyncio` |
|---|---|---|
| D: python314 | 0.07 s | **4.14 s** |
| C: pythoncore-3.14-64 | 0.06 s | **0.23 s** |

Bare interpreter start is identical, so it is not the interpreter — it is
reading the library. Raw per-file reads of the `.pyc` files an import actually
loads: **D: 37.4 ms/file median (p90 156 ms) against C: 0.66 ms/file** — 57x.
`import asyncio` pulls 143 modules with no single one dominating (`heapq`
0.176 s, `__future__` 0.124 s, `_weakrefset` 0.108 s); a flat ~23 ms per file
regardless of content is seek latency, not code.

**Why the antivirus explanation fails.** The same import was run **nine times
in a row**: if a scan result were cached, runs 2-9 would collapse. The
*minimum* stayed at 2.95 s. And `json` (0.06 s) and `logging` (0.13 s) on the
same drive were fine, so it was not blanket interception either. Defender's
real-time protection is on for both drives; only the platter is slow.

It also explains two side observations from the report: the only other
slow-launching program was `qtpyrc` — Qt has a large import graph and also
lives on D: — and everything that launches fine is everything with a small one.

**Fix.** `orch2.bat` now launches with the C: interpreter (and drops
`pymanager exec -3.14`, whose resolution added ~0.7 s per launch, in favour of
the exe it resolves to). `requirements.txt` was installed there; it already had
most of it. Backup at `D:\utils\orch2.bat.bak-20260906`.

| | before (D:) | after (C:) |
|---|---|---|
| `server.py --help` (imports + parse) | 2.89 s warm, ~9 s cold | **0.72 s** |
| `import server` | 2.99 s warm, 11.75 s cold | **0.81 s** |
| full test suite | ~7 min | **~3 min 50 s** |

A side benefit: the SDK's bundled `claude.exe` is 220 MB and is read on every
CLI spawn. It now comes off the SSD too.

**Notes.** The in-app restart uses `sys.executable`, so it follows
automatically. The C: interpreter picked up claude-agent-sdk **0.2.152** (one
newer than D:'s 0.2.151). Tool usage strings and design.md's documented test
command were updated to the new path.

**Left alone, and why:** `_manual_restart.py` still hardcodes the D: path, but
it is dead scaffolding from a past incident (`OLD_PID = "12544"`, plus a
session id and cwd from that repair) and is on no live path — worth deleting,
but that is a separate decision.

## Opening a session took ~42 s to show its transcript — FIXED (2026-09-06)

**Asked:** "when does that 41 s read happen? is that why it takes so long
between launching a session and the tab opening?"

**Measured answer.** The tab's *shell* appears in ~5 s; what takes ~42 s is the
**transcript**. From the log of a three-session launch:

```
+ 0.0s  runtime s38 started, connect begins
+ 5.4s  [history] loading begins        <- tab UI is already up
+47.0s  [history] rendered 1726 msgs, sent
```

Three independent causes, all fixed:

**1. The "tail" read was half the file.** `_tail_read_jsonl` derived its
average line size *from the file size*:

```py
avg_line_bytes = file_size // (max_records * 8)     # 2000 * 8 = 16000
seek_bytes     = max_records * avg_line_bytes * 4   # = file_size / 2, always
```

```
1448 MB transcript -> 724 MB read      603 MB -> 302 MB
 588 MB transcript -> 294 MB read      ... always exactly 50%
```

It also undershot its own target — 294 MB yielded 693 records against a
`max_records` of 2000, because these transcripts carry enormous tool results.
The estimate is now a *lower* bound, capped at `MAX_TAIL_BYTES` (32 MB).
Measured after, machine idle: **1448 MB → 3.0 s, 603 MB → 1.4 s, 588 MB →
1.3 s**, against 4.9 s for the *smallest* of them before.

**2. Six readers, one disk.** The history read ran concurrently with the CLI's
own resume read of the same file. The CLI reads take turns via a lock; the
history reads did not, so a three-session launch had ~4.0 GB in flight at once
(1448+603+588 CLI, 724+302+294 history) — which is why an operation that costs
~5 s alone took ~41 s. The history read now takes a turn in the same queue.

**3. Rendering is the other half.** Building a thousand-odd DOM nodes takes
real time, and the user watches an empty chat throughout. The server now sends
the newest `HISTORY_FIRST_SLICE` (200) messages as `history`, then the rest as
`history_prepend`, which `chat.js` renders into a detached container and
inserts *above* — re-anchoring `scrollTop` by exactly the height added so a
reader partway up is not thrown backwards.

**Trade-off, stated plainly:** the bounded read means a `TodoWrite` further
back than 32 MB is no longer found, so one of the three test sessions now seeds
an empty plan panel. That list was 4 days and ~700 MB stale; an empty panel
beats a confidently wrong one. Nothing short of reading most of the file would
have recovered it.

**Tests:** `tests/test_history_read.py` (8), `tests/history_backfill.test.js`
(13, jsdom). Mutation-checked via `histread` (3) and `backfill` (6), all
caught. Four survivors along the way:

- the byte-counting in the "not read in full" test measured nothing —
  `for raw in fb:` uses buffered reads and never touched the wrapped
  `fh.read`, so removing the cap left the test green. Rewritten to assert on
  *records returned*, which is a faithful proxy for a fixed-size fixture.
- the re-anchoring test never left the bottom: `_autoScroll` is set by a
  *debounced* scroll listener, so assigning `scrollTop` alone left the view
  pinned and the assertion vacuous.
- two mutations were **equivalent** and were removed rather than papered over:
  queuing `history_prepend` through `_pendingMessages` changes when it renders,
  not what; and `_replayInProgress` inside `_prependHistory` was inert — the
  function is synchronous, so nothing can interleave with it. That flag was
  deleted, along with a comment claiming a protection it could not provide.

## The plan panel was empty after a restart — FIXED (2026-09-06)

**Asked:** do TodoWrite lists survive a session/hub restart, and if not, should
they?

They did not. `state.current_todos` was written only by a *live* `TodoWrite`
tool call, and nothing seeded it from the transcript — `render_session_history`
had no TodoWrite handling at all. So after a restart the panel sat blank until
the agent happened to rewrite its list.

The asymmetry that made it worth fixing: the CLI rebuilds **its own** todo
state from the transcript on resume (the SDK is non-interactive, so todo v1
applies and `sessionRestore.ts` reconstructs `AppState.todos`). Claude still
knew the plan; only the user's view of it was missing.

**There is one writer, and each write is a full replace.** Checked before
building, against three live transcripts: their tails contain `TodoWrite` and
nothing else touching todos — `TaskOutput` (1085x) and `TaskStop` (52x) belong
to the background-task system. Every `TodoWrite` carries the *entire* list,
each item `{content, activeForm, status}` with status in
`pending`/`in_progress`/`completed`. **The strikethrough in the panel is that
`completed` status being rendered, not a separate "mark it done" call**, so the
last write is authoritative on its own — including which items are struck out —
and there is nothing further to scan for.

Because of that, the list is seeded **verbatim**, completed items included:
that is exactly what the panel showed before the restart, it matches what the
agent believes, and `/api/todos/clear` already exists for anyone who wants the
finished ones gone. (Filtering them here would have made the panel disagree
with the agent for no gain.)

**Fix.** `last_todos_from_records()` in `session.py`, returned as a fourth
element from `render_session_history()` and applied in `_send_initial_state`
only when `current_todos` is empty, so a live TodoWrite is never clobbered. It
rides along on the read the renderer already does: these transcripts reach
600 MB and take ~40 s to walk, so a second pass just to recover a side panel
would cost more than the panel is worth.

**An earlier worry, discharged:** I had flagged that a `/compact` could strand
the todos. It doesn't — compaction changes *context*, not the CLI's in-memory
todo state; only a restart loses that, and then both the CLI and this code read
the same transcript. Gating on `compact_boundary` would have blanked the panel
after every compaction for no benefit.

**Tests:** `tests/test_todo_seeding.py` (23). Mutation-checked via the
`todoseed` target — 7 mutations, all caught. Three survivors first time, each a
real gap:

- the tool-name check was untested, because none of the "other tool" fixtures
  carried a `todos` key — matching on payload shape alone would make the panel
  hostage to any tool using that parameter name;
- `isinstance(items, list)` looked redundant (a string filters to empty) until
  a non-iterable payload: `for t in 5` raises, inside the session load, where a
  side panel must never be why a transcript fails to open;
- the OSError branch was never reached — neither a missing file nor a directory
  provokes it — so the first version of that test named the error path and
  pinned nothing. Its own "the error path was not taken" guard is what caught
  that.

## A prompt reached only the tab that typed it — FIXED (2026-09-06)

**Reported:** a session open in two tabs; a turn interrupted, then a new prompt
typed. It appeared only in the tab it was typed in — yet *both* tabs showed the
agent start working and reply. One tab displayed an answer to a question it had
never shown.

**Cause.** The frontend echoes a prompt optimistically when it isn't busy at
send time, and reports that to the server as `client_echoed`. `_enqueue_prompt`
read that as "the prompt is already on screen" and skipped the broadcast
entirely — but it only ever meant "*the sender* drew it". Every other viewer was
left without it, while the reply, broadcast normally like all turn output,
arrived regardless. That asymmetry is the whole reported shape.

**Fix.** `SessionRuntime.broadcast(..., exclude=<socket>)` skips one *viewer*
rather than the whole send, and `_enqueue_prompt` takes `echoed_by=<socket>`.

The boolean and the socket were briefly separate parameters, and a mutation
sweep showed why that was wrong: `client_echoed=True` with no known origin is a
meaningless state, and the branch handling it was redundant (`exclude=None` is
already "send to everyone"). Collapsing the two into one `echoed_by` socket
makes the state that caused the bug **unrepresentable** rather than merely
handled.

**Tests:** `tests/test_multi_tab_echo.py` (11), plus two pre-existing tests in
`test_prompt_echo.py` updated to the new signature — they encoded the correct
property ("don't double-echo the sender") and still do. Mutation-checked via
two targets, `tabecho` (server.py, 4) and `tabecho-rt` (session_runtime.py, 2),
all caught. Two lessons from survivors:

- the suite exercised the helper but never the **call site**, so dropping the
  origin from `_handle_ws_message` restored the exact bug with every test
  green. Now pinned. (The naive version of that test also sliced from the
  *first* of three `_enqueue_prompt` calls in the handler and asserted against
  the wrong one.)
- `ws is exclude` → `ws == exclude` is an **equivalent mutant**: neither
  Starlette's WebSocket nor the test double defines `__eq__`, so no test could
  ever distinguish them. The mutation was removed rather than "covered" by a
  test contriving an `__eq__` real sockets don't have.

## A phantom "Continue from where you left off." on resume, which nothing acted on — FIXED (2026-09-06)

**Reported:** three restarted SlateOS sessions showed a prompt the user never
typed — *"Continue from where you left off."* — answered by *"No response
requested."*, and the interrupted work was not picked up.

**Not orchestrator2's text.** `grep` finds it nowhere in this repo. It is the
**CLI's own** conversation recovery (`src/utils/conversationRecovery.ts`): when
a resumed session's last turn was cut off mid-flight, it appends a *synthetic*
user message with `isMeta: true`, plus an assistant sentinel, so the transcript
stays API-valid if nothing acts on it. The transcript records name their own
origin: `entrypoint: "sdk-py"`, `isMeta: true`, `version: 2.1.258`, bracketed
by `queue-operation` entries.

**Why nothing happened.** In an interactive terminal the user is offered the
choice; accepting makes the CLI delete that pair and re-enqueue it as a real
prompt. Non-interactively there is no chooser, so the pair just *stays* — a
prompt nobody typed, a refusal to answer it, and abandoned work.

**When it happened — the counter-intuitive part.** It looks like a parting shot
from the dying process, because it is the *last* thing in the transcript. It is
actually the *first* thing the **new** process wrote. Aligning the transcript
(UTC) against `orchestrator2.log` (local) puts every pair inside its own
resume, ~0.7 s before that session's connect completed:

| session | resume began | pair written | connect done |
|---|---|---|---|
| 2e281cb3 | 09:11:51.86 | **09:12:06.47** | 09:12:07.19 |
| 7baa01e0 | 09:11:52.80 | **09:12:31.25** | 09:12:31.96 |
| c108f948 | 09:11:53.62 | **09:12:40.85** | 09:12:41.62 |

It reads as "the end" only because the CLI answers "No response requested." and
stops, so nothing follows it. All **three** sessions got it, not two.

**Fix** (`sdk_bridge.py`): set `CLAUDE_CODE_RESUME_INTERRUPTED_TURN=1` in the
CLI subprocess env. That is the supported opt-in for exactly this path
(`src/cli/print.ts`): remove the synthetic pair and re-enqueue it once, so the
agent actually finishes the turn. Of the available behaviours — finish the
work, or keep the noise *and* abandon the work — only one has any upside, so it
defaults on. `--no-resume-interrupted-turn` opts out, for opening an
interrupted session without it immediately starting to act.

**Not caused by the SDK upgrade:** both the old (2.1.105) and new (2.1.258)
bundled CLIs contain the string, so the injection predates it — though the new
one carries far more auto-resume plumbing (15 references to the env var vs 3).

**The transcript is left verbatim, deliberately.** A first version of this fix
also filtered the pair out of the replayed history — classifying the
continuation as an injected prompt and dropping the sentinel. That was reverted
at the user's request, and they were right: this project has spent a lot of
effort on failures that were invisible, and hiding records from the transcript
makes the next diagnosis harder. The pair stays visible; the env var means
future resumes act on it instead of abandoning it.

**Tests:** `tests/test_resume_interrupted_turn.py` (10). Mutation-checked via
the `resumeturn` target of `tools/mutate.py` — 5 mutations, all caught. One
survivor: `getattr(config, ..., True)`'s fallback was unverified because
`parse_args` always sets the field; it is load-bearing for hand-built configs,
so it is pinned by a test rather than deleted.

## Sessions running in another hub were listed under "recent" — FIXED (2026-09-03)

**Reported:** "my sessions view is showing OSb, OSc, and Good Photons under
'recents' rather than currently running, and they're both currently running."

**Cause.** `_session_list_payload()` built its running list from
`runtimes.values()` — *this process's* registry. The hub reuses one server per
account/port, so a machine driving several accounts runs several hub processes;
a session live in any of the others is not in `runtimes`, falls through to the
on-disk scan, and is presented as merely recent.

Confirmed on the reporter's machine before changing anything — all three were
held by foreign `claude` processes under hubs on their own ports:

| title | holder pid | hub port |
|---|---|---|
| OSb | 46320 | 51842 |
| Good Photons | 34092 | 63978 |
| OSc | 116872 | 51843 |

(this hub was on 8420). Those high ports are the OS-assigned fallback when 8420
is taken — which is *also* why a remote client can't reach a sibling hub, the
thing the mobile "switch to that window" fix now probes for.

**Fix.** New `proc_guard.map_foreign_session_holders()`: one `process_iter`
walk returning `{session_id: ForeignHolder}`. The existing
`find_foreign_session_holder()` scans per id — fine for the one-shot open path,
useless for a list, since a cold walk measured **13 s** here (0.4 s warm) and
the lobby ticks every 2 s. `_session_list_payload` promotes matching sessions
into `running` with `foreign: True`, `port`, `holder_pid`, `started`; the scan
is cached for 8 s and anything in this process's own `runtimes` is excluded
regardless of what the (possibly stale) map says.

In the UI the card is dashed/dimmed with an `other window :<port>` tag and no
× — this window can neither drive nor close it.

**A dead end avoided while fixing this:** the running-card click handler starts
`if (!rid) return;`, and a foreign card has no rid, so the new cards would have
been *completely unclickable* — the same silent-nothing failure as the last
three reports. They now route through the same `?open=<sid>` path a "recent"
row uses, via a shared `_openSessionById()` so the two can't drift.

**Tests:** `tests/test_foreign_running_sessions.py` (10). Mutation-checked via
the new `foreign` target of `tools/mutate.py` — 9 mutations, all caught. One
test is deliberately tolerant of a holder exiting mid-check: process turnover
was observed for real during this investigation (OSb's and OSc's CLIs turned
over between two probes), and a test that treats that as a failure would cry
wolf.

## Shipped with a hardcoded internet password, and a throttle that punished the first visit — FIXED (2026-09-03)

Three reports, one investigation.

### 1. "Every time I put in my password it says wait 9 seconds, then I reload and it works"

The password was **correct** the whole time. `_ExternalAuthMiddleware` recorded
a brute-force failure for *any* request without valid credentials:

```py
if not (header_ok or param_ok or cookie_ok):
    self._record_failure()
```

A credential-less request is not a guess — it is a browser that has not been
asked yet. One cold page load fires the document, ~12 static assets, and a run
of WebSocket upgrades from `app.js`'s 20-try reconnect loop, none carrying
credentials. Five of those armed the lockout (2s → 4s → **8s**, reported as
"9s" via `int(8.x)+1`), so the user was told "too many failed attempts" while
entering the right password for the first time. Reloading "fixed" it only
because the auth cookie existed by then.

**Fix:** count a failure only when a credential was *presented and wrong*. A
real guess always presents something, so the protection is unchanged. Also:
a **valid cookie now passes during a lockout** — it is proof of a past success,
not a guess, which removes the one real bite of a global counter (a stranger
guessing from anywhere could lock the owner out of their own session for five
minutes). And the 429 body now says "failed **password** attempts", because
"too many attempts" was indistinguishable from "your password is wrong" — which
is precisely why the reporter could not tell which had happened.

### 2. A default password, in the source

`config.DEFAULT_EXTERNAL_PASSWORD = "uncommon11"` was applied automatically
whenever neither `--external-password` nor `ORCH2_EXTERNAL_PASSWORD` was set.
Every install was therefore reachable from the internet with a password that is
not a secret. Worse than a blank one, because it looks like security.

**Fix:** the constant is gone with no replacement, and external access is now
**off by default** behind two independent knobs — `--external-access on|off` /
`ORCH2_EXTERNAL_ACCESS` and `--external-password` / `ORCH2_EXTERNAL_PASSWORD`.
On + password → allowed. On + no password → **refused, with a warning** rather
than left open or silently ignored. A whitespace-only password is not a
password. See design.md §9.

Discoverability, since a refusal you cannot act on is just a wall:
`EXTERNAL_HOWTO` is written once and quoted in the startup log, in `/status`,
and in the body served to the refused external client.

### 2a. ...and that source is a public repository — OPEN (action needed)

`copy_to_github.bat` mirrors this project to `D:\github\orchestrator2`, whose
remote is **https://github.com/inhahe/orchestrator2 — public**. So the default
was not merely "in the source", it was published:

* introduced **2026-07-17** (`f13ee5f`, as the dataclass default), refactored
  into a named constant **2026-07-19** (`278b256`); **48 days public**;
* still present in the mirror's current `config.py` at the time of writing, and
  in the history of two commits.

**Removing it from the working tree does not un-publish it.** Once the fixed
files are copied over and pushed, HEAD will be clean, but those two commits
still contain the string, and GitHub keeps unreferenced objects reachable by
SHA for a long time — quite apart from forks, clones and caches nobody
controls.

**So treat `uncommon11` as burned.** If it is used anywhere else — and its shape
suggests a person chose it rather than a placeholder a tool invented — it should
be changed there too. The live orchestrator2 password has already been rotated
to a fresh 59-bit value held only in `D:\utils\orch2.bat`, which
`copy_to_github.bat` does **not** publish (verified).

**Open action, operator only:** purging it from published history needs a
rewrite (`git filter-repo` / BFG) plus a force-push of `main`. That is a
destructive rewrite of shared history, and pushing is the user's action in this
project, so it has deliberately **not** been done. It is also of limited value
by itself — rotation is the part that actually protects anything. Worth doing
only if the string itself must go.

### 3. Mobile: "already open in another window" → "yes" → nothing

`lobby.js:onSessionElsewhere()` navigated to
`location.hostname + ':' + msg.port`. That fires only for a session held by a
*foreign process* — a second hub on another port (same-hub sessions already
attach as extra viewers, so a PC and a phone can share one session fine).

Two ways that navigation goes nowhere, both silent: on a phone the "window that
already has it" is on a **different machine**, and a remote client normally
reaches the hub through a **single forwarded port**, so a sibling hub on another
port is not routable. The code already had `_isMobile()` special-casing in
`openHub()` and the session-click handler; this path never got it.

**Fix:** probe the target with `fetch(..., {mode:'no-cors'})` and a 2.5 s
timeout before offering to navigate, and skip the offer entirely on mobile.
When it isn't reachable, explain *why* and say what to do (close it in the
window that holds it) instead of steering into a blank page.

**Tests:** `tests/test_external_access_policy.py` (25), plus the existing
`test_external_auth.py` (12). Mutation-checked via the new `extauth` target of
`tools/mutate.py` — 10 mutations, all caught.

## A mistyped launch flag failed in total silence — FIXED (2026-09-03)

**Reported:** "when i passed `--noresume`, it didn't resume it even though it's
not the correct parameter for that, as it didn't show the history playback or
continuing working as the current session had been."

**The flag did nothing whatsoever — including starting the server.** Verified:
`parse_args(["--noresume"])` exits 2 ("unrecognized arguments"). The real flag
is `--no-continue`.

**Why nobody could tell.** `orch2c.bat` -> `orch2.bat` -> `start /MIN ""
pymanager ... tray_minimizer.py ... cmd /c python server.py ... %*`. The
process gets a real console whose window is then hidden, argparse writes to
*that* console, and it exits **before `--log-file` has been read** — so there
is no console output anyone sees and no log line either. The launch simply
evaporated.

**Why that was actively misleading, not just unhelpful.** The session the user
was looking at came from a *different* launch. Log, same minute:

```
23:43:36  runtime s12 started (cwd=...forward raytracer, resume=8d532b0a-...)
23:48:03  runtime s13 started (cwd=...forward raytracer, resume=None)
23:48:34  run_turn start: prompt="i tried release.bat and it says the version hasn't changed..."
23:49:17  run_turn start: prompt='er, that was an old prompt, n/m tha'
```

`s13` had `resume=None` — no history playback — because it ran under the
**default account**, which has **zero** sessions for that directory:

| account | raytracer sessions |
|---|---|
| `.claude` | **0** |
| `.claude-account-b` | 1 |
| `.claude-account-c` | 1 |

So "it didn't resume" was an *account* effect, and the silent failure of the
typo'd flag got the credit for it. Meanwhile the genuine bug sitting right next
to it — that fresh session inheriting a stale queued prompt (entry below) — was
harder to see for the false explanation stacked on top.

**Fix** (`server.py`): `main()` now parses through `_parse_args_or_report()`.
argparse's message still goes to stderr unchanged, and is *also* captured so
`_report_launch_failure()` can:

- append it, with the full command line, to `launch-error.log` beside
  `server.py` — always, so there is a trace even when nothing was watching;
- pop a dialog **only** when `_console_is_hidden()` — a console that exists but
  whose window is hidden, i.e. exactly the tray launcher.

`--help` exits 0 and is not treated as a failure.

**A trap worth recording, because the first version of this fix fell into it.**
The dialog was originally gated on `not _console_is_visible()`. "No console at
all" — a piped run, a test, CI — also has no visible console, so a piped
invocation popped a *modal* `MessageBoxW` and pinned the process until it was
killed. Hence the narrower `_console_is_hidden()` (console exists **and** is
hidden), `MessageBoxTimeoutW` so it can never wedge indefinitely, and an
`ORCH2_NO_DIALOG` escape hatch for automation. All three are pinned by tests.

**Tests:** `tests/test_launch_errors.py` (8), including one that exists purely
to catch a re-introduced hang.

## A new session inherited an old session's queued prompt — and sent it — FIXED (2026-09-03)

**Reported:** "I ran a second session in a directory using `--noresume`, and it
injected a prompt that I'd already given it before, I had to abort it."

**The flag is `--no-continue`** (there is no `--noresume`; argparse would have
rejected it), but the flag was never the problem — it worked. The queue did not.

**Cause.** `State.queued_prompts` is mirrored to disk so typed-but-not-yet-run
prompts survive a server restart. The file was keyed by **cwd alone**
(`<config-dir>/orchestrator2/queues/<sanitized-cwd>.json`) and
`_attach_queue_persistence()` restored it **unconditionally** — before
`state.session_id` was even seeded, with no way for a caller to decline. So
*any* session started in a directory adopted whatever an older session had left
queued there.

**And a restored prompt is not merely displayed — it is sent.** `connect()`
ends by calling `_poke_for_queued_prompt()`, so the worker pops the inherited
prompt and runs it as a real turn with no user action. That is the "it injected
a prompt" in the report.

Three defects in one:

1. **A fresh session inherited an old one's queue.** `--no-continue` means
   "start fresh"; nothing consulted it.
2. **Two sessions in one directory clobbered each other.** The hub hosts
   several at once and both wrote to the same path — last writer won, the
   other's queue vanished.
3. **Nothing ever expired.** Found still armed on disk during this
   investigation, 18 days old:
   `~/.claude-account-c/orchestrator2/queues/D--visual-studio-projects-forward-raytracer.json`,
   `saved_at 2026-08-16`, holding a prompt about `release.bat`. It would have
   fired on the next session opened in that directory.

**Fix** (`session.py`, `server.py`): **a queue belongs to a session, not to a
directory.**

- The file is keyed by session id (`<cwd>__<session-id>.json`, `__new` for a
  session that has not taken a turn yet), which fixes (2) by construction.
- `load_persisted_queue()` refuses anything that is not that session's own
  recent work: the id in the filename *and* the id recorded inside must match,
  and the queue must be younger than `QUEUE_MAX_AGE_S` (24 h). Legacy cwd-only
  files are unreachable under the new naming, so the landmine above is inert
  without anyone having to delete it.
- **No session id, no queue**, enforced in the loader rather than at each
  caller: `load_persisted_queue()` returns nothing when `session_id is None`
  and `save_persisted_queue()` writes nothing. `--no-continue` has no id by
  definition, so it can neither inherit a queue nor leave a directory-shaped
  one behind for the next session.
- All three call sites moved to **after** the session id is seeded — the
  ordering the original code got wrong — and that ordering is now pinned by a
  test that reads `server.py` itself.
- Saving is wired for every session and reads `st.session_id` at save time, so
  a session begins persisting the moment it has an identity to restore into.

**Tests:** `tests/test_queue_persistence.py` (26). No test covered queue
*restoration* before — which is exactly why this shipped. Mutation-checked via
the `queue` and `queue-server` targets of `tools/mutate.py`.

**Two survivors on the first sweep, both worth having.** One was a *test*
defect: the age test computed its own timestamp from `QUEUE_MAX_AGE_S`, so it
passed for any value of the constant — including a 1-second cap that would
discard the queue on every ordinary restart. Rewritten against absolute
durations (30 s, 5 min, 2 h) that a real restart actually spans. The other was
a *design* defect: the fix originally put a `restore=` gate on each call site,
and the sweep showed the safety did not depend on it. Rather than write a test
for the flag, the rule moved into the loader and the flag was deleted — see
design.md §10 on why a survivor is a fact about the code, not a gap to paper
over.

**Residual:** a restored queue still auto-sends on connect. That is the
feature's point (survive a restart), and it is now bounded to the same session
within 24 h — but if it ever surprises anyone again, the fix is to show the
restored prompts in the queue panel *without* poking the worker, and let the
user press send.

## A `/model` switch silently orphaned running background tasks, and dropped the session out of bg-wait — FIXED (2026-09-02)

**Found while checking** whether the CLI-recycle feature (entry above)
"took the task list into account". It did: `_maybe_recycle_cli()` refuses while
`state.background_tasks` is non-empty, `force` does not skip that, and the guard
cannot latch because `tool_manager.complete_bg_task()` pops finished tasks. The
recycle was fine. **Nothing else was.**

`reconnect()` has eight callers. Only the recycle checked. `/model`, `/effort`,
`/thinking`, `/connect`, `_recover_dead_transport`, the
`auto_reconnect`-after-a-failed-turn path and the context trim all ran:

```py
n_stale = len(self.state.background_tasks)
if n_stale:
    self.state.background_tasks.clear()
    self.state.completed_panel_bg.clear()
    log.info("cleared %d stale bg task(s) on reconnect", n_stale)
```

**The lost `TaskOutput` handle was the smaller half.** `_between_turns()` parks
the session in bg-wait *only* while `state.background_tasks` is non-empty, and
leaves the park on the `bg-all-done` wakeup `_complete_bg_task` sends when the
dict drains. Clearing the dict from underneath it means the park is skipped, the
wakeup can never fire (the tasks it counted are gone), and **an autonomous
session waiting on background work drops straight to idle** — while the OS
processes it spawned keep running, untracked. The user is told nothing; the only
record is an INFO line in a log file.

The `_apply_idle_config_command` site is the worse of the two, because it is
reached from `_await_next_prompt()` *while the session is parked in bg-wait* —
so a `/model` typed at exactly the moment the wait matters is the one that
breaks it.

**Fix** (`sdk_bridge.py`, design.md §6d): push the recycle's knowledge down into
`reconnect()` so being careful stops being one caller's private virtue.

- `_orphan_bg_tasks(why)` clears the registry **and broadcasts a warning naming
  the tasks**. Losing the bookkeeping is sometimes unavoidable; losing it
  quietly never is.
- `_reconnect_or_defer(reason)` — `/model`, `/effort`, `/thinking` are
  discretionary, so with tasks running they record `_deferred_reconnect` and
  tell the user (including that `/connect` overrides).
- `_flush_deferred_reconnect()` applies it at the top of `_between_turns()`
  (above the queued-prompt branch, so the next prompt runs under the new
  settings rather than one turn late) and on the `bg-all-done` wakeup in
  `_await_next_prompt()` — the common case, since a parked session has no turn
  boundary coming.
- Not deferred: `/connect` (the user overruling us on purpose), a dead transport
  (no CLI left to protect), and the context trim, which is **skipped** instead
  because `trim_session()` mints the new session id before the reconnect would
  happen — deferring would leave `state.session_id` pointing at a session the
  live CLI is not resumed on.

**Not a bug, checked while here:** the CLI *does* revive the todo list across a
resume. The SDK runs non-interactive, so `isTodoV2Enabled()` is false and
`sessionRestore.ts` rebuilds `AppState.todos` from the transcript. **Caveat
worth remembering:** it rebuilds from the resumed *messages*, so a `/compact`
that drops the `TodoWrite` calls can lose them — and this orchestrator
auto-compacts. Not observed; logged so the next person to see an empty todo
panel after a compact does not start from zero.

**Tests:** `tests/test_reconnect_bg_tasks.py` (37 tests). Mutation-checked via
the new `reconnect` target of `tools/mutate.py` (renamed from
`mutate_frontend.py`, which now also drives pytest) — 20 mutations, all caught.

## A socket that died while the window was hidden never came back — FIXED (2026-08-31)

**Found while auditing** the rest of the frontend against the rule the entry
below established (*nothing that must keep working may be gated on an animation
frame*). Not separately reported, but it produces the same symptom as that
report — a window that "isn't updated" — by a different and more damaging
mechanism, and the fix below would not have addressed it.

**Cause.** `app.js` runs its reconnect backoff on a `setTimeout`, which a hidden
window clamps to 1 s and then to 1/minute under intensive throttling. The
20-attempt budget spans ~7.5 minutes while visible; hidden, it burns down over
~20 unattended minutes and then sets `_serverShutdown = true`, which **latches
auto-reconnect off permanently**. The tab then sits on a dead transcript that
nothing will refill — recoverable only by typing `/connect`, which requires
knowing that is what happened.

A laptop suspend/resume drops the socket, and does it while the window is
hidden, essentially by definition. So this is the ordinary case rather than an
exotic one, and `app.js` had **no `visibilitychange` handler at all**.

**Fix** (`static/app.js`): `_onVisibilityChange()` reconnects on show whenever
the socket is not up, which also short-circuits an ordinary backoff still
holding up to 30 s — the user is here; waiting out a timer helps nobody. Two
guards keep it safe: an **OPEN** socket returns early (`reconnect()` would treat
that as a server-side bridge reconnect and send `/connect`, a command the user
never typed — firing it on every tab focus is its own bug), and
**`_retriesExhausted`** is tracked separately from `_serverShutdown` so that a
deliberate `server_shutdown` (reconnecting fails noisily) or `server_restart`
(Lobby is already polling and will reload the page) is never overruled. Only our
own impatience is reversible. Clearing that flag in `reconnect()` is
load-bearing: left stale, the *next* genuine shutdown would look retryable.

**Tests:** `tests/reconnect_on_show.test.js` (11 tests, jsdom, real `app.js` in
the real `index.html`, against a fake WebSocket and a manual clock so the
backoff is steppable). Mutation-checked, 8/8 caught.

**Two survivors here were code smells, not test gaps.** A three-valued
`_shutdownCause` (`'exhausted' | 'shutdown' | 'restart'`) had two values no
branch could ever read, so mutating them changed nothing — replaced by the
boolean the logic actually used. A `CONNECTING` check in the visibility handler
duplicated one `reconnect()` already makes — removed. See design.md §7.

## A hidden window stopped trimming its transcript, then crawled when shown — FIXED (2026-08-31)

**Report:** *"It seems sometimes the window isn't updated while it's not in
focus, and once it's put into focus, it takes a long time to get up to speed
because it only adds a few lines per second. Though sometimes it seems it's
already up to date when I put focus on the window."*

**Cause.** A hidden window (minimised, fully covered, or a background tab) gets
**no** `requestAnimationFrame` callbacks at all, and its `setTimeout` is clamped
to 1 s — then to 1/minute once Chrome's intensive throttling starts five minutes
in. WebSocket delivery is not throttled, so messages kept arriving and kept
being appended while everything scheduled on a frame stayed frozen.

`_maybeTrimOldMessages` set `_trimPending = true` and scheduled the actual trim
on a frame that never came. The latch never cleared, so **`max_dom_messages`
stopped being enforced entirely** and the list grew without bound. Every append
meanwhile still ran `scrollTop = scrollHeight`, which forces a synchronous
layout of the whole list — O(nodes) — so the append rate decayed as the list
grew and *stayed* decayed after the window was shown, because the DOM was still
enormous. That is the "few lines per second", and it is why it persisted after
focus rather than clearing instantly.

Four secondary routes to the same symptom, all fixed with it:

- **A trim stranded by hiding.** `_onNextFrame` only helps when the cap is
  crossed while *already* hidden. Cross it while **visible** and the trim parks
  on a frame correctly — then hiding the window strands that frame and
  `_trimPending` is latched exactly as in the primary bug. Found by mutation
  testing after the first fix was written and believed complete; fixed by
  `_reclaimStrandedTrim()` on the hide side.
- **History replay** yields `setTimeout(_renderBatch, 0)` between 50-message
  batches. Clamped to 1 s that is 50 messages/second; under intensive throttling
  it is 50 a *minute*.
- **The two collapse gaps** (`_gapActive` / `_shortGapActive`) make
  `_maybeTrimOldMessages` return early. A gap open at the moment the window was
  hidden wedged the trimmer shut for the whole hidden period — unbounded growth
  again, by another route.
- **Trimming removed nodes one `removeChild` at a time**, which is what turned
  the eventual catch-up on a 30k-node list into a visible freeze.

**Why "sometimes".** A window that is merely **unfocused but still visible**
keeps getting frames, so none of this happens — that is the case where the
transcript was already up to date. Focus was never the relevant fact;
`document.hidden` is.

**Fix** (`static/chat.js`): `_onNextFrame()` runs its callback immediately when
hidden and on a frame otherwise, and the trimmer uses it; `_scrollToBottom()`
defers while hidden and a single `visibilitychange` catch-up settles the
backlog; `visibilitychange` also releases both collapse gaps; `_replayHistory`
consumes every batch in one **loop** (not recursion) while hidden; the trim is
one `Range.deleteContents()`; and `_reclaimStrandedTrim()` runs a
frame-parked trim synchronously on hide. `_trimNow()` is idempotent — it
re-reads the list and re-checks the cap — which is what makes the frame it
orphans harmless, and is why there is no `cancelAnimationFrame` bookkeeping (a
`_trimFrame` id survived two mutations, i.e. no test could tell it from its
absence, so it was removed rather than kept unverified).
`_scheduleStreamRender` is deliberately left as a bare rAF — see design.md §7
for why making it eager would be worse.

**Tests:** `tests/hidden_window.test.js` (16 tests, jsdom, `document.hidden`
tied to rAF suppression), run from `pytest tests/` via
`tests/test_frontend_js.py`. Mutation-checked with 15 mutations that each undo
one part of the fix — **all 15 caught** (`tools/mutate.py chat`).
Requires `npm install` (jsdom — the repo's only JS dependency, test-only); the
pytest wrapper skips when it is absent.

## A 1.29 GB session could never connect: the SDK's own 60 s deadline pre-empted ours — FIXED (2026-08-29)

**Report:** *"i resumed three sessions under ..\os, two of them took a long time
to connect, the third hasn't connected yet and is on its 8th attempt."*

**What the log showed.** Three sessions in `D:\visual studio projects\os`, all
spawned within 0.1 s of each other at 03:37:11:

```
03:37:11  START  2e281cb3 / c108f948 / 7baa01e0
03:38:13  OK     (58.5s)
03:38:22  FAIL   attempt 1: Control request timeout: initialize
03:39:22  OK     (58.3s)
03:39:30  FAIL   attempt 2: Control request timeout: initialize
   … eight more, every one 65.3s ± 0.1s after its spawn …
03:51:11  FAIL   attempt 10 — gave up
```

A fixed interval to within 100 ms across nine failures is a cutoff, not a fault.

**Cause 1 — the deadline was not ours.** `CONNECT_TIMEOUT` had already been
raised to 180 s, and it never fired, because `ClaudeSDKClient.connect()` has a
smaller one:

```python
initialize_timeout_ms = int(os.environ.get("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT", "60000"))
initialize_timeout = max(initialize_timeout_ms / 1000.0, 60.0)
```

Read from **this process's `os.environ`**, not from `options.env`, and evaluated
on every `connect()`. Unset, the SDK gives up at 60 s regardless of what we
allow. 65.3 s = that 60 s plus spawn and teardown.

**Cause 2 — the work is O(file), and the file was 1.29 GB.** Resuming makes the
CLI re-read and re-parse the entire session JSONL. The three sessions live under
three *different* accounts, which is why they are easy to mistake for one:

| session | account | size | result |
|---|---|---|---|
| `c108f948` | `.claude-account-b` | 333 MB | connected, 2nd attempt (~58 s) |
| `2e281cb3` | `.claude` | 431 MB | connected (~58 s) |
| `7baa01e0` | `.claude-account-c` | **1233 MB** | never — 10 attempts, all cut at 65.3 s |

Measured directly with the ceiling lifted and nothing else running:
**148 s**. ~115 s/GB. It was not hung and not slow; at a 60 s ceiling it was
*impossible*, and retrying could not have helped. The two that did connect
landed at 58.5 s and 58.3 s — a hair under the ceiling they were racing.

**Cause 3 — they were racing each other for the disk.** Three whole-file reads
started simultaneously don't finish in a third of the time; they contend, and
each is pushed towards its deadline by the other two. That is why even the
333 MB session needed a retry.

**Fixes** (all in `sdk_bridge.py`, tests in `tests/test_connect_timeout.py`):

1. `_raise_sdk_initialize_ceiling()` sets `CLAUDE_CODE_STREAM_CLOSE_TIMEOUT` at
   import to just above `CONNECT_TIMEOUT_MAX`, so the SDK's deadline exists only
   to never be the one that fires. An already-larger value is respected.
2. `connect_timeout_for(bytes)` = `CONNECT_TIMEOUT + 360 s/GB`, capped at
   `CONNECT_TIMEOUT_MAX` (1800 s). ~3× the measured slope: too long costs a slow
   failure, too short costs the session permanently. A fresh session, or one
   whose file can't be located, keeps the flat 180 s backstop. Size comes from
   `_resume_jsonl_size()` via `find_session_dir(sid, config_dir)` — account-scoped,
   or the largest session would have been handed the smallest budget.
3. Sessions ≥ `HEAVY_CONNECT_BYTES` (128 MB) take a process-wide `asyncio.Lock`
   around the handshake, so large reads queue instead of contending. The connect
   clock starts *after* the lock (queue time is not hang time), while
   `state.connect_started_at` is set before it so the UI timer keeps running.
4. The timeout message reports the budget that actually applied, not the
   constant.

Result: 1233 MB → 613 s budget (vs 148 s measured), 431 MB → 332 s, 333 MB →
297 s, all serialised. 451 tests pass; 13/13 mutations of these fixes are caught.

**Note for future tuning:** `CONNECT_TIMEOUT` is now the *floor* of a curve, not
the ceiling. Raising it raises every session's budget; the per-GB slope is the
knob for large ones.

## The persisted prompt queue is keyed by cwd alone, so it fires into the wrong session — FIXED (found 2026-08-28, fixed 2026-09-03)

Noticed while smoke-testing the per-session close button. A throwaway server
started with **`--no-continue`** (a deliberately fresh, empty session) in
`D:\visual studio projects\orchestrator2` immediately ran a prompt left over
from an *earlier, different* session in that folder:

```
run_turn start: ... prompt='"Say the word and I\'ll do the re-link ...'
```

`_attach_queue_persistence(st, cwd)` (`server.py:715`) loads
`load_persisted_queue(cwd)` — **cwd is the whole key**. It is called from all
three places a session's state is built (`server.py:766` startup,
`:982` `_create_runtime`, `:1686` `_reconfigure`), so:

- **`--no-continue` inherits the queue anyway.** A prompt written for a session
  with context runs blind in a fresh one — and costs a real turn.
- **Two live sessions in one folder share one queue file.** Both load it (so
  the same prompt runs twice, in two sessions, against one working tree) and
  both write it back through their own `on_change`, so whichever saves last
  wins and the other's pending prompts are silently dropped.

Proper fix: key the persisted queue by **session id** (falling back to cwd only
for a session that has no id yet), drop the load entirely under `--no-continue`,
and have `_create_runtime` refuse to adopt a queue another live runtime has
already claimed. The file lives via `load_persisted_queue`/`save_persisted_queue`
in `session.py`.

**Done 2026-09-03** — this entry sat marked OPEN until 2026-09-07 describing a
fix that had already shipped, which is its own small defect in a tracking file:
a stale OPEN is read as work outstanding.

`queue_file_for_cwd(cwd, session_id)` puts the id in the *filename*, so two
sessions on one directory cannot overwrite each other, and `load_persisted_queue`
applies three independent gates:

* the file is session-scoped, so another session reads a different path;
* the id recorded *inside* must still match, catching a file left in a reused
  slot;
* the queue must be younger than `QUEUE_MAX_AGE_S` (24 h) — a queue exists to
  survive a restart, not a week.

`session_id is None` returns `[]` before any of that, which is the load-bearing
half: `--no-continue` has no id by definition, so it cannot inherit anything, and
the three call sites need no flag to say so — they only need to run *after* the
id is seeded. That ordering is what the original bug got wrong and is pinned by
its own test. The third clause of the proposed fix (refusing a queue another
live runtime claimed) became unnecessary rather than skipped: with the id in the
filename, two live runtimes cannot address the same file at all.

The save side is wired on every mutation (`queued_prompts.on_change`), not at
shutdown, so the file is already current when the process dies — including a
`taskkill`, where no shutdown code runs. Emptying the queue deletes the file
rather than leaving a stale empty.

Verified by `tests/test_queue_persistence.py` (27) and the `queue`
(`session.py` × 10) / `queue-server` (`server.py` × 5) mutation targets.

## Session-file NUL holes are caused by OUR title write, not a "lost write" — FIXED (2026-08-28)

Second report of the amnesia warning on the SlateOS session
(`7baa01e0-…`, `.claude-account-c`, 1.29 GB, 443,902 records). The
existing explanation in `session.py` §"silent-amnesia detector" was **wrong**.

- **All three holes end in the same 93 bytes — our own record:**

  ```
  [NUL x 124834]{"type":"custom-title","customTitle":"OS","sessionId":"7baa01e0-…"}
  [NUL x 13564] {"type":"custom-title","customTitle":"OS","sessionId":"7baa01e0-…"}
  [NUL x 893]   {"type":"custom-title","customTitle":"OS","sessionId":"7baa01e0-…"}
  ```

  `write_session_title()` (`session.py:595`) appends that with
  `jsonl.open("a")` into a file the CLI holds open. NTFS zero-fills the gap
  when a write lands past the valid data length, so the NULs are **inserted**,
  not overwritten. Three for three is not a coincidence.
- **Nothing was destroyed.** 442,371 parent links contain only 4 dangling
  references, and *none* at a hole — a zeroed record would leave its children
  pointing at a uuid that no longer exists. The repair is therefore lossless.
- **Ruled out, with evidence:** crash zero-fill (last boot 2026-08-20 19:33;
  hole 3 is 23:33 the same evening with no reboot since) and the Bun
  `2147483651` deaths (07-16, 08-01, 08-20 03:06, 08-25, 08-27 — none match a
  hole).
- **The damage mechanism is parse truncation, not loss.** The CLI stops
  reading at the first NUL line (byte 186,499,053 of 1,292,919,911), so every
  resume since 2026-07-24 re-attached at node `c8290a41` and started a sibling.
  All **15** branches hang off that one node — one per resume, 3,740 records.
  The browser reads linearly, so it kept showing everything.
- **Done:** `tools/repair_session_jsonl.py` (+ `tests/test_repair_session_jsonl.py`,
  17 tests). Streams the file, strips NUL padding, and **verifies every
  surviving record is byte-identical and in order** before replacing anything;
  refuses outright if a corrupt line's remainder isn't complete JSON (that
  would be real loss, not filler). Run on the real file: 139,291 bytes removed
  from 3 lines, 453,845 lines in and out, 0 unparseable lines afterwards.
  Backups in `~/.claude-session-backups/`.
### Follow-ups, all now closed (2026-08-28)

**1 — the fork: `--relink`, but not the one that was planned.** The plan above
("chain all 15 branches in chronological order") was **wrong**, and surveying
the file properly is what showed it. The session has **1,535 components**, one
per compaction: a compact boundary is written with `parentUuid: null`, so each
compaction starts a disjoint tree and the CLI's chain stops there *by design*.
The 453,756 records "off-chain" are ordinary pre-compaction history, not loss.

The actual fault was a single **misattached tail**: 4 records written
2026-08-28 15:36 that hung off the July-24 node instead of off the current
segment. Being newest in the file, they won the CLI's leaf election, so the
resume chain was 89 records rooted at a *five-week-old* compact summary while
the current segment (68 records, boundary 14:13) sat unread. That is the
amnesia.

Chaining the other 14 branches would have made it worse, not better: ~3,700
stale records spliced into the live context, blowing past the compaction
threshold, so the first thing the CLI would do is compact them — taking the
current state with them. They stay in the file and stay readable.

`--relink` therefore re-parents only records that postdate the newest
compaction segment, onto that segment's newest leaf. On the real file: one
`parentUuid` changed, chain 89 → 72 records, now rooted at the 2026-08-28
14:13 boundary. Seam verified clean (assistant-ending-in-text → user prompt, no
unanswered `tool_use`). Backup at `…jsonl.prerelink.bak`.

**2 — stop causing it. NOT via `rename_session`.** That was checked and the
premise is false: `claude_agent_sdk/_internal/session_mutations.py` implements
`rename_session` as `os.open(O_WRONLY | O_APPEND)` + `os.write` of the same 93
bytes. It lands in exactly the same place, so switching to it fixes nothing.

The real mechanism, from the three holes' surroundings: each sits at a
session-*idle* boundary where a CLI had been shut down, and each is followed by
our title record and then, minutes-to-hours later, the next prompt. So the CLI
was killed mid-write, Windows had already extended the file size but the data
never became valid, and the tail read back as zeros — then our `O_APPEND` wrote
*after* the zeros and moved them from the end of the file into the middle of it.

Fixed by `heal_tail_before_append()` (`session.py`), called on both write paths:
before appending, drop a trailing NUL run and a trailing half-written record.
Both are at EOF, so nothing can reference them, and a *complete* last line is
never touched. 7 tests in `tests/test_session_titles.py`.

**3 — the detector caches a positive forever.** Fixed. A cached verdict is now
only reused while `(size, mtime_ns)` is unchanged — size alone is not enough,
because a relink rewrites one `parentUuid` in place and both uuids are 36
characters, so the file is repaired *without changing length by a byte*. The
index now stores `holes` (a list) via a new `find_nul_holes()`, so a file with
several holes no longer looks like a file with one, and an incremental scan
that resumes past a repaired first hole can no longer report the rest as clean.
6 tests in `tests/test_session_integrity.py`.

Verified on the real file afterwards: `find_nul_holes` → `[]`, and
`check_session_integrity(..., force=True)` → clean, no warning.
All 11 mutations of these fixes are caught by the tests.

## Duplicate-session refusal surfaced as "worker may be wedged" — FIXED (2026-08-28)

- **Reported:** resumed the `orchestrator2c` session (account-c) into the hub and
  sent a prompt; got *"The server accepted your prompt but this session never
  started running it — its worker may be wedged. Try /connect, or ☰ Sessions →
  ↻ Restart server."* Neither remedy was the right one.
- **Actual cause — not a wedge at all.** That session was already open in a
  *second* orchestrator2 server (a separate `--cwd …\orchestrator2` hub, live
  since earlier that morning) via a live `claude.exe --resume 424654f8…`. When
  the current hub tried to resume the same session as a new runtime, the
  duplicate-session guard (`sdk_bridge.connect` → `find_foreign_claude_for_session`)
  correctly **refused** — two agents on one JSONL commit over each other.
- **Why the message was misleading — three compounding gaps:**
  1. `worker_loop`'s `DuplicateSessionError` branch broadcast the real reason and
     then **`return`ed — the worker task exited.**
  2. With no worker draining the `event_queue`, `_enqueue_prompt` happily put the
     prompt on it (`rt.bridge is not None`), the ack said `enqueued`, no turn
     started, and the frontend's 8 s watchdog fired the generic wedge text.
  3. The real explanation had been broadcast once, at connect time. A tab that
     attached *after* that (the lobby attaches slightly after the runtime spins
     up) never received it — `_send_initial_state` didn't replay it. So the user
     only ever saw the generic watchdog.
  - Bonus breakage: the refusal message told the user to "reconnect with
    /connect", but with the worker exited nothing drained the `event_queue`, so
    `/connect` was a no-op. The advice couldn't work.
- **Fix:**
  - New `State.connect_blocked_msg`. Set in the `DuplicateSessionError` branch;
    cleared in `connect()`'s success block (covers every connect path).
  - The worker no longer exits — it parks in `_wait_for_reconnect_trigger()`
    until an explicit `/connect` (or a message), then re-attempts. So once the
    other process is closed, `/connect` actually recovers (as the message says).
    Still no backoff spin — it only retries on a real trigger.
  - `_enqueue_prompt` rejects a prompt while `connect_blocked_msg` is set,
    re-surfacing the real reason and returning False → the tab acks `rejected`
    (which the watchdog ignores) instead of `enqueued`.
  - `_send_initial_state` replays `connect_blocked_msg` (after history) so a
    late-attaching tab sees the reason.
  - Status bar shows `already open elsewhere` (error class), not `idle`.
  - Tests: `test_prompt_echo.py::test_enqueue_rejects_and_resurfaces_reason_when_connect_blocked`
    and five `_wait_for_reconnect_trigger` cases in `test_queue_drain.py`. Suite
    365 passed.
- **Note for users:** the underlying situation (a session genuinely open in two
  places) is still a real thing you have to resolve — switch to the other
  server's window, or `taskkill /PID <pid> /T /F` the orphaned `claude.exe` and
  `/connect`. The fix only makes the orchestrator *say so correctly* instead of
  blaming a phantom wedge.
- **Caveat re: killing the child `claude.exe`:** if its parent server is still
  alive, that server auto-reconnects and **respawns a replacement within a
  second**, re-grabbing the session — so killing the child alone doesn't free it.
  The lever is the *owning server* (or use its window). Observed 2026-08-28:
  killing the holder immediately produced a new `claude.exe` from the same
  parent server.
- **Follow-up enhancement — DONE (2026-08-28):** the lobby now detects this
  *before* forking a doomed runtime. `open` calls
  `proc_guard.find_foreign_session_holder(sid)`; if the session is already held
  elsewhere it sends `session_elsewhere` (with the owning hub's port when the
  holder is one of our servers) and `lobby.js` offers to go to that window
  instead. See design.md § 8. Still open: the `POST /api/session/launch` path
  (a second launcher, which is how this was originally hit) doesn't yet redirect
  — it parks with the connect-blocked message. Wiring the same detector into the
  launch response + the launcher's browser-open is the remaining piece.


## A server that falls back to a random port is orphaned forever — OPEN (found 2026-08-28)

Found while checking a user's control experiment ("I started a session from
Windows, then tried WSL again, same problem"). It didn't test what it looked
like it tested, and this is why.

- **Observed:** four `server.py` processes alive at once, listening on **57645,
  53935, 51177, 51176** — every one an ephemeral port, **none on the default
  8420**, and none of them passed `--port` or `--standalone`. Two were 8 days
  old. Meanwhile 8420 was completely free and bound fine on the first try.
- **How they get there.** `_bind_port` falls back to an auto-picked free port
  when the requested one is held (`server.py` ~4191): a launch arriving while
  the outgoing hub still owns 8420 retries `_probe_hub` for 30 s, and if the
  dying hub never answers it prints *"Port 8420 in use but couldn't join the
  hub — starting a separate server"* and binds a random port. Confirmed here:
  hub pid 3640's last activity was 10:19:01, and the launch at 10:19:54 landed
  on 57645.
- **Why it's permanent.** Discovery is *only* "probe the configured port".
  A server on 57645 is undiscoverable: it never migrates to 8420 when 8420 frees
  up, and no later launch can ever find it. So the fallback isn't a graceful
  degradation, it's a one-way door — every subsequent launch repeats the whole
  dance and adds another orphan. Hub reuse is effectively dead on this machine
  and each session pays a full separate server (and its own SDK startup).
- **It also masks the namespace check.** A WSL launch probing 8420 finds
  nothing at all, so it never reaches the win32-vs-`wsl:*` comparison — it goes
  straight to serving. That is why the control experiment above could not
  reproduce a hub join regardless of the code.
- **Proper fix:** discovery must not be "one hard-coded port". Have the serving
  process write a small hub record (port, pid, namespace, account) to a
  well-known file and have `_probe_hub` consult it when the configured port has
  no hub, validating the pid is alive before trusting it. Then a server that
  had to take 57645 is still findable. Secondary: when the fallback fires,
  consider re-trying the canonical port briefly after the outgoing hub exits,
  so the common "restart" case lands back on 8420 rather than drifting.

## A WSL launch joined the Windows hub and got an invented `D:\mnt\d\…` cwd — FIXED (2026-08-28)

- **Symptom:** *"i ran server.py from within WSL, then went to localhost:8240
  from windows, went into the session, and it says SDK connection failed
  (attempt 1/10: Failed to start Claude Code: [WinError 267] The directory name
  is invalid), retrying in 2s…"*
- **What happened.** WSL2 shares `localhost` with Windows, so the WSL launch
  probed port 8240, found the **Windows** hub (pid 3640), and POSTed
  `/api/session/launch` with `cwd=/mnt/d/mypage/inhahe.com`. It never started a
  server of its own — there isn't a single Linux-side line in the log.
- **`Path.resolve()` doesn't reject a POSIX path on Windows, it fabricates one.**
  It anchors the leading `/` to the current drive, so `_create_runtime`'s
  `Path(cwd).resolve(strict=False)` produced `D:\mnt\d\mypage\inhahe.com` — a
  directory that has never existed:

  ```
  09:56:47  runtime s17 started (cwd=D:\mnt\d\mypage\inhahe.com, resume=None)
  09:56:47  connect: resume=None, cwd=D:\mnt\d\mypage\inhahe.com
  09:56:51  SDK connect failed (attempt 2): Failed to start Claude Code: [WinError 267] ...
  ```

- **Then it retried for nine minutes.** Attempt 1 at 09:56:47, still going at
  10:05:11 — one `claude.exe` spawn every 30 s that could not possibly start.
  The connect loop assumes failures are transient: 10 attempts with backoff, and
  then, if the user has queued a prompt, retry *forever*. And the only thing the
  user was ever told is `[WinError 267] The directory name is invalid`, which
  does not contain the directory.
- **Fix — three independent defects, three fixes** (`server.py`, `sdk_bridge.py`):
  - **Reuse now checks the path namespace.** `_path_namespace()` returns
    `win32`, `wsl:<distro>`, or the bare platform; `/api/whoami` reports it and
    `_probe_hub` refuses a hub whose namespace differs, printing both sides and
    falling through to starting its own server. This is the earliest possible
    point and protects every path in the handover — `config_dir` and the
    resume id's project directory as much as `cwd`. A hub with no `namespace`
    key predates the check and is still joined, so a routine same-OS relaunch
    isn't split onto a second port after the upgrade.
  - **`_create_runtime` rejects a cwd it can't see**, raising
    `NotADirectoryError` naming *both* the requested and the resolved form
    (showing only `D:\mnt\d\…` would send the user hunting for a path they
    never typed). This is the funnel for every way a session is born — the
    launch API and the lobby's *open*/*new* — and it covers the old-hub window
    that the namespace check can't. `_launch_into_hub` now prints the hub's
    rejection reason instead of swallowing it.
  - **A missing directory is a fatal connect failure, not a transient one.**
    `connect()` raises `UnusableCwd` before spawning anything, and the loop's
    new `_fatal` flag skips both the attempt cap and the retry-forever branch,
    replacing "Send a message to retry" — which was false — with the one action
    that works.
- **Tests:** `tests/test_cwd_namespace.py` (14). Mutation-verified against 10
  mutations — dropping the namespace check, collapsing WSL and Windows into one
  namespace, treating a missing `namespace` key as a mismatch, removing the cwd
  validation, naming only the resolved path, skipping the pre-spawn check, and
  each half of the fatal-failure short-circuit — all caught. The two connect-loop
  cases drive the real `worker_loop` and assert **one** attempt, not ten.
- **Residual:** running orchestrator2 in WSL now starts a *separate* server on
  another port rather than joining the Windows hub. That is correct — they can't
  share paths — but it does mean two hubs and two browser tabs if you work in
  both. Sharing a session across the boundary would need path translation
  (`/mnt/d/x` ↔ `D:\x`) and a CLI binary per namespace; not attempted.
  **That residual produced the next entry immediately.**

## A WSL launch died with a bare `ModuleNotFoundError: No module named 'uvicorn'` — FIXED (2026-08-28)

- **Symptom:** *"when i ran server.py from wsl the first time, it worked, then
  after i shut the server down and ran server.py from wsl again, it says
  'ModuleNotFoundError: No module named 'uvicorn''"*
- **The first run never started a server**, so "it worked" is not evidence that
  anything was installed. It joined the Windows hub (the entry above) and
  `sys.exit(0)`'d at `server.py:3983` — several hundred lines above `import
  uvicorn`, which sits inside `main()` because the `--detach` parent must not
  pay ~1.5 s to import a stack it never serves with. The second run, with that
  hub shut down, fell through to serving for the first time and met a WSL
  `python3` that genuinely has none of the dependencies.
- **The namespace fix made this the permanent WSL path.** A WSL launch now
  always declines to join a Windows hub, so it always reaches the lazy imports
  — meaning a bare traceback, with nothing in it to say *which* of the several
  Pythons on this machine lacked the package, became the standard WSL first-run
  experience. That is a usability regression introduced by the previous fix,
  not a pre-existing one.
- **Fix:** `_require_dependencies()` (`server.py`) pre-flights `fastapi`,
  `uvicorn`, `websockets`, `psutil` and `claude-agent-sdk` with
  `importlib.util.find_spec` — which *locates without importing*, so the lazy
  imports it is protecting stay lazy — and on failure prints every missing
  distribution at once (fixing five one traceback at a time is five restarts),
  the interpreter's own `sys.executable`, and the exact
  `<that python> -m pip install -r <abs path>/requirements.txt` to run. On a
  non-Windows launch it adds that this is not the Windows Python and that a
  server started here can't share paths or sessions with that one.
  `graphifyy` is deliberately *not* required: it is named in a prompt for the
  agent to run, never imported by us.
- **Where it is called matters.** It runs immediately before the startup
  message, which is after every path that exits without serving (the hub join,
  `--detach`, and the port-in-use re-join) and before the splash pre-server
  thread starts. Earlier would reject launches that only ever join a hub and
  need none of these packages; later would leave a splash page listening on a
  bound socket with a traceback behind it.
- **Tests:** `tests/test_dependency_preflight.py` (16). Mutation-verified
  against 11 mutations — removing the call, hoisting it above the hub join,
  making a miss non-fatal, short-circuiting the check, printing the module name
  instead of the distribution, reporting only the first miss, letting a broken
  spec raise, dropping the interpreter line / the install command / the
  non-Windows hint, and reporting everything as installed — all caught. The two
  call-site tests drive the real `main()`.

## A dead `claude.exe` left the session unusable for 5 hours, and survived a restart — FIXED (2026-08-25)

- **Symptom:** *"in my 'OSb' session, I got 'Turn ended without a result message
  (CLIConnectionError: Cannot write to terminated process (exit code:
  2147483651))' … i closed the window, ran orchestrator2 in that directory
  again, told claude to continue, and got the same api error again."*
- **Not the drive this time.** The same exit code as the 2026-08-20 entry
  (`0x80000003`, `STATUS_BREAKPOINT`) but with 79 GB free on `D:` and 577 GB on
  `C:`, so the earlier full-disk explanation doesn't apply — it's a Bun panic
  inside the bundled CLI (see "Root cause of the crash itself" below). The next
  several bullets are about what we did *after* it aborted, which was the worse
  half of the bug.
- **We saw it die and did nothing.** At 15:03:25 the SDK reported the exit code
  straight out of the message reader:

  ```
  15:03:25,172  Fatal error in message reader: Command failed with exit code 2147483651
  15:03:25,194  dispatcher crashed: gen=1 Command failed with exit code 2147483651
  15:03:25,566  run_turn failed: SDK dispatcher died mid-turn
  ```

  `worker_loop`'s handler then reached `if config.auto_reconnect:` — which
  defaults to `False` and wasn't set — and fell through to "await next input".
- **So the runtime became a zombie for 5h21m.** State clean, `busy` False,
  status bar reading a perfectly ordinary "idle" — indistinguishable from a
  healthy session — while `bridge.client` pointed at a corpse. The next log
  entry for that runtime is the user typing "Continue" at **20:24:48**, which
  failed on the first write.
- **And restarting couldn't fix it.** The relaunch is visible in the log as
  pid 107816 starting at 20:24:38 and shutting down 0.3 s later: standard hub
  reuse. It reattached to the *same* runtime `s3`, holding the *same* dead
  client, so the second "Continue" at 20:25:24 failed identically. There was no
  user-reachable route to recovery at all short of knowing about `/connect` or
  killing the hub.
- **Root cause:** recovery from a dead subprocess was opt-in. But a dead
  subprocess is not a preference — there is nothing left to talk to and no
  prompt can ever succeed again, so "leave it broken" is not a policy anyone
  would choose. The one thing that genuinely needed a knob (reconnecting after
  *any* failed turn, including ones where the CLI is fine) had been conflated
  with it under one flag whose help text still promised an "auto-continue" that
  was removed in 2026-08-14.
- **Fix** (`sdk_bridge.py`):
  - Two detectors, because the CLI can die at either end of the pipe:
    `_message_dispatcher` (read side — `receive_messages()` raises with the exit
    code, and this is the **only** one that fires while the session is parked,
    i.e. the entire 5-hour window) and `run_turn`'s `query()` (write side —
    `CLIConnectionError`, what a typed prompt hits). Both call
    `_note_transport_death()`.
  - That sets `_transport_dead` and, when no turn is live, pushes
    `("connect", "transport-dead")` onto `event_queue` — it can't reconnect
    itself, since the SDK transport's anyio scope belongs to the worker task.
  - `_recover_dead_transport()` reconnects on the worker task and *says so* in
    the transcript, both on the attempt and on the outcome. Silence was half the
    bug: after "Turn failed" the UI looks normal whether or not the session can
    ever work again.
  - Bounded by `TRANSPORT_DEATH_MAX_RECOVERIES` (3) in
    `TRANSPORT_DEATH_WINDOW` (600 s), because unconditional recovery of a CLI
    that dies *on every connect* would respawn it forever. On giving up it names
    `/connect`, and `/connect` clears the budget.
  - `_is_transport_death()` matches the transport's message text rather than
    just `CLIConnectionError`, so an ordinary turn error never tears down a
    healthy CLI. `_transport_dead` clears only on a *successful* `connect()`, so
    a failed reconnect can't restart the budget.
  - `worker_loop`'s handler moved into `_handle_turn_failure()` so the decision
    is directly testable; its `except Exception: pass` around the reconnect is
    now logged.
- **Root cause of the crash itself: a Bun panic.** `0x80000003` is not a
  debugger break here — the bundled `claude.exe` is a Bun standalone
  executable, and `STATUS_BREAKPOINT` is how Bun's panic handler aborts. An
  earlier occurrence (2026-08-11 15:30:22) got its report into our log before
  the race below started winning:

  ```
  [cli stderr] oh no: Bun has crashed. This indicates a bug in Bun, not your code.
  [cli stderr] https://bun.report/1.3.13/e_11b55f1dmgggEuhogC2qj71Co1/...
  ```

  That URL decodes to a stack trace, so it is the whole diagnosis — and it only
  ever appears on the CLI's stderr. Nothing we can fix from outside the process;
  what we *can* do is stop losing the report.
- **The 2026-08-25 death logged zero stderr lines** — though `_on_sdk_stderr`
  has been wired via `_make_options` the whole time. A race inside the SDK: it
  raises `ProcessError` the moment stdout closes and `wait()` returns non-zero,
  with the placeholder text "Check stderr output for details" and *without*
  draining stderr; `SubprocessCLITransport.close()` then does
  `self._stderr_task_group.cancel_scope.cancel()`, discarding every line the
  reader hadn't reached. A panic report is several KB, so it loses that race
  essentially always. **The recovery fix above tightened the race**, because it
  calls `close()` sooner than a human would — it would have destroyed the
  evidence for the bug it recovers from.
- **Fix (2026-08-25, part 2 — `sdk_bridge.py`):** `_on_sdk_stderr` now also
  appends to a `STDERR_TAIL_LINES` (60) ring buffer with an arrival timestamp;
  `_recover_dead_transport` awaits `_drain_stderr()` and logs
  `_log_stderr_tail()` **before** anything that could close the transport,
  including the give-up path. The drain returns after `STDERR_DRAIN_QUIET`
  (0.30 s) with no new line, restarting that window on each line so a burst
  isn't truncated, capped at `STDERR_DRAIN_MAX` (3 s) so evidence-gathering can
  never postpone the reconnect. The quiet period is measured from the later of
  the last line and the drain's start, so an *empty* tail still waits — that is
  the exact lost-report case, not a silent CLI. `connect()` clears the buffer so
  a new subprocess is never logged with the corpse's dying words, and the tail
  goes out as one log record rather than N.
- **Tests:** `tests/test_dead_cli_recovery.py` (29). Mutation-verified —
  including re-gating recovery behind `--auto-reconnect`, silencing either
  detector, dropping the budget, treating every exception as a death, returning
  immediately from the drain on an empty tail, draining after the reconnect
  instead of before, removing the drain's cap, and not clearing the tail on
  connect — all caught.
- **Residual:** the Bun crash is not reproducible on demand (three occurrences,
  all during long sessions with heavy background-task use). The orchestrator now
  recovers from it and preserves the `bun.report` URL; acting on that URL is a
  Bun / Claude-CLI matter, not ours.

## A turn that died before its first message said "working" for 16 hours — FIXED (2026-08-20)

- **Symptom:** *"i got 'Turn failed: Cannot write to terminated process (exit
  code: 2147483651)' in session OSa, that may have been due to a full drive, but
  the problem is that after that it still said 'working' for 16 hours."*
- **The CLI's death is not ours.** Exit code 2147483651 is `0x80000003`,
  `STATUS_BREAKPOINT` — the bundled `claude.exe` aborting. (Originally read as
  the drive filling up underneath it; the 2026-08-25 entry above identifies it
  as a Bun panic instead, from a `bun.report` URL the CLI printed on stderr.
  The drive was not full in either case.) What *is* ours is the state it left
  behind.
- **What the log showed** was two lines two milliseconds apart with nothing
  between them:

  ```
  03:09:52,548  run_turn enter: turns=234 drained=0 ta.is_set=True prompt=...
  03:09:52,550  run_turn failed: Cannot write to terminated process (exit code: 2147483651)
  ```

  **No `run_turn finally` line.** That block exists precisely to make this
  impossible — its own comment says it "ensures state.busy can never get stuck
  True" — but it was never reached.
- **Root cause** (`sdk_bridge.py`, `run_turn`): the statements that mark the
  session working — `state.busy = True`, `state.turn_started_at`,
  `self.turn_active.set()`, the `turn_start` broadcast — and the
  `await self.client.query(prompt_text)` that follows them all sat **above** the
  `try:`. The SDK's `subprocess_cli.write()` raises `CLIConnectionError`
  *synchronously* on the caller's stack when the process is gone
  (`claude_agent_sdk/_internal/transport/subprocess_cli.py:513`), so the one
  failure mode that happens before the turn has read a single message was the
  one failure mode the guard did not cover.
- **The status bar was the lesser half.** `turn_active` stayed set too, and
  `_message_dispatcher` uses that flag to choose between `turn_msg_queue` and
  `_handle_async_message`. Left set, every subsequent SDK message was filed into
  a queue with no reader: the session was not merely mislabelled, it was deaf,
  and no later prompt could have recovered it.
- **Fix:** move the whole block inside the `try:`, so the `finally` covers the
  turn from the instant it claims the session. Also fixed a latent bug found
  while testing it — the `finally` nulled `state.turn_started_at` and *then*
  computed the synthetic `turn_end` duration from it, so every abnormal turn
  reported `0:00`; the start time is now captured into `_started_at` first and
  passed to `_finish_interrupt()`, which grew an optional parameter for it.
- **Tests:** `tests/test_turn_start_failure.py` (6). Mutation-verified —
  restoring the old `try:` placement fails 4 of them, reproducing the report.

## `CONNECT_TIMEOUT` was cutting live connects at 45 s — FIXED (2026-08-20)

> **Superseded 2026-08-29.** The 180 s this landed on was never actually
> reached: the SDK's own 60 s `initialize` deadline pre-empted it, and the
> budget needed to scale with session size anyway. See the entry at the top of
> this file.

- **Symptom:** *"i often get 'sdk connetion failed' one or more times when
  starting up a session, like this time. maybe it times out too quickly?"* —
  which was right.
- **Measured, over 580 connects** in `orchestrator2.log` (`connect: resume=` to
  the `dispatcher start` that follows a completed handshake): p50 **5.3 s**, p90
  **20.4 s**, p98 **34.9 s**, p99 **38.2 s**, max **44.7 s** — against a ceiling
  of 45 s. And 32 timeouts, **5.5% of attempts**.
- **That shape is diagnostic.** A genuinely hung connect and a merely slow one
  would form two populations with a gap between them. Instead the successes run
  continuously into the ceiling and stop dead there. That is a cutoff slicing a
  live distribution, not a threshold catching a distinct failure.
- **The retries confirm it.** Both logged startup failures look like this:

  ```
  19:10:50  connect: resume=424654f8..., cwd=D:\...\orchestrator2
  19:11:36  SDK connect timed out after 45s - retrying
  19:11:43  connect: resume=424654f8...      (attempt 2)
  19:12:05  SDK connected after 1 retries    (21.9s)
  ```

  The second attempt does nothing the first wasn't; it just runs against a warm
  file cache after the first paid to read the bundled `claude.exe` past Windows
  Defender. Timing the first one out therefore *costs* a teardown, a backoff and
  a whole second spawn to reach a handshake that was nearly done — so the short
  timeout made startup slower on average, not faster. Hub startup compounds it
  by spawning several CLIs within seconds of each other.
- **Fix** (`sdk_bridge.py`): `CONNECT_TIMEOUT` 45 s → **180 s** (~4.7× p99,
  4× the slowest observed success). It remains a backstop against a hang, not a
  performance budget — a hung handshake still gets torn down, just not a live
  one. `connect()` now also logs the handshake cost on *success*, so the
  distribution stays observable instead of having to be reconstructed from
  surrounding lines.
- **And the message said nothing.** `str(asyncio.TimeoutError())` is the empty
  string, which is why the log read `SDK connect failed (attempt 1): ` and the
  user saw an unexplained "SDK connection failed". All three connect-failure
  messages now interpolate a `_why` that names the timeout and its duration, or
  falls back to the exception's class name when it too has no text.
- **Tests:** `tests/test_connect_timeout.py` (6). Mutation-verified — restoring
  45 s fails the margin test.

## The `bg-done` bell rang three seconds before the model carried on — FIXED (2026-08-16)

- **Symptom:** *"i seem to still be hearing bells when no turn is starting or
  stopping, even though i have `--bell turn-done bg-done rate-hit`"*.
- **The filter was innocent.** The per-ring logging added for the previous
  report answered this one directly: all 20 rings in the log were `turn-done` or
  `bg-done`, and the only *suppressed* rings were two `interrupt`s — correctly
  filtered. Nothing outside the `--bell` set ever reached the browser.
- **What the log did show** was a correlation with no exceptions. Every one of
  the nine `bg-done` rings was followed by a `ghost turn begin` **in the same
  session**, 2.3–5.0 s later:

  ```
  22:49:06,679  bg task bb1hiiw91 (Benchmark boot test (background)) completed: status=completed (via task_updated)
  22:49:06,681  bell: bg-done rung session=2e281cb3-47e title='OSa' busy=False turns=7
  22:49:10,233  ghost turn begin: SDK streaming without active run_turn (session_id=2e281cb3-47e)
  ```

  Same shape at 19:58:16→19:58:20, 20:20:51→20:20:54, 22:24:02→22:24:05,
  22:32:23→22:32:26, 23:02:12→23:02:17, 23:03:29→23:03:33, 23:21:56→23:22:00,
  23:23:37→23:23:40. Nine for nine.
- **Root cause.** That gap is the CLI's documented behaviour, already described
  in the "Why output can continue after an interrupt" comment at the top of
  `sdk_bridge.py`: a finished background task becomes a `<task-notification>`
  fed to the model, which resumes streaming. So at the completion instant
  `run_turn` has exited and `state.busy` is False — the session *looks* parked —
  but it is about to keep working on its own. `in_bg_wait()` samples exactly
  that instant, so "the model is between tool calls" was read as "the user is
  waiting on this task".
  The bell means *come and look, this is finished and nothing else is
  happening*; ringing three seconds before the model picks the result up
  inverts it. Worse, a false ring is indistinguishable by ear from a real one,
  so it devalues every other bell.
- **Fix** (`sdk_bridge.py`): defer the *judgement*, not the bell.
  `_announce_bg_completion` now calls `_arm_bg_done_bell()`, which starts a
  `BG_DONE_BELL_GRACE` (10 s, comfortably above the observed 5.0 s worst case)
  timer; `_ring_bg_done_after_grace` re-checks `in_bg_wait()` at the deadline and
  logs `bell: bg-done withdrawn` if the session resumed.
  `_cancel_bg_done_bell()` is called wherever `busy` flips True — `run_turn` and
  `_begin_ghost_turn_if_needed` — so a ghost turn that both begins *and* ends
  inside the grace (2.1 s ones are logged) still withdraws the ring, which the
  fire-time re-check alone would miss. One pending timer at a time, so a burst of
  completions rings once; `_shutdown` cancels it.
- **Tests:** `tests/test_bg_done_bell_grace.py` (9). Mutation-verified — removing
  the coalescing, the ghost-turn cancel, or the deferral each fails a test.
- **Residual:** the ghost turns themselves are a separate, still-open issue
  (`turn_active` is set only by `run_turn`; see the entry further down). This
  fix makes the bell correct *given* ghost turns rather than depending on them
  going away.

## A silently-cancelled worker made a session ignore every prompt — FIXED (2026-08-16)

- **Symptom:** *"in the same session, i manually entered the prompt again while
  it was idle since it didn't send, and it didn't do anything. it showed the
  prompt coming from me, but stayed idle."* Same session as the entry below
  (rid `s5`, forward raytracer), but a **different bug** — and this one is not
  in the "prompt didn't send" class at all: the prompt was routed *correctly*.
- **How it was found.** Every observable signal said the session was healthy, so
  the log alone could not settle it. The chain was:
  1. `bell: turn-done -> browser` at 21:30:29.161 proves the worker reached
     `_await_next_prompt` and parked (that log line is its first statement).
  2. The persisted queue file
     (`…/orchestrator2/queues/D--visual-studio-projects-forward-raytracer.json`)
     still held exactly **one** entry with `saved_at` = 21:39:24 and was never
     touched again. `PersistentDeque` mirrors on *every* mutation and does not
     dedup — so the second prompt never reached `queued_prompts`, i.e. `busy`
     and `connecting` were both False and it went to `event_queue`.
  3. No `run_turn start` for bridge `6d70` ever followed ⇒ nobody consumed it.
  4. A live probe attached to `ws://…/ws?rid=s5` and sent `/connect`: **nothing
     happened at all.** The worker was gone.
  5. `sys.remote_exec` (PEP 768) into the running hub dumped every runtime's
     worker task. For `s5`: `"stop_event": false`, `"busy": false`,
     `"viewers": 1`, `"event_queue_size": 3`, and
     `"worker_repr": "<Task cancelled name='sdk-worker' …>"`.
- **Root cause — a cross-task anyio cancel scope inside the SDK.**
  `claude_agent_sdk`'s `SubprocessCLITransport.connect()` does
  `self._stderr_task_group = anyio.create_task_group(); await …__aenter__()`
  in whichever task calls it — the **worker**. Its `close()` does
  `cancel_scope.cancel(); await …__aexit__(...)` in whichever task calls
  `disconnect()`. anyio delivers a scope's cancellation to the task that
  *entered* it, so a disconnect from any other task **cancels the worker**; and
  `close()` wraps the whole thing in `with suppress(Exception)`, which also eats
  the "cancel scope exited in a different task" `RuntimeError`. The offender was
  `api_session_launch`'s `asyncio.create_task(existing.bridge.reconnect())`.
- **Why it was invisible.** The reconnect *succeeds* (`dispatcher start: gen=3`
  is in the log), `stop_event` stays clear, the client, state, runtime and
  sockets are all fine, and the status bar reads "idle" — which is true. The old
  `_safe_worker` re-raised `CancelledError`, so the task just vanished with no
  log line. Every prompt afterwards landed in an `event_queue` nobody was
  reading, forever.
- **Fixed in three layers:**
  1. *The trigger* — `api_session_launch` now pushes `("connect", "")` onto
     `bridge.event_queue` so the reconnect runs **on the worker task**. This
     also matches what a user-typed `/model` mid-turn already did.
  2. *Detection* — `SDKBridge._warn_if_foreign_task()` logs an error with a
     stack from `connect()`/`disconnect()` whenever they run off the worker
     while it is alive. `_shutdown()` is exempt by construction: it has already
     joined the worker, so `_worker_task.done()` is True.
  3. *Recovery* — `SDKBridge._spawn_worker()` replaces `start()`'s inline task.
     A `CancelledError` arriving with `stop_event` clear is never legitimate, so
     it now logs loudly and **respawns a fresh worker task**. It must be a fresh
     task, not a retry inside the existing one: anyio's
     `CancelScope._deliver_cancellation` re-arms via `call_soon` for every task
     still in the scope, so a worker that swallowed the cancellation would be
     cancelled again at its next await forever. The restart passes
     `skip_connect=self.client is not None` so it cannot fork a second
     `claude.exe` onto the session, skips `config.initial_prompt`, and clears
     stale `busy`/`connecting`.
- **Covered by** `tests/test_worker_survival.py` (10 tests), including one that
  pins the *upstream* anyio behaviour — if a future anyio/SDK release stops
  cancelling the host task, that test fails and tells us the workaround can be
  revisited.

## The 8s prompt watchdog was disarmed by ambient traffic — FIXED (2026-08-16)

- **Was:** `PROMPT_WATCHDOG_MS = 8000` in `static/app.js` was cleared by **any**
  inbound message, on the theory that traffic proves the prompt is being
  handled. It doesn't. `_tick_runtime` force-sends a status snapshot every
  `_STATUS_HEARTBEAT_SECONDS = 30.0` whether or not anything changed, and bells,
  panel updates and background-task notices arrive independently of any prompt.
  A backend that had stopped processing prompts entirely kept re-disarming the
  watchdog meant to notice exactly that. It didn't *cause* the wedged-worker
  outage above, but it is why the tab sat on "idle" for an hour without a word.
- **Now — a correlated ack.** The tab tags each watched prompt with a
  `prompt_id` (`TAB_ID + '-' + seq`); the server replies
  `{"type": "prompt_ack", "prompt_id", "disposition"}` **to that socket only**,
  naming where the prompt landed:

  | disposition | meaning | watchdog verdict |
  |---|---|---|
  | `queued` | parked in `state.queued_prompts` behind a turn/connect | benign — it's visible in the panel |
  | `enqueued` | handed to the worker on `event_queue` to run *now* | a turn **must** start, else escalate |
  | `immediate` | answered inline (immediate command, `/mcp`, interrupt) | done |
  | `rejected` | nothing took it | already explained by the server |
  | *(no ack)* | the server never took it | half-open socket / dead server — close and reconnect |

  Only `enqueued` carries an obligation, and it is the disposition the outage
  produced. Its failure gets its own message — reconnecting wouldn't help,
  because the server is fine and the *session* isn't.
- **Two subtleties worth keeping:**
  - The check is "did a turn ever start", not "is it busy now" — a short turn
    can start *and finish* inside the 8s window, and accusing a session that did
    exactly what it was asked is worse than not checking. `_watchedPrompt.started`
    latches the busy transition.
  - "A turn visibly ran" also satisfies the *no-ack* branch, so a cached tab
    talking to a pre-ack backend behaves as before rather than closing healthy
    sockets. Frontend/backend version skew is a recurring reality here.
- **The ack is applied in one place**, a wrapper around `_dispatch_ws_message`
  that defaults to `immediate` when no branch dispositioned the prompt. The
  dispatcher has ~two dozen early returns and a missing ack does *not* fail
  safe: it produces a spurious "no response from the server" and closes a
  working socket. `{type:'command'}` re-dispatches the **same dict** so the ack
  bookkeeping isn't duplicated.
- **Covered by** `tests/test_prompt_ack.py` (14 tests), each mutation-verified.

## "The prompt didn't send", 4th instance — CLASS CLOSED (2026-08-16)

- **Symptom:** *"i started a session, sent a prompt while it was connecting, it
  queued it, after it connected, the prompt didn't send. it seems there's
  always another 'the prompt didn't send' bug since the beginning."*
- **This trigger:** `api_session_launch` (server.py) reuses a live runtime when
  a second launch targets a session already hosted, and applies the launcher's
  `--model`/`--effort` by firing `asyncio.create_task(existing.bridge.reconnect())`
  — from the server, unawaited, with no worker checkpoint after it. Confirmed in
  `orchestrator2.log`: launcher pid 97544 POSTed at 21:38:55, the hub reconnected
  runtime s5 (forward raytracer), and `connect` → `dispatcher start` spans
  **21:38:55.646 → 21:39:28.847 = 33 s** of `state.connecting`. A prompt typed in
  that window went to `queued_prompts`, and no `run_turn` for that bridge ever
  followed.
- **The class, not the instance.** Prompts typed while `busy`/`connecting` go to
  `state.queued_prompts`; the worker parks on `event_queue.get()`; the two are
  joined only by hand-placed drain checkpoints. Previous instances (`/model`
  reconnect, `bg-all-done` ghost turn) were each fixed by adding one more
  checkpoint, which is why there was always another one.
- **Fixed structurally:** the poke is wired to the *container*
  (`PersistentDeque.add_listener` → `SDKBridge._poke_for_queued_prompt`), so
  every writer pokes the parked worker automatically and a future writer cannot
  reintroduce the bug. Plus `connect()` re-pokes on the way out (covering
  reconnects nobody awaits), the poke is declined while `connecting` (the client
  is assigned before its handshake completes), and it yields — by re-queueing,
  not dropping — to any event already pending, so it can't preempt `/model` or
  `/btw`.
- **Covered by** `tests/test_queue_poke.py` (14 tests), each mutation-verified;
  the end-to-end one reproduces the report against a fake SDK client with a slow
  `connect()`, and fails with a timeout when the `connect()` re-poke is removed.
- **Two existing tests changed meaning** and were rewritten rather than deleted:
  `test_btw_while_idle_sends_the_btw_text_not_the_queue_head` used a premise that
  is now unreachable (a prompt sitting in the queue while the worker is parked
  and unpoked), and now models the reachable race instead — both signals landing
  together as a turn ends — which pins the priority rule as well as the original
  `/btw` bug.

## A 141-second compaction looked like a hung session — FIXED (2026-08-16)

- **Symptom** (reported as *"in 'OSc' it said what it had to say and then
  scheduled a wakeup, but the client shows still 'working'"*): the status bar
  read `working` for minutes with no output, after the model had visibly
  finished speaking.
- **Not a stuck status.** The status was accurate — the turn genuinely had not
  ended. Reconstructed from the session JSONL (`7baa01e0…`) and
  `orchestrator2.log` (bridge `be00`): `run_turn start` at 19:28:05 with no
  ResultMessage ever, so `state.busy` stayed True. At 23:30:22Z the model
  emitted its closing text **plus a `ScheduleWakeup` tool call** — a tool, so
  `stop_reason: "tool_use"`, so the harness had to make another API call and the
  turn could not end there. That next step tripped an auto-compaction:
  `durationMs: 140989` (141 s), `preTokens: 167897 → postTokens: 5941`,
  `trigger: "auto"`, zero output for the whole window. The post-compaction
  continuation prompt then restarted the work, so the turn ran on (still going
  at 23:35:54).
- **Two real defects behind it:**
  1. The compaction was invisible. The CLI *does* emit
     `{type:'system', subtype:'status', status:'compacting'}`
     (`services/compact/compact.ts` → `setSDKStatus`); orchestrator2 ignored
     the `status` subtype entirely.
  2. Our own wakeup fired at 19:34:22 into that still-live turn and injected
     `"Resume work now…"` as a user message the model had to obey, mid-thought.
- **Fixed:** `_handle_system_message` records `state.cli_status` /
  `cli_status_started_at` and pushes a status update; `state_to_status_dict`
  ranks `compacting` above `busy` and times it; `run_turn` clears the flag at
  turn start so a CLI that dies mid-compaction can't strand the label;
  `_wakeup_timer` defers (re-arms for `WAKEUP_MIN_DELAY`) instead of injecting
  when `state.busy`; and `_cancel_wakeup` gained the same self-cancel guard as
  `_cancel_idle_timer` below, since the re-arm path makes the timer its own
  caller.
- **Covered by** `tests/test_compacting_status.py` (13 tests), each verified
  against a mutant with the corresponding line removed or inverted.
- **Remaining gap (accepted):** the deferral is time-based, not event-based —
  a wakeup that fires during a very long turn re-checks every
  `WAKEUP_MIN_DELAY` rather than waking on the turn-end edge. Cheap and
  self-correcting; worth revisiting only if the re-arm log line gets noisy.

## Idle teardown cancelled itself and leaked 19 live agents — FIXED (2026-08-16)

- **Symptom:** a hub up for one day had **19 `claude.exe` children still
  running**, several resuming the *same* session file (`424654f8`×3,
  `2af9ba44`×2, `7baa01e0`×2, `fd903a58`×2). These are not stray handles: each
  is a live agent, still resumed on its session, still able to edit files and
  commit — two of them on one conversation is exactly the hazard `proc_guard`
  exists to prevent.
- **Root cause** (`server.py`, `_cancel_idle_timer`): `rt.idle_timer` *is* the
  task running `_idle_teardown_after` → `_teardown_runtime` → `_cancel_idle_timer`,
  so the unconditional `timer.cancel()` fired a `CancelledError` into its own
  teardown. It landed at the next await — `await rt.bridge.stop()` — so
  `disconnect()` never ran and the CLI survived. `_teardown_runtime` had
  *already* popped the runtime from `runtimes`, so nothing listed the orphan
  and `api_session_launch`'s duplicate check could not see it either; that is
  why relaunching a session spawned a second and third agent on it.
- **Why it was silent for three weeks.** `CancelledError` is not an
  `Exception` (3.8+), so `except Exception` never saw it; and a task that ends
  *cancelled* rather than failed produces no `Task exception was never
  retrieved`. `py-spy dump` showed an idle event loop — correct and useless,
  because the dead teardown left nothing running. The only trace was in the
  log's counters: `tearing down` 126 vs `torn down` **58**, `reaped` **0**,
  last successful teardown **2026-07-27**.
- **Fixed in three layers:**
  1. `_cancel_idle_timer` compares against `asyncio.current_task()` and never
     cancels the caller (root cause).
  2. `SDKBridge.stop()` = `await asyncio.shield(self._shutdown())`, and
     `disconnect()` moved into a `finally` around the worker join, so neither a
     cancellation nor a wedged worker can skip it. `disconnect()` now snapshots
     the CLI (`proc_guard.snapshot_process`, pid + `create_time`) and kills it
     if it outlived the SDK's teardown, logging instead of swallowing.
  3. `cancel_and_join` is bounded by `CANCEL_JOIN_TIMEOUT` (10 s) and logs the
     stuck task's own stack, so the next variant of this diagnoses itself.
- **Tests:** `tests/test_bridge_shutdown.py` (5, real child processes) and
  `test_multisession.py::test_teardown_from_its_own_idle_timer_still_stops_the_bridge`.
  All mutation-verified: reverting the backstop fails 4 tests; reverting the
  shield fails `test_stop_survives_a_caller_that_keeps_cancelling`.
  `FakeBridge.stop()` in `test_multisession.py` was also fixed to `await` — with
  no await point it ran atomically and could never be interrupted, which is
  what hid this bug from the suite.
- **Still open (accepted):** `find_foreign_claude_for_session` excludes
  `_own_tree_pids()`, so it can never catch an orphan of our own making.
  Narrowing it risks a reconnect false-positiving against its own still-dying
  CLI; the three fixes above prevent the orphan at source instead.

## A lost write made every resume restart from July 24 — REPAIRED (2026-08-16)

- **Reported:** "after i changed the model, claude seemed not to remember any of
  the conversation that just happened."
- **It is a resume-point bug, not data loss.** On 2026-07-24 a lost write left
  NUL runs mid-file in two session JSONLs three minutes apart (size committed,
  data never reached disk). The record in the hole was a `tool_result`, so from
  then on every resume re-attached at the last intact node and grew a new branch
  there. Each new session therefore opened with three-week-old context. It had
  been doing this silently for weeks; a `/model` switch (which reconnects, and a
  reconnect is a resume) is what finally made it visible mid-conversation.
- **Corrected numbers.** An earlier version of this entry said "112,022 records
  (99.7%) unreachable". That was wrong and the metric behind it was wrong: a
  compaction writes `parentUuid: null`, so all pre-compaction history is
  off-chain *by design*. 361 of the raytracer's 362 roots are compact
  boundaries. Real abandoned work: **26 branches / 1,581 records** (raytracer),
  **10 branches / 2,695 records** (SlateOS) — and even those are mostly session
  prefixes whose work continued in later compaction segments.
- **Files:** `~/.claude-account-b/…/8d532b0a-….jsonl` (623 MB, 3 corrupt lines:
  15326 / 46927 / 49223) and `~/.claude-account-c/…/7baa01e0-….jsonl` (914 MB,
  2 corrupt lines: 68164 / 106218). Whole-corpus scan: exactly 2 of 48 files
  (2.92 GB) contain NULs — one machine-level event, not a per-session bug.
- **SlateOS needed no repair.** It compacted after the damage, so its live chain
  is rooted at a fresh boundary and forks away from nothing.
- **Repaired (raytracer), 2026-08-16:** dropped the 3 corrupt lines and
  re-parented *only* the live branch root onto the leaf of the newest preceding
  compaction segment (2026-08-16T14:00) — where the user actually left off.
  Verified: 112,309 records before and after (nothing lost), 0 corrupt lines, no
  NUL hole, chain 375 rooted at the 14:00 boundary, 0 abandoned. Backups and the
  repair script are in `D:\session-backups\`.
  - **The obvious repair would have been wrong.** Chaining all 27 sibling
    branches into one line looks right until you check the roots: they are
    compactions, so the branches are not a severed conversation and stitching
    them would fabricate an order that never happened.
  - Old branches were deliberately left in place — they are history, and
    rewriting history to look tidier destroys the evidence of what happened.
  - The stale `~/.orchestrator2_title_index.json` entry was dropped, since it is
    keyed on file size and the rewrite changed it.
- **Detector:** `check_session_integrity()` runs on every connect, warns once per
  bridge, and requires a hole *and* abandoned work. Two bugs found and fixed in
  it while validating against the real files: it reported the misleading
  `total − chain`, and `_REC_TYPE_RE` took the first `"type":"…"` on the line —
  which on a real assistant record is the message's own `"type":"message"`, so
  it never matched `assistant` at all. Tests: `tests/test_session_integrity.py`
  (20), both fixes mutation-verified. The first attempt at the metric test did
  *not* catch its mutation, because every fixture lacked pre-compaction history
  and so made the right and wrong metrics numerically identical.

## Why the browser kept showing a transcript the model could not see

- We render history by walking the JSONL top-to-bottom; the CLI rebuilds context
  by walking `parentUuid` backwards from the last message. While the chain is
  intact they agree, so the difference is invisible — and when it breaks they
  diverge in the worst direction: the transcript still looks complete.
- Our one guard against a bad resume (`expected_resume_sid`) was gated on
  `first_init`, and `init_seen` is set once and never cleared, so it could never
  fire on a *reconnect* — the only case that matters. Gate removed 2026-08-16.

## A prompt typed during a `/model` switch was never sent — FIXED (2026-08-16)

- **Reported:** "i changed the model, and while it was reconnecting to switch
  models, i typed in a prompt, which it queued, and when it finished changing
  the model, it sat there idle without sending the queued prompt."
- **Confirmed in `orchestrator2.log`** (bridge `6990`, session
  `8d532b0a`, forward-raytracer): turn ends 16:57:11 → worker parks in
  `_await_next_prompt()` → `/model` at 16:57:17 → `dispatcher cancelled gen=2`,
  `connect: resume=8d532b0a…`, `dispatcher start: gen=3` at 16:57:25 → and then
  **nothing for that bridge, ever again**. No `run_turn start` follows.
- **Cause — the two halves.** (1) `server.py` routes a typed message to
  `state.queued_prompts` whenever `state.busy or state.connecting`, and every
  config command (`/model`, `/effort`, `/thinking`, `/connect`, `/clear`)
  reconnects, holding `connecting` True for seconds. So a prompt typed in that
  window is *always* queued. (2) `_await_next_prompt` applied the command and
  went straight back to `await self.event_queue.get()`. Nothing else pokes it →
  idle forever with the prompt sitting in the panel.
- **Why it looked intermittent:** `_between_turns` reconnects at the top and
  drains `queued_prompts` further down, so the identical keystrokes worked when
  a turn happened to be ending and stranded the prompt when the session was
  already parked.
- **Fixed:** `_await_next_prompt` pops a queued prompt after
  `_apply_idle_config_command` returns True. Race-free because `connect()`
  clears `connecting` in a `finally` before `reconnect()` returns.
- **Also fixed, same family:** `worker_loop`'s initial-prompt wait was a second,
  hand-rolled copy of the idle loop that additionally dropped `/compact`,
  `/btw` and wakeups before the first turn. It now calls `_await_next_prompt()`,
  so there is exactly one idle wait.
- **Tests:** `tests/test_idle_config_reconnect_queue.py` (9). Mutation-tested
  both halves; removing the drain reproduces the hang as a `TimeoutError`.

## Bell rings when nothing bell-worthy happened — INSTRUMENTED, one cause fixed (2026-08-16)

- **Reported:** "it seems i often hear it when a turn wasn't just completed and
  no task just finished and there was no rate-hit, even though i start it with
  `--bell turn-done bg-done rate-hit`."
- **Ruled out by reading the code:** `parse_bell_events("turn-done bg-done
  rate-hit")` is REPLACE mode and yields exactly those three, so
  `requires-action` / `interrupt` are filtered at ring time. An *interrupted*
  turn returns early and rings `interrupt` (disabled), never `turn-done`. Ghost
  turns don't ring at all. `rate-hit` is edge-triggered on the status
  transitioning to `rejected`. The live hub (pid 6760) was verified to have
  been started with the reported flags.
- **Fixed — `--bell` is dropped when a launch reuses a running hub.**
  `_launch_into_hub()` forwarded only `cwd`/`resume`/`no_continue`/`model`/
  `effort`/`config_dir`, so a session launched into an existing hub inherited
  the *hub process's* bell set. Start the hub without `--bell` and every later
  `--bell ...` launch is silently ignored — precisely "even though i start it
  with". `bell_on` is now in the payload, in `_create_runtime()`'s overrides,
  and applied to an already-live runtime.
- **Instrumented — a bell used to leave no trace at all.** Neither the ring,
  the suppression, nor the broadcast logged anything, and the `bell` WS message
  carried no session identity. In a hub hosting several sessions, a bell from a
  background tab sounds identical to one for the session you're watching, so
  the report could not be confirmed *or* refuted. `ring_bell()` now logs every
  ring **and** every suppressed ring with event + session; `_flush_bell()` logs
  the broadcast; the message carries `session_id`/`session_title`.
- **Still open:** the remaining suspect is exactly that cross-session case — a
  turn completing in another session ringing while the user looks at a
  different tab. Next occurrence, grep `bell:` in the log: the line names the
  event and the session, which settles it. If it is cross-session, the fix is
  UI (attribute or mute per session), not the ring sites.
- **Tests:** 6 in `tests/test_bell_attribution.py`, verified against three
  reintroductions (payload field dropped; ring log dropped; suppression log
  dropped).

## Ctrl-C said "interrupted" but the turn kept talking — PARTLY FIXED, partly not-a-bug (2026-08-15)

- **Reported:** "often when i hit ctrl-c to interrupt a turn, it says it's
  interrupted but then it keeps working and outputting text, and i have to hit
  ctrl-c once or maybe twice more to get it to stop."
- **The "it says it's interrupted" half was a real bug — FIXED.** `server.py`
  broadcast `"Turn interrupted."` the instant Ctrl-C arrived, *before* the
  control request was even sent, and on top of `run_turn`'s own reporting. So
  the UI asserted the turn had stopped while the CLI was demonstrably still
  streaming. `_do_interrupt()` now stays quiet when a live turn will report for
  itself, and otherwise says `"Interrupting…"` — a claim it can actually vouch
  for.
- **The "keeps working" half is not a bug, and the guard written for it was
  reverted.** From `orchestrator2.log` (pid 6760, bridge=6990):

  ```
  23:05:11,152  UserMessage during turn: '[Request interrupted by user for tool use]'
  23:05:11,153  run_turn exit: normal (turns=2)
  23:05:22,033  async AssistantMessage between turns: ... blocks=['ThinkingBlock']
  23:05:22,033  ghost turn begin: SDK streaming without active run_turn
  23:05:24,087  async UserMessage between turns: '[Request interrupted by user]'
  ```

  The matching session JSONL fills in the gap: at `23:05:15.371` a background
  Bash task ("Find light area usage in scenes", exit code 0) completed and
  enqueued a `<task-notification>`, which the CLI fed to the model. The CLI
  builds its `abortController` **per dequeued command** (`print.ts:2133`) and
  the SDK `interrupt` control request only calls `abortController.abort()` — it
  never clears the command queue, so the notification dequeued afterwards ran
  with a fresh controller. `print.ts:2010-2013` states this "matches TUI
  behavior", so interactive Claude Code does the same. The variable delay
  (2.1 s / 8.5 s / 10.9 s across the 3 resurrections in 20 logged interrupts) is
  each background task's own runtime plus the API round trip.
- **Why the guard was reverted rather than kept:** it discarded a completed
  background task's result that the CLI had already written to the session
  JSONL, so the live view would disagree with the history on reload — output
  vanishing and then reappearing. It also re-asserted `client.interrupt()` at a
  legitimately woken model, cancelling work the user could see.
- **Tests:** 7 in `tests/test_interrupt_ghost_turn.py` (replacing the deleted
  `test_interrupt_guard.py`), verified against three reintroductions —
  suppression guard restored, auto-re-assert restored, unconditional server
  announcement restored. Each fails a distinct subset.
- **Open, if the user wants it:** the SDK has no equivalent of interactive
  mode's `ctrl+x ctrl+k` (`handleKillAgents` → `clearCommandQueue()` + kill
  running agents) or Esc-pops-queued-commands. A deliberate "stop background
  tasks too" control would give the user the "stop *everything*" reading of
  Ctrl-C honestly, instead of inferring it.

## A completed background task could vanish without a trace — FIXED (2026-08-15)

- **Found while answering:** "why wouldn't it say 'background task x
  completed'? there's already a mechanism that shows when a background task
  completes." There is, and it's wired end to end
  (`task_notification` → `bg_complete` → `chat.js:_addBgComplete`) — but it had
  a silent hole.
- **Root cause:** `_handle_system_message`'s `task_notification` and
  `task_updated` branches were both `if entry: <broadcast>` with **no `else`**,
  and `complete_bg_task()` returns `None` for two unrelated reasons — a
  duplicate completion (fine to ignore) *and* a task that was never registered.
  The latter is real: the CLI keeps background tasks running across a
  `--continue`/`--resume`, so a bridge that attached afterwards never receives
  their `task_started`. Such a completion produced **no broadcast and no log
  line**, so the model would wake an idle session with no visible cause and
  nothing afterwards could say whether the notification had even arrived.
- **Fix:** both branches now call `_announce_bg_completion()`, which separates
  the two cases — duplicates stay silent but log it; an unregistered task is
  rendered from the notification's own fields. The bg-wait bell and the
  `bg-all-done` wakeup remain gated on a known entry, so an unregistered task
  can't fire a wakeup for work nobody tracked. `task_started`,
  `task_notification` and `task_updated` all log now, so a completion with no
  matching start line identifies the case directly.
- **Tests:** 6 in `tests/test_bg_completion_announce.py`, verified against
  three reintroductions (silent drop restored; wakeup ungated; dedupe removed).
  Each fails a distinct test.
- **Note on the 2026-08-15 interrupt case above:** it is *not* established that
  this hole is what hid the notification there — that path logged nothing at
  the time, so the log simply cannot say. It is now loggable.

## `/rename` reverted to the old title on every reload — FIXED (2026-08-15)

- **Reported:** "i keep doing `/rename OSc` in the project for
  `d:\visual studio projects\os` under `c:\users\inhahe\.claude-account-c`, but
  every time i load it later, it's `OS` again."
- **Root cause — we were losing an append race we can't win.** Claude Code keeps
  the title in memory (`currentSessionTitle`) and re-appends it at EOF from
  `reAppendSessionMetadata()` on **every compaction** and again on **resume**
  (`adoptResumedSessionFile`). It only notices a rename written by anyone else
  if that record is still inside the last **64 KiB** of the file when it next
  looks (`readFileTailSync` / `LITE_READ_BUF_SIZE = 65536`), and the resume path
  passes `skipTitleRefresh` so it doesn't look at all. orchestrator2 resolved
  titles as "last `custom-title` in the file wins", so the CLI's stale stamp
  always had the last word.
- **Evidence** (from the reporter's own 845 MB session
  `7baa01e0…jsonl`, dumped with a throwaway script):
  - 979 `custom-title` records: **933 `'OS'` vs 46 `'OSc'`**, recurring every
    ~700 KB–1.3 MB — the compaction cadence.
  - Neither the first nor the last 64 KiB of the file contains *any*
    `custom-title` record, so the CLI's external-writer refresh is a permanent
    no-op on a file this size.
  - Rename #912 at offset 794,639,953 → reverted by a compaction stamp at
    794,733,905, i.e. **94 KB later**, just outside the 64 KiB window.
  - Rename #932 at 811,000,930 landed <64 KiB before a compaction, was absorbed,
    and survived 45 stamps — then was reverted at 844,841,088 by a *resume*
    stamp (recognisable by having no `last-prompt` before it and a 6-hour gap).
- **Fix:** `session.py` now records a **rename pin** in
  `~/.orchestrator2_renames.json` (`{title, stale[], at}` keyed by JSONL path) and
  `_apply_rename_pin` overlays it on the raw scan. A JSONL title in `stale` is
  treated as the CLI re-stamping something the user already replaced; a title we
  have never seen is a genuine newer rename and drops the pin. See design.md §8a.
- **Also fixed here:** the manual-append fallback serialised with default
  `json.dumps` separators (`{"type": "custom-title", …}`), which the CLI's
  `startsWith('{"type":"custom-title"')` check could never match — so that path
  was invisible to the CLI even inside the 64 KiB window. Now compact.
- **Tests:** 6 in `tests/test_session_titles.py`; each verified to fail when its
  fix is reverted (3 on the pin overlay, 1 on write-through persistence, 1 on
  cross-session leakage, 1 on the separators).
- **Known limitation (documented, not a bug):** renaming *back* to a
  previously-used title from outside orchestrator2 looks identical to a stale
  re-stamp and will be overridden. Rename from orchestrator2 to escape.

## Idle tabs burned CPU forever — FIXED (2026-08-15)

- **Reported:** "my four open orchestrator2 tabs are taking the most cpu of all
  my chrome tabs by far, even though they're doing nothing at all atm."
- **Measured** with Playwright against the live hub (`tools/measure_tab_cpu.py`):
  one visible, *idle* tab cost **~2.7% of a core**, against 0.4% for
  `about:blank` in the same browser. Four tabs ≈ 10% of a core, permanently.
- **Root cause:** the 2s status ticker broadcast a `status_update` even when the
  snapshot was byte-identical, and several renderers wrote the DOM
  unconditionally on receipt. Each write forced a style recalc + layout +
  repaint of a **53,000-element** document. Main-thread JS was 0.02% of a core,
  so a JS profile showed the page as 99% idle — the cost was entirely in the
  paint/raster/composite path, which `Performance.getMetrics` cannot see.
- **Offenders, in order of size:** `_renderTodos` (no change guard at all — full
  `innerHTML` rebuild + `querySelector` + listener re-attach every 2s);
  `_renderTools`/`_renderBg` (guard placed *after* the empty-state `innerHTML`
  write); unconditional `textContent`/`title`/`style.color` assignments in
  `status.js`; and `Commands.setBusy`, whose `classList.add('hidden')` on an
  element that already had the class still re-set the whole `class` attribute.
- **Fix:** (a) `server._tick_runtime()` compares the serialised snapshot against
  `rt.last_status_sig` and sends only on a real change, with a 30s heartbeat;
  (b) `_setText`/`_setHTML` helpers, a signature guard on the todos panel,
  guards moved before the empty-state writes, and `setBusy` made idempotent in
  both `commands.js` and `panels.js`.
- **Result:** an idle tab now performs **zero DOM mutations**
  (`tools/probe_mutations.py`); measured cost fell to 1.5% with the old server
  still ticking, of which 0.7% is just Chrome holding the large DOM on screen.
- **Tests:** `tests/test_status_ticker.py` (7 tests; verified by reintroducing
  the unconditional broadcast — 5 fail).
- **Residual, not a bug:** ~0.7% of a core per tab is the static cost of the
  message DOM itself and scales with `--max-dom-messages` (default 2000,
  ~48 elements per message). Lowering it trades scrollback for idle cost.

## Interrupt sent the queued prompt but the turn died instantly — FIXED (2026-08-14)

- **Reported:** "i just had a queued prompt and then ctrl+c'd the turn and then
  it sent the queued prompt (or at least showed it as coming from me) but it
  didn't start working again, it stayed idle, i had to send the prompt again."
- **Regression from the 2026-08-07 parked-worker interrupt poke** — before that,
  Ctrl-C during a ghost turn did nothing at all, so there was no race to lose.
- **Root cause:** `interrupt()` queued the parked-worker wakeup *before*
  awaiting `client.interrupt()`. The worker drained the queue, echoed the
  prompt and entered `run_turn` (setting `turn_active`) while the CLI's
  wind-down was still in flight, so the dispatcher delivered the *previous*
  stream's terminator into the *new* turn. From `orchestrator2.log`:
  ```
  20:02:34 ghost turn begin: SDK streaming without active run_turn
  20:05:36,935 run_turn enter: turns=37 drained=0 ta.is_set=True
  20:05:36,972 UserMessage during turn: '[Request interrupted by user]'
  20:05:36,973 run_turn ResultMessage: error_during_execution elapsed=0.0s
  20:05:36,974 run_turn exit: normal (turns=38)
  ```
  A 40 ms turn, killed by a result belonging to the stream it replaced. The
  ghost turn's own `ghost turn end` never logged either — same message, eaten
  by the same turn.
- **Fix:** `SDKBridge._interrupt_settled` (`asyncio.Event`, set by default).
  `interrupt()` clears it only when interrupting a stream nobody is consuming
  (`state.busy` and not `turn_active`), issues `client.interrupt()` before the
  wakeup poke, and `run_turn` awaits it (5 s cap) *before* setting
  `turn_active` — so the terminator still reaches `_handle_async_message`,
  which closes the ghost turn and sets the event. Released again in
  `run_turn`'s `finally` so a stale clear can't outlive a turn.
- **Why wait rather than swallow the stray result:** waiting guarantees the
  prompt is sent to a quiesced CLI. Swallowing it after `query()` would leave
  us unable to tell whether that query had been discarded along with the
  interrupt — a hung turn instead of a dead one.
- **Tests:** `tests/test_queue_drain.py` —
  `test_interrupting_a_ghost_turn_holds_the_next_turn_until_it_winds_down`,
  `test_interrupting_an_idle_session_does_not_hold_anything_back`,
  `test_interrupting_a_live_turn_does_not_hold_anything_back`,
  `test_the_wind_down_result_releases_the_next_turn`,
  `test_a_starting_turn_waits_for_the_wind_down`,
  `test_the_wait_gives_up_rather_than_stalling_forever`.

## Auto-continue deleted entirely — REMOVED (2026-08-14)

- **Was:** `config.auto_continue` defaulted to `False` and nothing ever set it —
  no `--auto-continue` flag (argparse had only `--auto-compact` /
  `--auto-reconnect` / `--auto-shutdown`), no `/auto` command, no assignment to
  `state._auto_continue`. The single gate in `_await_next_prompt` was therefore
  unreachable, and it was the **only** site that returned
  `state.continue_prompt` — so `CONTINUE_PROMPT` never reached the model, and the
  model was never asked to emit `[WAITING]` / `[DONE]` at all. Meanwhile
  `README.md` and `design.md` described the loop as running every turn and
  checking sentinels and burst limits: prose for behaviour that could not happen,
  which is exactly how a reader ends up "fixing" a bug that isn't there. (The
  first version of this entry did precisely that — it diagnosed a missing
  sentinel scan inside a live loop. There was no live loop.)
- **Removed:** `CONTINUE_PROMPT`, `WAITING_SENTINEL` / `DONE_SENTINEL`,
  `CONTINUE_RESPONSE_DELAY_SECONDS` / `CONTINUE_BURST_LIMIT` /
  `CONTINUE_BURST_WINDOW_SECONDS` plus the four `Config` fields built on them,
  `Config.auto_continue`, `State.continue_prompt`, `SDKBridge._is_auto_turn`
  (write-only), the auto-resume gate in `_await_next_prompt`, and the
  `"waiting"` / `"done"` / `"burst"` branches in `state_to_status_dict`.
  `needs_user_attention` is now only ever `None` or `"api-error"`. `README.md`,
  `design.md`, and the stale code comments (the `config.py` bell-events note,
  `_between_turns`, and `_surface_api_error_loop`'s "Auto-continue has been
  paused" banner) were updated to match. Suite green: 155 passed.
- **This is already-known, acted-on design:** `config.py:117-120` records that
  the `'done'` / `'waiting'` / `'stalled'` bell events were *removed* because the
  loop is dead, along with `'api-stall'` ("it only existed to halt that dead
  auto-continue loop"). Every completed turn returns control to the user
  (`sdk_bridge.py:2738-2749`).
- **Not "broken if enabled" — just far narrower than documented.** Flipping
  `auto_continue` to `True` would *not* restore the documented per-turn loop: no
  such path exists. `_between_turns` returns `_await_next_prompt()` on turn
  completion (`sdk_bridge.py:2749`), and `state.continue_prompt` is returned from
  exactly one place in the whole file (`:2853-2855`). Enabling the flag would buy
  only a one-shot auto-resume on `bg-all-done` / `rate-limit reset` /
  `api-status-recovered`. That path works mechanically — being event-driven and
  one-shot, it can't hammer, which is why it never needed the burst knobs. Its
  one incoherence: `CONTINUE_PROMPT` still instructs the model to emit
  `[WAITING]`/`[DONE]`, and nothing reads them.
- **Deliberately KEPT — `session.py`'s prefix match** on "If you need input from
  me before continuing, pause and include". It classifies *replayed* history:
  old transcripts, and any from the single-file `Python Agent/orchestrator.py`
  (which does still implement auto-continue), contain that text, and dropping the
  match would re-render them as ordinary user bubbles instead of collapsed
  injected-prompt boxes. It is a literal string, so deleting `CONTINUE_PROMPT`
  did not break it. Do not "finish the cleanup" by removing it — the comment
  there says so too.
- **If it is ever revived:** the sentinel scan must run over *visible assistant
  text only* and must **not** include `ThinkingBlock` content — reasoning
  routinely discusses the sentinels by name, so scanning thinking would let a
  turn trip its own stop condition just by thinking about it. (Moot on models
  newer than opus-4-6, whose thinking text is returned encrypted and empty — see
  the encrypted-reasoning entry below — but live again on opus-4-6.)

## "Turn N completed" appeared *after* the next turn's prompt — FIXED (2026-08-12)

- **Reported:** "sometimes it says `Turn 102 completed (0:26:41) — success` not
  after the turn completes but after the prompt for the next turn. maybe only
  when the prompt was queued."
- **Root cause:** a turn that ends on a compaction deliberately does *not*
  broadcast `turn_end`. `run_turn` parks it in `state.pending_compact_turn_end`
  and arms a 10 s timer, so a post-compact ghost turn can claim the marker
  instead (`_claim_pending_compact_turn_end`). The last-resort backstop is
  `run_turn`'s own flush at the **start** of the following turn
  (`sdk_bridge.py`, top of `run_turn`) — which runs after `_between_turns` →
  `_pop_queued_prompt()` has already echoed the next prompt. Hence:
  1. turn N ends on a compaction → pending armed, no marker;
  2. `_pop_queued_prompt()` broadcasts `user_message` ← the prompt appears;
  3. next `run_turn` flushes ← "Turn 102 completed" appears *below* it.
- **Why "only when queued" was a correlation, not a rule:** a queued prompt
  starts the next turn immediately, always inside the 10 s grace, so it always
  loses the race to the timer. A hand-typed prompt usually takes longer than
  10 s, letting the timer win — but typing within 10 s reproduces the same
  inversion through `server.py::_enqueue_prompt`.
- **Fix:** new public `SDKBridge.flush_pending_turn_end()`, called before the
  echo at both echo sites — `_pop_queued_prompt()` (bridge) and
  `_enqueue_prompt()` (server). Pure reordering: `run_turn` would have flushed
  the same marker moments later, and flushing with nothing pending early-returns.
  A held-back pop (queue head being edited) doesn't flush — nothing is echoed
  there, so the ghost-turn claim keeps its chance at the marker.
- **Residue (cosmetic, unfixed):** when the browser echoes a prompt
  *optimistically* (`client_echoed`), the echo has already been painted
  client-side before the server sees the message, so the flushed marker still
  lands below it. Fixing that would need the frontend to insert `turn_end`
  above a trailing unconfirmed echo. Not worth the machinery: the deferred
  marker only exists in the seconds after a compaction.
- **Tests:** `tests/test_queue_drain.py`
  (`test_pop_closes_out_a_deferred_turn_end_before_echoing`,
  `test_pop_with_nothing_pending_emits_no_turn_end`,
  `test_held_back_pop_does_not_flush_the_turn_end`) and
  `tests/test_prompt_echo.py`
  (`test_enqueue_closes_out_a_deferred_turn_end_first`,
  `test_send_arrow_closes_out_a_deferred_turn_end_first`,
  `test_enqueue_flushes_even_when_the_client_echoed`,
  `test_enqueue_with_nothing_pending_emits_no_marker`).

## Prompts sent by the queue's send arrow never appeared as "you:" — FIXED (2026-08-07)

- **Reported:** after an interrupt failed to send a queued prompt, the user hit
  the green ▶ arrow next to it — "then it answered the prompt, but my prompt
  never showed up as a message from 'you:'." An answer under no question.
- **Root cause:** only *one* producer of `("message", …)` on the event queue
  echoes the prompt to the transcript — the browser's optimistic echo in
  `app.js send()`, which fires only when it believes the session is idle and
  reports itself via `client_echoed`. Every other producer echoed nowhere:
  `/api/queue/send` (the ▶ arrow), `/queue send` via `forward_payload`, and
  `/graphify` while idle. All three are REST calls or server-side syntheses.
  The consumer end (`_await_next_prompt`'s `kind == "message"` branch) never
  echoed either — it just returned the payload.
- **Why the existing flag didn't cover it:** `State.initial_prompt_client_echoed`
  was a *single slot* set only by the WebSocket handler and read only by
  `worker_loop`'s initial-prompt wait. It described the last message that
  handler happened to see, so it was wrong whenever two prompts were in flight,
  and the REST producers never set it at all.
- **Fix:** `server.py::_enqueue_prompt(rt, prompt, *, client_echoed)` — one
  choke point that echoes unless the client already did, then enqueues. All
  four producers routed through it; the `worker_loop` fallback echo and the
  `initial_prompt_client_echoed` field both deleted (the fallback would now
  double-echo). Prompts arriving via `queued_prompts` are still echoed by
  `_pop_queued_prompt`, so every path is covered exactly once.
- **Note the ordering:** the echo is awaited *before* the `put_nowait`, so the
  question reaches the transcript before the answer starts streaming into it.
- **Regression tests:** `tests/test_prompt_echo.py` (7). Verified by reverting
  the send-arrow call site — 2 of the 7 fail.

## Sent prompt stayed listed in the pending-prompt queue — FIXED (2026-08-07)

- **Reported:** "i entered a prompt while it was working, it queued it, then i
  ctrl-c interrupted it, it sent the prompt, but then the prompt was still also
  in the queue."
- **Confirmed in the log** (`orchestrator2.log`, bridge `d400`, session
  forward-raytracer): turn `'add the fur creature from fur_creature_gi.png…'`
  starts 22:57:37; `[Request interrupted by user]` at 23:15:37,474; queued
  prompt `'sorry, i toldy ou the wrong gallery'` starts 23:15:38,669. Sent
  exactly once — so the model never saw a duplicate; the duplication was
  purely what the panel showed.
- **Root cause 1 (the visible one):** the queue panel renders from a snapshot
  (`queue_update`, or the `panels` blob on `status_update`). `_between_turns`
  popped the prompt and echoed a `user_message` but **never published a new
  snapshot**, so the row stayed on screen — running in the transcript and
  pending in the panel at the same time — until the 2 s status ticker in
  `server.py` happened to overwrite it. That self-healing 2 s window is why it
  survived so long: it looked like a glitch, not a bug. The initial-prompt path
  *did* publish one, which is what showed the omission was accidental.
- **Root cause 2 (latent, worse):** `_between_turns` returned
  `await self._await_next_prompt()` from the `if interrupted:` branch **before**
  the queued-prompt drain. Nothing pokes the worker after an interrupt, so on
  that path the queued prompt sat unsent indefinitely. Which symptom you got
  was a race — `interrupted` is only True when `run_turn` notices the interrupt
  before the SDK's `ResultMessage`, and on a short turn the `ResultMessage`
  wins (that's why this incident took the working path). Same keypress, two
  behaviours.
- **Root cause 3 (found next door):** the `btw` branch of `_await_next_prompt`
  did `queued_prompts.append(payload); return queued_prompts.popleft()` — a
  no-op only when the queue is empty. With anything already pending it sent
  *that* prompt and left the `/btw` text behind.
- **Fix:** one `_pop_queued_prompt()` helper that all four drain sites funnel
  through (initial-prompt wait, `_between_turns`, the `wakeup` branch, and the
  interrupt branch), doing dequeue + transcript echo + `queue_update` together;
  a `_broadcast_queue()` twin of the server's `_broadcast_queue_update`; the
  interrupt branch now drains before parking (and still returns immediately, so
  an interrupted turn can't reach auto-continue/compaction); `/btw` returns its
  own payload.
- **Root cause 4 (the parked-worker case, found by follow-up question "shouldn't
  it send the prompt if i interrupt it and it goes to bg-wait?"):**
  `interrupt()` only poked `turn_msg_queue` when `turn_active` was set, and
  `turn_active` is set *only* by `run_turn`. While a **ghost turn** streams (a
  background task producing output) `state.busy` is True — so server.py routes
  typed prompts into `queued_prompts` — but the worker is parked in
  `_await_next_prompt` and `_between_turns` never runs. Ctrl-C in that window
  did nothing whatsoever: no sentinel (no live turn), no wakeup, no drain.
- **Fix:** `interrupt()` now has two mutually exclusive pokes — sentinel when a
  turn is live, `("wakeup", "interrupt")` on the event queue when parked. The
  wakeup branch drains the queue; on an empty queue it falls through to the
  auto-resume check, which ignores an `"interrupt"` payload, so it can't
  conjure a turn.
- **Regression tests:** `tests/test_queue_drain.py` (14). Verified by
  reintroducing each bug in turn — 5 of 14 fail without the drain/broadcast
  fixes, 1 without the parked-worker poke — and the interrupt cases fail on a
  bounded `wait_for` rather than hanging the suite.

## Background tasks registered with a blank name — FIXED (2026-08-07)

- **Was:** every background task in the bg panel showed an unlabelled row.
- **Root cause:** the `task_started` branch read
  `getattr(msg, "name", None) or ""`, but the SDK's `TaskStartedMessage` has
  no `name` field — the human-readable label is **`description`**. `getattr`
  with a default swallowed the mismatch, so it failed silently and forever.
  Verified against the live SDK:
  `TaskStartedMessage -> [subtype, data, task_id, description, uuid,
  session_id, tool_use_id, task_type]`.
- **Fix:** read `description`, falling back to `name` in case a future SDK
  renames it back. `tests/test_bg_task_fields.py` pins the SDK field names so
  the next rename fails loudly instead of blanking the panel again.
- **Found while investigating a false alarm** (see below) — worth noting that
  the reported symptom was not itself a bug.

## `TaskOutput` events are not background-task completions — NOT A BUG (2026-08-07)

- **Reported:** two `TaskOutput` events appeared, one rendered under *tools*,
  and two background tasks stayed listed as running — suspicion was that
  `TaskOutput` signals a task *finishing* and orchestrator2 was misreading it.
- **Actually:** `TaskOutput` is a genuine tool the model calls to poll or block
  on a background task's output. From the session JSONL (`Good Photons`,
  `8d532b0a`): `tool_use {"name":"TaskOutput","input":{"task_id":"bmhmsjzig",
  "block":true,"timeout":600000}}`. Rendering it under tools is correct.
- **And the tasks really were still running** — the tool results say so
  outright: `<retrieval_status>not_ready</retrieval_status> … <status>running
  </status>`, then a second call returning `<retrieval_status>timeout</…>` with
  `<status>running</status>` after blocking the full 600 s. The panel was
  right. The reason only one of the two showed as a completed tool row is that
  the other was still in flight — a blocking `TaskOutput` occupies the active
  tools panel for up to ten minutes.
- **Keep in mind for future reports:** completion arrives as a `task_notification`
  (or a `task_updated` patch with a terminal status), never as `TaskOutput`.
  The SDK models only `task_started`, `task_progress`, `task_notification`;
  `task_updated` / `task_output` reach the handler as generic `SystemMessage`s
  via the parser's `case _` fallback, so those branches are forward-compat for
  CLI-level subtypes, **not** dead code — don't delete them.

## `--show-thinking` was dead config — FIXED (2026-08-06)

- **Was:** the flag was parsed (`config.py`), copied onto `State`
  (`state.py:553,597`) — and read by nothing. Passing `--show-thinking` did
  exactly nothing, silently, and had for a long time. Its help text
  ("Print full thinking blocks (default: collapsed snippet)") described
  behaviour that did not exist.
- **Root cause:** the chain `Config → State → status dict → WebSocket →
  chat.js` was only built for its first two hops. Nothing serialised the value
  and nothing on the frontend consumed it.
- **Fix — completed the chain, as a display gate rather than a content gate.**
  The considered alternative was gating the *emitters* (the two `sdk_bridge`
  `ThinkingBlock` branches + the `session.py` replay branch) so thinking text
  wasn't sent unless the flag was on. Rejected: the frontend already renders
  thinking as a collapsed, click-to-expand block, so suppressing the text would
  have *removed* a working feature to implement a flag. Instead
  `show_thinking` now rides in `state_to_status_dict()`, `status.js` pushes it
  to `Chat.setShowThinking()`, and `_addThinking()` starts the block open. Full
  text is always sent; the flag only picks the initial state.
- **Also added:** `/show-thinking [on|off]` (immediate command, mirrors
  `/collapse`) so it's changeable at runtime, plus tab-completion. Kept
  deliberately distinct from `/thinking`, which is API-level and forces a
  reconnect — conflating them would mean a display toggle silently disabling
  reasoning.
- **Ordering constraint worth remembering:** `_send_initial_state()` sends
  `status_update` before `history`. Replayed thinking blocks go through the
  same `_addThinking()`, so the reverse order would make the flag silently
  fail on resume.
- **Tests:** `tests/test_show_thinking.py` (10) pins every hop, including
  grepping `status.js`/`chat.js` for the setter and the `if (_showThinking)`
  gate — a string check, but that missing wire is exactly how this became dead
  config. Suite 111 → 121.

## Dead sessions leak their MCP servers under a live hub — FIXED (2026-08-06)

- **Was:** found while sweeping for orphans after the `claude.exe` fix below —
  three parentless process stacks on the machine: two `npx ue-mcp` (Unreal MCP,
  started 08-04 13:45 and 08-06 00:07) and one `bash --norc p6.sh`. The 00:07
  one predates the then-current hub (started 00:32:42), so its parent was an
  earlier, killed server generation.
- **Root cause:** the same Windows "no process-tree teardown" rule, one level
  further down. Real shape, measured on the live hub — each stdio MCP server is
  a five-deep stack under the CLI:

  ```
  python server.py
    +- claude.exe
         +- cmd.exe -> node npx-cli -> cmd.exe -> node ue-mcp
         +- cmd.exe -> node npx-cli -> cmd.exe -> node photoshop-mcp
  ```

  The SDK stops the CLI with `terminate()`/`kill()` — both `TerminateProcess`
  on Windows — which kills only `claude.exe` itself and orphans that entire
  stack. The server's job object *does* eventually collect them, but not until
  the **server** exits, so a long-lived hub cycling through sessions (idle
  teardown, `/cwd`, reconnects, crashes) accumulates a full MCP stack per dead
  session.
- **Fix:** `SDKBridge.disconnect()` — the single choke point all teardown paths
  funnel through (`reconnect()`, `stop()`, the connect-error path) — now calls
  `proc_guard.snapshot_descendants(cli_pid)` *before* stopping the client and
  `proc_guard.reap_descendants()` after. Killing these is unambiguously safe: a
  stdio MCP server speaks to exactly one client over the pipes it was born
  with, so once that client is dead it can never serve anyone again. Remote
  HTTP/SSE MCP servers are not our children and are untouched.
- **Rejected design — nested job objects.** Giving each `claude.exe` its own
  nested job looks like the natural extension of the fix below, but it cannot
  work, and the measurement is worth keeping so nobody retries it: **job
  membership is inherited only at process creation.** Assigning an
  already-running process to a job does not retroactively capture the children
  it already spawned. Measured both ways — assign-then-spawn reaps the
  grandchild; spawn-then-assign leaves it running. Since the CLI spawns its MCP
  servers during its own init, we could only ever assign too late. (En route,
  `AssignProcessToJobObject` also returned `ACCESS_DENIED` until the
  `OpenProcess` mask included `PROCESS_SET_QUOTA | PROCESS_TERMINATE`;
  `PROCESS_SET_INFORMATION` is not enough. Nesting itself works fine.)
- **Why a pid list is sufficient:** the only case where no cleanup code can run
  is the *server* dying, and the job object already covers that. Whenever a
  session is torn down, the server is by definition alive.
- **Two subtleties in the implementation:** the snapshot must be taken while
  the CLI is alive (orphaning destroys the parent links needed to walk the
  tree), and each entry records a creation time because Windows recycles pids —
  the window between snapshot and reap is exactly when the CLI's pid becomes
  available, so killing on a bare pid could shoot an unrelated process.
- **Fragility guarded:** `_cli_pid()` reads SDK-private
  `client._transport._process.pid` and fails soft to `None`. Since a silent
  `None` would quietly restore the leak,
  `test_sdk_still_exposes_the_cli_process_we_reach_for` asserts those
  attributes still exist.
- **Tests:** 6 new in `tests/test_proc_guard.py`, including one that reproduces
  the leak for real (spawn a CLI-like process with a child, kill the parent,
  assert the child *survives*, then reap it) and one asserting a recycled pid
  is spared. Suite 105 → 111.

## Killed servers orphan their `claude.exe`, giving two agents one repo — FIXED (2026-08-06)

- **Was:** after killing all Python processes and starting one session back up,
  the agent in that session reported — correctly — that *another live agent* was
  working the same repo: HEAD advancing while it ran only read-only commands,
  a source file growing 96 → 165 insertions over 40 s with no edits of its own,
  and a corpus case appearing for an issue logged minutes earlier. The Sessions
  lobby showed only one instance of that project.
- **Root cause:** on Windows, killing a process does **not** kill its children —
  there is no automatic process-tree teardown. The SDK spawns `claude.exe` as a
  child, so every orchestrator2 server that died left its `claude.exe` children
  running, still resumed on their sessions, still editing files and committing.
  They belong to no `SessionRuntime`, so the lobby can't see them — hence "only
  one instance" while two were running.

  Measured on the live machine: `claude.exe` PID 34480,
  `--resume 7baa01e0…` (the `os` project), parent python PID 39380 **gone**.
  Started 8/2, still alive 8/6 — **four days**, 15,814 s of CPU (4.4 h), 1.9 GB
  RSS, and it spawned a fresh `bash.exe` *while the diagnosis was running*.
  Meanwhile PID 80900 was a second `claude.exe --resume 7baa01e0…` under the
  current server. Two agents, one 548 MB session JSONL, one working tree.
  (The three processes that started together at 00:33 were a red herring —
  three different projects, one per tab, all legitimate.)
- **Fix — `proc_guard.py`, two independent nets:**
  1. **Job object.** `install_process_reaper()` puts the server process in a
     Windows Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. Children
     inherit job membership, so when the server dies *by any means* — clean
     exit, crash, `taskkill /F` — the kernel terminates every `claude.exe`
     under it. This is the only mechanism that survives a hard kill, because
     no shutdown handler runs for one. Installed at the top of `lifespan`, i.e.
     in the serving process only and before anything can spawn.
  2. **Duplicate-session guard.** `find_foreign_claude_for_session()` looks for
     a `claude` process already resuming the same session id *outside our own
     process tree*; `sdk_bridge.connect()` raises `DuplicateSessionError`
     rather than becoming the second writer. This catches orphans that predate
     the reaper, a second hub, or a user's own `claude --resume` in a terminal.
     The error names the PID and the exact `taskkill` command. Override with
     `--allow-duplicate-session`.
- **Restart interaction (important):** the job would otherwise kill the ⟳
  Restart replacement server, since it's spawned as a child. The job sets
  `JOB_OBJECT_LIMIT_BREAKAWAY_OK` and `_restart_server` adds
  `CREATE_BREAKAWAY_FROM_JOB` (via `proc_guard.breakaway_flags()`, which is 0
  when no reaper installed, so it's safe to add unconditionally). The
  `--detach` parent and the picker console deliberately don't install a reaper.
- **Verified, not assumed:** `tests/test_proc_guard.py` spawns a real parent
  that installs the reaper plus two children, then `taskkill /F /PID <parent>`
  **without `/T`** — the in-job child is reaped, the breakaway child survives.
  Also confirmed end-to-end against a real server: log shows `process reaper:
  active`, and hard-killing that server's PID alone took its `claude.exe` with
  it.
- **Files:** `proc_guard.py` (new), `server.py` (lifespan install + restart
  breakaway), `sdk_bridge.py` (`connect` guard, non-retryable in the connect
  loop), `config.py` (`--allow-duplicate-session`), `requirements.txt`
  (`psutil`).
- **Note:** existing orphans from *before* this fix are not cleaned up
  automatically — the guard will refuse to connect and tell you which PID to
  kill.

## A real model (`claude-fable-5`) missing from `/model` — FIXED (2026-08-05)

- **Was:** `/model` listed only a short, out-of-date set of models.
  `claude-fable-5` had shown up there before and definitely exists —
  `/model claude-fable-5` selected it fine — but it stopped being listed.
- **Root cause:** two failures compounding.
  1. The list comes from Anthropic `/v1/models`, cached in-process because
     `/model` runs on the event loop and must never block on the network. That
     cache was filled by a **single fire-and-forget fetch at startup**
     (`server.py`, task `warm-model-cache`), and `fetch_available_models()`
     swallowed *every* failure with a bare `except Exception: return None` —
     no log line. So one transient failure at startup (an OAuth access token
     that happened to be expired right then → 401, or a network blip) pinned
     that server process to the hardcoded fallback **for its entire lifetime**,
     with nothing anywhere saying why.
  2. The fallback, `KNOWN_MODELS`, was stale: opus-4-8 / opus-4-6 /
     sonnet-4-6 / haiku-3-5 — no fable-5, opus-5 or sonnet-5.

  It looked like the model didn't exist, while typing the id worked, because
  `/model <id>` never validates against the list.
- **Verified, not assumed:** calling `fetch_available_models()` directly
  returns 10 models including `claude-fable-5` — so the API was fine and the
  running process's cache was the problem.
- **Fix:**
  - `config.py`: every failure path now logs *why* (HTTP status, with a
    "token expired? re-login with /login" hint on 401/403; network error;
    unexpected response shape; missing credentials).
  - `config.py`: added `_model_cache_at`, `MODEL_CACHE_TTL` (1 h) and
    `model_cache_is_stale()`.
  - `server.py`: the one-shot warm became `_model_cache_loop()` — retry with
    exponential backoff (30 s → 10 min) while failing, re-fetch every TTL once
    working (so a hub left up for days picks up newly released models),
    cancelled at shutdown with the same `asyncio.wait` pattern as the ticker.
  - `KNOWN_MODELS` refreshed (opus-5, sonnet-5, fable-5, opus-4-7, …).
  - `/model` now reports `live: false` when serving the fallback, and
    `chat.js` renders a note saying the list may be out of date and that any
    id still works when typed. A silent fallback is what made this invisible.
- **Files:** `config.py`, `server.py`, `commands.py` (`_cmd_model_show`),
  `static/chat.js`, `static/styles.css`.
- **Tests:** `tests/test_model_list.py` (+6) — caching, staleness before first
  fetch, TTL expiry, a 401 not wiping a good cache, network-error fallback, and
  a guard that `KNOWN_MODELS` contains the current models.

## Tapping a session in the lobby does nothing on mobile — FIXED (2026-08-05)

- **Was:** on Chrome for Android (and iOS Safari), tapping a session card in the
  `☰ Sessions` overlay never switched sessions. The lobby just stayed open and
  kept live-refreshing that card every ~2 s (the `lobby_watch` push), so the tap
  looked completely ignored.
- **Root cause:** `static/lobby.js` was desktop-only in how it opened a session.
  Every click path went through `_focusOrOpen()` / `_openInTab()`, which relies
  on two things mobile browsers don't provide:
  1. `window.focus()` cannot raise another tab to the foreground on Android
     Chrome / iOS Safari — it's a silent no-op there.
  2. `window.open('', name)` only reuses a named window inside the same
     browsing-context group. A tab the user opened separately isn't related to
     this one, so the lookup misses and the browser spawns a *new blank tab*
     instead of re-using the session's tab.

  Compounding it: a phone typically has a single tab in *landing* mode (no
  session bound), so `_currentRid` is `null` and the "this session is already
  what you're viewing — just close the lobby" short-circuit never fired either.
  Net effect: a stray blank tab in the background, and the phone still parked on
  the lobby.
- **Fix:** detect mobile (`navigator.userAgentData.mobile`, falling back to a UA
  regex) and, on mobile, switch **in place over the existing WebSocket** rather
  than trying to juggle tabs. The server already spoke this protocol — the
  lobby just wasn't using it:
  - running card → `attach {rid}`
  - recent item → `open {session_id, cwd, account}`
  - new-session button → `new {cwd}`

  The server replies `attached {session}`, and `app.js` rewrites the URL to
  `?rid=<rid>` + clears the chat, so a later refresh re-attaches instead of
  forking the session. `openHub()` also just opens the overlay on mobile instead
  of spawning a hub tab.
- **Also fixed (feedback):** the tap used to be silent while the switch was in
  flight. `_setBusyNotice('Opening session…')` now shows immediately, is cleared
  by `onAttached`, and has a 12 s watchdog that turns into "That session didn't
  open — it may have stopped running." `showNotice()` clears the watchdog first
  so a genuine server notice can't be clobbered by it later. And the server's
  `attach`-to-a-dead-rid path now sends a `lobby_notice` in addition to the
  `system_msg` — on mobile the overlay covers the chat, so a `system_msg` alone
  would have been invisible.
- **Files:** `static/lobby.js` (`_isMobile`, `_setBusyNotice`,
  `_clearBusyNotice`, `_attachInPlace`, `_openInPlace`, the three click
  handlers, `openHub`, `onAttached`, `showNotice`);
  `server.py` `_handle_lobby_message` (`attach` failure branch).
- **Note:** picking this up needs a `python server.py` restart *and* a hard
  refresh on the phone (`lobby.js` is cached).

## No thinking blocks shown for models newer than opus-4-6 — FIXED (2026-07-27)

- **Was:** `/thinking` was on and the models were reasoning, but orchestrator2
  displayed no thought blocks at all when running `claude-opus-4-8` or
  `claude-opus-5` (the launcher's `--model`). opus-4-6 showed them fine.
- **Root cause (measured, not guessed):** counting content blocks straight out
  of the live session JSONL:

  | model | thinking blocks | empty text | median chars |
  |---|---|---|---|
  | `claude-opus-4-6` | 111 | 0 | 1162 |
  | `claude-opus-4-8` | 1242 | **1242 (all)** | 0 |
  | `claude-opus-5` | 27 | **27 (all)** | 0 |

  Models newer than 4-6 return **encrypted reasoning**: the `thinking` block
  carries a `signature` but an empty `thinking` string. Confirmed against the
  bundled CLI directly — with `--include-partial-messages` the stream shows
  `content_block_start{type:thinking}` → a single `signature_delta` →
  `content_block_stop`, with **zero `thinking_delta` events**, so the text is
  never sent to *any* client. `--thinking-display summarized` doesn't change it.
  Both of orchestrator2's handlers then did `if thinking_text.strip():` and
  dropped the block outright, so the user saw no sign the model had reasoned.
- **Fix:** treat "empty text + signature" as a real (encrypted) thinking block
  instead of discarding it — `sdk_bridge.run_turn()` and `_message_dispatcher`
  both broadcast `{"type":"thinking","content":"","encrypted":true}`;
  `session.render_session_history` emits the same for replayed transcripts so
  scrollback matches the live view; `chat.js _addThinking` renders a
  non-expandable `💭 Thinking (hidden by model)` row (styled via
  `.msg-thinking-encrypted`); `tool_manager.register_thinking(..., encrypted=)`
  records the flag so `/show #k<seq>` says
  "(reasoning returned encrypted by the model — no text available)" rather than
  showing blank content.
- **Not a bug:** the *content* genuinely cannot be recovered for these models —
  this is an API-side property, not a client limitation. Also note adaptive
  thinking means even 4-6 skips thinking entirely on easy prompts.

## Server burns 100% of one CPU core indefinitely (anyio cancellation spin) — FIXED IN SOURCE (2026-07-26; a running server must be restarted to pick it up)

- **Observed:** a long-lived `server.py` process (PID 17172, started 7/24 05:40, flags
  `--cwd "D:\visual studio projects\forward raytracer" --open --effort max --model
  claude-opus-4-8 --bell turn-done bg-done rate-hit --no-auto-shutdown`) had accumulated
  **60.8 CPU-hours over ~63 wall-clock hours**, i.e. one full core pinned continuously. A
  direct probe on an otherwise idle machine measured **101.4% of one core** (8.11 CPU-s
  over 8 s). The UI was responsive throughout — the spin is invisible from the browser,
  which is why it went unnoticed for days.
- **Why it matters beyond waste:** it silently steals a core from everything else on the
  machine. It corrupted raytracer benchmark measurements twice (the same 4 M-photon mode-M
  render measured 351 s clean vs 813–918 s while contending), and cost several wasted
  render hours chasing "machine throughput drift" that was actually this.
- **Root cause (identified, sampled with py-spy — already installed at
  `D:\python314\Scripts\py-spy.exe`):** the event loop is stuck in anyio's cancellation
  delivery retry loop. Repeated `py-spy dump --pid <pid>` samples show the one active
  thread alternating between a **zero-timeout** `select` (`asyncio\windows_events.py:446`
  via `base_events.py:2018`) and
  `_deliver_cancellation (anyio\_backends\_asyncio.py:611)`. That line is the self-re-arm:

      if should_retry:
          self._cancel_handle = get_running_loop().call_soon(
              self._deliver_cancellation, origin)

  `should_retry` is set `True` for **every** task still in `TaskGroup._tasks`, so once a
  cancel scope has been cancelled and one of its child tasks never finishes exiting, anyio
  reschedules the delivery callback on *every* loop iteration forever. A permanently-ready
  callback means the loop never sleeps in `select`, hence exactly 100% of one core.
- **Narrowing it down from the live process:** `py-spy dump --locals` on the spinning frame
  gave `should_retry=True`, `current=None`, `task=<_asyncio.Task ...>`, **`waiter=None`**.
  `waiter` is `task._fut_waiter`, so the surviving task is **not** parked on a future — it
  is runnable, and anyio therefore calls `task.cancel()` on it again on every single loop
  iteration. That rules out the "blocked in an un-cancellable worker thread" theory (all
  other threads sampled idle: `concurrent.futures` `_worker`s plus anyio's `WorkerThread`
  on `queue.get`) and points squarely at a task that **swallows its own `CancelledError`
  and keeps going**.
- **Prime suspects — three sites that catch their own cancellation:** `sdk_bridge.py:411`
  (`connect`, awaiting an orphan dispatcher), `sdk_bridge.py:494` (`disconnect`, awaiting
  `self._dispatcher_task`) and `sdk_bridge.py:2820` (`stop`, awaiting `self._worker_task`)
  all do:

      try:
          await other_task
      except (asyncio.CancelledError, Exception):
          pass

  That `except` is meant to absorb the *awaited task's* cancellation, but it also absorbs a
  `CancelledError` delivered to **the awaiting task itself** — so if the enclosing scope is
  cancelled here, the coroutine swallows it and carries on to its next `await` (in
  `disconnect`: `await self.client.disconnect()`, itself wrapped in `except Exception`).
  Re-cancelled at that await on the next loop iteration, it never gets to finish, never
  leaves `TaskGroup._tasks`, and anyio retries forever. Proper fix: separate the two cases
  everywhere this pattern appears — `except asyncio.CancelledError: raise if it's ours`, or
  more simply use `await asyncio.wait([other_task])` / `contextlib.suppress` around a
  shielded await so only the *awaitee's* cancellation is absorbed:

      try:
          await asyncio.shield(other_task)      # or: await asyncio.wait({other_task})
      except asyncio.CancelledError:
          if not other_task.cancelled():
              raise                              # our own cancellation — propagate
      except Exception:
          pass

  (`ws_channel.py` was audited too — its writer tasks are stopped with a sentinel, never
  cancelled, so it never had this shape.)
- **Fix as applied:** new `sdk_bridge.cancel_and_join(task, what)` — `task.cancel()` then
  `await asyncio.wait({task})`, which *reports* the awaitee's outcome instead of re-raising
  it, so the awaitee's cancellation/exception is absorbed while a cancellation aimed at us
  propagates normally out of the `await`. Its docstring carries the full rationale. All
  three trap sites now call it (`connect` orphan-dispatcher kill, `disconnect`, `stop`), and
  `server.py`'s lifespan-shutdown `_ticker_task` join uses the same `asyncio.wait` idiom
  inline (avoids importing `sdk_bridge` at shutdown when no bridge ever ran).
- **Regression test:** `tests/test_cancel_and_join.py` pins the two-rule contract — the
  awaitee's cancellation *and* a real exception it died of are absorbed; a cancellation
  aimed at the caller propagates. A fourth test asserts the *old* naive pattern still does
  swallow the caller's cancellation, so the third test can't silently stop discriminating if
  asyncio semantics change.
- **Still worth adding (not done):** the idle-CPU self-check described above. Nothing in the
  product would have reported this spin; it was found only because it was stealing a core
  from unrelated benchmarks.
- **How to pin it down next time (before restarting the process — a restart destroys the
  evidence):** `py-spy dump --pid <pid> --locals` to see `_deliver_cancellation`'s `origin`
  scope and the surviving `self._tasks`; that names the stuck coroutine directly. Failing
  that, add a debug endpoint dumping `asyncio.all_tasks()` with `task.get_coro()` so a
  wedged task group can be identified from the browser.
- **Detection worth adding regardless:** the server already tracks its own state for the
  toolbar — sample `process_time()` vs wall clock periodically and log/surface a warning
  when idle CPU usage stays near a full core. A spin that the UI can't show is a spin
  nobody reports.

## Immediate commands (`/help`, `/cls`) hang for a long time during a turn / bg-wait — FIXED (2026-07-23)

- **Was:** Typing `/help` mid-turn, or `/cls` while a session was in the
  `bg-wait` state, appeared to do nothing for a long time before the command
  "finally" ran. The command classification/dispatch itself was already
  synchronous and fast (`try_immediate_command` runs before the busy-queue
  check); the delay was in *delivery*. `broadcast` / `send_to` / `rt.broadcast`
  each did `await ws.send_text(...)` **inline on the event loop**, per client.
  During a turn (or while a background task streams output), high-volume
  `status_update` / `panel_update` snapshots flood a client whose socket drains
  slowly (a tablet on a weak link, or just a browser tab busy rendering). TCP
  backpressure then made every awaiting send suspend, the write buffer filled
  with a backlog of stale snapshots, and the response to the immediate command
  (the `/help` modal payload, the `/cls` `clear_screen`) queued *behind* that
  backlog. Worse, when an immediate-command handler awaited its own `send_to`
  behind the congested buffer, the per-connection WebSocket **receive loop**
  blocked too, so even the next command couldn't be read until the buffer
  drained.
- **Fix — per-client outbound channel (`ws_channel.py`):** every socket now gets
  a `ClientChannel` (registered at `ws.accept()`, torn down on disconnect) with
  its own outbound `deque` drained by a dedicated writer task. `broadcast`,
  `send_to`, `rt.broadcast` and `lobby_broadcast` now call `ws_channel.send(ws,
  msg)` — a **non-blocking enqueue** — so no fan-out and no receive loop ever
  waits on a slow client. The writer task owns the only `await ws.send_text`.
  Snapshot message types (`status_update`, `panel_update`, `queue_update`,
  `session_list`) **coalesce in place** while queued (newest content wins,
  position preserved), so a client that has fallen behind never accumulates a
  long backlog of stale snapshots — real ordered content (chat text, tool
  output, `clear_screen`, `history`, the `/help` modal, bells) flows out right
  behind at most one pending snapshot of each type. Shutdown does a best-effort
  `ws_channel.drain_all(1.0)` after broadcasting `server_shutdown` so the
  goodbye frame isn't lost to the async enqueue.
- **Tests:** `tests/test_ws_channel.py` (5 tests) — snapshot coalescing while
  the writer is stalled, non-blocking enqueue under a blocked client, ordered
  content preserved, send-after-close ignored, unregistered-socket fallback.

## Lobby "loading sessions…" slow on first connect after restart — FIXED (2026-07-22)

- **Was:** The first tab to open the lobby after a server restart stalled for
  several seconds on "loading sessions…". Cause: `_recent_disk_sessions` scans
  every Claude config dir's JSONL sessions (head+tail read per file). The
  per-file parse is cached by `(mtime, size)` in `_session_info_cache`, but that
  cache is empty right after a restart, so the first scan pays a full cold read —
  and `_enter_lobby` *awaited* that scan before painting the lobby, so the tab
  showed only the spinner until it finished.
- **Fix (three parts):**
  1. **Startup warm** (`server.lifespan`) — a background `asyncio.to_thread(
     _recent_disk_sessions)` task primes `_session_info_cache` right after the
     server starts accepting connections, so the cache is usually hot before the
     first tab connects.
  2. **Two-phase landing** (`server._enter_lobby` + `_session_list_payload(
     include_recent=False)`) — the lobby is painted immediately with the running
     sessions (in-memory, instant) plus `recent_pending: True`; the full list
     (with the disk-scanned recent sessions) is pushed from a background task
     once the scan finishes. The landing no longer blocks on the disk scan.
  3. **Frontend** (`static/lobby.js` `render`) — while `recent_pending` is set,
     it renders the running list but keeps the "loading sessions…" spinner up
     (instead of flashing an empty "recent" placeholder), then fills recent in
     when the full list arrives. Landing/visibility logic was factored into
     `_finishRenderLanding` so both phases share it.
- **Note:** the LAN/loopback auth bypass (a tablet on the same router hitting the
  public hostname arrives with a private source IP via NAT hairpin, so it skips
  the external password) is **working as intended** — not a bug. Watch out only
  if a reverse proxy is ever put in front: every external client would then look
  like `127.0.0.1` and skip the password (no `X-Forwarded-For` handling).

## Stale "rate limited" status after re-login / mid-turn — FIXED (2026-07-22)

- **Was:** After hitting a rate limit, the user logged out and back in (often to
  a different account), sent a prompt, and the turn streamed output — but the
  status bar stayed on **"rate limited"** for a while before flipping to
  "working". Cause: `state_to_status_dict` gives the rate-limit label precedence
  over `busy`, and the "rejected" state (`rate_limit_status` + a future
  `rate_limit_resets_at`) only cleared when a `RateLimitEvent`/`ResultMessage`
  carrying `status="allowed"` arrived *partway through the turn*. Nothing cleared
  the previous account's stale rejection promptly.
- **Fix (two spots):**
  1. `sdk_bridge.run_turn` — the first `AssistantMessage` of a turn proves the
     API accepted the request, so if `rate_limit_status == "rejected"` it's
     cleared right then (→ `allowed`, `resets_at=None`) and a status refresh is
     broadcast. Status now flips to "working" the instant content streams.
  2. `server._watch_login` — on detected sign-in completion, do a **full**
     rate-limit reset via `state.reset_rate_limit()` (not just the "rejected"
     lockout, but also the per-window `rate_limit_utils` utilisation percentages
     that drive the status bar's usage display). A re-login usually switches to
     a *different* account, so the previous account's usage numbers are stale
     too; fixes the window *after* `/login` + `/connect` but *before* the next
     prompt.
  3. **Sibling propagation** (`server._propagate_login_to_siblings`) — a
     `/login` rewrites the *shared* `<config-dir>/.credentials.json`, so every
     other live session on the same config dir is re-authenticated to the same
     account. On login completion the fix now pushes the refreshed `account`,
     cleared `auth_error`, and a full `reset_rate_limit()` to all sibling
     runtimes sharing that config dir (matched via
     `config_dir_path(...).resolve()`), so re-logging in *one* tab fixes them
     all — the user no longer has to repeat `/login` per session.
- **`reset_rate_limit` vs. stale-only clear:** the earlier
  `_clear_stale_rate_limit` helper only cleared when `rate_limit_status ==
  "rejected"`, so after switching accounts the old account's utilisation
  percentages could linger in the bar. It was replaced by `state.reset_rate_limit`
  (unconditional full wipe) on the account-switch paths. The mid-turn clear (spot
  1) still only clears an active rejection, since live utilisation there still
  describes the current account.

## `/model` (and `/effort`, `/thinking`) silently dropped before the first turn — FIXED (2026-07-22)

- **Was:** On a brand-new session with no turn started yet, `worker_loop` parks
  in its *initial-prompt* wait loop, which only recognised `message` and
  `quit`/`force-quit` events — every other kind was explicitly dropped ("Other
  kinds: drop and wait again"). So `/model <name>` (also `/effort`, `/thinking`,
  `/connect`) issued before the first prompt did **nothing**. The same commands
  worked once parked *between* turns because `_await_next_prompt` handled them.
- **Fix:** factored the idle config-change handling into
  `SDKBridge._apply_idle_config_command(kind, payload)` (sets state, tells the
  user, reconnects so the change takes effect) and called it from **both** idle
  wait points — the initial-prompt loop and `_await_next_prompt` — so a config
  command is applied the same way regardless of which one is parked. Side
  benefit: changing model/effort/thinking while parked between turns now also
  emits a `model → …` / `effort → …` / `thinking → …` confirmation (previously
  silent on that path).

## External-password brute-force protection — FIXED (2026-07-21)

- **Was:** `_ExternalAuthMiddleware` (server.py) had **no** attempt tracking — a
  public-IP attacker could guess the password thousands of times per second with
  no penalty, and password/token checks used plain `==` (timing-attackable).
- **Fix:** a **global** failure throttle in the middleware (deliberately NOT
  per-IP — per-IP would let a botnet get a fresh attempt budget per address).
  First `_FREE_ATTEMPTS` (5) failures are free; past that the hub is locked for
  `_BASE_LOCKOUT * 2**(n-1)` seconds (2 s doubling, capped at `_MAX_LOCKOUT` =
  300 s), during which every external request is refused with `429` +
  `Retry-After` **before** the password is examined. A successful auth clears
  the counter (`_record_success`). All secret comparisons now use
  `hmac.compare_digest`. Tracking is in-process only (a restart forgives).
  - **Accepted trade-off:** a global lockout means an attacker can keep the door
    armed and deny the owner's *remote* access. Mitigated because LAN/loopback
    bypasses the throttle entirely and any single lockout is capped at 5 min.
- **Tests:** `tests/test_external_auth.py` (12 cases) drives the ASGI middleware
  directly — LAN bypass, correct/incorrect password paths, lockout arming,
  global (cross-IP) lockout, counter reset on success, growing window, cookie auth.

## Bundled-CLI cache_control TTL-ordering 400 on long resumed sessions

- **What:** A resumed turn fails with
  `API Error: 400 ... messages.N.content.M.cache_control.ttl: a ttl='1h'
  cache_control block must not come after a ttl='5m' cache_control block`. This is
  **not** an orchestrator2 bug and **not** an auth bug — it's the bundled `claude`
  CLI (`claude_agent_sdk/_bundled/claude.exe`) mis-ordering prompt-cache
  breakpoints. The 1h TTL is gated by an eligibility check
  (`should1hCacheTTL` in Claude Code's `services/api/claude.ts`) that depends on
  `!currentLimits.isUsingOverage`; when that flips mid-session (e.g. you cross
  into rate-limit overage) some blocks get `ttl='1h'` and others `ttl='5m'`, and
  the API rejects the mixed ordering. Claude Code's own
  `promptCacheBreakDetection.ts` comments say a fix is being verified upstream, so
  it's a known CLI-side race.
- **Where:** entirely inside the bundled CLI; orchestrator2 sets no cache_control.
- **Symptom vs auth:** because it's a *400* reaching the API, it actually proves
  auth is working (a dead session 401s or shows "Not logged in" first).
  `detect_api_error` catches it (`API Error: 400`) and, after two identical
  repeats, breaks the auto-continue loop with a recovery hint.
- **Workaround (implemented):** `--disable-prompt-cache` sets `DISABLE_PROMPT_CACHING=1`
  in the CLI subprocess env (`_make_options`), so the CLI emits **no** cache_control
  blocks and the ordering error can't occur (at the cost of prompt-cache savings).
  You can also just export `DISABLE_PROMPT_CACHING=1` before launch — orchestrator2
  inherits the environment through to the CLI. `/compact` sometimes clears the
  specific poisoned request too.
- **Proper fix:** update `claude-agent-sdk` (pip) to pull a bundled CLI where the
  TTL-flip latch is fixed; then the flag can be dropped.
- **2026-09-02:** the SDK was updated (0.1.59 → 0.2.151, bundled CLI
  2.1.105 → 2.1.258). Whether that fixed the TTL-flip latch has **not** been
  verified — the way to check is to run without `--disable-prompt-cache` and
  cross into overage. Until someone does, keep the flag.

## Oversized WebSocket `history` frame (>1 MB)

- **What:** On attach, the server sends the whole session scrollback as a single
  `history` WebSocket message. For long-lived sessions this frame can exceed
  1 MiB. The Python `websockets` client rejects it with close code 1009
  ("message too big", default `max_size=1048576`); browsers have higher/other
  limits but a large frame still means a slow, memory-heavy attach and a
  needless re-send on every reconnect.
- **Where:** `_send_initial_state()` in `server.py` builds and sends the
  `history` payload; `render_session_history()` in `session.py` produces it.
- **Repro:** attach a WS client with default limits to a session that has a very
  large scrollback (e.g. the default runtime after a long conversation) — the
  socket closes with 1009 before the app can render.
- **Proper fix:** cap/trim the initial history to a sane tail (it already has a
  `max_history` arg) *and/or* chunk the history across multiple frames so no
  single frame is huge. Also consider that clicking the already-viewed session
  in the lobby used to trigger a full reconnect + history re-send (fixed
  2026-07-18: lobby now just closes the overlay for the current session).

## `/login` reports "already signed in" for a fully-expired OAuth session — FIXED 2026-07-21

- **What:** `auth.is_logged_in()` fast-paths on `_has_credentials_file()`, which
  only checks that an `accessToken` string exists in `<config-dir>/.credentials.json`
  — not that the session can still authenticate. When *both* the access token and
  the refresh token have expired (the CLI fails with "OAuth session expired and
  could not be refreshed"), the file still contains a token, so `/login` replied
  "Already signed in … Use /logout to switch accounts" while every turn 401s
  (`authentication_error: Invalid authentication credentials`). The user was stuck:
  the app insisted they were signed in, but nothing worked. Note `claude auth
  status` was *also* unreliable here — it reported `loggedIn: true` for the dead
  session — so it can't be the sole authority.
- **Where:** `auth.py` (`is_logged_in`), `_cmd_login()` in `commands.py`.
- **Fix:** use the authoritative signal — the API's own auth failures.
  `state.auth_error` (sticky bool, added to the `State` dataclass) is set from two
  places in `sdk_bridge.py`: (1) per-turn, when a ResultMessage carries
  `err_code == "401"` (`detect_api_error`); and (2) at *connect* time, when the CLI
  prints an auth failure to stderr — `_on_sdk_stderr` matches `_AUTH_FAIL_RE`
  ("OAuth session expired", "could not be refreshed", "Failed to authenticate",
  "Invalid authentication credentials", "authentication_error"). It's cleared on a
  successful `connect()` and on the next clean turn. `_cmd_login()` treats
  `state.auth_error` (or an explicit `/login force`) as "must re-authenticate" and
  launches the OAuth flow instead of stonewalling; the "already signed in" message
  points at `/login force`. Connect-failure messages now say "run /login to
  re-authenticate", and the connect give-up loop wakes on a `/connect` event (not
  just a user message) so re-auth → `/connect` restarts the SDK cleanly.
- **Follow-up 1 — codeless "Not logged in" banner + false-positive fix (2026-07-21):**
  the CLI can also refuse a turn with a *codeless* banner ("Not logged in · Please
  run /login") emitted as the turn's assistant text while still closing the turn
  `subtype="success"`. `detect_api_error` only matches `API Error: <code>`, so this
  slipped through and the ResultMessage `else` branch actively *cleared*
  `state.auth_error` — `/login` kept saying "already signed in".
  **First attempt (buggy):** matched "Not logged in" / "Please run /login" as a
  *loose substring* of the assistant/result text. That backfired immediately: in a
  debugging conversation *about* the auth code, the model's own response contains
  those phrases (and "API Error: 401"), so a perfectly good turn was flagged
  `auth_error=True` and the status showed "not authed".
  **Proper fix:** the CLI's error/refusal banners are always the *leading* content
  of the turn output, whereas the model quoting them never *begins* its whole
  response with them. So detection is now **anchored to the start** of the stripped
  turn output: `detect_api_error` uses `_API_ERROR_RE.match(text.lstrip())` (not
  `.search`), and a dedicated anchored `_AUTH_BANNER_RE` (`.match`) catches the
  codeless banner. `_AUTH_FAIL_RE` stays a loose match but is used **only for
  stderr** (the CLI subprocess's output, never model prose). Regression test:
  `tests/test_api_error_detect.py` (real banners detected; quoted-mid-prose not).
- **Follow-up 2 — auth checks were account-blind (2026-07-21):** every `auth.py`
  helper (`is_logged_in`, `account_email`, `auth_status`, `launch_login`) operated
  on the *process* `CLAUDE_CONFIG_DIR`, so `/login`/`/logout` for a cross-account
  hub runtime checked and re-authed the wrong account. Fix: all four now take an
  optional `config_dir` and run the CLI subprocess with `CLAUDE_CONFIG_DIR` pinned
  to it (new `_auth_env()` helper); `_cmd_login`/`_cmd_logout` pass the runtime's
  `config.config_dir`. Startup `_ensure_logged_in()` still uses the pinned env
  default (correct for the default account).

## Frontend gives up reconnecting and can't recover without a page reload — FIXED 2026-07-21

- **What:** After the browser↔server WebSocket dropped (e.g. server restart), the
  frontend auto-reconnected for up to `MAX_RECONNECT_ATTEMPTS` (20, ~8 min with
  backoff), then latched `_serverShutdown = true` and stopped trying. In that
  latched state the tab was permanently dead: typed prompts hit `send()` → socket
  not open → "Not connected to server" and were **dropped** (not queued); `/connect`
  also failed because it was a command sent *over* the dead socket. Only a manual
  page reload recovered. A slow restart (e.g. while re-logging in) easily exceeded
  the 8-min budget, stranding the tab even though the server was back up.
- **Fix (static/app.js, static/commands.js):** added `App.reconnect()` which clears
  the `_serverShutdown`/attempt latch and rebuilds the socket client-side (or, if the
  socket is alive, forwards `/connect` as a server-side SDK reconnect). `/connect`
  /`/reconnect` are now intercepted in `commands.js` `_send()` and routed through it,
  so they work with a dead socket. Prompts typed while disconnected are queued in
  `_pendingSends` and flushed on the next `ws.onopen` (`_flushPending`). The
  give-up message now tells the user to type `/connect`.
- **Possible further improvement:** surface a visible "Reconnect" button in the
  "server stopped" status instead of relying on the `/connect` command.

## `/history` & `/export` report "not found on disk" for a cross-account session — FIXED 2026-07-21

- **What:** In the multi-session hub each `SessionRuntime` may run under a
  *different* Claude account (its own `config.config_dir` / `CLAUDE_CONFIG_DIR`),
  and its session JSONL lives under *that* account's `projects/` tree. But
  `_cmd_history` and `_cmd_export` called `find_session_dir(sid)` with **no**
  `config_dir`, so the lookup fell back to the hub process's own env account and
  searched the wrong tree. For a session belonging to another account (e.g.
  `5cd4115d` under `.claude-account-b/...forward-raytracer/`) the file was never
  found and the command replied "Session 5cd4115d not found on disk."
- **Where:** `_cmd_history`, `_cmd_export` in `commands.py`; also the
  `find_session_dir` calls in `server.py` (`_reconfigure`, the launch/attach
  endpoint, and the startup `--resume` resolve).
- **Fix:** pass the runtime's `config.config_dir` to `find_session_dir(sid, ...)`
  everywhere. `_cmd_history`/`_cmd_export` use `getattr(config, "config_dir", None)`;
  the server.py call sites use `config.config_dir` (the title reads there already
  did). `find_session_dir(session_id, config_dir)` → `claude_projects_dir` scopes
  the scan to the correct account tree.

## Frontend/backend version skew after code edits

- **What:** Static JS/CSS is served live from disk, but the Python backend only
  changes on process restart. Editing both leaves a running server with new
  frontend + old backend, producing confusing behavior (e.g. bare `/` URLs
  auto-attaching to the default session instead of landing in the lobby).
- **Mitigation:** use the ⟳ Restart button (or relaunch) after backend edits;
  hard-refresh browser tabs (especially mobile, which caches aggressively).
- **Possible improvement:** add a build/version stamp the frontend checks against
  the backend and surfaces a "reload / restart" hint on mismatch.
