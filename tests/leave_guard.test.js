/* With --confirm-close, closing a tab asks first unless its session is
 * resting idle.
 *
 * Asked 2026-10-27: "confirm with a popup before the user can close a tab
 * that's in any state other than idle (and maybe waiting for a loop)".  These
 * drive static/leave_guard.js on its own, as the browser does: a
 * `beforeunload` event, asking if the page cancels it.  That the page wires
 * it up -- the status bar feeding it, the lobby switching it off -- is checked
 * in reconnect_on_show.test.js, against the real page.
 */

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const STATIC = path.join(__dirname, '..', 'static');
const GUARD_JS = fs.readFileSync(path.join(STATIC, 'util.js'), 'utf8') + '\n'
  + fs.readFileSync(path.join(STATIC, 'leave_guard.js'), 'utf8');

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

/* A page with the guard, and a function telling whether closing it now asks. */
function page() {
  const dom = new JSDOM('<!doctype html><html><body></body></html>',
                        { runScripts: 'outside-only' });
  const win = dom.window;
  const timers = [];
  win.setTimeout = (fn) => { timers.push(fn); return timers.length; };
  win.eval(GUARD_JS + '\n;window.LeaveGuard = LeaveGuard;');
  // Asking takes both: preventDefault, which Firefox and today's Chrome read,
  // and returnValue, which older Chrome and Edge read.  Either alone cancels
  // the event in jsdom, so each is watched on its own.
  const asks = () => {
    const e = new win.Event('beforeunload', { cancelable: true });
    let prevented = false;
    e.preventDefault = () => { prevented = true; };
    let returned;
    Object.defineProperty(e, 'returnValue', { set: (v) => { returned = v; }, get: () => returned });
    win.dispatchEvent(e);
    if (prevented !== (returned !== undefined)) {
      throw new Error(`asked by halves: preventDefault ${prevented}, returnValue ${JSON.stringify(returned)}`);
    }
    return prevented;
  };
  return { guard: win.LeaveGuard, asks, runTimers: () => timers.splice(0).forEach((f) => f()) };
}

const ON = { confirm_close: true };

console.log('\nleave guard\n');

test('closing a working session asks', () => {
  const { guard, asks } = page();
  guard.update({ ...ON, busy_class: 'working' });
  assert(asks());
});

test('so does any state but resting idle', () => {
  for (const s of [{ busy_class: 'connecting' }, { busy_class: 'compacting' },
                   { busy_class: 'bg-wait' }, { busy_class: 'error' },
                   { busy_class: 'reconnecting' },
                   { busy_class: 'idle', bg_count: 2 },
                   { busy_class: 'idle', wakeup_at: 1893456000 }]) {
    const { guard, asks } = page();
    guard.update({ ...ON, ...s });
    assert(asks(), JSON.stringify(s));
  }
});

test('resting idle does not', () => {
  const { guard, asks } = page();
  guard.update({ ...ON, busy_class: 'idle', bg_count: 0, wakeup_at: null });
  assert(!asks());
});

test('nor anything, without --confirm-close', () => {
  const { guard, asks } = page();
  guard.update({ busy_class: 'working', confirm_close: false });
  assert(!asks());
});

test('nor a session the hub has shut down, with nothing left to watch', () => {
  const { guard, asks } = page();
  guard.update({ ...ON, busy_class: 'shutdown' });
  assert(!asks());
});

test('nor a tab with no session -- the lobby', () => {
  const { guard, asks } = page();
  guard.update({ ...ON, busy_class: 'working' });
  guard.setLanding(true);
  assert(!asks());
  guard.setLanding(false);
  guard.update({ ...ON, busy_class: 'working' });
  assert(asks(), 'not asking again once it has a session');
});

test('nor the page navigating on purpose -- for a moment', () => {
  const { guard, asks, runTimers } = page();
  guard.update({ ...ON, busy_class: 'working' });
  guard.leavingOnPurpose();
  assert(!asks(), 'asked on the page\'s own navigation');
  runTimers();
  assert(asks(), 'a navigation that did not happen left closing unguarded');
});

test('it asks still while the hub cannot be reached', () => {
  // The page says so itself, with only that in the snapshot.
  const { guard, asks } = page();
  guard.update({ ...ON, busy_class: 'idle' });
  guard.update({ busy_class: 'reconnecting', busy_label: 'reconnecting', connection: true });
  assert(asks(), 'the reconnecting tab forgot --confirm-close');
  guard.update({ busy_class: 'shutdown', busy_label: 'server shut down', connection: true });
  assert(!asks(), 'asked with the hub shut down');
});

test('the latest state counts', () => {
  const { guard, asks } = page();
  guard.update({ ...ON, busy_class: 'working' });
  guard.update({ ...ON, busy_class: 'idle' });
  assert(!asks());
});

console.log(`\n${ran - failures}/${ran} passed`);
process.exit(failures ? 1 : 0);
