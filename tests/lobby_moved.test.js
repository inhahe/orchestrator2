/* A session /move left behind says where it went, and takes you there.
 *
 * Built 2026-10-23 with /move's "delete the original afterwards": an original
 * that is kept looks in the session list just as it did before the move -- an
 * invitation to carry on in the copy's past.  So the server marks it
 * (`moved_to`, tests/test_move_delete_original.py) and the list shows the mark:
 * the item is dimmed, says "moved -> <copy>", and opens the copy, with the
 * original a smaller link away.
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

/* The lobby, showing *recent*; `opened` is every tab a click asked for, as
 * {name, url}. */
function page(recent) {
  const body = IDS.map((id) => `<div id="${id}"></div>`).join('');
  const dom = new JSDOM(`<!doctype html><html><body>${body}</body></html>`,
                        { url: 'http://localhost:8240/',
                          runScripts: 'outside-only', pretendToBeVisual: false });
  const win = dom.window;
  win.App = { send: () => {}, isConnected: () => true };
  win.Util = { formatDuration: (s) => `${s}s` };
  const opened = [];
  win.open = (_url, name) => {
    const tab = { name, location: { href: 'about:blank' }, focus() {} };
    opened.push(tab);
    return tab;
  };
  win.eval(LOBBY_JS + '\n;globalThis.__Lobby = Lobby;');
  win.__Lobby.init();
  win.__Lobby.render({ running: [], recent: recent, recent_pending: false });
  const doc = win.document;
  return {
    doc, win,
    item(i) { return doc.querySelectorAll('#lobby-recent .lobby-item')[i || 0]; },
    click(el) {
      el.dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
      const tab = opened[opened.length - 1];
      return tab && { name: tab.name, url: new URL(tab.location.href, 'http://localhost:8240/') };
    },
  };
}

const ORIG = {
  session_id: 'orig-1111', title: 'OS lane A', cwd: 'D:\\os',
  account: 'C:\\Users\\me\\.claude', age: '2h',
};
const COPY = {
  session_id: 'copy-2222', title: 'OS lane A (b)', cwd: 'E:\\os',
  account: 'C:\\Users\\me\\.claude-account-b',
};
const MOVED = Object.assign({}, ORIG, { moved_to: COPY });

console.log('\nlobby: moved sessions\n');

test('a moved original opens its copy', () => {
  const p = page([MOVED]);
  const got = p.click(p.item().querySelector('.lobby-item-title'));
  assert(got && got.name === 'orch2-sess-copy-2222', got && got.name);
  assert(got.url.searchParams.get('open') === 'copy-2222', got.url.href);
  assert(got.url.searchParams.get('cwd') === 'E:\\os', got.url.href);
  assert(got.url.searchParams.get('account') === COPY.account, got.url.href);
});

test('so does its link, opened in a new tab', () => {
  const href = page([MOVED]).item().querySelector('.lobby-item-title').getAttribute('href');
  assert(new URL(href, 'http://x/').searchParams.get('open') === 'copy-2222', href);
});

test('it says where it went: the copy, and its account when that is another', () => {
  const said = page([MOVED]).item().querySelector('.lobby-item-moved-to');
  assert(said, 'no mark');
  const text = said.textContent.replace(/\s+/g, ' ');
  assert(/moved/.test(text) && text.includes('OS lane A (b)'), text);
  assert(text.includes('.claude-account-b'), text);
});

test('an account it did not leave is not named', () => {
  const same = Object.assign({}, ORIG, {
    moved_to: Object.assign({}, COPY, { account: 'C:/Users/me/.claude' }) });
  const text = page([same]).item().querySelector('.lobby-item-moved-to').textContent;
  assert(!text.includes('.claude'), text);
});

test('the original is still a click away', () => {
  const p = page([MOVED]);
  const got = p.click(p.item().querySelector('.lobby-item-original'));
  assert(got && got.name === 'orch2-sess-orig-1111', got && got.name);
  assert(got.url.searchParams.get('open') === 'orig-1111', got.url.href);
  assert(got.url.searchParams.get('account') === ORIG.account, got.url.href);
});

test('it is dimmed, so the copy is what the eye finds', () => {
  assert(page([MOVED]).item().classList.contains('lobby-item-moved'), 'not dimmed');
});

test('a session that was not moved is as it was', () => {
  const p = page([ORIG]);
  const item = p.item();
  assert(!item.classList.contains('lobby-item-moved'), 'dimmed');
  assert(!item.querySelector('.lobby-item-moved-to'), 'marked');
  const got = p.click(item.querySelector('.lobby-item-title'));
  assert(got && got.name === 'orch2-sess-orig-1111', got && got.name);
});

test('the copy\'s title is text, not markup', () => {
  const evil = Object.assign({}, ORIG, {
    moved_to: Object.assign({}, COPY, { title: '<img src=x onerror=alert(1)>' }) });
  const said = page([evil]).item().querySelector('.lobby-item-moved-to');
  assert(!said.querySelector('img'), 'made an element');
  assert(said.textContent.includes('<img'), said.textContent);
});

console.log(`\n${ran - failures}/${ran} passed`);
process.exit(failures ? 1 : 0);
