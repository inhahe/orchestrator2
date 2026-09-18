/* Rendering must not stop because nobody is looking.
 *
 * From a report: "sometimes the window isn't updated while it's not in focus,
 * and once it's put into focus, it takes a long time to get up to speed because
 * it only adds a few lines per second. Though sometimes it seems it's already
 * up to date when I put focus on the window."
 *
 * A hidden window gets no animation frames at all, and its timers are clamped
 * to 1s -- then to 1/minute once Chrome's intensive throttling starts five
 * minutes in.  WebSocket delivery is *not* throttled.  So messages keep
 * arriving and keep being appended, while anything scheduled on a frame is
 * frozen -- and any "already scheduled, skip" latch guarding a frame stays
 * latched for the entire time the window is hidden.
 *
 * `_maybeTrimOldMessages` was such a latch.  `_trimPending` went true, its
 * requestAnimationFrame never ran, and the 2000-message cap stopped being
 * enforced.  The list then grew without bound while every append kept doing
 * `scrollTop = scrollHeight`, forcing a synchronous layout over the whole list.
 * Layout is O(nodes), so the append rate decayed as the list grew and stayed
 * decayed after focus, because the DOM was still enormous.  That is the "few
 * lines per second"; and a window that is merely unfocused but still *visible*
 * keeps getting frames, which is the "sometimes it's already up to date".
 *
 * These tests drive the real chat.js in jsdom with frames suppressed, which is
 * what a hidden window actually is.  Run by tests/test_hidden_window.py.
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

/**
 * A page running chat.js.
 *
 * `hidden` drives document.hidden *and* whether requestAnimationFrame ever
 * fires -- because in a real browser those are the same fact, and a harness
 * that let frames run while claiming to be hidden would test nothing.
 */
function makePage({ hidden = false } = {}) {
  const dom = new JSDOM(
    `<!doctype html><html><body><div id="messages"></div></body></html>`,
    { runScripts: 'outside-only', pretendToBeVisual: false });
  const win = dom.window;
  const doc = win.document;

  let _hidden = hidden;
  Object.defineProperty(doc, 'hidden', {
    configurable: true,
    get: () => _hidden,
  });

  // Frames only exist while visible.
  const frames = [];
  win.requestAnimationFrame = (fn) => {
    if (_hidden) return 0;             // never delivered, exactly as in Chrome
    frames.push(fn);
    return frames.length;
  };
  win.cancelAnimationFrame = (id) => { if (id) frames[id - 1] = null; };

  // Timers: while hidden a browser clamps these hard.  Capture the requested
  // delay so a test can prove the code did not rely on a fast one.
  const timers = [];
  win.setTimeout = (fn, ms) => { timers.push({ fn, ms: ms || 0 }); return timers.length; };
  win.clearTimeout = (id) => { if (id) timers[id - 1] = null; };

  // jsdom does no layout at all: scrollHeight is 0 and a scrollTop write is
  // dropped.  Two things are needed from geometry here, so model just enough of
  // it and no more:
  //
  //   * the *count* of layout reads and scroll writes, since the cost being
  //     avoided while hidden is the forced synchronous layout each one causes;
  //   * a scrollHeight that actually shrinks when a run of tool blocks is
  //     collapsed into one group, because that shrink is the only thing that
  //     opens a collapse gap -- and a gap left open across a hide is one of the
  //     failure modes under test.  With a constant scrollHeight no gap can ever
  //     open and the gap test would pass vacuously.
  //
  // So: every top-level child is ROW_H tall, plus whatever padding-bottom the
  // gap machinery has applied (a real scrollHeight includes it).
  const ROW_H = 20;
  const el = doc.getElementById('messages');
  let layoutReads = 0;
  let scrollWrites = 0;
  Object.defineProperty(el, 'scrollHeight', {
    configurable: true,
    get: () => {
      layoutReads++;
      return ROW_H * el.children.length + (parseFloat(el.style.paddingBottom) || 0);
    },
  });
  Object.defineProperty(el, 'clientHeight', {
    configurable: true, get: () => { layoutReads++; return 500; },
  });
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => { layoutReads++; return 0; },
    set: () => { scrollWrites++; },
  });

  // A message shape chat.js does not recognise is dropped with a warning, and
  // a dropped message appends nothing -- which would let every cap assertion
  // below pass against an empty list.  Turn that into a hard failure so the
  // harness cannot lie about what it exercised.
  win.console = Object.assign(Object.create(console), {
    warn: (...a) => { throw new Error('chat.js warned: ' + a.join(' ')); },
    error: (...a) => { throw new Error('chat.js errored: ' + a.join(' ')); },
  });

  // `const Chat = ...` at top level of an eval does not become a window
  // property, so hand it over explicitly.  (In the browser it is a script-level
  // global and `Chat` resolves directly.)
  win.eval(CHAT_JS + '\n;window.Chat = Chat;');
  win.Chat.init();

  return {
    win, doc, el,
    get Chat() { return win.Chat; },
    get children() { return el.children.length; },
    get layoutReads() { return layoutReads; },
    get scrollWrites() { return scrollWrites; },
    resetCounters() { layoutReads = 0; scrollWrites = 0; },
    /** Deliver every pending frame (only possible while visible). */
    flushFrames() {
      for (let guard = 0; frames.length && guard < 100; guard++) {
        const batch = frames.splice(0, frames.length);
        for (const fn of batch) if (fn) fn(0);
      }
    },
    flushTimers() {
      for (let guard = 0; timers.length && guard < 10000; guard++) {
        const batch = timers.splice(0, timers.length);
        for (const t of batch) if (t) t.fn();
      }
    },
    pendingTimers() { return timers.filter(Boolean).length; },
    timerDelays() { return timers.filter(Boolean).map(t => t.ms); },
    setHidden(v) {
      _hidden = v;
      const ev = doc.createEvent('Event');
      ev.initEvent('visibilitychange', true, true);
      doc.dispatchEvent(ev);
    },
    /** N assistant messages, as the WebSocket would deliver them.
     *
     * `assistant_text` is the wire type -- `_dispatchMessage` warns and drops
     * anything else, which would make every assertion below pass against an
     * empty list. */
    send(n) {
      for (let i = 0; i < n; i++) {
        this.Chat.handleMessage(
          { type: 'assistant_text', content: `line ${i}`, delta: false });
      }
    },
    /** Text of every message element, oldest first. */
    texts() {
      return Array.from(el.children).map(c => c.textContent);
    },
    /** Open a collapse gap, the way the UI does: a run of tool blocks between
     *  two assistant messages, while following the bottom.  _collapseActivity
     *  wraps the run into one group, the scroller shrinks, and the reclaimed
     *  space is held open as padding-bottom.  Keep the list short -- the gap
     *  releases itself once real content would fill the frozen viewport. */
    openGap() {
      const C = this.Chat;
      C.handleMessage({ type: 'assistant_text', content: 'before', delta: false });
      for (const id of ['t1', 't2']) {
        C.handleMessage({
          type: 'tool_use', tool_use_id: id, name: 'Bash',
          status: 'running', input: { command: 'echo hi' },
        });
      }
      C.handleMessage({ type: 'assistant_text', content: 'after', delta: false });
    },
    /** The gap is observable only through the padding it holds open. */
    get gapOpen() { return el.style.paddingBottom !== ''; },
  };
}


// -------------------------------------------------------------------------
// The harness itself
// -------------------------------------------------------------------------

test('messages sent by the harness actually reach the DOM', () => {
  // Everything below asserts an upper bound on the node count, so a harness
  // that silently appended nothing would pass the whole file.  This is the
  // one test that fails if `send` stops working.
  const p = makePage({ hidden: true });
  p.Chat.setMaxDomMessages(0);       // no cap, so the count is exact
  p.send(25);
  assert(p.children === 25, `sent 25 messages, DOM has ${p.children} nodes`);
  assert(p.texts()[7] === 'line 7', `node 7 is ${JSON.stringify(p.texts()[7])}`);
});


// -------------------------------------------------------------------------
// The cap must hold while hidden
// -------------------------------------------------------------------------

test('a hidden window still trims, so the DOM stays bounded', () => {
  const p = makePage({ hidden: true });
  p.Chat.setMaxDomMessages(200);
  p.send(1500);
  assert(p.children <= 200 + 400,
    `DOM grew to ${p.children} nodes while hidden; the cap was 200`);
});

test('the old bug reproduces when frames are the only way to trim', () => {
  // Guards the premise rather than the fix: if a future browser delivered
  // frames to hidden windows this whole mechanism would be moot, and this test
  // is how we would find out instead of carrying a workaround forever.
  const p = makePage({ hidden: true });
  let requested = 0;
  p.win.requestAnimationFrame = () => { requested++; return 0; };  // never delivered
  p.Chat.setMaxDomMessages(100);
  p.send(1000);
  // The premise: chat.js must not have *needed* a frame that cannot arrive.
  assert(p.children <= 500, `trimming needed a frame that never came (${p.children} nodes)`);
  assert(requested === 0,
    `chat.js asked for ${requested} frames while hidden; a hidden window gets none`);
});

test('a visible window trims on a frame, as before', () => {
  const p = makePage({ hidden: false });
  p.Chat.setMaxDomMessages(200);
  p.send(1000);
  // Still deferred to a frame while visible -- batching is the point there.
  p.flushFrames();
  assert(p.children <= 200 + 400,
    `visible trimming regressed (${p.children} nodes)`);
});

test('trimming keeps the newest messages, not the oldest', () => {
  const p = makePage({ hidden: true });
  p.Chat.setMaxDomMessages(100);
  p.send(1000);
  const texts = p.texts();
  assert(texts[texts.length - 1] === 'line 999',
    `last message is ${JSON.stringify(texts[texts.length - 1])}, not the newest`);
  assert(!texts.includes('line 0'), 'the oldest message survived the trim');
  // And the survivors are a contiguous suffix, not an arbitrary subset.
  const first = Number(texts[0].slice(5));
  texts.forEach((t, i) => assert(t === `line ${first + i}`,
    `node ${i} is ${JSON.stringify(t)}, expected line ${first + i}`));
});

test('a trim already waiting on a frame is not stranded by hiding', () => {
  // Found by mutation testing, and it is the original bug by another door: the
  // cap can just as easily be crossed while the window is *visible*, and then
  // `_trimPending` is latched with the work sitting on a frame.  Hide the
  // window before that frame is delivered and it never arrives -- so the latch
  // stays set and the cap stops being enforced for the whole hidden period,
  // which is exactly the state the hidden-path fix was meant to make
  // impossible.
  const p = makePage({ hidden: false });
  p.Chat.setMaxDomMessages(200);
  p.send(300);                 // crosses the cap while visible: trim is queued
  // Deliberately no flushFrames() -- hiding the window is precisely what stops
  // that frame ever being delivered.
  p.setHidden(true);
  p.send(2000);
  assert(p.children <= 200 + 400,
    `DOM at ${p.children} nodes: the latch set while visible was never released`);
});

test('a stale trim frame does not eat messages it was not meant to', () => {
  // The reclaim above runs the trim early and leaves the already-scheduled
  // frame alone rather than cancelling it.  That is only safe because _trimNow
  // re-reads the list and re-checks the cap.  Without that check the late frame
  // computes its removal count from a stale overflow and deletes live messages
  // out of a list that is comfortably under the cap.
  const p = makePage({ hidden: false });
  p.Chat.setMaxDomMessages(800);
  p.send(1100);                 // crosses the cap: a trim is queued on a frame
  p.setHidden(true);            // stranded, so reclaimed and run now
  p.setHidden(false);
  p.send(100);                  // list is well under the cap again
  const before = p.children;
  p.flushFrames();              // the original frame is finally delivered
  assert(p.children === before,
    `the stale frame removed ${before - p.children} messages that were under the cap`);
});

test('the trim latch is released even when it runs synchronously', () => {
  // _trimPending going true and never false is the whole bug; a synchronous
  // path that forgot to clear it would fail exactly the same way.
  const p = makePage({ hidden: true });
  p.Chat.setMaxDomMessages(100);
  p.send(600);
  const after1 = p.children;
  p.send(600);
  assert(p.children <= after1 + 600,
    'the latch stuck: trimming stopped after the first pass');
  assert(p.children <= 500, `DOM at ${p.children} nodes; latch never released`);
});


// -------------------------------------------------------------------------
// Not paying for layout nobody sees
// -------------------------------------------------------------------------

test('a hidden window does not force layout on every message', () => {
  const p = makePage({ hidden: true });
  p.resetCounters();
  p.send(500);
  assert(p.scrollWrites === 0,
    `${p.scrollWrites} scroll writes while hidden; each forces a full layout`);
});

test('a visible window still follows the bottom', () => {
  const p = makePage({ hidden: false });
  p.resetCounters();
  p.send(20);
  assert(p.scrollWrites >= 20,
    `only ${p.scrollWrites} scroll writes while visible; auto-follow broke`);
});

test('becoming visible catches up with one scroll, not one per message', () => {
  const p = makePage({ hidden: true });
  p.send(500);
  p.resetCounters();
  p.setHidden(false);
  assert(p.scrollWrites === 1,
    `expected a single catch-up scroll, got ${p.scrollWrites}`);

  // The catch-up is a latch, and a latch that is set but never cleared is the
  // shape of the bug this whole file is about.  visibilitychange fires for
  // reasons other than a hide/show pair, so a stuck one would scroll -- and
  // force a full layout -- on every single one of them.
  p.resetCounters();
  p.setHidden(false);
  assert(p.scrollWrites === 0,
    `the catch-up latch was never cleared: it fired again (${p.scrollWrites})`);
});

test('a window that was never hidden needs no catch-up', () => {
  const p = makePage({ hidden: false });
  p.send(10);
  p.resetCounters();
  p.setHidden(false);          // spurious event
  assert(p.scrollWrites === 0, 'catch-up fired without anything to catch up on');
});


// -------------------------------------------------------------------------
// The gaps must not wedge the trimmer shut
// -------------------------------------------------------------------------

test('hiding releases the collapse gaps that disable trimming', () => {
  // _maybeTrimOldMessages returns early while a gap is open.  A gap left open
  // across a hide is the unbounded-growth bug again, by another route.
  const p = makePage({ hidden: false });
  p.Chat.setMaxDomMessages(100);
  p.openGap();
  assert(p.gapOpen, 'the harness never opened a gap, so this test proves nothing');

  p.setHidden(true);
  assert(!p.gapOpen, 'the gap survived the hide');

  p.send(2000);
  assert(p.children <= 500,
    `DOM at ${p.children} nodes: a gap kept the trimmer disabled while hidden`);
});


// -------------------------------------------------------------------------
// History replay must not drip-feed a hidden window
// -------------------------------------------------------------------------

function history(n) {
  const out = [];
  for (let i = 0; i < n; i++) out.push({ type: 'assistant', content: `h${i}` });
  return out;
}

test('history replay does not yield per batch while hidden', () => {
  // Batching yields via setTimeout(0), which a hidden window clamps to 1s --
  // 1 minute under intensive throttling.  At 50 messages a batch that is the
  // second way to produce "a few lines per second".
  const p = makePage({ hidden: true });
  p.Chat.setMaxDomMessages(0);              // isolate replay from trimming
  p.Chat.handleMessage({ type: 'history', messages: history(2000) });
  assert(p.pendingTimers() === 0,
    `replay left ${p.pendingTimers()} throttled timers queued while hidden`);
  assert(p.texts().some(t => t === 'h1999'),
    'replay did not finish in one pass while hidden');
});

test('history replay still yields while visible', () => {
  // The batching exists to keep a *watched* window responsive; running a whole
  // session's history in one blocking pass would freeze it.
  const p = makePage({ hidden: false });
  p.Chat.setMaxDomMessages(0);
  p.Chat.handleMessage({ type: 'history', messages: history(2000) });
  assert(p.pendingTimers() > 0, 'replay stopped yielding while visible');
  assert(p.children < 2000, 'the whole history rendered before yielding once');
});

test('replay does not recurse per batch while hidden', () => {
  // The hidden path had to consume every batch in one call, and the obvious
  // way to write that -- call _renderBatch again instead of yielding -- costs
  // a stack frame per batch.  Measure the stack directly rather than trying to
  // overflow it: an enormous history is slow, and the depth at which jsdom
  // gives up is not a number this test should depend on.
  const p = makePage({ hidden: true });
  p.Chat.setMaxDomMessages(0);

  // V8 truncates `.stack` at 10 frames by default, which would report the same
  // depth however deep the recursion actually went -- i.e. a guaranteed pass.
  const savedLimit = Error.stackTraceLimit;
  Error.stackTraceLimit = 4000;

  const depths = [];
  let seen = 0;
  const realAppend = p.el.appendChild.bind(p.el);
  p.el.appendChild = function (node) {
    // Capturing a 4000-frame stack is expensive; one sample per batch is
    // enough to see a per-batch frame being added.
    if (seen++ % 50 === 0) depths.push(new Error().stack.split('\n').length);
    return realAppend(node);
  };

  const BATCHES = 60;                        // BATCH is 50 in chat.js
  try {
    p.Chat.handleMessage({ type: 'history', messages: history(50 * BATCHES) });
  } finally {
    Error.stackTraceLimit = savedLimit;
  }

  assert(depths.length >= BATCHES, `only ${depths.length} samples over ${BATCHES} batches`);
  const first = depths[1], last = depths[depths.length - 1];
  assert(last - first < 10,
    `stack grew ${last - first} frames over ${BATCHES} batches ` +
    `(${first} -> ${last}): the hidden replay path recurses`);
});


console.log(`\n${ran - failures}/${ran} passed`);
process.exit(failures ? 1 : 0);
