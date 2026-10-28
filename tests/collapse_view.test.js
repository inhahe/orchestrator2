/* A run of tools collapsing leaves something on screen.
 *
 * Reported 2026-10-27: "sometimes the tool list fills the view and then when
 * they all collapse, the view is blank. it's especially confusing if you just
 * tabbed over to that tab and the page is blank."
 *
 * A collapse holds the view still, so the text above the run does not lurch:
 * the space the run gave up is held open as padding at the bottom
 * (_collapseActivity's gap).  When the run filled the view, nothing above it
 * was on screen to hold, and the held view was the empty space -- found blank
 * on coming back to the tab, when the run had collapsed meanwhile.
 *
 * jsdom does no layout, so a small model of one stands in: every element has a
 * height by its kind, the list scrolls, and an element's place in the view is
 * the sum of what is above it less how far the list has scrolled.
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

const VIEW_H = 500;

/* An element's height in the model: a tool block 100 px, a collapsed group
 * 24, anything else (a message) 40. */
function heightOf(el) {
  if (el.classList.contains('msg-tool')) return 100;
  if (el.classList.contains('activity-group')) return 24;
  return 40;
}

function makePage({ hidden = false } = {}) {
  // Layout reads, counted: each would be a forced layout in a browser.
  let reads = 0;
  const dom = new JSDOM('<!doctype html><html><body><div id="messages"></div></body></html>',
                        { runScripts: 'outside-only' });
  const win = dom.window;
  const doc = win.document;
  let _hidden = hidden;
  Object.defineProperty(doc, 'hidden', { configurable: true, get: () => _hidden });
  const frames = [];
  win.requestAnimationFrame = (fn) => { if (!_hidden) frames.push(fn); return frames.length; };
  win.cancelAnimationFrame = () => {};
  win.setTimeout = () => 0;
  win.clearTimeout = () => {};

  const el = doc.getElementById('messages');
  const content = () => [...el.children].reduce((n, c) => n + heightOf(c), 0);
  const pad = () => parseFloat(el.style.paddingBottom) || 0;
  let top = 0;
  const maxTop = () => Math.max(0, content() + pad() - VIEW_H);
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => Math.max(VIEW_H, content() + pad()) });
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => VIEW_H });
  Object.defineProperty(el, 'clientWidth', { configurable: true, get: () => 800 });
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => { reads++; return Math.min(top, maxTop()); },
    set: (v) => { top = Math.max(0, Math.min(v, maxTop())); },
  });
  // The list at the top of the view; each of its children where it is.
  const rect = (t, h) => ({ top: t, bottom: t + h, height: h, left: 0, right: 800, width: 800 });
  el.getBoundingClientRect = () => rect(0, VIEW_H);
  const realRect = win.HTMLElement.prototype.getBoundingClientRect;
  win.HTMLElement.prototype.getBoundingClientRect = function () {
    if (this.parentElement !== el) return realRect.call(this);
    reads++;
    let y = 0;
    for (const c of el.children) {
      if (c === this) break;
      y += heightOf(c);
    }
    return rect(y - el.scrollTop, heightOf(this));
  };

  win.console = Object.assign(Object.create(console), {
    warn: (...a) => { throw new Error('chat.js warned: ' + a.join(' ')); },
    error: (...a) => { throw new Error('chat.js errored: ' + a.join(' ')); },
  });
  win.eval(CHAT_JS + '\n;window.Chat = Chat;');
  win.Chat.init();

  const C = win.Chat;
  return {
    el,
    get reads() { return reads; },
    set reads(v) { reads = v; },
    flush() {
      for (let g = 0; frames.length && g < 100; g++) frames.splice(0).forEach((f) => f && f(0));
    },
    text(t) { C.handleMessage({ type: 'assistant_text', content: t, delta: false }); this.flush(); },
    tools(n) {
      for (let i = 0; i < n; i++) {
        C.handleMessage({ type: 'tool_use', tool_use_id: `t${Math.random()}`, name: 'Bash',
                          status: 'running', input: { command: 'echo hi' } });
      }
      this.flush();
    },
    setHidden(v) {
      _hidden = v;
      const ev = doc.createEvent('Event');
      ev.initEvent('visibilitychange', true, true);
      doc.dispatchEvent(ev);
      this.flush();
    },
    /** How much of the view shows something, not held-open space. */
    get shown() { return Math.max(0, Math.min(content() - el.scrollTop, VIEW_H)); },
    get gapOpen() { return el.style.paddingBottom !== ''; },
  };
}

console.log('\ncollapse view\n');

test('a run that filled the view collapses into something to see', () => {
  const p = makePage();
  p.text('before');
  p.tools(8);                    // 800 px of tools: the view is all tools
  p.text('after');               // the run collapses
  assert(p.shown > 0, 'the view is blank');
  assert(!p.gapOpen, 'a gap was held open with nothing above the run on screen');
  // The newest text is in view.
  assert(p.shown >= 40 + 24, `only ${p.shown} px shown: the summary and the new text should be`);
});

test('a short run below text on screen still holds that text still', () => {
  const p = makePage();
  for (let i = 0; i < 20; i++) p.text(`line ${i}`);   // 800 px: the list scrolls
  p.tools(2);
  const before = p.el.scrollTop;
  p.text('after');
  assert(p.gapOpen, 'no gap: the text above the run lurched');
  assert(p.el.scrollTop === before, `the view moved from ${before} to ${p.el.scrollTop}`);
  assert(p.shown > 0, 'the view is blank');
});

test('a collapse in a hidden window measures nothing', () => {
  // A gap holds what is shown, and a hidden window shows nothing: each layout
  // read for one would be a forced layout of the whole list, for nobody.
  const p = makePage();
  p.text('before');
  p.setHidden(true);
  p.tools(8);
  p.reads = 0;
  p.text('after');
  assert(p.reads === 0, `${p.reads} layout reads for a collapse nobody sees`);
  assert(!p.gapOpen, 'a gap opened while nothing was shown');
  p.setHidden(false);
  assert(p.shown >= 40 + 24, `only ${p.shown} px shown on coming back to the tab`);
});

console.log(`\n${ran - failures}/${ran} passed`);
process.exit(failures ? 1 : 0);
