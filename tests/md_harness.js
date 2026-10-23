/* The chat's markdown renderer, driven as the page drives it: an assistant
 * message through Chat.handleMessage, in jsdom.  Shared by the markdown tests,
 * so that every one of them fails on a warning or an error chat.js logs while
 * rendering, not only the one that thought to check. */

const { JSDOM } = require('jsdom');

// chat.js after the markdown library, as the page loads it.
const CHAT_JS = require('./chat_source');

/* The rendered message for assistant text *text*. */
function rendered(text) {
  const dom = new JSDOM(
    '<!doctype html><html><body><div id="messages"></div></body></html>',
    { runScripts: 'outside-only' });
  const win = dom.window;
  const pending = [];
  win.setTimeout = (fn) => { pending.push(fn); return pending.length; };
  win.clearTimeout = () => {};
  win.requestAnimationFrame = (fn) => { pending.push(() => fn(0)); return pending.length; };
  win.cancelAnimationFrame = () => {};
  win.console = Object.assign(Object.create(console), {
    warn: (...a) => { throw new Error('chat.js warned: ' + a.join(' ')); },
    error: (...a) => { throw new Error('chat.js errored: ' + a.join(' ')); },
    log: () => {},
  });
  win.eval(CHAT_JS + '\n;window.Chat = Chat;');
  win.Chat.init();
  win.Chat.handleMessage({ type: 'assistant_text', content: text, delta: false });
  for (let g = 0; pending.length && g < 200; g++) pending.shift()();
  const el = win.document.querySelector('.md-rendered');
  if (!el) throw new Error('nothing rendered');
  return el;
}

/* A suite titled *title*: test(name, fn), assert(cond, msg), and done(),
 * which prints the tally and sets the exit code. */
function suite(title) {
  let failures = 0;
  let ran = 0;
  console.log(`\n${title}\n`);
  return {
    test(name, fn) {
      ran++;
      try {
        fn();
        console.log(`  ok    ${name}`);
      } catch (e) {
        failures++;
        console.log(`  FAIL  ${name}\n        ${e && e.message}`);
      }
    },
    assert(cond, msg) {
      if (!cond) throw new Error(msg || 'assertion failed');
    },
    done() {
      console.log(`\n${ran - failures}/${ran} passed`);
      process.exit(failures ? 1 : 0);
    },
  };
}

module.exports = { rendered, suite };
