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

test('a group held open for what was being read folds once that is closed', () => {
  const p = makePage();
  p.text('Working.');
  const a = p.tool();
  const b = p.tool();
  p.tool();
  p.click(a, '.tool-header');
  p.click(b, '.tool-header');
  p.text('Done reading.');
  p.click(a, '.tool-header');
  assert(shown(b), 'folded while another of its blocks was still open');
  p.click(b, '.tool-header');
  assert(!shown(b), 'left open with nothing open in it');
  assert(p.groups()[0].querySelector('.activity-group-toggle').textContent === '▶ 3 tools');
});

test('but a group opened at its header stays as the user left it', () => {
  const p = makePage();
  p.text('Working.');
  const a = p.tool();
  p.tool();
  p.text('Done.');
  p.groups()[0].querySelector('.activity-group-toggle').click();
  p.click(a, '.tool-header');
  p.click(a, '.tool-header');
  assert(shown(a), 'the group the user opened folded under them');
});

test('so does one held open, once the user has folded and opened it again', () => {
  const p = makePage();
  p.text('Working.');
  const a = p.tool();
  p.tool();
  p.click(a, '.tool-header');
  p.text('Done.');
  const toggle = p.groups()[0].querySelector('.activity-group-toggle');
  toggle.click();
  toggle.click();
  p.click(a, '.tool-header');
  assert(shown(a), 'the group the user opened folded under them');
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

test('a question answered reads as answered', () => {
  const p = makePage();
  const q = ask(p);
  p.send({ type: 'user_message', content: 'B', local: true });
  const icon = q.querySelector('.tool-status-icon');
  assert(icon.classList.contains('complete') && icon.textContent === '✓',
         `${icon.className} ${icon.textContent}`);
  assert(!q.querySelector('.ask-reply-hint'), 'it still says to answer it');
});

test('in history too, while the last one unanswered still waits', () => {
  const p = makePage();
  const turn = (id) => [
    { type: 'tool_use', tool_use_id: id, name: 'AskUserQuestion', input: QUESTIONS },
    { type: 'tool_result', tool_use_id: id, is_error: true, content: DENIAL,
      awaiting_reply: true }];
  p.send({ type: 'history', messages: [...turn('q1'), { type: 'user', content: 'A' },
                                        ...turn('q2')] });
  assert(p.block('q1').querySelector('.tool-status-icon').classList.contains('complete'),
         'an answered question still waits');
  assert(p.block('q2').querySelector('.tool-status-icon').classList.contains('asking'),
         'the open question reads as answered');
});

test('one in older history was answered, if a prompt is shown below it', () => {
  const p = makePage();
  p.send({ type: 'history', more_above: 2, messages: [{ type: 'user', content: 'A' }] });
  p.send({ type: 'history_prepend', messages: [
    { type: 'tool_use', tool_use_id: 'q1', name: 'AskUserQuestion', input: QUESTIONS },
    { type: 'tool_result', tool_use_id: 'q1', is_error: true, content: DENIAL,
      awaiting_reply: true }] });
  assert(p.block('q1').querySelector('.tool-status-icon').classList.contains('complete'),
         'answered below, and still waiting');
});

test('and older history does not answer the question waiting now', () => {
  const p = makePage();
  p.send({ type: 'history', more_above: 2, messages: [
    { type: 'tool_use', tool_use_id: 'q2', name: 'AskUserQuestion', input: QUESTIONS },
    { type: 'tool_result', tool_use_id: 'q2', is_error: true, content: DENIAL,
      awaiting_reply: true }] });
  p.send({ type: 'history_prepend', messages: [{ type: 'user', content: 'older' }] });
  assert(p.block('q2').querySelector('.tool-status-icon').classList.contains('asking'),
         'a prompt from before it was taken for its answer');
  p.send({ type: 'user_message', content: 'B', local: true });
  assert(p.block('q2').querySelector('.tool-status-icon').classList.contains('complete'),
         'the backfill lost it: the reply does not answer it');
});

test('one at the end of older history, with no prompt since, still waits', () => {
  const p = makePage();
  p.send({ type: 'history', more_above: 2, messages: [{ type: 'assistant', content: 'Over to you.' }] });
  p.send({ type: 'history_prepend', messages: [
    { type: 'tool_use', tool_use_id: 'q1', name: 'AskUserQuestion', input: QUESTIONS },
    { type: 'tool_result', tool_use_id: 'q1', is_error: true, content: DENIAL,
      awaiting_reply: true }] });
  assert(p.block('q1').querySelector('.tool-status-icon').classList.contains('asking'),
         'taken for answered with nothing said since');
});

test('the same result twice says where to answer once', () => {
  const p = makePage();
  const q = ask(p);
  p.send({ type: 'tool_result', tool_use_id: 'q1', is_error: true, content: DENIAL,
           awaiting_reply: true });
  assert(q.querySelectorAll('.ask-reply-hint').length === 1,
         `${q.querySelectorAll('.ask-reply-hint').length} hints`);
});

test('only a question\'s block reads as waiting, whatever a result says', () => {
  const p = makePage();
  p.send({ type: 'tool_use', tool_use_id: 'b1', name: 'Bash', status: 'running',
           input: { command: 'x' } });
  p.send({ type: 'tool_result', tool_use_id: 'b1', is_error: true, content: DENIAL + ' boom',
           awaiting_reply: true });
  const b = p.block('b1');
  assert(b.querySelector('.tool-status-icon').classList.contains('error'), 'not a failure');
  assert(b.textContent.includes('boom'), 'its error is hidden');
});

// ---- what the user asked for -------------------------------------------------

test('a /btw exchange is never collapsed, and what ran before it is', () => {
  const p = makePage();
  p.text('Working.');
  const before = p.tool();
  p.tool();
  p.send({ type: 'btw_start', id: 'w1', question: 'what is X?' });
  p.send({ type: 'btw_delta', id: 'w1', text: 'X is' });
  p.tool();
  p.tool();
  p.text('Done.');
  const btw = p.doc.querySelector('.msg-btw');
  assert(!btw.closest('.activity-group'), 'the answer being read was collapsed away');
  assert(!shown(before), 'the tools before it were left out of every group');
});

test('nor is a command\'s output', () => {
  const p = makePage();
  p.text('Working.');
  p.tool();
  p.send({ type: 'command_data', label: 'status', data: 'all well' });
  p.tool();
  p.tool();
  p.text('Done.');
  const out = p.doc.querySelector('.msg-command');
  assert(!out.closest('.activity-group'), 'what the user asked for was collapsed away');
});

test('the warning that Claude wrote a tool call as text stays with that text', () => {
  const p = makePage();
  p.text('Here: <invoke name="Bash">');
  p.tool();
  p.tool();
  p.text('Done.');
  const warn = p.doc.querySelector('.msg-system.warning');
  assert(warn && !warn.closest('.activity-group'), 'the warning was collapsed away');
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
