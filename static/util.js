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

  return { formatDuration };
})();
