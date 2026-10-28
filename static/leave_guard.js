/* leave_guard.js — with --confirm-close, closing a tab asks first, unless its
 * session is resting idle.
 *
 * Asked for 2026-10-27: "confirm with a popup before the user can close a tab
 * that's in any state other than idle (and maybe waiting for a loop)".
 * Resting idle is what the tab's grey dot shows (Util.restsIdle): any other
 * colour -- working, connecting, compacting, background tasks running, a loop
 * scheduled, an error -- asks.  Closing the tab stops none of those (the hub
 * carries on without it); what it loses is the user's view of them.
 *
 * The browser draws the question itself, in its own words ("Leave site?"):
 * a page may only ask for it, from ``beforeunload``.  So it is asked as well
 * when the page navigates away -- which it does on purpose now and then: a
 * server's ``navigate``, the reload after a hub restart.  Those call
 * ``leavingOnPurpose`` first.  A phone's browser may not ask at all.
 */

const LeaveGuard = (() => {
  let _status = null;      // the session's latest status snapshot; null: none
  let _onPurpose = false;  // the page is navigating itself

  /** Whether closing now should ask: the hub was started (or joined) with
   *  --confirm-close, and the session is not resting idle -- nor gone, with
   *  the hub shut down, when there is nothing left to watch. */
  function wouldAsk() {
    const s = _status;
    if (!s || !s.confirm_close || s.busy_class === 'shutdown') return false;
    return !Util.restsIdle(s);
  }

  /** Keep the latest status snapshot (called from Status.update).  One the
   *  page makes of its connection (``connection: true``: reconnecting, shut
   *  down) says only that, so it goes over the last one the hub sent. */
  function update(status) {
    if (!status) return;
    _status = status.connection && _status ? { ..._status, ...status } : status;
  }

  /** The tab has no session (on) -- the lobby -- or has one again (off). */
  function setLanding(on) {
    if (on) _status = null;
  }

  /** The page is about to navigate itself: not the user closing it.  Lapses
   *  after a moment, should the navigation not happen. */
  function leavingOnPurpose() {
    _onPurpose = true;
    setTimeout(() => { _onPurpose = false; }, 3000);
  }

  window.addEventListener('beforeunload', (e) => {
    if (_onPurpose || !wouldAsk()) return;
    e.preventDefault();
    // Chrome and Edge ask only if this is set; its text is not shown.
    e.returnValue = '';
  });

  return { update, setLanding, leavingOnPurpose, wouldAsk };
})();
