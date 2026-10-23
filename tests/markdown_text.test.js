/* The agent's text is shown as written, and only as text.
 *
 * The chat renders it with markdown-it (CommonMark), with the line breaks
 * the model wrote kept, raw HTML shown as text and images never fetched.
 * Driven through Chat.handleMessage, as the page renders an assistant
 * message; the last test loads chat.js without the library.
 */

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');
const { rendered, suite } = require('./md_harness');

const { test, assert, done } = suite('markdown text');

test('a line break the model wrote is a line break', () => {
  // A bold label over its paragraph: CommonMark alone would run them together.
  const el = rendered('**Bug 1 — the tab was replaced**\nThe three open paths test clean.');
  assert(el.querySelector('strong'), 'no bold');
  assert(el.querySelector('br'), 'the two lines were joined: ' + el.innerHTML);
});

test('a blank line is a new paragraph', () => {
  const el = rendered('one\n\ntwo');
  assert(el.querySelectorAll('p').length === 2, el.innerHTML);
});

test('raw HTML is shown as text', () => {
  const el = rendered('a <b>bold</b> claim <script>alert(1)</script> <img src=x onerror=alert(2)>');
  assert(!el.querySelector('b, script, img'), 'made an element: ' + el.innerHTML);
  assert(el.textContent.includes('<b>bold</b>'), el.textContent);
});

test('an image is a link to it, never fetched', () => {
  const el = rendered('![a chart](https://example.com/chart.png)');
  assert(!el.querySelector('img'), 'an image was put in the page');
  const a = el.querySelector('a');
  assert(a && a.getAttribute('href') === 'https://example.com/chart.png', el.innerHTML);
});

test('a quote is a quote', () => {
  const el = rendered('> quoted words');
  assert(el.querySelector('blockquote.md-quote'), el.innerHTML);
});

test('headings, tables and rules keep the chat\'s own markup', () => {
  const el = rendered('# Title\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n---\n\n~~gone~~');
  assert(el.querySelector('div.md-h.md-h1'), 'heading: ' + el.innerHTML);
  assert(el.querySelector('table.md-table th'), 'table');
  assert(el.querySelector('hr.md-hr'), 'rule');
  assert(el.querySelector('s'), 'strikethrough');
  const lists = rendered('- a\n\n1. b');
  assert(lists.querySelector('ul.md-list') && lists.querySelector('ol.md-list'), lists.innerHTML);
});

test('without the library the text is still shown, and the console told once', () => {
  const chat = fs.readFileSync(path.join(__dirname, '..', 'static', 'chat.js'), 'utf8');
  const dom = new JSDOM('<!doctype html><html><body><div id="messages"></div></body></html>',
                        { runScripts: 'outside-only' });
  const win = dom.window;
  const warned = [];
  const pending = [];
  win.setTimeout = (fn) => { pending.push(fn); return pending.length; };
  win.clearTimeout = () => {};
  win.requestAnimationFrame = (fn) => { pending.push(() => fn(0)); return pending.length; };
  win.cancelAnimationFrame = () => {};
  win.console = Object.assign(Object.create(console), {
    warn: (...a) => warned.push(a.join(' ')), log: () => {} });
  win.eval(chat + '\n;window.Chat = Chat;');
  win.Chat.init();
  for (const text of ['**one** <b>x</b>', 'two\nlines']) {
    win.Chat.handleMessage({ type: 'assistant_text', content: text, delta: false });
  }
  for (let g = 0; pending.length && g < 200; g++) pending.shift()();
  const els = [...win.document.querySelectorAll('.md-rendered')];
  const shown = els.map((e) => e.textContent);
  assert(shown[0] === '**one** <b>x</b>' && shown[1] === 'twolines', JSON.stringify(shown));
  assert(!win.document.querySelector('.md-rendered b'), 'markup ran without the library');
  // The page lays rendered text out as HTML: a line break must be one.
  assert(els[1].innerHTML === 'two<br>lines', els[1].innerHTML);
  assert(warned.length === 1, `told ${warned.length} times`);
});

done();
