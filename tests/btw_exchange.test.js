/* `/btw` renders inline, and unmistakably apart from the conversation.
 *
 * `/btw` is answered in a *fork* of the session: a second CLI, a new session
 * id, running alongside the live turn, which the real session never sees.  That
 * makes the rendering load-bearing rather than cosmetic -- an answer folded
 * into the transcript unmarked would have you reading replies to a question
 * the session never received, which is worse than not having the feature.
 *
 * Driven against the real chat.js in jsdom.  Run by tests/test_frontend_js.py.
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

function makePage() {
  const dom = new JSDOM(
    `<!doctype html><html><body><div id="messages"></div></body></html>`,
    { runScripts: 'outside-only', pretendToBeVisual: false });
  const win = dom.window;
  win.requestAnimationFrame = (fn) => { fn(); return 1; };
  win.cancelAnimationFrame = () => {};
  win.setTimeout = (fn) => { fn(); return 1; };
  win.clearTimeout = () => {};
  win.console = Object.assign(Object.create(console), { log() {}, warn() {}, error() {} });
  // NOTE: deliberately no `win.CSS`.  jsdom does not define it, and neither do
  // some browsers for older APIs -- a renderer that reads a bare `CSS` throws a
  // ReferenceError rather than short-circuiting, which is a real bug this file
  // is positioned to catch.
  win.eval(CHAT_JS + '\n;window.Chat = Chat;');
  win.Chat.init();
  const el = win.document.getElementById('messages');
  return {
    win,
    el,
    send: (m) => win.Chat.handleMessage(m),
    btw: () => el.querySelector('.msg-btw'),
  };
}

// ---------------------------------------------------------------------------

test('the question appears inline as soon as it is asked', () => {
  const p = makePage();
  p.send({ type: 'btw_start', id: 'btw1', question: 'why sqlite?' });

  const el = p.btw();
  assert(el, 'no side-exchange block was rendered');
  assert(el.textContent.includes('why sqlite?'), 'the question is not shown');
});

test('it is marked as a side exchange, not ordinary output', () => {
  const p = makePage();
  p.send({ type: 'btw_start', id: 'btw1', question: 'q' });

  const el = p.btw();
  assert(el.className.includes('msg-btw'),
         'nothing distinguishes it from a normal message');
  assert(/btw/i.test(el.querySelector('.btw-tag').textContent),
         'the block is not labelled');
});

test('it says the session itself never saw it', () => {
  const p = makePage();
  p.send({ type: 'btw_start', id: 'btw1', question: 'q' });

  const note = p.btw().querySelector('.btw-note').textContent.toLowerCase();
  assert(note.includes('fork') || note.includes('not seen'),
         'nothing tells the reader this is outside the conversation');
});

test('the answer streams in, appended rather than replaced', () => {
  const p = makePage();
  p.send({ type: 'btw_start', id: 'btw1', question: 'q' });
  p.send({ type: 'btw_delta', id: 'btw1', text: 'because ' });
  p.send({ type: 'btw_delta', id: 'btw1', text: 'it is embedded.' });

  const answer = p.btw().querySelector('.btw-answer').textContent;
  assert(answer === 'because it is embedded.',
         `blocks were not accumulated: ${JSON.stringify(answer)}`);
});

test('it is pending until it ends', () => {
  const p = makePage();
  p.send({ type: 'btw_start', id: 'btw1', question: 'q' });
  assert(p.btw().className.includes('pending'),
         'a fork that has not answered yet looks finished');

  p.send({ type: 'btw_delta', id: 'btw1', text: 'answer' });
  p.send({ type: 'btw_end', id: 'btw1' });
  assert(!p.btw().className.includes('pending'),
         'it still claims to be thinking after it answered');
});

test('a fork that failed says so instead of looking empty', () => {
  const p = makePage();
  p.send({ type: 'btw_start', id: 'btw1', question: 'q' });
  p.send({ type: 'btw_end', id: 'btw1', error: 'TimeoutError: connect' });

  const text = p.btw().textContent;
  assert(text.includes('TimeoutError'),
         'a failed aside renders as an empty block, reading as "nothing to say"');
});

test('a failure after partial output keeps what was said', () => {
  const p = makePage();
  p.send({ type: 'btw_start', id: 'btw1', question: 'q' });
  p.send({ type: 'btw_delta', id: 'btw1', text: 'half an answ' });
  p.send({ type: 'btw_end', id: 'btw1', error: 'CLIJSONDecodeError' });

  const text = p.btw().textContent;
  assert(text.includes('half an answ'), 'the partial answer was discarded');
  assert(text.includes('CLIJSONDecodeError'), 'the failure is not reported');
});

test('two asides do not write into each other', () => {
  const p = makePage();
  p.send({ type: 'btw_start', id: 'a', question: 'first' });
  p.send({ type: 'btw_start', id: 'b', question: 'second' });
  p.send({ type: 'btw_delta', id: 'a', text: 'ANSWER-A' });
  p.send({ type: 'btw_delta', id: 'b', text: 'ANSWER-B' });

  const blocks = p.el.querySelectorAll('.msg-btw');
  assert(blocks.length === 2, `expected 2 side exchanges, got ${blocks.length}`);
  const a = blocks[0].querySelector('.btw-answer').textContent;
  const b = blocks[1].querySelector('.btw-answer').textContent;
  assert(a === 'ANSWER-A' && b === 'ANSWER-B',
         `answers crossed over: ${JSON.stringify([a, b])}`);
});

test('a delta for an unknown id is ignored rather than thrown', () => {
  const p = makePage();
  p.send({ type: 'btw_delta', id: 'nope', text: 'x' });
  assert(!p.btw(), 'an orphan delta invented a block');
});

test('ending an unknown id does not throw', () => {
  const p = makePage();
  p.send({ type: 'btw_end', id: 'nope', error: 'boom' });
  assert(true);
});

test('a /btw exchange is never replayed from history', () => {
  // The fork writes to its own throwaway session, so nothing of it is in this
  // session's transcript.  A history renderer that handled these types would be
  // dead code pretending to be a feature.
  const p = makePage();
  p.send({ type: 'history', messages: [
    { type: 'btw_start', id: 'h', question: 'from history' },
  ], more_above: 0 });

  assert(!p.btw(), 'history replay rendered a side exchange');
});

console.log(`\n${ran - failures}/${ran} passed`);
process.exit(failures ? 1 : 0);
