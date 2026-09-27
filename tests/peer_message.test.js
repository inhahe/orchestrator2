/* A peer session's message is shown as the peer's; a prompt the session was
 * handed mid-turn is shown as the user's, marked.
 *
 * Reported 2026-09-27: "the ai responded with 'reply sent' and never showed me
 * anything ... i reloaded the page, and i didn't see my message shown, instead
 * i saw a message sent from another agent that wasn't showing before the
 * reload."  The server half is tests/test_cli_queue_race.py; this is how
 * chat.js draws what it is sent.
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
    '<!doctype html><html><body><div id="messages"></div></body></html>',
    { runScripts: 'outside-only' });
  const win = dom.window;
  const pending = [];
  win.setTimeout = (fn) => { pending.push(fn); return pending.length; };
  win.clearTimeout = () => {};
  win.requestAnimationFrame = (fn) => { pending.push(() => fn(0)); return pending.length; };
  win.cancelAnimationFrame = () => {};
  // An unhandled message type is a console.warn in chat.js -- make it fatal.
  win.console = Object.assign(Object.create(console), {
    warn: (...a) => { throw new Error('chat.js warned: ' + a.join(' ')); },
    error: (...a) => { throw new Error('chat.js errored: ' + a.join(' ')); },
    log: () => {},
  });
  win.eval(CHAT_JS + '\n;window.Chat = Chat;');
  win.Chat.init();
  const doc = win.document;
  return {
    doc,
    send(msg) { win.Chat.handleMessage(msg); this.flush(); },
    flush() {
      for (let guard = 0; pending.length && guard < 200; guard++) {
        pending.shift()();
      }
    },
    all(sel) { return Array.from(doc.querySelectorAll(sel)); },
  };
}

console.log('\npeer messages\n');

// ---- live ------------------------------------------------------------------

test('a peer\'s message is shown, with who sent it', () => {
  const p = makePage();
  p.send({ type: 'peer_message', name: 'Lane-A',
           body: 'Lane A -> Lane D: two operator answers' });
  const [el] = p.all('.msg-peer');
  assert(el, 'nothing drawn for a peer_message');
  assert(el.querySelector('.msg-label').textContent.includes('Lane-A'),
         'the sender is not named: ' + el.querySelector('.msg-label').textContent);
  assert(el.querySelector('.msg-content').textContent
         === 'Lane A -> Lane D: two operator answers', 'the body is not shown');
});

test('it is not drawn as the user\'s own', () => {
  const p = makePage();
  p.send({ type: 'peer_message', name: 'Lane-A', body: 'hello' });
  assert(!p.all('.msg-user').length, 'a peer\'s words were put in a "You" box');
});

test('its body is text, never markup', () => {
  // It comes from another process; nothing in it may become page content.
  const p = makePage();
  const body = '<img src=x onerror="window.pwned=1"><b>bold</b>';
  p.send({ type: 'peer_message', name: '<i>Lane</i>', body });
  const [el] = p.all('.msg-peer');
  assert(!el.querySelector('img, b, i'), 'markup in a peer message was rendered');
  assert(el.querySelector('.msg-content').textContent === body, 'the text was altered');
});

test('an empty one draws nothing', () => {
  const p = makePage();
  p.send({ type: 'peer_message', name: 'Lane-A', body: '' });
  assert(!p.all('.msg-peer').length, 'drew an empty peer message');
});

test('it is never folded away into a collapsed activity group', () => {
  // Collapsing takes *every* element since the last boundary, not just tool
  // calls.  A peer's message that is not a boundary is swept into the group
  // made from the calls after it -- collapsed, so out of sight: the very
  // thing this message type exists to show.
  const p = makePage();
  const tool = (id) => ({ type: 'tool_use', name: 'Bash', input: { command: id },
                          tool_use_id: id, status: 'complete', header: 'Bash ' + id });
  p.send(tool('t-before-1'));
  p.send(tool('t-before-2'));
  p.send({ type: 'peer_message', name: 'Lane-A', body: 'hello' });
  p.send(tool('t-after-1'));
  p.send(tool('t-after-2'));
  p.send({ type: 'user_message', content: 'next' });
  const peer = p.doc.querySelector('.msg-peer');
  assert(peer, 'the peer message is gone');
  assert(!peer.closest('.activity-group'),
         'the peer message was collapsed into an activity group');
  // ...while the calls on either side still collapse, each on its own side.
  const order = p.all('#messages *');
  const at = (sel) => order.indexOf(p.doc.querySelector(sel));
  assert(at('[data-tool-use-id="t-before-2"]') < order.indexOf(peer),
         'a call from before it was moved below it');
  assert(at('[data-tool-use-id="t-after-1"]') > order.indexOf(peer),
         'a call from after it was moved above it');
});

// ---- history ---------------------------------------------------------------

test('in history too it is the peer\'s', () => {
  const p = makePage();
  p.send({ type: 'history', more_above: 0, messages: [
    { type: 'peer_message', name: 'Lane-A', body: 'hello', is_history: true },
  ] });
  assert(p.all('.msg-peer').length === 1, 'a history peer message was not drawn');
  assert(!p.all('.msg-user').length, 'it was drawn as the user\'s');
});

test('a prompt handed to a running turn shows as yours, marked', () => {
  const p = makePage();
  p.send({ type: 'history', more_above: 0, messages: [
    { type: 'user', content: 'my question', mid_turn: true, is_history: true },
  ] });
  const [el] = p.all('.msg-user');
  assert(el, 'the user\'s mid-turn message was not drawn');
  assert(el.querySelector('.msg-content').textContent === 'my question');
  assert(el.querySelector('.msg-midturn'),
         'nothing says it went in while the session was working');
});

test('an ordinary prompt carries no such mark', () => {
  const p = makePage();
  p.send({ type: 'history', more_above: 0, messages: [
    { type: 'user', content: 'plain', is_history: true },
  ] });
  const [el] = p.all('.msg-user');
  assert(el && !el.querySelector('.msg-midturn'), 'an ordinary prompt was marked');
});

console.log(`\n${ran - failures}/${ran} passed`);
process.exit(failures ? 1 : 0);
