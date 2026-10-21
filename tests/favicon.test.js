/* The tab's icon shows the session's state, as one dot.
 *
 * The icon is a dot filling it, in the colour the status bar writes the
 * state in, so the state reads from the tab strip; when several states hold
 * at once, the first of red, yellow, green, purple, grey wins.  Asked
 * 2026-10-21, after an icon of three small LEDs proved hard to read: "change
 * them to a dot showing red, green or yellow. (green takes precedence over
 * purple.)"
 *
 * These drive static/favicon.js on its own.  That the page wires it up -- the
 * status bar feeding it, the lobby switching it off -- is checked in
 * reconnect_on_show.test.js, against the real page.
 */

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const STATIC = path.join(__dirname, '..', 'static');
const FAVICON_JS = fs.readFileSync(path.join(STATIC, 'favicon.js'), 'utf8');

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

// The default theme, which is what the module falls back to.
const GREEN = '#0dbc79', PURPLE = '#bc3fbc', GREY = '#666666';
const YELLOW = '#e5e510', RED = '#cd3131';

/* A page holding the icon link.  *theme* is what the page's CSS variables
 * resolve to, as getComputedStyle reports them in a browser. */
function page(theme) {
  const dom = new JSDOM(
    '<!doctype html><html><head><link rel="icon" id="favicon" '
    + 'type="image/svg+xml" href="/static/favicon.svg"></head><body></body></html>',
    { url: 'http://localhost:8420/', runScripts: 'outside-only' });
  const win = dom.window;
  const vars = theme || {};
  win.getComputedStyle = () => ({ getPropertyValue: (n) => vars[n] || '' });
  const link = win.document.getElementById('favicon');
  let writes = 0;
  const href = Object.getOwnPropertyDescriptor(win.HTMLLinkElement.prototype, 'href');
  Object.defineProperty(link, 'href', {
    get() { return href.get.call(this); },
    set(v) { writes++; href.set.call(this, v); },
  });
  win.eval(FAVICON_JS + '\n;globalThis.__Favicon = Favicon;');
  return {
    win, link, F: win.__Favicon,
    get writes() { return writes; },
    get plain() { return link.href.endsWith('/static/favicon.svg'); },
    get svg() { return svgOf(link.href); },
  };
}

function svgOf(href) {
  if (!href.startsWith('data:image/svg+xml,')) return null;
  return decodeURIComponent(href.slice('data:image/svg+xml,'.length));
}

function circles(svg) {
  return [...svg.matchAll(/<circle ([^>]*)\/>/g)].map((m) => {
    const a = {};
    for (const [, k, v] of m[1].matchAll(/([\w-]+)="([^"]*)"/g)) a[k] = v;
    return a;
  });
}

/* The dot's colour.  Checks the icon is that one dot and nothing else. */
function dot(svg) {
  const all = circles(svg);
  assert(all.length === 1, `${all.length} shapes, not one dot`);
  return all[0].fill;
}

function shown(status, theme) {
  const p = page(theme);
  p.F.update(status);
  assert(p.svg, 'the icon was not replaced: ' + p.link.href);
  return dot(p.svg);
}

function is(status, expected, what, theme) {
  const got = shown(status, theme);
  assert(got === expected, `${what}: expected ${expected}, got ${got}`);
}

const LOOP = 1790000000;

console.log('\nfavicon\n');

// ---- each state on its own ----------------------------------------------

test('working is green', () => {
  is({ busy_class: 'working' }, GREEN, 'working');
});

test('compacting is yellow, as its text is', () => {
  is({ busy_class: 'compacting' }, YELLOW, 'compacting');
});

test('an error is red, as its text is', () => {
  is({ busy_class: 'error', busy_label: 'api error' }, RED, 'error');
});

test('bg-wait is purple', () => {
  is({ busy_class: 'bg-wait', bg_count: 2 }, PURPLE, 'bg-wait');
});

test('waiting to loop is purple, the loop field\'s colour', () => {
  is({ busy_class: 'idle', wakeup_at: LOOP }, PURPLE, 'waiting to loop');
});

test('idle is grey', () => {
  is({ busy_class: 'idle' }, GREY, 'idle');
});

test('a snapshot with no state reads as idle', () => {
  is({}, GREY, 'no busy_class');
});

// ---- several at once: red, yellow, green, purple, grey ---------------------

test('green takes precedence over purple', () => {
  // The rule as asked.
  is({ busy_class: 'working', bg_count: 3 }, GREEN, 'working with bg tasks');
  is({ busy_class: 'working', wakeup_at: LOOP }, GREEN, 'working with a loop');
});

test('compacting during bg-wait is yellow', () => {
  is({ busy_class: 'compacting', bg_count: 1, wakeup_at: LOOP }, YELLOW,
     'compacting with bg tasks and a loop');
});

test('an error with background tasks running is red', () => {
  is({ busy_class: 'error', bg_count: 1 }, RED, 'error with bg tasks');
});

test('idle with background tasks or a loop is not idle: purple', () => {
  is({ busy_class: 'idle', bg_count: 1 }, PURPLE, 'idle + bg');
  is({ busy_class: 'bg-wait', bg_count: 1, wakeup_at: LOOP }, PURPLE, 'bg-wait + loop');
});

test('an older hub that sends no bg_count still shows bg-wait', () => {
  is({ busy_class: 'bg-wait' }, PURPLE, 'bg-wait without a count');
});

test('no background tasks: a turn is just green', () => {
  is({ busy_class: 'working', bg_count: 0 }, GREEN, 'bg_count 0');
});

// ---- not connected ---------------------------------------------------------

test('disconnected and reconnecting is yellow, as its text is', () => {
  is({ busy_label: 'disconnected', busy_class: 'reconnecting' }, YELLOW, 'reconnecting');
});

test('connecting is yellow', () => {
  is({ busy_class: 'connecting' }, YELLOW, 'connecting');
});

test('and red once it has given up, as its text then is', () => {
  is({ busy_label: 'disconnected', busy_class: 'shutdown' }, RED, 'given up');
});

test('nothing else shows while not connected: it is not known', () => {
  is({ busy_class: 'reconnecting', bg_count: 2, wakeup_at: LOOP }, YELLOW, 'stale facts');
});

// ---- the dot ---------------------------------------------------------------

test('the dot fills the icon', () => {
  // Three 16-pixel LEDs could not be read from the tab strip.
  const p = page();
  p.F.update({ busy_class: 'working' });
  const c = circles(p.svg)[0];
  assert(Number(c.r) >= 26 && c.cx === '32' && c.cy === '32',
         `a dot of radius ${c.r} at ${c.cx},${c.cy} in a 64-unit icon`);
});

test('the dot has a dark rim, for a pale colour on a pale tab strip', () => {
  const p = page();
  p.F.update({ busy_class: 'compacting' });
  const c = circles(p.svg)[0];
  assert(c.stroke === '#000' && Number(c['stroke-width']) > 0, JSON.stringify(c));
});

// ---- colours come from the theme --------------------------------------

test('the dot is the theme\'s colour, not a fixed one', () => {
  is({ busy_class: 'working' }, '#12ab34', 'themed working',
     { '--indicator-working': '#12ab34' });
  is({ busy_class: 'idle', wakeup_at: 1 }, 'rgb(200, 100, 250)', 'themed loop',
     { '--indicator-bg-wait': 'rgb(200, 100, 250)' });
  is({ busy_class: 'compacting' }, '#aabb00', 'themed compacting',
     { '--indicator-compacting': '#aabb00' });
});

test('a theme value that is not a colour never reaches the icon', () => {
  const p = page({ '--indicator-working': '"/><script>alert(1)</script><x a="' });
  p.F.update({ busy_class: 'working' });
  assert(!/script/i.test(p.svg), 'the theme value was written into the SVG');
  assert(dot(p.svg) === GREEN, 'did not fall back to the default');
});

test('the icon is a well-formed SVG image', () => {
  const p = page();
  p.F.update({ busy_class: 'compacting', bg_count: 1, wakeup_at: LOOP });
  const doc = new p.win.DOMParser().parseFromString(p.svg, 'image/svg+xml');
  assert(!doc.getElementsByTagName('parsererror').length, 'the SVG does not parse');
  assert(doc.documentElement.nodeName === 'svg', 'not an <svg> document');
});

// ---- when it changes ----------------------------------------------------

test('an unchanged colour does not rewrite the icon', () => {
  // Status snapshots arrive every couple of seconds; rewriting the link each
  // time makes some browsers refetch and flicker the tab.
  const p = page();
  p.F.update({ busy_class: 'working', busy_label: 'working (1s)', bg_count: 1 });
  p.F.update({ busy_class: 'working', busy_label: 'working (3s)', bg_count: 0 });
  assert(p.writes === 1, `wrote the icon ${p.writes} times for one colour`);
  p.F.update({ busy_class: 'bg-wait', bg_count: 1 });
  assert(p.writes === 2, 'a new colour did not change the icon');
});

test('a tab with no session shows the app\'s icon, and keeps it', () => {
  // A lobby tab gets no status updates, so any dot it showed would be one
  // left over from the session it last viewed.
  const p = page();
  p.F.update({ busy_class: 'working' });
  p.F.setLanding(true);
  assert(p.plain, 'landing did not restore the app\'s icon: ' + p.link.href);
  p.F.update({ busy_class: 'reconnecting' });
  assert(p.plain, 'an update showed a dot in a tab with no session');
});

test('and shows a dot again once it has a session', () => {
  const p = page();
  p.F.setLanding(true);
  p.F.setLanding(false);
  p.F.update({ busy_class: 'bg-wait', bg_count: 1 });
  assert(dot(p.svg) === PURPLE, 'after attaching: ' + dot(p.svg));
});

test('landing again after a state was shown really restores the dot', () => {
  const p = page();
  p.F.update({ busy_class: 'working' });
  p.F.setLanding(true);
  p.F.setLanding(false);
  p.F.update({ busy_class: 'working' });
  assert(p.svg, 'the same state as before landing was not shown again');
});

console.log(`\n${ran - failures}/${ran} passed`);
process.exit(failures ? 1 : 0);
