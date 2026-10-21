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

  return { formatDuration, STATE_COLOUR_VARS };
})();
