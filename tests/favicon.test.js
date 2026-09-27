/* The tab's icon shows the session's state.
 *
 * Asked 2026-09-27: "Make different browser tab icons for Orchestrator2, same
 * shape as now, but with different lights on it ... one green for being in
 * the working state, one purple for bg-wait, one grey for idle, one yellow or
 * red for not connected - whichever color disconnected shows up as in the
 * text, and one color for waiting to loop, maybe purple for that too since
 * that's the color 'loop' shows up in, but at a different spot from the
 * bg-wait purple?"
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

/* The lit LED: {row, colour} -- and how many are off. */
function lights(svg) {
  const all = circles(svg);
  const lamps = all.filter((c) => c.fill !== OFF && c.fill !== '#fff' && !c.opacity);
  assert(lamps.length <= 1, `${lamps.length} lights on at once`);
  return {
    lit: lamps.length ? { row: ROW[lamps[0].cy], colour: lamps[0].fill } : null,
    off: all.filter((c) => c.fill === OFF).length,
  };
}

function shown(status, theme) {
  const p = page(theme);
  p.F.update(status);
  assert(p.svg, 'the icon was not replaced: ' + p.link.href);
  return lights(p.svg).lit;
}

function is(lit, row, colour, what) {
  assert(lit && lit.row === row && lit.colour === colour,
         `${what}: expected the ${row} light ${colour}, got `
         + (lit ? `the ${lit.row} light ${lit.colour}` : 'no light'));
}

console.log('\nfavicon\n');

// ---- each state ----------------------------------------------------------

test('working lights the top LED green', () => {
  is(shown({ busy_class: 'working' }), 'top', GREEN, 'working');
});

test('compacting is working: it happens inside a turn', () => {
  is(shown({ busy_class: 'compacting' }), 'top', GREEN, 'compacting');
});

test('bg-wait lights the middle LED purple', () => {
  is(shown({ busy_class: 'bg-wait' }), 'middle', PURPLE, 'bg-wait');
});

test('waiting to loop lights the bottom LED, in the loop field\'s purple', () => {
  is(shown({ busy_class: 'idle', wakeup_at: 1790000000 }), 'bottom', PURPLE,
     'waiting to loop');
});

test('idle lights the top LED grey', () => {
  is(shown({ busy_class: 'idle' }), 'top', GREY, 'idle');
});

test('disconnected is yellow while it reconnects, as its text is', () => {
  is(shown({ busy_label: 'disconnected', busy_class: 'reconnecting' }),
     'top', YELLOW, 'reconnecting');
});

test('and red once it has given up, as its text then is', () => {
  is(shown({ busy_label: 'disconnected', busy_class: 'shutdown' }),
     'top', RED, 'given up');
});

test('connecting is yellow, as its text is', () => {
  is(shown({ busy_class: 'connecting' }), 'top', YELLOW, 'connecting');
});

test('an error is red, as its text is', () => {
  is(shown({ busy_class: 'error', busy_label: 'api error' }), 'top', RED, 'error');
});

test('a snapshot with no state reads as idle', () => {
  is(shown({}), 'top', GREY, 'no busy_class');
});

// ---- the two purples, and what outranks the loop ----------------------

test('bg-wait and the loop are told apart by position', () => {
  const bg = shown({ busy_class: 'bg-wait' });
  const loop = shown({ busy_class: 'idle', wakeup_at: 1790000000 });
  assert(bg.colour === loop.colour, 'the same purple, as the status bar has it');
  assert(bg.row !== loop.row, `both lit the ${bg.row} LED`);
});

test('a loop scheduled during a turn still shows working', () => {
  is(shown({ busy_class: 'working', wakeup_at: 1790000000 }), 'top', GREEN,
     'working with a loop armed');
});

test('a loop scheduled during bg-wait still shows bg-wait', () => {
  is(shown({ busy_class: 'bg-wait', wakeup_at: 1790000000 }), 'middle', PURPLE,
     'bg-wait with a loop armed');
});

test('a dropped connection outranks a scheduled loop', () => {
  is(shown({ busy_class: 'reconnecting', wakeup_at: 1790000000 }), 'top', YELLOW,
     'reconnecting with a loop armed');
});

test('one light is on and the other two are off, in every state', () => {
  for (const cls of ['working', 'bg-wait', 'idle', 'reconnecting', 'shutdown']) {
    for (const loop of [false, true]) {
      const p = page();
      p.F.update(loop ? { busy_class: cls, wakeup_at: 1 } : { busy_class: cls });
      const l = lights(p.svg);
      assert(l.lit && l.off === 2, `${cls}${loop ? ' + loop' : ''}: `
             + `${l.lit ? 1 : 0} on, ${l.off} off`);
    }
  }
});

// ---- colours come from the theme --------------------------------------

test('the lights are the theme\'s colours, not fixed ones', () => {
  is(shown({ busy_class: 'working' }, { '--indicator-working': '#12ab34' }),
     'top', '#12ab34', 'themed working');
  is(shown({ busy_class: 'idle', wakeup_at: 1 },
           { '--indicator-bg-wait': 'rgb(200, 100, 250)' }),
     'bottom', 'rgb(200, 100, 250)', 'themed loop');
});

test('a theme value that is not a colour never reaches the icon', () => {
  const p = page({ '--indicator-working': '"/><script>alert(1)</script><x a="' });
  p.F.update({ busy_class: 'working' });
  assert(!/script/i.test(p.svg), 'the theme value was written into the SVG');
  is(lights(p.svg).lit, 'top', GREEN, 'fell back to the default');
});

test('the icon is a well-formed SVG image', () => {
  const p = page();
  p.F.update({ busy_class: 'bg-wait' });
  const doc = new p.win.DOMParser().parseFromString(p.svg, 'image/svg+xml');
  assert(!doc.getElementsByTagName('parsererror').length, 'the SVG does not parse');
  assert(doc.documentElement.nodeName === 'svg', 'not an <svg> document');
});

// ---- when it changes ----------------------------------------------------

test('an unchanged state does not rewrite the icon', () => {
  // Status snapshots arrive every couple of seconds; rewriting the link each
  // time makes some browsers refetch and flicker the tab.
  const p = page();
  p.F.update({ busy_class: 'working', busy_label: 'working (1s)' });
  p.F.update({ busy_class: 'working', busy_label: 'working (3s)' });
  assert(p.writes === 1, `wrote the icon ${p.writes} times for one state`);
  p.F.update({ busy_class: 'idle' });
  assert(p.writes === 2, 'a new state did not change the icon');
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
  p.F.update({ busy_class: 'bg-wait' });
  is(lights(p.svg).lit, 'middle', PURPLE, 'after attaching');
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
  const l = lights(F.svg(null));
  assert(!l.lit && l.off === 3, `the plain icon has ${l.off} lights off`);
});

console.log(`\n${ran - failures}/${ran} passed`);
process.exit(failures ? 1 : 0);
