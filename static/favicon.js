/* favicon.js — the browser tab's icon shows what its session is doing.
 *
 * A tab viewing a session shows one dot filling the icon, so its state can be
 * read from the tab strip at a glance.  It is the colour the status bar
 * writes its state in (Util.STATE_COLOUR_VARS):
 *
 *   red     an error, or disconnected for good / the server shut down
 *   yellow  compacting, or not connected (connecting, or reconnecting)
 *   green   working
 *   purple  waiting on background tasks (bg-wait)
 *   grey    idle -- but purple while background tasks run or a loop is
 *           scheduled, which the status bar shows in fields of their own
 *
 * The status bar shows one state, which the hub chose, so a turn with
 * background tasks running is green and compacting during bg-wait is yellow.
 *
 * Colours are read from the page's CSS variables rather than written in here,
 * so the dot stays the colour of the state's text when the theme changes
 * them.  A tab with no session (the lobby) shows the app's own icon,
 * static/favicon.svg, whatever happens.
 */

const Favicon = (() => {
  const DEFAULT_HREF = '/static/favicon.svg';

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

  let _key = null;        // the colour shown now; null = the app's icon
  let _landing = false;   // no session in this tab

  /** The CSS variable whose colour the dot shows for a status snapshot. */
  function _colourFor(status) {
    const s = status || {};
    const stateVar = Util.STATE_COLOUR_VARS[s.busy_class || 'idle'];
    if (stateVar) return stateVar;
    if (s.bg_count > 0 || typeof s.wakeup_at === 'number') return '--indicator-bg-wait';
    return '--indicator-idle';
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

  /** The icon as SVG text: a dot in *colour*, as large as the icon allows.
   *  Its dark rim keeps a pale colour visible on a pale tab strip. */
  function _svg(colour) {
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">'
      + `<circle cx="32" cy="32" r="28" fill="${colour}"`
      + ' stroke="#000" stroke-opacity=".35" stroke-width="4"/></svg>';
  }

  function _show(key, href) {
    _key = key;
    const link = document.getElementById('favicon')
              || document.querySelector('link[rel~="icon"]');
    if (link) link.href = href;
  }

  /** Show the dot for a status snapshot (called from Status.update, every
   *  couple of seconds: the icon is built only when its colour changes). */
  function update(status) {
    if (_landing) return;
    const colour = _resolve(_colourFor(status));
    if (colour === _key) return;
    _show(colour, 'data:image/svg+xml,' + encodeURIComponent(_svg(colour)));
  }

  /** The tab has no session (on) or has one again (off). */
  function setLanding(on) {
    _landing = !!on;
    if (_landing) _show(null, DEFAULT_HREF);
  }

  return { update, setLanding };
})();
