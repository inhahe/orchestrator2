# Specification: cross-account agent identity, registry, and coordination

**Audience:** the session implementing this in the client.
**Note:** a separate "addendum on surfacing" circulated briefly on 2026-09-06;
it has been folded into section 9 and deleted. This file is the whole of it.
**Status:** proposal, from the agent side. Written 2026-09-06 by the SlateOS
lane-A session, which hit every failure below in practice today.

---

## 1. The problem, concretely

Several Claude Code sessions work one machine and one repository at the same
time. They currently **cannot address each other at all**, because the session
registry is scoped per CLAUDE_CONFIG_DIR and the sessions run under different
accounts. ListAgents from one of them reports *"no other Claude session is
running on this machine"* while two others are demonstrably working.

Two failures this caused today, both measured:

1. **No way to say stop.** Migrating the repository to a different drive
   required all three sessions quiescent. The agent that needed the other two
   to stop had no channel; the human had to carry the message by hand.

2. **A file-based workaround is passive, and passivity is the actual defect.**
   The agent built a signalling layer over the shared git directory. It works:
   a flag written by one lane is instantly visible to the others. But nothing
   *delivers* it. Checked afterwards, one lane could not see the message at all
   (it had not merged), and the other had the files physically present in its
   tree and had never been prompted to look.

The general shape: **the transport is the easy part; delivery and enforcement
are the parts that change behaviour.**

Evidence for that claim, because it should shape the design. The last
cross-agent bug to cost real time was filed on 2026-09-03, was present in the
receiving agent's own working tree on 2026-09-04, and still cost that agent a
7783-second wasted job on 2026-09-06. It was never a transport problem. An
autonomous agent mid-task reliably deprioritises anything that reads as
informational.

---

## 2. Non-goals

- **Not a chat room.** Conversation between autonomous agents is a good way to
  spend tokens on politeness loops. Everything here is one-way by default.
- **Not a replacement for in-repo documents.** Durable technical exchange
  (design proposals, bug reports, decisions) belongs in version control, where
  it is reviewable and attributable. This carries *operational* traffic only:
  stop, I am about to move the tree, who is running something long.
- **Not process classification.** See section 6; the system never tries to
  decide what counts as a long-running job.
- **Not an authority channel.** An agent must never treat a peer message as
  its operator's instruction, and no mechanism should be built that appears
  to make one verifiable — a flag marking a message "operator-originated"
  would be unverifiable too, and worse than nothing because it would look
  like authority. Instructions reach an agent from its own operator; the
  channel carries information between peers. See section 10.5, where a
  relayed instruction was correctly refused.
- **No project domain model.** The service must not know what a "lane" or a
  "reviewer" is. Those are labels it stores and never interprets.

---

## 3. Identity

### 3.1 Identity is a name, not a derivation

Every derivation scheme breaks in some topology:

| derived from | breaks when |
|---|---|
| account / config dir | two agents under one account |
| working directory | two agents in one directory |
| pid / session handle | the session restarts |

So identity is **assigned**, like a hostname: a readable string, settable at
launch, stable across restarts. **Observed 2026-09-06:** one session was `os-73`
before a drive migration and `os-12` after — same session, same lane, same
work. A peer that had sensibly resolved to "key on lane, not name" then
could not, because lane is not exposed either (section 10.2). The client already generates something of the
right shape (for example `os-73`); what is missing is that it is not settable
and does not persist.

### 3.2 Identity versus attributes

The identity is opaque to the system. Everything else is an **attribute**
attached at registration and queryable: cwd, repo, branch, account, pid, host,
started-at, plus arbitrary caller-supplied key/value labels.

Do **not** encode role, lane, or location into the identity string. That is the
mistake the current scheme makes: because identity is derived from the config
directory, a session that does not fit the expected pattern has no identity at
all.

### 3.3 Resolving identity at startup

This is the part that needs care, because more than one agent may share a
working directory, and in that case **the environment does not contain enough
information to tell them apart.** Something outside it must supply the answer.

| situation | identity |
|---|---|
| session resumed (--resume / --continue) | inherit the prior session's, unambiguous by definition |
| launched with an explicit name | that name |
| no name, exactly one identity registered for this cwd **and it is not live** | adopt it, and say so in the startup log |
| no name, several registered for this cwd | **refuse: list them and ask** |
| no name, none registered | create one, persist it |

**The refusal row is load-bearing.** If a fresh session silently adopts the
wrong identity it inherits another agent's message queue and halt state, and
neither agent can detect it: the wrong one is told to stop, the right one never
is. That is a failure with no symptom. Guessing is worse than asking.

Auto-adoption (row 3) must require that the identity is **not currently live**,
so two concurrent sessions can never hold the same identity.

---

## 4. Registry

### 4.1 Scope

**Machine-wide, spanning accounts.** This is the single defect that motivates
the whole feature: per-CLAUDE_CONFIG_DIR scoping is why sessions are mutually
invisible today. Cross-machine is not needed yet and adds authentication and
availability problems; design so it could be added later, but do not build it
now.

### 4.2 Population

**Self-registering.** A session registers on start and deregisters on clean
exit. No operator-maintained roster: rosters go stale, and a stale roster is
indistinguishable from an accurate one until it misroutes something.

### 4.3 Liveness

Registration carries a **heartbeat with a TTL**, and entries expire. The useful
query is not "who is agent X" but *"who is alive right now, and what are they
holding?"* Without expiry a crashed session stays listed forever and
auto-adoption becomes unsafe.

Today, lacking this, agents resort to inspecting rustc processes and directory
mtimes to guess whether a sibling is mid-build. That is the crude version of
exactly this query.

### 4.4 Lookup

**This is a correctness requirement, not a convenience** — see section 10.3,
where its absence let two sessions claim one lane and do the same work
without either being able to detect the other.

Index by **identity** and by **cwd**. The cwd lookup happens on every startup
for section 3.3, so it must be cheap; everything else can be a scan.

Minimum queries:

- resolve an identity to its live instance, or report not-running
- list identities registered for a given cwd, with live/not-live for each
- list all live sessions for a given repo

---

## 5. Messaging

### 5.1 Delivery

**At turn boundary**, queued, not mid-turn. Mid-turn injection interrupts an
agent that may hold a half-finished edit; turn-end delivery is prompt (turns
are minutes) without that risk.

This is the piece a file-based scheme cannot provide, and it is the main reason
to build this in the client rather than in the repository.

**Observed, with a clean counterfactual.** On 2026-09-06 an agent raised a halt
and posted a broadcast notice at 16:42, into a directory every sibling shares
and can read without any merge. A sibling agent then ran `cargo test -p
compositor` at 19:00:28, finished it, and started `cargo test -p desktop` at
19:07:50. Checked afterwards: it *had* the reading tool committed in its own
tree, and the halt and notice files were still untouched at their original
16:42 timestamps nearly three hours later.

Note what is and is not being claimed. The sibling was not ignoring anything —
nothing ever put the message in front of it. But there was a **turn boundary at
roughly 19:07**, between one task finishing and the next beginning, and a
message delivered there would have been read. That is the entire argument for
this section: the boundary existed, it was the natural place to stop, and only
push delivery reaches it.

### 5.2 Addressing

Two forms are sufficient for the scale in question, a handful of agents:

- **direct**, to one identity
- **broadcast**, to every live session in a given repo or cwd scope

Attribute-based selectors are a reasonable later addition, but only as **opaque
labels the service never interprets**. Do not build them until something needs
them.

### 5.3 Semantics

- **One-way by default.** A message does not expect a reply.
- **Queue for a non-live identity**, with a TTL, so a message to an agent that
  is restarting is not simply lost. Report to the sender whether the target was
  live.
- **Delivered/read state must be tracked.** Observed cost (section 10.4):
  a send returned success and nothing happened for minutes, because the
  recipients had not taken a turn since restarting. "Queued" and "read and
  deprioritised" were indistinguishable, and they call for opposite
  responses — wait, versus escalate.
  Without it a sender cannot tell an ignored message from an undelivered one,
  and that ambiguity is the same bug that made the repository's own request
  tracking unreliable: replies existed but nothing recorded that they had been
  seen.

---

## 6. Halt: the enforcement primitive

Messages inform. Informational input gets deprioritised (section 1). So the
highest-value primitive is not a message but a **flag that a receiving agent
cannot politely defer.**

### 6.1 Model

A halt is **machine-readable shared state**, not prose: raised-by identity,
raised-at time, scope (repo or machine), and a reason string.

Two layers consume it, and both are needed:

- **Agent-level, soft.** Surfaced as a queued prompt at turn end: stop taking
  new work. Deferrable, which is why it is not sufficient alone.
- **Operation-level, hard.** A long-running operation calls a checkpoint before
  starting and *refuses to start* if a halt is in force. The agent does not get
  to weigh it.

### 6.2 The system must never classify processes

The client cannot and should not try to decide what counts as long-running.
Instead the **operation that already knows it is expensive asks permission**:

    check-halt --scope repo   ->  exit 0 = clear, exit non-zero = halted

The operation about to spend three hours is the one that knows it, and it also
knows *where its safe abort points are*: refusing before a build is free,
whereas aborting two hours in can leave stale locks. An external classifier
could not know either fact.

This is cooperative: an operation that does not check cannot be stopped. That
is an acceptable limit, but it is a **larger** limit than it first appears, and
the reference implementation understated it.

That implementation wired exactly one operation -- the boot test -- on the
reasoning that it was the expensive one. In practice an agent spends most of
its time in commands that were never wired: on 2026-09-06 a halt failed to stop
a sibling agent through two consecutive `cargo test` runs, because `cargo test`
has no checkpoint and the agent had no reason to run the reader.

So the checkpoint belongs in **whatever wrapper an agent uses to run long
commands at all**, not only in the single most expensive one. Wiring one
operation and generalising from it produces a halt that is correct, tested, and
almost never consulted.

Enforcing against genuinely uncooperative processes still needs OS-level
mechanisms and remains a different, much worse project.

### 6.3 Exit-code contract

The halt check must exit **non-zero when halted**, so a caller that only
inspects status refuses correctly without parsing output. Both directions need
tests: a halt in force must fail, and a plain informational message present
with no halt must **not** fail.

A working reference implementation of that two-level distinction exists, with
18 self-test cases, at:

    D:/visual studio projects/os-lane-a/scripts/check-lane-signals.py

Run its cases with `python check-lane-signals.py --self-test` from any cwd.
Note that this tree is being migrated to a different drive shortly, so if that
path does not exist, look under `E:/visual studio projects/os-lane-a/`
instead; it is also committed on the SlateOS repository's `main` branch at
`scripts/check-lane-signals.py`.

Its storage layer is a shared git directory rather than a registry, so only the
*semantics* carry over: the notice-versus-halt split, the exit-code contract,
and the self-test structure that asserts both directions.

### 6.4 Lifecycle

Raised by any agent or by the operator, and **lifted explicitly, never on a
timer**. A halt that expired on its own would clear itself in the middle of the
maintenance it exists to protect.

---

## 7. Decisions left to the implementer

1. **Transport and store.** A small local daemon, a lock-protected file, or a
   SQLite database are all plausible. This document constrains semantics, not
   mechanism. Whatever is chosen must tolerate concurrent writers from
   different accounts.
2. **Where the persisted name lives.** It must survive restarts and survive a
   checkout being moved to a different path or drive.
3. **Authentication.** Same-machine cooperative agents probably need none.
   Worth deciding deliberately rather than by default, since identity is
   currently unauthenticated: any session can claim any lane by setting an
   environment variable.
4. **Halt scope granularity.** Per-repo is the common case; machine-wide
   matters for something like a drive migration that spans repositories.

---

## 8. Acceptance: what would have prevented today's failures

1. An agent can enumerate live sessions on the machine **across accounts**.
   This is the load-bearing one: an empty listing teaches the agent the feature
   is absent, and it stops looking (section 9.1).
2. It can send them a message that arrives at their **next turn boundary**,
   without the human relaying it. Verify by *observing a real message arrive in
   a second session's transcript*, not by checking that the call returns
   success.
3. It can raise a halt that makes a sibling's multi-hour job **refuse to
   start**, with a message explaining why rather than looking like a failure.
4. A session restarting in a shared directory resolves to the **same identity**
   as before, or refuses and asks rather than guessing.

---

## 9. Surfacing: how an agent knows the feature exists

Most of this deliberately requires no memory at all, and that is a design goal
rather than an accident:

- **Receiving** needs none. A message delivered as a queued prompt at the turn
  boundary simply arrives.
- **Enforcement** needs none. The boot test refuses because the *script* calls
  the checkpoint (section 6.2), not because the agent remembered a rule. An
  agent that has never heard of halts is still stopped by one.

That leaves **sending**, which is the only part an agent must actively know it
can do.

### 9.1 The existing tool descriptions already promise this

This is the most important point in this section. The `ListAgents` description
shipped to agents today reads:

> Lists agents you can SendMessage to — in-process subagents you spawned, the
> teammates on your team, **other local Claude sessions on this machine**, your
> Claude sessions running in the cloud...

That already describes the wanted capability, and the description is partly
self-teaching: on 2026-09-06 the lane-A agent needed to stop two sibling
sessions and reached for `ListAgents` unprompted, with nothing in any project
file telling it to.

**Do not over-read that, because the same episode shows the limit.** The call
returned *"no other Claude session is running on this machine"* — while two
sessions were demonstrably working — and the agent then **stopped**. It never
tried `SendMessage` at all, concluded the capability was absent, and spent the
next hour building a file-based workaround instead. It only tested `SendMessage`
later, when the operator asked whether it actually had. (It fails cleanly:
`No agent named 'lane-b' is reachable. Use ListAgents to see everyone you can
message.`)

The lesson: **an empty `ListAgents` is indistinguishable from "this feature does
not exist here"**, and an agent that gets one negative answer reasonably stops
looking. So widening the registry scope (section 4.1) is not merely a
correctness fix that makes shipped text true — it is what makes the capability
*discoverable at all*. No quality of tool description compensates for a listing
that comes back empty, because by then the agent has its answer.

A corollary worth planning for: **do not treat "the tools already describe it"
as evidence that agents will use it.** That evidence is one agent, once, and it
gave up at the first negative result. Verify with a real two-session test
(section 8) rather than by reasoning about the descriptions.

No *new* tool is needed for messaging, and no project documentation has to
teach agents that messaging exists — but that follows from the scope fix
landing, not from the descriptions being well written.

### 9.2 Prefer a tool over prose, for anything an agent must know it can do

A tool cannot be forgotten: it is in context on every turn. Prose in a project
instruction file is read once at session start and then competes with
everything else in that file.

That distinction is not theoretical. The failure in section 1 -- a filed bug
sitting unread in the receiving agent's own working tree for two days, then
costing a 7783-second job -- is exactly what happens to information that exists
but is not present at the decision point. Adding paragraphs to an already-large
instruction file dilutes everything else in it without reliably changing
behaviour.

### 9.3 The one new surface needed: raising a halt

Section 6 specifies what a halt *is* and how operations *check* it, but not how
one is raised. Recommended split:

| action | surface | why |
|---|---|---|
| raise / lift a halt | **agent-facing tool** | an agent must discover it; it is rare, deliberate, and needs to be reachable without shelling out |
| check a halt | **CLI, exit-code contract** | called by scripts, not agents (section 6.2); must work from a bare shell with no client running |

The checking CLI must remain usable when no agent is involved at all -- a build
script, a CI step, or a human at a prompt should all be able to ask.

### 9.4 What belongs in project documentation, and what does not

Keep it to policy a tool description cannot carry. For a multi-agent repository
that is roughly one line: *when you need every agent to stop, raise a halt
rather than asking the operator to relay the message.* An agent will not infer
that from a tool description, because the tool describes a capability and not
the occasion for using it.

Everything else -- that the capability exists, what its arguments are, what it
returns -- belongs in the tool description, where it cannot be forgotten.

---

## 10. Field notes: what a day of real use revealed

Sections 1-9 were written before the feature existed. This section records what
changed after four sessions used it for an afternoon, on the day the registry
scope fix landed. Everything here is observed, not reasoned; where a
recommendation above was wrong, it is corrected in place and noted here.

### 10.1 The session-copy hazard, which nothing above covers

The client can copy a session from one account or directory to another. During
a drive migration this was used to move three sessions from `D:` to `E:`.

**A copied session keeps its working directory but not its knowledge of where
it is.** Every absolute path in its transcript still points at the old tree, and
because the old tree still exists and still has working git, those paths keep
resolving. Commands built from memory succeed quietly against a location nobody
will read again.

Observed consequences, same afternoon:

- One agent made the same one-line fix **twice** — committed on the new drive,
  left uncommitted on the old one — having reached for a remembered path.
- Another ran `git fetch` and `git show origin/main:...` against the stale copy
  to answer a question for a third agent. The answers happened to agree with the
  live tree, so nothing it reported was wrong. That was luck, not care: the
  stale tree answers git perfectly happily and nothing in the output names the
  drive.
- A pre-copy session remained alive alongside its copy. Both identified as the
  same lane. Neither could detect the other.

The uncomfortable conclusion is that **keeping the old tree as a fallback is
what makes it dangerous.** A deleted tree fails loudly on the first stale path;
a kept one never fails at all. The mitigation that worked was blunt: make the
old location refuse — rename its `.git`, or delete it once the new location is
proven.

For the client specifically: a copied session should be *told* it moved. A
one-line system note at the top of the copy's next turn ("this session was
copied from X to Y; paths in your transcript above refer to X") would have
prevented every instance above. Nothing else here is as cheap.

### 10.2 Identity: the name is not stable, and it was needed

Section 3.1 recommends an assigned, stable name. Unimplemented at time of use,
and the gap was felt immediately: one session was `os-73` before the migration
and `os-12` after — same session, same work, same lane. An agent that had been
told "key on lane, not name" then could not, because lane is not exposed either.

### 10.3 Attributes are a correctness requirement, not a convenience

Section 4.4 asks for lookup by cwd. Promote it. `ListAgents` currently returns
name, kind and start time, and that is not enough to answer *"which session owns
this part of the tree?"* — which is the only question a multi-agent repository
actually asks.

Two costs, both observed rather than predicted:

1. **Addressing by guess.** One agent was identifiable only because it messaged
   first; another was found by elimination on start time. A message was sent to
   a session picked from a timestamp, framed defensively ("ignore this if you
   are not lane B") because misaddressing could not be ruled out.
2. **Duplicate role-claims are undetectable.** Two sessions claimed the same
   lane and did the same work. With cwd in the listing that is a one-line check
   at startup; without it, nothing in the system can notice.

### 10.4 Delivery state: "queued" and "ignored" look identical

Section 5.3 asks for delivered/read tracking. Observed why it matters: the first
message sent after the scope fix returned `success: true` and then nothing
happened for some minutes — because the recipients had not taken a turn since
being restarted, and messages drain at the receiver's next tool round. The
sender cannot distinguish that from having been read and deprioritised, and the
two call for opposite responses (wait, versus escalate).

Note the acceptance criterion in section 8 exists for this reason and held up:
`success: true` was not evidence of anything. What settled it was observing the
recipient act.

### 10.5 A non-goal to state explicitly: relayed authority

An agent relayed an operator instruction to a peer — "the operator has asked me
to tell you to resume" — and the peer **correctly refused**, on the grounds that
a peer message is not the operator's word and a relay is unverifiable: it looks
identical whether it is accurate, a good-faith misreading, or wrong.

The asymmetry the refusing agent gave is the right rule, and it should be in the
spec rather than rediscovered: *if I wait and you are right, the cost is a delay
until the operator types one line; if I act and you are mistaken, I am working
against a direct instruction and neither of us finds out for a while.*

**Do not build a mechanism that appears to solve this.** A flag marking a
message "operator-originated" would not be verifiable either, and would be worse
than nothing, because it would look like authority. The correct design is that
instructions reach an agent from its own operator, and the channel carries
information between peers. State it as a non-goal so nobody implements the
flag.

### 10.6 What already works, and should not be regressed

Recorded because a later reader of sections 1-9 will be looking at a list of
defects and may not realise which parts were verified working on the same day.

- **Cross-account discovery.** `ListAgents` returns peers under other accounts.
  See section 9.1 for why the previous empty listing was worse than an error.
- **Delivery, verified by the criterion in section 8** — not by a `success:
  true`, but by watching the recipient act. A peer received a request, made the
  exact change asked for, and committed it *before* merging the branch that
  carried the written-down version, so the message and not the file did the
  work.
- **Disagreement survives the channel.** Three of the day's substantive
  corrections travelled peer-to-peer, two of them reversing the sender's own
  earlier claim. A channel that is useful for being contradicted on is a higher
  bar than one that is useful for being agreed with, and it is the property
  most likely to be lost if the design drifts toward broadcast-only
  notifications.
