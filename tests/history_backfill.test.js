/* Opening a session shows the newest messages first, then backfills.
 *
 * Asked 2026-09-06: "when does that 41 s read happen? is that why it takes so
 * long between launching a session and the tab opening?"
 *
 * Measured answer: the tab's shell appears in ~5 s, but the transcript stayed
 * blank for another ~42 s.  Three separate causes, fixed together:
 *
 *   1. `_tail_read_jsonl` derived its average line size *from the file size*,
 *      which made the "tail" come out at exactly half the file every time --
 *      724 MB of a 1.4 GB transcript.  Now capped at MAX_TAIL_BYTES.
 *   2. the history read ran concurrently with the CLI's own resume read of the
 *      same file; on a three-session launch that was six readers moving ~4 GB.
 *      Now it takes a turn in the same queue.
 *   3. reading is only half the wait -- building a thousand-odd DOM nodes
 *      takes real time too, and the user stares at an empty chat throughout.
 *
 * These tests cover (3): the newest slice is rendered on its own, and the older
 * messages are inserted *above* it without moving what the user is reading.
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
  const doc = win.document;

  Object.defineProperty(doc, 'hidden', { configurable: true, get: () => false });

  const frames = [];
  win.requestAnimationFrame = (fn) => { frames.push(fn); return frames.length; };
  win.cancelAnimationFrame = (id) => { if (id) frames[id - 1] = null; };
  const timers = [];
  win.setTimeout = (fn, ms) => { timers.push({ fn, ms: ms || 0 }); return timers.length; };
  win.clearTimeout = (id) => { if (id) timers[id - 1] = null; };

  // Enough geometry to make "did the viewport move?" a real question:
  // every top-level child is ROW_H tall, and scrollTop is a stored value.
  const ROW_H = 20;
  const el = doc.getElementById('messages');
  let scrollTop = 0;
  let scrollWrites = 0;
  Object.defineProperty(el, 'scrollHeight', {
    configurable: true, get: () => ROW_H * el.children.length,
  });
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => 100 });
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => scrollTop,
    set: (v) => { scrollTop = v; scrollWrites++; },
  });

  win.console = Object.assign(Object.create(console), {
    warn: (...a) => { throw new Error('chat.js warned: ' + a.join(' ')); },
    error: (...a) => { throw new Error('chat.js errored: ' + a.join(' ')); },
  });

  win.eval(CHAT_JS + '\n;window.Chat = Chat;');
  win.Chat.init();

  return {
    win, doc, el,
    get Chat() { return win.Chat; },
    get children() { return el.children.length; },
    get scrollTop() { return scrollTop; },
    set scrollTop(v) { scrollTop = v; },
    get scrollWrites() { return scrollWrites; },
    resetScrollWrites() { scrollWrites = 0; },
    /** Leave the bottom for real: _autoScroll is set by a *debounced* scroll
     *  listener, so setting scrollTop alone leaves the view still "pinned" and
     *  every re-anchoring assertion passes vacuously. */
    scrollUp() {
      scrollTop = 0;
      const ev = doc.createEvent('Event');
      ev.initEvent('scroll', true, true);
      el.dispatchEvent(ev);
      this.flushTimers();
    },
    texts() {
      return Array.from(el.children).map(c => c.textContent);
    },
    flushTimers() {
      for (let guard = 0; timers.length && guard < 10000; guard++) {
        const batch = timers.splice(0, timers.length);
        for (const t of batch) if (t) t.fn();
      }
    },
    flushFrames() {
      for (let guard = 0; frames.length && guard < 100; guard++) {
        const batch = frames.splice(0, frames.length);
        for (const fn of batch) if (fn) fn(0);
      }
    },
    hist(msgs, moreAbove) {
      this.Chat.handleMessage({ type: 'history', messages: msgs,
                               more_above: moreAbove });
      this.flushTimers();
    },
    backfill(msgs) {
      this.Chat.handleMessage({ type: 'history_prepend', messages: msgs });
      this.flushTimers();
    },
  };
}

function msgs(prefix, n) {
  const out = [];
  for (let i = 0; i < n; i++) out.push({ type: 'assistant', content: `${prefix}${i}` });
  return out;
}

// ---------------------------------------------------------------------------

test('the first frame renders without waiting for the older messages', () => {
  const p = makePage();
  p.hist(msgs('new', 5), 100);
  const text = p.texts().join('|');
  assert(text.includes('new0') && text.includes('new4'),
         'the newest slice did not render on its own');
});

test('older messages land above the newer ones', () => {
  const p = makePage();
  p.hist(msgs('new', 3), 4);
  p.backfill(msgs('old', 4));
  const text = p.texts().join('|');
  const firstOld = text.indexOf('old0');
  const firstNew = text.indexOf('new0');
  assert(firstOld !== -1, 'the backfill rendered nothing');
  assert(firstOld < firstNew,
         'older history was appended below the newer messages');
});

test('every backfilled message is rendered', () => {
  const p = makePage();
  p.hist(msgs('new', 3), 10);
  p.backfill(msgs('old', 10));
  const text = p.texts().join('|');
  for (let i = 0; i < 10; i++) {
    assert(text.includes(`old${i}`), `old${i} missing from the backfill`);
  }
});

test('the "session history" separator is withheld until the top arrives', () => {
  const p = makePage();
  p.hist(msgs('new', 3), 5);
  assert(!p.texts().join('|').includes('Session history'),
         'labelled the top of the transcript while more was still to come');
  p.backfill(msgs('old', 5));
  assert(p.texts().join('|').includes('Session history'),
         'the separator never arrived with the backfill');
});

test('with nothing above, the separator comes with the first frame', () => {
  const p = makePage();
  p.hist(msgs('only', 3), 0);
  assert(p.texts().join('|').includes('Session history'),
         'a complete history lost its separator');
});

test('the separator reports the whole history, not just the backfill', () => {
  const p = makePage();
  p.hist(msgs('new', 3), 7);
  p.backfill(msgs('old', 7));
  const sep = p.texts().find(t => t.includes('Session history'));
  assert(/10 messages/.test(sep), `separator said: ${sep}`);
});

test('a reader partway up is not scrolled away by the backfill', () => {
  const p = makePage();
  p.hist(msgs('new', 12), 20);
  p.flushFrames();
  p.scrollUp();                       // really unpinned, not just scrollTop=0
  const before = p.scrollTop;
  const heightBefore = p.el.scrollHeight;
  p.backfill(msgs('old', 20));
  const grew = p.el.scrollHeight - heightBefore;
  assert(grew > 0, 'the backfill added no height, so this proves nothing');
  assert(p.scrollTop === before + grew,
         `viewport not re-anchored: scrollTop ${before} -> ${p.scrollTop}, ` +
         `content grew by ${grew}`);
});

test('a reader pinned to the bottom stays at the bottom', () => {
  const p = makePage();
  p.hist(msgs('new', 5), 10);
  p.backfill(msgs('old', 10));
  p.flushFrames();
  // Still pinned: scrollTop should track the (now taller) content.
  assert(p.scrollTop >= p.el.scrollHeight - p.el.clientHeight - 80,
         'a pinned reader was left stranded partway up after the backfill');
});

test('the backfill does not scroll once per message', () => {
  // _prependHistory holds _replayInProgress, which is what stops the per
  // message scroll-pinning from firing for every node it renders. Without it
  // a thousand-message backfill does a thousand forced layouts -- the exact
  // cost this whole change exists to remove.
  const p = makePage();
  p.hist(msgs('new', 3), 40);
  p.resetScrollWrites();
  p.backfill(msgs('old', 40));
  assert(p.scrollWrites < 10,
         `backfilling 40 messages caused ${p.scrollWrites} scroll writes`);
});

test('a backfill arriving mid-replay is not queued away', () => {
  // The server sends history and history_prepend back to back, so the second
  // routinely lands while the first is still rendering in batches. If the
  // dispatcher treats it as a live message it goes into _pendingMessages and
  // is replayed *after* the tail -- i.e. appended below, in the wrong order.
  const p = makePage();
  p.Chat.handleMessage({ type: 'history', messages: msgs('new', 300),
                         more_above: 5 });
  // deliberately NOT flushing: the batched replay is still in flight
  p.Chat.handleMessage({ type: 'history_prepend', messages: msgs('old', 5) });
  p.flushTimers();
  const text = p.texts().join('|');
  assert(text.indexOf('old0') !== -1, 'the mid-replay backfill was lost');
  assert(text.indexOf('old0') < text.indexOf('new0'),
         'the mid-replay backfill was appended below instead of prepended');
});

test('an empty backfill changes nothing', () => {
  const p = makePage();
  p.hist(msgs('new', 3), 0);
  const before = p.texts().join('|');
  p.backfill([]);
  assert(p.texts().join('|') === before, 'an empty backfill altered the DOM');
});

test('live messages arriving during a backfill are not lost', () => {
  const p = makePage();
  p.hist(msgs('new', 3), 5);
  p.backfill(msgs('old', 5));
  p.Chat.handleMessage({ type: 'assistant_text', content: 'live one', delta: false });
  p.flushTimers();
  assert(p.texts().join('|').includes('live one'),
         'a message delivered around the backfill vanished');
});

test('the backfill renders into the real list, not a detached node', () => {
  // The implementation points elMessages at a scratch container while
  // rendering; if the nodes were not moved back, the count would not grow.
  const p = makePage();
  p.hist(msgs('new', 2), 6);
  const before = p.children;
  p.backfill(msgs('old', 6));
  assert(p.children > before,
         'the backfilled nodes never made it into the visible list');
});

console.log(`\n${ran - failures}/${ran} passed`);
process.exit(failures ? 1 : 0);
