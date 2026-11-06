/* A run of activity collapses, but not what the user is reading.
 *
 * Reported 2026-11-06: "when i have a tool expanded to read it, sometimes it
 * autocollapses what i'm reading as i'm reading it." -- and -- "a question
 * collapsed automatically because questions apparently count as tool uses. i
 * could have missed it if i hadn't been watching before it collapsed."
 *
 * When Claude's next text, a user message or the turn's end arrives, the
 * tools, thinking and notices since the last boundary are wrapped into one
 * group, collapsed (_collapseActivity).  A run holding a block the user has
 * open is grouped open instead; a question is a block of its own that no
 * group takes, shown once, and reads as waiting for its reply.
 */

const { JSDOM } = require('jsdom');

const CHAT_JS = require('./chat_source');

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
  const dom = new JSDOM('<!doctype html><html><body><div id="messages"></div></body></html>',
                        { runScripts: 'outside-only' });
  const win = dom.window;
  const pending = [];
  win.setTimeout = (fn) => { pending.push(fn); return pending.length; };
  win.clearTimeout = () => {};
  win.requestAnimationFrame = (fn) => { pending.push(() => fn(0)); return pending.length; };
  win.cancelAnimationFrame = () => {};
  win.HTMLElement.prototype.scrollIntoView = () => {};
  win.console = Object.assign(Object.create(console), {
    warn: (...a) => { throw new Error('chat.js warned: ' + a.join(' ')); },
    error: (...a) => { throw new Error('chat.js errored: ' + a.join(' ')); },
    log: () => {},
  });
  win.eval(CHAT_JS + '\n;window.Chat = Chat;');
  win.Chat.init();
  const doc = win.document;
  let n = 0;
  return {
    doc,
    Chat: win.Chat,
    send(msg) {
      win.Chat.handleMessage(msg);
      for (let g = 0; pending.length && g < 200; g++) pending.shift()();
    },
    text(t) { this.send({ type: 'assistant_text', content: t, delta: false }); },
    tool(name = 'Read', input = { file_path: '/x' }) {
      const id = `t${++n}`;
      this.send({ type: 'tool_use', tool_use_id: id, name, status: 'running', input });
      this.send({ type: 'tool_result', tool_use_id: id, content: 'file text' });
      return this.block(id);
    },
    block(id) { return doc.querySelector(`.msg-tool[data-tool-use-id="${id}"]`); },
    groups() { return [...doc.querySelectorAll('.activity-group')]; },
    click(el, sel) { el.querySelector(sel).click(); },
  };
}

/** Whether *el* is shown: in no collapsed group. */
function shown(el) {
  return !el.closest('.activity-group.collapsed');
}

const QUESTIONS = { questions: [{ question: 'Which one?', header: 'Pick', multiSelect: false,
  options: [{ label: 'A', description: 'a' }, { label: 'B', description: 'b' }] }] };
const DENIAL = "AskUserQuestion isn't supported in this interface, so it was not run.";

/** A turn that asks, as the hub sends it: the question, its denial, and
 *  Claude thinking and writing on before the turn ends. */
function ask(p, { awaiting = true } = {}) {
  p.send({ type: 'tool_use', tool_use_id: 'q1', name: 'AskUserQuestion', status: 'running',
           input: QUESTIONS });
  p.send({ type: 'tool_result', tool_use_id: 'q1', is_error: true, content: DENIAL,
           awaiting_reply: awaiting });
  return p.block('q1');
}

console.log('\nactivity collapse\n');

// ---- a block the user opened ----------------------------------------------

test('a run holding a tool the user opened is grouped open', () => {
  const p = makePage();
  p.text('Working.');
  p.tool();
  const read = p.tool();
  p.tool();
  p.click(read, '.tool-header');
  p.text('Done reading.');
  const [g] = p.groups();
  assert(g, 'not grouped at all');
  assert(shown(read), 'the tool being read was collapsed away');
  assert(read.querySelector('.tool-detail').classList.contains('open'), 'its detail closed');
  assert(g.querySelector('.activity-group-toggle').textContent.startsWith('▼'),
         'the header does not show the group open: '
         + g.querySelector('.activity-group-toggle').textContent);
});

test('and its header still folds it away', () => {
  const p = makePage();
  p.text('Working.');
  const read = p.tool();
  p.tool();
  p.click(read, '.tool-header');
  p.text('Done reading.');
  const [g] = p.groups();
  g.querySelector('.activity-group-toggle').click();
  assert(!shown(read), 'still shown');
  assert(g.querySelector('.activity-group-toggle').textContent === '▶ 2 tools',
         g.querySelector('.activity-group-toggle').textContent);
});

test('a run whose tool was opened and closed again collapses', () => {
  const p = makePage();
  p.text('Working.');
  const read = p.tool();
  p.tool();
  p.click(read, '.tool-header');
  p.click(read, '.tool-header');
  p.text('Done reading.');
  assert(!shown(read), 'left open with nothing open in it');
});

test('a run nobody opened collapses, as ever', () => {
  const p = makePage();
  p.text('Working.');
  const a = p.tool();
  p.tool();
  p.text('Done.');
  assert(!shown(a), 'not collapsed');
  assert(p.groups()[0].querySelector('.activity-group-toggle').textContent === '▶ 2 tools');
});

test('a thinking block the user opened keeps its run open', () => {
  const p = makePage();
  p.text('Working.');
  p.send({ type: 'thinking', content: 'line one\nline two' });
  const thinking = p.doc.querySelector('.msg-thinking');
  p.tool();
  p.click(thinking, '.thinking-header');
  p.text('Done.');
  assert(shown(thinking), 'the thinking being read was collapsed away');
});

test('one drawn open by --show-thinking is not one the user opened', () => {
  const p = makePage();
  p.Chat.setShowThinking(true);
  p.text('Working.');
  p.send({ type: 'thinking', content: 'line one\nline two' });
  const thinking = p.doc.querySelector('.msg-thinking');
  assert(thinking.querySelector('.thinking-body').classList.contains('open'), 'not drawn open');
  p.tool();
  p.text('Done.');
  assert(!shown(thinking), 'every run with thinking in it would stay open');
});

test('so does a background task\'s output the user opened', () => {
  const p = makePage();
  p.text('Working.');
  p.send({ type: 'bg_complete', task_id: 'bg1', name: 'build', status: 'completed',
           command: 'make', output: 'built' });
  const bg = p.doc.querySelector('[data-bg-task-id="bg1"]');
  p.tool();
  p.click(bg, '.tool-header');
  p.text('Done.');
  assert(shown(bg), 'the output being read was collapsed away');
});

test('a block shown from a panel is shown out of its group, open', () => {
  const p = makePage();
  p.text('Working.');
  const a = p.tool();
  p.tool();
  p.text('Done.');
  assert(!shown(a), 'precondition: collapsed');
  p.Chat.reveal(a);
  assert(shown(a), 'still in a collapsed group');
  assert(a.querySelector('.tool-detail').classList.contains('open'), 'its detail is shut');
  assert(p.groups()[0].querySelector('.activity-group-toggle').textContent === '▼ 2 tools',
         p.groups()[0].querySelector('.activity-group-toggle').textContent);
});

test('and counts as opened by the user, so its run is not collapsed under them', () => {
  const p = makePage();
  p.text('Working.');
  const a = p.tool();
  p.tool();
  p.Chat.reveal(a);
  p.text('Done.');
  assert(shown(a), 'collapsed while being read');
});

// ---- a question --------------------------------------------------------------

test('a question is never collapsed, and what ran before it is', () => {
  const p = makePage();
  p.text('Let me check, then ask.');
  p.send({ type: 'thinking', content: 'hmm\nhmm' });
  const before = p.tool('Bash', { command: 'ls' });
  const q = ask(p);
  p.send({ type: 'thinking', content: 'wait for the answer\nnow' });
  p.text('Over to you.');
  p.send({ type: 'turn_end', turns: 1, duration: '1s', subtype: 'success' });
  assert(!q.closest('.activity-group'), 'the question went into a group');
  assert(q.querySelector('.tool-detail').classList.contains('open'), 'its questions are shut');
  assert(q.textContent.includes('Which one?'), 'its question is not shown');
  // A question cuts the run short: nothing later reaches back past it.
  assert(!shown(before), 'the tool before the question was left out of every group');
});

test('a question waiting for its reply reads as asked, not as a failed tool', () => {
  const p = makePage();
  const q = ask(p);
  const icon = q.querySelector('.tool-status-icon');
  assert(icon.classList.contains('asking') && icon.textContent === '?',
         `${icon.className} ${icon.textContent}`);
  assert(!q.querySelector('.tool-name').style.color, 'its name is tinted as a failure');
  assert(!q.textContent.includes("isn't supported"), 'the denial meant for Claude is shown');
  assert(q.querySelector('.ask-reply-hint'), 'nothing says where to answer');
});

test('a question that really failed still reads as failed', () => {
  const p = makePage();
  const q = ask(p, { awaiting: false });
  assert(q.querySelector('.tool-status-icon').classList.contains('error'));
  assert(q.textContent.includes("isn't supported"), 'the error is hidden');
  assert(!q.querySelector('.ask-reply-hint'));
});

test('a question from history reads the same', () => {
  const p = makePage();
  p.send({ type: 'history', messages: [
    { type: 'assistant', content: 'Asking.' },
    { type: 'tool_use', tool_use_id: 'q1', name: 'AskUserQuestion', input: QUESTIONS },
    { type: 'tool_result', tool_use_id: 'q1', is_error: true, content: DENIAL,
      awaiting_reply: true },
    { type: 'thinking', content: 'a\nb' },
    { type: 'assistant', content: 'Over to you.' },
  ] });
  const q = p.block('q1');
  assert(q && !q.closest('.activity-group'), 'the question went into a group');
  assert(q.querySelector('.tool-status-icon').classList.contains('asking'));
});

console.log(`\n${ran - failures}/${ran} passed`);
process.exit(failures ? 1 : 0);
