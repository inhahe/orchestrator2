/* A running card must say what it is, and say only what it knows.
 *
 * Reported 2026-09-06, with a screenshot: "Good Photons is showing with its
 * title mostly blocked by other things on the same line. and why is 'current'
 * there anyway, when none of the other ones say 'current'?"
 *
 * Two independent bugs in the same card, both on the *foreign* path (a session
 * running in another hub process):
 *
 * 1. **"current" on the one card that cannot be current.**  A foreign entry
 *    carries `rid: null` -- there is nothing here to attach to -- and
 *    `_currentRid` is `null` in a tab not attached to a session, which is
 *    exactly what a standalone `?lobby=1` tab is.  `null === null` is true, so
 *    every foreign card in that tab labelled itself "current".  The claim is
 *    not merely unfounded, it is inverted: a session running in another hub is
 *    the one session this tab is definitely *not* attached to, which the same
 *    card said in the next breath with "other window :63978".
 *
 * 2. **The title lost the space fight to its own annotations.**  The header
 *    was a flex row of dot / title / badges, with the title at `flex: 1`
 *    (a *zero* basis) and the badges at `flex: 0 0 auto`.  So the badges took
 *    their full natural width first and the title got the remainder -- in a
 *    three-across grid, next to "other window :63978", almost none.  "Good
 *    Photons" rendered as "Goo...".
 *
 * A card's title is its identity; badges are annotations on it.  Annotations
 * do not get to evict the thing they annotate.
 */

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const LOBBY_JS = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'lobby.js'), 'utf8');

let failures = 0;
let ran = 0;

function test(name, fn) {
  ran++;
  try {
    fn();
    console.log(`  ok    ${name}`);
  } catch (e) {
    failures++;
    console.log(`  FAIL  ${name}\n        ${e && e.message}`);
  }
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg || 'assertion failed');
}

const IDS = [
  'lobby', 'lobby-running', 'lobby-running-empty', 'lobby-running-count',
  'lobby-recent', 'lobby-recent-empty', 'lobby-recent-count',
  'lobby-recent-loading', 'lobby-new-cwd', 'lobby-new-btn', 'lobby-close',
  'sessions-btn', 'lobby-shutdown', 'lobby-restart', 'lobby-notice',
  'lobby-notice-text', 'lobby-notice-close',
];

function makePage() {
  const body = IDS.map((id) => `<div id="${id}"></div>`).join('');
  // A real `url`, not the default `about:blank`.  The lobby builds relative
  // hrefs and assigns them to `location`, which jsdom cannot resolve against
  // about:blank — it throws, and a throw escaping mid-handler hides whatever
  // the handler would have done next.  With a URL, jsdom merely declines to
  // navigate, which is the behaviour a test wants to observe around.
  const dom = new JSDOM(`<!doctype html><html><body>${body}</body></html>`,
                        { url: 'http://localhost:8240/',
                          runScripts: 'outside-only', pretendToBeVisual: false });
  const win = dom.window;
  win.App = { send: () => {}, isConnected: () => true };
  win.Util = { formatDuration: (s) => `${s}s` };
  // jsdom's window.open returns null, which sends the desktop path into its
  // `window.location.href = url` fallback and a "Not implemented: navigation"
  // warning.  A stub keeps the click tests about the click.
  win.open = () => ({ focus() {}, location: { href: 'about:blank' } });
  win.eval(LOBBY_JS + '\n;globalThis.__Lobby = Lobby;');
  const Lobby = win.__Lobby;
  Lobby.init();

  return {
    win, Lobby,
    render(running, opts) {
      Lobby.render({ running: running, recent: [], recent_pending: false });
      return this;
    },
    attach(rid) {
      // `onAttached` takes the session meta itself, as app.js passes it.
      Lobby.onAttached({ rid: rid, cwd: 'D:\\x', title: 't',
                         session_id: 'aaaa1111' });
      return this;
    },
    cards() {
      return Array.from(win.document.querySelectorAll('#lobby-running .lobby-card'));
    },
    card(i) { return this.cards()[i]; },
  };
}

const OURS = {
  rid: 's3', session_id: 'aaaa1111', title: 'OSc',
  cwd: 'D:\\visual studio projects\\os', account: 'C:\\Users\\me\\.claude-account-c',
  busy: false, viewers: 1, last_activity: Math.floor(Date.now() / 1000) - 60,
};

// The default is the *unknown* case: the owning hub could not be reached, so
// `busy` and `viewers` are null rather than fabricated.  Tests that want a
// known state override them together with `live_known`.
const FOREIGN = {
  rid: null, session_id: 'bbbb2222', title: 'Good Photons',
  cwd: 'D:\\visual studio projects\\forward raytracer',
  account: 'C:\\Users\\me\\.claude-account-b',
  busy: null, viewers: null, live_known: false,
  last_activity: Math.floor(Date.now() / 1000) - 1680,
  foreign: true, port: 63978, holder_pid: 4242, started: '2026-09-06 10:00',
};

function textOf(el) { return (el.textContent || '').replace(/\s+/g, ' ').trim(); }

// --- 1. "current" -----------------------------------------------------------

test('a foreign card is never labelled current', () => {
  // The reported case exactly: a lobby tab with no session of its own, so
  // `_currentRid` is null, and a foreign card whose rid is also null.
  const p = makePage().render([FOREIGN]);
  assert(!p.card(0).querySelector('.lobby-current-tag'),
         'null rid matched null _currentRid and claimed to be current');
  assert(!p.card(0).classList.contains('current'),
         'and highlighted the card as current too');
});

test('an ordinary card is not current until this tab attaches to it', () => {
  const p = makePage().render([OURS]);
  assert(!p.card(0).querySelector('.lobby-current-tag'),
         'called a session current before this tab attached to anything');
});

test('the card this tab is attached to IS labelled current', () => {
  // The feature still has to work -- the fix is a guard, not a removal.
  const p = makePage().attach('s3').render([OURS]);
  assert(p.card(0).querySelector('.lobby-current-tag'), 'lost the current tag');
  assert(p.card(0).classList.contains('current'), 'lost the current highlight');
});

test('only the attached card is current, not its neighbours', () => {
  const other = Object.assign({}, OURS, { rid: 's1', title: 'OSa' });
  const p = makePage().attach('s3').render([OURS, other]);
  assert(p.card(0).querySelector('.lobby-current-tag'), 'wrong card');
  assert(!p.card(1).querySelector('.lobby-current-tag'),
         'labelled a second card current');
});

test('a foreign card stays uncurrent even while this tab has a session', () => {
  const p = makePage().attach('s3').render([FOREIGN]);
  assert(!p.card(0).querySelector('.lobby-current-tag'), 'foreign went current');
});

test('a foreign card is not current even if its rid collides with ours', () => {
  // rids are per-hub and sequential (`s1`, `s2`, ...), so two hubs on one
  // machine routinely mint the same ones.  Foreign entries carry `rid: null`
  // today, which is why the `!m.foreign` clause looks redundant -- but the
  // day a foreign card gains a rid (it already carries a port and a holder
  // pid), `s3 === s3` would resurrect exactly this bug against a session in a
  // different process entirely.  The clause is the guard for that, so it gets
  // a test rather than being trimmed as unverifiable.
  const collide = Object.assign({}, FOREIGN, { rid: 's3' });
  const p = makePage().attach('s3').render([collide]);
  assert(!p.card(0).querySelector('.lobby-current-tag'),
         "another hub's s3 was mistaken for ours");
  assert(!p.card(0).classList.contains('current'), 'and highlighted as well');
});

// --- 2. The title -----------------------------------------------------------

test('the full title is rendered, not shortened by the markup', () => {
  // Elision is the CSS layer's job (`text-overflow`), and it can restore the
  // text at any width.  Anything dropped here is gone for good.
  const p = makePage().render([FOREIGN]);
  const t = p.card(0).querySelector('.lobby-card-title');
  assert(textOf(t) === 'Good Photons', `title was ${JSON.stringify(textOf(t))}`);
});

test('the title carries a tooltip, so an elided one is still readable', () => {
  const p = makePage().render([FOREIGN]);
  const t = p.card(0).querySelector('.lobby-card-title');
  assert(t.getAttribute('title') === 'Good Photons', t.getAttribute('title'));
});

test('nothing shares the title row with the title but the dot and the badge', () => {
  // The whole bug: a long annotation sitting in the header squeezed the
  // title.  The header may hold the status dot, the title, the "current"
  // badge and the close button -- and nothing else.
  const p = makePage().render([FOREIGN]);
  const top = p.card(0).querySelector('.lobby-card-top');
  const classes = Array.from(top.children).map((c) => c.className.split(' ')[0]);
  const allowed = ['lobby-dot', 'lobby-card-title', 'lobby-current-tag',
                   'lobby-card-close'];
  classes.forEach((c) => assert(allowed.includes(c),
                                `'${c}' is competing with the title for space`));
});

test('the foreign marker moved out of the header, keeping its port', () => {
  const p = makePage().render([FOREIGN]);
  const tag = p.card(0).querySelector('.lobby-foreign-tag');
  assert(tag, 'the foreign marker vanished instead of moving');
  assert(!p.card(0).querySelector('.lobby-card-top .lobby-foreign-tag'),
         'still competing with the title for the header row');
  assert(textOf(tag).includes('63978'), textOf(tag));
});

test('the foreign marker gets its own row, not a slot in the meta run', () => {
  // A bordered pill inside `a · b · c` pushed the run past the card width and
  // wrapped it, leaving a separator stranded at the end of the first line.
  const p = makePage().render([FOREIGN]);
  const row = p.card(0).querySelector('.lobby-card-tags');
  assert(row && row.querySelector('.lobby-foreign-tag'), 'no tag row');
  assert(!p.card(0).querySelector('.lobby-card-meta .lobby-foreign-tag'),
         'the pill is back inside the separator run');
});

test('an ordinary card grows no empty tag row', () => {
  const p = makePage().render([OURS]);
  assert(!p.card(0).querySelector('.lobby-card-tags'),
         'emitted an empty tag row, which still costs vertical margin');
});

test('the foreign marker keeps its explanatory tooltip', () => {
  const p = makePage().render([FOREIGN]);
  const tag = p.card(0).querySelector('.lobby-foreign-tag');
  const t = tag.getAttribute('title') || '';
  assert(t.includes('63978'), 'lost the port');
  assert(/can.t drive it/.test(t), 'lost the explanation of what to do');
});

// --- 3. Saying only what it knows ------------------------------------------

test('a foreign card does not claim a viewer count it cannot know', () => {
  // `viewers: null` means the owning hub could not be asked.  Printing
  // "0 viewers" would state as fact something we have no way to establish.
  const p = makePage().render([FOREIGN]);
  assert(!textOf(p.card(0)).includes('viewer'),
         'reported a viewer count for a session owned by another process');
});

// --- 3b. The dot has three states, because there are three answers ---------

test('an unreachable session gets the unknown dot, not the idle one', () => {
  // The bug: `busy: false` was a placeholder for "we could not ask", and the
  // idle dot rendered it as the assertion "not working".  A session hammering
  // away in another window looked idle.
  const p = makePage().render([FOREIGN]);
  const dot = p.card(0).querySelector('.lobby-dot');
  assert(dot.classList.contains('unknown'),
         `dot was ${dot.className} for an unknown state`);
  assert(!dot.classList.contains('idle'), 'claimed the session is idle');
});

test('the unknown dot says why it does not know, and which kind of not-knowing', () => {
  const p = makePage().render([FOREIGN]);
  const t = p.card(0).querySelector('.lobby-dot').getAttribute('title') || '';
  assert(/63978/.test(t), `no port in the explanation: ${t}`);
  assert(!/^idle$/.test(t), 'still just says "idle"');
});

test('a holder with no port is described as unqueryable, not unresponsive', () => {
  // A bare `claude --resume` in a terminal has no API.  That is permanent and
  // expected; a hub that did not answer is a thing to go and look at.
  const noPort = Object.assign({}, FOREIGN, { port: null });
  const p = makePage().render([noPort]);
  const t = p.card(0).querySelector('.lobby-dot').getAttribute('title') || '';
  assert(/not an orchestrator2 hub|cannot be queried/.test(t), t);
});

test('a foreign session the owning hub says is working shows the busy dot', () => {
  const busy = Object.assign({}, FOREIGN,
                             { busy: true, viewers: 2, live_known: true });
  const p = makePage().render([busy]);
  const dot = p.card(0).querySelector('.lobby-dot');
  assert(dot.classList.contains('busy'), `dot was ${dot.className}`);
  assert(dot.getAttribute('title') === 'working', dot.getAttribute('title'));
});

test('a foreign session the owning hub says is idle shows the idle dot', () => {
  const idle = Object.assign({}, FOREIGN,
                             { busy: false, viewers: 0, live_known: true });
  const p = makePage().render([idle]);
  const dot = p.card(0).querySelector('.lobby-dot');
  assert(dot.classList.contains('idle'), `dot was ${dot.className}`);
  assert(!dot.classList.contains('unknown'), 'hedged on a known answer');
});

test('a known viewer count is shown even on a foreign card', () => {
  // The rule is "say what you know", not "say nothing about other windows".
  const known = Object.assign({}, FOREIGN,
                              { busy: false, viewers: 2, live_known: true });
  const p = makePage().render([known]);
  assert(textOf(p.card(0)).includes('2 viewers'), textOf(p.card(0)));
});

test('a known zero viewer count is shown, not mistaken for unknown', () => {
  // `0` is falsy, so a `viewers || 0`-style check cannot tell "nobody is
  // watching" from "we have no idea" -- and the two mean opposite things.
  const known = Object.assign({}, FOREIGN,
                              { busy: false, viewers: 0, live_known: true });
  const p = makePage().render([known]);
  assert(textOf(p.card(0)).includes('0 viewers'), textOf(p.card(0)));
});

test('our own cards still use the two-state dot', () => {
  const p = makePage().render([OURS, Object.assign({}, OURS, { busy: true })]);
  assert(p.card(0).querySelector('.lobby-dot').classList.contains('idle'));
  assert(p.card(1).querySelector('.lobby-dot').classList.contains('busy'));
});

test('an ordinary card still reports its viewers', () => {
  const p = makePage().render([OURS]);
  assert(textOf(p.card(0)).includes('1 viewer'), textOf(p.card(0)));
});

test('the meta parts are separated', () => {
  // Stated on its own, because "no dangling separator" is also satisfied by
  // having no separators at all -- which runs "1 viewer" into "active 1m ago".
  const p = makePage().render([OURS]);
  const seps = p.card(0).querySelectorAll('.lobby-card-meta .lobby-card-sep');
  const parts = p.card(0).querySelectorAll('.lobby-card-meta > span:not(.lobby-card-sep)');
  assert(parts.length >= 2, `only ${parts.length} meta parts`);
  assert(seps.length === parts.length - 1,
         `${parts.length} parts but ${seps.length} separators`);
});

test('an absent rid takes its separator with it', () => {
  // The meta row used to emit the rid unconditionally, so a foreign card
  // showed two separators in a row around an empty span.
  const p = makePage().render([FOREIGN]);
  assert(!/\u00b7\s*\u00b7/.test(textOf(p.card(0))),
         `dangling separator: ${textOf(p.card(0))}`);
  const seps = p.card(0).querySelectorAll('.lobby-card-meta .lobby-card-sep');
  const parts = p.card(0).querySelectorAll('.lobby-card-meta > span:not(.lobby-card-sep)');
  assert(seps.length === parts.length - 1,
         `${parts.length} parts but ${seps.length} separators`);
});

test('an ordinary card still shows its rid', () => {
  const p = makePage().render([OURS]);
  assert(textOf(p.card(0)).includes('s3'), textOf(p.card(0)));
});

test('both card kinds still show the account', () => {
  const p = makePage().render([OURS, FOREIGN]);
  assert(textOf(p.card(0)).includes('.claude-account-c'), textOf(p.card(0)));
  assert(textOf(p.card(1)).includes('.claude-account-b'), textOf(p.card(1)));
});

test('a foreign card keeps its dashed styling and withholds the close button', () => {
  const p = makePage().render([FOREIGN]);
  assert(p.card(0).classList.contains('foreign'), 'lost the foreign class');
  assert(!p.card(0).querySelector('.lobby-card-close'),
         'offered a close button for a session it cannot close');
});

// --- 4. "This hub is running older code" -----------------------------------
//
// Python is read once at import, so a server goes on running whatever was on
// disk when it started, for days, silently.  That cost a real misdiagnosis: a
// bug reported against a hub whose process predated its own fix by a day.

function _staleEl(p) { return p.win.document.getElementById('lobby-stale'); }

function renderWith(p, stale) {
  p.Lobby.render({ running: [], recent: [], recent_pending: false,
                   stale: stale });
  return p;
}

test('a hub whose sources have changed says so', () => {
  const p = renderWith(makePage(), {
    count: 3, files: ['sdk_bridge.py', 'server.py'],
    newest: 1, started_at: Math.floor(Date.now() / 1000) - 86400 });
  const el = _staleEl(p);
  assert(el, 'no notice rendered');
  assert(/older code/i.test(el.textContent), el.textContent);
  assert(el.textContent.includes('3'), 'did not say how many');
});

test('the notice names the files, so you can judge whether it matters', () => {
  const p = renderWith(makePage(), {
    count: 2, files: ['sdk_bridge.py', 'server.py'], newest: 1, started_at: 1 });
  assert(_staleEl(p).textContent.includes('sdk_bridge.py'));
});

test('the notice says how long the hub has been running', () => {
  // "14 files changed" means little without "and this has been up since
  // Tuesday" -- the age is what makes it actionable.
  const p = renderWith(makePage(), {
    count: 1, files: ['x.py'], newest: 1,
    started_at: Math.floor(Date.now() / 1000) - 3 * 86400 });
  assert(/\d/.test(_staleEl(p).textContent), 'no age at all');
  assert(/3d|72h|day/.test(_staleEl(p).textContent),
         _staleEl(p).textContent);
});

test('an up-to-date hub shows nothing', () => {
  const p = renderWith(makePage(), { count: 0, files: [], started_at: 1 });
  assert(!_staleEl(p), 'nagged about a hub that is current');
});

test('a payload with no staleness info shows nothing', () => {
  // Older servers, and any path that forgets the field, must not produce a
  // banner reading "undefined source files changed".
  const p = makePage();
  p.Lobby.render({ running: [], recent: [], recent_pending: false });
  assert(!_staleEl(p), 'invented a warning from a missing field');
});

test('the notice disappears once the hub is restarted', () => {
  // The lobby is live-refreshing, so the very next tick after a restart must
  // clear it -- a stuck warning is worse than none.
  const p = renderWith(makePage(), { count: 2, files: ['a.py'], started_at: 1 });
  assert(_staleEl(p), 'setup failed');
  renderWith(p, { count: 0, files: [], started_at: 1 });
  assert(!_staleEl(p), 'the warning outlived the condition');
});

test('the notice is not duplicated across refreshes', () => {
  const p = renderWith(makePage(), { count: 1, files: ['a.py'], started_at: 1 });
  renderWith(p, { count: 1, files: ['a.py'], started_at: 1 });
  assert(p.win.document.querySelectorAll('.lobby-stale').length === 1,
         'stacked a second banner on every tick');
});

test('the notice sits above the lobby notice bar, not inside the card list', () => {
  const p = renderWith(makePage(), { count: 1, files: ['a.py'], started_at: 1 });
  const el = _staleEl(p);
  assert(!el.closest('#lobby-running'), 'rendered among the session cards');
});

test('one changed file reads as singular', () => {
  const p = renderWith(makePage(), { count: 1, files: ['a.py'], started_at: 1 });
  const t = _staleEl(p).textContent;
  assert(/1 loaded source file changed/.test(t), t);
});

// --- 5. a session can be opened in its own tab -----------------------------
//
// Reported 2026-09-14: "on mobile (android chrome at least) i seem to only be
// able to have one orchestrator2 tab open at a time".  True: the cards were
// `<div>`s with click handlers, and a click handler has no "open in new tab" —
// long-press offers nothing, and neither does ctrl/middle-click on a desktop.
// The in-place switch was the only route to a session, so one tab was all you
// could have.  Making the title an anchor hands the job back to the browser,
// which already has that affordance on every platform.

test('a running session title is a real link', () => {
  const p = makePage().render([OURS]);
  const a = p.card(0).querySelector('a.lobby-card-title');
  assert(a, 'the title is not an anchor, so it cannot be opened in a new tab');
  assert(a.getAttribute('href'), 'anchor with no href');
});

test('the link points at the session it names', () => {
  const p = makePage().render([OURS]);
  const href = p.card(0).querySelector('a.lobby-card-title').getAttribute('href');
  assert(href.includes(encodeURIComponent(OURS.rid)), href);
});

test('a foreign session links by session id, having no local rid', () => {
  // `?rid=` names a runtime in *this* hub; a session held elsewhere has none,
  // so the link has to carry what does identify it.
  const p = makePage().render([FOREIGN]);
  const href = p.card(0).querySelector('a.lobby-card-title').getAttribute('href');
  assert(href.includes(encodeURIComponent(FOREIGN.session_id)), href);
  assert(!/[?&]rid=/.test(href), `linked by rid anyway: ${href}`);
});

test('the link carries the cwd and account a foreign session needs', () => {
  // Opening one starts a runtime for it, which needs both — a bare session id
  // would resolve against the wrong account or directory.
  const p = makePage().render([FOREIGN]);
  const href = p.card(0).querySelector('a.lobby-card-title').getAttribute('href');
  assert(href.includes('cwd='), href);
  assert(href.includes('account='), href);
});

function _click(p, el, opts) {
  const ev = new p.win.MouseEvent(
    'click', Object.assign({ bubbles: true, cancelable: true }, opts || {}));
  el.dispatchEvent(ev);
  return ev;
}

test('a plain click on a session title is handled in-page, not followed', () => {
  // Otherwise the tab navigates *and* switches in place: two journeys for one
  // tap, the second racing the first.
  const p = makePage().render([OURS]);
  const ev = _click(p, p.card(0).querySelector('a.lobby-card-title'));
  assert(ev.defaultPrevented, 'the plain click was allowed to navigate too');
});

test('a ctrl-click on a session title is left to the browser', () => {
  const p = makePage().render([OURS]);
  const ev = _click(p, p.card(0).querySelector('a.lobby-card-title'),
                    { ctrlKey: true });
  assert(!ev.defaultPrevented, 'swallowed the gesture the anchor exists for');
});

test('a middle-click on a session title is left to the browser', () => {
  const p = makePage().render([OURS]);
  const ev = _click(p, p.card(0).querySelector('a.lobby-card-title'),
                    { button: 1 });
  assert(!ev.defaultPrevented, 'swallowed a middle-click');
});

test('a recent session title is a link too', () => {
  const p = makePage();
  p.Lobby.render({ running: [], recent_pending: false, recent: [
    { session_id: 'r1', title: 'Old Thing', cwd: 'D:\\p', account: 'acct',
      age: '2h ago' }] });
  const a = p.win.document.querySelector('#lobby-recent a.lobby-item-title');
  assert(a, 'recent titles are not links');
  assert(a.getAttribute('href').includes('r1'), a.getAttribute('href'));
});

test('a plain click on a recent title is handled in-page, not followed', () => {
  const p = makePage();
  p.Lobby.render({ running: [], recent_pending: false, recent: [
    { session_id: 'r1', title: 'Old Thing', cwd: 'D:\\p', account: 'acct',
      age: '2h ago' }] });
  const a = p.win.document.querySelector('#lobby-recent a.lobby-item-title');
  assert(_click(p, a).defaultPrevented, 'the recent list navigated as well');
});

test('a ctrl-click on a recent title is left to the browser', () => {
  const p = makePage();
  p.Lobby.render({ running: [], recent_pending: false, recent: [
    { session_id: 'r1', title: 'Old Thing', cwd: 'D:\\p', account: 'acct',
      age: '2h ago' }] });
  const a = p.win.document.querySelector('#lobby-recent a.lobby-item-title');
  assert(!_click(p, a, { ctrlKey: true }).defaultPrevented,
         'swallowed a ctrl-click in the recent list');
});

test('the title still carries its tooltip', () => {
  // Added when the title was elided; turning it into an anchor must not drop
  // the only way to read a shortened one.
  const p = makePage().render([FOREIGN]);
  const a = p.card(0).querySelector('a.lobby-card-title');
  assert(a.getAttribute('title') === 'Good Photons', a.getAttribute('title'));
});

// --- 6. a tab must be able to reopen the session it used to be -------------
//
// Reported 2026-09-16: "my E OSc session somehow turned into a sessions window.
// so i clicked on 'E OSc' in the recents list to try to reload it. every time i
// click on it, it does nothing."
//
// `onAttached` stamps the tab `orch2-sess-<sid>` so other tabs can focus it.
// When that session's runtime went away the server pushed a `lobby_notice`,
// which forces the lobby in front — but left `_hasSession` true, so the
// landing path that resets the name never ran.  The tab was then a lobby
// wearing a session's name, and `window.open('', name)` resolves the *current*
// browsing context first, so clicking that session handed the tab itself,
// which it found non-blank and merely focused.  A no-op, repeatable forever.

// jsdom refuses a replaced `window.location`, and its navigation error carries
// no URL, so the assertion is on the behaviour that actually differed: the old
// code's *entire* effect was `focus()` on the window that already had focus.
// A click that only re-focuses the current tab is the bug, by definition.
// jsdom refuses a replaced `window.location`, so the assertion is on the
// behaviour that actually differed: the old code's *entire* effect was
// `focus()` on the window that already had focus.  A click whose only effect
// is re-focusing the current tab is the bug, by definition.
function _selfNamingPage() {
  const p = makePage();
  let focused = 0;
  p.win.focus = () => { focused++; };
  // Browsing-context lookup by name finds the current context first.
  p.win.open = (url, target) => (target === p.win.name ? p.win : null);
  return { p, focuses: () => focused };
}

test('clicking a session whose name this tab still carries is not a no-op', () => {
  const { p, focuses } = _selfNamingPage();
  p.Lobby.render({ running: [], recent_pending: false, recent: [
    { session_id: FOREIGN.session_id, title: 'E OSc', cwd: 'E:\\p',
      account: 'acct', age: '1h ago' }] });
  // The stale name is applied *after* the render, because rendering into a
  // never-attached tab is itself a landing and resets it.  The real sequence
  // is: attach (name claimed) -> session dies -> notice forces the lobby up
  // -> the name is left behind.
  p.win.name = 'orch2-sess-' + FOREIGN.session_id;
  const a = p.win.document.querySelector('#lobby-recent a.lobby-item-title');
  a.dispatchEvent(new p.win.MouseEvent('click', { bubbles: true, cancelable: true }));
  assert(focuses() === 0,
         'the click only re-focused the tab it was already in — the reported '
         + 'no-op, repeatable forever');
});

test('a session already open in another tab still focuses that tab', () => {
  // The self-check must not cost the feature it sits in front of: when the
  // session really is in a *different* tab, that tab is raised and left where
  // it is, with no reload.
  const p = makePage();
  let focused = 0;
  const other = { focus() { focused++; },
                  location: { href: 'http://localhost:8240/?rid=s9' } };
  p.win.open = () => other;
  p.Lobby.render({ running: [], recent_pending: false, recent: [
    { session_id: FOREIGN.session_id, title: 'E OSc', cwd: 'E:\\p',
      account: 'acct', age: '1h ago' }] });
  const a = p.win.document.querySelector('#lobby-recent a.lobby-item-title');
  a.dispatchEvent(new p.win.MouseEvent('click', { bubbles: true, cancelable: true }));
  assert(focused === 1, 'the other tab was not raised');
  assert(other.location.href === 'http://localhost:8240/?rid=s9',
         'reloaded a tab that was already showing the session');
});

test('a notice makes the tab give up the session name it is no longer showing', () => {
  // Otherwise every *other* tab that clicks that session focuses this one and
  // is handed a lobby — the same bug seen from the outside.
  const p = makePage();
  p.win.name = 'orch2-sess-abc';
  p.Lobby.onAttached({ rid: 's1', cwd: 'E:\\p', session_id: 'abc' });
  p.Lobby.showNotice('That session is no longer running.');
  assert(p.win.name !== 'orch2-sess-abc',
         'the tab still claims to be a session it is not showing');
});

test('an attached tab claims the session name', () => {
  // The fix must not cost the feature: focusing the right tab is why the name
  // exists.
  const p = makePage();
  p.Lobby.onAttached({ rid: 's1', cwd: 'E:\\p', session_id: 'abc' });
  assert(p.win.name === 'orch2-sess-abc', p.win.name);
});

console.log(`\n${ran - failures}/${ran} passed`);
process.exit(failures ? 1 : 0);
