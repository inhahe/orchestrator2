/* A socket that dies while the window is hidden must come back when it is shown.
 *
 * Companion to hidden_window.test.js.  That one covers the *rendering* half of
 * "the window isn't updated while it's not in focus"; this one covers the
 * transport half, which is the more damaging of the two: a hidden window runs
 * its reconnect backoff on a setTimeout that Chrome clamps to 1s and then to
 * 1/minute, so the 20-attempt budget can burn down unattended and latch
 * auto-reconnect off *permanently*.  A laptop suspend/resume drops the socket
 * while hidden essentially by definition, so this is the ordinary case.
 *
 * The harness drives the real app.js inside the real index.html, with two
 * substitutions: a fake WebSocket (so sockets can be failed on demand and
 * counted) and a manual clock (so the backoff can be advanced without waiting
 * out real seconds).  Both are installed before app.js is evaluated.
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const ROOT = path.join(__dirname, '..');
const STATIC = path.join(ROOT, 'static');
const SCRIPTS = ['util.js', 'diff.js', 'status.js', 'panels.js', 'commands.js',
                 'chat.js', 'lobby.js', 'move.js', 'app.js'];

let failures = 0;
let passes = 0;

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

function test(name, fn) {
  let h;
  try {
    h = build();
    fn(h);
    console.log('  ok    ' + name);
    passes++;
  } catch (e) {
    console.log('  FAIL  ' + name + '\n          ' + e.message);
    failures++;
  } finally {
    if (h) h.dom.window.close();
  }
}

/* ---- harness ---------------------------------------------------------- */

function build() {
  const html = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8');
  const dom = new JSDOM(html, { url: 'http://localhost:8240/', runScripts: 'outside-only' });
  const win = dom.window;

  // --- manual clock.  app.js's reconnect backoff is the thing under test, so
  // it must be steppable rather than real.
  let now = 0;
  let seq = 0;
  const timers = new Map();
  win.setTimeout = (fn, delay) => {
    const id = ++seq;
    timers.set(id, { fn, at: now + (delay || 0) });
    return id;
  };
  win.clearTimeout = (id) => { timers.delete(id); };

  // Intervals are recorded but never fired by `advance`: a live 1s ticker
  // under `advance(600000)` would run six hundred thousand iterations.  Tests
  // that care about a ticker fire it explicitly with `tickIntervals()`.
  const intervals = new Map();
  win.setInterval = (fn, ms) => {
    const id = ++seq;
    intervals.set(id, { fn, ms: ms || 0 });
    return id;
  };
  win.clearInterval = (id) => { intervals.delete(id); };

  // A controllable `Date.now`, because a countdown is a function of the clock
  // and "does it tick" cannot be asked of a clock that only moves in real
  // time.  Defaults to the real one, so nothing that ignores it changes.
  let fakeNow = Date.now();
  const RealDate = win.Date;
  win.Date = new Proxy(RealDate, {
    get(t, prop, r) {
      if (prop === 'now') return () => fakeNow;
      return Reflect.get(t, prop, r);
    },
  });

  function advance(ms) {
    now += ms;
    // Re-scan after each callback: a fired timer commonly schedules the next.
    for (;;) {
      const due = [...timers.entries()]
        .filter(([, t]) => t.at <= now)
        .sort((a, b) => a[1].at - b[1].at);
      if (!due.length) return;
      const [id, t] = due[0];
      timers.delete(id);
      t.fn();
    }
  }

  // --- fake WebSocket.
  const sockets = [];
  class FakeWebSocket {
    constructor(url) {
      this.url = url;
      this.readyState = FakeWebSocket.CONNECTING;
      this.sent = [];
      this.onopen = this.onclose = this.onerror = this.onmessage = null;
      sockets.push(this);
    }
    send(data) { this.sent.push(data); }
    close() {
      if (this.readyState === FakeWebSocket.CLOSED) return;
      this.readyState = FakeWebSocket.CLOSED;
      if (this.onclose) this.onclose({ code: 1000, reason: 'test close' });
    }
    // -- test controls
    accept() {
      this.readyState = FakeWebSocket.OPEN;
      if (this.onopen) this.onopen({});
    }
    drop(code = 1006) {
      this.readyState = FakeWebSocket.CLOSED;
      if (this.onclose) this.onclose({ code, reason: 'test drop' });
    }
    deliver(obj) {
      if (this.onmessage) this.onmessage({ data: JSON.stringify(obj) });
    }
  }
  FakeWebSocket.CONNECTING = 0;
  FakeWebSocket.OPEN = 1;
  FakeWebSocket.CLOSING = 2;
  FakeWebSocket.CLOSED = 3;
  win.WebSocket = FakeWebSocket;

  // Surface unexpected breakage instead of letting it pass silently.  console
  // .log is left alone: app.js narrates connect/close on it by design.
  const errors = [];
  win.console = Object.assign(Object.create(console), {
    error: (...a) => { errors.push(a.join(' ')); },
    warn: (...a) => { errors.push(a.join(' ')); },
  });

  // One eval, not one per file: each module is a top-level `const Name = ...`,
  // and `const` inside an eval is scoped to *that* eval.  Evaluating the files
  // separately would hide every module from every other one, which is not how
  // the browser loads them.  Concatenating reproduces the shared script scope.
  // Exposed under a deliberately test-only name.  This used to be
  // `window.App = App; window.Lobby = Lobby; ...`, which made the fixture lie:
  // production guarded itself with `if (window.Lobby)`, and a top-level
  // `const` is NOT a property of `window`, so in a real browser every one of
  // those guards was permanently false.  The harness assigned the very
  // properties the guards looked for, making six dead branches look alive —
  // including the one that renders the `/move` account list.  Exposing under
  // `__mods` lets the tests reach the modules without being able to satisfy
  // production's own lookups by accident.
  win.eval(SCRIPTS.map((s) => fs.readFileSync(path.join(STATIC, s), 'utf8'))
                  .join('\n;\n')
           + '\n;window.__mods = { App, Lobby, Chat, Move, Commands, Status };');

  // Keep the restart path from starting a real poll loop (it would fetch()).
  let reloadPolls = 0;
  win.__mods.Lobby.showReloadingAndPoll = () => { reloadPolls++; };

  let hidden = false;
  Object.defineProperty(win.document, 'hidden', {
    configurable: true, get: () => hidden,
  });

  // init() runs on DOMContentLoaded, which jsdom already fired while parsing.
  win.document.dispatchEvent(new win.Event('DOMContentLoaded'));

  return {
    dom, win, sockets, advance, errors,
    /* Move the fake clock and fire every registered interval once per step. */
    tickIntervals(times = 1, stepMs = 1000) {
      for (let i = 0; i < times; i++) {
        fakeNow += stepMs;
        for (const t of [...intervals.values()]) t.fn();
      }
    },
    get now() { return fakeNow; },
    get intervalCount() { return intervals.size; },
    get reloadPolls() { return reloadPolls; },
    get live() { return sockets[sockets.length - 1]; },
    setHidden(v) {
      hidden = v;
      win.document.dispatchEvent(new win.Event('visibilitychange'));
    },
    /* Burn the whole retry budget, as an unattended hidden window does. */
    exhaustRetries() {
      for (let i = 0; i < 40; i++) {
        if (this.live && this.live.readyState !== FakeWebSocket.CLOSED) this.live.drop();
        this.advance(60000);          // >= any backoff step
      }
    },
  };
}

/* ---- tests ------------------------------------------------------------ */

console.log('\nreconnect-on-show\n');

test('harness sanity: init opens exactly one socket', (h) => {
  assert(h.sockets.length === 1, `expected 1 socket, got ${h.sockets.length}`);
  assert(/\/ws/.test(h.live.url), 'socket did not target /ws: ' + h.live.url);
  assert(h.errors.length === 0, 'unexpected console noise: ' + h.errors.join(' | '));
});

test('harness sanity: a dropped socket schedules a retry on the clock', (h) => {
  h.live.accept();
  h.live.drop();
  const before = h.sockets.length;
  h.advance(1500);
  assert(h.sockets.length === before + 1,
         'backoff did not open a new socket after its delay');
});

test('showing the window reconnects a socket dropped while hidden', (h) => {
  h.live.accept();
  h.setHidden(true);
  h.live.drop();
  const afterDrop = h.sockets.length;
  // The backoff timer exists but has not come due -- exactly the state a user
  // returns to when the drop was recent.
  h.setHidden(false);
  assert(h.sockets.length === afterDrop + 1,
         'becoming visible did not reconnect (sockets: '
         + `${afterDrop} -> ${h.sockets.length})`);
});

test('showing the window recovers a reconnect that gave up while hidden', (h) => {
  h.live.accept();
  h.setHidden(true);
  h.exhaustRetries();
  const afterGiveUp = h.sockets.length;
  // Latched off: more time alone will never produce another socket.
  h.advance(600000);
  assert(h.sockets.length === afterGiveUp,
         'retry budget was not actually exhausted -- test proves nothing');
  h.setHidden(false);
  assert(h.sockets.length === afterGiveUp + 1,
         'becoming visible did not undo the exhausted-retry latch');
});

test('a server that said it is shutting down is not overruled by showing', (h) => {
  h.live.accept();
  h.live.deliver({ type: 'server_shutdown', reason: 'bye' });
  h.live.drop();
  const before = h.sockets.length;
  h.setHidden(true);
  h.setHidden(false);
  h.advance(600000);
  assert(h.sockets.length === before,
         'showing the window reconnected to a deliberately stopped server');
});

test('a restarting server is left to Lobby, not reconnected underneath it', (h) => {
  h.live.accept();
  h.live.deliver({ type: 'server_restart' });
  h.live.drop();
  const before = h.sockets.length;
  h.setHidden(true);
  h.setHidden(false);
  h.advance(600000);
  assert(h.reloadPolls === 1, 'Lobby reload-poll was not started');
  assert(h.sockets.length === before,
         'showing the window raced Lobby by opening its own socket');
});

test('showing the window with a healthy socket changes nothing', (h) => {
  h.live.accept();
  const before = h.sockets.length;
  const sentBefore = h.live.sent.length;
  h.setHidden(true);
  h.setHidden(false);
  assert(h.sockets.length === before,
         'a spurious socket was opened over a live one');
  assert(h.live.readyState === 1, 'the live socket was disturbed');
  // reconnect() on an OPEN socket means "/connect" -- a command the user never
  // typed.  Merely focusing the tab must not issue it.
  assert(h.live.sent.length === sentBefore,
         'focusing the tab sent an unrequested command: '
         + h.live.sent.slice(sentBefore).join(' | '));
});

test('showing the window mid-handshake does not orphan the pending socket', (h) => {
  h.live.drop();
  h.advance(1500);                 // backoff opens a socket; leave it CONNECTING
  const pending = h.live;
  assert(pending.readyState === 0, 'expected a CONNECTING socket to test against');
  const before = h.sockets.length;
  h.setHidden(true);
  h.setHidden(false);
  assert(h.sockets.length === before,
         'opened a second socket while one was still handshaking');
  // And the pending one still works.
  pending.accept();
  assert(pending.readyState === 1, 'the handshaking socket did not open');
});

test('hiding the window never opens a socket', (h) => {
  h.live.accept();
  h.live.drop();
  const before = h.sockets.length;
  h.setHidden(true);
  assert(h.sockets.length === before, 'hiding opened a socket');
});

test('a reconnect that succeeds clears the latch for next time', (h) => {
  h.live.accept();
  h.setHidden(true);
  h.exhaustRetries();
  h.setHidden(false);
  h.live.accept();                 // the recovery socket connects
  // A later drop must go back to ordinary backoff, not stay latched off.
  h.live.drop();
  const before = h.sockets.length;
  h.advance(1500);
  assert(h.sockets.length === before + 1,
         'auto-reconnect stayed latched off after a successful recovery');
});

test('recovering from an exhausted budget does not license overruling a later '
     + 'server shutdown', (h) => {
  // The exhausted flag is sticky state.  If a recovery forgets to clear it, the
  // *next* deliberate server shutdown looks retryable and the tab reconnects to
  // a server that deliberately went away.
  h.live.accept();
  h.setHidden(true);
  h.exhaustRetries();
  h.setHidden(false);            // recovers; clears the flag
  h.live.accept();
  h.live.deliver({ type: 'server_shutdown', reason: 'bye' });
  h.live.drop();
  const before = h.sockets.length;
  h.setHidden(true);
  h.setHidden(false);
  h.advance(600000);
  assert(h.sockets.length === before,
         'a stale exhausted-retry flag let showing overrule a real shutdown');
});

/* ---- module lookups actually resolve ----------------------------------- *
 *
 * Reported 2026-09-07: "/move is stuck on 'loading accounts'".  The account
 * list arrived and was thrown away, because app.js dispatched it behind
 * `if (window.Move)` and every module here is declared `const Move = …`
 * at the top level of a classic script.  A top-level `const` creates a binding
 * in the global *declarative* record; only `var` and function declarations
 * become properties of `window`.  So `window.Move` was `undefined` — always
 * — and the guard read as caution while meaning "never".
 *
 * Six such guards existed.  Each is a branch that cannot run, wearing the
 * clothes of a branch that runs defensively, and none of them could fail
 * loudly: the fallback is silence.  These tests assert the wiring by making
 * the module observably act, so a guard that stops matching is a failure
 * rather than a quiet nothing.
 */

test('no module is looked up as a property of window', (h) => {
  // The root cause, stated once.  A future `if (window.Chat)` is the same bug.
  const names = Object.keys(h.win.__mods);
  names.forEach((n) => {
    assert(h.win[n] === undefined,
           `window.${n} exists, so a window.${n} guard would work here but `
           + 'not in a browser — the fixture is lying');
  });
});

test('a move_accounts message reaches the Move overlay', (h) => {
  h.live.accept();
  const seen = [];
  h.win.__mods.Move.renderAccounts = (m) => seen.push(m);
  h.live.deliver({ type: 'move_accounts', accounts: [], has_session: true });
  assert(seen.length === 1,
         'the account list was dropped — /move sits on "Loading accounts"');
});

test('a move_error message reaches the Move overlay', (h) => {
  h.live.accept();
  const seen = [];
  h.win.__mods.Move.error = (m) => seen.push(m);
  h.live.deliver({ type: 'move_error', message: 'nope' });
  assert(seen.length === 1, 'a failed switch reported nothing to the user');
});

test('a completed move closes the overlay', (h) => {
  h.live.accept();
  let closed = 0;
  h.win.__mods.Move.close = () => { closed++; };
  h.live.deliver({ type: 'attached', session: { rid: 's1', cwd: 'D:\\x' } });
  assert(closed === 1, 'the overlay stayed up over the switched-in session');
});

test('a reconnect tells the lobby to resubscribe', (h) => {
  // Lobby.onReconnect re-sends `lobby_watch` on the fresh socket; without it a
  // reconnected lobby silently stops receiving live updates.
  let calls = 0;
  h.win.__mods.Lobby.onReconnect = () => { calls++; };
  h.live.accept();
  assert(calls === 1, 'the lobby was never told the socket is new');
});

test('/help opens a real modal instead of falling back to inline text', (h) => {
  // chat.js's `_openModal` had the same dead guard (`if (window.App && …)`),
  // so every modal — /help, /status, /debug — silently took the "App is not
  // available" fallback and rendered as a system message.  A fallback that
  // always fires is not a fallback, and this one degraded quietly enough that
  // it read as a design choice.
  h.live.accept();
  const opened = [];
  h.win.__mods.App.openModal = (t, c) => opened.push([t, c]);
  h.win.__mods.Chat.handleMessage({
    type: 'modal', title: 'Help', content: 'Commands:' });
  assert(opened.length === 1, 'fell back to inline text with App right there');
  assert(opened[0][0] === 'Help', opened[0][0]);
  // …and *only* the modal.  The inline render is the fallback for when there
  // is no modal to open; doing both would print the whole of /help twice.
  const msgs = h.win.document.getElementById('messages');
  assert(!(msgs && msgs.textContent.includes('Commands:')),
         'the content was rendered inline as well as in the modal');
});

/* ---- /move (formerly /switch) ------------------------------------------ */

function typeCommand(h, text) {
  const input = h.win.document.getElementById('input-box');
  input.value = text;
  input.dispatchEvent(new h.win.KeyboardEvent(
    'keydown', { key: 'Enter', bubbles: true, cancelable: true }));
}

test('/move opens the move overlay', (h) => {
  h.live.accept();
  const opens = [];
  h.win.__mods.Move.open = (p) => opens.push(p);
  typeCommand(h, '/move');
  assert(opens.length === 1, 'the command did nothing');
  assert(opens[0] === '', `prefill was ${JSON.stringify(opens[0])}`);
});

test('/move <path> prefills the destination directory', (h) => {
  h.live.accept();
  const opens = [];
  h.win.__mods.Move.open = (p) => opens.push(p);
  typeCommand(h, '/move D:\\visual studio projects\\os');
  assert(opens[0] === 'D:\\visual studio projects\\os', opens[0]);
});

test('a command that merely starts with /move is not swallowed', (h) => {
  // `/moved` is not `/move`. Prefix matching without the space would send it
  // to the overlay instead of the server, where it belongs (as an error).
  h.live.accept();
  const opens = [];
  h.win.__mods.Move.open = (p) => opens.push(p);
  typeCommand(h, '/moved something');
  assert(opens.length === 0, 'swallowed a different command');
});

test('a /move command is not also sent to the server', (h) => {
  // It is a client-driven overlay; forwarding it too would produce an
  // "unknown command" error alongside the overlay.
  h.live.accept();
  h.win.__mods.Move.open = () => {};
  const before = h.live.sent.length;
  typeCommand(h, '/move');
  const sentTexts = h.live.sent.slice(before).map((s) => {
    try { return JSON.parse(s).text || ''; } catch (e) { return ''; }
  });
  assert(!sentTexts.some((t) => t.startsWith('/move')),
         'the command was forwarded as a prompt as well');
});

/* ---- the scheduled-wakeup indicator ------------------------------------ *
 *
 * The loop injects prompts into the session on a timer, from outside the
 * conversation.  `/loop` made that askable; the status bar makes it visible,
 * which is the difference between noticing a scheduled wakeup and having to
 * suspect one.
 */

function statusOf(h, extra) {
  h.live.accept();
  h.live.deliver({ type: 'status_update',
                   status: Object.assign({ busy_class: 'idle',
                                           busy_label: 'idle' }, extra || {}) });
  // Deadlines in these tests are expressed against the harness clock, so
  // `h.now` -- not the wall clock -- is what a countdown is measured from.
  return {
    field: h.win.document.getElementById('status-loop'),
    sep: h.win.document.getElementById('status-loop-sep'),
    label: h.win.document.getElementById('status-loop-label'),
  };
}

test('an armed wakeup shows a countdown in the status bar', (h) => {
  const els = statusOf(h, { wakeup_at: h.now / 1000 + 300 });
  assert(!els.field.hidden, 'the field stayed hidden while a wakeup was armed');
  assert(/\d/.test(els.field.textContent), els.field.textContent);
  assert(/m/.test(els.field.textContent),
         `expected a minutes countdown, got ${els.field.textContent}`);
});

test('the label and separator appear with it', (h) => {
  // A bare number in a bar of labelled fields is unreadable.
  const els = statusOf(h, { wakeup_at: h.now / 1000 + 300 });
  assert(!els.label.hidden && !els.sep.hidden, 'field shown without its label');
});

test('no wakeup means no field at all', (h) => {
  // Most sessions never use the loop; a permanent "loop --" would be noise in
  // a bar that is already full.
  const els = statusOf(h, { wakeup_at: null });
  assert(els.field.hidden && els.label.hidden && els.sep.hidden,
         'showed a loop field for a session with no loop');
});

test('a status update without the field at all shows nothing', (h) => {
  const els = statusOf(h, {});
  assert(els.field.hidden, 'invented a countdown from a missing field');
});

test('the countdown disappears when the wakeup is cancelled', (h) => {
  // A countdown that outlives its wakeup promises a prompt that is not coming.
  const els = statusOf(h, { wakeup_at: h.now / 1000 + 300 });
  assert(!els.field.hidden, 'setup failed');
  statusOf(h, { wakeup_at: null });
  assert(els.field.hidden, 'the countdown outlived the wakeup');
});

test('a deferred wakeup says so', (h) => {
  // "armed" alone would imply it will fire; a deferral count means it keeps
  // landing mid-turn and is being pushed back, and at the cap it is dropped.
  const els = statusOf(h, { wakeup_at: h.now / 1000 + 60,
                            wakeup_defers: 3 });
  assert(/deferred 3/.test(els.field.textContent), els.field.textContent);
});

test('an undeferred wakeup does not mention deferrals', (h) => {
  const els = statusOf(h, { wakeup_at: h.now / 1000 + 60,
                            wakeup_defers: 0 });
  assert(!/deferred/.test(els.field.textContent), els.field.textContent);
});

test('a wakeup already due reads as "now" rather than a negative number', (h) => {
  const els = statusOf(h, { wakeup_at: h.now / 1000 - 5 });
  assert(els.field.textContent === 'now', els.field.textContent);
});

test('the countdown ticks down without a new status update', (h) => {
  // The whole reason for a local ticker: status pushes are ~2s and can stall
  // while the SDK blocks the event loop, and a countdown that freezes is worse
  // than none.  Nothing here delivers a second status_update.
  const els = statusOf(h, { wakeup_at: h.now / 1000 + 300 });
  const before = els.field.textContent;
  h.tickIntervals(60);                       // 60 seconds of clock, no pushes
  const after = els.field.textContent;
  assert(after !== before,
         `the countdown froze at ${before} over a minute of clock`);
  assert(/4m/.test(after), `expected ~4m left, got ${after}`);
});

test('the ticker stops when the wakeup is cancelled', (h) => {
  // An interval left running per armed-then-cancelled wakeup would accumulate
  // one timer per cycle for the life of the tab.
  statusOf(h, { wakeup_at: h.now / 1000 + 300 });
  const armed = h.intervalCount;
  statusOf(h, { wakeup_at: null });
  assert(h.intervalCount < armed,
         'the countdown interval outlived the wakeup it was counting');
});

test('re-arming does not stack a second ticker', (h) => {
  statusOf(h, { wakeup_at: h.now / 1000 + 300 });
  const one = h.intervalCount;
  statusOf(h, { wakeup_at: h.now / 1000 + 600 });
  assert(h.intervalCount === one,
         `intervals grew from ${one} to ${h.intervalCount} on a re-arm`);
});

test('the tooltip says what it is and how to stop it', (h) => {
  const els = statusOf(h, { wakeup_at: h.now / 1000 + 300 });
  const t = els.field.title || '';
  assert(/inject/i.test(t), t);
  assert(/\/loop off/.test(t), 'did not say how to stop it');
});

/* ---- the panels toggle (mobile) --------------------------------------- *
 *
 * Reported 2026-09-14: "on mobile, i can't see any of the side panes including
 * queued prompts."  They were one tap away the whole time, behind an
 * unlabelled 30x25 cog sitting next to a labelled "☰ Sessions" -- which is
 * how it was read: as decoration.  Nothing on screen gave a reason to tap it
 * either: the status bar announces "bg wait (1)" out loud but has never
 * mentioned queued prompts.
 */

function toggleEls(h) {
  return {
    btn: h.win.document.getElementById('sidebar-toggle'),
    count: h.win.document.getElementById('sidebar-toggle-count'),
  };
}

test('the panels toggle says what it is', (h) => {
  const { btn } = toggleEls(h);
  assert(/Panels/.test(btn.textContent), btn.textContent.trim());
});

test('a queue puts a count on the panels toggle', (h) => {
  // The one thing the status bar never surfaced, and the thing the report
  // named.
  statusOf(h, { queued_count: 3 });
  const { count } = toggleEls(h);
  assert(!count.hidden, 'the queue was invisible from outside the panels');
  assert(count.textContent === '3', count.textContent);
});

test('an empty queue shows no badge', (h) => {
  // A permanent "0" is not a reason to look, and a badge that is always there
  // stops being a signal.
  statusOf(h, { queued_count: 0 });
  assert(toggleEls(h).count.hidden, 'showed a zero badge');
});

test('the badge clears when the queue drains', (h) => {
  statusOf(h, { queued_count: 2 });
  assert(!toggleEls(h).count.hidden, 'setup failed');
  statusOf(h, { queued_count: 0 });
  assert(toggleEls(h).count.hidden, 'the badge outlived the queue');
});

test('the badge says what the number means', (h) => {
  // A bare number next to a cog could be anything.
  statusOf(h, { queued_count: 1 });
  assert(/1 queued prompt/.test(toggleEls(h).count.title),
         toggleEls(h).count.title);
});

test('tapping the toggle reveals the sidebar', (h) => {
  const sidebar = h.win.document.getElementById('sidebar');
  assert(!sidebar.classList.contains('mobile-visible'), 'open to begin with');
  toggleEls(h).btn.click();
  assert(sidebar.classList.contains('mobile-visible'),
         'the panels stayed hidden');
});

test('tapping it again hides the sidebar', (h) => {
  const sidebar = h.win.document.getElementById('sidebar');
  const { btn } = toggleEls(h);
  btn.click();
  btn.click();
  assert(!sidebar.classList.contains('mobile-visible'), 'could not close it');
});

test('the backdrop closes the sidebar', (h) => {
  // No hover on touch and the sidebar covers most of the screen; tapping
  // beside it is the gesture people try first.
  const sidebar = h.win.document.getElementById('sidebar');
  toggleEls(h).btn.click();
  h.win.document.getElementById('sidebar-backdrop').click();
  assert(!sidebar.classList.contains('mobile-visible'),
         'tapping outside left the sidebar up');
});

test('a server_restart drives the lobby reload poll', (h) => {
  h.live.accept();
  let polls = 0;
  h.win.__mods.Lobby.showReloadingAndPoll = () => { polls++; };
  h.live.deliver({ type: 'server_restart' });
  assert(polls === 1, 'the restart UI never ran');
});

/* ---- opening a session in its own tab --------------------------------- *
 *
 * The lobby's session titles and the ☰ Sessions control are anchors so the
 * browser can offer "open in new tab" — a long-press on a phone, ctrl or
 * middle-click on a desktop.  That capability is the whole point, so the
 * delegated handlers must swallow a *plain* click (which takes the in-place
 * path) and keep their hands off a modified one.
 */

function clickOn(el, opts) {
  const ev = new el.ownerDocument.defaultView.MouseEvent(
    'click', Object.assign({ bubbles: true, cancelable: true }, opts || {}));
  el.dispatchEvent(ev);
  return ev;
}

test('the Sessions control is a link', (h) => {
  const el = h.win.document.getElementById('sessions-btn');
  assert(el.tagName === 'A', `expected an anchor, got <${el.tagName}>`);
  assert(/lobby=1/.test(el.getAttribute('href') || ''), el.getAttribute('href'));
});

test('a plain click on Sessions is handled in-page, not followed', (h) => {
  h.live.accept();
  h.win.__mods.Lobby.open = () => {};
  const ev = clickOn(h.win.document.getElementById('sessions-btn'));
  assert(ev.defaultPrevented, 'the plain click was allowed to navigate as well');
});

test('a ctrl-click on Sessions is left to the browser', (h) => {
  // Swallowing it would take back the capability the anchor exists for.
  h.live.accept();
  const ev = clickOn(h.win.document.getElementById('sessions-btn'),
                     { ctrlKey: true });
  assert(!ev.defaultPrevented,
         'preventDefault() on a ctrl-click: no new tab after all');
});

test('a middle-click on Sessions is left to the browser', (h) => {
  h.live.accept();
  const ev = clickOn(h.win.document.getElementById('sessions-btn'),
                     { button: 1 });
  assert(!ev.defaultPrevented, 'swallowed a middle-click');
});

test('a meta-click (macOS) is left to the browser too', (h) => {
  h.live.accept();
  const ev = clickOn(h.win.document.getElementById('sessions-btn'),
                     { metaKey: true });
  assert(!ev.defaultPrevented, 'swallowed a cmd-click');
});

console.log(`\n${passes}/${passes + failures} passed`);
process.exit(failures ? 1 : 0);
