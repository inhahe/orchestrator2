/* favicon.js — the browser tab's icon shows what its session is doing.
 *
 * The icon is the rack in static/favicon.svg, whose three LEDs are drawn off.
 * For a tab viewing a session, each LED stands for one thing, lit in the
 * colour the status bar writes it in, and they light independently -- a
 * session doing two things at once shows both:
 *
 *   top     the turn    green working, yellow compacting, red an error
 *                       (rate limited, not authed, api error, open elsewhere),
 *                       grey idle -- grey only when nothing else is lit
 *   middle  purple      background tasks running (bg-wait, or during a turn)
 *   bottom  purple      a loop is scheduled
 *
 * Asked for 2026-09-27: "multiple lights should be able to show at once if
 * it's doing two things at once that show lights. for example, compacting
 * during bg-wait."  Hence one fact per LED.
 *
 * Not connected -- connecting, or disconnected and reconnecting -- lights all
 * three yellow, and red once it has given up or the server shut down.  Nothing
 * else about the session is known then, and a single yellow would read as
 * compacting.
 *
 * Colours are read from the page's CSS variables rather than written in here,
 * so a light stays the colour of its text when the theme changes them.  A tab
 * with no session (the lobby) shows the plain icon, whatever happens.
 */

const Favicon = (() => {
  const DEFAULT_HREF = '/static/favicon.svg';

  // Centre line of each rack unit's LED, top to bottom.
  const ROWS = { top: 17, middle: 32, bottom: 47 };
  const LED_X = 42;

  // The default theme's values, for a variable that does not resolve.
  const FALLBACK = {
    '--indicator-working':    '#0dbc79',
    '--indicator-bg-wait':    '#bc3fbc',
    '--indicator-idle':       '#666666',
    '--indicator-compacting': '#e5e510',
    '--indicator-connecting': '#e5e510',
    '--system-warning':       '#e5e510',
    '--system-error':         '#cd3131',
  };

  let _key = null;        // what the icon shows now; null = the plain icon
  let _landing = false;   // no session in this tab

  function _all(cssVar) {
    return { top: cssVar, middle: cssVar, bottom: cssVar };
  }

  /** Which LEDs a status snapshot lights: {top?, middle?, bottom?} -> the CSS
   *  variable each is lit in.  Unlit LEDs are absent. */
  function lightsFor(status) {
    const s = status || {};
    const cls = s.busy_class || 'idle';

    // Not connected: nothing else is known.
    if (cls === 'connecting')   return _all('--indicator-connecting');
    if (cls === 'reconnecting') return _all('--system-warning');
    if (cls === 'shutdown')     return _all('--system-error');

    const lit = {};
    if (cls === 'working')         lit.top = '--indicator-working';
    else if (cls === 'compacting') lit.top = '--indicator-compacting';
    else if (cls === 'error')      lit.top = '--system-error';
    // bg_count says so during a turn too; an older hub only says bg-wait.
    if (s.bg_count > 0 || cls === 'bg-wait') lit.middle = '--indicator-bg-wait';
    if (typeof s.wakeup_at === 'number')     lit.bottom = '--indicator-bg-wait';
    if (!lit.top && !lit.middle && !lit.bottom) lit.top = '--indicator-idle';
    return lit;
  }

  // Only plain colour syntax reaches the SVG.  The value comes from a theme
  // file, and anything else would at best make the icon invalid.
  const COLOUR = /^(#[0-9a-f]{3,8}|(rgba?|hsla?)\([0-9.,%\s]+\)|[a-z]+)$/i;

  function _resolve(cssVar) {
    let v = '';
    try {
      v = getComputedStyle(document.documentElement).getPropertyValue(cssVar).trim();
    } catch (e) { /* no styles yet */ }
    return COLOUR.test(v) ? v : FALLBACK[cssVar];
  }

  /** The icon as SVG text.  *lit* maps a row to the colour its LED is lit in;
   *  rows it leaves out are drawn off.  Null or {} is the plain icon. */
  function svg(lit) {
    const on = lit || {};
    let leds = '';
    for (const [name, y] of Object.entries(ROWS)) {
      const colour = on[name];
      if (colour) {
        leds +=
          `<circle cx="${LED_X}" cy="${y}" r="8" fill="${colour}" opacity=".35"/>` +
          `<circle cx="${LED_X}" cy="${y}" r="4.6" fill="${colour}"/>` +
          `<circle cx="${LED_X - 1.4}" cy="${y - 1.4}" r="1.5" fill="#fff" opacity=".6"/>`;
      } else {
        leds += `<circle cx="${LED_X}" cy="${y}" r="3.6" fill="#11151d"` +
                ` stroke="#3d4759" stroke-width="1"/>`;
      }
    }
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">' +
      '<defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0" stop-color="#2b3242"/><stop offset="1" stop-color="#1a1f2b"/>' +
      '</linearGradient></defs>' +
      '<rect x="10" y="6" width="44" height="52" rx="6" fill="url(#bg)" stroke="#3d4759" stroke-width="2"/>' +
      '<g fill="#232a38">' +
      '<rect x="16" y="12" width="32" height="10" rx="2"/>' +
      '<rect x="16" y="27" width="32" height="10" rx="2"/>' +
      '<rect x="16" y="42" width="32" height="10" rx="2"/></g>' +
      '<g fill="#4a5468">' +
      '<rect x="20" y="15" width="14" height="1.6" rx="0.8"/>' +
      '<rect x="20" y="18" width="14" height="1.6" rx="0.8"/>' +
      '<rect x="20" y="30" width="14" height="1.6" rx="0.8"/>' +
      '<rect x="20" y="33" width="14" height="1.6" rx="0.8"/>' +
      '<rect x="20" y="45" width="14" height="1.6" rx="0.8"/>' +
      '<rect x="20" y="48" width="14" height="1.6" rx="0.8"/></g>' +
      leds + '</svg>';
  }

  function _show(key, href) {
    if (key === _key) return;
    _key = key;
    const link = document.getElementById('favicon')
              || document.querySelector('link[rel~="icon"]');
    if (link) link.href = href;
  }

  /** Light the LEDs for a status snapshot (called from Status.update). */
  function update(status) {
    if (_landing) return;
    const lit = {};
    for (const [row, cssVar] of Object.entries(lightsFor(status))) {
      lit[row] = _resolve(cssVar);
    }
    const key = Object.keys(ROWS).map((r) => r + '=' + (lit[r] || '')).join(' ');
    _show(key, 'data:image/svg+xml,' + encodeURIComponent(svg(lit)));
  }

  /** The tab has no session (on) or has one again (off). */
  function setLanding(on) {
    _landing = !!on;
    if (_landing) _show(null, DEFAULT_HREF);
  }

  return { update, setLanding, lightsFor, svg };
})();
