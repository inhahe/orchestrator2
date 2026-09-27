/* The tab's icon shows the session's state.
 *
 * Asked 2026-09-27: "Make different browser tab icons for Orchestrator2, same
 * shape as now, but with different lights on it ... one green for being in
 * the working state, one purple for bg-wait, one grey for idle, one yellow or
 * red for not connected - whichever color disconnected shows up as in the
 * text, and one color for waiting to loop, maybe purple for that too since
 * that's the color 'loop' shows up in, but at a different spot from the
 * bg-wait purple?"  Then: "add a yellow light to the browser tab icon for
 * when it's compacting", and "multiple lights should be able to show at once
 * if it's doing two things at once that show lights. for example, compacting
 * during bg-wait."
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
const PLAIN_SVG = fs.readFileSync(path.join(STATIC, 'favicon.svg'), 'utf8');

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
const ROW = { 17: 'top', 32: 'middle', 47: 'bottom' };
const OFF = '#11151d';

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

/* What the icon shows: "top=#..., middle=#..." for the lit LEDs, in rack
 * order, or "" when none is.  Checks every LED is either lit or drawn off. */
function lights(svg) {
  const all = circles(svg);
  const lamps = all.filter((c) => c.fill !== OFF && c.fill !== '#fff' && !c.opacity);
  const off = all.filter((c) => c.fill === OFF).length;
  assert(lamps.length + off === 3, `${lamps.length} lit + ${off} off is not three LEDs`);
  return lamps.map((c) => `${ROW[c.cy]}=${c.fill}`).join(', ');
}

function shown(status, theme) {
  const p = page(theme);
  p.F.update(status);
  assert(p.svg, 'the icon was not replaced: ' + p.link.href);
  return lights(p.svg);
}

function is(status, expected, what, theme) {
  const got = shown(status, theme);
  assert(got === expected, `${what}: expected "${expected}", got "${got}"`);
}

const LOOP = 1790000000;

console.log('\nfavicon\n');

// ---- each state on its own ----------------------------------------------

test('working lights the top LED green', () => {
  is({ busy_class: 'working' }, `top=${GREEN}`, 'working');
});

test('compacting lights the top LED yellow', () => {
  // Yellow is also the status bar's compacting colour.
  is({ busy_class: 'compacting' }, `top=${YELLOW}`, 'compacting');
});

test('bg-wait lights the middle LED purple', () => {
  is({ busy_class: 'bg-wait', bg_count: 2 }, `middle=${PURPLE}`, 'bg-wait');
});

test('waiting to loop lights the bottom LED, in the loop field\'s purple', () => {
  is({ busy_class: 'idle', wakeup_at: LOOP }, `bottom=${PURPLE}`, 'waiting to loop');
});

test('idle lights the top LED grey', () => {
  is({ busy_class: 'idle' }, `top=${GREY}`, 'idle');
});

test('an error lights the top LED red, as its text is', () => {
  is({ busy_class: 'error', busy_label: 'api error' }, `top=${RED}`, 'error');
});

test('a snapshot with no state reads as idle', () => {
  is({}, `top=${GREY}`, 'no busy_class');
});

// ---- two things at once ---------------------------------------------------

test('compacting during bg-wait shows both', () => {
  // The example the request gave.
  is({ busy_class: 'compacting', bg_count: 1 },
     `top=${YELLOW}, middle=${PURPLE}`, 'compacting with bg tasks');
});

test('a turn with background tasks running shows both', () => {
  is({ busy_class: 'working', bg_count: 3 },
     `top=${GREEN}, middle=${PURPLE}`, 'working with bg tasks');
});

test('a turn with a loop scheduled shows both', () => {
  is({ busy_class: 'working', wakeup_at: LOOP },
     `top=${GREEN}, bottom=${PURPLE}`, 'working with a loop');
});

test('bg-wait with a loop scheduled shows both', () => {
  is({ busy_class: 'bg-wait', bg_count: 1, wakeup_at: LOOP },
     `middle=${PURPLE}, bottom=${PURPLE}`, 'bg-wait with a loop');
});

test('all three at once', () => {
  is({ busy_class: 'compacting', bg_count: 1, wakeup_at: LOOP },
     `top=${YELLOW}, middle=${PURPLE}, bottom=${PURPLE}`, 'everything');
});

test('an error with background tasks running shows both', () => {
  is({ busy_class: 'error', bg_count: 1 },
     `top=${RED}, middle=${PURPLE}`, 'error with bg tasks');
});

test('idle\'s grey never sits beside another light', () => {
  // Grey means nothing is happening: with background tasks or a loop, it is
  // not idle, it is waiting on them.
  is({ busy_class: 'idle', wakeup_at: LOOP }, `bottom=${PURPLE}`, 'idle + loop');
  is({ busy_class: 'idle', bg_count: 1 }, `middle=${PURPLE}`, 'idle + bg');
});

test('an older hub that sends no bg_count still shows bg-wait', () => {
  is({ busy_class: 'bg-wait' }, `middle=${PURPLE}`, 'bg-wait without a count');
});

test('no background tasks, no middle light', () => {
  is({ busy_class: 'working', bg_count: 0 }, `top=${GREEN}`, 'bg_count 0');
});

// ---- not connected ---------------------------------------------------------

test('disconnected and reconnecting lights all three yellow, as its text is', () => {
  is({ busy_label: 'disconnected', busy_class: 'reconnecting' },
     `top=${YELLOW}, middle=${YELLOW}, bottom=${YELLOW}`, 'reconnecting');
});

test('and all three red once it has given up, as its text then is', () => {
  is({ busy_label: 'disconnected', busy_class: 'shutdown' },
     `top=${RED}, middle=${RED}, bottom=${RED}`, 'given up');
});

test('connecting is not connected yet: all three yellow', () => {
  is({ busy_class: 'connecting' },
     `top=${YELLOW}, middle=${YELLOW}, bottom=${YELLOW}`, 'connecting');
});

test('nothing else shows while not connected: it is not known', () => {
  is({ busy_class: 'reconnecting', bg_count: 2, wakeup_at: LOOP },
     `top=${YELLOW}, middle=${YELLOW}, bottom=${YELLOW}`, 'stale facts');
});

// ---- the same colour twice is told apart ----------------------------------

test('background tasks and the loop are told apart by position', () => {
  const bg = shown({ busy_class: 'bg-wait', bg_count: 1 });
  const loop = shown({ busy_class: 'idle', wakeup_at: LOOP });
  assert(bg !== loop, `both show "${bg}"`);
});

test('compacting and a dropped connection do not look alike', () => {
  const compacting = shown({ busy_class: 'compacting' });
  const dropped = shown({ busy_class: 'reconnecting' });
  assert(compacting !== dropped, `both show "${compacting}"`);
});

test('an error and a lost connection do not look alike', () => {
  assert(shown({ busy_class: 'error' }) !== shown({ busy_class: 'shutdown' }));
});

// ---- colours come from the theme --------------------------------------

test('the lights are the theme\'s colours, not fixed ones', () => {
  is({ busy_class: 'working' }, 'top=#12ab34', 'themed working',
     { '--indicator-working': '#12ab34' });
  is({ busy_class: 'idle', wakeup_at: 1 }, 'bottom=rgb(200, 100, 250)', 'themed loop',
     { '--indicator-bg-wait': 'rgb(200, 100, 250)' });
  is({ busy_class: 'compacting' }, 'top=#aabb00', 'themed compacting',
     { '--indicator-compacting': '#aabb00' });
});

test('a theme value that is not a colour never reaches the icon', () => {
  const p = page({ '--indicator-working': '"/><script>alert(1)</script><x a="' });
  p.F.update({ busy_class: 'working' });
  assert(!/script/i.test(p.svg), 'the theme value was written into the SVG');
  assert(lights(p.svg) === `top=${GREEN}`, 'did not fall back to the default');
});

test('the icon is a well-formed SVG image', () => {
  const p = page();
  p.F.update({ busy_class: 'compacting', bg_count: 1, wakeup_at: LOOP });
  const doc = new p.win.DOMParser().parseFromString(p.svg, 'image/svg+xml');
  assert(!doc.getElementsByTagName('parsererror').length, 'the SVG does not parse');
  assert(doc.documentElement.nodeName === 'svg', 'not an <svg> document');
});

// ---- when it changes ----------------------------------------------------

test('an unchanged state does not rewrite the icon', () => {
  // Status snapshots arrive every couple of seconds; rewriting the link each
  // time makes some browsers refetch and flicker the tab.
  const p = page();
  p.F.update({ busy_class: 'working', busy_label: 'working (1s)', bg_count: 1 });
  p.F.update({ busy_class: 'working', busy_label: 'working (3s)', bg_count: 2 });
  assert(p.writes === 1, `wrote the icon ${p.writes} times for one set of lights`);
  p.F.update({ busy_class: 'working', bg_count: 0 });
  assert(p.writes === 2, 'a light going out did not change the icon');
});

test('a tab with no session shows the plain icon, and keeps it', () => {
  // A lobby tab gets no status updates, so any light it showed would be one
  // left over from the session it last viewed.
  const p = page();
  p.F.update({ busy_class: 'working' });
  p.F.setLanding(true);
  assert(p.plain, 'landing did not restore the plain icon: ' + p.link.href);
  p.F.update({ busy_class: 'reconnecting' });
  assert(p.plain, 'an update lit a light in a tab with no session');
});

test('and lights up again once it has a session', () => {
  const p = page();
  p.F.setLanding(true);
  p.F.setLanding(false);
  p.F.update({ busy_class: 'bg-wait', bg_count: 1 });
  assert(lights(p.svg) === `middle=${PURPLE}`, 'after attaching: ' + lights(p.svg));
});

test('landing again after a state was shown really restores the icon', () => {
  const p = page();
  p.F.update({ busy_class: 'working' });
  p.F.setLanding(true);
  p.F.setLanding(false);
  p.F.update({ busy_class: 'working' });
  assert(p.svg, 'the same state as before landing was not shown again');
});

// ---- the plain icon ------------------------------------------------------

const norm = (s) => s.replace(/<!--[\s\S]*?-->/g, '').replace(/>\s+</g, '><').trim();

test('the plain icon is this same rack with every light off', () => {
  const F = page().F;
  assert(norm(PLAIN_SVG) === norm(F.svg(null)),
         'static/favicon.svg and favicon.js draw different racks');
  assert(lights(F.svg({})) === '', 'the plain icon has a light on');
});

console.log(`\n${ran - failures}/${ran} passed`);
process.exit(failures ? 1 : 0);
