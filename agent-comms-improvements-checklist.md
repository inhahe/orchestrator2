# Checklist: five improvements to the agent messaging feature

> **Status 2026-09-07 — all five done.**  Verified by
> `tests/test_agent_comms_checklist.py` (25) and the `checklist` target of
> `tools/mutate.py` (9 mutations, all caught).  Per item:
>
> 1. **Attributes** — free-form `labels` on every registration
>    (`--agent-label k=v`, repeatable), returned by `live_agents()` and printed
>    by `agents.py list`, which additionally *states* a same-directory
>    collision rather than leaving it to be noticed.  The registry never
>    interprets a label.
> 2. **Tell a copied session that it moved** — `/move` sets a one-shot
>    `--session-note`, prepended to the copy's next prompt by
>    `SDKBridge._with_session_note`.  To the model, not the browser.
> 3. **Assignable, stable identity** — was already implemented:
>    `agent_comms.resolve_identity()` is the §3.3 table verbatim, refusal row
>    included.
> 4. **Delivery and read state** — `agent_comms.message_state()` reports
>    `queued` / `delivered` / `expired` plus `target_live_at_send` (recorded at
>    send, not merely returned); `agents.py status <id>` says which of *wait*
>    and *escalate* the answer implies.  No state beyond `delivered`: delivery
>    is observable, comprehension is not.
> 5. **Relayed authority** — stated as a non-goal in `tools/agents.py`'s
>    docstring and in `send --help`, and every delivered message now carries
>    the framing so the correct refusal does not depend on the receiving agent
>    being unusually careful.  No mechanism was built; a test asserts
>    `send_message` has grown no `operator`/`authority`/`priority` parameter.
>
> The "must not regress" section was re-checked: cross-account discovery,
> recorded delivery, and the message body arriving **intact** (a channel worth
> being contradicted on is the property most easily lost to well-meant
> wrapping) each have a test.

Companion to `agent-comms-spec.md`. That document is the design; this is the
short list to verify an implementation against. Each item states what was
**observed** on 2026-09-06 (the day the cross-account registry landed and four
sessions used it for an afternoon), what to change, and how to tell it is done.

Ordered by how much each one cost that day.

---

## 1. Attributes in the session listing

**Observed.** `ListAgents` returns name, kind and start time. There is no cwd,
no branch, no lane. Consequences, both real:

- One agent was identifiable only because it messaged first; another was found
  by elimination on start time. A message was sent to a session chosen from a
  timestamp and framed defensively ("ignore this if you are not lane B")
  because misaddressing could not be ruled out.
- **Two sessions claimed the same lane and did the same one-line fix**, one of
  them on a superseded tree. Neither could detect the other, and nothing in the
  system could either.

**Change.** Attach arbitrary caller-supplied key/value labels at registration,
and return them in the listing. Index by cwd — that lookup runs at every
startup. The service must not interpret the labels; "lane" is a project's word,
not the client's.

**Done when.** An agent can answer *"which live session owns this directory?"*
without messaging anyone, and a second session registering the same cwd is
detectable.

---

## 2. Tell a copied session that it moved

**Observed.** Copying a session preserves its working directory but not its
knowledge of where it is. Every absolute path in the transcript still resolves,
because the old tree still exists. During a drive migration this caused:

- the same one-line fix made **twice**, committed on the new drive and left
  uncommitted on the old one;
- an agent running `git fetch` and `git show origin/main:...` against the stale
  copy to answer a question for a third agent — the answers happened to agree,
  which was luck, not care;
- a pre-copy session left running beside its copy, both claiming one lane.

**Change.** When a session is copied, inject a one-line note at the top of the
copy's next turn: *"this session was copied from X to Y; absolute paths earlier
in this transcript refer to X."*

**Done when.** A copied session's first turn states its new location. This is
the cheapest item on the list and would have prevented every instance above.

---

## 3. Assignable, stable identity

**Observed.** One session was `os-73` before the migration and `os-12` after —
same session, same lane, same work. A peer that had sensibly decided to "key on
lane, not name" then could not, because lane is not exposed either (item 1).

**Change.** A name settable at launch and persisted, so it survives a restart
and a change of directory. Auto-generate when unspecified. Keep the identity
opaque: do not encode role or location into it — those are attributes (item 1).

**Resolution at startup**, where more than one agent may share a directory and
the environment cannot distinguish them:

| situation | identity |
|---|---|
| session resumed | inherit the prior session's |
| launched with an explicit name | that name |
| no name, exactly one identity registered for this cwd **and not live** | adopt it, and log that it did |
| no name, several registered for this cwd | **refuse: list them and ask** |
| no name, none registered | create one, persist it |

**Done when.** Restarting an agent in a new directory keeps its identity, and an
ambiguous case refuses rather than guessing. The refusal row is the load-bearing
one: silently adopting the wrong identity inherits another agent's queue and
halt state, and neither agent can detect it.

---

## 4. Delivery and read state

**Observed.** A send returned `success: true` and nothing happened for several
minutes — the recipients had not taken a turn since being restarted, and
messages drain at the receiver's next tool round. The sender could not
distinguish that from having been read and deprioritised. **The two call for
opposite responses: wait, versus escalate.**

**Change.** Report per-message state — queued / delivered / read — and whether
the target was live at send time. Queue for a non-live identity with a TTL
rather than dropping.

**Done when.** A sender can tell "not yet seen" from "seen and not acted on"
without asking.

---

## 5. State the non-goal: relayed authority

**Observed.** An agent relayed an operator instruction to a peer — *"the
operator has asked me to tell you to resume"* — and the peer **correctly
refused**: a relay is unverifiable, and looks identical whether it is accurate,
a good-faith misreading, or wrong. Its reasoning was the right one: *if I wait
and you are right, the cost is a delay until the operator types one line; if I
act and you are mistaken, I am working against a direct instruction and neither
of us finds out for a while.*

**Change.** Write it into the docs as a non-goal, and **do not build a mechanism
that appears to solve it.** A flag marking a message "operator-originated" would
not be verifiable either, and would be worse than nothing because it would look
like authority.

**Done when.** The tool description says a peer message is never the operator's
instruction, and no feature contradicts that.

---

---

## Not an improvement: what already works, and must not regress

**This is not a sixth item.** It is here because the five above are a list of
defects, and an implementer working only from them could regress something
that was verified working on the same day. Also in the spec as section 10.6.

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
