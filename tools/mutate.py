"""Mutation-check the test suites that guard the trickiest fixes.

Each mutation undoes one part of a fix.  A mutation the suite still *passes* is
a part of that fix nothing actually verifies -- either the test is missing or
the code is dead.  Both kinds have been found here: a vacuous collapse-gap
test, a catch-up test that never fired a second visibilitychange, a real hole
where a trim parked on a frame while visible was stranded by hiding, and a
cancelAnimationFrame that no test could distinguish from its absence (removed
rather than kept unverified).

Targets:

  chat       static/chat.js  x tests/hidden_window.test.js         (node)
             the rendering half -- trimming and scrolling while hidden
  app        static/app.js   x tests/reconnect_on_show.test.js     (node)
             the transport half -- reconnecting a socket that died while hidden
  reconnect  sdk_bridge.py   x tests/test_reconnect_bg_tasks.py    (pytest)
             what a reconnect does to running background tasks
  queue      session.py      x tests/test_queue_persistence.py     (pytest)
  queue-     server.py       x tests/test_queue_persistence.py     (pytest)
   server    whose queued prompts a starting session may adopt
  extauth    server.py       x test_external_access_policy.py +    (pytest)
                               test_external_auth.py
             who may reach the hub from outside the LAN, and what counts
             as a failed password attempt
  foreign    server.py       x test_foreign_running_sessions.py    (pytest)
             a session running in *another* hub is running, not "recent"
  stale      server.py       x tests/test_stale_sources.py      (pytest)
             a hub that has been up for days is running the code it
             started with, and must be able to say so
  lobbycard  static/lobby.js  x tests/lobby_running_card.test.js (node)
             a running card says what it is and only what it knows:
             the title outranks its badges, and a session owned by
             another hub is never "current"
  move-    static/move.js x tests/move_overlay.test.js    (node)
   ui        the overlay sends both axes -- account and directory --
             and leaving one alone does not disturb the other
  move-    copy_session.py x tests/test_move_directory.py     (pytest)
   copy      what a session carries with it when it changes directory:
             the project slug, the per-record cwd and gitBranch -- and
             what it must NOT carry, i.e. the transcript's own paths
  move-    server.py       x tests/test_move_directory.py     (pytest)
   server    where /move files the copy and starts the runtime
  loop       sdk_bridge.py   x tests/test_loop_control.py          (pytest)
             the self-paced wakeup loop can be stopped -- by the agent
             (ScheduleWakeup stop=true) and by the operator (/loop off) --
             and a wakeup that keeps landing mid-turn gives up rather than
             re-arming itself forever
  agentcomms agent_comms.py   x tests/test_agent_comms.py          (pytest)
  agentcli   tools/agents.py x tests/test_agent_comms.py          (pytest)
             cross-account agent identity, registry, messaging, halt --
             including the two rules a plausible implementation gets wrong:
             refusing an ambiguous identity rather than guessing, and a
             notice not counting as a halt

The first two are design.md section 7, "Nothing that must keep working may be
gated on an animation frame"; the third is section 6d, "A reconnect is not free
while background tasks are running"; the queue pair is section 6, "The queue is
persisted, and it belongs to a *session*, not to a directory"; extauth is
section 9, "External access is off until you turn it on, with a password";
foreign is section 8, "The lobby".

Both kinds of survivor have shown up in the queue target and both were worth
more than the mutation: an age test that derived its timestamps from the
constant it was meant to pin (so it passed for a 1-second cap), and a
``restore=`` flag on each call site that the sweep proved the safety did not
depend on -- deleted rather than covered.

Run from anywhere:

    python tools/mutate.py             # every target
    python tools/mutate.py app         # one target

Exits 0 only when every mutation is caught.  Sources are restored on every exit
path; if a run is killed hard, a leftover <source>.mutbak holds the original --
its presence means a sweep is in flight or died mid-mutation.
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MOVECMD_MUTATIONS = [
    ("/move is not intercepted, so it goes to the server as an unknown command",
     "    if (text === '/move' || text.startsWith('/move ')) {",
     "    if (false) {"),

    ("a bare prefix match swallows /moved and anything else starting with it",
     "    if (text === '/move' || text.startsWith('/move ')) {",
     "    if (text.startsWith('/move')) {"),

    ("only the bare command is caught, so /move <dir> goes to the server",
     "    if (text === '/move' || text.startsWith('/move ')) {",
     "    if (text === '/move') {"),

    ("the path argument is dropped, so /move <dir> prefills nothing",
     "      Move.open(text.slice('/move'.length).trim());",
     "      Move.open();"),

    ("the command is opened *and* forwarded to the server",
     "      Move.open(text.slice('/move'.length).trim());\n      return;",
     "      Move.open(text.slice('/move'.length).trim());"),
]

PANELSTOGGLE_MUTATIONS = [
    ("the queue count never reaches the toggle, so nothing says the panels "
     "hold anything (the original report)",
     "    _updateLoop(status);\n    _updateToggleCount(status);",
     "    _updateLoop(status);"),

    ("the badge is shown even when the queue is empty, so it stops being a "
     "signal",
     "    elToggleCount.hidden = n === 0;",
     "    elToggleCount.hidden = false;"),

    ("the badge is never shown",
     "    elToggleCount.hidden = n === 0;",
     "    elToggleCount.hidden = true;"),

    ("the badge shows a stale count, because the change-guard latches",
     "    if (_prev.toggleCount === n) return;\n    _prev.toggleCount = n;",
     "    if (_prev.toggleCount !== undefined) return;\n    _prev.toggleCount = n;"),

    ("the count is read from the wrong field",
     "    const n = status.queued_count || 0;",
     "    const n = status.bg_count || 0;"),

    ("the badge is a bare number with nothing saying what it counts",
     "    elToggleCount.title = n === 1 ? '1 queued prompt' : n + ' queued prompts';",
     "    elToggleCount.title = '';"),
]

STATUSLOOP_MUTATIONS = [
    ("the wakeup countdown is never shown (the original gap)",
     "      const show = at !== null;",
     "      const show = false;"),

    ("the field is shown even with no wakeup armed",
     "      const show = at !== null;",
     "      const show = true;"),

    ("a missing field is read as an armed wakeup",
     "    const at = (typeof status.wakeup_at === 'number') ? status.wakeup_at : null;",
     "    const at = status.wakeup_at || 0;"),

    ("the label and separator are left hidden, so a bare number appears",
     "      for (const el of [elLoop, elLoopSep, elLoopLabel]) {",
     "      for (const el of [elLoop]) {"),

    ("the countdown never ticks, so it freezes between status pushes",
     "      if (show && !_loopTimer) _loopTimer = setInterval(_renderLoop, 1000);",
     "      if (false) _loopTimer = setInterval(_renderLoop, 1000);"),

    ("a deferred wakeup looks like an ordinary one",
     "    if (_wakeupDefers) text += ' \u00b7 deferred ' + _wakeupDefers;",
     ""),

    ("every wakeup claims to be deferred",
     "    if (_wakeupDefers) text += ' \u00b7 deferred ' + _wakeupDefers;",
     "    text += ' \u00b7 deferred ' + _wakeupDefers;"),

    ("the tooltip stops saying how to stop it",
     "         + '  Stop it with /loop off.');",
     "         + '');"),
]

CHATMODAL_MUTATIONS = [
    ("every modal falls back to inline text again, because a top-level const "
     "is not a property of window (the original bug)",
     "    if (typeof App !== 'undefined' && typeof App.openModal === 'function') {",
     "    if (window.App && typeof App.openModal === 'function') {"),

    ("the modal is opened *and* echoed inline, duplicating every /help",
     "      App.openModal(title, content);\n    } else {",
     "      App.openModal(title, content);\n    }\n    if (true) {"),
]

CHAT_MUTATIONS = [
    ("_onNextFrame always schedules a frame (the original bug)",
     "if (_hidden()) { fn(); return null; }",
     ""),

    ("_hidden() never reports hidden",
     "return typeof document !== 'undefined' && document.hidden === true;",
     "return false;"),

    ("_scrollToBottom pays for layout while hidden",
     "    if (_hidden()) {\n      _scrollPendingOnShow = true;\n      _maybeTrimOldMessages();\n      return;\n    }",
     ""),

    ("becoming visible does not catch up the deferred scroll",
     "    if (_scrollPendingOnShow) {\n      _scrollPendingOnShow = false;\n      if (_autoScroll) elMessages.scrollTop = elMessages.scrollHeight;\n    }",
     ""),

    ("the catch-up latch is never cleared, so it fires spuriously",
     "      _scrollPendingOnShow = false;\n",
     ""),

    ("hiding leaves the collapse gaps open (they wedge the trimmer shut)",
     "      _cancelGap();\n      _cancelShortGap();\n",
     ""),

    ("hiding does not reclaim a trim stranded on a frame",
     "      _reclaimStrandedTrim();\n",
     ""),

    ("_trimNow does not re-check the cap, so a stale frame eats live messages",
     "    const n = elMessages.children.length;\n    if (n <= _maxDomChildren) return;",
     "    const n = elMessages.children.length;"),

    ("no visibilitychange listener at all",
     "document.addEventListener('visibilitychange', _onVisibilityChange);",
     ""),

    ("history replay yields per batch even while hidden",
     "      } while (idx < messages.length && _hidden());",
     "      } while (false);"),

    ("history replay never yields, even while visible",
     "      } while (idx < messages.length && _hidden());",
     "      } while (idx < messages.length);"),

    ("the do/while degenerates into a plain block",
     "      do {\n        const end = Math.min(idx + BATCH, messages.length);",
     "      {\n        const end = Math.min(idx + BATCH, messages.length);"),

    ("the hidden replay path recurses instead of looping",
     "      } while (idx < messages.length && _hidden());",
     "      } while (false);\n"
     "      if (idx < messages.length && _hidden()) { _renderBatch(); return; }"),

    ("the trim latch is never released",
     "  function _trimNow() {\n    _trimPending = false;",
     "  function _trimNow() {"),

    ("trimming removes from the wrong end (newest go, oldest stay)",
     "    range.setStartBefore(elMessages.firstChild);\n    range.setEndBefore(elMessages.children[remove]);",
     "    range.setStartBefore(elMessages.children[n - remove]);\n    range.setEndAfter(elMessages.lastChild);"),
]

APP_MUTATIONS = [
    ("the /move account list is dispatched behind window.Move again, "
     "which a top-level const never sets (the original bug)",
     "      if (typeof Move !== 'undefined') Move.renderAccounts(msg);",
     "      if (window.Move) Move.renderAccounts(msg);"),

    ("a failed switch is reported to nobody",
     "      if (typeof Move !== 'undefined') Move.error(msg);",
     "      if (window.Move) Move.error(msg);"),

    ("the switch overlay is never closed, so it covers the session it "
     "switched to",
     "      if (typeof Move !== 'undefined') Move.close();",
     "      if (window.Move) Move.close();"),

    ("a reconnect stops telling the lobby to resubscribe",
     "      if (typeof Lobby !== 'undefined' && Lobby.onReconnect) Lobby.onReconnect();",
     "      if (window.Lobby && Lobby.onReconnect) Lobby.onReconnect();"),

    ("the restart UI is gated on a property that does not exist",
     "      if (typeof Lobby !== 'undefined' && Lobby.showReloadingAndPoll) Lobby.showReloadingAndPoll();",
     "      if (window.Lobby && Lobby.showReloadingAndPoll) Lobby.showReloadingAndPoll();"),

    ("no visibilitychange listener at all (the original bug)",
     "    document.addEventListener('visibilitychange', _onVisibilityChange);",
     ""),

    ("becoming visible does not reconnect, it only checks",
     "    if (_serverShutdown && !_retriesExhausted) return;\n    reconnect();\n  }",
     "    if (_serverShutdown && !_retriesExhausted) return;\n  }"),

    ("hiding reconnects too, so the retry storm runs unattended",
     "    if (document.hidden) return;\n",
     ""),

    ("an exhausted retry budget is not recorded, so it can never be undone",
     "      _retriesExhausted = true;\n",
     ""),

    ("a deliberate server shutdown is treated as our own impatience",
     "    if (_serverShutdown && !_retriesExhausted) return;",
     ""),

    ("the exhausted-budget exception is dropped, so giving up stays permanent",
     "    if (_serverShutdown && !_retriesExhausted) return;",
     "    if (_serverShutdown) return;"),

    ("showing reconnects over a healthy socket, sending an untyped /connect",
     "    if (ws && ws.readyState === WebSocket.OPEN) return;\n",
     ""),

    ("the exhausted flag is never cleared, so it outlives its recovery",
     "    _serverShutdown = false;\n    _retriesExhausted = false;\n    reconnectAttempt = 0;",
     "    _serverShutdown = false;\n    reconnectAttempt = 0;"),
]

RECONNECT_MUTATIONS = [
    ("the orphaned tasks are dropped in silence again (the original bug)",
     '        await self._orphan_bg_tasks("the reconnect")\n',
     ""),

    ("the registry is left populated, so bg-wait parks on a wakeup that can "
     "never come",
     "        self.state.background_tasks.clear()\n"
     "        self.state.completed_panel_bg.clear()\n",
     ""),

    ("a nameless task warns about nothing",
     '            labels.append(str(label).strip() if label else f"task {task_id}")',
     '            labels.append(str(label).strip() if label else "")'),

    ("the whole list is dumped into the warning",
     '                    len(labels), why, ", ".join(labels))\n        shown = ", ".join(labels[:5])\n        if len(labels) > 5:\n            shown += f", and {len(labels) - 5} more"\n',
     '                    len(labels), why, ", ".join(labels))\n        shown = ", ".join(labels)\n'),

    ("a reconnect with nothing running still warns",
     "        if not labels:\n            return 0\n",
     ""),

    ("/model reconnects regardless, orphaning the tasks it was told about",
     '        running = self._active_bg_tasks()\n        if not running:\n            await self.reconnect()\n            return True\n',
     '        running = self._active_bg_tasks()\n        await self.reconnect()\n        return True\n'),

    ("the deferred request is never recorded, so it is simply dropped",
     '        self._deferred_reconnect = reason\n',
     ""),

    ("the deferral is silent, so /model looks like it did nothing",
     '                f"{reason} — takes effect once the {n} running background "',
     '                f"— takes effect once the {n} running background "'),

    ("the flush ignores tasks still running, which un-does the deferral",
     '        if (self._active_bg_tasks() or self.turn_active.is_set()\n                or self.state.connecting):\n            return False\n',
     ''),

    ("nothing is ever flushed, so a deferred switch never applies",
     "        reason = self._deferred_reconnect\n        if reason is None:\n"
     "            return False\n",
     "        return False\n        reason = self._deferred_reconnect\n"
     "        if reason is None:\n            return False\n"),

    ("a reconnect does not satisfy the pending one, so it happens twice",
     "        self._deferred_reconnect = None\n"
     "        # The old CLI subprocess is about to die",
     "        # The old CLI subprocess is about to die"),

    ("/connect is deferred too, leaving no way to override the wait",
     "                reconnect_forced = True\n",
     ""),

    ("every between-turns reconnect is forced, so /model orphans again",
     "            elif reconnect_forced:\n                await self.reconnect()\n"
     "            else:\n"
     "                await self._reconnect_or_defer(reconnect_reason)",
     "            else:\n                await self.reconnect()"),

    ("the turn boundary does not flush, so the switch waits for the one after",
     "            await self._flush_deferred_reconnect()\n\n"
     "        # A /rename typed during the turn",
     "            pass\n\n        # A /rename typed during the turn"),

    ("the bg-all-done wakeup does not flush, so a parked session never applies it",
     "                await self._flush_deferred_reconnect()\n"
     "                prompt = await self._pop_queued_prompt()",
     "                prompt = await self._pop_queued_prompt()"),

    ("the context trim runs under a background task and swaps the session id",
     '        if (max_ctx > 0 and state.context_tokens > max_ctx and state.session_id\n                and not self._active_bg_tasks()):',
     '        if max_ctx > 0 and state.context_tokens > max_ctx and state.session_id:'),

    ("the trim is skipped unconditionally, disabling the rolling window",
     '                and not self._active_bg_tasks()):',
     '                and not state.session_id):'),

    ("/clear discards the tasks without saying which",
     '        await self._orphan_bg_tasks("/clear", tell_model=False)\n',
     ""),

    ("/clear leaves a deferred switch armed, so it reconnects a second time",
     "        self._deferred_reconnect = None\n"
     "        # ...and a /rename still waiting for the CLI",
     "        # ...and a /rename still waiting for the CLI"),

    ("giving up on a dead CLI leaves the registry pointing at a dead process",
     '            await self._orphan_bg_tasks("the CLI exiting for good")\n',
     ""),
]

QUEUE_MUTATIONS = [
    ("the queue is keyed by cwd alone again (the original bug)",
     '    slot = _sanitize_cwd(session_id) if session_id else "new"\n'
     '    return _orch2_state_dir() / "queues" / f"{_sanitize_cwd(resolved)}__{slot}.json"',
     '    return _orch2_state_dir() / "queues" / f"{_sanitize_cwd(resolved)}.json"'),

    ("the recorded session id is not checked on load",
     '    if data.get("session_id") != session_id:\n        return []\n    saved_at = data.get("saved_at")\n    if not isinstance(saved_at, (int, float)):\n        return []\n    if max_age_s > 0 and (time.time() - saved_at) > max_age_s:\n        return []\n    items = data.get("queue")',
     '    if False:\n        return []\n    saved_at = data.get("saved_at")\n    if not isinstance(saved_at, (int, float)):\n        return []\n    if max_age_s > 0 and (time.time() - saved_at) > max_age_s:\n        return []\n    items = data.get("queue")'),

    ("the session id is not recorded, so nothing can be checked against it",
     '            json.dump({"cwd": cwd, "session_id": session_id,\n'
     '                       "queue": lst, "saved_at": time.time()}, f)',
     '            json.dump({"cwd": cwd, "queue": lst, "saved_at": time.time()}, f)'),

    ("a queue never goes stale",
     '    if data.get("session_id") != session_id:\n        return []\n    saved_at = data.get("saved_at")\n    if not isinstance(saved_at, (int, float)):\n        return []\n    if max_age_s > 0 and (time.time() - saved_at) > max_age_s:\n        return []\n    items = data.get("queue")',
     '    if data.get("session_id") != session_id:\n        return []\n    saved_at = data.get("saved_at")\n    if not isinstance(saved_at, (int, float)):\n        return []\n    if False:\n        return []\n    items = data.get("queue")'),

    ("an undated file is treated as fresh",
     '    if data.get("session_id") != session_id:\n        return []\n    saved_at = data.get("saved_at")\n    if not isinstance(saved_at, (int, float)):\n        return []\n    if max_age_s > 0 and (time.time() - saved_at) > max_age_s:\n        return []\n    items = data.get("queue")',
     '    if data.get("session_id") != session_id:\n        return []\n    saved_at = data.get("saved_at")\n    if False:\n        return []\n    if max_age_s > 0 and (time.time() - saved_at) > max_age_s:\n        return []\n    items = data.get("queue")'),

    ("the age cap is so tight an ordinary restart loses the queue",
     "QUEUE_MAX_AGE_S = 24 * 3600",
     "QUEUE_MAX_AGE_S = 1"),

    ("a session with no id restores whatever is in the id-less slot",
     "    if session_id is None:\n"
     "        # No session, nothing to restore *into*.",
     "    if False:\n"
     "        # No session, nothing to restore *into*."),

    ("a queue is saved for a session with no identity to restore it into",
     '    if session_id is None:\n        return\n    path = queue_file_for_cwd(cwd, session_id)\n',
     '    path = queue_file_for_cwd(cwd, session_id)\n'),

    ("non-string queue entries are trusted",
     "    return [x for x in items if isinstance(x, str)]",
     "    return list(items)"),

    ("the session id is not sanitised, so it can escape the queues directory",
     '    slot = _sanitize_cwd(session_id) if session_id else "new"\n    return _orch2_state_dir() / "queues" / f"{_sanitize_cwd(resolved)}__{slot}.json"',
     '    slot = session_id if session_id else "new"\n    return _orch2_state_dir() / "queues" / f"{_sanitize_cwd(resolved)}__{slot}.json"'),
]

SERVER_QUEUE_MUTATIONS = [
    ("the queue is looked up without a session id at all",
     "        saved = load_persisted_queue(cwd, st.session_id)",
     "        saved = load_persisted_queue(cwd)"),

    ("the save captures the id once instead of reading it each time",
     "        lambda: save_persisted_queue(cwd, list(st.queued_prompts), st.session_id)",
     "        lambda _sid=st.session_id: save_persisted_queue("
     "cwd, list(st.queued_prompts), _sid)"),

    ("a broken queue file takes the whole session down with it",
     "    except Exception:\n        saved = []",
     "    except ValueError:\n        saved = []"),

    ("the runtime never attaches persistence at all",
     "        _attach_queue_persistence(st, cfg.cwd)",
     ""),

    ("startup attaches before it knows which session it is on (the ordering bug)",
     "    _attach_queue_persistence(state, config.cwd)\n\n"
     "    # If --resume was passed without an argument",
     "    # If --resume was passed without an argument"),
]

EXTAUTH_MUTATIONS = [
    ("a credential-less request counts as a failed attempt (the original bug)",
     "                    if auth or pw_param is not None:\n"
     "                        self._record_failure()",
     "                    self._record_failure()"),

    ("external access defaults back to on",
     '    enabled = cfg_access in ("on", "1", "true", "yes")',
     "    enabled = True"),

    ("on with no password silently opens up instead of refusing",
     "    if password is None:\n"
     "        return ExternalAuthPolicy(False, None, (",
     "    if False:\n"
     "        return ExternalAuthPolicy(False, None, ("),

    ("a blank/whitespace password counts as a real one",
     '    password = (cfg_pw or "").strip() or None',
     '    password = cfg_pw'),

    ("the misconfiguration is refused but never announced",
     '        return ExternalAuthPolicy(False, None, (\n'
     '            "external access is ON but no password is set',
     '        return ExternalAuthPolicy(False, None, (\n'
     '            "'),

    ("a password alone switches external access on",
     "    if not enabled:\n        return ExternalAuthPolicy(False, None, None)",
     "    if not enabled and password is None:\n"
     "        return ExternalAuthPolicy(False, None, None)"),

    ("a refused client is not told how to enable it",
     '        self.disabled_message = disabled_message or (\n'
     '            "External access is disabled on this server.\\n\\n" + EXTERNAL_HOWTO)',
     '        self.disabled_message = disabled_message or "Forbidden."'),

    ("a valid cookie no longer survives someone else's lockout",
     "                if cookie_ok:\n"
     "                    self._record_success()\n"
     "                    await self.app(scope, receive, send)\n"
     "                    return\n",
     ""),

    ("a forged cookie is accepted",
     "                cookie_ok = self._cookie_ok(headers.get(b\"cookie\", b\"\"))",
     "                cookie_ok = bool(headers.get(b\"cookie\"))"),

    ("wrong passwords stop being counted at all",
     "                    if auth or pw_param is not None:\n"
     "                        self._record_failure()",
     "                    pass"),
]

RESUMETURN_MUTATIONS = [
    ("an interrupted turn is abandoned again (the original bug)",
     '        if getattr(self.config, "resume_interrupted_turn", True):\n'
     '            kwargs["env"]["CLAUDE_CODE_RESUME_INTERRUPTED_TURN"] = "1"',
     ""),

    ("the opt-out is ignored, so it always resumes",
     '        if getattr(self.config, "resume_interrupted_turn", True):',
     "        if True:"),

    ("it defaults off, so the default install keeps the phantom prompt",
     '        if getattr(self.config, "resume_interrupted_turn", True):',
     '        if getattr(self.config, "resume_interrupted_turn", False):'),

    ("the env var name is misspelled, so the CLI ignores it",
     '            kwargs["env"]["CLAUDE_CODE_RESUME_INTERRUPTED_TURN"] = "1"',
     '            kwargs["env"]["CLAUDE_CODE_RESUME_INTERRUPTED"] = "1"'),

    ("setting it clobbers the rest of the subprocess environment",
     '            kwargs["env"]["CLAUDE_CODE_RESUME_INTERRUPTED_TURN"] = "1"',
     '            kwargs["env"] = {"CLAUDE_CODE_RESUME_INTERRUPTED_TURN": "1"}'),
]

BACKFILL_MUTATIONS = [
    ("older history is appended below instead of prepended (the original bug)",
     "    real.insertBefore(scratch.firstChild ? _drain(scratch) : document.createDocumentFragment(),\n"
     "                      real.firstChild);",
     "    real.appendChild(scratch.firstChild ? _drain(scratch) : document.createDocumentFragment());"),

    ("the backfilled nodes are never moved out of the scratch container",
     "    real.insertBefore(scratch.firstChild ? _drain(scratch) : document.createDocumentFragment(),\n"
     "                      real.firstChild);",
     ""),

    ("the viewport is not re-anchored, so the reader is thrown backwards",
     "      real.scrollTop += (real.scrollHeight - before);",
     ""),

    ("the top separator is written before the top has arrived",
     "    if (!_moreAbove) {",
     "    if (true) {"),

    ("the separator is never written at all",
     "    if (!_moreAbove) {",
     "    if (false) {"),

    # Two mutations deliberately absent here, both equivalent -- kept as a note
    # so nobody "fixes the coverage gap" by adding them back:
    #
    #  * routing history_prepend through _pendingMessages instead of straight
    #    to _prependHistory changes *when* it renders, not *what*: the queued
    #    copy is dispatched to the same function afterwards and still inserts
    #    above.  The dispatcher case is kept for directness, not correctness.
    #  * raising _replayInProgress inside _prependHistory was deleted outright.
    #    The function is synchronous, so nothing can interleave with it, and no
    #    test could distinguish the flag from its absence -- reassuring, inert,
    #    and its comment claimed a protection it could not provide.

    ("elMessages is left pointing at the scratch container",
     "      elMessages = real;\n",
     ""),
]

HISTREAD_MUTATIONS = [
    ("the tail read is uncapped again, so it reads half the file",
     "    seek_bytes = min(max_records * avg_line_bytes * 4, file_size, MAX_TAIL_BYTES)",
     "    seek_bytes = min(max_records * avg_line_bytes * 4, file_size)"),

    ("the cap is so large it may as well not exist",
     "MAX_TAIL_BYTES = 32 * 1024 * 1024",
     "MAX_TAIL_BYTES = 32 * 1024 * 1024 * 1024"),

    ("the cap is so tight no history survives",
     "MAX_TAIL_BYTES = 32 * 1024 * 1024",
     "MAX_TAIL_BYTES = 4096"),
]

STALE_MUTATIONS = [
    ("a hub can no longer tell it is running old code (the original gap)",
     "        if m > base:\n            changed.append(path.name)",
     "        if False:\n            changed.append(path.name)"),

    ("every loaded module is called stale, so the notice is permanent noise",
     "        if m > base:",
     "        if True:"),

    ("an older file counts as a change too",
     "        if m > base:",
     "        if m != base:"),

    ("the baseline is overwritten each check, so nothing is ever stale",
     "        base = _source_baseline.setdefault(key, m)",
     "        _source_baseline[key] = m\n        base = m"),

    ("a module imported after the snapshot is flagged rather than baselined",
     "        base = _source_baseline.setdefault(key, m)",
     "        base = _source_baseline.get(key, 0.0)"),

    ("third-party and stdlib files count, so a pip install cries wolf",
     "        try:\n"
     "            p = Path(f).resolve()\n"
     "            p.relative_to(root)\n"
     "        except (ValueError, OSError):\n"
     "            continue",
     "        try:\n"
     "            p = Path(f).resolve()\n"
     "        except (ValueError, OSError):\n"
     "            continue"),

    ("non-Python files count, so a CSS edit demands a server restart",
     '        if not f or not f.endswith(".py"):',
     "        if not f:"),

    ("the file list is uncapped, so a checkout floods the banner",
     '        "files": sorted(changed)[:12],',
     '        "files": sorted(changed),'),

    ("the notice cannot say how old the running code is",
     '        "started_at": _started_at,',
     '        "started_at": None,'),

    ("the check is uncached, so it stats on every lobby tick",
     "    if _stale_cache is not None and now - _stale_cache[0] < _STALE_CACHE_TTL:\n"
     "        return _stale_cache[1]",
     ""),

    ("the cache never expires, so a change is never noticed",
     "    if _stale_cache is not None and now - _stale_cache[0] < _STALE_CACHE_TTL:",
     "    if _stale_cache is not None:"),

    ("a deleted source file takes the whole check down",
     "        try:\n            m = path.stat().st_mtime\n        except OSError:\n            continue",
     "        m = path.stat().st_mtime"),

    ("the session list no longer carries it, so the lobby cannot show it",
     '        "recent": recent,\n        "stale": stale,',
     '        "recent": recent,'),

    ("the fast landing payload drops it, so it appears only after the disk scan",
     '            "recent_pending": True,\n            "stale": stale,',
     '            "recent_pending": True,'),

    ("the snapshot is never taken, so the first check baselines whatever it "
     "finds and nothing is ever stale",
     '        log.info("source snapshot: %d loaded project modules", snapshot_sources())',
     ""),
]

STALETAB_MUTATIONS = [
    ("a tab still carrying a session's name focuses itself and does nothing "
     "(the original report)",
     "    if (w === window) { window.location.href = url; return; }\n",
     ""),

    ("every target is treated as self, so no other tab is ever focused",
     "    if (w === window) { window.location.href = url; return; }",
     "    { window.location.href = url; return; }"),

    ("a notice leaves the tab wearing the name of a session it is not showing",
     "    if (!_hasSession) _setLanding(true);\n"
     "    // A notice forces the lobby in front, so whatever this tab was showing, it\n"
     "    // is not showing it now.  Give up the per-session window name: keeping it\n"
     "    // makes this tab the target of every *other* tab's \"focus the tab with\n"
     "    // this session\", which would hand them a lobby instead.\n"
     "    try { window.name = 'orch2-lobby'; } catch (e) {}",
     "    if (!_hasSession) _setLanding(true);"),

    ("an attached tab no longer claims its session name, so nothing can focus it",
     "        if (meta.session_id) window.name = 'orch2-sess-' + meta.session_id;",
     "        if (false) window.name = 'orch2-sess-' + meta.session_id;"),
]

NEWTAB_MUTATIONS = [
    ("the session title goes back to a span, so there is no way to open a "
     "second tab (the original report)",
     '        <a class="lobby-card-title" title="${_esc(title)}"',
     '        <span class="lobby-card-title" title="${_esc(title)}"'),

    ("the link has no href, which is the same as not being a link",
     "           href=\"${_esc(m.rid ? '/?rid=' + encodeURIComponent(m.rid)",
     "           data-href=\"${_esc(m.rid ? '/?rid=' + encodeURIComponent(m.rid)"),

    ("a foreign session is linked by a rid it does not have",
     "           href=\"${_esc(m.rid ? '/?rid=' + encodeURIComponent(m.rid)\n"
     "                              : _sessionHref(m.session_id || '', m.cwd || '',\n"
     "                                             m.account || ''))}\"",
     "           href=\"${_esc('/?rid=' + encodeURIComponent(m.rid || ''))}\""),

    ("the link drops the cwd and account, so opening it resolves against the "
     "wrong account",
     "    if (cwd) url += '&cwd=' + encodeURIComponent(cwd);\n"
     "    if (account) url += '&account=' + encodeURIComponent(account);\n"
     "    return url;",
     "    return url;"),

    ("a plain click follows the link as well as switching in place, so the "
     "tab navigates out from under the switch",
     "        if (_plainClick(e)) e.preventDefault(); else return;\n"
     "        const rid = card.getAttribute('data-rid');",
     "        const rid = card.getAttribute('data-rid');"),

    ("a modified click is swallowed too, so no new tab ever opens",
     "  function _plainClick(e) {\n"
     "    return !(e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button === 1);",
     "  function _plainClick(e) {\n    return true;"),

    ("every click is treated as modified, so a plain tap navigates instead of "
     "switching in place",
     "  function _plainClick(e) {\n"
     "    return !(e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button === 1);",
     "  function _plainClick(e) {\n    return false;"),

    ("ctrl-click is no longer recognised (the Windows/Linux gesture)",
     "    return !(e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button === 1);",
     "    return !(e.metaKey || e.shiftKey || e.altKey || e.button === 1);"),

    ("cmd-click is no longer recognised (the macOS gesture)",
     "    return !(e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button === 1);",
     "    return !(e.ctrlKey || e.shiftKey || e.altKey || e.button === 1);"),

    ("middle-click is no longer recognised",
     "    return !(e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button === 1);",
     "    return !(e.ctrlKey || e.metaKey || e.shiftKey || e.altKey);"),

    ("the recent list stops swallowing plain clicks",
     "        if (_plainClick(e)) e.preventDefault(); else return;\n"
     "        const sid = item.getAttribute('data-sid');",
     "        const sid = item.getAttribute('data-sid');"),

    ("the title loses the tooltip it needs when elided",
     '        <a class="lobby-card-title" title="${_esc(title)}"',
     '        <a class="lobby-card-title"'),
]

LOBBYCARD_MUTATIONS = [
    ("two nulls compare equal again, so a foreign card claims to be current "
     "(the original bug)",
     "    const isCurrent = !!m.rid && m.rid === _currentRid && !m.foreign;",
     "    const isCurrent = m.rid === _currentRid;"),

    ("a foreign card can be current as long as this tab has no session",
     "    const isCurrent = !!m.rid && m.rid === _currentRid && !m.foreign;",
     "    const isCurrent = !!m.rid && m.rid === _currentRid;"),

    ("nothing is ever current, so the badge is dead weight",
     "    const isCurrent = !!m.rid && m.rid === _currentRid && !m.foreign;",
     "    const isCurrent = false;"),

    ("the current highlight and the current badge disagree",
     "    if (isCurrent) card.classList.add('current');",
     "    if (m.rid === _currentRid) card.classList.add('current');"),

    ("the foreign tag is back in the title row, squeezing the title",
     '        ${isCurrent ? \'<span class="lobby-current-tag">current</span>\' : \'\'}\n',
     '        ${isCurrent ? \'<span class="lobby-current-tag">current</span>\' : \'\'}\n'
     "        ${foreignTag}\n"),

    ("the foreign marker is dropped entirely",
     "      ${foreignTag ? `<div class=\"lobby-card-tags\">${foreignTag}</div>` : ''}\n",
     ""),

    ("the pill goes back into the separator run, where it wrapped the line",
     "    const metaParts = [];\n",
     "    const metaParts = [];\n"
     "    if (foreignTag) metaParts.push(foreignTag);\n"),

    ("an empty tag row is emitted for every card, costing vertical margin",
     "      ${foreignTag ? `<div class=\"lobby-card-tags\">${foreignTag}</div>` : ''}\n",
     "      <div class=\"lobby-card-tags\">${foreignTag}</div>\n"),

    ("the title is truncated in the markup rather than by CSS",
     "                                             m.account || ''))}\"\n"
     "           >${_esc(title)}</a>",
     "                                             m.account || ''))}\"\n"
     "           >${_esc(title.length > 6 ? title.slice(0, 6) + '\\u2026' "
     ": title)}</a>"),

    ("an elided title has no tooltip to fall back on",
     '        <a class="lobby-card-title" title="${_esc(title)}"',
     '        <a class="lobby-card-title"'),

    ("a foreign card reports a viewer count it cannot know",
     "    if (viewers !== null) metaParts.push(`<span>${_esc(viewerLabel)}</span>`);",
     "    metaParts.push(`<span>${_esc(viewerLabel)}</span>`);"),

    ("no card reports its viewers",
     "    if (viewers !== null) metaParts.push(`<span>${_esc(viewerLabel)}</span>`);",
     ""),

    ("a known zero viewer count is mistaken for an unknown one",
     "    const viewers = typeof m.viewers === 'number' ? m.viewers : null;",
     "    const viewers = m.viewers || null;"),

    ("unknown is rendered as the idle dot again (the original bug)",
     "    const dotCls = m.busy === true ? 'busy' : (known ? 'idle' : 'unknown');",
     "    const dotCls = m.busy ? 'busy' : 'idle';"),

    ("unknown is rendered as the busy dot, crying wolf instead",
     "    const dotCls = m.busy === true ? 'busy' : (known ? 'idle' : 'unknown');",
     "    const dotCls = m.busy === false ? 'idle' : 'busy';"),

    ("a null busy counts as known, so nothing is ever unknown",
     "    const known = m.busy === true || m.busy === false;",
     "    const known = true;"),

    ("every card is unknown, so a state we do know is hedged",
     "    const known = m.busy === true || m.busy === false;",
     "    const known = false;"),

    ("the dot tooltip goes back to the two-state text",
     "        <span class=\"lobby-dot ${dotCls}\" title=\"${_esc(dotTitle)}\"></span>",
     "        <span class=\"lobby-dot ${dotCls}\" title=\"${m.busy ? 'working' : 'idle'}\"></span>"),

    ("the two kinds of not-knowing are described identically",
     "               : (m.port",
     "               : (false"),

    ("an empty rid is emitted anyway, leaving a dangling separator",
     "    if (m.rid) metaParts.push(`<span class=\"lobby-card-rid\">${_esc(m.rid)}</span>`);",
     "    metaParts.push(`<span class=\"lobby-card-rid\">${_esc(m.rid || '')}</span>`);"),

    ("the account is dropped from the meta line",
     "    if (acct) metaParts.push(`<span class=\"lobby-card-acct\">${_esc(acct)}</span>`);",
     ""),

    ("the meta parts run together with no separators at all",
     "        ${metaParts.join('<span class=\"lobby-card-sep\">·</span>')}",
     "        ${metaParts.join('')}"),

    ("a stale hub says nothing, as before",
     "    _renderStale(msg.stale);\n",
     ""),

    ("the notice is rendered but never cleared, so it outlives the restart",
     "    if (!n) {\n      if (el) el.remove();\n      return;\n    }",
     "    if (!n) {\n      return;\n    }"),

    ("a missing staleness field invents a warning",
     "    const n = (stale && stale.count) || 0;",
     "    const n = stale ? stale.count : 1;"),

    ("a fresh banner is appended on every tick",
     "    let el = document.getElementById('lobby-stale');",
     "    let el = null;"),

    ("the notice does not say which files changed",
     "      (files ? '<span class=\"lobby-stale-files\">' + _esc(files) + '</span>' : '');",
     "      '';"),

    ("the notice does not say how long the hub has been up",
     "    const age = stale.started_at ? _fmtAgo(stale.started_at) : '';",
     "    const age = '';"),

    ("a foreign card offers a close button it cannot honour",
     "        ${m.foreign ? '' : `<button class=\"lobby-card-close\" type=\"button\"",
     "        ${false ? '' : `<button class=\"lobby-card-close\" type=\"button\""),
]

MOVEUI_MUTATIONS = [
    ("the directory is never sent, so /move can only change account "
     "(the original gap)",
     "      new_name: name,\n      cwd: cwd,",
     "      new_name: name,"),

    ("the directory field is rendered but never read",
     "    const go = () => _submit(input.value, cwdInput.value);",
     "    const go = () => _submit(input.value);"),

    ("the name and the directory are swapped",
     "    const go = () => _submit(input.value, cwdInput.value);",
     "    const go = () => _submit(cwdInput.value, input.value);"),

    ("the directory starts blank instead of where the session is",
     "    cwdInput.value = _prefillCwd || _currentCwd || '';",
     "    cwdInput.value = _prefillCwd || '';"),

    ("a /move <path> argument is dropped",
     "    _prefillCwd = (prefillCwd || '').trim();",
     "    _prefillCwd = '';"),

    ("Back wipes the cached directory list",
     "    if (Array.isArray(msg.dirs)) _dirs = msg.dirs;",
     "    _dirs = msg.dirs || [];"),

    ("Back forgets where the session currently is",
     "    if (msg.current_cwd != null) _currentCwd = msg.current_cwd;",
     "    _currentCwd = msg.current_cwd || '';"),

    ("the current directory is offered as a destination",
     "    const suggestions = _dirs.filter((d) => !_samePath(d.path, _currentCwd));",
     "    const suggestions = _dirs;"),

    ("the suggestion filter compares paths as raw strings",
     "  function _samePath(a, b) {\n"
     "    const norm = (s) => String(s || '').replace(/\\\\/g, '/')\n"
     "      .replace(/\\/+$/, '').toLowerCase();\n"
     "    return norm(a) === norm(b);",
     "  function _samePath(a, b) {\n    return a === b;"),

    ("a suggestion's tooltip is elided too, so two projects can look alike",
     "html += '<button class=\"move-dir\" data-idx=\"' + i + '\" title=\"' +\n"
     "          _esc(d.path) + '\">' + _esc(_shortPath(d.path)) + '</button>';",
     "html += '<button class=\"move-dir\" data-idx=\"' + i + '\" title=\"' +\n"
     "          _esc(_shortPath(d.path)) + '\">' + _esc(_shortPath(d.path)) + '</button>';"),

    ("a suggestion fills the name field instead of the directory",
     "        cwdInput.value = suggestions[idx].path;",
     "        input.value = suggestions[idx].path;"),

    ("an empty name is accepted",
     "    if (!name) {",
     "    if (false) {"),

    ("Enter only submits from the name field",
     "    [input, cwdInput].forEach((el) => {",
     "    [input].forEach((el) => {"),
]

MOVECOPY_MUTATIONS = [
    ("a move leaves the records naming the old directory (the original gap)",
     '                if new_cwd is not None and "cwd" in rec:\n'
     '                    rec["cwd"] = new_cwd',
     "                pass"),

    ("only the first cwd record is moved -- enough to fool sniff_session_cwd, "
     "so a /cwd session ends up split across two directories",
     '                if new_cwd is not None and "cwd" in rec:\n'
     '                    rec["cwd"] = new_cwd',
     '                if new_cwd is not None and "cwd" in rec:\n'
     '                    rec["cwd"] = new_cwd\n'
     "                    new_cwd = None"),

    ("every record is given a cwd, including ones that never had one",
     '                if new_cwd is not None and "cwd" in rec:',
     "                if new_cwd is not None:"),

    ("the move is done as a blind text replacement, which falsifies the "
     "transcript along with the metadata",
     '    tmp = dest_jsonl.with_name(dest_jsonl.name + ".tmp")\n'
     '    with src_jsonl.open(encoding="utf-8", errors="replace") as fin, \\\n'
     '            tmp.open("w", encoding="utf-8") as fout:\n'
     "        for line in fin:\n"
     "            stripped = line.strip()",
     '    tmp = dest_jsonl.with_name(dest_jsonl.name + ".tmp")\n'
     '    _old_cwd = _read_session_meta(src_jsonl).get("cwd") if new_cwd else None\n'
     '    with src_jsonl.open(encoding="utf-8", errors="replace") as fin, \\\n'
     '            tmp.open("w", encoding="utf-8") as fout:\n'
     "        for line in fin:\n"
     "            if _old_cwd:\n"
     "                line = line.replace(json.dumps(_old_cwd)[1:-1],\n"
     "                                    json.dumps(new_cwd)[1:-1])\n"
     "            stripped = line.strip()"),

    ("the branch is carried into a tree it has nothing to do with",
     '                if new_branch is not None and "gitBranch" in rec:\n'
     '                    rec["gitBranch"] = new_branch',
     "                pass"),

    ('an empty branch is treated as "leave it alone"',
     "                if new_branch is not None",
     "                if new_branch"),

    ("the plain-copy shortcut is keyed on the id again, so a same-id move "
     "skips every rewrite",
     "    if not rewrite_id and new_cwd is None and new_branch is None:",
     "    if not rewrite_id:"),

    ("nothing is ever plain-copied, so an untouched copy is re-serialised",
     "    if not rewrite_id and new_cwd is None and new_branch is None:",
     "    if False:"),
]

MOVESERVER_MUTATIONS = [
    ("a copied session is never told it moved (checklist item 2)",
     "            session_note=note)",
     "            session_note=None)"),

    ("the note fires even when nothing moved, so it becomes noise",
     "    note = None\n    if dir_changed or",
     "    note = None\n    if True or"),

    ("an account-only move is treated as no move at all",
     "    note = None\n    if dir_changed or normalize_path_for_compare",
     "    note = None\n    if dir_changed and normalize_path_for_compare"),

    ("the note omits where the session came from",
     '            parts.append(f"from {cwd} to {dest_cwd}")',
     '            parts.append(f"to {dest_cwd}")'),

    ("the note drops the part that matters -- that the old paths still work",
     '            "location, which still exists — re-check any path you carry "\n'
     '            "forward from before this line. The original session is still "\n'
     '            "there and may still be running."',
     '            "location."'),

    ("the copy is filed under the old slug, where --resume will never look",
     "    slug = _sanitize_cwd(dest_cwd) if dir_changed else src_dir.name",
     "    slug = src_dir.name"),

    ("staying put recomputes the slug, relocating sessions found by the "
     "cwd-sniffing fallback",
     "    slug = _sanitize_cwd(dest_cwd) if dir_changed else src_dir.name",
     "    slug = _sanitize_cwd(dest_cwd)"),

    ("the runtime starts in the old directory",
     "            cwd=dest_cwd, resume=new_id, config_dir=target_cfg,",
     "            cwd=cwd, resume=new_id, config_dir=target_cfg,"),

    ("a destination that does not exist is accepted",
     "        if not await asyncio.to_thread(resolved.is_dir):",
     "        if False:"),

    ("a file counts as a directory",
     "        if not await asyncio.to_thread(resolved.is_dir):",
     "        if not await asyncio.to_thread(resolved.exists):"),

    ("the records are rewritten even when nothing moved",
     "                              new_cwd=dest_cwd if dir_changed else None,",
     "                              new_cwd=dest_cwd,"),

    ("the same directory spelled differently counts as a move",
     "    dir_changed = normalize_path_for_compare(dest_cwd) \\\n"
     "        != normalize_path_for_compare(cwd)",
     "    dir_changed = dest_cwd != cwd"),

    ("a blank directory is read as a request to move to the filesystem root",
     "    dest_cwd = cwd\n    if want_cwd:",
     "    dest_cwd = cwd\n    if True:"),

    ("the name is no longer required",
     "    if not new_name:",
     "    if False:"),

    ("directories that no longer exist are still offered",
     '    alive = [d for d in out if os.path.isdir(d["path"])]\n'
     "    return alive[:limit]",
     "    return out[:limit]"),

    ("the directory list is oldest first",
     '    out = sorted(best.values(), key=lambda d: d["mtime"], reverse=True)',
     '    out = sorted(best.values(), key=lambda d: d["mtime"])'),

    ("a directory seen under two accounts keeps whichever was scanned last",
     '            if prev is None or mtime > prev["mtime"]:',
     "            if True:"),

    ("one unreadable account aborts the whole scan",
     "        except Exception:\n"
     '            log.warning("switch: failed to scan projects in %s", cdir, exc_info=True)\n'
     "            continue",
     "        except Exception:\n"
     '            log.warning("switch: failed to scan projects in %s", cdir, exc_info=True)\n'
     "            raise"),

    ("the overlay is not sent the directories it is meant to offer",
     '            "dirs": dirs,\n',
     ""),

    ("the overlay is not told where the session currently is",
     '            "current_cwd": cur_cwd,\n',
     ""),
]

LOOP_MUTATIONS = [
    ("the status bar can no longer see an armed wakeup",
     "        self.state.wakeup_at = time.time() + delay",
     "        self.state.wakeup_at = None"),

    ("the deadline is published on the monotonic clock, which means nothing "
     "to a browser",
     "        self.state.wakeup_at = time.time() + delay",
     "        self.state.wakeup_at = time.monotonic() + delay"),

    ("a cancelled wakeup leaves its countdown running",
     "        self._wakeup_fire_at = None\n"
     "        self.state.wakeup_at = None\n"
     "        self.state.wakeup_defers = 0\n"
     "        clear_wakeup(self.config.cwd, self.state.session_id)\n"
     "        if t is None or t.done():",
     "        self._wakeup_fire_at = None\n"
     "        clear_wakeup(self.config.cwd, self.state.session_id)\n"
     "        if t is None or t.done():"),

    ("a fired wakeup leaves its countdown running",
     '        self.state.wakeup_at = None\n'
     '        self.state.wakeup_defers = 0\n'
     '        clear_wakeup(self.config.cwd, self.state.session_id)\n'
     '        log.info("wakeup fired: injecting scheduled prompt %r", prompt[:80])',
     '        clear_wakeup(self.config.cwd, self.state.session_id)\n'
     '        log.info("wakeup fired: injecting scheduled prompt %r", prompt[:80])'),

    ("a dropped wakeup leaves a countdown to a prompt that is never coming",
     "                self._wakeup_defers = 0\n"
     "                self.state.wakeup_at = None\n"
     "                self.state.wakeup_defers = 0\n"
     "                clear_wakeup(self.config.cwd, self.state.session_id)\n",
     "                self._wakeup_defers = 0\n"
     "                clear_wakeup(self.config.cwd, self.state.session_id)\n"),

    ("the deferral count is not published, so 'armed' implies it will fire",
     "        self.state.wakeup_defers = self._wakeup_defers",
     "        self.state.wakeup_defers = 0"),

    ("ScheduleWakeup(stop=true) is ignored again (the original bug)",
     '        if bool(inp.get("stop")):',
     "        if False:"),

    ("stop arms a fresh wakeup instead of cancelling",
     "            self._cancel_wakeup()\n"
     '            log.info("wakeup loop stopped by ScheduleWakeup(stop=true)%s",',
     "            self._arm_wakeup(WAKEUP_DEFAULT_DELAY, WAKEUP_RESOLVED_PROMPT)\n"
     '            log.info("wakeup loop stopped by ScheduleWakeup(stop=true)%s",'),

    ("any stop value counts, so stop:false ends the loop too",
     '        if bool(inp.get("stop")):',
     '        if "stop" in inp:'),

    ("the mid-turn deferral is unbounded again",
     "            if self._wakeup_defers > WAKEUP_MAX_DEFERS:",
     "            if False:"),

    ("a deferral resets its own counter, so the bound is unreachable",
     "            self._arm_wakeup(WAKEUP_MIN_DELAY, prompt, _keep_defers=True)",
     "            self._arm_wakeup(WAKEUP_MIN_DELAY, prompt)"),

    ("the bound is so tight the first wakeup landing mid-turn is simply lost",
     "            if self._wakeup_defers > WAKEUP_MAX_DEFERS:",
     "            if self._wakeup_defers > 0:"),

    ("the bound is off by one",
     "            if self._wakeup_defers > WAKEUP_MAX_DEFERS:",
     "            if self._wakeup_defers >= WAKEUP_MAX_DEFERS:"),

    ("/loop off does not actually cancel",
     "        was = self._wakeup_fire_at is not None\n"
     "        self._cancel_wakeup()",
     "        was = self._wakeup_fire_at is not None"),

    ("/loop off is not remembered, so the UI cannot tell stopped from never-armed",
     "        self._loop_stopped = True\n"
     '        log.info("wakeup loop stopped by /loop off%s",',
     '        log.info("wakeup loop stopped by /loop off%s",'),

    ("start_loop does not clamp, so a 1-second loop is accepted",
     "        delay = max(WAKEUP_MIN_DELAY, min(WAKEUP_MAX_DELAY, float(delay)))",
     "        delay = float(delay)"),

    ("loop_status always reports armed",
     '            "armed": fire_at is not None,',
     '            "armed": True,'),
]

LOOP_SERVER_MUTATIONS = [
    ("/loop off is only reported to the tab that typed it",
     "        was = bridge.stop_loop()\n"
     "        await broadcast({",
     "        was = bridge.stop_loop()\n"
     "        await send_to(ws, {"),

    ("0 is read as a duration, so /loop 0 arms a 60s loop instead of stopping",
     '    if arg in ("off", "stop", "cancel", "end", "0"):',
     '    if arg in ("off", "stop", "cancel", "end"):'),

    ("a typo falls through and arms a loop after reporting the usage",
     '                               "usage: /loop [status|off|on|<seconds>]")}})\n'
     "        return\n",
     '                               "usage: /loop [status|off|on|<seconds>]")}})\n'
     "        seconds = WAKEUP_DEFAULT_DELAY\n"),

    ("one tab asking for status interrupts every other tab",
     '        await send_to(ws, {"type": "system_msg", "subtype": "info",\n'
     '                           "data": {"message": _fmt(bridge.loop_status())}})',
     '        await broadcast({"type": "system_msg", "subtype": "info",\n'
     '                         "data": {"message": _fmt(bridge.loop_status())}})'),

    ("arming is only reported to the tab that typed it",
     "        d = bridge.start_loop(WAKEUP_DEFAULT_DELAY)\n"
     '        await broadcast({"type": "system_msg", "subtype": "info",\n'
     '                         "data": {"message": f"Wakeup loop armed: fires in {d:.0f}s."}})',
     "        d = bridge.start_loop(WAKEUP_DEFAULT_DELAY)\n"
     '        await send_to(ws, {"type": "system_msg", "subtype": "info",\n'
     '                           "data": {"message": f"Wakeup loop armed: fires in {d:.0f}s."}})'),

    ("a stopped loop reads the same as one that was never armed",
     '            return ("Wakeup loop: not armed"\n'
     '                    + (" (stopped)." if st["stopped"] else "."))',
     '            return "Wakeup loop: not armed."'),

    ("a session with the loop disabled is described as merely unarmed",
     '        if not st["enabled"]:',
     "        if False:"),

    ("clamping happens but is not disclosed",
     "    if abs(d - seconds) > 0.5:",
     "    if False:"),
]

QUEUEPANEL_MUTATIONS = [
    ("a delivered peer message never reaches the queue panel (the original "
     "report: it ran, but nothing showed)",
     "            state.queued_prompts.add_listener(self._schedule_queue_push)\n",
     ""),

    ("the coalescing latch is never set, so a batch of three costs three pushes",
     "        self._queue_push_pending = True\n",
     ""),

    ("the latch is never cleared, so the panel refreshes exactly once ever",
     "            finally:\n                self._queue_push_pending = False",
     "            finally:\n                pass"),

    ("the push is skipped when a previous one is still in flight, so the last "
     "state of a burst is the one nobody sees",
     "        if self._queue_push_pending:\n            return",
     "        return"),

    ("the panel is sent an empty queue regardless of what is in it",
     '                "queue": [{"index": i, "text": t}\n'
     "                          for i, t in enumerate(self.state.queued_prompts)],",
     '                "queue": [],'),
]

IDLE_MUTATIONS = [
    ("a sleeping phone starts the ordinary 5-minute clock again "
     "(the original report)",
     "    mobile = (rt.idle_mobile if departing is None\n"
     "              else departing in _mobile_ws)",
     "    mobile = False"),

    ("every session gets the mobile grace, so nothing is ever reaped",
     "    mobile = (rt.idle_mobile if departing is None\n"
     "              else departing in _mobile_ws)",
     "    mobile = True"),

    ("a deferral drops a mobile session onto the short desktop clock",
     "    mobile = (rt.idle_mobile if departing is None\n"
     "              else departing in _mobile_ws)",
     "    mobile = departing is not None and departing in _mobile_ws"),

    ("the mobile grace is not remembered, so a deferral cannot preserve it",
     "    rt.idle_mobile = mobile\n",
     ""),

    ("a working session is torn down mid-turn again",
     "    if busy or bg or waiting:",
     "    if False:"),

    ("background tasks do not count as work in progress",
     "    if busy or bg or waiting:",
     "    if busy:"),

    ("nothing is ever torn down, because everything counts as busy",
     "    if busy or bg or waiting:",
     "    if True:"),

    ("a deferred teardown does not re-arm, so the session is never reaped",
     "        rt.idle_timer = None\n"
     "        _maybe_start_idle_timer(rt, departing=None)\n"
     "        return",
     "        return"),


    ("every client is treated as mobile by the sniffer",
     "    return bool(_MOBILE_UA_RE.search(ua))",
     "    return True"),

    ("no client is",
     "    return bool(_MOBILE_UA_RE.search(ua))",
     "    return False"),

    ("an unreadable User-Agent takes the connection down",
     "    try:\n"
     '        ua = ws.headers.get("user-agent") or ""\n'
     "    except Exception:\n"
     "        return False",
     '    ua = ws.headers.get("user-agent") or ""'),

    ("the socket is sniffed at teardown instead of recorded at accept, when "
     "a sleeping page may no longer answer",
     "    if _ws_looks_mobile(ws):\n        _mobile_ws.add(ws)",
     "    pass"),
]

CHECKLIST_MUTATIONS = [
    # -- item 1: attributes in the listing
    ("labels are dropped at registration, so the listing is name-only again",
     "             started, now, json.dumps(labels or {})),",
     '             started, now, "{}"),'),

    ("labels are reset on every heartbeat, so they last 30 seconds",
     "            \"heartbeat_at=excluded.heartbeat_at, labels=excluded.labels\",",
     "            \"heartbeat_at=excluded.heartbeat_at\",",),

    ("the registry interprets labels instead of carrying them",
     "                     heartbeat_at=now, labels=dict(labels or {}))",
     "                     heartbeat_at=now, labels={})"),

    ("the cwd filter is ignored, so 'who owns this directory' cannot be asked",
     '            sql += " AND cwd = ?"\n            args.append(norm_path(cwd))',
     "            pass"),

    # -- item 4: delivery state
    ("whether the target was live is not recorded, only returned",
     "             frm, body, now, now + ttl, 1 if live else 0),",
     "             frm, body, now, now + ttl, 0),"),

    ("a queued message is reported as delivered",
     '        if got:\n            state = "delivered"',
     '        if True:\n            state = "delivered"'),

    ("an expired message is reported as still queued",
     '        elif row["expires_at"] <= now:\n            state = "expired"',
     '        elif False:\n            state = "expired"'),

    ("an unknown id is reported as an ordinary queued message",
     '            return {"id": message_id, "state": "unknown", "delivered_to": []}',
     '            return {"id": message_id, "state": "queued", "delivered_to": []}'),

    ("a delivered-then-expired message is reported as expired, sending the "
     "sender after a delivery that already happened",
     '        if got:\n            state = "delivered"\n'
     '        elif row["expires_at"] <= now:\n            state = "expired"',
     '        if row["expires_at"] <= now:\n            state = "expired"\n'
     '        elif got:\n            state = "delivered"'),
]

AGENTCOMMS_MUTATIONS = [
    ("a fresh session guesses an identity instead of refusing (the load-bearing row)",
     "        raise IdentityRefused(cwd, [r[\"identity\"] for r in rows])",
     "        return rows[0][\"identity\"], \"adopted\""),

    ("a LIVE identity is adopted, so two sessions share one name",
     "            if now - rows[0][\"heartbeat_at\"] <= ttl:",
     "            if False:"),

    ("registrations never expire, so auto-adoption is unsafe",
     "        sql = \"SELECT * FROM agents WHERE heartbeat_at >= ?\"\n"
     "        args: list[Any] = [now - ttl]",
     "        sql = \"SELECT * FROM agents WHERE heartbeat_at >= ?\"\n"
     "        args: list[Any] = [0]"),

    ("a resumed session does not inherit its identity",
     "        if session_id:\n"
     "            row = conn.execute(\n"
     "                \"SELECT identity FROM agents WHERE session_id = ?\",",
     "        if False:\n"
     "            row = conn.execute(\n"
     "                \"SELECT identity FROM agents WHERE session_id = ?\","),

    ("a broadcast comes back to its own sender",
     "            \"  AND m.from_identity != ? \"",
     "            \"  AND m.from_identity != coalesce(NULL, '') \""),

    ("delivery is not tracked, so a message repeats forever",
     "            \"  AND NOT EXISTS (SELECT 1 FROM deliveries d \"\n"
     "            \"                  WHERE d.message_id = m.id AND d.identity = ?) \"",
     "            \"  AND (? IS NOT NULL) \""),

    ("a repo broadcast leaks into every other repo",
     "            \"              OR (m.scope_kind = 'repo' AND m.scope_value = ?) \"",
     "            \"              OR (m.scope_kind = 'repo' AND ? IS NOT NULL) \""),

    ("messages never expire",
     "            \"WHERE m.expires_at > ? \"",
     "            \"WHERE m.expires_at > -1 AND ? IS NOT NULL \""),

    ("a halt expires on its own (spec forbids it explicitly)",
     "            \"SELECT * FROM halts WHERE lifted_at IS NULL AND (\"",
     "            \"SELECT * FROM halts WHERE lifted_at IS NULL \"\n"
     "            \"AND raised_at > strftime('%s','now') - 3600 AND (\""),

    ("a repo halt halts every repo",
     "            \"  OR (scope_kind = 'repo' AND scope_value = ?)) \"\n"
     "            \"ORDER BY raised_at LIMIT 1\",",
     "            \"  OR (scope_kind = 'repo' AND ? IS NOT NULL)) \"\n"
     "            \"ORDER BY raised_at LIMIT 1\","),

    ("a machine halt stops covering other repos",
     "            \"  scope_kind = 'machine' \"\n"
     "            \"  OR (scope_kind = 'repo' AND scope_value = ?)) \"\n"
     "            \"ORDER BY raised_at LIMIT 1\",",
     "            \"  scope_kind = 'repo' AND scope_value = ?) \"\n"
     "            \"ORDER BY raised_at LIMIT 1\","),

    # NB: an earlier attempt here mutated halt_in_force to call
    # pending_halt_for(''), which is *equivalent* -- nothing ever delivers to
    # the empty identity, so it can never be consumed.  The realistic form of
    # this bug is conflating "this agent has been told" with "the halt is
    # over", which is what the mutation below actually does.
    ("marking a halt delivered lifts it, so telling one agent un-halts everyone",
     "        conn.execute(\n"
     "            \"INSERT OR IGNORE INTO halt_deliveries (halt_id, identity, \"\n"
     "            \"delivered_at) VALUES (?,?,?)\", (halt_id, identity, now))\n"
     "        conn.commit()",
     "        conn.execute(\n"
     "            \"INSERT OR IGNORE INTO halt_deliveries (halt_id, identity, \"\n"
     "            \"delivered_at) VALUES (?,?,?)\", (halt_id, identity, now))\n"
     "        conn.execute(\"UPDATE halts SET lifted_at = ? WHERE id = ?\",\n"
     "                     (now, halt_id))\n"
     "        conn.commit()"),

    ("a halt is announced to every agent again on every poll",
     "            \"AND NOT EXISTS (SELECT 1 FROM halt_deliveries d \"\n"
     "            \"                WHERE d.halt_id = h.id AND d.identity = ?) \"",
     "            \"AND (? IS NOT NULL) \""),

    ("the registry moves into a config dir, reproducing the original defect",
     '    base = os.environ.get("LOCALAPPDATA")\n'
     '    root = Path(base) if base else (Path.home() / ".local" / "share")',
     '    base = os.environ.get("CLAUDE_CONFIG_DIR")\n'
     '    root = Path(base) if base else (Path.home() / ".claude")'),

    ("paths are not normalised, so one directory looks like several",
     "        return os.path.normcase(str(Path(p).resolve()))",
     "        return str(p)"),

    ("an invalid identity is stored rather than refused",
     "            if not _valid_identity(explicit):",
     "            if False:"),
]

MIRROR_MUTATIONS = [
    ("no mirroring, so every other account sees an EMPTY listing again",
     "                manifest[str(dest)] = str(g)\n                added += 1",
     "                pass"),

    ("the auth key is left behind, so a peer can be listed but not messaged",
     '        group = [src] + list((src_dir / "sessions").glob(f"{stem}.*.key"))',
     "        group = [src]"),

    ("dead sessions are advertised, so peers look reachable and hang",
     "            if _pid_is_a_live_claude(pid):\n                live.append((d, f))",
     "            live.append((d, f))"),

    ("any process with that pid counts, so a recycled pid keeps a ghost listed",
     '        return psutil.pid_exists(pid) and "claude" in (\n'
     '            psutil.Process(pid).name() or "").lower()',
     "        return psutil.pid_exists(pid)"),

    ("mirrors are never reaped, so exited sessions stay advertised forever",
     "        if not keep:\n            try:\n                os.unlink(dest)\n"
     "                removed += 1",
     "        if False:\n            try:\n                os.unlink(dest)\n"
     "                removed += 1"),

    ("the reaper deletes real advertisements, unregistering live sessions",
     "    for dest, src in list(manifest.items()):",
     "    for dest, src in [(str(p), '') for d in dirs "
     "for p in (d / 'sessions').glob('*.json')]:"),

    ("mirrors are re-mirrored as originals, so the manifest grows without bound",
     "            if str(f) in manifest:\n                continue",
     "            if False:\n                continue"),

    ("turning it off removes the originals too",
     "    for dest in list(manifest):",
     "    for dest in [str(p) for d in _config_dirs() "
     "for p in (d / 'sessions').glob('*')]:"),
]

AGENTTOOLS_MUTATIONS = [
    ("a messaging tool is added, which the addendum forbids",
     "    return [raise_halt_tool, lift_halt_tool]",
     "    return [raise_halt_tool, lift_halt_tool, raise_halt_tool]"),

    ("the halt tool disappears from the tool list, so no agent can find it",
     "    return [raise_halt_tool, lift_halt_tool]",
     "    return []"),

    ("the description stops saying WHEN to use it",
     '    "Use this when you need every agent quiescent before doing something that "',
     '    "This raises a halt. "'),

    ("a halt with no reason is accepted, so peers are stopped for nothing",
     '        if not reason:',
     "        if False:"),

    ("the tool no longer says the halt must be lifted",
     '    "It is deliberate and rare. It stays in force until you lift it explicitly "',
     '    "It is deliberate and rare. "'),

    ("raising does not actually raise anything",
     "        hid = ac.raise_halt(reason, by=by, scope_kind=scope, scope_value=value)",
     "        hid = 0"),

    ("lifting does not actually lift anything",
     "        n = ac.lift_halt(scope_kind=scope, scope_value=value)",
     "        n = 0"),

    ("an unknown scope is silently treated as repo",
     '    if scope not in ("repo", "machine"):\n'
     '        raise ValueError(f"scope must be \'repo\' or \'machine\', not {scope!r}")',
     '    scope = "repo" if scope not in ("repo", "machine") else scope'),
]

AGENTCLI_MUTATIONS = [
    ("check-halt exits zero even when halted (the whole contract)",
     "    print(\"=== HALT IN FORCE -- do not start this operation ===\", file=sys.stderr)",
     "    return 0\n    print(\"=== HALT IN FORCE ===\", file=sys.stderr)"),

    ("check-halt exits non-zero when clear, so callers learn to ignore it",
     "        if not args.quiet:\n"
     "            print(f\"check-halt: clear ({args.scope})\")\n"
     "        return 0",
     "        if not args.quiet:\n"
     "            print(f\"check-halt: clear ({args.scope})\")\n"
     "        return 1"),

    ("a repo check consults the machine scope, so it never sees its own halt",
     '    repo = ac.repo_key(cwd) if args.scope == "repo" else ""',
     '    repo = ""'),

    ("the refusal says nothing about who or why",
     '    print(f"reason    : {halt.reason}", file=sys.stderr)',
     ""),
]

TODOSEED_MUTATIONS = [
    ("the plan is never recovered from the transcript (the original gap)",
     "    return rendered, messages, [], last_todos_from_records(records)",
     "    return rendered, messages, [], []"),

    ("an earlier TodoWrite wins instead of the latest",
     "                if isinstance(items, list):\n                    todos = [t for t in items if isinstance(t, dict)]",
     "                if isinstance(items, list) and not todos:\n"
     "                    todos = [t for t in items if isinstance(t, dict)]"),

    ("any tool's input is treated as a todo list",
     '                    and block.get("name") == "TodoWrite"):',
     "                    and True):"),

    ("completed items are dropped, so the panel disagrees with the agent",
     "                    todos = [t for t in items if isinstance(t, dict)]",
     "                    todos = [t for t in items if isinstance(t, dict)\n"
     '                             and t.get("status") != "completed"]'),

    ("a malformed todos payload is trusted",
     "                if isinstance(items, list):",
     "                if items is not None:"),

    ("non-dict entries are kept",
     "                    todos = [t for t in items if isinstance(t, dict)]",
     "                    todos = list(items)"),

    ("the error path returns the wrong arity, breaking every caller",
     '            "is_history": True,\n        }], [], []',
     '            "is_history": True,\n        }], []'),
]

TABECHO_MUTATIONS = [
    ("a locally-echoed prompt is not broadcast at all (the original bug)",
     '    await rt.broadcast({"type": "user_message", "content": prompt},\n'
     "                       exclude=echoed_by)",
     '    if echoed_by is None:\n'
     '        await rt.broadcast({"type": "user_message", "content": prompt})'),

    ("the sender is echoed a second time",
     "                       exclude=echoed_by)",
     "                       exclude=None)"),

    ("the handler never says which socket echoed",
     "                echoed_by=ws if msg.get(\"client_echoed\") else None)",
     "                echoed_by=None)"),

    ("the client's own report is ignored, so the sender is double-echoed",
     "                echoed_by=ws if msg.get(\"client_echoed\") else None)",
     "                echoed_by=ws)"),
]

# The broadcast primitive lives in its own module, so it needs its own target:
# a sweep only mutates the one file it is pointed at, and anchors aimed at
# another file silently register as "anchor appears 0x" rather than as passes.
TABECHO_RT_MUTATIONS = [
    ("exclude is ignored, so the sender sees its prompt twice",
     "            if exclude is not None and ws is exclude:\n                continue\n",
     ""),

    # Deliberately NOT mutating ``is`` to ``==``: neither Starlette's WebSocket
    # nor the test double defines ``__eq__``, so the two are the same operation
    # and no test could ever tell them apart.  ``is`` stays because identity is
    # the intent, but pretending a sweep verifies it would be theatre.

    ("exclude swallows the whole broadcast",
     "            if exclude is not None and ws is exclude:\n                continue\n",
     "            if exclude is not None:\n                continue\n"),
]

FOREIGN_MUTATIONS = [
    ("sessions running in another hub stay under 'recent' (the original bug)",
     "            if holder is not None and sid not in mine:\n"
     "                running.append(_foreign_running_entry(\n"
     "                    sess, holder, live_by_sid.get(sid)))\n"
     "            else:\n"
     "                still_recent.append(sess)",
     "            still_recent.append(sess)"),

    ("the owning hub is never asked, so every foreign card is unknown",
     "        live_by_sid = await loop.run_in_executor(\n"
     "            None, _probe_foreign_hubs, holders)",
     "        live_by_sid = {}"),

    ("an unreachable hub is reported as idle rather than unknown "
     "(the original placeholder)",
     '        "busy": live.get("busy") if live else None,',
     '        "busy": bool(live.get("busy")),'),

    ("an unreachable hub is reported as having no viewers",
     '        "viewers": live.get("viewers") if live else None,',
     '        "viewers": live.get("viewers") or 0,'),

    ("the card cannot tell a confirmed state from a guessed one",
     '        "live_known": bool(live),',
     '        "live_known": True,'),

    ("a peer's live title is ignored, so a rename is invisible until it hits disk",
     '        "title": (live.get("title")\n'
     '                  or sess.get("title") or sess.get("first_user_msg")),',
     '        "title": sess.get("title") or sess.get("first_user_msg"),'),

    ("a peer with no title blanks out the one the disk scan resolved",
     '        "title": (live.get("title")\n'
     '                  or sess.get("title") or sess.get("first_user_msg")),',
     '        "title": live.get("title"),'),

    ("the file mtime wins over the peer's real last-turn time",
     '        "last_activity": live.get("last_activity") or sess.get("mtime", 0),',
     '        "last_activity": sess.get("mtime", 0),'),

    ("each session is probed separately instead of each hub",
     "    ports = {getattr(h, \"port\", None) for h in holders.values()}",
     "    ports = [getattr(h, \"port\", None) for h in holders.values()]"),

    ("a holder with no port is probed anyway",
     "    for port in sorted(p for p in ports if p):",
     "    for port in sorted(ports, key=lambda p: p or 0):"),

    ("a failed probe is cached as briefly as a good one, so a wedged hub "
     "is retried on every tick",
     "        ttl = _HUB_PROBE_TTL if cached is not None else _HUB_PROBE_FAIL_TTL",
     "        ttl = _HUB_PROBE_TTL"),

    ("the probe result is never cached",
     "    _hub_probe_cache[port] = (now, result)",
     ""),

    ("a good probe is cached as long as a failed one, so busy goes stale",
     "        ttl = _HUB_PROBE_TTL if cached is not None else _HUB_PROBE_FAIL_TTL",
     "        ttl = _HUB_PROBE_FAIL_TTL"),

    ("the backoff never expires, so a recovered hub stays unknown forever",
     "        if now - stamp < ttl:\n            return cached",
     "        if cached is None or now - stamp < ttl:\n            return cached"),

    ("a failed probe is indistinguishable from an empty one",
     "    result: dict[str, Any] | None = None",
     "    result: dict[str, Any] | None = {}"),

    ("the probe has no timeout, so a hung peer hangs the session list",
     "                timeout=_HUB_PROBE_TIMEOUT) as resp:",
     "                timeout=None) as resp:"),

    ("the probe leaves the machine",
     '                f"http://127.0.0.1:{port}/api/running",',
     '                f"http://0.0.0.0:{port}/api/running",'),

    ("a promoted session is not flagged, so it looks locally attachable",
     '        "foreign": True,',
     '        "foreign": False,'),

    ("promoted sessions are left in 'recent' as well, so they appear twice",
     "            else:\n                still_recent.append(sess)",
     "            still_recent.append(sess)"),

    ("our own live session can be relabelled as somebody else's",
     "            if holder is not None and sid not in mine:",
     "            if holder is not None:"),

    ("every recent session is promoted, holder or not",
     "            holder = holders.get(sid) if sid else None",
     "            holder = holders.get(sid) if sid else None\n"
     "            holder = holder or object()"),

    ("the promoted card carries no port, so there is nowhere to send the user",
     '        "port": getattr(holder, "port", None),',
     '        "port": None,'),

    ("the running list is no longer sorted after promotion",
     '        running.sort(key=lambda m: m.get("last_activity", 0), reverse=True)\n\n    return {\n        "type": "session_list",',
     '    return {\n        "type": "session_list",'),

    ("the expensive process scan is run on every lobby tick",
     "    if _holders_cache is not None:\n"
     "        stamp, cached = _holders_cache\n"
     "        if now - stamp < _HOLDERS_CACHE_TTL:\n"
     "            return cached\n",
     ""),

    ("a failing scan propagates and breaks the session list",
     "    try:\n        holders = proc_guard.map_foreign_session_holders()\n"
     "    except Exception:\n"
     '        log.warning("foreign-holder scan failed", exc_info=True)\n'
     "        holders = {}",
     "    holders = proc_guard.map_foreign_session_holders()"),
]

RESUMENOTICE_MUTATIONS = [
    ("a session resumes into a turn nobody asked for and says nothing -- "
     "the reported bug, restored",
     "        if unprompted_resume:",
     "        if False:"),

    ("every ghost turn is blamed on a resume, so a background task waking "
     "the model claims the user's turn was interrupted",
     "        unprompted_resume = self._unprompted_resume_pending",
     "        unprompted_resume = True"),

    ("the notice is never disarmed, so it repeats on every ghost turn of "
     "the connection",
     "        # One notice per connection: a later ghost turn in the same "
     "connection\n"
     "        # has some other cause (a background task waking the model).\n"
     "        self._unprompted_resume_pending = False",
     "        # One notice per connection: a later ghost turn in the same "
     "connection\n"
     "        # has some other cause (a background task waking the model).\n"
     "        pass"),

    ("a brand-new session with nothing to resume announces a recovery",
     '        self._unprompted_resume_pending = bool(kwargs.get("resume"))',
     "        self._unprompted_resume_pending = True"),

    ("sending a prompt no longer disarms it, so the user's own first turn "
     "on a resumed session is labelled a recovery",
     "        # Whatever streams from here on was asked for.\n"
     "        self._unprompted_resume_pending = False",
     "        # Whatever streams from here on was asked for.\n"
     "        pass"),

    ("the notice arrives after the UI has already flipped to working, so the "
     "output starts before the explanation",
     "        if unprompted_resume:\n"
     "            await self._announce_resumed_interrupted_turn()\n"
     "        try:\n"
     "            await self.broadcast({\n"
     '                "type": "status_update",',
     "        try:\n"
     "            await self.broadcast({\n"
     '                "type": "status_update",'),

    ("a correct recovery is styled as an error",
     '                "type": "system_msg",\n                "subtype": "info",\n'
     '                "data": {"message": (\n'
     '                    "Continuing a turn that was interrupted before this "',
     '                "type": "system_msg",\n                "subtype": "error",\n'
     '                "data": {"message": (\n'
     '                    "Continuing a turn that was interrupted before this "'),

    ("the notice stops saying the prompt did not come from the user, which "
     "was the whole question asked",
     '                    "session was last closed. Nothing was sent from here -- "\n'
     '                    "the CLI picked the unfinished turn back up on resume. "',
     '                    "session was last closed. "'),

    ("a failed notice broadcast aborts the ghost turn, so the UI sits idle "
     "while the SDK streams",
     "        except Exception as exc:\n"
     '            log.warning("resumed-turn notice broadcast failed: %r", exc)',
     "        except Exception:\n            raise"),
]


LOSTBG_MUTATIONS = [
    ("the notice is appended, so a prompt queued earlier is answered by a "
     "model that still thinks its background work is alive",
     "        self.state.queued_prompts.appendleft(text)",
     "        self.state.queued_prompts.append(text)"),

    ("a resumed session is told nothing -- the reported bug, restored",
     "        self._queue_lost_bg_notice(\n"
     '            items, "this session was cut off and has just been resumed")',
     "        pass"),

    ("a reconnect still tells only the browser, so the model keeps waiting on "
     "dead TaskOutput handles",
     '        if tell_model:\n            self._queue_lost_bg_notice(\n                items, f"this session\'s CLI was replaced ({why})")',
     '        if False:\n            pass'),

    ("the record is never written, so a torn-down session comes back knowing "
     "nothing",
     "            # One more task that would be lost if this process died now.\n"
     "            self._save_bg_tasks()",
     "            # One more task that would be lost if this process died now.\n"
     "            pass"),

    ("finished tasks stay in the record and get reported as lost -- the exact "
     "false claim that makes the notice untrustworthy",
     "        if entry is not None:\n"
     "            # One fewer task to report if this process dies now.\n"
     "            self._save_bg_tasks()",
     "        if entry is not None:\n"
     "            # One fewer task to report if this process dies now.\n"
     "            pass"),

    ("the record is not cleared when read, so every resume re-reports the "
     "same dead tasks forever",
     "        try:\n"
     "            save_persisted_bg_tasks(self.config.cwd, {}, resume_sid)\n"
     "        except Exception:\n"
     "            pass",
     "        pass"),

    ("a live orphan announcement leaves the record on disk, so the next "
     "resume reports the same loss a second time",
     "        # Reported here and now, so the on-disk record must not report the same\n"
     "        # loss again at the next resume.\n"
     "        self._save_bg_tasks()",
     "        # Reported here and now, so the on-disk record must not report the same\n"
     "        # loss again at the next resume.\n"
     "        pass"),

    # One mutation deliberately absent here, equivalent -- kept as a note so
    # nobody "fixes the coverage gap" by adding it back:
    #
    #  * deleting `_report_lost_bg_tasks`'s `if not resume_sid: return 0`
    #    changes nothing observable.  The call it guards,
    #    `load_persisted_bg_tasks(cwd, None)`, has the same gate inside and
    #    returns [] -- which is the point of putting it there (see the
    #    prompt-queue loader: "start fresh" is made safe at the one place
    #    that reads the file, not by trusting call sites).  The outer guard
    #    is an early-out, so the only thing its removal costs is a stat().
    #    The gate that actually matters is covered by the session.py target.

    ('/clear hands a freshly wiped context a notice about work it has just lost all memory of starting',
     '        await self._orphan_bg_tasks("/clear", tell_model=False)',
     '        await self._orphan_bg_tasks("/clear", tell_model=True)'),
]

LOSTBG_SESSION_MUTATIONS = [
    ("the notice claims the tasks failed, which the log disproves: two "
     "completed 1 s and 2 s into a teardown",
     '        "unknown: a task can complete in the seconds while a session is being "',
     '        "certain -- they were aborted. No task completes while a session is being "'),

    ("the model is not told to stop waiting, so it parks on a notification "
     "nobody can send",
     '        "(files written, commits made, processes still running) before "\n'
     '        "re-running anything, and do not wait on them."',
     '        "(files written, commits made, processes still running)."'),

    ("it is told to re-run blindly, losing whatever a finished task committed",
     '        "torn down. Do not assume either way. Check for their effects "',
     '        "torn down. Re-run them all now, ignoring their effects "'),

    ("durations are dropped, so \"a task was running\" replaces \"had been "
     "running for 40 minutes\"",
     '            mins = max(0, int((now - started) // 60))\n'
     '            lines.append(f"- {label} (running for {mins} min at that point)")',
     '            lines.append(f"- {label}")'),

    ("monotonic clocks are persisted raw, so every restored duration is "
     "nonsense in the new process",
     "            started_wall = now_wall - max(0.0, now_mono - started)",
     "            started_wall = started"),

    ("another session's record is read, spending a turn on work this session "
     "never started",
     '    if data.get("session_id") != session_id:\n        return []\n    saved_at = data.get("saved_at")\n    if not isinstance(saved_at, (int, float)):\n        return []\n    if max_age_s > 0 and (time.time() - saved_at) > max_age_s:\n        return []\n    items = data.get("tasks")',
     '    if False:\n        return []\n    saved_at = data.get("saved_at")\n    if not isinstance(saved_at, (int, float)):\n        return []\n    if max_age_s > 0 and (time.time() - saved_at) > max_age_s:\n        return []\n    items = data.get("tasks")'),

    ("a week-old record is restored and reported as if it just happened",
     '    if data.get("session_id") != session_id:\n        return []\n    saved_at = data.get("saved_at")\n    if not isinstance(saved_at, (int, float)):\n        return []\n    if max_age_s > 0 and (time.time() - saved_at) > max_age_s:\n        return []\n    items = data.get("tasks")',
     '    if data.get("session_id") != session_id:\n        return []\n    saved_at = data.get("saved_at")\n    if not isinstance(saved_at, (int, float)):\n        return []\n    if False:\n        return []\n    items = data.get("tasks")'),

    ("a session with no id reads the shared \"new\" slot, inheriting another "
     "session's dead tasks",
     "    if session_id is None:\n        return []\n    path = bg_tasks_file_for_cwd(cwd, session_id)",
     "    path = bg_tasks_file_for_cwd(cwd, session_id)"),

    ("an emptied registry leaves its file behind, so a session that finished "
     "its work still reports it as lost",
     "        if not items:\n"
     "            try:\n"
     "                path.unlink()\n"
     "            except FileNotFoundError:\n"
     "                pass\n"
     "            return",
     "        if not items:\n            return"),

    ("an unnamed task becomes an empty bullet the model cannot act on",
     '    return label or f"task {str(item.get(\'task_id\') or \'?\')[:12]}"',
     "    return label"),
]


GHOSTQUEUE_MUTATIONS = [
    ("a prompt is popped while a ghost turn streams -- the reported bug: "
     "echoed as You:, then the turn dies on the ghost turn's result",
     "                if self.state.busy:\n"
     '                    log.info("queue poke during a ghost turn — deferred to "\n'
     '                             "its end (%d queued)", len(self.state.queued_prompts))\n'
     "                    continue\n",
     ""),

    ("the guard asks turn_active, which a ghost turn never sets -- the exact "
     "confusion that caused the bug",
     "                if self.state.busy:\n"
     '                    log.info("queue poke during a ghost turn — deferred to "',
     "                if self.turn_active.is_set():\n"
     '                    log.info("queue poke during a ghost turn — deferred to "'),

    ('the poke that makes declining safe is removed, so a prompt deferred past a ghost turn sits in the queue forever -- worse than sending it early',
     '                self.event_queue.put_nowait(("wakeup", "queue-edit-done"))',
     '                pass'),

    ("the ghost turn never reports itself finished, so every later turn waits "
     "out the full timeout",
     "        state.active_tools.clear()\n        self._ghost_settled.set()",
     "        state.active_tools.clear()"),

    ("a streaming ghost turn is not recorded, so run_turn's guard never fires",
     "        state.turn_started_at = time.monotonic()\n"
     "        self._ghost_settled.clear()",
     "        state.turn_started_at = time.monotonic()"),

    ("run_turn stops waiting for a streaming ghost turn, so any other path in "
     "still eats its result",
     "        await self._await_ghost_settled()\n",
     ""),

    ("the wait gives up after the usual few seconds, which is shorter than "
     "every ghost turn that matters (the reported one ran 1159 s)",
     "    async def _await_ghost_settled(self, timeout: float = 1800.0) -> None:",
     "    async def _await_ghost_settled(self, timeout: float = 5.0) -> None:"),

    ("a ghost turn that never terminates wedges every later turn, not just "
     "the one that waited for it",
     "        finally:\n"
     "            # Either way the wait is over; a stale clear must not delay every\n"
     "            # future turn.\n"
     "            self._ghost_settled.set()",
     "        finally:\n"
     "            pass"),
]


BGSTALL_MUTATIONS = [
    ("the quiet clock runs from the *older* signal, so a task whose output "
     "file is still growing is called stalled -- the bug the first draft had",
     '    quiet_since = max(\n        entry.get("output_changed_at", now),\n        entry.get("cpu_changed_at", now),\n        entry.get("io_changed_at", now),\n    )',
     '    quiet_since = min(\n        entry.get("output_changed_at", now),\n        entry.get("cpu_changed_at", now),\n        entry.get("io_changed_at", now),\n    )'),

    ("a task we merely lost track of is declared stalled, releasing every "
     "gate holding a session that may well be working",
     '    if not entry.get("proc_known"):\n        return QUIET\n',
     ''),

    # One mutation deliberately absent here, equivalent -- kept as a note so
    # nobody "fixes the coverage gap" by adding it back:
    #
    #  * deleting stall_state's `if not entry.get("stall_probed")` guard
    #    changes nothing observable.  An unprobed entry has no
    #    output_changed_at/cpu_changed_at either, so the `.get(..., now)`
    #    defaults already make quiet_for() zero and the state WORKING.  The
    #    guard stays because it names the invariant -- we do not judge a task
    #    we have not looked at -- and would keep that true if a future caller
    #    populated the timestamps without probing.

    ("CPU that goes *down* counts as progress, so a sampling wobble keeps a "
     "hung task looking healthy forever",
     "    if cpu_s is not None and (prev is None or cpu_s > prev):",
     "    if cpu_s is not None and (prev is None or cpu_s != prev):"),

    ("nothing is ever quiet for long enough, so a hung task pins the session "
     "exactly as before",
     '    if quiet_for(entry, now=now) < stall_after:\n        return WORKING',
     '    if True:\n        return WORKING'),

    ("a quiet task releases the gates too, so a session that is working but "
     "whose process we cannot see gets reaped",
     "    return stall_state(entry, now=now, stall_after=stall_after) != STALLED",
     "    return stall_state(entry, now=now, stall_after=stall_after) == WORKING"),

    ("the filter returns everything, so a proven-hung task keeps its hold",
     "    return {\n"
     "        tid: entry for tid, entry in (tasks or {}).items()\n"
     "        if is_load_bearing(entry, now=now, stall_after=stall_after)\n"
     "    }",
     "    return dict(tasks or {})"),

    ("the note states a conclusion we cannot prove instead of what we saw",
     '        return f"no output, CPU or disk I/O for {age}"',
     '        return "hung"'),

    ("the quiet note claims the CPU half we could not read",
     '    return f"no output for {age}"',
     '    return f"no output or CPU for {age}"'),

    ("long stalls read as an ever-growing minute count",
     "    if mins < 60:\n"
     '        return f"{mins}m"\n'
     '    return f"{mins // 60}h{mins % 60:02d}m"',
     '    return f"{mins}m"'),

    ("a negative age reaches the panel when the clock is read out of order",
     "    return max(0.0, now - quiet_since)",
     "    return now - quiet_since"),

    ("a command too short to identify anything is matched anyway, so the "
     "wrong process's CPU keeps a hung task looking healthy",
     "    if len(needle) < 12:",
     "    if False:"),

    ("an inner wrapper is measured instead of the outermost, so work done by a sibling branch of the task's own process tree is invisible",
     '        return min(hits, key=lambda p: p.create_time())',
     '        return max(hits, key=lambda p: p.create_time())'),

    ("quoting and wrapping defeat the match again, so the CLI's rewritten command line identifies nothing and every task stays unknown",
     '        if needle in _normalise_cmd(cmdline):',
     '        if needle in cmdline:'),

    ("one unreadable process aborts the whole scan, so nothing is ever "
     "identified on a busy tree",
     "        try:\n"
     "            cmdline = \" \".join(proc.cmdline() or [])\n"
     "        except Exception:\n"
     "            continue",
     "        cmdline = \" \".join(proc.cmdline() or [])"),

    ("the output path stops matching the CLI's layout, so no task ever has "
     "output information and the CPU half decides alone",
     '    return (base / "claude" / _sanitize(resolved) / str(session_id)\n'
     '            / "tasks" / f"{task_id}.output")',
     '    return (base / "claude" / str(session_id)\n'
     '            / "tasks" / f"{task_id}.output")'),

    ("a missing output file raises instead of reporting nothing",
     "    try:\n"
     "        st = path.stat()\n"
     "    except OSError:\n"
     "        return None",
     "    st = path.stat()"),

    ("the stall bar drifts away from the idle grace, so the two disagree "
     "about what counts as quiet",
     "STALL_AFTER_S = 300.0",
     "STALL_AFTER_S = 30.0"),

    ("only the task's shell is measured, so a task whose child is compiling reads as zero CPU and gets declared stalled",
     '    try:\n        children = proc.children(recursive=True)\n    except Exception:\n        return cpu_total, io_total\n    for child in children:\n        c_cpu, c_io = _sample(child)\n        cpu_total += c_cpu\n        io_total += c_io\n    return cpu_total, io_total',
     '    return cpu_total, io_total'),

    ("a process whose io_counters are denied loses its whole sample, so a readable process becomes 'unknown' and a hung task holds on forever",
     '        try:\n            io = p.io_counters()\n            ops = int(io.read_count) + int(io.write_count)\n        except Exception:\n            pass',
     '        io = p.io_counters()\n        ops = int(io.read_count) + int(io.write_count)'),

    ('an unreadable process reports zero CPU instead of none, which is indistinguishable from a hung one and releases the gates',
     "    try:\n        proc.cpu_times()          # the liveness check: "
     "can we read it at all?\n    except Exception:\n        return None",
     "    try:\n        proc.cpu_times()          # the liveness check: "
     "can we read it at all?\n    except Exception:\n        pass"),

    ('disk I/O is ignored, so a task copying gigabytes with near-zero CPU is declared stalled mid-copy',
     '    prev_io = entry.get("io_ops")\n    if io_ops is not None and (prev_io is None or io_ops > prev_io):\n        entry["io_changed_at"] = now\n',
     ''),

    ('an I/O counter that goes *down* counts as progress, so a process leaving the tree makes a hung task look alive',
     '    if io_ops is not None and (prev_io is None or io_ops > prev_io):',
     '    if io_ops is not None and (prev_io is None or io_ops != prev_io):'),

    ('I/O is measured in bytes, so a task re-reading the same cached page looks idle',
     '            ops = int(io.read_count) + int(io.write_count)',
     '            ops = int(io.read_bytes) + int(io.write_bytes)'),
]

BGSTALLWIRE_MUTATIONS = [
    ("a stalled task is dropped from the panel, hiding the hung process that "
     "the panel was the only thing reporting",
     "    bg_tasks_list = []\n    _mono = time.monotonic()\n"
     "    for task_id, info in state.background_tasks.items():",
     "    bg_tasks_list = []\n    _mono = time.monotonic()\n"
     "    for task_id, info in bg_stall.active_tasks(\n"
     "            state.background_tasks, now=time.monotonic()).items():"),

    ("the panel stops saying anything is wrong, which is the whole report",
     '            "stall_note": bg_stall.describe_stall(info, now=_mono),',
     '            "stall_note": None,'),

    ("the kill button is offered for tasks with no process to kill",
     '            "killable": bool(info.get("pid")),',
     '            "killable": True,'),
]


BTW_MUTATIONS = [
    ("a /btw typed during a turn waits behind every prompt already queued -- "
     "the reported bug, restored",
     "        for text in reversed(btw_prompts):\n"
     "            state.queued_prompts.appendleft(text)",
     "        for text in btw_prompts:\n"
     "            state.queued_prompts.append(text)"),

    ("two asides typed in one turn come out backwards",
     "        for text in reversed(btw_prompts):",
     "        for text in btw_prompts:"),

    ("the aside is collected and then never queued at all, so it is silently "
     "dropped instead of merely late",
     "        for text in reversed(btw_prompts):\n"
     "            state.queued_prompts.appendleft(text)\n",
     ""),

    ("an aside is queued as it arrives rather than collected, which reverses "
     "two of them and puts them behind a prompt queued later in the drain",
     '            elif kind == "btw":\n'
     "                # Collected, not queued here: they go to the *front* below, and\n"
     "                # pushing each one as it arrives would reverse two asides typed\n"
     "                # in one turn.\n"
     "                btw_prompts.append(payload)",
     '            elif kind == "btw":\n'
     "                state.queued_prompts.appendleft(payload)"),
]


BTWFORK_MUTATIONS = [
    ("the aside is appended to the live conversation instead of a fork -- the "
     "exact interference the feature exists to prevent, now landing mid-turn",
     '        "fork_session": True,',
     '        "fork_session": False,'),

    ("it forks nothing, so the aside has none of the context it was asked "
     "about",
     '        "resume": state.session_id,',
     '        "resume": None,'),

    ("the fork runs against the wrong account, where the session does not "
     "exist",
     '    if getattr(config, "config_dir", None):\n'
     '        kwargs["env"]["CLAUDE_CONFIG_DIR"] = config.config_dir\n',
     ""),

    ("the fork can run Bash, so an aside can rebuild or commit things while "
     "the real turn is mid-edit on the same tree",
     '    "Bash",\n',
     ""),

    ("the fork can edit files concurrently with the live turn",
     '    "Write",\n    "Edit",\n',
     ""),

    ("the fork loses Read, so it can only guess about the code it is asked "
     "about",
     "BTW_DISALLOWED_TOOLS = [",
     'BTW_DISALLOWED_TOOLS = [\n    "Read",'),

    ("the fork stops to ask a permission nobody is watching for, hanging the "
     "aside forever",
     '        "permission_mode": "bypassPermissions",',
     '        "permission_mode": "default",'),

    ("the fork resumes the interrupted turn it can see in its history, with "
     "its tools disabled, instead of answering",
     '    kwargs["env"]["CLAUDE_CODE_RESUME_INTERRUPTED_TURN"] = "0"',
     '    kwargs["env"]["CLAUDE_CODE_RESUME_INTERRUPTED_TURN"] = "1"'),

    ("the fork is told nothing, so it reads its transcript, decides a job is "
     "in progress, and tries to do it",
     "    return BTW_PREAMBLE + question.strip()",
     "    return question.strip()"),

    ("a session with no transcript claims it can fork, so the aside is asked "
     "of an empty conversation instead of falling back",
     '    return bool(getattr(state, "session_id", None))',
     "    return True"),
]


BTWUI_MUTATIONS = [
    ("the answer replaces itself on every block, so only the last fragment of "
     "a streamed reply is ever shown",
     "    target.textContent += msg.text || '';",
     "    target.textContent = msg.text || '';"),

    ("a delta for an id that is gone throws, killing the message dispatch for "
     "everything after it",
     "    const target = _btwEls.get(msg.id || '');\n    if (!target) return;",
     "    const target = _btwEls.get(msg.id || '');"),

    ("the side exchange renders as an ordinary message, so a fork's answer "
     "reads as something this session said",
     "    el.className = 'msg msg-btw pending';",
     "    el.className = 'msg msg-system';"),

    ("nothing tells the reader the session never saw it",
     '<span class="btw-note">side question — forked conversation, not seen by this session</span>',
     '<span class="btw-note"></span>'),

    ("it claims to still be thinking after it has answered",
     "    if (el) el.classList.remove('pending');",
     "    if (el) el.classList.add('pending');"),

    ("a fork that failed renders as an empty block, reading as 'it had "
     "nothing to say'",
     "      target.textContent = `(no answer — ${msg.error})`;",
     "      target.textContent = '';"),

    ("a failure after partial output is swallowed, so a truncated answer looks "
     "complete",
     "      warn.textContent = `(ended early — ${msg.error})`;\n      el.appendChild(warn);",
     "      warn.textContent = `(ended early — ${msg.error})`;"),

    ("every aside writes into the first one's element",
     "    _btwEls.set(id, el.querySelector('.btw-answer'));",
     "    if (!_btwEls.size) _btwEls.set(id, el.querySelector('.btw-answer'));"),

    ("the question is dropped, leaving an answer with nothing to answer",
     '<div class="btw-question">${_esc(msg.question || \'\')}</div>',
     '<div class="btw-question"></div>'),
]


FORKSKIP_MUTATIONS = [
    ("a /btw fork counts as a foreign holder, so a session looks held "
     "elsewhere for the seconds an aside takes to answer -- and this scan is "
     "what stops a session opening",
     "            if _is_session_fork(argv):\n"
     "                continue\n"
     "            started = \"\"",
     "            started = \"\""),

    ("the fork skip swallows the real holder as well, so two agents can drive "
     "one session file",
     '    return "--fork-session" in argv',
     "    return True"),

    ("nothing is ever treated as a fork, restoring the false positive",
     '    return "--fork-session" in argv',
     "    return False"),

    ("the holder map still lists forks, so the lobby shows the session "
     "running somewhere it is not",
     "            if _is_session_fork(argv):\n"
     "                continue          # reads it, does not hold it\n",
     ""),

    ("the flag is matched as a substring of the whole line rather than an "
     "argument, so a path or prompt mentioning it disables the guard",
     '    return "--fork-session" in argv',
     '    return "--fork-session" in " ".join(argv)'),
]


WAKEUPIDLE_MUTATIONS = [
    ("a session waiting on a wakeup is reaped between loop iterations, "
     "cancelling the loop outright -- the reported bug, restored",
     "    if busy or bg or waiting:",
     "    if busy or bg:"),

    ("a stale past wakeup pins the runtime forever on a schedule nothing will "
     "honour",
     "    waiting = isinstance(wake, (int, float)) and wake > time.time()",
     "    waiting = isinstance(wake, (int, float))"),

    ("any session at all is treated as waiting, so nothing is ever reaped",
     "    waiting = isinstance(wake, (int, float)) and wake > time.time()",
     "    waiting = True"),

    ("the teardown reason is never recorded, so every stale tab falls back to "
     "blaming a restart that may not have happened",
     "    _record_teardown(rt, reason)\n",
     ""),

    # One mutation deliberately absent here, equivalent -- kept as a note
    # so nobody "fixes the coverage gap" by adding it back:
    #
    #  * moving `_record_teardown(rt, reason)` below `runtimes.pop(...)`
    #    changes nothing observable.  Popping the registry entry does not
    #    touch `rt.state`, so the title and session id the recorder reads
    #    are still there either way.  The call sits early for readability;
    #    an earlier comment claimed the position was load-bearing, which
    #    the sweep disproved.

    ("a stale tab is told a guess even when the real reason is known",
     "    rec = _teardown_reasons.get(rid)\n    if rec is None:",
     "    rec = None\n    if rec is None:"),

    ("the record grows without bound",
     "    while len(_teardown_reasons) > _TEARDOWN_MEMORY:\n"
     "        _teardown_reasons.popitem(last=False)",
     "    pass"),

    ("the newest record is evicted instead of the oldest",
     "        _teardown_reasons.popitem(last=False)",
     "        _teardown_reasons.popitem(last=True)"),

    ('an idle reap is reported as an ordinary close, hiding the five-minute rule the user did not know about',
     '    await _teardown_runtime(\n        rt, reason=f"was closed after {timeout // 60} minutes with no tab "\n                   f"connected")',
     '    await _teardown_runtime(rt)'),
]


WAKESTORE_MUTATIONS = [
    ("a wakeup that went stale overnight fires anyway, running an unattended "
     "turn on a plan from eight hours ago",
     "    if late <= budget:\n        return Plan(FIRE",
     "    if True:\n        return Plan(FIRE"),

    ("nothing ever fires, so restoring a loop silently does nothing -- the "
     "reported bug wearing a nicer hat",
     "    if late <= budget:",
     "    if False:"),

    ("the freshness cap is gone, so an 18-day-old record can still run a turn",
     '    if max_age_s > 0 and (now - float(rec["saved_at"])) > max_age_s:\n'
     '        return Plan(DISCARD, 0.0, "older than the freshness cap")\n',
     ""),

    ("a record with no prompt or no session is treated as restorable",
     "    if not _valid(rec):\n"
     '        return Plan(DISCARD, 0.0, "not a usable record")\n',
     ""),

    ("a future wakeup is treated as overdue and fires immediately instead of "
     "waiting out its remaining time",
     "    if due > now:\n        return Plan(ARM, due - now",
     "    if False:\n        return Plan(ARM, due - now"),

    ("the lateness budget loses its floor, so a fast loop is declared stale by "
     "a restart that took a few seconds",
     "    return max(MIN_LATE_S, min(MAX_LATE_S, interval))",
     "    return min(MAX_LATE_S, interval)"),

    ("the lateness budget loses its ceiling, so a six-hour loop may fire six "
     "hours late",
     "    return max(MIN_LATE_S, min(MAX_LATE_S, interval))",
     "    return max(MIN_LATE_S, interval)"),

    # One mutation deliberately absent here, equivalent -- kept as a note so
    # nobody "fixes the coverage gap" by adding it back:
    #
    #  * deleting `if interval <= 0: interval = MIN_LATE_S` changes nothing.
    #    The `max(MIN_LATE_S, ...)` on the next line already floors a negative
    #    interval to exactly MIN_LATE_S.  The guard stays because it names the
    #    case (a corrupt or hand-edited record) at the point it arises, rather
    #    than leaving a reader to work out that the floor covers it.

    ("a record is restored into whatever session shares its slot, running a "
     "turn in the wrong conversation",
     '        if wakeup_file_for(rec["cwd"], rec["session_id"]).name != path.name:\n'
     "            continue\n",
     ""),

    ("one corrupt file costs every other loop its schedule",
     "        except (OSError, ValueError):\n            continue",
     "        except OSError:\n            continue"),

    ("records come back in arbitrary order, so the resurrection cap revives "
     "whichever the filesystem listed first rather than the soonest due",
     '    recs.sort(key=lambda r: r.get("due_at", 0.0))\n',
     ""),

    ("clearing a fired wakeup does nothing, leaving a record that runs the "
     "same turn again on the next start",
     "    try:\n        wakeup_file_for(cwd, session_id).unlink()\n"
     "    except (OSError, FileNotFoundError):\n        pass",
     "    pass"),

    ("a session with no id, or a wakeup with no prompt, is written to disk "
     "anyway",
     '    if not session_id or not prompt or not isinstance(due_at, (int, float)):\n'
     "        return\n",
     ""),
]

WAKEREVIVE_MUTATIONS = [
    ("a stale record is thrown away instead of being left for the session to "
     "surface when opened",
     "        if plan.action == wakeup_store.PAUSED:",
     "        if False:"),

    ("the resurrection cap is gone, so one boot can spawn a CLI per record on "
     "a machine that has exhausted its commit limit before",
     "        if revived >= wakeup_store.MAX_RESURRECT:",
     "        if False:"),

    ("a session that is already running is resurrected a second time, putting "
     "two bridges on one conversation",
     "        if sid in live_sids:",
     "        if False:"),

    ("an overdue wakeup fires the instant the process comes up, before any tab "
     "has reconnected to see it",
     "            else WAKEUP_RESTORE_SETTLE_S",
     "            else 0.0"),

    ("the session never says why it woke up, so a turn starts by itself with "
     "no explanation",
     "    asyncio.create_task(rt.broadcast({",
     "    asyncio.create_task(_noop_broadcast({"),
]


LOOPDROP_MUTATIONS = [
    ("a dropped loop goes back to being a log line nobody reads, and the "
     "countdown just vanishes from the status bar",
     "                await self._announce_wakeup_dropped()\n",
     ""),

    ("the notice stops saying how to start the loop again",
     '                    f"is done."',
     '                    f"is done.".replace("/loop", "the command")'),

    ("every deferral is announced, turning one recoverable situation into ten "
     "messages",
     "            if self._wakeup_defers > WAKEUP_MAX_DEFERS:",
     "            if True:"),

    ("a failed notice escapes the timer task, where it becomes an asyncio "
     "warning nobody reads rather than anything actionable",
     "        except Exception as exc:\n"
     '            log.warning("wakeup-dropped notice failed: %r", exc)',
     "        except Exception:\n            raise"),
]


MODELLIVE_MUTATIONS = [
    ("/model answers from the cache again, so a model released mid-hour stays "
     "invisible -- the reported bug, restored",
     "        if kind == \"model-show\":\n"
     "            await _refresh_models_for_show()\n",
     ""),

    ("the refresh is fired and forgotten, so the list rendered a line later is "
     "still the old one",
     "            await _refresh_models_for_show()",
     "            asyncio.create_task(_refresh_models_for_show())"),

    ("a hung API hangs the command instead of costing it the budget",
     "            await asyncio.wait_for(\n"
     "                asyncio.to_thread(fetch_available_models,\n"
     "                                  MODEL_SHOW_FETCH_BUDGET),\n"
     "                MODEL_SHOW_FETCH_BUDGET + 1.0,\n"
     "            )",
     "            await asyncio.to_thread(fetch_available_models,\n"
     "                                    MODEL_SHOW_FETCH_BUDGET)"),

    ("a failed refresh propagates instead of falling back to the list we "
     "already have",
     "        except asyncio.TimeoutError:",
     "        except _NeverRaised:"),

    ("concurrent /model from several tabs each fetch, so the lock only makes "
     "three requests sequential instead of parallel",
     "        age = model_cache_age()\n"
     "        if age is not None and age <= MODEL_SHOW_COALESCE_S:\n"
     "            return\n",
     ""),

    ("the coalescing window grows to cache-TTL scale and starts hiding "
     "releases again",
     "MODEL_SHOW_COALESCE_S = 2.0",
     "MODEL_SHOW_COALESCE_S = 1800.0"),
]

MODELFRESH_MUTATIONS = [
    ("\"live\" goes back to meaning \"the cache has not aged out\", which was "
     "True in exactly the case that misled",
     "    return \"live\" if age <= MODEL_LIST_FRESH_S else \"cache\"",
     "    return \"live\""),

    ("a stale cached list is reported as the built-in fallback, so the picker "
     "cries wolf about a perfectly real list",
     "    if age is None:\n        return \"builtin\"",
     "    if age is not None:\n        return \"builtin\""),

    ("the freshness window widens to the cache TTL and the distinction "
     "collapses",
     "MODEL_LIST_FRESH_S = 60.0",
     "MODEL_LIST_FRESH_S = 3600.0"),

    ("a clock that moved backwards renders as \"fetched -3 minutes ago\"",
     "    return max(0.0, time.monotonic() - _model_cache_at)",
     "    return time.monotonic() - _model_cache_at"),

    ("a never-fetched list reports an age, so the picker shows the built-in "
     "list as though it had come from somewhere",
     "    if _model_cache is None:\n        return None\n"
     "    return max(0.0, time.monotonic() - _model_cache_at)",
     "    return max(0.0, time.monotonic() - _model_cache_at)"),
]


CLIPATH_MUTATIONS = [
    ("the override is ignored, so a model newer than the SDK's pinned CLI is "
     "refused again -- the reported bug, restored",
     '                kwargs["cli_path"] = cli_path\n',
     ""),

    ("a typo in the path makes the session unable to connect at all, instead "
     "of falling back to a CLI that at least works",
     "            if os.path.exists(cli_path):",
     "            if True:"),

    ("the bundled CLI is overridden with nothing when the option is unset, so "
     "the SDK's own resolution never runs",
     '        cli_path = getattr(self.config, "cli_path", None)\n'
     "        if cli_path:",
     '        cli_path = getattr(self.config, "cli_path", None)\n'
     "        if True:"),

    ("a missing binary is swallowed, so \"my new model is still refused\" "
     "comes with nothing to explain it",
     "                log.warning(\n"
     '                    "--cli-path %s does not exist — falling back to the "\n'
     '                    "bundled CLI; models newer than it will be refused",\n'
     "                    cli_path)",
     "                pass"),
]

CLIPATHCFG_MUTATIONS = [
    ("the hub's --cli-path never reaches a session",
     "        cli_path=os.path.abspath(args.cli_path) if args.cli_path else None,\n",
     ""),

    ("a relative --cli-path handed to a running hub is read against the hub's "
     "directory, not the one it was typed in",
     "        cli_path=os.path.abspath(args.cli_path) if args.cli_path else None,\n",
     "        cli_path=args.cli_path,\n"),

    ("the option help stops naming the error it solves, so nobody hitting "
     "that error can find it",
     '            "Agent SDK. The SDK pins a CLI version, so a model released after "\n'
     '            "that pin is rejected with \'does not support this model; version "',
     '            "Agent SDK. "\n'
     '            "\'"'),
]


RESUMEFAIL_MUTATIONS = [
    ("the requested resume is spent before the connect is known to have worked "
     "again, so a failed first attempt silently becomes a blank session -- the "
     "general hazard the report exposed",
     "        # Consumed at the bottom of this method, once the connect succeeds.\n",
     "        # Consumed at the bottom of this method, once the connect succeeds.\n"
     "        self._initial_resume_id = None\n"),

    ("a missing session is retried like a transient failure, ten times over "
     "several minutes of backoff",
     "                _unknown_session = _is_unknown_session_error(_why)",
     "                _unknown_session = False"),

    ("a missing session is recognised but still retried",
     "                if _unknown_session:\n                    _fatal = True\n",
     ""),

    ("the bogus id survives as the session id, so /rename looks for \"OS A\" "
     "on disk -- the reported symptom",
     "                        if state.session_id == requested:\n"
     "                            state.session_id = None\n",
     ""),

    ("the user is told nothing about which sessions do exist",
     '                                f"session\'s title. {listing} Open one of those "',
     '                                f"session\'s title. Open one of those "'),

    ("nothing is recognised as a missing session",
     '    "does not match any session title",\n'
     '    "requires a valid session id or session title",\n',
     ""),

    ("every connect failure is taken for a missing session, so a timeout gives "
     "up on a session the next attempt would have opened",
     "    return any(m in t for m in _UNKNOWN_SESSION_MARKERS)",
     "    return True"),

    ("after /clear the new session's first turn warns that prior context may "
     "not be loaded -- a false alarm on the one occasion a new session is the "
     "point",
     "            self.state.expected_resume_sid = None\n"
     "\n"
     "        # A resumed session can start streaming with no prompt from us; arm",
     "\n"
     "        # A resumed session can start streaming with no prompt from us; arm"),
]

RESUMELIST_MUTATIONS = [
    ("untitled sessions vanish from the listing",
     "    if untitled:\n        parts.append(f\"{untitled} untitled\")\n",
     ""),

    ("an empty directory is described as though it held sessions",
     "    if not sessions:\n"
     "        return \"There are no sessions in this directory to resume.\"\n",
     ""),

    ("the listing reads the hub's account instead of the session's, so a "
     "cross-account session's siblings are never shown",
     "    project = claude_projects_dir(config_dir) / _sanitize_cwd(resolved)",
     "    project = claude_projects_dir() / _sanitize_cwd(resolved)"),

    ("the newest sessions are no longer the ones shown",
     "    found.sort(key=lambda x: x[1], reverse=True)",
     "    found.sort(key=lambda x: x[1])"),
]

# The name other Claude sessions address this one by (ListAgents /
# SendMessage), and /rename reaching the live CLI.  tests/test_session_name.py.
#
# Not mutated, because equivalent: the explicit ``kind == "cli-command"``
# branches in _await_next_prompt and _between_turns' drain.  An unknown kind is
# already dropped by both loops, which then reach the same flush; the branches
# exist to say so, not to change what happens.
SESSIONNAME_MUTATIONS = [
    ("the name is never passed to the CLI, so every session keeps its auto "
     "name -- the reported 'still named os-f5'",
     '            kwargs["env"]["CLAUDE_CODE_SESSION_NAME"] = name\n',
     "            pass\n"),

    ("a name the hub inherited from whatever launched it reaches every "
     "unnamed session",
     '        os.environ.pop("CLAUDE_CODE_SESSION_NAME", None)\n',
     ""),

    ("a blank title is passed as the name",
     "        return (st.agent_name or st.pending_rename or st.human_title\n"
     '                or "").strip() or None',
     "        return (st.agent_name or st.pending_rename or st.human_title\n"
     "                or None)"),

    ("a /rename made before the session existed never names the CLI",
     "        return (st.agent_name or st.pending_rename or st.human_title\n",
     "        return (st.agent_name or st.human_title\n"),

    ("a session's own --agent-name is not its address, so the two registries "
     "disagree about who it is",
     "        return (st.agent_name or st.pending_rename or st.human_title\n",
     "        return (st.pending_rename or st.human_title\n"),

    ("a hub restart, which resumes through the initial resume id, brings the "
     "CLI up without its name",
     "        rid = resume_id or self._initial_resume_id\n        if rid:\n"
     "            return rid\n",
     "        rid = resume_id\n        if rid:\n            return rid\n"),

    ("connect never reads the title, so a restarted CLI is nameless",
     "        if target != self.state.human_title_sid:",
     "        if False:"),

    ("connect reads the title only when none is known, so a second session in "
     "the tab inherits the first one's name",
     "        if target != self.state.human_title_sid:",
     "        if self.state.human_title is None:"),

    ("every /model reconnect re-reads the transcript, and a rename made in "
     "this runtime is overwritten by what is on disk",
     "        if target != self.state.human_title_sid:",
     "        if True:"),

    ("a failed read of the title is never retried",
     "                # Left unmatched, so the next connect tries again.\n"
     "                self.state.human_title = None\n",
     "                self.state.human_title = None\n"
     "                self.state.human_title_sid = target\n"),

    ("the worker is not woken for a rename, so it waits for the next prompt",
     '            self.event_queue.put_nowait(("cli-command", ""))\n'
     "        except Exception:\n"
     '            log.debug("could not poke the worker for a CLI command",',
     "            pass\n"
     "        except Exception:\n"
     '            log.debug("could not poke the worker for a CLI command",'),

    ("the first of two renames wins",
     "        self._pending_cli_command = text\n        try:\n",
     "        self._pending_cli_command = self._pending_cli_command or text\n"
     "        try:\n"),

    ("a rename is dropped, not kept, when the CLI is not free for it",
     "        text = self._pending_cli_command\n        if not text:\n"
     "            return\n",
     "        text = self._pending_cli_command\n"
     "        self._pending_cli_command = None\n"
     "        if not text:\n            return\n"),

    ("a rename is sent into a running turn, where the CLI can absorb it",
     "        if (self.client is None or state.busy or state.connecting\n",
     "        if (self.client is None or state.connecting\n"),

    ("a rename is sent into a connect in progress",
     "        if (self.client is None or state.busy or state.connecting\n",
     "        if (self.client is None or state.busy\n"),

    ("a rename is sent while the stream is already claimed",
     "                or self.turn_active.is_set() or self._transport_dead\n",
     "                or self._transport_dead\n"),

    ("a rename is written to a CLI already known to be dead",
     "                or self.turn_active.is_set() or self._transport_dead\n",
     "                or self.turn_active.is_set()\n"),

    ("a rename is sent into a turn the CLI has announced but not yet shown "
     "output for -- a background task's notification waking the model",
     "                or self.turn_active.is_set() or self._transport_dead\n"
     '                or self._cli_state == "running"):\n',
     "                or self.turn_active.is_set() or self._transport_dead):\n"),

    ("a turn's result does not end its 'running', so every rename made after "
     "a real turn waits indefinitely -- the CLI's 'idle' lands in the turn's "
     "queue and is thrown away (found end-to-end against CLI 2.1.280)",
     '                    # the end of the turn it announced as "running".\n'
     '                    self._cli_state = "idle"\n',
     '                    # the end of the turn it announced as "running".\n'),

    ("the exchange's own result does not end its 'running', so a second "
     "rename waits indefinitely",
     "                    # queue we are about to stop reading.\n"
     '                    self._cli_state = "idle"\n',
     "                    # queue we are about to stop reading.\n"),

    ("the CLI's own report that it started a turn is ignored",
     "            if session_state:\n"
     "                self._cli_state = session_state\n",
     ""),

    ("the CLI going idle does not wake a worker that deferred a rename, which "
     "then waits for the next prompt",
     '            if session_state == "idle" and self._pending_cli_command:\n',
     "            if False:\n"),

    ("the stream is not claimed, so the CLI's reply renders as a ghost turn",
     "        self.turn_active.set()\n        reply = \"\"\n",
     "        reply = \"\"\n"),

    ("the stream is never released, so the next ghost turn's output goes to a "
     "queue nobody reads",
     "        finally:\n            self.turn_active.clear()\n"
     "        for m in handoff:\n",
     "        finally:\n            pass\n        for m in handoff:\n"),

    ("the CLI's own reply is taken for the model and rendered as a turn",
     '                        and getattr(msg, "model", None) == self._CLI_SYNTHETIC_MODEL):',
     '                        and getattr(msg, "model", None) == "never"):'),

    ("a model turn that got to the CLI first is swallowed as the reply",
     "                if (isinstance(msg, AssistantMessage)\n"
     '                        and getattr(msg, "model", None) == self._CLI_SYNTHETIC_MODEL):',
     "                if isinstance(msg, AssistantMessage):"),

    ("only the first message of a colliding turn is rendered; what was queued "
     "behind it is lost",
     "                handoff.append(msg)\n"
     "                while not self.turn_msg_queue.empty():\n",
     "                handoff.append(msg)\n"
     "                while False:\n"),

    ("a CLI that dies during the exchange is never reconnected",
     '            self.event_queue.put_nowait(("connect", _CONNECT_TRANSPORT_DEAD))\n'
     "            return\n        if not handoff:\n",
     "            return\n        if not handoff:\n"),

    ("an idle, parked worker never runs the rename",
     "            await self._run_pending_cli_command()\n"
     "            kind, payload = await self.event_queue.get()\n",
     "            kind, payload = await self.event_queue.get()\n"),

    ("a rename typed during a turn waits behind the next queued prompt",
     "        # is not a turn, so it goes before anything that starts one.\n"
     "        await self._run_pending_cli_command()\n",
     "        # is not a turn, so it goes before anything that starts one.\n"),

    ("the end of a ghost turn does not wake the worker for a waiting rename",
     "        if self._pending_cli_command:\n            try:\n"
     '                self.event_queue.put_nowait(("cli-command", ""))\n'
     "            except Exception as exc:\n"
     '                log.warning("ghost-turn CLI-command poke failed',
     "        if False:\n            try:\n"
     '                self.event_queue.put_nowait(("cli-command", ""))\n'
     "            except Exception as exc:\n"
     '                log.warning("ghost-turn CLI-command poke failed'),

    ("/clear keeps a waiting rename, which then names the new session",
     "        # being wiped, and would otherwise be run against the new one.\n"
     "        self._pending_cli_command = None\n",
     "        # being wiped, and would otherwise be run against the new one.\n"),
]

SESSIONNAME_CMD_MUTATIONS = [
    ("/rename sets only the title; the live CLI is never told",
     "        forward_to_sdk=True,\n"
     '        forward_payload=f"/rename {new_title}",\n'
     "    )\n\n\ndef _cmd_export",
     "    )\n\n\ndef _cmd_export"),

    ("/rename before the first turn is not handed to the CLI already running",
     "            forward_to_sdk=True,\n"
     '            forward_payload=f"/rename {new_title}",\n'
     "        )\n    try:\n",
     "        )\n    try:\n"),

    ("the new title is not recorded as the name, so the next reconnect drops it",
     "    state.human_title = new_title\n    state.human_title_sid = sid\n",
     "    state.human_title_sid = sid\n"),

    ("the new title is not tied to its session, so the next reconnect re-reads "
     "the transcript over it",
     "    state.human_title = new_title\n    state.human_title_sid = sid\n",
     "    state.human_title = new_title\n"),

    ("a rename during a turn claims to have taken effect already",
     '    when = " once the current turn ends" if state.busy else ""',
     '    when = ""'),

    ("the reply does not say the name is now an address",
     'f"(other sessions will address it by this name{when})"',
     'f"({when})"'),
]

SESSIONNAME_SERVER_MUTATIONS = [
    ("/rename goes through the prompt path: echoed, queued, mergeable into "
     "the next prompt, and run as a turn",
     '                if kind == "rename":\n'
     "                    await _forward_cli_command(rt, result.forward_payload)",
     "                if False:\n"
     "                    await _forward_cli_command(rt, result.forward_payload)"),

    ("a session with no CLI is poked anyway",
     '    if st is None or br is None or getattr(br, "client", None) is None:\n'
     "        return\n"
     '    if getattr(st, "connect_blocked_msg", None):\n',
     "    if st is None or br is None:\n"
     "        return\n"
     '    if getattr(st, "connect_blocked_msg", None):\n'),

    ("a session blocked on a duplicate is poked anyway",
     '    if getattr(st, "connect_blocked_msg", None):\n'
     "        return\n"
     "    br.request_cli_command(text)",
     "    br.request_cli_command(text)"),

    ("a launch that joins a running hub drops --cli-path",
     '        "cli_path": cfg.cli_path,\n',
     ""),

    ("the hand-over leaves --cli-path off the wire",
     '        "cli_path": cli_path,\n        "agent_name": agent_name,\n',
     '        "agent_name": agent_name,\n'),

    ("the hub opens the session on its own CLI, not the one asked for",
     "                                   config_dir=config_dir, bell_on=bell_on,\n"
     "                                   cli_path=cli_path, agent_name=agent_name,\n",
     "                                   config_dir=config_dir, bell_on=bell_on,\n"
     "                                   agent_name=agent_name,\n"),
]

# --resume <title> into a running hub, and two sessions sharing a name.
# tests/test_resume_by_title.py.
#
# Not mutated: the resolver call in the hub's own startup (lifespan).  Driving
# lifespan installs the process reaper and connects a real CLI; the call is one
# line, and what it calls is covered here directly.
RESUMETITLE_MUTATIONS = [
    ("part of a title counts as the title, so --resume 'Lane A' opens 'OS "
     "Lane A'",
     "            if t and t.strip().casefold() == want]\n",
     "            if t and want in t.strip().casefold()]\n"),

    ("the title has to be typed in the same case",
     '    want = (title or "").strip().casefold()\n',
     '    want = (title or "").strip()\n'),

    ("a title two sessions carry is resolved to one of them, silently",
     "    return matches[0][0] if len(matches) == 1 else ref\n",
     "    return matches[0][0] if matches else ref\n"),

    ("only the six newest sessions are searched",
     "    return [(sid, t) for sid, t in resumable_sessions(cwd, config_dir, limit=None)\n",
     "    return [(sid, t) for sid, t in resumable_sessions(cwd, config_dir)\n"),

    ("titles are never resolved -- the reported bug",
     "    matches = sessions_titled(ref, cwd, config_dir)\n"
     "    return matches[0][0] if len(matches) == 1 else ref\n",
     "    return ref\n"),
]

RESUMETITLE_SERVER_MUTATIONS = [
    ("a launch into a running hub keeps the title as the session id -- the "
     "reported bug",
     "    if resume:\n        resume = await asyncio.to_thread(\n"
     "            resolve_session_ref, resume, cwd or config.cwd, config_dir)\n",
     ""),

    ("the title is looked up in the hub's directory, not the launch's",
     "            resolve_session_ref, resume, cwd or config.cwd, config_dir)\n",
     "            resolve_session_ref, resume, config.cwd, config_dir)\n"),

    ("the title is looked up in the hub's account, not the launch's",
     "            resolve_session_ref, resume, cwd or config.cwd, config_dir)\n",
     "            resolve_session_ref, resume, cwd or config.cwd, None)\n"),
]

RESUMETITLE_BRIDGE_MUTATIONS = [
    ("a title several sessions carry is retried ten times as if transient",
     '    "requires a valid session id or session title",\n'
     "    _AMBIGUOUS_SESSION_MARKER,\n)",
     '    "requires a valid session id or session title",\n)'),

    ("a title several sessions carry is reported as no session at all",
     "                        if _AMBIGUOUS_SESSION_MARKER in (_why or \"\").lower():\n",
     "                        if False:\n"),

    ("a closing session deletes the registration another session now holds",
     "                lambda: agent_comms.deregister(ident, session_id=sid))\n",
     "                lambda: agent_comms.deregister(ident))\n"),

    ("a vanished registration is never restored, and the session goes on "
     "believing it is registered",
     "            if not alive:\n",
     "            if False:\n"),

    ("a vanished registration is re-resolved, and can come back under a "
     "different name",
     "                            self.agent_identity)\n"
     "                await self._republish_agent()\n",
     "                            self.agent_identity)\n"
     "                self.agent_identity = None\n"
     "                await self.register_agent()\n"),

    ("a second live session takes the name in silence",
     "        if how == \"explicit\":\n"
     "            await self._warn_if_name_in_use(ident)\n",
     ""),

    ("a session reconnecting is warned about itself",
     "        if held is None or held.session_id in (\"\", self.state.session_id or \"\"):\n",
     "        if held is None:\n"),

    ("a holder that stopped heartbeating long ago counts as live",
     "        if time.time() - held.heartbeat_at > agent_comms.AGENT_TTL:\n"
     "            return\n",
     ""),
]

RESUMETITLE_COMMS_MUTATIONS = [
    ("deregistering deletes the entry whoever holds it now",
     "                \"DELETE FROM agents WHERE identity = ? AND session_id IN (?, '')\",\n"
     "                (identity, session_id))",
     "                \"DELETE FROM agents WHERE identity = ?\",\n"
     "                (identity,))"),

    ("an entry registered before its session had an id can never be removed",
     "session_id IN (?, '')",
     "session_id = ?"),
]

# --agent-name names one session, not the hub.  tests/test_agent_name_per_session.py.
AGENTNAME_MUTATIONS = [
    ("the identity comes from the environment again, which every session in "
     "the hub shares",
     '        explicit = (self.state.agent_name or "").strip() or None\n',
     '        explicit = (self.state.agent_name or os.environ.get("ORCH2_AGENT_NAME")'
     ' or "").strip() or None\n'),

    ("a name the registry refuses vanishes into the log again",
     "        except agent_comms.AgentCommsError as exc:\n",
     "        except ValueError as exc:\n"),

    ("the refusal names no spelling that would work",
     "                    f\"{re.sub(r'[^A-Za-z0-9._-]+', '-', explicit or '')!r} \"\n",
     "                    f\"{explicit!r} \"\n"),

    ("a session reopened without the flag is never looked up, so it loses its "
     "name on the first reopen",
     "                and target and target != self._agent_name_checked\n",
     "                and False\n"),

    ("a named session takes the record's name, and an old name beats the one "
     "it was launched with",
     "            if remembered and not self.state.agent_name:\n",
     "            if remembered:\n"),

    ("the name is looked up again on every reconnect",
     "                and target and target != self._agent_name_checked\n",
     "                and target\n"),

    ("a remembered name is found but not used",
     "            if remembered and not self.state.agent_name:\n"
     "                self.state.agent_name = remembered\n",
     "            if remembered and not self.state.agent_name:\n"
     "                pass\n"),

    ("the name is never recorded, so nothing is remembered",
     "            await asyncio.to_thread(\n"
     "                lambda: agent_comms.name_session(sid, name, labels=labels))\n",
     "            pass\n"),

    ("the record is rewritten on every tick",
     "        if self._agent_name_recorded == key:\n            return\n",
     ""),

    ("what was recorded is not noted, so the record is rewritten on every tick",
     "            self._agent_name_recorded = key\n",
     ""),

    ("a name the registry refused is not remembered either",
     "        await self._remember_agent_name()\n"
     "        if not self._agent_enabled() or not self.agent_identity:\n",
     "        if not self._agent_enabled() or not self.agent_identity:\n"),

    ("naming a running session leaves it on its old identity, which keeps "
     "the inbox",
     "        if self.agent_identity and self.agent_identity != name:\n"
     "            await self.deregister_agent()\n",
     ""),

    ("naming a running session is not remembered until the next tick",
     "        self.state.agent_name = name\n        await self._remember_agent_name()\n",
     "        self.state.agent_name = name\n"),

    ("the running CLI is not told, so ListAgents changes only at the next "
     "reconnect",
     '        self.request_cli_command(f"/rename {name}")\n',
     ""),

    # --agent-label
    ("the registry entry carries the hub's labels, not the session's",
     '        out.update(getattr(self.state, "agent_labels", None) or {})\n',
     '        out.update(getattr(self.config, "agent_labels", None) or {})\n'),

    ("a session reopened without flags loses its labels",
     "            if remembered_labels and not self.state.agent_labels:\n",
     "            if False:\n"),

    ("remembered labels overwrite the ones this launch gave",
     "            if remembered_labels and not self.state.agent_labels:\n",
     "            if remembered_labels:\n"),

    ("a session launched with only a name never gets its labels back",
     "        if ((not self.state.agent_name or not self.state.agent_labels)\n",
     "        if ((not self.state.agent_name)\n"),

    ("a label change is not remembered",
     "        key = (sid, name, tuple(sorted(labels.items())))\n",
     "        key = (sid, name)\n"),

    ("labels are never recorded",
     "                lambda: agent_comms.name_session(sid, name, labels=labels))\n",
     "                lambda: agent_comms.name_session(sid, name))\n"),

    ("a session given only labels is not remembered",
     "        if not (name or labels) or not sid or not self._agent_enabled():\n",
     "        if not name or not sid or not self._agent_enabled():\n"),

    ("new labels for a running session are not published",
     "        if not self.agent_identity or not self._agent_enabled():\n"
     "            return\n        ident = self.agent_identity\n",
     "        return\n        ident = self.agent_identity\n"),

    ("new labels for a running session are not remembered",
     "        self.state.agent_labels = labels\n        await self._remember_agent_name()\n",
     "        self.state.agent_labels = labels\n"),
]

AGENTNAME_SERVER_MUTATIONS = [
    ("every session the hub opens inherits the hub's name -- the bug",
     '    overrides["agent_name"] = (agent_name or "").strip() or None\n',
     ""),

    ("a launch that joins a running hub drops --agent-name",
     '        "agent_name": cfg.agent_name,\n',
     ""),

    ("the hand-over leaves the name off the wire",
     '        "agent_name": agent_name,\n        "agent_labels": agent_labels or {},\n',
     '        "agent_labels": agent_labels or {},\n'),

    ("the hub opens the handed-over session unnamed",
     "                                   cli_path=cli_path, agent_name=agent_name,\n",
     "                                   cli_path=cli_path,\n"),

    ("naming a session that is already open does nothing",
     "                await existing.bridge.adopt_agent_name(agent_name)\n",
     "                pass\n"),

    ("the same name again re-registers a running session",
     "                    and existing.state.agent_name != agent_name\n",
     ""),

    ("--cli-path for a session that is already open is recorded but never "
     "applied",
     "                    existing.bridge.config = new_cfg\n"
     "                need_reconnect = True\n",
     "                    existing.bridge.config = new_cfg\n"),

    ("the runtime and its bridge disagree about which CLI it runs",
     "                if existing.bridge is not None:\n"
     "                    existing.bridge.config = new_cfg\n",
     ""),

    ("a detached child loses a name that came from the environment",
     "    if cfg.agent_name:\n"
     '        child_argv.extend(["--agent-name", cfg.agent_name])\n',
     ""),

    ("a detached child is named twice",
     '        if a == "--agent-name":\n            i += 2\n            continue\n'
     '        if a.startswith("--agent-name="):\n            i += 1\n            continue\n'
     "        child_argv.append(a)\n",
     "        child_argv.append(a)\n"),

    ("a restart re-applies the name the launch command once said",
     '        if a in ("--agent-name", "--agent-label"):\n',
     '        if a in ("--agent-label",):\n'),

    ("a restart re-applies the labels the launch command once gave",
     '        if a in ("--agent-name", "--agent-label"):\n',
     '        if a in ("--agent-name",):\n'),

    ("a restart re-applies labels given in --agent-label=K=V form",
     '        if a.startswith(("--agent-name=", "--agent-label=")):\n',
     '        if a.startswith(("--agent-name=",)):\n'),

    ("a restart brings the primary session back unlabelled",
     '    for key, value in (getattr(state, "agent_labels", None) or {}).items():\n'
     '        child.extend(["--agent-label", f"{key}={value}"])\n',
     ""),

    ("every session the hub opens inherits the hub's labels",
     '    overrides["agent_labels"] = dict(agent_labels or {})\n',
     ""),

    ("a launch that joins a running hub drops --agent-label",
     '        "agent_labels": dict(cfg.agent_labels or {}),\n',
     ""),

    ("the hand-over leaves the labels off the wire",
     '        "agent_labels": agent_labels or {},\n',
     ""),

    ("the hub opens the handed-over session unlabelled",
     "                                   agent_labels=agent_labels)",
     "                                   )"),

    ("labels that are not a JSON object are passed on as they are",
     "                    if isinstance(raw_labels, dict) else {})\n",
     "                    if isinstance(raw_labels, dict) else raw_labels)\n"),

    ("labels for a session that is already open are ignored",
     "                await existing.bridge.adopt_agent_labels(agent_labels)\n",
     "                pass\n"),

    ("the same labels again re-publish a running session",
     "                    and existing.state.agent_labels != agent_labels\n",
     ""),

    ("a restart brings the primary session back unnamed",
     "    if name:\n"
     '        child.extend(["--agent-name", name])\n',
     ""),
]

AGENTNAME_CFG_MUTATIONS = [
    ("ORCH2_AGENT_NAME stays in the environment, so every session the hub "
     "starts inherits it",
     '    env = os.environ.pop("ORCH2_AGENT_NAME", None)\n',
     '    env = os.environ.get("ORCH2_AGENT_NAME")\n'),

    ("ORCH2_AGENT_NAME is ignored",
     '    return (flag or "").strip() or (env or "").strip() or None\n',
     '    return (flag or "").strip() or None\n'),

    ("the variable beats the flag",
     '    return (flag or "").strip() or (env or "").strip() or None\n',
     '    return (env or "").strip() or (flag or "").strip() or None\n'),

    ("a blank flag hides the variable",
     '    return (flag or "").strip() or (env or "").strip() or None\n',
     '    return (flag or env or "").strip() or None\n'),
]

AGENTNAME_COMMS_MUTATIONS = [
    ("a second name for a session does not replace the first",
     '"name=excluded.name, named_at=excluded.named_at, "',
     '"named_at=excluded.named_at, "'),

    ("a session given only labels is not remembered",
     "    if not sid or not (nm or lab):\n",
     "    if not sid or not nm:\n"),

    ("a blank name and no labels are recorded anyway",
     "    if not sid or not (nm or lab):\n",
     "    if not sid:\n"),

    ("a second set of labels does not replace the first",
     '"labels=excluded.labels",',
     '"named_at=excluded.named_at",'),

    ("remembered labels are never read back",
     '    return (row["name"] or None), {str(k): str(v) for k, v in labels.items()}\n',
     '    return (row["name"] or None), {}\n'),

    ("a labels-only record reads as a session named ''",
     '    return (row["name"] or None), {str(k): str(v) for k, v in labels.items()}\n',
     '    return row["name"], {str(k): str(v) for k, v in labels.items()}\n'),

    ("a registry from before labels were remembered cannot record them",
     '        if "labels" not in cols:\n',
     "        if False:\n"),
]

AGENTNAME_STATE_MUTATIONS = [
    ("the session a launch opens does not start with the launch's name",
     '        agent_name=getattr(config, "agent_name", None),\n',
     ""),

    ("the session a launch opens does not start with the launch's labels",
     '        agent_labels=dict(getattr(config, "agent_labels", None) or {}),\n',
     ""),
]

AGENTNAME_CMD_MUTATIONS = [
    ("/rename in a named session renames the CLI away from its agent name",
     "    if state.agent_name:\n"
     "        # A session with an explicit name keeps it:",
     "    if False:\n"
     "        # A session with an explicit name keeps it:"),

    ("the same before the session exists",
     "        if state.agent_name:\n            return CommandResult(\n",
     "        if False:\n            return CommandResult(\n"),
]

SESSIONNAME_DISK_MUTATIONS = [
    ("an AI summary becomes the name other sessions address this one by",
     "    return _apply_rename_pin(str(jsonl), custom) or None\n\n\n"
     "def title_from_jsonl",
     "    return _apply_rename_pin(str(jsonl), custom) or _ai\n\n\n"
     "def title_from_jsonl"),

    ("the name ignores the rename pin, so it differs from the title the tab "
     "shows",
     "    return _apply_rename_pin(str(jsonl), custom) or None\n\n\n"
     "def title_from_jsonl",
     "    return custom or None\n\n\ndef title_from_jsonl"),
]


TARGETS = {
    "chat": ("static/chat.js", "tests/hidden_window.test.js",
             CHAT_MUTATIONS, "node"),
    "movecmd": ("static/commands.js", "tests/reconnect_on_show.test.js",
                MOVECMD_MUTATIONS, "node"),
    "panelstoggle": ("static/status.js", "tests/reconnect_on_show.test.js",
                     PANELSTOGGLE_MUTATIONS, "node"),
    "statusloop": ("static/status.js", "tests/reconnect_on_show.test.js",
                   STATUSLOOP_MUTATIONS, "node"),
    "chat-modal": ("static/chat.js", "tests/reconnect_on_show.test.js",
                   CHATMODAL_MUTATIONS, "node"),
    "app": ("static/app.js", "tests/reconnect_on_show.test.js",
            APP_MUTATIONS, "node"),
    "reconnect": ("sdk_bridge.py", "tests/test_reconnect_bg_tasks.py",
                  RECONNECT_MUTATIONS, "pytest"),
    "queue": ("session.py", "tests/test_queue_persistence.py",
              QUEUE_MUTATIONS, "pytest"),
    "queue-server": ("server.py", "tests/test_queue_persistence.py",
                     SERVER_QUEUE_MUTATIONS, "pytest"),
    "resumeturn": ("sdk_bridge.py", "tests/test_resume_interrupted_turn.py",
                   RESUMETURN_MUTATIONS, "pytest"),
    "resumenotice": ("sdk_bridge.py", "tests/test_resumed_turn_notice.py",
                     RESUMENOTICE_MUTATIONS, "pytest"),
    "lostbg": ("sdk_bridge.py", "tests/test_lost_bg_tasks.py",
               LOSTBG_MUTATIONS, "pytest"),
    "ghostqueue": ("sdk_bridge.py", "tests/test_prompt_during_ghost_turn.py",
                   GHOSTQUEUE_MUTATIONS, "pytest"),
    "bgstall": ("bg_stall.py", "tests/test_bg_stall.py",
                BGSTALL_MUTATIONS, "pytest"),
    "btw": ("sdk_bridge.py", "tests/test_queue_drain.py",
            BTW_MUTATIONS, "pytest"),
    "btwfork": ("btw.py", "tests/test_btw_fork.py",
                BTWFORK_MUTATIONS, "pytest"),
    "btwui": ("static/chat.js", "tests/btw_exchange.test.js",
              BTWUI_MUTATIONS, "node"),
    "forkskip": ("proc_guard.py", "tests/test_proc_guard.py",
                 FORKSKIP_MUTATIONS, "pytest"),
    "wakeupidle": ("server.py", "tests/test_idle_teardown.py",
                   WAKEUPIDLE_MUTATIONS, "pytest"),
    "wakestore": ("wakeup_store.py", "tests/test_wakeup_store.py",
                  WAKESTORE_MUTATIONS, "pytest"),
    "loopdrop": ("sdk_bridge.py", "tests/test_loop_control.py",
                 LOOPDROP_MUTATIONS, "pytest"),
    "modellive": ("server.py", "tests/test_model_list.py",
                  MODELLIVE_MUTATIONS, "pytest"),
    "modelfresh": ("config.py", "tests/test_model_list.py",
                   MODELFRESH_MUTATIONS, "pytest"),
    "clipath": ("sdk_bridge.py", "tests/test_cli_path.py",
                CLIPATH_MUTATIONS, "pytest"),
    "clipath-cfg": ("config.py", "tests/test_cli_path.py",
                    CLIPATHCFG_MUTATIONS, "pytest"),
    "resumefail": ("sdk_bridge.py", "tests/test_resume_unknown_session.py",
                   RESUMEFAIL_MUTATIONS, "pytest"),
    "resumelist": ("session.py", "tests/test_resume_unknown_session.py",
                   RESUMELIST_MUTATIONS, "pytest"),
    "sessionname": ("sdk_bridge.py", "tests/test_session_name.py",
                    SESSIONNAME_MUTATIONS, "pytest"),
    "sessionname-cmd": ("commands.py", "tests/test_session_name.py",
                        SESSIONNAME_CMD_MUTATIONS, "pytest"),
    "sessionname-server": ("server.py",
                           "tests/test_session_name.py tests/test_cli_path.py",
                           SESSIONNAME_SERVER_MUTATIONS, "pytest"),
    "sessionname-disk": ("session.py", "tests/test_session_name.py",
                         SESSIONNAME_DISK_MUTATIONS, "pytest"),
    "resumetitle": ("session.py", "tests/test_resume_by_title.py",
                    RESUMETITLE_MUTATIONS, "pytest"),
    "resumetitle-server": ("server.py", "tests/test_resume_by_title.py",
                           RESUMETITLE_SERVER_MUTATIONS, "pytest"),
    "resumetitle-bridge": ("sdk_bridge.py", "tests/test_resume_by_title.py",
                           RESUMETITLE_BRIDGE_MUTATIONS, "pytest"),
    "resumetitle-comms": ("agent_comms.py", "tests/test_resume_by_title.py",
                          RESUMETITLE_COMMS_MUTATIONS, "pytest"),
    "agentname": ("sdk_bridge.py", "tests/test_agent_name_per_session.py",
                  AGENTNAME_MUTATIONS, "pytest"),
    "agentname-server": ("server.py", "tests/test_agent_name_per_session.py",
                         AGENTNAME_SERVER_MUTATIONS, "pytest"),
    "agentname-cfg": ("config.py", "tests/test_agent_name_per_session.py",
                      AGENTNAME_CFG_MUTATIONS, "pytest"),
    "agentname-comms": ("agent_comms.py", "tests/test_agent_name_per_session.py",
                        AGENTNAME_COMMS_MUTATIONS, "pytest"),
    "agentname-state": ("state.py", "tests/test_agent_name_per_session.py",
                        AGENTNAME_STATE_MUTATIONS, "pytest"),
    "agentname-cmd": ("commands.py", "tests/test_agent_name_per_session.py",
                      AGENTNAME_CMD_MUTATIONS, "pytest"),
    "wakerevive": ("server.py", "tests/test_wakeup_store.py",
                   WAKEREVIVE_MUTATIONS, "pytest"),
    "bgstall-wire": ("state.py", "tests/test_bg_stall.py",
                     BGSTALLWIRE_MUTATIONS, "pytest"),
    "lostbg-session": ("session.py", "tests/test_lost_bg_tasks.py",
                       LOSTBG_SESSION_MUTATIONS, "pytest"),
    "backfill": ("static/chat.js", "tests/history_backfill.test.js",
                 BACKFILL_MUTATIONS, "node"),
    "histread": ("session.py", "tests/test_history_read.py",
                 HISTREAD_MUTATIONS, "pytest"),
    "stale": ("server.py", "tests/test_stale_sources.py",
              STALE_MUTATIONS, "pytest"),
    "staletab": ("static/lobby.js", "tests/lobby_running_card.test.js",
                 STALETAB_MUTATIONS, "node"),
    "newtab": ("static/lobby.js",
               "tests/lobby_running_card.test.js tests/reconnect_on_show.test.js",
               NEWTAB_MUTATIONS, "node"),
    "lobbycard": ("static/lobby.js", "tests/lobby_running_card.test.js",
                  LOBBYCARD_MUTATIONS, "node"),
    "moveui": ("static/move.js", "tests/move_overlay.test.js",
                 MOVEUI_MUTATIONS, "node"),
    "movecopy": ("copy_session.py", "tests/test_move_directory.py",
                   MOVECOPY_MUTATIONS, "pytest"),
    "moveserver": ("server.py", "tests/test_move_directory.py",
                     MOVESERVER_MUTATIONS, "pytest"),
    "loop": ("sdk_bridge.py", "tests/test_loop_control.py",
             LOOP_MUTATIONS, "pytest"),
    "loop-server": ("server.py", "tests/test_loop_control.py",
                    LOOP_SERVER_MUTATIONS, "pytest"),
    "queuepanel": ("sdk_bridge.py", "tests/test_agent_comms_checklist.py",
                   QUEUEPANEL_MUTATIONS, "pytest"),
    "idle": ("server.py", "tests/test_idle_teardown.py",
             IDLE_MUTATIONS, "pytest"),
    "checklist": ("agent_comms.py", "tests/test_agent_comms_checklist.py",
                  CHECKLIST_MUTATIONS, "pytest"),
    "agentcomms": ("agent_comms.py", "tests/test_agent_comms.py",
                   AGENTCOMMS_MUTATIONS, "pytest"),
    "mirror": ("agent_comms.py", "tests/test_agent_comms_surfacing.py",
               MIRROR_MUTATIONS, "pytest"),
    "agenttools": ("agent_tools.py", "tests/test_agent_comms_surfacing.py",
                   AGENTTOOLS_MUTATIONS, "pytest"),
    "agentcli": ("tools/agents.py", "tests/test_agent_comms.py",
                 AGENTCLI_MUTATIONS, "pytest"),
    "todoseed": ("session.py", "tests/test_todo_seeding.py",
                 TODOSEED_MUTATIONS, "pytest"),
    "tabecho": ("server.py", "tests/test_multi_tab_echo.py",
                TABECHO_MUTATIONS, "pytest"),
    "tabecho-rt": ("session_runtime.py", "tests/test_multi_tab_echo.py",
                   TABECHO_RT_MUTATIONS, "pytest"),
    "foreign": ("server.py", "tests/test_foreign_running_sessions.py",
                FOREIGN_MUTATIONS, "pytest"),
    "extauth": ("server.py",
                "tests/test_external_access_policy.py tests/test_external_auth.py",
                EXTAUTH_MUTATIONS, "pytest"),
}


def _cmds(runner: str, test_rel: str) -> list[list[str]]:
    """The command(s) that run *test_rel*.  All must pass for the suite to pass.

    ``node`` takes one script at a time, so several files become several
    commands; pytest takes them all at once.  A target needs more than one file
    whenever the behaviour under test is split across harnesses -- e.g. the
    lobby's markup is checked in jsdom against the module alone, while what a
    *click* on it does needs the whole app loaded in the real page.
    """
    if runner == "node":
        node = shutil.which("node") or "node"
        return [[node, str(ROOT / t)] for t in test_rel.split()]
    # -p no:cacheprovider: a mutated run must not leave a .pytest_cache
    # describing a source tree that no longer exists by the time it is read.
    return [[sys.executable, "-m", "pytest"]
            + [str(ROOT / t) for t in test_rel.split()]
            + ["-q", "-x", "-p", "no:cacheprovider"]]


def run(runner: str, test_rel: str, timeout: float = 300.0) -> bool:
    """True only if the suite *passed*.  A mutant that makes it hang has not
    survived -- it just failed slowly, so a timeout is a False, not a stall.

    A mutant that makes the source unimportable is also caught: pytest exits
    non-zero on a collection error, which is what a syntax-breaking mutation
    produces.  That is a weaker kill than a failing assertion, so the mutations
    above are written to stay syntactically valid wherever they can be.
    """
    for cmd in _cmds(runner, test_rel):
        try:
            p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        if p.returncode != 0:
            return False
    return True


def sweep(label: str, src_rel: str, test_rel: str, mutations,
          runner: str = "node") -> list[str]:
    src = ROOT / src_rel
    bak = src.with_suffix(src.suffix + ".mutbak")
    original = src.read_text(encoding="utf-8")
    bak.write_text(original, encoding="utf-8")

    print(f"\n=== {label}: {src_rel} x {test_rel} ===")
    if not run(runner, test_rel):
        print("BASELINE FAILS -- fix that first")
        bak.unlink()
        return ["<baseline failure>"]

    survivors = []
    try:
        for i, (name, old, new) in enumerate(mutations, 1):
            n = original.count(old)
            if n != 1:
                print(f"{i:2}. SKIP (anchor appears {n}x) {name}")
                survivors.append(f"{label}: {name}  [anchor not unique]")
                continue
            src.write_text(original.replace(old, new), encoding="utf-8")
            passed = run(runner, test_rel)
            print(f"{i:2}. {'SURVIVED' if passed else 'caught  '} {name}")
            if passed:
                survivors.append(f"{label}: {name}")
    finally:
        src.write_text(original, encoding="utf-8")
        bak.unlink()

    print(f"{len(mutations) - len([s for s in survivors])}/{len(mutations)} caught")
    return survivors


def main(argv: list[str]) -> int:
    wanted = argv[1:] or list(TARGETS)
    unknown = [w for w in wanted if w not in TARGETS]
    if unknown:
        print(f"unknown target(s): {', '.join(unknown)}")
        print(f"available: {', '.join(TARGETS)}")
        return 2

    survivors = []
    total = 0
    for label in wanted:
        src_rel, test_rel, mutations, runner = TARGETS[label]
        total += len(mutations)
        survivors += sweep(label, src_rel, test_rel, mutations, runner)

    print(f"\n{total - len(survivors)}/{total} caught overall")
    for s in survivors:
        print("  survivor:", s)
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
