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

Exits 0 only when every mutation is caught.

**The project itself is never modified.**  A run copies it to a scratch
directory -- every file git tracks, the untracked files it would add, and
node_modules -- mutates the copy, runs the suites there, and deletes it on the
way out.  Until 2026-10-02 the mutants were written into the live tree and
the originals put back afterwards, and that cost a file.  A backup taken
mid-sweep captured copy_session.py with a mutation in it; when the file was
later deleted by accident, the backup restored the mutant.  A live tree has
more readers than the sweep: backups, the hub (which serves static/ fresh to
every tab that loads, so a tab reloaded during a sweep of app.js ran a mutant),
and other agents' editors.  The restore was a hazard too: it wrote back the
file as read at the start, undoing any edit made to it during the sweep.

The copy goes in the system temp directory, or ORCH2_MUTATE_SCRATCH if set,
named orchestrator2-mutate-<pid>-...  A run killed outright cannot delete its
own copy, so each run first deletes any whose process has gone.  Python runs
with PYTHONDONTWRITEBYTECODE and the copy has no __pycache__, so every run
compiles the source as it stands (see ``run``).  A <source>.mutbak left in the
project is from the old in-place sweep killed mid-mutation, and the source
beside it may still be a mutant; a run refuses to start until it is dealt
with.
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
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
     "    _updateAgent(status);\n    _updateToggleCount(status);",
     "    _updateAgent(status);"),

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
     "    if (_scrollPendingOnShow) {\n      _scrollPendingOnShow = false;\n      if (_autoScroll) _followToBottom();\n    }",
     ""),

    ("the catch-up latch is never cleared, so it fires spuriously",
     "      _scrollPendingOnShow = false;\n",
     ""),

    ("hiding leaves the collapse gaps open (they wedge the trimmer shut)",
     "      _cancelGap();\n      _cancelShortGap();\n"
     "      // Likewise a trim that was already waiting on a frame",
     "      // Likewise a trim that was already waiting on a frame"),

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
     "    if (document.hidden) { _hiddenAt = Date.now(); return; }\n",
     "    if (document.hidden) { _hiddenAt = Date.now(); }\n"),

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
     '        await self._orphan_bg_tasks("/clear")\n',
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
    ("opening a session finishes its cut-off turn again -- the 2026-09-29 report",
     '        finish = recovering or bool(\n'
     '            getattr(self.config, "resume_interrupted_turn", False))',
     "        finish = True"),

    ("a recovery leaves its own turn waiting, abandoning work the user watched run",
     '        finish = recovering or bool(\n'
     '            getattr(self.config, "resume_interrupted_turn", False))',
     '        finish = bool(\n'
     '            getattr(self.config, "resume_interrupted_turn", False))'),

    ("--resume-interrupted-turn is ignored",
     '        finish = recovering or bool(\n'
     '            getattr(self.config, "resume_interrupted_turn", False))',
     "        finish = recovering"),

    ("a config without the field finishes it",
     '            getattr(self.config, "resume_interrupted_turn", False))',
     '            getattr(self.config, "resume_interrupted_turn", True))'),

    ("off is left out, so an inherited 1 turns it back on",
     '        kwargs["env"]["CLAUDE_CODE_RESUME_INTERRUPTED_TURN"] = "1" if finish else "0"',
     '        if finish:\n'
     '            kwargs["env"]["CLAUDE_CODE_RESUME_INTERRUPTED_TURN"] = "1"'),

    ("the env var name is misspelled, so the CLI ignores it",
     '        kwargs["env"]["CLAUDE_CODE_RESUME_INTERRUPTED_TURN"] = "1" if finish else "0"',
     '        kwargs["env"]["CLAUDE_CODE_RESUME_INTERRUPTED"] = "1" if finish else "0"'),

    ("setting it clobbers the rest of the subprocess environment",
     '        kwargs["env"]["CLAUDE_CODE_RESUME_INTERRUPTED_TURN"] = "1" if finish else "0"',
     '        kwargs["env"] = {"CLAUDE_CODE_RESUME_INTERRUPTED_TURN": "1" if finish else "0"}'),

    ("reconnect is never a recovery, so a CLI that died mid-turn abandons it",
     "        await self.connect(resume_id=sid, recovering=recovering)",
     "        await self.connect(resume_id=sid)"),

    ("a death mid-turn is not remembered, so the turn is abandoned",
     "        self._turn_cut_short = self.turn_active.is_set() or bool(self.state.busy)",
     "        self._turn_cut_short = False"),

    ("every reconnect is a recovery, so a /model switch finishes a turn left "
     "waiting when the session was opened",
     "        recovering = (self._turn_cut_short or self.turn_active.is_set()\n"
     "                      or bool(self.state.busy))",
     "        recovering = True"),

    ("reconnecting a stuck turn abandons it",
     "        recovering = (self._turn_cut_short or self.turn_active.is_set()\n"
     "                      or bool(self.state.busy))",
     "        recovering = self._turn_cut_short"),

    ("a death is remembered forever, so every later reconnect finishes turns",
     "        self._turn_cut_short = False\n        self._last_reconnect_recovered = recovering\n",
     "        self._last_reconnect_recovered = recovering\n"),

    ("the ghost-turn notice is armed on every resume, calling a peer's message "
     "a recovered turn",
     '        self._unprompted_resume_pending = bool(kwargs.get("resume")) and finish',
     '        self._unprompted_resume_pending = bool(kwargs.get("resume"))'),

    ("the connect is never told whether it finishes one",
     "        self._finishing_cut_off_turn = finish\n",
     ""),
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
     "            session_note=note, agent_name=",
     "            session_note=None, agent_name="),

    ("the note fires even when nothing moved, so it becomes noise",
     "    note = None\n    if dir_changed or",
     "    note = None\n    if True or"),

    ("an account-only move is treated as no move at all",
     "    note = None\n    if dir_changed or account_changed:",
     "    note = None\n    if dir_changed:"),

    ("the note omits where the session came from",
     '            parts.append(f"from {cwd} to {dest_cwd}")',
     '            parts.append(f"to {dest_cwd}")'),

    ("the note drops the part that matters -- that the old paths still work",
     '            "location, which still exists — re-check any path you carry "\n'
     '            "forward from before this line. The original session was stopped "',
     '            "location. The original session was stopped "'),

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
     "        if forget:\n"
     "            clear_wakeup(self.config.cwd, self.state.session_id)\n"
     "        if t is None or t.done():",
     "        self._wakeup_fire_at = None\n"
     "        if forget:\n"
     "            clear_wakeup(self.config.cwd, self.state.session_id)\n"
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
     "              else departing in _mobile_ws or departing in _asleep_ws)",
     "    mobile = False"),

    ("every session gets the mobile grace, so nothing is ever reaped",
     "    mobile = (rt.idle_mobile if departing is None\n"
     "              else departing in _mobile_ws or departing in _asleep_ws)",
     "    mobile = True"),

    ("a deferral drops a mobile session onto the short desktop clock",
     "    mobile = (rt.idle_mobile if departing is None\n"
     "              else departing in _mobile_ws or departing in _asleep_ws)",
     "    mobile = departing is not None and (departing in _mobile_ws\n"
     "                                        or departing in _asleep_ws)"),

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
     "            if exclude is not None and ws is exclude:",
     "            if False:"),

    # Deliberately NOT mutating ``is`` to ``==``: neither Starlette's WebSocket
    # nor the test double defines ``__eq__``, so the two are the same operation
    # and no test could ever tell them apart.  ``is`` stays because identity is
    # the intent, but pretending a sweep verifies it would be theatre.

    ("exclude swallows the whole broadcast",
     "            if exclude is not None and ws is exclude:",
     "            if exclude is not None:"),
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
     '        self._unprompted_resume_pending = bool(kwargs.get("resume")) and finish',
     "        self._unprompted_resume_pending = finish"),

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
    ("a resumed session is told nothing -- the 2026-09-16 report, restored",
     '        await self._post_open_notice("warning", (\n'
     '            f"{len(labels)} background task{plural} {was} running when this "',
     '        (lambda *a: None)("warning", (\n'
     '            f"{len(labels)} background task{plural} {was} running when this "'),

    ("the loss is sent to the model as a prompt again, which sets an opened "
     "session working -- the 2026-09-29 report",
     '            f"next prompt it gets."\n        ))\n        return len(labels)',
     '            f"next prompt it gets."\n        ))\n'
     '        self.state.queued_prompts.appendleft("[orchestrator2] lost tasks")\n'
     '        return len(labels)'),

    ("the notice claims the tasks failed, which the log disproves: two "
     "completed 1 s and 2 s into a teardown",
     '            f"they finished is unknown. The session is told along with the "',
     '            f"they were aborted. The session is told along with the "'),

    ("a tab that opens after the resume is not told",
     "        self.state.open_notices.append(notice)\n",
     ""),

    ("a reconnect sends the model the loss too, a second account of what the "
     "new CLI reports",
     '                f"completion notices are lost. Re-run anything you still need."\n'
     '            )},\n        })\n        return len(labels)',
     '                f"completion notices are lost. Re-run anything you still need."\n'
     '            )},\n        })\n'
     '        self.state.queued_prompts.appendleft("[orchestrator2] lost tasks")\n'
     '        return len(labels)'),

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

]

LOSTBG_SESSION_MUTATIONS = [
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
     "        state.active_tools.clear()\n        self._ghost_settled.set()\n"
     "        self._turn_ended_at = time.monotonic()\n        # The prompt a parked worker",
     "        state.active_tools.clear()\n"
     "        self._turn_ended_at = time.monotonic()\n        # The prompt a parked worker"),

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
    # Both scanners -- the duplicate guard and the lobby's holder map -- read
    # one walk (_foreign_holders) since 2026-10-09, so one skip serves both.
    ("a /btw fork counts as a foreign holder, so a session looks held "
     "elsewhere for the seconds an aside takes to answer, the open is refused, "
     "and the lobby shows it running somewhere it is not",
     "            if _is_session_fork(argv):\n"
     "                continue\n"
     "            sid = advertised",
     "            sid = advertised"),

    ("the fork skip swallows the real holder as well, so two agents can drive "
     "one session file",
     '    return "--fork-session" in argv',
     "    return True"),

    ("nothing is ever treated as a fork, restoring the false positive",
     '    return "--fork-session" in argv',
     "    return False"),

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
     "        if live_rt is not None:",
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
     "        # A resumed session whose CLI finishes a cut-off turn starts streaming",
     "\n"
     "        # A resumed session whose CLI finishes a cut-off turn starts streaming"),
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

# /move stops the session it moves.  tests/test_move_stops_original.py.
#
# Not mutated: stopping the original *before* the copy rather than after -- an
# ordering, not a line that a string swap can move.  The test that pins it
# (test_it_is_stopped_before_the_copy_is_taken) is kept regardless.
MOVESTOP_MUTATIONS = [
    ("the original is never stopped -- the report: both versions running",
     "    await _teardown_runtime(\n        rt, force=True,\n"
     "        reason=f\"was moved {where}",
     "    if False: await _teardown_runtime(\n        rt, force=True,\n"
     "        reason=f\"was moved {where}"),

    ("the tab doing the move is sent to the lobby with the ones left behind",
     "    _detach_ws(ws, rt)\n",
     ""),

    ("a tab left on the original is told only that it was closed",
     "        viewer_message=(f\"That session was moved",
     "        viewer_message=None and (f\"That session was moved"),

    ("the copy is told the original may still be running",
     "\"forward from before this line. The original session was stopped \"",
     "\"forward from before this line. The original session may still be running \""),

    ("the user is not told what happened to the original",
     "    await send_to(ws, {\"type\": \"system_msg\", \"subtype\": \"info\",\n"
     "                       \"data\": {\"message\": \" \".join(said)}})\n",
     ""),

    ("queued prompts are left behind in the stopped original",
     "        new_rt.state.queued_prompts.extend(carry[\"queue\"])\n",
     "        pass\n"),

    ("the loop is left behind, and ends",
     "    loop_moved = _rearm_carried_wakeup(new_rt, carry[\"wakeup\"])\n",
     "    loop_moved = False\n"),

    ("the loop comes along but fires on the settle clock, not when it was due",
     "                float(rec.get(\"due_at\", 0)) - time.time())\n",
     "                0.0)\n"),

    ("a loop due mid-move fires while the copy's CLI is still connecting",
     "    delay = max(WAKEUP_RESTORE_SETTLE_S,\n",
     "    delay = max(1.0,\n"),

    ("the original keeps its queue file, so reopening it re-sends the prompts",
     "        await asyncio.to_thread(save_persisted_queue, cwd, [], sid)\n",
     ""),

    ("the queue is taken from the original even when the move then fails",
     "    return {\n        \"queue\": queue,\n",
     "    save_persisted_queue(cwd, [], sid)\n"
     "    return {\n        \"queue\": queue,\n"),

    ("a failed move reopens another account's session without its queue",
     "        if carry[\"queue\"] and q is not None and not q:\n"
     "            q.extend(carry[\"queue\"])\n",
     ""),

    ("a reopen that restored its own queue gets it twice",
     "        if carry[\"queue\"] and q is not None and not q:\n",
     "        if carry[\"queue\"] and q is not None:\n"),

    ("the explicit name is left behind",
     "            session_note=note, agent_name=carry[\"agent_name\"],\n",
     "            session_note=note, agent_name=None,\n"),

    ("the copy is never told which background tasks died",
     "    if carry[\"bg_tasks\"]:\n        await asyncio.to_thread(\n",
     "    if False:\n        await asyncio.to_thread(\n"),

    ("a copy that will not start leaves the user with nothing running",
     "        await _move_failed(f\"Couldn't start the moved session: {exc}\")\n",
     "        await send_to(ws, {\"type\": \"move_error\", \"message\": "
     "f\"Couldn't start the moved session: {exc}\"})\n"),

    ("a failed copy leaves the user with nothing running",
     "        await _move_failed(f\"Copy failed: {exc}\")\n",
     "        await send_to(ws, {\"type\": \"move_error\", \"message\": "
     "f\"Copy failed: {exc}\"})\n"),

    ("a tab whose original could not be reopened is left on the stopped session",
     "            # still showing -- and able to type into -- a stopped session.\n"
     "            await _enter_lobby(ws)\n",
     "            # still showing -- and able to type into -- a stopped session.\n"),

    ("the reopened original has lost its loop",
     "        _rearm_carried_wakeup(orig, carry[\"wakeup\"])\n",
     ""),
]

MOVESTOP_WAKEUP_MUTATIONS = [
    ("a recorded wakeup can never be read back, so no loop survives a move",
     "    return rec if _valid(rec) else None\n",
     "    return None\n"),
]

# A background tab Chrome kills is asleep, not gone: its session is kept.
# tests/test_asleep_tab.py and tests/reconnect_on_show.test.js.
ASLEEP_SERVER_MUTATIONS = [
    ("a tab the browser put to sleep is never marked asleep -- the request",
     "    _asleep_ws.add(ws)\n    log.info(\"tab for %s was dropped in the background",
     "    log.info(\"tab for %s was dropped in the background"),

    ("any abnormal close counts: a tab killed as you look at it is kept too",
     "    if code in _DELIBERATE_CLOSES or ws not in _hidden_ws:",
     "    if code in _DELIBERATE_CLOSES:"),

    ("a background tab closed by hand is kept as if asleep",
     "    if code in _DELIBERATE_CLOSES or ws not in _hidden_ws:",
     "    if ws not in _hidden_ws:"),

    ("the page's own ws.close() counts as being killed",
     "_DELIBERATE_CLOSES = frozenset({1000, 1001})",
     "_DELIBERATE_CLOSES = frozenset({1001})"),

    ("the idle timer ignores an asleep tab",
     "              else departing in _mobile_ws or departing in _asleep_ws)",
     "              else departing in _mobile_ws)"),

    ("a disconnect is never classified",
     "        _note_how_it_ended(ws, getattr(exc, \"code\", None))",
     "        pass"),

    ("a socket the hub found gone first is never classified",
     "        _note_how_it_ended(ws, None)\n    finally:",
     "    finally:"),

    ("the hub never hears a tab go to the background",
     "        if msg.get(\"hidden\"):\n            _hidden_ws.add(ws)",
     "        if False:\n            _hidden_ws.add(ws)"),

    ("the hub never hears it come back",
     "        else:\n            _hidden_ws.discard(ws)\n        return True",
     "        else:\n            pass\n        return True"),

    ("a gone socket is left in the sets",
     "    _hidden_ws.discard(ws)\n    _asleep_ws.discard(ws)\n",
     ""),

    ("the hub never says it hears visibility, so no page sends it",
     "HUB_HEARS = [\"visibility\", \"leaving\"]",
     "HUB_HEARS = [\"leaving\"]"),
]

ASLEEP_APP_MUTATIONS = [
    ("an older hub is sent it, and answers in the chat",
     "    if (!_hubHears.includes('visibility')) return;\n",
     ""),

    ("the hub is not told when the tab goes to the background",
     "    _sendVisibility();\n    if (document.hidden) { _hiddenAt",
     "    if (document.hidden) { _hiddenAt"),

    ("a tab attaching in the background never says so",
     "      _hubHears = Array.isArray(msg.hears) ? msg.hears : [];\n      _sendVisibility();",
     "      _hubHears = Array.isArray(msg.hears) ? msg.hears : [];"),

    ("a new socket keeps what the last hub heard",
     "    _hubHears = [];                // until this socket's hub says otherwise\n",
     ""),

    ("the tab always says it is visible",
     "hidden: !!document.hidden }",
     "hidden: false }"),
]

# A tab whose socket drops resumes; it does not reload, and keeps its name.
# tests/test_resume_stream.py and tests/reconnect_on_show.test.js.
#
# Not mutated: replay_since's `seq < 0` guard and its caught-up `return []`
# (each is also covered by the check after it, so removing either changes
# nothing); document.wasDiscarded, which jsdom does not implement.
RESUME_RUNTIME_MUTATIONS = [
    ("nothing is numbered or kept, so every reconnect reloads",
     "        if msg.get(\"type\") not in NOT_REPLAYED:\n            msg = self._record(msg)",
     "        if False:\n            msg = self._record(msg)"),

    ("a bell is replayed, ringing late",
     "                          \"session_list\", \"bell\"})",
     "                          \"session_list\"})"),

    ("what is said with nobody watching is not kept -- the tab that dropped",
     "        seq: int | None = None\n        if msg.get(\"type\") not in NOT_REPLAYED:",
     "        if not self.clients:\n            return\n"
     "        seq: int | None = None\n        if msg.get(\"type\") not in NOT_REPLAYED:"),

    ("the tab that sent a prompt loses its place, and is sent it again",
     "                if seq is not None:\n"
     "                    ws_channel.send(ws, {\"type\": \"seq\", \"seq\": seq})\n",
     ""),

    ("every message gets the same number",
     "        self.seq += 1\n",
     ""),

    ("what is kept is unbounded in size",
     "        while self._replay and (self._replay_bytes > REPLAY_MAX_BYTES\n",
     "        while self._replay and (False\n"),

    ("what is kept is unbounded in count",
     "                                or len(self._replay) > REPLAY_MAX_MESSAGES):",
     "                                or False):"),

    ("a dropped message's size is still counted, so everything goes",
     "            self._replay_bytes -= dropped\n",
     ""),

    ("another runtime's stream is resumed -- a hub restart",
     "        if epoch != self.epoch or not isinstance(seq, int) or isinstance(seq, bool):",
     "        if not isinstance(seq, int) or isinstance(seq, bool):"),

    ("True is taken for position 1",
     "        if epoch != self.epoch or not isinstance(seq, int) or isinstance(seq, bool):",
     "        if epoch != self.epoch or not isinstance(seq, int):"),

    ("a position the stream never reached is resumed",
     "        if seq < 0 or seq > self.seq:\n            return None",
     "        if seq < 0:\n            return None"),

    ("a tab is resumed past messages that were dropped, skipping them",
     "        if seq + 1 < oldest:\n            return None\n",
     ""),
]

RESUME_SERVER_MUTATIONS = [
    ("a reconnect's position is never read",
     "    return rt.replay_since(epoch, position)",
     "    return None"),

    ("the hub never resumes",
     "            if missed is not None:\n                await _resume_ws(ws, target, missed)",
     "            if False:\n                await _resume_ws(ws, target, missed)"),

    ("a resumed tab is cleared anyway",
     "    ws_channel.send(ws, {\"type\": \"attached\", \"session\": rt.meta(),\n"
     "                         \"epoch\": rt.epoch, \"seq\": rt.seq, \"resumed\": True,",
     "    ws_channel.send(ws, {\"type\": \"clear_screen\"})\n"
     "    ws_channel.send(ws, {\"type\": \"attached\", \"session\": rt.meta(),\n"
     "                         \"epoch\": rt.epoch, \"seq\": rt.seq, \"resumed\": True,"),

    ("a resumed tab is not told what changed meanwhile",
     "    if state is not None and config is not None:\n        ws_channel.send(ws, {\n"
     "            \"type\": \"status_update\",",
     "    if False:\n        ws_channel.send(ws, {\n"
     "            \"type\": \"status_update\","),

    ("a resumed tab is not sent what it missed",
     "    for msg in missed:\n        ws_channel.send(ws, msg)\n",
     ""),

    ("a resumed tab is not mapped to its session",
     "    _ws_runtime[ws] = rt\n    log.info(\"tab resumed",
     "    log.info(\"tab resumed"),

    ("a full attach does not say where in the stream the tab joins",
     "    await send_to(ws, {\"type\": \"attached\", \"session\": rt.meta(),\n"
     "                       \"epoch\": rt.epoch, \"seq\": rt.seq, \"hears\": HUB_HEARS})",
     "    await send_to(ws, {\"type\": \"attached\", \"session\": rt.meta(),\n"
     "                       \"hears\": HUB_HEARS})"),

    ("why a tab reconnected is never logged",
     "        _log_reconnect_reason(ws, requested_rid)\n",
     ""),

    ("a frozen page is logged as not frozen",
     "\"yes\" if q.get(\"frozen\") == \"1\" else \"no\")",
     "\"no\")"),
]

RESUME_APP_MUTATIONS = [
    ("a reconnect never asks to resume",
     "    if (rid && _stream && _stream.rid === rid) {\n      q.push(`resume=",
     "    if (false) {\n      q.push(`resume="),

    ("the tab never learns how far it got",
     "    if (type !== 'attached' && typeof msg.seq === 'number' && _stream",
     "    if (false && typeof msg.seq === 'number' && _stream"),

    ("the stream's position in attached is taken for a message shown",
     "    if (type !== 'attached' && typeof msg.seq === 'number' && _stream",
     "    if (typeof msg.seq === 'number' && _stream"),

    ("a seq marker reaches the chat",
     "    if (type === 'seq') return;\n",
     ""),

    ("a resumed attach jumps to the latest position, losing a replay cut short",
     "      if (!resumedHere) {",
     "      if (true) {"),

    ("a hub that numbers nothing is asked to resume anyway",
     "      _stream = (s.rid && msg.epoch)",
     "      _stream = (s.rid)"),

    ("why the socket dropped is never reported",
     "    if (_lastClose) {\n      q.push(`why=",
     "    if (false) {\n      q.push(`why="),

    ("a freeze goes unnoticed",
     "    document.addEventListener('freeze', () => { _frozen = true; });",
     "    document.addEventListener('freeze', () => {});"),

    ("a failed retry is reported instead of the loss",
     "      if (!_lastClose) {\n        _lastClose = {",
     "      if (true) {\n        _lastClose = {"),

    ("an old loss is reported after a good connection",
     "      _lastClose = null;\n      _frozen = false;\n",
     ""),

    ("how long it was hidden is not measured from when it hid",
     "    if (document.hidden) { _hiddenAt = Date.now(); return; }",
     "    if (document.hidden) { return; }"),

    ("a dropped socket's update is not marked as connection-only",
     "      Status.update({ busy_label: label, busy_class: 'reconnecting', connection: true });",
     "      Status.update({ busy_label: label, busy_class: 'reconnecting' });"),
]

RESUME_STATUS_MUTATIONS = [
    ("a dropped connection renames the tab -- the report",
     "    if (status.connection) return;\n",
     ""),

    ("the name is not kept for a reload",
     "        try {\n          sessionStorage.setItem(TITLE_KEY, JSON.stringify(",
     "        try {\n          if (false) sessionStorage.setItem(TITLE_KEY, JSON.stringify("),

    ("a reloaded tab still shows the page's own title",
     "        document.title = saved.title;\n",
     ""),

    ("a reloaded tab shows another session's name",
     "      if (saved && saved.title && rid && saved.rid === rid) {",
     "      if (saved && saved.title) {"),
]

# A prompt sent as the CLI starts a turn of its own.  tests/test_cli_queue_race.py
# and tests/peer_message.test.js.
CLIQUEUE_BRIDGE_MUTATIONS = [
    ("a queued prompt goes out the instant a turn ends -- the report",
     "        if self._turn_ended_at is not None:\n"
     "            wait = self._turn_ended_at + TURN_END_SETTLE_S - time.monotonic()",
     "        if False:\n"
     "            wait = self._turn_ended_at + TURN_END_SETTLE_S - time.monotonic()"),

    ("it waits, but goes into the turn the CLI started anyway",
     "                if state.busy or self.turn_active.is_set():\n"
     "                    log.info(\"queued prompt held:",
     "                if False:\n"
     "                    log.info(\"queued prompt held:"),

    ("a ghost turn's end does not start the settle",
     "        self._ghost_settled.set()\n        self._turn_ended_at = time.monotonic()\n"
     "        # The prompt a parked worker",
     "        self._ghost_settled.set()\n        # The prompt a parked worker"),

    ("our own turn's end does not start the settle",
     "            state.turn_started_at = None\n            self._turn_ended_at = time.monotonic()\n",
     "            state.turn_started_at = None\n"),

    ("a stopped bridge still sends the prompt",
     "                if self.stop_event.is_set():\n                    return None\n"
     "                if state.busy or self.turn_active.is_set():",
     "                if state.busy or self.turn_active.is_set():"),

    ("a head put under edit during the settle is sent half-typed",
     "                if not state.queued_prompts or state.queue_editing_index == 0:\n"
     "                    return None\n        prompt = state.queued_prompts.popleft()",
     "        prompt = state.queued_prompts.popleft()"),

    ("a peer's message is not recognised as one",
     "        if isinstance(origin, dict) and origin.get(\"kind\") == \"peer\":\n"
     "            return origin",
     "        if isinstance(origin, dict) and origin.get(\"kind\") == \"peer-no\":\n"
     "            return origin"),

    ("between turns, a peer's message is still dropped",
     "                await self._broadcast_peer_message(peer, msg, during_turn=False)\n",
     "                pass\n"),

    ("a peer's turn is shown but not marked running, so a prompt goes into it",
     "            if peer is not None:\n"
     "                if not await self._begin_ghost_turn_if_needed():\n"
     "                    return\n",
     "            if peer is not None:\n"),

    ("inside our turn, a peer's message is still dropped",
     "                        await self._broadcast_peer_message(peer, msg, during_turn=True)\n",
     "                        pass\n"),

    ("with no decoded body, an empty message is shown",
     "            content = getattr(msg, \"content\", None)\n"
     "            if isinstance(content, str):\n                body = content\n",
     "            content = getattr(msg, \"content\", None)\n"
     "            if isinstance(content, str):\n                body = \"\"\n"),

    ("a turn that ended on someone else's result says nothing",
     "                    await self._announce_if_not_our_turn(msg)\n",
     ""),

    ("our own turns are reported as someone else's",
     "        if not kind or kind == \"human\":\n            return\n",
     "        if not kind:\n            return\n"),

    ("the notice does not say who the turn was for",
     "            what = \"a message from \" + (origin.get(\"name\") or \"another session\")",
     "            what = \"a message from another session\""),
]

CLIQUEUE_HISTORY_MUTATIONS = [
    ("a peer's message is drawn in history as the user's own",
     "        if isinstance(origin, dict) and origin.get(\"kind\") == \"peer\" \\\n",
     "        if False and isinstance(origin, dict) \\\n"),

    ("history shows the raw envelope rather than the peer's words",
     "            body = origin.get(\"body\")\n            if not isinstance(body, str) or not body.strip():\n"
     "                body = _extract_text(msg.get(\"content\"))",
     "            body = None\n            if not isinstance(body, str) or not body.strip():\n"
     "                body = _extract_text(msg.get(\"content\"))"),

    ("a prompt passed to a running turn vanishes on reload -- the report",
     "            if isinstance(att, dict) and att.get(\"type\") == \"queued_command\" \\\n"
     "                    and att.get(\"commandMode\", \"prompt\") == \"prompt\" \\\n"
     "                    and isinstance(att.get(\"prompt\"), str) \\\n",
     "            if isinstance(att, dict) and att.get(\"type\") == \"queued_command-no\" \\\n"
     "                    and att.get(\"commandMode\", \"prompt\") == \"prompt\" \\\n"
     "                    and isinstance(att.get(\"prompt\"), str) \\\n"),

    ("one that was also a turn is shown twice",
     "                    and not (att.get(\"source_uuid\") in user_uuids):",
     "                    and True:"),

    ("a queued shell command is shown as a prompt",
     "                    and att.get(\"commandMode\", \"prompt\") == \"prompt\" \\\n",
     "                    and True \\\n"),

    ("nothing marks it as sent while the session was working",
     "                        \"mid_turn\": True,\n",
     ""),
]

CLIQUEUE_CHAT_MUTATIONS = [
    ("a live peer_message is not drawn at all",
     "      case 'peer_message':    _addPeerMessage(msg); break;\n",
     ""),

    ("a history peer_message is not drawn",
     "    } else if (type === 'peer_message') {\n      _addPeerMessage(m);\n",
     ""),

    ("the sender is not named",
     "      '\\u2709 From ' + ((msg && msg.name) || 'another session');",
     "      '\\u2709 From another session';"),

    ("a peer's text is rendered as markup",
     "    el.querySelector('.msg-content').textContent = body;",
     "    el.querySelector('.msg-content').innerHTML = body;"),

    ("an empty peer message draws an empty box",
     "    const body = (msg && msg.body) || '';\n    if (!body) return;\n",
     "    const body = (msg && msg.body) || '';\n"),

    ("activity is folded together across a peer's message",
     "          el.classList.contains('msg-peer') ||\n",
     ""),

    ("a mid-turn prompt loses its mark",
     "      _addUserMessage(m.content || m.text || '', false, m.mid_turn === true);",
     "      _addUserMessage(m.content || m.text || '', false, false);"),

    ("every prompt in history is marked mid-turn",
     "    const label = midTurn\n",
     "    const label = true\n"),
]

# The tab's icon shows the session's state.  tests/favicon.test.js.
FAVICON_MUTATIONS = [
    ("working lights the wrong LED",
     "    if (cls === 'working')         lit.top = '--indicator-working';",
     "    if (cls === 'working')         lit.middle = '--indicator-working';"),

    ("compacting is lit in working's green",
     "    else if (cls === 'compacting') lit.top = '--indicator-compacting';",
     "    else if (cls === 'compacting') lit.top = '--indicator-working';"),

    ("compacting lights nothing",
     "    else if (cls === 'compacting') lit.top",
     "    else if (cls === 'compacting-no') lit.top"),

    ("an error lights nothing",
     "    else if (cls === 'error')      lit.top",
     "    else if (cls === 'error-no')      lit.top"),

    ("background tasks show only when idle -- compacting during bg-wait shows one light",
     "    if (s.bg_count > 0 || cls === 'bg-wait') lit.middle",
     "    if (cls === 'bg-wait') lit.middle"),

    ("an older hub's bg-wait shows nothing",
     "    if (s.bg_count > 0 || cls === 'bg-wait') lit.middle",
     "    if (s.bg_count > 0) lit.middle"),

    ("background tasks and the loop share a position",
     "cls === 'bg-wait') lit.middle = '--indicator-bg-wait';",
     "cls === 'bg-wait') lit.bottom = '--indicator-bg-wait';"),

    ("a scheduled loop is never shown",
     "    if (typeof s.wakeup_at === 'number')     lit.bottom",
     "    if (false)     lit.bottom"),

    ("idle's grey is lit beside another light",
     "    if (!lit.top && !lit.middle && !lit.bottom) lit.top = '--indicator-idle';",
     "    if (!lit.top) lit.top = '--indicator-idle';"),

    ("idle lights nothing",
     "    if (!lit.top && !lit.middle && !lit.bottom) lit.top = '--indicator-idle';\n",
     ""),

    ("a dropped connection lights one yellow, the same as compacting",
     "    if (cls === 'reconnecting') return _all('--system-warning');",
     "    if (cls === 'reconnecting') return { top: '--system-warning' };"),

    ("reconnecting is red, though its text is yellow",
     "    if (cls === 'reconnecting') return _all('--system-warning');",
     "    if (cls === 'reconnecting') return _all('--system-error');"),

    ("a dropped connection still shows what was last known",
     "    if (cls === 'reconnecting') return _all('--system-warning');\n",
     ""),

    ("giving up is yellow, though its text is red",
     "    if (cls === 'shutdown')     return _all('--system-error');",
     "    if (cls === 'shutdown')     return _all('--system-warning');"),

    ("connecting reads as idle",
     "    if (cls === 'connecting')   return",
     "    if (cls === 'connecting-no')   return"),

    ("compacting has no colour when the theme gives none",
     "    '--indicator-compacting': '#e5e510',\n",
     ""),

    ("the theme is ignored: the lights are always the default colours",
     "    return COLOUR.test(v) ? v : FALLBACK[cssVar];",
     "    return FALLBACK[cssVar];"),

    ("anything a theme says is written into the SVG",
     "    return COLOUR.test(v) ? v : FALLBACK[cssVar];",
     "    return v || FALLBACK[cssVar];"),

    ("the icon is rewritten on every status snapshot",
     "    if (key === _key) return;\n",
     ""),

    ("a light going out does not change the icon",
     "    const key = Object.keys(ROWS).map((r) => r + '=' + (lit[r] || '')).join(' ');",
     "    const key = 'lit';"),

    ("lit LEDs are drawn off and off ones lit",
     "      if (colour) {",
     "      if (!colour) {"),

    ("a tab with no session still lights up",
     "    if (_landing) return;\n",
     ""),

    ("landing leaves the last session's light on",
     "    if (_landing) _show(null, DEFAULT_HREF);",
     "    if (_landing) {}"),

    ("a tab that attaches again stays dark",
     "    _landing = !!on;",
     "    _landing = _landing || !!on;"),
]

FAVICON_STATUS_MUTATIONS = [
    ("the status bar never tells the icon",
     "    if (typeof Favicon !== 'undefined') Favicon.update(status);\n",
     ""),
]

FAVICON_LOBBY_MUTATIONS = [
    ("a tab whose session closed keeps its last light",
     "    if (typeof Favicon !== 'undefined') Favicon.setLanding(on);\n",
     ""),
]

# A loop survives the hub restarting; closing a session still ends it.
# tests/test_loops_survive_restart.py and tests/test_wakeup_store.py.
LOOPKEEP_BRIDGE_MUTATIONS = [
    ("every stop erases the loop again -- the bug: a restart ends every loop",
     "            self._cancel_wakeup(forget=not hub_exiting)\n",
     "            self._cancel_wakeup()\n"),

    ("no stop erases it, so a closed session comes back at the next start",
     "            self._cancel_wakeup(forget=not hub_exiting)\n",
     "            self._cancel_wakeup(forget=False)\n"),

    ("the record is erased whatever forget says",
     "        if forget:\n            clear_wakeup(",
     "        if True:\n            clear_wakeup("),

    ("stop() drops what it was told on the way to _shutdown",
     "        await asyncio.shield(self._shutdown(hub_exiting=hub_exiting))\n",
     "        await asyncio.shield(self._shutdown())\n"),
]

LOOPKEEP_SERVER_MUTATIONS = [
    ("the lobby's Shut down erases every session's loop",
     "                # Scheduled loops come back at the next start (SDKBridge.stop).\n"
     "                await rt.bridge.stop(hub_exiting=True)\n",
     "                await rt.bridge.stop()\n"),

    ("the lobby's Restart erases every session's loop",
     "                # restore was built for (SDKBridge.stop).\n"
     "                await rt.bridge.stop(hub_exiting=True)\n",
     "                await rt.bridge.stop()\n"),

    ("the no-tabs auto-shutdown erases every session's loop",
     "                    # A hub exit, so scheduled loops survive it (SDKBridge.stop).\n"
     "                    await rt.bridge.stop(hub_exiting=True)\n",
     "                    await rt.bridge.stop()\n"),

    ("Ctrl+C erases the primary session's loop",
     "        await bridge.stop(hub_exiting=True)\n",
     "        await bridge.stop()\n"),

    ("closing a session from the lobby keeps its loop, so it comes back",
     "            await rt.bridge.stop()\n        except asyncio.CancelledError:",
     "            await rt.bridge.stop(hub_exiting=True)\n        except asyncio.CancelledError:"),

    ("/quit keeps the session's loop, so it comes back",
     "            await bridge.stop()\n"
     "            await send_to(ws, {\"type\": \"system_msg\", \"subtype\": \"shutdown\",",
     "            await bridge.stop(hub_exiting=True)\n"
     "            await send_to(ws, {\"type\": \"system_msg\", \"subtype\": \"shutdown\","),

    ("/cwd and /resume keep the session they leave's loop",
     "            await bridge.stop()\n        except Exception:",
     "            await bridge.stop(hub_exiting=True)\n        except Exception:"),
]

LOOPKEEP_RESTORE_MUTATIONS = [
    ("an already-open session's loop is dropped -- the primary's, every restart",
     "            await _rearm_live_wakeup(live_rt, rec)\n",
     ""),

    ("a loop the running session armed itself is overwritten by the old record",
     "    if getattr(getattr(rt, \"state\", None), \"wakeup_at\", None) is not None:\n",
     "    if False:\n"),

    ("an overdue loop in a running session fires before it settles",
     "        else WAKEUP_RESTORE_SETTLE_S\n"
     "    log.warning(\"wakeup restore: %s is already open as %s",
     "        else 0.0\n"
     "    log.warning(\"wakeup restore: %s is already open as %s"),

    ("a session that was already open is told it was reopened automatically",
     "    _rearm_restored_wakeup(rt, rec, delay, plan, reopened=False)\n",
     "    _rearm_restored_wakeup(rt, rec, delay, plan, reopened=True)\n"),

    ("every restored loop claims its session was reopened automatically",
     "    if reopened:\n        message +=",
     "    if True:\n        message +="),
]

# A backlog handed to a visible tab at once (a frozen background tab being
# shown): one scroll per frame, user scroll intent obeyed at once, and only a
# prompt typed here forcing the view down.  tests/hidden_window.test.js.
#
# Not mutated: the scrollbar mousedown.  jsdom has no offsetX, so no test here
# can press on a scrollbar.
SCROLLBURST_MUTATIONS = [
    ("a visible window scrolls once per message again -- the reported drip",
     "      if (typeof requestAnimationFrame === 'function') requestAnimationFrame(_scrollNow);\n",
     "      if (typeof requestAnimationFrame === 'function') _scrollNow();\n"),

    ("every call asks for a frame of its own",
     "    if (!_scrollFrame) {\n      _scrollFrame = true;\n",
     "    if (true) {\n      _scrollFrame = true;\n"),

    ("a scroll frame orphaned by hiding leaves the latch set, and the view "
     "never follows again",
     "      if (_scrollFrame) {\n        _scrollFrame = false;\n"
     "        _scrollPendingOnShow = true;\n      }\n",
     ""),

    ("a scroll owed by an orphaned frame is not made on show",
     "        _scrollFrame = false;\n        _scrollPendingOnShow = true;\n",
     "        _scrollFrame = false;\n"),

    ("wheeling up waits for the throttled check, and the backlog wins the race",
     "      if (e.deltaY < 0) _stopFollowing();\n",
     "      if (false) _stopFollowing();\n"),

    ("dragging the content down with a finger does not stop following",
     "      if (e.touches[0].clientY > _touchY + 8) _stopFollowing();\n",
     "      if (false) _stopFollowing();\n"),

    ("paging back with the keyboard does not stop following",
     "      if (e.key === 'PageUp' || e.key === 'ArrowUp' || e.key === 'Home') {\n",
     "      if (false) {\n"),

    ("every user message drags a reader back to the bottom -- the reported "
     "theft",
     "    if (local) _autoScroll = true;\n",
     "    _autoScroll = true;\n"),

    ("a prompt typed here is left out of view",
     "    if (local) _autoScroll = true;\n",
     ""),

    ("the local mark is dropped on the way in",
     "_addUserMessage(msg.content, msg.local === true)",
     "_addUserMessage(msg.content, false)"),
]

SCROLLBURST_APP_MUTATIONS = [
    ("the echo of a typed prompt is not marked as local, so it no longer "
     "brings the view down",
     "Chat.handleMessage({ type: 'user_message', content: msg.text, local: true });",
     "Chat.handleMessage({ type: 'user_message', content: msg.text });"),
]

# The status bar's timers count from when the state began.
# tests/reconnect_on_show.test.js and tests/test_status_busy_since.py.
STATUSTIMER_MUTATIONS = [
    ("the server's start is ignored, so each tab counts from when it looked",
     "    const start = sinceMs !== null ? sinceMs\n",
     "    const start = false ? sinceMs\n"),

    ("bg-wait shows each snapshot's own old clock -- the reported catch-up",
     "                  || (cls === 'bg-wait' && since !== null);\n",
     "                  || false;\n"),

    ("an older hub's bg-wait is timed from nothing and reads 0:0:00",
     "                  || (cls === 'bg-wait' && since !== null);\n",
     "                  || (cls === 'bg-wait');\n"),

    ("the label loses what it is counting",
     "    const pre = prefix || cls;\n",
     "    const pre = cls;\n"),

    ("a new turn, or a new task count, keeps the old clock",
     "    if (_localTimerClass === cls && _localTimerStart === start\n"
     "        && _localTimerPrefix === pre) return;\n",
     "    if (_localTimerClass === cls) return;\n"),

    ("the clock stops between snapshots",
     "    if (!_localTimerInterval) _localTimerInterval = setInterval(_renderLocalTimer, 1000);\n",
     ""),
]

BUSYSINCE_MUTATIONS = [
    ("the start jitters, so every snapshot differs and none is suppressed",
     "    busy_since = (round(time.time() - (time.monotonic() - since_mono))\n",
     "    busy_since = ((time.time() - (time.monotonic() - since_mono))\n"),

    ("a turn does not say when it began",
     '        busy_prefix, since_mono = "working", state.turn_started_at\n',
     '        busy_prefix, since_mono = "working", None\n'),

    ("bg-wait does not say when it began",
     "        since_mono = oldest\n",
     "        since_mono = None\n"),

    ("bg-wait loses its task count",
     '        busy_prefix = f"bg wait ({len(state.background_tasks)})"\n',
     '        busy_prefix = "bg wait"\n'),

    ("connecting does not say when it began",
     '        busy_prefix, since_mono = "connecting", state.connect_started_at\n',
     '        busy_prefix, since_mono = "connecting", None\n'),

    ("compacting does not say when it began",
     '        busy_prefix, since_mono = "compacting", state.cli_status_started_at\n',
     '        busy_prefix, since_mono = "compacting", None\n'),

    ("the start never reaches the status bar",
     '        "busy_since": busy_since,\n',
     '        "busy_since": None,\n'),
]

# The status bar's agent name.  tests/test_status_agent_name.py and, for the
# field itself, tests/reconnect_on_show.test.js.
STATUSAGENT_JS_MUTATIONS = [
    ("the agent field is never shown",
     "    const show = name !== '';\n",
     "    const show = false;\n"),

    ("a field is shown for a session whose name is not known yet",
     "    const show = name !== '';\n",
     "    const show = true;\n"),

    ("the field is shown without its label and separator",
     "      for (const el of [elAgent, elAgentSep, elAgentLabel]) {\n",
     "      for (const el of [elAgent]) {\n"),

    ("the name itself is never written into the field",
     "    _set(elAgent, 'textContent', name);\n",
     ""),

    ("the field is never updated at all",
     "    _updateAgent(status);\n",
     ""),

    ("the tooltip never gives the registry name",
     "      parts.push('In the orchestrator2 agent registry it is \"' + reg + '\".');\n",
     ""),

    ("the tooltip repeats a registry name that is the same",
     "    } else if (reg !== name) {\n",
     "    } else {\n"),

    ("a session outside the registry is not said to be",
     "    if (!reg) {\n",
     "    if (false) {\n"),

    ("a name given with --agent-name is not said to be",
     "    if (status.agent_name_given && status.agent_name_given === name) {\n",
     "    if (false) {\n"),
]

STATUSAGENT_MUTATIONS = [
    ("the CLI's registry file is never read",
     "            self.state.cli_name = await asyncio.to_thread(read)\n",
     "            pass\n"),

    ("the hub's account is read for a session in another account",
     '        path = (config_dir_path(getattr(self.config, "config_dir", None))\n',
     "        path = (config_dir_path(None)\n"),

    ("the file is re-read on every tick",
     "        if now - self._cli_name_read_at < self.CLI_NAME_REFRESH_S:\n"
     "            return\n",
     ""),

    ("a CLI that stopped keeps showing its old name",
     "        if pid is None:\n            self.state.cli_name = None\n"
     "            return\n",
     "        if pid is None:\n            return\n"),

    ("a blank name is shown as the name",
     "            return name.strip() or None\n",
     "            return name\n"),

    ("a name that is not text keeps the last one on screen",
     "            if not isinstance(name, str):\n                return None\n",
     ""),

    ("the registry identity never reaches the status bar",
     "        self.state.agent_registry_name = value\n",
     ""),
]

STATUSAGENT_STATE_MUTATIONS = [
    ("an unknown name is guessed from the title",
     '        "agent_name": state.cli_name,\n',
     '        "agent_name": state.cli_name or state.human_title,\n'),

    ("the registry identity is left out of the status",
     '        "agent_registry": state.agent_registry_name,\n',
     '        "agent_registry": None,\n'),

    ("whether the name was given with --agent-name is left out",
     '        "agent_name_given": state.agent_name,\n',
     '        "agent_name_given": None,\n'),
]

STATUSAGENT_SERVER_MUTATIONS = [
    ("the ticker never reads the name, so the field never appears",
     "            await rt.bridge.refresh_cli_name()\n",
     "            pass\n"),
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
     '        if a in ("--agent-name", "--config-dir"):\n',
     '        if a in ("--config-dir",):\n'),

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
     "                                   agent_labels=agent_labels,\n",
     "\n"),

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


# /usage: the account's plan limits.  plan_usage.py, the command, the
# server's handler, and static/usage.js.  tests/test_usage.py,
# tests/usage.test.js and tests/reconnect_on_show.test.js.
USAGE_PY_MUTATIONS = [
    ("the request goes to another endpoint",
     'USAGE_URL = "https://api.anthropic.com/api/oauth/usage"',
     'USAGE_URL = "https://api.anthropic.com/api/oauth/profile"'),

    ("the oauth beta header is not sent",
     '        "anthropic-beta": OAUTH_BETA,\n',
     ''),

    ("the token is not sent as a bearer token",
     '"Authorization": f"Bearer {token}",',
     '"Authorization": token,'),

    ("an account with no login is asked anyway",
     '    if not isinstance(token, str) or not token:\n',
     '    if False:\n'),

    ("a token without the profile scope is sent",
     '    if isinstance(scopes, list) and PROFILE_SCOPE not in scopes:\n',
     '    if False:\n'),

    ("a record that lists no scopes is refused",
     '    if isinstance(scopes, list) and PROFILE_SCOPE not in scopes:\n',
     '    if PROFILE_SCOPE not in (scopes or []):\n'),

    ("an expired token is sent",
     '    if isinstance(expires_ms, (int, float)) and expires_ms / 1000 <= now_s:\n',
     '    if False:\n'),

    ("a token is expired only after its moment",
     'expires_ms / 1000 <= now_s:',
     'expires_ms / 1000 < now_s:'),

    ("the expiry is read as seconds",
     'expires_ms / 1000 <= now_s:',
     'expires_ms <= now_s:'),

    ("a record with no expiry is refused",
     '    if isinstance(expires_ms, (int, float)) and expires_ms / 1000 <= now_s:\n',
     '    if expires_ms is None or expires_ms / 1000 <= now_s:\n'),

    ("the request has no timeout",
     '        with opener(req, timeout=timeout) as resp:',
     '        with opener(req) as resp:'),

    ("the credentials are read from the hub's account",
     '    path = config_dir_path(config_dir) / ".credentials.json"',
     '    path = config_dir_path(None) / ".credentials.json"'),

    ("an unreadable credentials file escapes",
     '    except (OSError, ValueError):\n        return None\n    oauth =',
     '    except OSError:\n        return None\n    oauth ='),

    ("a credentials file that is not an object escapes",
     'oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None',
     'oauth = data.get("claudeAiOauth")'),

    ("a login record that is not an object is used",
     '    return oauth if isinstance(oauth, dict) else None',
     '    return oauth'),

    ("rate limiting is not told apart",
     '    if code == 429:\n',
     '    if code == 4290:\n'),

    ("a rejected token is not told apart",
     '    if code == 401:\n',
     '    if code == 4010:\n'),

    ("the API's reason is dropped",
     '        f": {detail}" if detail else ".")',
     '        ".")'),

    ("a long reason is not cut short",
     '    return msg[:200] + ("..." if len(msg) > 200 else "")',
     '    return msg'),

    ("a reason's line breaks are kept",
     '    msg = " ".join(msg.split())\n',
     ''),

    ("an error body that is not the API's is repeated",
     '    msg = err.get("message") if isinstance(err, dict) else None',
     '    msg = err.get("message") if isinstance(err, dict) else err'),

    ("an error body that cannot be read escapes",
     '    except (OSError, ValueError, http.client.HTTPException):\n        return ""',
     '    except ValueError:\n        return ""'),

    ("the token is not scrubbed from an HTTP error",
     '        raise UsageError(_scrub(_http_error_text(exc), token)) from None',
     '        raise UsageError(_http_error_text(exc)) from None'),

    ("an HTTP error chains back to the request",
     '        raise UsageError(_scrub(_http_error_text(exc), token)) from None',
     '        raise UsageError(_scrub(_http_error_text(exc), token))'),

    ("the token is not scrubbed from a network error",
     '            _scrub(f"Failed to load usage data: {reason}", token)) from None',
     '            f"Failed to load usage data: {reason}") from None'),

    ("a network error chains back to the request",
     '            _scrub(f"Failed to load usage data: {reason}", token)) from None',
     '            _scrub(f"Failed to load usage data: {reason}", token))'),

    ("a network failure's reason is wrapped",
     '        reason = getattr(exc, "reason", None) or exc\n',
     '        reason = exc\n'),

    ("a reply cut short escapes",
     '    except (OSError, ValueError, http.client.HTTPException) as exc:\n',
     '    except (OSError, ValueError) as exc:\n'),

    ("a reply that is not JSON escapes",
     '    except ValueError:\n        raise UsageError(',
     '    except KeyError:\n        raise UsageError('),

    ("a reply that is not an object is passed on",
     '    if not isinstance(body, dict):\n',
     '    if False:\n'),

    ("the plan is not reported",
     '"subscription_type": _text(oauth.get("subscriptionType")),',
     '"subscription_type": None,'),

    ("the tier is not reported",
     '"rate_limit_tier": _text(oauth.get("rateLimitTier")),',
     '"rate_limit_tier": None,'),

    ("an empty plan is passed on as a plan",
     '    return value if isinstance(value, str) and value else None',
     '    return value if isinstance(value, str) else None'),

    ("the report does not say whose email",
     'account = {"email": info.get("email"), "config_dir": info.get("config_dir")}',
     'account = {"email": None, "config_dir": info.get("config_dir")}'),

    ("a failed report does not say whose",
     '        return {"error": str(exc), "account": account}',
     '        return {"error": str(exc)}'),

    ("a report does not say whose",
     '    report["account"] = account\n',
     ''),
]

USAGE_SERVER_MUTATIONS = [
    ("/usage is not handled",
     '        if kind == "usage":\n            await _handle_usage(ws, rt)\n            return\n',
     ''),

    ("no loading message",
     '    await send_to(ws, {"type": "command_data", "label": "usage",\n'
     '                       "data": {"loading": True}})\n',
     ''),

    ("the fetch blocks the hub",
     '        data = await asyncio.to_thread(plan_usage.usage_report, config_dir)',
     '        data = plan_usage.usage_report(config_dir)'),

    ("every session gets the hub's account",
     '    config_dir = getattr(rt.config, "config_dir", None)\n'
     '    await send_to(ws, {"type": "command_data", "label": "usage",',
     '    config_dir = None\n'
     '    await send_to(ws, {"type": "command_data", "label": "usage",'),

    ("a bug leaves it loading",
     '        data = {"error": "Failed to load usage data (see the hub log)."}\n',
     '        return\n'),

    ("a bug is not logged",
     '        log.exception("/usage: building the report failed")\n',
     ''),

    ("every tab gets the answer",
     '    await send_to(ws, {"type": "command_data", "label": "usage", "data": data})',
     '    await rt.broadcast({"type": "command_data", "label": "usage", "data": data})'),

    ("the answer is not sent",
     '    await send_to(ws, {"type": "command_data", "label": "usage", "data": data})\n',
     ''),
]

USAGE_CMD_MUTATIONS = [
    ("/usage is not a command",
     '    if cmd == "usage":\n',
     '    if cmd == "usages":\n'),

    ("/usage is not in /help",
     '        ("/usage",                       "plan limits: how much of the 5-hour'
     ' and weekly limits is used, and when they reset"),\n',
     ''),
]

USAGE_CONFIG_MUTATIONS = [
    ("/usage is not offered for completion",
     '"/cost", "/usage", "/cwd"',
     '"/cost", "/cwd"'),
]

USAGE_JS_MUTATIONS = [
    ("the wide bar is another width",
     "  const WIDE_BAR = 50;", "  const WIDE_BAR = 40;"),

    ("the wide layout starts later",
     "  const WIDE_AT = 62;", "  const WIDE_AT = 63;"),

    ("the default width is narrower",
     "  const DEFAULT_COLS = 80;", "  const DEFAULT_COLS = 60;"),

    ("a half cell needs more than half",
     "(exact - whole >= 0.5 ? HALF : EMPTY)",
     "(exact - whole > 0.5 ? HALF : EMPTY)"),

    ("a full bar runs over",
     "    if (whole >= width) return FULL.repeat(width);\n", ""),

    ("a negative figure is not floored at zero",
     "Math.max(0, Number(ratio) || 0) * width", "(Number(ratio) || 0) * width"),

    ("a figure that is not a number is not zero",
     "Math.max(0, Number(ratio) || 0) * width", "Math.max(0, Number(ratio)) * width"),

    ("exactly a day away shows the date",
     "    if (o.alwaysDate || hoursAway > 24) {",
     "    if (o.alwaysDate || hoursAway >= 24) {"),

    ("a weekly limit's reset today shows no date",
     "    if (o.alwaysDate || hoursAway > 24) {",
     "    if (hoursAway > 24) {"),

    ("dated resets always show minutes",
     "        if (minute !== 0) f.minute = '2-digit';\n      }",
     "        f.minute = '2-digit';\n      }"),

    ("time-only resets always show minutes",
     "timeZone: tz };\n      if (minute !== 0) f.minute = '2-digit';",
     "timeZone: tz };\n      f.minute = '2-digit';"),

    ("minutes are read in UTC",
     "    const minute = Number(_part(d, tz, { minute: 'numeric' }, 'minute'));",
     "    const minute = Number(_part(d, 'UTC', { minute: 'numeric' }, 'minute'));"),

    ("the year is never shown",
     "      if (_part(d, tz, { year: 'numeric' }, 'year')\n"
     "          !== _part(now, tz, { year: 'numeric' }, 'year')) {",
     "      if (false) {"),

    ("the year is read in UTC",
     "      if (_part(d, tz, { year: 'numeric' }, 'year')",
     "      if (_part(d, 'UTC', { year: 'numeric' }, 'year')"),

    ("dated resets always show the time",
     "      if (showTime) {\n", "      if (true) {\n"),

    ("am/pm after a narrow no-break space is left",
     "s.replace(/[ \\u202f]([AP]M)/i,", "s.replace(/ ([AP]M)/i,"),

    ("am/pm stays upper case",
     "(_m, ap) => ap.toLowerCase());", "(_m, ap) => ap);"),

    ("the zone named is always this browser's",
     "    return s + ' (' + (tz || localZone()) + ')';",
     "    return s + ' (' + localZone() + ')';"),

    ("an unreadable time is formatted",
     "    if (isNaN(d.getTime())) return null;\n    const now",
     "    const now"),

    ("epoch seconds are read as milliseconds",
     "typeof when === 'number' ? new Date(when * 1000) : new Date(when)",
     "new Date(when)"),

    ("a limit without a figure is drawn",
     "    if (typeof util !== 'number' || !isFinite(util)) return null;",
     "    if (util === null) return null;"),

    ("the percentage is rounded, not rounded down",
     "    const used = Math.floor(util) + '% used';",
     "    const used = Math.round(util) + '% used';"),

    ("a limit with no reset time is given one",
     "    if (limit.resets_at) {\n", "    if (true) {\n"),

    ("what is spent replaces when it resets",
     "if (o.extra) sub = sub ? o.extra + DOT + sub : o.extra;",
     "if (o.extra) sub = o.extra;"),

    ("the credit's note is not used",
     "    if (o.override !== undefined) sub = o.override;\n", ""),

    ("a wide block with no reset gets an empty line",
     "      if (sub) lines.push(sub);", "      lines.push(sub);"),

    ("a narrow block with no reset says so anyway",
     "title + (sub ? DOT + sub : '')", "title + DOT + sub"),

    ("the narrow bar is the wide one",
     "bar(util / 100, o.maxWidth), used]", "bar(util / 100, WIDE_BAR), used]"),

    ("other kinds of model-scoped limit count as weekly",
     "const model = l && l.kind === 'weekly_scoped' && l.scope && l.scope.model;",
     "const model = l && l.scope && l.scope.model;"),

    ("a Sonnet per-model limit is always skipped",
     "      if (sonnetShown && name.toLowerCase() === 'sonnet') continue;",
     "      if (name.toLowerCase() === 'sonnet') continue;"),

    ("every per-model limit is skipped once Sonnet's is shown",
     "      if (sonnetShown && name.toLowerCase() === 'sonnet') continue;",
     "      if (sonnetShown) continue;"),

    ("the Sonnet match is case-sensitive",
     "name.toLowerCase() === 'sonnet'", "name === 'sonnet'"),

    ("a per-model limit's figure is not its percent",
     "limit: { utilization: l.percent, resets_at: l.resets_at },",
     "limit: { utilization: l.utilization, resets_at: l.resets_at },"),

    ("a currency without cents is divided by 100",
     "    const whole = fmt && fmt.resolvedOptions().maximumFractionDigits === 0;",
     "    const whole = false;"),

    ("an unknown currency drops its cents",
     "code + ' ' + amount.toFixed(2)", "code + ' ' + amount"),

    ("an unknown currency code is shown as typed",
     "    const code = String(currency || 'USD').toUpperCase();",
     "    const code = String(currency || 'USD');"),

    ("no currency is not dollars",
     "String(currency || 'USD')", "String(currency)"),

    ("credits reset on the 1st of this month",
     "Date.UTC(y, m, 1, 12)", "Date.UTC(y, m - 1, 1, 12)"),

    ("the credit's expiry is formatted when unreadable",
     "    if (d && !isNaN(d.getTime())) {", "    if (d) {"),

    ("Enterprise credits are not shown",
     "if (!proOrMax && plan !== 'team' && plan !== 'enterprise') return null;",
     "if (!proOrMax && plan !== 'team') return null;"),

    ("Team's credits are shown when off",
     "    if (!x.is_enabled) return proOrMax ? TITLE + '\\nUsage credits are off' : null;",
     "    if (!x.is_enabled) return TITLE + '\\nUsage credits are off';"),

    ("a missing monthly limit is a limit",
     "    if (x.monthly_limit === null || x.monthly_limit === undefined) {",
     "    if (x.monthly_limit === null) {"),

    ("Pro and Max credits with no limit are not unlimited",
     "      if (proOrMax) return TITLE + '\\nUnlimited';\n", ""),

    ("nothing spent is spent",
     "      if (typeof x.used_credits !== 'number') return null;\n      return TITLE",
     "      return TITLE"),

    ("credits with no spend figure are drawn",
     "    }\n    if (typeof x.used_credits !== 'number') return null;\n    let util",
     "    }\n    let util"),

    ("spending over the limit is not capped",
     "Math.max(0, Math.min(100, x.used_credits / x.monthly_limit * 100))",
     "x.used_credits / x.monthly_limit * 100"),

    ("a zero limit is empty, not full",
     "        : 100;\n", "        : 0;\n"),

    ("the endpoint's null figure is used",
     "    if (typeof util !== 'number') {\n      util = x.monthly_limit > 0",
     "    if (util === undefined) {\n      util = x.monthly_limit > 0"),

    ("products with no share are listed",
     "&& typeof r.percent === 'number' && r.percent > 0) : [];",
     "&& typeof r.percent === 'number' && r.percent >= 0) : [];"),

    ("a product's share is not rounded",
     "Math.round(r.percent) + '%'", "r.percent + '%'"),

    ("a nameless product is listed",
     "&& typeof r.display_name === 'string' && r.display_name\n",
     "&& typeof r.display_name === 'string'\n"),

    ("the plan's size is left off",
     "    if (m) name += ' ' + m[1];\n", ""),

    ("a backslash path is not split",
     "p.split(/[\\\\/]+/)", "p.split('/')"),

    ("a trailing separator names nothing",
     ".split(/[\\\\/]+/).filter(Boolean);", ".split(/[\\\\/]+/);"),

    ("the email is not preferred",
     "    const who = (typeof a.email === 'string' && a.email) || _folder(a.config_dir);",
     "    const who = _folder(a.config_dir);"),

    ("an unknown plan gets no Sonnet-only limit",
     "    if (plan === null || plan === 'max' || plan === 'team') {",
     "    if (plan === 'max' || plan === 'team') {"),

    ("Team gets no Sonnet-only limit",
     "    if (plan === null || plan === 'max' || plan === 'team') {",
     "    if (plan === null || plan === 'max') {"),

    ("a Sonnet-only limit shown does not stop the duplicate",
     "scopedWeeklies(u.limits, sonnet !== null)", "scopedWeeklies(u.limits, false)"),

    ("a per-model weekly limit shows no date today",
     "      add(limitBlock(s.title, s.limit, weekly));",
     "      add(limitBlock(s.title, s.limit, o));"),

    ("the weekly limit shows no date today",
     "    add(limitBlock('Current week (all models)', u.seven_day, weekly));",
     "    add(limitBlock('Current week (all models)', u.seven_day, o));"),

    ("an empty report says nothing",
     "    if (!blocks.length) blocks.push('No plan limits reported for this account.');\n",
     ""),

    ("an error shows the limits anyway",
     "    if (data.error) {\n", "    if (false) {\n"),

    ("no reply at all breaks it",
     "    if (!data || typeof data !== 'object') return 'Error: no usage data came back.';\n",
     ""),

    ("a very narrow modal gets a sliver of a bar",
     "o.maxWidth = Math.max(10, (o.cols || DEFAULT_COLS) - 2);",
     "o.maxWidth = (o.cols || DEFAULT_COLS) - 2;"),

    ("the layout is for the whole width, not less 2",
     "(o.cols || DEFAULT_COLS) - 2);", "(o.cols || DEFAULT_COLS));"),

    ("the probe is left in the modal",
     "    probe.remove();\n", ""),

    ("the padding is counted as room",
     "    const inner = el.clientWidth\n"
     "      - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0);",
     "    const inner = el.clientWidth;"),

    ("cells are counted rounding up",
     "Math.floor(inner / cell) : DEFAULT_COLS", "Math.ceil(inner / cell) : DEFAULT_COLS"),

    ("the credit block is not shown",
     "    add(creditBlock(u.cinder_cove, o));\n", ""),

    ("the product line is not shown",
     "    add(byProduct(u.seven_day_breakdown));\n", ""),
]

USAGE_PAGE_MUTATIONS = [
    ("loading does not open the modal",
     "      App.openModal('Usage', 'Loading usage data\\u2026');\n", ""),

    ("an answer to a closed modal is not recognised",
     "      _awaiting = true;\n", ""),

    ("an answer reopens a modal closed while it loaded",
     "(modal.classList.contains('hidden') || title.textContent !== 'Usage')",
     "(title.textContent !== 'Usage')"),

    ("an answer replaces another modal",
     "(modal.classList.contains('hidden') || title.textContent !== 'Usage')",
     "(modal.classList.contains('hidden'))"),

    ("a dropped answer is still awaited",
     "    const awaited = _awaiting;\n    _awaiting = false;\n",
     "    const awaited = _awaiting;\n"),

    ("the report is measured while hidden",
     "    App.openModal('Usage', '');            // shown, so its width can be measured\n",
     ""),

    ("the report is laid out for 80 columns",
     "render(data, { cols: columnsFor(body) })", "render(data, {})"),

    ("a page without the modal loses the report",
     "        || typeof App === 'undefined' || typeof App.openModal !== 'function') {\n"
     "      return false;",
     "        || typeof App === 'undefined' || typeof App.openModal !== 'function') {\n"
     "      return true;"),
]

USAGE_CHAT_MUTATIONS = [
    ("the report is printed, not drawn",
     "    if (msg.label === 'usage' && typeof Usage !== 'undefined' && Usage.show(msg.data)) {\n"
     "      return;\n    }\n",
     ""),

    ("a report with no modal is lost",
     "&& Usage.show(msg.data)) {\n      return;\n    }",
     ") {\n      Usage.show(msg.data);\n      return;\n    }"),
]

USAGE_INDEX_MUTATIONS = [
    ("the page does not load usage.js",
     '  <script src="/static/usage.js"></script>\n', ""),
]


# A launch's --initial-prompt goes to its own session, once, shown, and in a
# turn of its own.  tests/test_initial_prompt.py.
INITPROMPT_BRIDGE_MUTATIONS = [
    ("a connect does not start the settle",
     "        self._turn_ended_at = time.monotonic()\n\n"
     "        # Anything the user typed during the connect went to",
     "        pass\n\n"
     "        # Anything the user typed during the connect went to"),

    ("the launch's prompt is not queued",
     "            state.queued_prompts.append(config.initial_prompt)\n",
     "            pass\n"),

    ("a restarted worker queues it again",
     "        if (config.initial_prompt and not skip_connect\n",
     "        if (config.initial_prompt\n"),

    ("a restored queue gets it twice",
     "\n                and config.initial_prompt not in state.queued_prompts):",
     "):"),

    ("it jumps ahead of what was queued before it",
     "            state.queued_prompts.append(config.initial_prompt)\n",
     "            state.queued_prompts.appendleft(config.initial_prompt)\n"),

    ("the worker's first pop is not made",
     "        if state.queued_prompts and state.queue_editing_index != 0:\n"
     "            next_prompt = await self._pop_queued_prompt()\n"
     "        if next_prompt is None:",
     "        if False:\n"
     "            next_prompt = await self._pop_queued_prompt()\n"
     "        if next_prompt is None:"),

    ("a popped prompt is dropped for the idle wait",
     "            next_prompt = await self._pop_queued_prompt()\n"
     "        if next_prompt is None:",
     "            next_prompt = await self._pop_queued_prompt()\n"
     "        if True:"),
]

INITPROMPT_SERVER_MUTATIONS = [
    ("a new session inherits the hub's prompt",
     '    overrides["initial_prompt"] = (\n'
     '        initial_prompt if initial_prompt and initial_prompt.strip() else None)\n',
     ''),

    ("a blank prompt is kept",
     'initial_prompt if initial_prompt and initial_prompt.strip() else None)',
     'initial_prompt or None)'),

    ("a new session inherits the hub's note",
     '    overrides["session_note"] = session_note or None\n',
     '    if session_note:\n        overrides["session_note"] = session_note\n'),

    ("the hand-over leaves it out",
     '        "initial_prompt": cfg.initial_prompt,\n',
     ''),

    ("the request leaves it out",
     '        "initial_prompt": initial_prompt,\n'
     '        "resume_interrupted_turn": bool(resume_interrupted_turn),\n'
     '    }).encode("utf-8")',
     '        "resume_interrupted_turn": bool(resume_interrupted_turn),\n'
     '    }).encode("utf-8")'),

    ("the launch API ignores it",
     '    initial_prompt = body.get("initial_prompt")\n',
     '    initial_prompt = None\n'),

    ("anything truthy is taken for a prompt",
     '    if not isinstance(initial_prompt, str) or not initial_prompt.strip():\n',
     '    if not initial_prompt:\n'),

    ("a new session is not given it",
     '                                   initial_prompt=initial_prompt,\n',
     '                                   initial_prompt=None,\n'),

    ("an open session is not sent it",
     '                existing.state.queued_prompts.append(initial_prompt)\n',
     '                pass\n'),

    ("an open session gets it twice",
     '\n                    and initial_prompt not in existing.state.queued_prompts):',
     '):'),

    ("/cwd and /resume send it again",
     '    if not _picker_mode:\n'
     '        overrides["initial_prompt"] = None\n'
     '        overrides["session_note"] = None\n',
     ''),

    ("the session picked in the picker loses it",
     '    if not _picker_mode:\n'
     '        overrides["initial_prompt"] = None\n',
     '    if True:\n'
     '        overrides["initial_prompt"] = None\n'),

    ("a restart sends it again",
     '        if a in ("--initial-prompt", "-p", "--session-note"):\n'
     '            i += 2\n'
     '            continue\n',
     ''),

    ("a restart keeps the joined forms",
     '        if (a.startswith(("--initial-prompt=", "--session-note="))\n'
     '                or (a.startswith("-p") and not a.startswith("--"))):\n'
     '            i += 1              # "-pTEXT", argparse\'s attached form\n'
     '            continue\n',
     ''),

    ("a restart keeps -pTEXT",
     '                or (a.startswith("-p") and not a.startswith("--"))):',
     '                or False):'),
]


# --resume is settled before a launch starts or joins anything; a joining
# launch says where its session is.  tests/test_launch_resume.py.
LAUNCHRESUME_SERVER_MUTATIONS = [
    ("the title in the launch directory does not win",
     "    if len(here) == 1:\n        return dataclasses.replace(cfg, resume=here[0][0]), None, None\n",
     ""),

    ("several here are not refused",
     "    if len(here) > 1:\n        ids = {sid for sid, _t in here}\n",
     "    if False:\n        ids = {sid for sid, _t in here}\n"),

    ("a copy elsewhere is not told apart from them",
     "        others = [s for s in named if s[\"session_id\"] not in ids]\n",
     "        others = []\n"),

    ("they are not newest first",
     "        mine.sort(key=lambda s: s.get(\"mtime\") or 0, reverse=True)\n",
     "        mine.sort(key=lambda s: s.get(\"mtime\") or 0)\n"),

    ("a session found elsewhere is not followed",
     "    if len(usable) == 1:\n        return _follow(usable[0])\n",
     ""),

    ("a given directory is not respected",
     "              if (not cfg.cwd_given or _same_path(s.get(\"cwd\"), cfg.cwd))\n",
     "              if True\n"),

    ("a chosen account is not respected",
     "              and (not account_given or _same_path(s.get(\"config_dir\"), account))]",
     "              ]"),

    ("the environment's account is not a choice",
     "    account_given = bool(cfg.config_dir or os.environ.get(\"CLAUDE_CONFIG_DIR\"))",
     "    account_given = bool(cfg.config_dir)"),

    ("the title elsewhere must match its case",
     "             if s[\"title\"] and s[\"title\"].strip().casefold() == want]",
     "             if s[\"title\"] and s[\"title\"].strip() == ref]"),

    ("the directory is not followed",
     "            new = dataclasses.replace(new, cwd=str(Path(where).resolve()))\n",
     ""),

    ("the account is not followed",
     "            new = dataclasses.replace(new, config_dir=s[\"config_dir\"])\n",
     ""),

    ("a directory that is gone is followed",
     "            if not Path(where).is_dir():\n",
     "            if False:\n"),

    ("following says nothing",
     "        return new, f\"{what} is {' and '.join(moved)}: resuming it there.\", None",
     "        return new, None, None"),

    ("an id with a --cwd it is not in goes ahead",
     "        if cfg.cwd_given and where and not _same_path(where, cfg.cwd):\n",
     "        if False:\n"),

    ("an id is not followed to its directory",
     "        return _follow({\"session_id\": ref, \"cwd\": where, \"config_dir\": account,",
     "        return _follow({\"session_id\": ref, \"cwd\": None, \"config_dir\": account,"),

    ("an id on another account is not recognised",
     "    holders = [s for s in _all() if s[\"session_id\"] == ref]\n",
     "    holders = []\n"),

    ("an id on another account is followed past a chosen account",
     "        if account_given:\n            s = holders[0]\n",
     "        if False:\n            s = holders[0]\n"),

    ("an id nobody has is taken for a title",
     "    if _UUID_RE.fullmatch(ref):\n",
     "    if False:\n"),

    ("no titles like it are offered",
     "    similar = [s for s in _all() if s[\"title\"] and want in s[\"title\"].casefold()]\n",
     "    similar = []\n"),

    ("the directory's own sessions are not listed",
     "        here_all = resumable_sessions(cfg.cwd, account)\n",
     "        here_all = []\n"),

    ("the one flag it needs is not named",
     "            lines.append(f\"Resume it with {' '.join(need)}.\" if need else\n",
     "            lines.append(\"Resume it by its id (--resume <id>).\" if need else\n"),

    ("the picker is settled as a title",
     "    if not ref or ref == _PICKER_SENTINEL:\n        return cfg, None, None\n    account = (",
     "    if not ref:\n        return cfg, None, None\n    account = ("),

    ("an unsettled launch goes on to the hub",
     "    if _err:\n        print(_err, file=sys.stderr)\n",
     "    if False:\n        print(_err, file=sys.stderr)\n"),

    ("an unsettled launch leaves no log",
     "        _report_launch_failure(_err, dialog=config.open_browser)\n",
     ""),

    ("the note is not printed",
     "    if _note:\n        print(_note)\n",
     "    if _note:\n        pass\n"),

    ("a followed account is not pinned",
     "        # The session may be on another account: pin it as above.\n"
     "        os.environ[\"CLAUDE_CONFIG_DIR\"] = str(Path(config.config_dir).resolve())\n",
     "        pass\n"),

    ("a joined launch prints no address",
     "    print(f\"Joined running orchestrator2 hub on port {port}: session {rid} \"\n"
     "          f\"at {url}\")\n",
     "    print(f\"Joined running orchestrator2 hub on port {port}: session {rid}\")\n"),

    ("a joined launch opens a tab without --open",
     "    if open_browser:\n        webbrowser.open(f\"{url}&t={int(time.time())}\")\n",
     "    if True:\n        webbrowser.open(f\"{url}&t={int(time.time())}\")\n"),

    ("nothing says why no tab opened",
     "        print(\"(Not opened in a tab; --open does that.)\")\n",
     "        pass\n"),

    ("a detached child is not given the account",
     "    if cfg.config_dir:\n        child_argv.extend([\"--config-dir\", cfg.config_dir])\n",
     ""),

    ("a detached child keeps the typed account too",
     "        if a in (\"--agent-name\", \"--config-dir\"):\n",
     "        if a in (\"--agent-name\",):\n"),

    ("a restart keeps the launch's --cwd",
     "        if sid and a == \"--cwd\":\n            i += 2\n            continue\n",
     ""),

    ("a restart drops --cwd with no session to follow",
     "        if sid and a == \"--cwd\":\n",
     "        if a == \"--cwd\":\n"),

    ("/resume in a session says a shared title is not found",
     "            if len(several) > 1:\n",
     "            if False:\n"),
]

LAUNCHRESUME_CONFIG_MUTATIONS = [
    ("--cwd is never counted as given",
     "        cwd_given=args.cwd is not None,\n",
     "        cwd_given=False,\n"),
]

LAUNCHRESUME_SESSION_MUTATIONS = [
    ("other accounts are not searched",
     "        dirs = [str(p) for p in discover_claude_dirs()]\n",
     "        dirs = [str(p) for p in discover_claude_dirs()][:1]\n"),

    ("a session is listed once per account that has it",
     "                if not sid or sid in seen:\n",
     "                if not sid:\n"),
]


# A page that says when it leaves is asleep unless it said so.
# tests/test_asleep_tab.py and tests/reconnect_on_show.test.js.
BYE_SERVER_MUTATIONS = [
    ("a page's bye=1 is not recorded",
     '    if ws.query_params.get("bye") == "1":\n        _says_bye_ws.add(ws)\n',
     '    if False:\n        _says_bye_ws.add(ws)\n'),

    ("any bye value declares it",
     'ws.query_params.get("bye") == "1"',
     'ws.query_params.get("bye")'),

    ("leaving is heard but not recorded",
     '    if msg_type == "leaving":\n        _leaving_ws.add(ws)\n        return True\n',
     '    if msg_type == "leaving":\n        return True\n'),

    ("leaving is passed on to the chat",
     '    if msg_type == "leaving":\n        _leaving_ws.add(ws)\n        return True\n',
     ''),

    ("the hub does not say it hears leaving",
     'HUB_HEARS = ["visibility", "leaving"]',
     'HUB_HEARS = ["visibility"]'),

    ("a page that says bye is judged by the old rule",
     '    if ws in _says_bye_ws:\n        if ws in _leaving_ws:\n',
     '    if False:\n        if ws in _leaving_ws:\n'),

    ("a page that said it was leaving is still asleep",
     '    if ws in _says_bye_ws:\n        if ws in _leaving_ws:\n',
     '    if ws in _says_bye_ws:\n        if False:\n'),

    ("a page that went without a word is not asleep",
     '        _asleep_ws.add(ws)\n        if where:\n            log.info("tab for %s went without saying',
     '        if where:\n            log.info("tab for %s went without saying'),

    ("the ending of a page that says bye is not explained",
     'log.info("tab for %s went without saying it was leaving (close "',
     'log.info("tab for %s went (close "'),

    ("a page that said so is not logged as leaving",
     'log.info("tab for %s left (it said so; close code %s)", where, shown)',
     'pass'),

    ("an old page's leaving is not logged",
     '            log.info("tab for %s left (close code %s%s)", where, shown,\n'
     '                     "" if ws in _hidden_ws else "; last said it was visible, "\n'
     '                     "or never said")\n',
     '            pass\n'),

    ("a gone page's bye is left behind",
     '    _says_bye_ws.discard(ws)\n    _leaving_ws.discard(ws)\n',
     ''),
]

BYE_APP_MUTATIONS = [
    ("the socket does not declare that the tab says bye",
     "    q.push('bye=1');\n",
     ""),

    ("pagehide says nothing",
     "    window.addEventListener('pagehide', _sayLeaving);\n",
     ""),

    ("a hub that does not hear it is sent it",
     "    if (!_hubHears.includes('leaving')) return;\n",
     ""),

    ("a socket already gone is written to",
     "    if (!ws || ws.readyState !== WebSocket.OPEN) return;\n"
     "    try {\n"
     "      ws.send(JSON.stringify({ type: 'leaving' }));",
     "    try {\n"
     "      ws.send(JSON.stringify({ type: 'leaving' }));"),
]


# Nothing says "working" after the hub has gone.
# tests/test_shutdown_status.py and tests/reconnect_on_show.test.js.
SHUTSTATUS_SERVER_MUTATIONS = [
    ("the ticker goes on after the tabs are told",
     "        if _hub_going:\n"
     "            # The tabs have been told the hub is going; see _broadcast_shutdown.\n"
     "            return\n",
     ""),

    ("telling the tabs does not stop the ticker",
     "    global _hub_going\n    _hub_going = True\n",
     "    global _hub_going\n"),

    ("no last status is sent",
     "        await rt.broadcast({\"type\": \"status_update\", \"status\": status})\n"
     "    await broadcast({\"type\": \"server_shutdown\", \"reason\": reason})\n",
     "    await broadcast({\"type\": \"server_shutdown\", \"reason\": reason})\n"),

    ("the last status still says what it was doing",
     "        status.update(busy_class=\"shutdown\", busy_label=\"server stopped\",\n"
     "                      busy_prefix=None, busy_since=None)\n",
     ""),

    ("the last status keeps its clock",
     "                      busy_prefix=None, busy_since=None)\n",
     "                      )\n"),

    ("a session nobody is viewing is sent one",
     "        if rt.state is None or not rt.clients:\n            continue\n"
     "        try:\n            status = state_to_status_dict(rt.state, rt.config)",
     "        if rt.state is None:\n            continue\n"
     "        try:\n            status = state_to_status_dict(rt.state, rt.config)"),

    ("the last status is sent after the notice",
     "        await rt.broadcast({\"type\": \"status_update\", \"status\": status})\n"
     "    await broadcast({\"type\": \"server_shutdown\", \"reason\": reason})\n",
     "        await broadcast({\"type\": \"server_shutdown\", \"reason\": reason})\n"
     "        await rt.broadcast({\"type\": \"status_update\", \"status\": status})\n"),
]

SHUTSTATUS_APP_MUTATIONS = [
    ("status sent after the notice is applied",
     "      if (_serverShutdown) return;\n      if (msg.status) {\n",
     "      if (msg.status) {\n"),

    ("the notice leaves the stop button up",
     "        connection: true,\n      });\n      _nothingRunning();\n",
     "        connection: true,\n      });\n"),

    ("giving up leaves the stop button up",
     "      Status.update({ busy_label: 'disconnected', busy_class: 'shutdown', connection: true });\n"
     "      _nothingRunning();\n",
     "      Status.update({ busy_label: 'disconnected', busy_class: 'shutdown', connection: true });\n"),

    ("nothing running still leaves the input busy",
     "    _isBusy = false;\n    Commands.setBusy(false);\n",
     "    _isBusy = false;\n"),

    ("/connect after a shutdown still ignores the next hub",
     "    _serverShutdown = false;\n    _retriesExhausted = false;\n    reconnectAttempt = 0;\n",
     "    _retriesExhausted = false;\n    reconnectAttempt = 0;\n"),
]

# One socket per tab, and a numbered message drawn at most once (2026-10-02:
# a session "is showing a lot of things twice").  tests/reconnect_on_show.test.js,
# "one socket per tab".  There is no mutation for the guard on `onopen`: a
# socket is replaced only once it is closing or closed, and neither ever opens,
# so no test can reach it.  It is there because the rule is "a replaced socket
# has no say", not "no say except", and costs nothing.
ONESOCKET_APP_MUTATIONS = [
    ("a replaced socket still speaks for the tab -- the report",
     "    const own = (handler) => (e) => { if (sock === ws) handler(e); };",
     "    const own = (handler) => handler;"),

    ("a replaced socket's late close opens another socket",
     "    sock.onclose = own((e) => {",
     "    sock.onclose = ((e) => {"),

    ("what a replaced socket still delivers is drawn",
     "    sock.onmessage = own((e) => {",
     "    sock.onmessage = ((e) => {"),

    ("a replaced socket's failure is logged as the tab's",
     "    sock.onerror = own((e) => {",
     "    sock.onerror = ((e) => {"),

    ("a prompt sent on the replaced socket gets the new one closed",
     "    _clearPromptWatchdog();\n    // Likewise a catch-up that socket was delivering",
     "    // Likewise a catch-up that socket was delivering"),

    ("a numbered message delivered twice is drawn twice",
     "      if (msg.seq <= _stream.seq) return;\n",
     ""),

    ("only an older number counts as a repeat, so the latest is drawn twice",
     "      if (msg.seq <= _stream.seq) return;\n",
     "      if (msg.seq < _stream.seq) return;\n"),
]

# Which session a claude process holds (2026-10-09: "'sessions' was showing
# 'os b' under 'recent' and not under 'running' even though it was 'working'
# right then").  tests/test_proc_guard.py.
HOLDERS_MUTATIONS = [
    ("--resume=<id>, the form the SDK passes, is not read -- the report",
     "            if arg.startswith(flag + \"=\"):\n",
     "            if False:\n"),

    ("--resume <id>, the form typed in a terminal, is not read",
     "            if arg == flag and i + 1 < len(argv) and not argv[i + 1].startswith(\"-\"):\n",
     "            if False:\n"),

    ("a dash-leading token after --resume is taken for the session",
     "and i + 1 < len(argv) and not argv[i + 1].startswith(\"-\"):\n",
     "and i + 1 < len(argv):\n"),

    ("-r, --resume's short form, is not read",
     "        for flag in (\"--resume\", \"-r\"):\n",
     "        for flag in (\"--resume\",):\n"),

    ("advertisements are not read, so a fresh session is never held elsewhere",
     "            sid = advertised.get(pid) or _resumed_session(argv)\n",
     "            sid = _resumed_session(argv)\n"),

    ("the command line outranks the advertisement",
     "            sid = advertised.get(pid) or _resumed_session(argv)\n",
     "            sid = _resumed_session(argv) or advertised.get(pid)\n"),

    ("an advertisement whose pid now belongs to another process counts",
     "                if abs(p.create_time() - started) > 1.0:\n",
     "                if False:\n"),

    ("an advertisement whose pid is not a claude counts",
     "                if not p.name().lower().startswith(\"claude\"):\n                    continue\n                started",
     "                if False:\n                    continue\n                started"),

    ("our own CLI is held elsewhere",
     "            pid = proc.info[\"pid\"]\n            if pid in mine:\n                continue\n",
     "            pid = proc.info[\"pid\"]\n"),
    # Skipping forks is the forkskip target's.
]

# Ending a session ends what it started (2026-10-09: "after closing my os
# sessions, it didn't stop those sessions' background processes").
# tests/test_session_job.py.
SESSIONJOB_MUTATIONS = [
    ("the session job allows breakaway, so Git Bash lets programs out",
     "            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE\n",
     "            info.BasicLimitInformation.LimitFlags = (\n"
     "                _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_BREAKAWAY_OK)\n"),

    ("the session job is not kill-on-close, so ending it kills nothing",
     "            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE\n",
     "            info.BasicLimitInformation.LimitFlags = 0\n"),

    ("the CLI is never put in the job",
     "                if not k32.AssignProcessToJobObject(job, proc):\n",
     "                if False:\n"),

    ("end() lets go of the job without closing it",
     "        _kernel32().CloseHandle(self._handle)\n        self._handle = None\n",
     "        self._handle = None\n"),
]

SESSIONJOB_BRIDGE_MUTATIONS = [
    ("connect never puts the CLI in a job",
     "                    self._adopt_session_job()\n                    # Log the handshake",
     "                    # Log the handshake"),

    ("the job made is not the CLI's",
     "        self._session_job = proc_guard.SessionJob.adopt(self._cli_pid())\n",
     "        self._session_job = None\n"),

    ("disconnect never ends the job -- the report",
     "        # its transcript: whatever the tree walk could not see.\n        self._end_session_job(cli_pid)\n",
     "        # its transcript: whatever the tree walk could not see.\n"),

    ("the job is ended before the SDK has stopped the CLI",
     "        try:\n            await self.client.disconnect()\n        except Exception:\n"
     "            # Not fatal",
     "        self._end_session_job(cli_pid)\n"
     "        try:\n            await self.client.disconnect()\n        except Exception:\n"
     "            # Not fatal"),

    ("a job whose client is already gone is left running",
     "            # No CLI left to stop, but whatever it started may still run.\n"
     "            self._end_session_job(None)\n",
     ""),

    ("a new CLI keeps the last one's job",
     "        # A job still held belongs to a CLI nothing drives any more.\n"
     "        self._end_session_job(None)\n",
     ""),
]

# The sweep works in a copy, never the project (2026-10-02: a backup taken
# mid-sweep restored copy_session.py as a mutant).  tests/test_mutate_tool.py.
# Every anchor here contains a real newline, which its own entry below spells
# as an escape, so no anchor can match the list that names it.
MUTATE_TOOL_MUTATIONS = [
    ("the sweep mutates the project, not the copy -- the report",
     "            survivors += sweep(label, src_rel, test_rel, mutations, runner,\n"
     "                               root=scratch)\n",
     "            survivors += sweep(label, src_rel, test_rel, mutations, runner,\n"
     "                               root=ROOT)\n"),

    ("the copy is never deleted",
     "    finally:\n        _rmtree(scratch)\n",
     "    finally:\n        pass\n"),

    ("a copy that fails halfway is left behind",
     "    except BaseException:\n        _rmtree(scratch)\n        raise\n",
     "    except BaseException:\n        raise\n"),

    ("a killed run's copy is never deleted",
     "        if pid is not None and path.is_dir() and not psutil.pid_exists(pid):\n"
     "            _rmtree(path)\n",
     "        if False:\n            _rmtree(path)\n"),

    ("a running sweep's copy is deleted from under it",
     "and not psutil.pid_exists(pid):\n            _rmtree(path)\n",
     ":\n            _rmtree(path)\n"),

    ("a name with no pid in it is read as having one",
     "    return int(pid) if pid.isdigit() and sep else None\n",
     "    return int(pid) if pid.isdigit() else None\n"),

    ("a read-only file strands the copy",
     "        os.chmod(failed, stat.S_IWRITE)\n        func(failed)\n",
     "        raise OSError(failed)\n"),

    ("ignored files are copied: logs, caches, a stray nul",
     '"ls-files", "-z", "--cached", "--others",\n             "--exclude-standard"],\n',
     '"ls-files", "-z", "--cached", "--others"],\n'),

    ("an untracked new file is left out of the copy",
     '"ls-files", "-z", "--cached", "--others",\n',
     '"ls-files", "-z", "--cached",\n'),

    ("node_modules is left out, so the jsdom suites cannot run",
     '        if (root / "node_modules").is_dir():\n',
     "        if False:\n"),

    ("without git, logs and bytecode are copied",
     '            if name.endswith((".log", ".pyc")) or name.lower() == "nul":\n',
     "            if False:\n"),

    ("without git, .git and the caches are copied",
     "        dirnames[:] = [d for d in dirnames if d not in _NOT_PROJECT_DIRS]\n",
     "        dirnames[:] = list(dirnames)\n"),

    ("a scratch directory inside the project is allowed",
     "    if parent.resolve().is_relative_to(root.resolve()):\n",
     "    if False:\n"),

    ("bytecode is written, so a same-size mutant can run the old code",
     '    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")\n',
     "    env = dict(os.environ)\n"),

    ("a failing baseline does not say why",
     "    if not run(runner, test_rel, root, show_failure=True):\n",
     "    if not run(runner, test_rel, root):\n"),

    ("each mutant is built on the one before",
     "            mutant = original.replace(old, new)\n",
     "            original = mutant = original.replace(old, new)\n"),

    ("line endings are not kept",
     "            if crlf:\n                mutant = ",
     "            if False:\n                mutant = "),

    ("the source is not put back for the next target",
     "    finally:\n        src.write_bytes(raw)\n",
     "    finally:\n        pass\n"),

    ("a leftover .mutbak is ignored, and a mutant is copied as the source",
     "    left = _left_mutated(ROOT)\n    if left:\n",
     "    left = _left_mutated(ROOT)\n    if False:\n"),
]


# An opened session leaves a cut-off turn waiting, and says so (2026-09-29).
# tests/test_cut_off_turn.py, tests/test_resume_interrupted_turn.py,
# tests/test_lost_bg_tasks.py, tests/history_backfill.test.js.
CUTOFF_SESSION_MUTATIONS = [
    ("a tool call that never came back is not seen",
     "            if pending:\n                stop, tool = i,",
     "            if False:\n                stop, tool = i,"),

    ("a turn that ended on an API error is called cut off",
     "            if rec.get(\"isApiErrorMessage\"):\n                return None",
     "            if False:\n                return None"),

    ("the model's last word is looked past, so a finished session is called cut off",
     "            if said:\n                return None",
     "            if False:\n                return None"),

    ("output that had only begun is taken for the model's last word",
     "            continue                  # thinking, or nothing yet: keep looking",
     "            return None"),

    ("a result the model never answered is not seen",
     "        if _has_tool_result(rec):\n            stop = i                  # a result the model never answered",
     "        if False:\n            stop = i                  # a result the model never answered"),

    ("a slash command is taken for a prompt owed a reply",
     "        if _record_text(msg).startswith(_COMMAND_RECORD_PREFIXES):\n"
     "            continue                  # a command, not a prompt\n",
     ""),

    ("an earlier resume's bookkeeping is taken for the turn",
     "        if _is_cli_resume_bookkeeping(rec):\n            continue\n        t = rec.get(\"type\")",
     "        t = rec.get(\"type\")"),

    ("the CLI's report of dead tasks is not recognised",
     "_CLI_ORPHAN_REPORT = \"didn't finish before the previous session ended\"",
     "_CLI_ORPHAN_REPORT = \"didn't finish before the previous session ended-no\""),

    ("the CLI's placeholder is taken for the model's last word",
     "    if t == \"assistant\" and isinstance(msg, dict) \\\n"
     "            and msg.get(\"model\") == \"<synthetic>\" \\\n"
     "            and _record_text(msg) == CLI_NO_RESPONSE:\n        return True\n",
     ""),

    ("a prompt sent mid-turn is not seen",
     "                stop = i              # a prompt sent mid-turn, never answered\n                break",
     "                continue"),

    ("a skill's text behind its command is taken for the prompt",
     "    elif rec.get(\"isMeta\"):\n        return None\n    else:",
     "    else:"),

    ("a command's line is shown as its raw record",
     "            text = _command_line(text) or \"\"",
     "            text = text"),

    ("the prompt of a turn older than the tail is never looked for",
     "    if start > 0:\n        found = _turn_start_before(jsonl, start)",
     "    if False:\n        found = _turn_start_before(jsonl, start)"),

    ("a compaction summary is taken for the prompt",
     "    if rec.get(\"type\") != \"user\" or rec.get(\"isSidechain\") \\\n"
     "            or rec.get(\"isCompactSummary\") or _has_tool_result(rec) \\\n",
     "    if rec.get(\"type\") != \"user\" or rec.get(\"isSidechain\") \\\n"
     "            or _has_tool_result(rec) \\\n"),

    ("a subagent's record is taken for the session's",
     "        if not isinstance(rec, dict) or rec.get(\"isSidechain\"):\n            continue",
     "        if not isinstance(rec, dict):\n            continue"),

    ("the notice does not say what to do",
     "    return text + (\" It won't carry on by itself: send ",
     "    return text + (\" send "),

    ("a long prompt is quoted whole",
     "        if len(line) > 80:",
     "        if False:"),

    ("a turn that stopped after its tool finished is said to be running it",
     "    if info.get(\"phase\") == \"tool\" and tool:",
     "    if tool:"),

    ("another day's time reads as today's",
     "    if when.date() == now.date():\n        return clock",
     "    if True:\n        return clock"),

    ("history draws the CLI's prompt as the user's -- the 2026-09-06 report",
     "                if rec.get(\"isMeta\") and text == CLI_CONTINUE_PROMPT:\n"
     "                    classified = \"injected_prompt\"\n",
     ""),

    ("history draws a prompt the user typed as the CLI's",
     "                if rec.get(\"isMeta\") and text == CLI_CONTINUE_PROMPT:",
     "                if text == CLI_CONTINUE_PROMPT:"),

    ("history draws the placeholder as the model refusing",
     "            if msg.get(\"model\") == \"<synthetic>\" \\\n"
     "                    and _record_text(msg) == CLI_NO_RESPONSE:\n                messages.append({",
     "            if False:\n                messages.append({"),

    ("history hides a real reply that happens to say the same",
     "            if msg.get(\"model\") == \"<synthetic>\" \\\n"
     "                    and _record_text(msg) == CLI_NO_RESPONSE:\n                messages.append({",
     "            if _record_text(msg) == CLI_NO_RESPONSE:\n                messages.append({"),
]

CUTOFF_BRIDGE_MUTATIONS = [
    ("an opened session's cut-off turn is never looked for",
     "        if resume_sid and not self._finishing_cut_off_turn:\n"
     "            cut_off = await asyncio.to_thread(self._read_cut_off_turn, resume_sid)",
     "        if False:\n"
     "            cut_off = await asyncio.to_thread(self._read_cut_off_turn, resume_sid)"),

    ("a recovery announces a turn its CLI is about to finish",
     "        if resume_sid and not self._finishing_cut_off_turn:",
     "        if resume_sid:"),

    ("the cut-off turn is found and never mentioned",
     "            await self._post_open_notice(\"warning\", describe_cut_off_turn(cut_off))",
     "            pass"),

    ("a tab that attaches later is not told",
     "        self.state.open_notices.append(notice)\n",
     ""),

    ("a new connect keeps what the last one said",
     "        self.state.open_notices = []\n        await self._report_lost_bg_tasks(resume_sid)",
     "        await self._report_lost_bg_tasks(resume_sid)"),

    ("the call to action comes before the dead tasks, above them",
     "        self.state.open_notices = []\n"
     "        await self._report_lost_bg_tasks(resume_sid)\n"
     "        if cut_off:\n"
     "            log.warning(\n"
     "                \"opened with its last turn cut off at %s (%s %s): left waiting\",\n"
     "                cut_off.get(\"stopped\"), cut_off.get(\"phase\"),\n"
     "                cut_off.get(\"tool\") or \"-\")\n"
     "            await self._post_open_notice(\"warning\", describe_cut_off_turn(cut_off))\n",
     "        self.state.open_notices = []\n"
     "        if cut_off:\n"
     "            await self._post_open_notice(\"warning\", describe_cut_off_turn(cut_off))\n"
     "        await self._report_lost_bg_tasks(resume_sid)\n"),

    ("a ghost turn leaves the open's notices to be replayed",
     "        # The session is working again, so what it said when it was opened is\n"
     "        # history -- as in run_turn.\n"
     "        state.open_notices = []\n",
     ""),

    ("a turn of ours leaves the open's notices to be replayed",
     "        # And what the session had to say when it was opened is now history:\n"
     "        # a tab attaching from here on is not told it again.\n"
     "        state.open_notices = []\n",
     ""),
]

CUTOFF_SERVER_MUTATIONS = [
    ("a tab that attaches after the open is not told",
     "    for notice in list(getattr(state, \"open_notices\", None) or []):\n"
     "        await send_to(ws, {\"type\": \"system_msg\", **notice})",
     ""),

    ("a runtime ignores what it was opened for",
     "    if resume_interrupted_turn is not None:\n"
     "        overrides[\"resume_interrupted_turn\"] = bool(resume_interrupted_turn)",
     ""),

    ("a launch that joins a hub drops the flag",
     "        \"resume_interrupted_turn\": cfg.resume_interrupted_turn,\n    }",
     "    }"),

    ("the flag is not put on the wire",
     "        \"resume_interrupted_turn\": bool(resume_interrupted_turn),\n    }).encode(\"utf-8\")",
     "    }).encode(\"utf-8\")"),

    ("the hub ignores the launch's flag",
     "    resume_interrupted_turn = body.get(\"resume_interrupted_turn\") is True",
     "    resume_interrupted_turn = False"),

    ("anything truthy on the wire turns it on",
     "    resume_interrupted_turn = body.get(\"resume_interrupted_turn\") is True",
     "    resume_interrupted_turn = bool(body.get(\"resume_interrupted_turn\"))"),

    ("the hub does not pass it to the session it opens",
     "                                   resume_interrupted_turn=resume_interrupted_turn)",
     "                                   resume_interrupted_turn=None)"),

    ("a moved session abandons the turn the move cut",
     "            agent_labels=carry[\"agent_labels\"],\n            resume_interrupted_turn=True)",
     "            agent_labels=carry[\"agent_labels\"])"),

    ("an original reopened after a failed move abandons it",
     "                agent_labels=carry[\"agent_labels\"],\n                resume_interrupted_turn=True)",
     "                agent_labels=carry[\"agent_labels\"])"),
]

CUTOFF_CONFIG_MUTATIONS = [
    ("--resume-interrupted-turn is on by default -- the report",
     "        action=\"store_true\",\n        default=False,\n        help=(\n"
     "            \"When you open a session whose last turn was cut off",
     "        action=\"store_true\",\n        default=True,\n        help=(\n"
     "            \"When you open a session whose last turn was cut off"),

    ("a Config built by hand finishes the turn",
     "    resume_interrupted_turn: bool = False",
     "    resume_interrupted_turn: bool = True"),
]

CUTOFF_CHAT_MUTATIONS = [
    ("what arrives while a session loads is drawn above its history again",
     "    if ((_replayInProgress || _loadingEl || _catchingUp)\n",
     "    if ((_replayInProgress || _catchingUp)\n"),

    ("the end of loading is held too, so loading never ends",
     "    ['history', 'history_prepend', 'session_loading', 'clear_screen']);",
     "    ['history', 'history_prepend', 'clear_screen']);"),

    ("held for a history that is not coming",
     "        if (!msg.on) _flushPending();   // no history is coming after all\n",
     ""),

    ("held behind an empty history",
     "    if (!messages || !messages.length) { _flushPending(); return; }",
     "    if (!messages || !messages.length) return;"),

    ("what was held for a session left behind is drawn in the next",
     "    // Held back for the view being wiped; drawn now, they would land in the\n"
     "    // next one.\n"
     "    _pendingMessages = [];\n",
     ""),

    ("a replay that ends while the next session loads draws its messages early",
     "    if (_replayInProgress || _loadingEl || _catchingUp) return;\n"
     "    const pending = _pendingMessages;",
     "    if (_replayInProgress || _catchingUp) return;\n"
     "    const pending = _pendingMessages;"),
]


# A file too large for the stream, and what the recovery said (2026-10-01).
# tests/test_dead_cli_recovery.py.
DEADCLI_LIMIT_MUTATIONS = [
    ("the limit is back to 10 MB, under the report's 10.4 MB file",
     "MAX_SDK_MESSAGE_BYTES = 256 * 1024 * 1024",
     "MAX_SDK_MESSAGE_BYTES = 10 * 1024 * 1024"),
]

DEADCLI_BTW_MUTATIONS = [
    ("a /btw fork reads at the old limit",
     "        \"max_buffer_size\": MAX_SDK_MESSAGE_BYTES,",
     "        \"max_buffer_size\": 10 * 1024 * 1024,"),
]

DEADCLI_RECOVERY_MUTATIONS = [
    ("the session reads at the old limit",
     "            \"max_buffer_size\": MAX_SDK_MESSAGE_BYTES,",
     "            \"max_buffer_size\": 10 * 1024 * 1024,"),

    ("what the detector saw kill it is not kept",
     "        self._transport_dead = True\n        self._death_reason = why\n",
     "        self._transport_dead = True\n"),

    ("the turn's own failure is shown -- the report",
     "        shown = _death_explained(self._death_reason or why)",
     "        shown = _death_explained(why)"),

    ("the reason is carried on to the next death",
     "        self._death_reason = None\n\n        if not self._may_auto_reconnect():",
     "\n        if not self._may_auto_reconnect():"),

    ("the buffer error is shown in the SDK's words",
     "    if \"exceeded maximum buffer size\" in (reason or \"\"):",
     "    if False:"),

    ("a ghost turn outlives its CLI, reading working for good -- the report",
     "        if self.state.busy and not self.turn_active.is_set():\n"
     "            self._abandon_ghost_turn()",
     "        pass"),

    ("a turn of ours is closed under it",
     "        if self.state.busy and not self.turn_active.is_set():\n"
     "            self._abandon_ghost_turn()",
     "        if self.state.busy:\n"
     "            self._abandon_ghost_turn()"),

    ("abandoning a ghost turn leaves it busy",
     "                    \"(elapsed=%.1fs)\", elapsed)\n        state.busy = False\n",
     "                    \"(elapsed=%.1fs)\", elapsed)\n"),

    ("abandoning a ghost turn leaves a prompt waiting on its end",
     "        state.active_tools.clear()\n        self._ghost_settled.set()\n"
     "        self._turn_ended_at = time.monotonic()\n\n    # ----",
     "        state.active_tools.clear()\n"
     "        self._turn_ended_at = time.monotonic()\n\n    # ----"),

    ("a death mid-turn is answered with re-send your last prompt -- the report",
     "        if self._last_reconnect_recovered:\n            text = (",
     "        if False:\n            text = ("),

    ("whether the reconnect finished a turn is not recorded",
     "        self._last_reconnect_recovered = recovering\n",
     ""),

    ("it does not warn that a step may be repeated -- the report's double edit",
     "                    \"Its last steps may not have been saved before it died, \"\n"
     "                    \"so it may repeat one that was already done.\")",
     "                    \"\")"),
]


# A tab Chrome had frozen "replays the recent history fairly slowly" when it is
# shown again (2026-10-10): its backlog was drawn a message at a time, with a
# repaint and a layout or more between, and a view following the bottom
# stopped following partway.  tests/history_backfill.test.js, "following
# through a burst".  No mutation for endCatchUp's `if (!_catchingUp) return;`
# or for clear() emptying _thinkingToFit: one spares a flush that would draw
# nothing, the other frees the summaries of a view already gone, and no test
# can tell either from its absence.
CATCHUP_CHAT_MUTATIONS = [
    ("a burst stops a following view following -- the report",
     "        } else if (elMessages.scrollTop < _followedTop - 2) {\n",
     "        } else {\n"),

    ("a view moved up by anything but wheel or keys keeps following",
     "        } else if (elMessages.scrollTop < _followedTop - 2) {\n",
     "        } else if (false) {\n"),

    ("following does not note where it left the view",
     "    elMessages.scrollTop = elMessages.scrollHeight;\n"
     "    _followedTop = elMessages.scrollTop;\n",
     "    elMessages.scrollTop = elMessages.scrollHeight;\n"),

    ("a trim reads as the user scrolling away",
     "    if (_autoScroll) _followedTop = elMessages.scrollTop;\n",
     ""),

    ("a catch-up is drawn a message at a time -- the report",
     "    if ((_replayInProgress || _loadingEl || _catchingUp)\n",
     "    if ((_replayInProgress || _loadingEl)\n"),

    ("a catch-up is drawn early by whatever else draws what waits",
     "    if (_replayInProgress || _loadingEl || _catchingUp) return;\n",
     "    if (_replayInProgress || _loadingEl) return;\n"),

    ("a catch-up is never drawn",
     "      _flushPending();\n    } finally {\n",
     "    } finally {\n"),

    ("one message that fails to draw loses the rest of a catch-up",
     "      try {\n        _dispatchMessage(m);\n      } catch (e) {\n",
     "      {\n        _dispatchMessage(m);\n      } if (false) {\n"),

    ("a catch-up reads the layout for every run it collapses",
     "    const _wasPinned = _autoScroll && !_replayInProgress && !_drawingInBulk;\n",
     "    const _wasPinned = _autoScroll && !_replayInProgress;\n"),

    ("a collapse reads the view's top when nothing will use it",
     "    const _beforeTop = _wasPinned ? elMessages.scrollTop : 0;\n",
     "    const _beforeTop = elMessages.scrollTop;\n"),

    ("a collapse reads the list's height when nothing will use it",
     "    const _beforeH = _wasPinned ? elMessages.scrollHeight : 0;\n",
     "    const _beforeH = elMessages.scrollHeight;\n"),

    ("a collapse measures the content when nothing will use it",
     "    const _beforeContentH = _wasPinned ? _contentHeight() : 0;\n",
     "    const _beforeContentH = _contentHeight();\n"),

    ("a catch-up is followed a frame late, after a scroll check may have run",
     "    if (_hidden()) _scrollToBottom(); else _scrollNow();\n",
     "    _scrollToBottom();\n"),

    ("clearing holds what comes next for the view it wiped",
     "    _pendingMessages = [];\n    _catchingUp = false;\n",
     "    _pendingMessages = [];\n"),

    ("a catch-up measures each thinking summary as it is drawn",
     "    if (isSingleLine && nChars > 0 && (_drawingInBulk || _replayInProgress)) {\n",
     "    if (isSingleLine && nChars > 0 && _replayInProgress) {\n"),

    ("a history measures each thinking summary as it is drawn",
     "    if (isSingleLine && nChars > 0 && (_drawingInBulk || _replayInProgress)) {\n",
     "    if (isSingleLine && nChars > 0 && _drawingInBulk) {\n"),

    ("every thinking block asks whether it is visible, a layout each",
     "    } else if (isSingleLine && nChars > 0 && el.offsetParent !== null) {\n",
     "    } else if (el.offsetParent !== null && isSingleLine && nChars > 0) {\n"),

    ("a catch-up's thinking summaries are never fitted",
     "    _fitThinking(_takeThinkingToFit());\n    // Followed now",
     "    // Followed now"),

    ("a history's thinking summaries are never fitted",
     "        _fitThinking(_takeThinkingToFit());\n        _replayInProgress = false;\n",
     "        _replayInProgress = false;\n"),

    ("a summary too wide for its line shows its text anyway",
     "      if (tooWide[i]) s.textContent = hints[i];\n",
     ""),

    ("a summary in a shut group is fitted where nothing can be measured",
     "    const visible = summaries.filter(s => s.offsetParent !== null);\n",
     "    const visible = summaries;\n"),

    ("opening a group leaves the summaries it hid unfitted",
     "        fitThinking(content);\n",
     ""),
]

# The same, from app.js: where a backlog ends, and what else ends one.
# tests/reconnect_on_show.test.js, "a resumed tab's backlog is drawn in one
# go".  No mutation for the `msg.seq > _stream.seq` that starts one: with
# nothing missed, the check after each message ends a catch-up straight after
# the attach that began it, so the guard only spares a begin and an end that
# draw nothing.  (`>=` there survived a sweep, as it must.)
CATCHUP_APP_MUTATIONS = [
    ("a resumed tab draws its backlog a message at a time -- the report",
     "        Chat.beginCatchUp();\n",
     ""),

    ("a backlog is held until the timer, not until its last message",
     "      if (_catchUpTo !== null && _stream && _stream.seq >= _catchUpTo) _endCatchUp();\n",
     ""),

    ("a backlog's last message does not end it",
     "_stream.seq >= _catchUpTo) _endCatchUp();",
     "_stream.seq > _catchUpTo) _endCatchUp();"),

    ("a backlog cut off by a dropped socket stays held",
     "      _clearPromptWatchdog();\n      _endCatchUp();\n",
     "      _clearPromptWatchdog();\n"),

    ("a backlog cut off by a replaced socket stays held",
     "    // The resume position has already moved past it, so it must be on screen.\n"
     "    _endCatchUp();\n",
     "    // The resume position has already moved past it, so it must be on screen.\n"),

    ("a backlog whose end never comes is held for good",
     "        _catchUpTimer = setTimeout(_endCatchUp, CATCH_UP_MAX_MS);\n",
     ""),

    ("a group opened to show a tool leaves the thinking it hid unfitted",
     "      Chat.fitThinking(group);\n",
     ""),
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
    "movestop": ("server.py", "tests/test_move_stops_original.py",
                 MOVESTOP_MUTATIONS, "pytest"),
    "movestop-wakeup": ("wakeup_store.py", "tests/test_move_stops_original.py",
                        MOVESTOP_WAKEUP_MUTATIONS, "pytest"),
    "asleep-server": ("server.py", "tests/test_asleep_tab.py",
                      ASLEEP_SERVER_MUTATIONS, "pytest"),
    "asleep-app": ("static/app.js", "tests/reconnect_on_show.test.js",
                   ASLEEP_APP_MUTATIONS, "node"),
    "resume-runtime": ("session_runtime.py", "tests/test_resume_stream.py",
                       RESUME_RUNTIME_MUTATIONS, "pytest"),
    "resume-server": ("server.py", "tests/test_resume_stream.py",
                      RESUME_SERVER_MUTATIONS, "pytest"),
    "resume-app": ("static/app.js", "tests/reconnect_on_show.test.js",
                   RESUME_APP_MUTATIONS, "node"),
    "resume-status": ("static/status.js", "tests/reconnect_on_show.test.js",
                      RESUME_STATUS_MUTATIONS, "node"),
    "cliqueue-bridge": ("sdk_bridge.py", "tests/test_cli_queue_race.py",
                        CLIQUEUE_BRIDGE_MUTATIONS, "pytest"),
    "cliqueue-history": ("session.py", "tests/test_cli_queue_race.py",
                         CLIQUEUE_HISTORY_MUTATIONS, "pytest"),
    "cliqueue-chat": ("static/chat.js", "tests/peer_message.test.js",
                      CLIQUEUE_CHAT_MUTATIONS, "node"),
    "favicon": ("static/favicon.js", "tests/favicon.test.js",
                FAVICON_MUTATIONS, "node"),
    "favicon-status": ("static/status.js", "tests/reconnect_on_show.test.js",
                       FAVICON_STATUS_MUTATIONS, "node"),
    "favicon-lobby": ("static/lobby.js", "tests/reconnect_on_show.test.js",
                      FAVICON_LOBBY_MUTATIONS, "node"),
    "loopkeep-bridge": ("sdk_bridge.py", "tests/test_loops_survive_restart.py",
                        LOOPKEEP_BRIDGE_MUTATIONS, "pytest"),
    "loopkeep-server": ("server.py", "tests/test_loops_survive_restart.py",
                        LOOPKEEP_SERVER_MUTATIONS, "pytest"),
    "loopkeep-restore": ("server.py", "tests/test_wakeup_store.py",
                         LOOPKEEP_RESTORE_MUTATIONS, "pytest"),
    "scrollburst": ("static/chat.js", "tests/hidden_window.test.js",
                    SCROLLBURST_MUTATIONS, "node"),
    "scrollburst-app": ("static/app.js", "tests/reconnect_on_show.test.js",
                        SCROLLBURST_APP_MUTATIONS, "node"),
    "statustimer": ("static/status.js", "tests/reconnect_on_show.test.js",
                    STATUSTIMER_MUTATIONS, "node"),
    "busysince": ("state.py", "tests/test_status_busy_since.py",
                  BUSYSINCE_MUTATIONS, "pytest"),
    "statusagent-js": ("static/status.js", "tests/reconnect_on_show.test.js",
                       STATUSAGENT_JS_MUTATIONS, "node"),
    "statusagent": ("sdk_bridge.py", "tests/test_status_agent_name.py",
                    STATUSAGENT_MUTATIONS, "pytest"),
    "statusagent-state": ("state.py", "tests/test_status_agent_name.py",
                          STATUSAGENT_STATE_MUTATIONS, "pytest"),
    "statusagent-server": ("server.py", "tests/test_status_agent_name.py",
                           STATUSAGENT_SERVER_MUTATIONS, "pytest"),
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
    "usage-py": ("plan_usage.py", "tests/test_usage.py",
                 USAGE_PY_MUTATIONS, "pytest"),
    "usage-server": ("server.py", "tests/test_usage_hub.py",
                     USAGE_SERVER_MUTATIONS, "pytest"),
    "usage-cmd": ("commands.py", "tests/test_usage.py",
                  USAGE_CMD_MUTATIONS, "pytest"),
    "usage-config": ("config.py", "tests/test_usage.py",
                     USAGE_CONFIG_MUTATIONS, "pytest"),
    "usage-js": ("static/usage.js", "tests/usage.test.js",
                 USAGE_JS_MUTATIONS, "node"),
    "usage-page": ("static/usage.js", "tests/reconnect_on_show.test.js",
                   USAGE_PAGE_MUTATIONS, "node"),
    "usage-chat": ("static/chat.js", "tests/reconnect_on_show.test.js",
                   USAGE_CHAT_MUTATIONS, "node"),
    "usage-index": ("static/index.html", "tests/reconnect_on_show.test.js",
                    USAGE_INDEX_MUTATIONS, "node"),
    "initprompt-bridge": ("sdk_bridge.py", "tests/test_initial_prompt.py",
                          INITPROMPT_BRIDGE_MUTATIONS, "pytest"),
    "initprompt-server": ("server.py", "tests/test_initial_prompt.py",
                          INITPROMPT_SERVER_MUTATIONS, "pytest"),
    "launchresume-server": ("server.py", "tests/test_launch_resume.py",
                            LAUNCHRESUME_SERVER_MUTATIONS, "pytest"),
    "launchresume-config": ("config.py", "tests/test_launch_resume.py",
                            LAUNCHRESUME_CONFIG_MUTATIONS, "pytest"),
    "launchresume-session": ("session.py", "tests/test_launch_resume.py",
                             LAUNCHRESUME_SESSION_MUTATIONS, "pytest"),
    "bye-server": ("server.py", "tests/test_asleep_tab.py",
                   BYE_SERVER_MUTATIONS, "pytest"),
    "bye-app": ("static/app.js", "tests/reconnect_on_show.test.js",
                BYE_APP_MUTATIONS, "node"),
    "shutstatus-server": ("server.py", "tests/test_shutdown_status.py",
                          SHUTSTATUS_SERVER_MUTATIONS, "pytest"),
    "shutstatus-app": ("static/app.js", "tests/reconnect_on_show.test.js",
                       SHUTSTATUS_APP_MUTATIONS, "node"),
    "onesocket-app": ("static/app.js", "tests/reconnect_on_show.test.js",
                      ONESOCKET_APP_MUTATIONS, "node"),
    "mutate-tool": ("tools/mutate.py", "tests/test_mutate_tool.py",
                    MUTATE_TOOL_MUTATIONS, "pytest"),
    "holders": ("proc_guard.py", "tests/test_proc_guard.py",
                HOLDERS_MUTATIONS, "pytest"),
    "sessionjob": ("proc_guard.py", "tests/test_session_job.py",
                   SESSIONJOB_MUTATIONS, "pytest"),
    "sessionjob-bridge": ("sdk_bridge.py", "tests/test_session_job.py",
                          SESSIONJOB_BRIDGE_MUTATIONS, "pytest"),
    "cutoff-session": ("session.py", "tests/test_cut_off_turn.py",
                       CUTOFF_SESSION_MUTATIONS, "pytest"),
    "cutoff-bridge": ("sdk_bridge.py", "tests/test_cut_off_turn.py",
                      CUTOFF_BRIDGE_MUTATIONS, "pytest"),
    "cutoff-server": ("server.py",
                      "tests/test_cut_off_turn.py tests/test_resume_interrupted_turn.py",
                      CUTOFF_SERVER_MUTATIONS, "pytest"),
    "cutoff-config": ("config.py", "tests/test_resume_interrupted_turn.py",
                      CUTOFF_CONFIG_MUTATIONS, "pytest"),
    "cutoff-chat": ("static/chat.js", "tests/history_backfill.test.js",
                    CUTOFF_CHAT_MUTATIONS, "node"),
    "deadcli-recovery": ("sdk_bridge.py", "tests/test_dead_cli_recovery.py",
                         DEADCLI_RECOVERY_MUTATIONS, "pytest"),
    "deadcli-limit": ("config.py", "tests/test_dead_cli_recovery.py",
                      DEADCLI_LIMIT_MUTATIONS, "pytest"),
    "deadcli-btw": ("btw.py", "tests/test_dead_cli_recovery.py",
                    DEADCLI_BTW_MUTATIONS, "pytest"),
    "extauth": ("server.py",
                "tests/test_external_access_policy.py tests/test_external_auth.py",
                EXTAUTH_MUTATIONS, "pytest"),
    "catchup-chat": ("static/chat.js", "tests/history_backfill.test.js",
                     CATCHUP_CHAT_MUTATIONS, "node"),
    "catchup-app": ("static/app.js", "tests/reconnect_on_show.test.js",
                    CATCHUP_APP_MUTATIONS, "node"),
}


# --- Running: always in a copy of the project ---------------------------------

#: Every scratch copy is named this, then the pid of the run that owns it.  A
#: run killed outright cannot delete its own copy; the next run deletes any
#: whose owner has gone.
SCRATCH_PREFIX = "orchestrator2-mutate-"

#: What a walk leaves out when git cannot say what the project is.
_NOT_PROJECT_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache",
                     ".claude"}


def _project_files(root: Path) -> list[Path]:
    """The files a copy of *root* needs, relative to it.

    What git tracks, plus the untracked files it would add: a new test not yet
    committed is part of the project.  Ignored files stay behind -- a log that
    runs to hundreds of megabytes, caches, and a stray ``nul`` that Windows
    reads as its null device.  A tracked file deleted from the working tree is
    not copied either: the copy is the project as it stands.
    """
    try:
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
             "--exclude-standard"],
            capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return _walked_files(root)
    names = {name for name in listed.decode("utf-8").split("\0") if name}
    return sorted(Path(n) for n in names if (root / n).is_file())


def _walked_files(root: Path) -> list[Path]:
    """``_project_files`` without git: everything but the usual junk."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _NOT_PROJECT_DIRS]
        for name in filenames:
            if name.endswith((".log", ".pyc")) or name.lower() == "nul":
                continue
            found.append(Path(dirpath, name).relative_to(root))
    return sorted(found)


def _scratch_parent() -> Path:
    """Where copies go: ORCH2_MUTATE_SCRATCH, or else the system temp directory.

    The temp directory rather than beside the project: it is usually on the
    faster drive, and backups skip it.  The suites do not depend on which
    drive they run from.
    """
    return Path(os.environ.get("ORCH2_MUTATE_SCRATCH") or tempfile.gettempdir())


def _owner_pid(name: str) -> int | None:
    """The pid in a scratch copy's name, or None if *name* is not one."""
    if not name.startswith(SCRATCH_PREFIX):
        return None
    pid, sep, _rest = name[len(SCRATCH_PREFIX):].partition("-")
    return int(pid) if pid.isdigit() and sep else None


def _delete_abandoned(parent: Path) -> None:
    """Delete copies whose run has gone: one killed outright runs no
    ``finally``.  A copy whose owner is still running is another sweep's."""
    try:
        import psutil
    except ImportError:   # in requirements.txt; without it, delete them by hand
        return
    for path in parent.glob(SCRATCH_PREFIX + "*"):
        pid = _owner_pid(path.name)
        if pid is not None and path.is_dir() and not psutil.pid_exists(pid):
            _rmtree(path)


def make_scratch(root: Path) -> Path:
    """Copy *root* to a new scratch directory and return the copy.

    It holds the project's files (``_project_files``) and node_modules, which
    the jsdom tests load.  No __pycache__ comes along; see ``run``.
    """
    parent = _scratch_parent()
    if parent.resolve().is_relative_to(root.resolve()):
        # git would list the copies as untracked project files, and the next
        # copy would contain the last one.
        raise SystemExit(f"ORCH2_MUTATE_SCRATCH ({parent}) must be outside "
                         f"the project ({root})")
    parent.mkdir(parents=True, exist_ok=True)
    _delete_abandoned(parent)
    scratch = Path(tempfile.mkdtemp(prefix=f"{SCRATCH_PREFIX}{os.getpid()}-",
                                    dir=parent))
    try:
        for rel in _project_files(root):
            (scratch / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / rel, scratch / rel)
        if (root / "node_modules").is_dir():
            shutil.copytree(root / "node_modules", scratch / "node_modules",
                            ignore_dangling_symlinks=True)
    except BaseException:
        _rmtree(scratch)
        raise
    return scratch


def _rmtree(path: Path) -> bool:
    """Delete *path*, and say so if it cannot be done.

    Retries for a few seconds: on Windows a process that has just exited, or a
    virus scanner reading a file it has just seen written, can hold a handle a
    moment longer.  A read-only file is made writable and tried again.  Never
    raises: only the copy is at stake, and the next run tries again.
    """
    def writable(func, failed, _exc):
        os.chmod(failed, stat.S_IWRITE)
        func(failed)

    for _attempt in range(20):
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=writable)
            else:
                shutil.rmtree(path, onerror=writable)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(0.25)
    print(f"could not delete the scratch copy {path}; the next run will")
    return False


def _cmds(runner: str, test_rel: str, root: Path) -> list[list[str]]:
    """The command(s) that run *test_rel*.  All must pass for the suite to pass.

    ``node`` takes one script at a time, so several files become several
    commands; pytest takes them all at once.  A target needs more than one file
    whenever the behaviour under test is split across harnesses -- e.g. the
    lobby's markup is checked in jsdom against the module alone, while what a
    *click* on it does needs the whole app loaded in the real page.
    """
    if runner == "node":
        node = shutil.which("node") or "node"
        return [[node, str(root / t)] for t in test_rel.split()]
    # -p no:cacheprovider: a mutated run must not leave a .pytest_cache
    # describing a source tree that no longer exists by the time it is read.
    return [[sys.executable, "-m", "pytest"]
            + [str(root / t) for t in test_rel.split()]
            + ["-q", "-x", "-p", "no:cacheprovider"]]


def run(runner: str, test_rel: str, root: Path, timeout: float = 300.0, *,
        show_failure: bool = False) -> bool:
    """True only if the suite *passed*.  A mutant that makes it hang has not
    survived -- it just failed slowly, so a timeout is a False, not a stall.

    A mutant that makes the source unimportable is also caught: pytest exits
    non-zero on a collection error, which is what a syntax-breaking mutation
    produces.  That is a weaker kill than a failing assertion, so the mutations
    above are written to stay syntactically valid wherever they can be.

    Python runs with PYTHONDONTWRITEBYTECODE.  The copy has no __pycache__ and
    none is written, so each run compiles the source as it stands.  Python
    trusts a cached .pyc whose recorded source mtime (whole seconds) and size
    match, so a mutant the same size as the version before it, written within
    the same second, would otherwise run the old bytecode.

    *show_failure* prints the end of a failing run's output: a baseline that
    fails is something someone has to go and read.
    """
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    for cmd in _cmds(runner, test_rel, root):
        try:
            p = subprocess.run(cmd, cwd=str(root), capture_output=True,
                               timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            if show_failure:
                print(f"timed out after {timeout:.0f}s: {' '.join(cmd)}")
            return False
        if p.returncode != 0:
            if show_failure:
                tail = (p.stdout + p.stderr).decode("utf-8", "replace")
                _print_safely("\n".join(tail.splitlines()[-30:]))
            return False
    return True


def _print_safely(text: str) -> None:
    """Print test output to whatever stdout can encode.  A pipe on Windows is
    cp1252, and an em dash in a test's output must not end the sweep."""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(enc, "replace").decode(enc))


def sweep(label: str, src_rel: str, test_rel: str, mutations,
          runner: str = "node", *, root: Path) -> list[str]:
    """Try each mutation of *src_rel* in the copy at *root*.

    Each mutant is written whole from the original, so none carries over into
    the next, and the file is put back byte for byte at the end: later targets
    test the same files in the same copy.  Line endings are kept as they were,
    so a mutant differs from the original only where the mutation says.
    """
    src = root / src_rel
    raw = src.read_bytes()
    crlf = b"\r\n" in raw
    # Anchors are written with \n, so match against the text the same way.
    original = raw.decode("utf-8").replace("\r\n", "\n")

    print(f"\n=== {label}: {src_rel} x {test_rel} ===")
    if not run(runner, test_rel, root, show_failure=True):
        print("BASELINE FAILS -- fix that first")
        return ["<baseline failure>"]

    survivors = []
    try:
        for i, (name, old, new) in enumerate(mutations, 1):
            n = original.count(old)
            if n != 1:
                print(f"{i:2}. SKIP (anchor appears {n}x) {name}")
                survivors.append(f"{label}: {name}  [anchor not unique]")
                continue
            mutant = original.replace(old, new)
            if crlf:
                mutant = mutant.replace("\n", "\r\n")
            src.write_bytes(mutant.encode("utf-8"))
            passed = run(runner, test_rel, root)
            print(f"{i:2}. {'SURVIVED' if passed else 'caught  '} {name}")
            if passed:
                survivors.append(f"{label}: {name}")
    finally:
        src.write_bytes(raw)

    print(f"{len(mutations) - len(survivors)}/{len(mutations)} caught")
    return survivors


def _left_mutated(root: Path) -> list[Path]:
    """Sources the old in-place sweep may have left mutated.  It kept each
    original beside its source as <source>.mutbak while a mutant was in place,
    and deleted it once the original was back."""
    return [p for p in _project_files(root) if p.name.endswith(".mutbak")]


def main(argv: list[str]) -> int:
    wanted = argv[1:] or list(TARGETS)
    unknown = [w for w in wanted if w not in TARGETS]
    if unknown:
        print(f"unknown target(s): {', '.join(unknown)}")
        print(f"available: {', '.join(TARGETS)}")
        return 2
    left = _left_mutated(ROOT)
    if left:
        print("A sweep from before 2026-10-02, which mutated files in place, "
              "was killed mid-mutation and left:")
        for p in left:
            print(f"  {p}")
        print("The file beside each may still be a mutant.  Compare the two, "
              "keep the real one, delete the .mutbak, and run again.")
        return 2

    scratch = make_scratch(ROOT)
    print(f"Working in a copy: {scratch}")
    survivors = []
    total = 0
    try:
        for label in wanted:
            src_rel, test_rel, mutations, runner = TARGETS[label]
            total += len(mutations)
            survivors += sweep(label, src_rel, test_rel, mutations, runner,
                               root=scratch)
    finally:
        _rmtree(scratch)

    print(f"\n{total - len(survivors)}/{total} caught overall")
    for s in survivors:
        print("  survivor:", s)
    return 1 if survivors else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:   # the copy is already gone: main's finally
        sys.exit(130)
