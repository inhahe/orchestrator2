/* favicon.js — the browser tab's icon shows what its session is doing.
 *
 * The icon is the rack in static/favicon.svg: three units, each with an LED
 * that is off.  For a tab viewing a session, one of them lights up, in the
 * colour the status bar writes that state in:
 *
 *   top     green   working (compacting too: it happens inside a turn)
 *   top     grey    idle
 *   top     yellow  connecting, or disconnected and reconnecting
 *   top     red     disconnected for good, the server shut down, or an error
 *                   (rate limited, not authed, api error, open elsewhere)
 *   middle  purple  bg-wait
 *   bottom  purple  waiting to loop: idle, with a wakeup scheduled
 *
 * bg-wait and the loop share a colour -- the loop field is written in the
 * bg-wait purple -- so they are told apart by position.  The top light is the
 * session's own state; the two below are the two things it can be waiting on.
 *
 * Colours are read from the page's CSS variables rather than written in here,
 * so the light stays the colour of the text when the theme changes them.  A
 * tab with no session (the lobby) shows the plain icon, whatever happens.
 */

const Favicon = (() => {
  const DEFAULT_HREF = '/static/favicon.svg';

  // Centre line of each rack unit's LED.
  const ROWS = { top: 17, middle: 32, bottom: 47 };
  const LED_X = 42;

  // The default theme's values, for a variable that does not resolve.
  const FALLBACK = {
    '--indicator-working':    '#0dbc79',
    '--indicator-bg-wait':    '#bc3fbc',
    '--indicator-idle':       '#666666',
    '--indicator-connecting': '#e5e510',
    '--system-warning':       '#e5e510',
    '--system-error':         '#cd3131',
  };

  let _key = null;        // what the icon shows now; null = the plain icon
  let _landing = false;   // no session in this tab

  /** Which light a status snapshot lights: {row, cssVar}. */
  function lightFor(status) {
    const cls = (status && status.busy_class) || 'idle';
    switch (cls) {
      case 'working':
      case 'compacting':   return { row: 'top', cssVar: '--indicator-working' };
      case 'bg-wait':      return { row: 'middle', cssVar: '--indicator-bg-wait' };
      case 'connecting':   return { row: 'top', cssVar: '--indicator-connecting' };
      case 'reconnecting': return { row: 'top', cssVar: '--system-warning' };
      case 'shutdown':
      case 'error':        return { row: 'top', cssVar: '--system-error' };
      default:
        // Idle -- and "waiting to loop" is idle with a wakeup scheduled.
        return (status && typeof status.wakeup_at === 'number')
          ? { row: 'bottom', cssVar: '--indicator-bg-wait' }
          : { row: 'top', cssVar: '--indicator-idle' };
    }
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

  /** The icon as SVG text, with the LED in *row* lit in *colour*; no row
   *  lit when *row* is null. */
  function svg(row, colour) {
    let leds = '';
    for (const [name, y] of Object.entries(ROWS)) {
      if (name === row) {
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

  /** Light the LED for a status snapshot (called from Status.update). */
  function update(status) {
    if (_landing) return;
    const { row, cssVar } = lightFor(status);
    const colour = _resolve(cssVar);
    _show(row + ' ' + colour,
          'data:image/svg+xml,' + encodeURIComponent(svg(row, colour)));
  }

  /** The tab has no session (on) or has one again (off). */
  function setLanding(on) {
    _landing = !!on;
    if (_landing) _show(null, DEFAULT_HREF);
  }

  return { update, setLanding, lightFor, svg };
})();
