/* The lobby's Restart says when the restart did not happen.
 *
 * Only an answer of ok:true means the hub is going.  Anything else -- an
 * error status, a body that is not the restart's, ok:false -- leaves the hub
 * up, and the "restarting..." page would find it answering at once and
 * reload as if it had restarted.
 */

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const UTIL_JS = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'util.js'), 'utf8');
const LOBBY_JS = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'lobby.js'), 'utf8');

let failures = 0;
let ran = 0;

async function test(name, fn) {
  ran++;
  try {
    await fn();
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

/* A lobby whose POST /api/restart answers *status* with *body* (an object,
 * or a string that is not JSON); returns what the user was shown. */
async function restartAnswered(status, body) {
  const html = IDS.map((id) => `<div id="${id}"></div>`).join('');
  const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`,
                        { url: 'http://localhost:8240/', runScripts: 'outside-only' });
  const win = dom.window;
  win.App = { send() {}, isConnected: () => true };
  win.eval('window.Util = (function () {' + UTIL_JS + ';return Util; })();');
  win.confirm = () => true;
  const alerts = [];
  win.alert = (m) => alerts.push(m);
  win.fetch = async (url) => {
    if (url === '/api/restart') {
      return {
        ok: status >= 200 && status < 300,
        status,
        json: async () => {
          if (typeof body === 'string') throw new SyntaxError('not JSON');
          return body;
        },
      };
    }
    // The reload poll: the hub answers, as one that never stopped does.
    return { ok: true, status: 200, json: async () => ({}) };
  };
  win.setTimeout = () => 0;            // the poll is not followed
  win.eval(LOBBY_JS + '\n;globalThis.__Lobby = Lobby;');
  win.__Lobby.init();
  win.document.getElementById('lobby-restart').click();
  for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
  const splash = /restarting orchestrator2/.test(win.document.body.textContent);
  dom.window.close();
  return { alerts, splash };
}

(async () => {
  console.log('\nlobby restart\n');

  await test('a restart under way shows the restarting page', async () => {
    const got = await restartAnswered(200, { ok: true, message: 'restarting' });
    assert(got.splash && got.alerts.length === 0, JSON.stringify(got));
  });

  await test('a replacement that failed to start is reported', async () => {
    const got = await restartAnswered(200, { ok: false, error: 'spawn failed: x' });
    assert(!got.splash, 'showed the restarting page');
    assert(got.alerts.length === 1 && got.alerts[0].includes('spawn failed: x'),
           JSON.stringify(got.alerts));
  });

  await test('a request that never reached the restart is reported', async () => {
    // 422: the route called something else.
    const got = await restartAnswered(422, { detail: [{ msg: 'Field required' }] });
    assert(!got.splash, 'a 422 was taken for a restart');
    assert(got.alerts.length === 1 && got.alerts[0].includes('HTTP 422'),
           JSON.stringify(got.alerts));
  });

  await test('a server error is reported', async () => {
    const got = await restartAnswered(500, 'Internal Server Error');
    assert(!got.splash, 'a 500 was taken for a restart');
    assert(got.alerts.length === 1 && got.alerts[0].includes('HTTP 500'),
           JSON.stringify(got.alerts));
  });

  console.log(`\n${ran - failures}/${ran} passed`);
  process.exit(failures ? 1 : 0);
})();
