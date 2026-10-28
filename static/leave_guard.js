/* leave_guard.js — with --confirm-close, closing a tab asks first, unless its
 * session is resting idle.
 *
 * Asked for 2026-10-27: "confirm with a popup before the user can close a tab
 * that's in any state other than idle (and maybe waiting for a loop)".
 * Resting idle is what the tab's grey dot shows (Util.restsIdle): any other
 * colour -- working, connecting, compacting, background tasks running, a loop
 * or a resume after a usage limit scheduled, an error -- asks.  Closing one
 * tab of several stops none of those, but closing the last can: a hub that
 * shuts down when its tabs are gone (auto-shutdown, which --open and --detach
 * turn on) stops its turns with it, and a session whose last tab closes is
 * closed after the idle timeout once nothing keeps it.
 *
 * The browser draws the question itself, in its own words ("Leave site?"):
 * a page may only ask for it, from ``beforeunload``, and is heard only once
 * the user has clicked or typed in it -- a tab opened and only watched is
 * closed without a question, as it is on a phone, whose browser may not ask
 * at all.  It is asked as well when the page navigates away, which it does
 * on purpose now and then -- a server's ``navigate``, the reload after a hub
 * restart -- so those call ``leavingOnPurpose`` first.
 */

const LeaveGuard = (() => {
  let _status = null;      // the session's latest status snapshot; null: none
  let _onPurpose = false;  // the page is navigating itself
  let _onPurposeTimer = null;

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
    // From the latest: an earlier one's would end the mark early.
    clearTimeout(_onPurposeTimer);
    _onPurposeTimer = setTimeout(() => { _onPurpose = false; }, 3000);
  }

  window.addEventListener('beforeunload', (e) => {
    if (_onPurpose || !wouldAsk()) return;
    e.preventDefault();
    // Chrome and Edge ask only if this is set; its text is not shown.
    e.returnValue = '';
  });

  return { update, setLanding, leavingOnPurpose, wouldAsk };
})();
