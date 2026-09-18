/* The /move overlay picks an account AND a directory.
 *
 * Asked 2026-09-06: "i don't think there's a /command to copy a session to
 * another project dir, it should probably be integrated with the command to
 * copy a session to another account, so that you can do it to another account
 * AND dir if you want".
 *
 * The server half is covered by tests/test_move_directory.py.  What only the
 * browser can get wrong is the shape of the request it sends: which fields the
 * `move_do` message carries, and whether the two axes really are independent
 * (leaving one alone must not disturb the other).  A missing `cwd` here is
 * indistinguishable, server-side, from a deliberate "stay where you are".
 */

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const MOVE_JS = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'move.js'), 'utf8');

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

const ACCOUNTS = [
  { config_dir: 'C:\\Users\\me\\.claude', name: '.claude',
    email: 'a@example.com', is_current: true },
  { config_dir: 'C:\\Users\\me\\.claude-b', name: '.claude-b',
    email: 'b@example.com', is_current: false },
];
const DIRS = [
  { path: 'D:\\visual studio projects\\os', mtime: 9 },
  { path: 'D:\\visual studio projects\\orchestrator2', mtime: 8 },
  // Deliberately the current directory spelled differently: the filter has to
  // normalise, not string-compare, or the operator is offered a no-op.
  { path: 'd:/HERE/', mtime: 7 },
];
const HERE = 'D:\\here';

function makePage() {
  const dom = new JSDOM('<!doctype html><html><body></body></html>',
                        { runScripts: 'outside-only', pretendToBeVisual: false });
  const win = dom.window;
  const sent = [];
  const sandbox = win;
  sandbox.App = { send: (m) => sent.push(m) };
  win.eval(MOVE_JS + '\n;globalThis.__Move = Move;');
  const Move = win.__Move;

  const api = {
    win, sent, Move,
    open(arg) { Move.open(arg); },
    list(extra) {
      Move.renderAccounts(Object.assign({
        accounts: ACCOUNTS, dirs: DIRS, has_session: true,
        current_title: 'my session', current_cwd: HERE,
      }, extra || {}));
    },
    pickAccount(i) {
      win.document.querySelectorAll('.move-account')[i].click();
    },
    get cwdInput() { return win.document.querySelector('#move-cwd'); },
    get nameInput() { return win.document.querySelector('#move-name'); },
    get dirButtons() {
      return Array.from(win.document.querySelectorAll('.move-dir'));
    },
    go() { win.document.querySelector('.move-go').click(); },
    back() { win.document.querySelector('.move-back').click(); },
    get last() { return sent[sent.length - 1]; },
  };
  return api;
}

function opened(arg) {
  const p = makePage();
  p.open(arg);
  p.list();
  p.pickAccount(0);
  return p;
}

test('the destination step offers both a directory and a name', () => {
  const p = opened();
  assert(p.cwdInput, 'no directory field');
  assert(p.nameInput, 'no name field');
});

test('the directory defaults to where the session already is', () => {
  const p = opened();
  assert(p.cwdInput.value === HERE,
         `expected the current cwd, got ${p.cwdInput.value}`);
});

test('leaving everything alone still sends the current directory', () => {
  // The server treats "same path" as staying put, so echoing it back is safe
  // and keeps the message shape uniform -- but it must be the real path, not
  // an empty string that some later change could reinterpret.
  const p = opened();
  p.go();
  assert(p.last.type === 'move_do', 'wrong message type');
  assert(p.last.cwd === HERE, `cwd was ${JSON.stringify(p.last.cwd)}`);
  assert(p.last.config_dir === ACCOUNTS[0].config_dir, 'wrong account');
});

test('a typed directory is what gets sent', () => {
  const p = opened();
  p.cwdInput.value = 'D:\\somewhere\\else';
  p.go();
  assert(p.last.cwd === 'D:\\somewhere\\else', p.last.cwd);
});

test('changing only the directory keeps the current account', () => {
  const p = opened();          // index 0 is the account marked is_current
  p.cwdInput.value = 'D:\\somewhere\\else';
  p.go();
  assert(p.last.config_dir === ACCOUNTS[0].config_dir,
         'the current account was not selectable as a destination');
});

test('changing only the account keeps the directory', () => {
  const p = makePage();
  p.open();
  p.list();
  p.pickAccount(1);
  p.go();
  assert(p.last.config_dir === ACCOUNTS[1].config_dir, 'wrong account');
  assert(p.last.cwd === HERE, 'the directory moved on an account-only switch');
});

test('both can change at once', () => {
  const p = makePage();
  p.open();
  p.list();
  p.pickAccount(1);
  p.cwdInput.value = 'D:\\somewhere\\else';
  p.go();
  assert(p.last.config_dir === ACCOUNTS[1].config_dir, 'wrong account');
  assert(p.last.cwd === 'D:\\somewhere\\else', 'wrong directory');
});

test('a suggestion fills the directory field', () => {
  const p = opened();
  const btns = p.dirButtons;
  assert(btns.length > 0, 'no directory suggestions rendered');
  btns[0].click();
  assert(p.cwdInput.value === DIRS[0].path,
         `expected ${DIRS[0].path}, got ${p.cwdInput.value}`);
});

test('the directory it is already in is not offered', () => {
  // Suggesting the current directory is offering a no-op as if it were a
  // choice.
  const p = opened();
  const labels = p.dirButtons.map((b) => b.getAttribute('title'));
  assert(!labels.includes(HERE), 'offered the current directory');
  assert(!labels.includes('d:/HERE/'),
         'offered the current directory under a different spelling');
  assert(labels.includes(DIRS[0].path), 'dropped a real suggestion');
});

test('a suggestion is titled with the full path, not the shortened one', () => {
  // The button text is elided to fit; the tooltip has to be unambiguous,
  // because two projects can share a last-two-segments tail.
  const p = opened();
  const b = p.dirButtons[0];
  assert(b.getAttribute('title') === DIRS[0].path, b.getAttribute('title'));
  assert(b.textContent !== DIRS[0].path, 'the label was not shortened at all');
  assert(b.textContent.includes('os'), b.textContent);
});

test('/move <path> prefills the directory', () => {
  const p = opened('D:\\typed\\path');
  assert(p.cwdInput.value === 'D:\\typed\\path', p.cwdInput.value);
});

test('/move <path> still lets you choose the account', () => {
  // A path says nothing about which account you meant, so it must not skip
  // the account step.
  const p = makePage();
  p.open('D:\\typed\\path');
  p.list();
  assert(p.win.document.querySelectorAll('.move-account').length === 2,
         'the account step was skipped');
});

test('an empty name is refused before anything is sent', () => {
  const p = opened();
  p.nameInput.value = '   ';
  p.go();
  assert(p.sent.filter((m) => m.type === 'move_do').length === 0,
         'sent a switch with no name');
  const msg = p.win.document.querySelector('.move-msg');
  assert(msg && !msg.classList.contains('hidden'), 'said nothing about it');
});

test('going Back and forward again keeps the directory list', () => {
  // `renderAccounts` is re-called from cached state on Back, without `dirs`
  // or `current_cwd`; clobbering them there would silently empty the picker.
  const p = opened();
  p.back();
  p.pickAccount(1);
  assert(p.dirButtons.length > 0, 'the suggestions were lost on Back');
  assert(p.cwdInput.value === HERE, 'the current directory was lost on Back');
});

test('Enter in the directory field submits, like Enter in the name field', () => {
  const p = opened();
  const ev = new p.win.KeyboardEvent('keydown', { key: 'Enter', bubbles: true });
  p.cwdInput.dispatchEvent(ev);
  assert(p.last && p.last.type === 'move_do',
         'Enter in the directory field did nothing');
});

test('a session-less tab is told so instead of being offered a copy', () => {
  const p = makePage();
  p.open();
  p.list({ has_session: false });
  assert(p.win.document.querySelector('.move-error'), 'no explanation shown');
  assert(!p.win.document.querySelector('.move-account'),
         'offered accounts for a session that does not exist');
});

console.log(`\n${ran - failures}/${ran} passed`);
process.exit(failures ? 1 : 0);
