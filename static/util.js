/* util.js — Shared frontend helpers
 *
 * Loaded before the other modules so they can all reuse the same
 * formatting logic instead of each rolling their own.
 */

const Util = (() => {
  /**
   * Format a duration given in seconds.
   *
   * @param {number} totalSecs  Duration in seconds (negatives clamp to 0).
   * @param {'clock'|'compact'} [style='clock']  Output style:
   *   - 'clock'   → "H:M:SS"   (e.g. "0:2:25") — zero-padded seconds.
   *   - 'compact' → "Hh Mm Ss" (e.g. "2m 25s", "45s", "1h 2m 25s") —
   *                 leading zero units dropped.
   * @returns {string}
   */
  function formatDuration(totalSecs, style = 'clock') {
    totalSecs = Math.max(0, Math.floor(totalSecs || 0));
    const h = Math.floor(totalSecs / 3600);
    const m = Math.floor((totalSecs % 3600) / 60);
    const s = totalSecs % 60;

    if (style === 'compact') {
      if (h > 0) return `${h}h ${m}m ${s}s`;
      if (m > 0) return `${m}m ${s}s`;
      return `${s}s`;
    }
    // 'clock'
    return `${h}:${m}:${String(s).padStart(2, '0')}`;
  }

  /**
   * The CSS variable each status state (``busy_class``) is written in: by
   * the status bar's state text, and by the tab icon's dot (favicon.js),
   * which must agree with it.  A state not here (idle) is written in the
   * default text colour.
   */
  const STATE_COLOUR_VARS = {
    working:      '--indicator-working',
    connecting:   '--indicator-connecting',
    reconnecting: '--system-warning',
    shutdown:     '--system-error',
    error:        '--system-error',
    'bg-wait':    '--indicator-bg-wait',
    compacting:   '--indicator-compacting',
  };

  /**
   * Whether the session of a status snapshot is resting idle -- the grey dot:
   * idle, with no background task running and nothing scheduled to start it
   * again, neither a loop's wakeup nor a resume after a usage limit.  The
   * tab's icon (favicon.js) and the guard on closing it (leave_guard.js)
   * agree on it through here.
   */
  function restsIdle(status) {
    const s = status || {};
    return (s.busy_class || 'idle') === 'idle'
      && !(s.bg_count > 0) && typeof s.wakeup_at !== 'number'
      && typeof s.limit_resume_at !== 'number';
  }

  /** Whether *cls* is a state (``busy_class``) this page knows: idle, or one
   *  of STATE_COLOUR_VARS' own -- not a name it merely inherits, such as
   *  "constructor", which a newer or broken hub's card could carry. */
  function knowsState(cls) {
    return cls === 'idle' || Object.prototype.hasOwnProperty.call(STATE_COLOUR_VARS, cls);
  }

  /**
   * The CSS variable a session's dot is drawn in, for a status snapshot or a
   * session-list card (which carries the same fields, SessionRuntime.meta):
   * its state's colour, else grey resting idle and purple idle with work to
   * come.  The tab's icon (favicon.js) and the session list's dot (lobby.js)
   * are drawn in it.  A state it does not know is taken for idle: the caller
   * that may meet one says what it knows instead (lobby.js).
   */
  function stateColourVar(status) {
    const s = status || {};
    const cls = s.busy_class || 'idle';
    if (cls !== 'idle' && knowsState(cls)) return STATE_COLOUR_VARS[cls];
    return restsIdle({ ...s, busy_class: 'idle' }) ? '--indicator-idle' : '--indicator-bg-wait';
  }

  return { formatDuration, STATE_COLOUR_VARS, restsIdle, stateColourVar, knowsState };
})();
