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

// ---------------------------------------------------------------------------
// What arrives before the history does
//
// Found 2026-09-29.  A large session's history waits behind its CLI's own read
// of the same file, so what the connect says -- that the session's last turn
// was cut off and is waiting -- arrived first, was drawn at once, and ended up
// above the history, scrolled out of sight.  While "loading session..." shows,
// live messages are held and drawn after the history.

function note(text) {
  return { type: 'system_msg', subtype: 'warning', data: { message: text } };
}

test('what arrives while the session loads is drawn after its history', () => {
  const p = makePage();
  p.Chat.handleMessage({ type: 'session_loading', on: true });
  p.Chat.handleMessage(note('last turn was cut off'));
  assert(!p.texts().some(t => t.includes('cut off')),
         'drawn before the history it belongs below');
  p.hist(msgs('h', 3));
  const t = p.texts();
  assert(t[t.length - 1] === 'last turn was cut off',
         'not last, under the history: ' + JSON.stringify(t));
  assert(t.indexOf('h2') < t.indexOf('last turn was cut off'), JSON.stringify(t));
});

test('...and when no history comes, when the loading ends', () => {
  const p = makePage();
  p.Chat.handleMessage({ type: 'session_loading', on: true });
  p.Chat.handleMessage(note('held'));
  p.Chat.handleMessage({ type: 'session_loading', on: false });
  assert(p.texts().includes('held'), JSON.stringify(p.texts()));
});

test('...and when the history is empty', () => {
  const p = makePage();
  p.Chat.handleMessage({ type: 'session_loading', on: true });
  p.Chat.handleMessage(note('held'));
  p.hist([]);
  assert(p.texts().includes('held'), JSON.stringify(p.texts()));
});

test('what was held for a session left behind is not drawn in the next', () => {
  const p = makePage();
  p.Chat.handleMessage({ type: 'session_loading', on: true });
  p.Chat.handleMessage(note('for the old one'));
  p.Chat.handleMessage({ type: 'clear_screen' });
  p.Chat.handleMessage({ type: 'session_loading', on: true });
  p.hist(msgs('new', 2));
  assert(!p.texts().includes('for the old one'), JSON.stringify(p.texts()));
});

test('a replay that ends while the next session loads leaves its messages held', () => {
  // The old session's replay is still drawing in batches when the tab moves
  // on; when it ends, the new session's messages must still wait for theirs.
  const p = makePage();
  p.Chat.handleMessage({ type: 'history', messages: msgs('old', 120) });
  p.Chat.handleMessage({ type: 'clear_screen' });
  p.Chat.handleMessage({ type: 'session_loading', on: true });
  p.Chat.handleMessage(note('for the new one'));
  p.flushTimers();                       // the old replay runs to its end
  assert(!p.texts().includes('for the new one'),
         'drawn before its own history: ' + JSON.stringify(p.texts().slice(-3)));
  p.hist(msgs('new', 2));
  const t = p.texts();
  assert(t[t.length - 1] === 'for the new one', JSON.stringify(t.slice(-3)));
});

test('nothing is held when no session is loading', () => {
  const p = makePage();
  p.Chat.handleMessage(note('right away'));
  assert(p.texts().includes('right away'), JSON.stringify(p.texts()));
});

// ---------------------------------------------------------------------------
// Following through a burst, and a catch-up drawn in one go (2026-10-10)
//
// Coming back to a tab Chrome had frozen "replays the recent history fairly
// slowly".  The backlog came a message at a time with a repaint between, and
// a view following the bottom stopped following partway: the scroll check
// read what a burst had added below as the user scrolling away.

function say(text) {
  return { type: 'assistant_text', content: text, delta: false };
}

function note(text) {
  return { type: 'system_msg', subtype: 'info', data: { message: text } };
}

function atTheBottom(p) {
  return p.scrollTop >= p.el.scrollHeight - p.el.clientHeight;
}

test('a burst does not stop a following view from following', () => {
  const p = makePage();
  p.hist(msgs('h', 20));
  p.flushFrames();
  // Messages land between one frame's scroll and the next...
  for (let i = 0; i < 10; i++) p.Chat.handleMessage(say('b' + i));
  // ...and the scroll check runs on the scroll our own write made.
  p.el.dispatchEvent(new p.win.Event('scroll'));
  p.flushTimers();
  p.flushFrames();
  assert(atTheBottom(p),
         `left ${p.el.scrollHeight - p.el.clientHeight - p.scrollTop}px above the bottom`);
});

test('a view moved up stops following, though nothing said why', () => {
  // Find in page, a link to an anchor: no wheel and no key, the view moves.
  const p = makePage();
  p.hist(msgs('h', 20));
  p.flushFrames();
  p.scrollTop = p.scrollTop - 300;
  p.el.dispatchEvent(new p.win.Event('scroll'));
  p.flushTimers();
  p.Chat.handleMessage(say('new'));
  p.flushFrames();
  assert(!atTheBottom(p), 'pulled back down to the bottom');
});

test('trimming what is above a following view does not stop it following', () => {
  // Removing it lowers scrollTop by as much, the browser keeping what is on
  // screen in place.  Modelled here: the harness has no layout to do it.
  const p = makePage();
  const deleteContents = p.win.Range.prototype.deleteContents;
  p.win.Range.prototype.deleteContents = function () {
    const before = p.el.scrollHeight;
    deleteContents.call(this);
    p.scrollTop = p.scrollTop - (before - p.el.scrollHeight);
  };
  p.Chat.setMaxDomMessages(10);
  p.hist(msgs('h', 5));
  p.flushFrames();
  for (let i = 0; i < 20; i++) p.Chat.handleMessage(say('b' + i));
  p.flushFrames();                          // follows, then trims
  for (let i = 0; i < 10; i++) p.Chat.handleMessage(say('c' + i));
  p.el.dispatchEvent(new p.win.Event('scroll'));
  p.flushTimers();
  p.flushFrames();
  assert(atTheBottom(p),
         `left ${p.el.scrollHeight - p.el.clientHeight - p.scrollTop}px above the bottom`);
});

test('a catch-up is held, then drawn in one go and in order', () => {
  const p = makePage();
  p.hist(msgs('h', 5));
  p.flushFrames();
  const before = p.children;
  p.Chat.beginCatchUp();
  for (let i = 0; i < 6; i++) p.Chat.handleMessage(say('c' + i));
  assert(p.children === before, 'drawn while held');
  p.Chat.endCatchUp();
  const t = p.texts();
  const at = [0, 1, 2, 3, 4, 5].map(i => t.findIndex(x => x.includes('c' + i)));
  assert(at.every((x, i) => x >= 0 && (i === 0 || x > at[i - 1])), JSON.stringify(at));
});

test('...and stays held whatever else draws what is waiting', () => {
  const p = makePage();
  p.Chat.beginCatchUp();
  p.Chat.handleMessage(say('held'));
  p.Chat.handleMessage({ type: 'session_loading', on: false });   // draws what waits
  assert(!p.texts().some(x => x.includes('held')), 'drawn before the catch-up ended');
  p.Chat.endCatchUp();
  assert(p.texts().some(x => x.includes('held')), 'never drawn');
});

test('...and one message that fails to draw does not lose the rest', () => {
  // Drawn one per task, each failed alone.  Drawn together, the same.
  const p = makePage();
  p.win.App = { openModal() { throw new Error('a renderer bug'); } };
  const reported = [];
  p.win.console.error = (...a) => reported.push(a.join(' '));
  p.Chat.beginCatchUp();
  p.Chat.handleMessage({ type: 'modal', title: 'x', content: 'y' });
  p.Chat.handleMessage(say('after it'));
  p.Chat.endCatchUp();
  assert(p.texts().some(x => x.includes('after it')), JSON.stringify(p.texts()));
  assert(reported.length === 1, `reported ${reported.length} times`);
});

test('...and followed to the bottom at once, not a frame later', () => {
  // Collapsing the runs a catch-up ends can move the view up.  A scroll check
  // due before the next frame would take that for the user leaving.
  const p = makePage();
  p.hist(msgs('h', 5));
  p.flushFrames();
  p.Chat.beginCatchUp();
  for (let i = 0; i < 6; i++) p.Chat.handleMessage(say('c' + i));
  p.Chat.endCatchUp();
  assert(atTheBottom(p),
         `${p.el.scrollHeight - p.el.clientHeight - p.scrollTop}px above the bottom`);
});

test('a catch-up is drawn without reading the layout once per message', () => {
  // Each read forces a layout of the whole list, and collapsing read it
  // several times over for every run of activity a message ended.
  const p = makePage();
  p.hist(msgs('h', 5));
  p.flushFrames();
  let reads = 0;
  for (const name of ['scrollHeight', 'scrollTop']) {
    const own = Object.getOwnPropertyDescriptor(p.el, name);
    Object.defineProperty(p.el, name, { configurable: true,
      get() { reads++; return own.get.call(this); }, set: own.set });
  }
  const rect = p.win.Element.prototype.getBoundingClientRect;
  p.win.Element.prototype.getBoundingClientRect = function () {
    reads++;
    return rect.call(this);
  };
  p.Chat.beginCatchUp();
  for (let i = 0; i < 10; i++) {
    p.Chat.handleMessage(note('s' + i));
    p.Chat.handleMessage(note('t' + i));
    p.Chat.handleMessage(say('a' + i));     // collapses the two before it
  }
  p.Chat.endCatchUp();
  // Two are the scroll to the bottom at the end.
  assert(reads <= 2, reads + ' layout reads drawing 30 messages');
});

test('clearing the screen drops a held catch-up', () => {
  const p = makePage();
  p.Chat.beginCatchUp();
  p.Chat.handleMessage(say('for the old view'));
  p.Chat.handleMessage({ type: 'clear_screen' });
  p.Chat.endCatchUp();
  p.hist(msgs('new', 2));
  assert(!p.texts().some(x => x.includes('for the old view')), JSON.stringify(p.texts()));
});

test('...and what comes after the clear is not held for the view it wiped', () => {
  const p = makePage();
  p.hist(msgs('h', 2));
  p.Chat.beginCatchUp();
  p.Chat.handleMessage({ type: 'clear_screen' });
  p.Chat.handleMessage(say('after the clear'));
  assert(p.texts().some(x => x.includes('after the clear')), JSON.stringify(p.texts()));
});

// Layout, as far as fitting a thinking summary needs it: 7 px a character,
// *available* px of room, and nothing measurable inside a collapsed group.
function measuring(p, available) {
  const proto = p.win.HTMLElement.prototype;
  Object.defineProperty(proto, 'offsetParent', { configurable: true,
    get() { return this.isConnected && !this.closest('.collapsed') ? p.doc.body : null; } });
  Object.defineProperty(proto, 'scrollWidth', { configurable: true,
    get() { return (this.textContent || '').length * 7; } });
  Object.defineProperty(proto, 'clientWidth', { configurable: true,
    get() { return available; } });
}

function summaries(p) {
  return Array.from(p.doc.querySelectorAll('.thinking-summary')).map(e => e.textContent);
}

test('a catch-up fits its thinking summaries once it is in', () => {
  const p = makePage();
  measuring(p, 140);                        // 20 characters fit
  p.Chat.beginCatchUp();
  p.Chat.handleMessage({ type: 'thinking', content: 'short and sweet' });
  p.Chat.handleMessage({ type: 'thinking', content: 'x'.repeat(60) });
  p.Chat.handleMessage({ type: 'thinking', content: 'two\nlines' });
  p.Chat.endCatchUp();
  const s = summaries(p);
  assert(s[0] === 'short and sweet' && s[1] === '(60 chars)' && s[2] === '(9 chars, 2 lines)',
         JSON.stringify(s));
});

// For each measurement a summary takes, how many thinking blocks were drawn
// by then.  One at a time, each forces a layout of the whole list.
function measurements(p) {
  const drawn = [];
  const proto = p.win.HTMLElement.prototype;
  for (const name of ['offsetParent', 'scrollWidth']) {
    const was = Object.getOwnPropertyDescriptor(proto, name);
    Object.defineProperty(proto, name, { configurable: true, get() {
      drawn.push(p.doc.querySelectorAll('.msg-thinking').length);
      return was.get.call(this);
    } });
  }
  return drawn;
}

const THREE_THOUGHTS = [{ type: 'thinking', content: 'one line' },
                        { type: 'thinking', content: 'two\nlines' },
                        { type: 'thinking', content: 'one more' }];

test('...measuring them together, once the last of them is drawn', () => {
  const p = makePage();
  measuring(p, 140);
  const drawn = measurements(p);
  p.Chat.beginCatchUp();
  THREE_THOUGHTS.forEach(m => p.Chat.handleMessage(m));
  p.Chat.endCatchUp();
  assert(drawn.length && drawn.every(n => n === 3),
         'measured with ' + JSON.stringify(drawn) + ' of 3 drawn');
});

test('...and one a collapsed group hid, when the group is opened', () => {
  const p = makePage();
  measuring(p, 140);
  p.Chat.beginCatchUp();
  p.Chat.handleMessage({ type: 'thinking', content: 'short and sweet' });
  p.Chat.handleMessage({ type: 'thinking', content: 'also short' });
  p.Chat.handleMessage(say('done'));        // collapses the two above
  p.Chat.endCatchUp();
  assert(summaries(p)[0] === '(15 chars)', 'fitted while hidden: ' + summaries(p)[0]);
  p.win.Element.prototype.scrollIntoView = () => {};   // jsdom has none
  p.doc.querySelector('.activity-group-toggle').click();
  assert(summaries(p)[0] === 'short and sweet', summaries(p)[0]);
});

test('a history fits its thinking summaries once it is drawn', () => {
  const p = makePage();
  measuring(p, 140);
  p.hist([{ type: 'thinking', content: 'short and sweet' },
          { type: 'thinking', content: 'x'.repeat(60) }]);
  const s = summaries(p);
  assert(s[0] === 'short and sweet' && s[1] === '(60 chars)', JSON.stringify(s));
});

test('...measuring them together too', () => {
  const p = makePage();
  measuring(p, 140);
  const drawn = measurements(p);
  p.hist(THREE_THOUGHTS);
  assert(drawn.length && drawn.every(n => n === 3),
         'measured with ' + JSON.stringify(drawn) + ' of 3 drawn');
});

console.log(`\n${ran - failures}/${ran} passed`);
process.exit(failures ? 1 : 0);
