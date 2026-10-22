/* The lobby shows every halt in force, and lifts one with a button.
 *
 * Nothing lifts a halt but an explicit act, and a raiser that has gone --
 * finished, or moved to another machine with its project -- never comes back
 * to do it.  The halt then tells every new session to stop, indefinitely, and
 * the operator learns of it one confused session at a time.  The banner puts
 * it in front of them where they look at the sessions it is keeping idle.
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
  const dom = new JSDOM(`<!doctype html><html><body>${body}</body></html>`,
                        { url: 'http://localhost:8240/',
                          runScripts: 'outside-only', pretendToBeVisual: false });
  const win = dom.window;
  const sent = [];
  win.App = { send: (m) => sent.push(m), isConnected: () => true };
  win.Util = { formatDuration: (s) => `${s}s` };
  win.open = () => ({ focus() {}, location: { href: 'about:blank' } });
  win.confirm = () => true;
  win.eval(LOBBY_JS + '\n;globalThis.__Lobby = Lobby;');
  const Lobby = win.__Lobby;
  Lobby.init();
  return {
    win, Lobby, sent,
    render(halts) {
      Lobby.render({ running: [], recent: [], recent_pending: false,
                     halts: halts });
      return this;
    },
    rows() {
      return Array.from(win.document.querySelectorAll('#lobby-halts .lobby-halt'));
    },
    lifts() { return sent.filter((m) => m.type === 'lift_halt'); },
  };
}

const NOW = Math.floor(Date.now() / 1000);

const MIGRATION = {
  id: 1, scope_kind: 'machine', scope_value: '', raised_by: 'Lane-F',
  raised_at: NOW - 4 * 86400, reason: 'PC migration: commit, push and stop.',
  raiser_running: false,
};

const REPAIR = {
  id: 2, scope_kind: 'repo', scope_value: 'd:\\visual studio projects\\os\\.git',
  raised_by: 'Lane-A', raised_at: NOW - 600, reason: 'repairing the index',
  raiser_running: true,
};

function textOf(el) { return (el.textContent || '').replace(/\s+/g, ' ').trim(); }

test('no halt, no banner', () => {
  const p = makePage().render([]);
  assert(!p.win.document.getElementById('lobby-halts'), 'empty banner');
});

test('a payload without the field shows nothing', () => {
  // An older hub, or a registry that could not be read (null).
  const p = makePage();
  p.Lobby.render({ running: [], recent: [], recent_pending: false });
  p.render(null);
  assert(!p.win.document.getElementById('lobby-halts'), 'invented a halt');
});

test('a halt shows who raised it, how long ago, and why', () => {
  const t = textOf(makePage().render([MIGRATION]).rows()[0]);
  assert(t.includes('Lane-F'), t);
  assert(/4d ago/.test(t), t);
  assert(t.includes('PC migration'), t);
  assert(t.includes('every session on this machine'), t);
});

test('a raiser that has gone is said out loud', () => {
  const t = textOf(makePage().render([MIGRATION]).rows()[0]);
  assert(/Lane-F has no live session on this machine/.test(t), t);
});

test('a raiser that is running, or is nobody, is not called gone', () => {
  const p = makePage().render([REPAIR,
    Object.assign({}, MIGRATION, { id: 3, raised_by: 'operator',
                                   raiser_running: null })]);
  p.rows().forEach((r) => assert(!/no live session/.test(textOf(r)), textOf(r)));
});

test('a repository halt names the repository, not its .git directory', () => {
  const t = textOf(makePage().render([REPAIR]).rows()[0]);
  assert(t.includes('every session in d:\\visual studio projects\\os'), t);
  assert(!/\.git/.test(t), t);
});

test('every halt gets a row of its own, oldest first', () => {
  const p = makePage().render([MIGRATION, REPAIR]);
  assert(p.rows().map((r) => r.dataset.haltId).join() === '1,2',
         p.rows().map((r) => r.dataset.haltId).join());
});

test('the reason is shown as text, never as markup', () => {
  const evil = Object.assign({}, MIGRATION,
                             { reason: '<img src=x onerror="alert(1)">' });
  const p = makePage().render([evil]);
  assert(!p.rows()[0].querySelector('img'), 'the reason was parsed as HTML');
  assert(textOf(p.rows()[0]).includes('<img'), textOf(p.rows()[0]));
});

test('Lift sends the id of the halt it sits beside', () => {
  const p = makePage().render([MIGRATION, REPAIR]);
  p.rows()[1].querySelector('button').click();
  assert(p.lifts().length === 1 && p.lifts()[0].id === 2,
         JSON.stringify(p.lifts()));
});

test('Lift asks first, and a "no" sends nothing', () => {
  const p = makePage().render([MIGRATION]);
  let asked = '';
  p.win.confirm = (m) => { asked = m; return false; };
  p.rows()[0].querySelector('button').click();
  assert(/Lift this halt/.test(asked), asked);
  assert(asked.includes('PC migration'), 'the question omits what is lifted');
  assert(p.lifts().length === 0, 'lifted after the operator said no');
});

test('a second click while the first is in flight sends nothing more', () => {
  const p = makePage().render([MIGRATION]);
  const btn = p.rows()[0].querySelector('button');
  btn.click();
  p.render([MIGRATION]);          // the next two-second tick, not yet lifted
  btn.click();
  assert(p.lifts().length === 1, `${p.lifts().length} lifts sent`);
  assert(btn.disabled, 'the tick re-armed the button at once');
});

test('the two-second refresh keeps the button it already has', () => {
  // A button replaced between mousedown and mouseup loses the click.
  const p = makePage().render([MIGRATION]);
  const before = p.rows()[0].querySelector('button');
  p.render([MIGRATION]);
  assert(p.rows()[0].querySelector('button') === before,
         'the row was rebuilt on a refresh');
});

test('a lifted halt leaves the banner', () => {
  const p = makePage().render([MIGRATION, REPAIR]);
  p.render([REPAIR]);
  assert(p.rows().map((r) => r.dataset.haltId).join() === '2');
  p.render([]);
  assert(!p.win.document.getElementById('lobby-halts'), 'the banner stayed');
});

test('the banner sits above the lobby notice, not among the cards', () => {
  const p = makePage().render([MIGRATION]);
  const box = p.win.document.getElementById('lobby-halts');
  assert(!box.closest('#lobby-running'), 'rendered among the session cards');
  assert(box.nextElementSibling &&
         box.nextElementSibling.id === 'lobby-notice', 'not before the notice');
});

console.log(`\n${ran - failures}/${ran} passed`);
process.exit(failures ? 1 : 0);
