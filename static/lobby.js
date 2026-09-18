/* lobby.js — Session lobby overlay
 *
 * The server is a multi-session hub.  This overlay lets a tab browse the
 * live sessions running on the server plus recent on-disk sessions, and
 * switch between them.  The tab stays attached to its current session
 * while the lobby is open (it sends `list`, not `detach`); picking a
 * session sends `attach` / `open` / `new`, and the server replies with an
 * `attached` message that closes the overlay and swaps in that session's
 * chat.
 *
 * Wire protocol
 *   send:    list, attach {rid}, open {session_id, cwd}, new {cwd},
 *            close {rid}
 *   receive: session_list {running:[meta], recent:[disk]}, attached {session},
 *            session_closed {rid, message}
 */

const Lobby = (() => {
  let elLobby, elRunning, elRunningEmpty, elRunningCount;
  let elRecent, elRecentEmpty, elRecentCount, elRecentLoading;
  let elNewCwd, elNewBtn, elClose, elSessionsBtn, elShutdown, elRestart;
  let elNotice, elNoticeText, elNoticeClose;

  // False until the first session_list arrives.  While false the Recent
  // section shows a "loading sessions…" spinner instead of the empty state,
  // so the first (disk-scanning) load doesn't look like "no sessions".
  let _loadedOnce = false;

  let _visible = false;
  // True once this tab has attached to a session (via `attached`).  Until
  // then the tab is in **landing mode**: the lobby is the primary view and
  // can't be dismissed (there's no chat behind it to reveal).
  let _hasSession = false;
  // rid of the session this tab is currently attached to (from `attached`).
  let _currentRid = null;
  // Last cwd we saw for the current session, used to prefill "New session".
  let _currentCwd = '';
  // setInterval handle that ticks the idle-teardown countdown badges.
  let _countdownTimer = null;
  // True when this tab was launched to open/create a session (via ?open / ?new)
  // and is waiting for its `attached`.  Suppresses the landing lobby flash.
  let _pendingAttach = false;
  // True while we're subscribed to live lobby updates on the server (i.e. this
  // ws is in the server's `_lobby_watchers` set).  Making the subscription
  // idempotent is critical: the server answers `lobby_watch {on:true}` with an
  // immediate `session_list`, and render() would re-send `lobby_watch on` for
  // every such push in landing mode — an infinite loop that rebuilt the list
  // DOM continuously (flicker on hover, clicks lost between mousedown/mouseup).
  let _watching = false;

  // Subscribe/unsubscribe from live lobby pushes, but only on an actual state
  // transition so repeated calls (e.g. once per received session_list) are
  // no-ops rather than a feedback loop.
  function _watch(on) {
    if (on === _watching) return;
    _watching = on;
    App.send({ type: 'lobby_watch', on: on });
    if (on) _startCountdownTick(); else _stopCountdownTick();
  }

  // The server drops this ws from `_lobby_watchers` when it disconnects, so a
  // reconnect must forget we were watching — otherwise _watch(true) would be a
  // no-op and we'd never re-subscribe.  Called from app.js on ws (re)open.  If
  // the overlay is still open, resubscribe on the fresh socket so live updates
  // resume; otherwise just clear the flag.
  function onReconnect() {
    _watching = false;
    if (_visible) _watch(true);
  }

  function init() {
    elLobby        = document.getElementById('lobby');
    elRunning      = document.getElementById('lobby-running');
    elRunningEmpty = document.getElementById('lobby-running-empty');
    elRunningCount = document.getElementById('lobby-running-count');
    elRecent       = document.getElementById('lobby-recent');
    elRecentEmpty  = document.getElementById('lobby-recent-empty');
    elRecentCount  = document.getElementById('lobby-recent-count');
    elRecentLoading = document.getElementById('lobby-recent-loading');
    elNewCwd       = document.getElementById('lobby-new-cwd');
    elNewBtn       = document.getElementById('lobby-new-btn');
    elClose        = document.getElementById('lobby-close');
    elSessionsBtn  = document.getElementById('sessions-btn');
    elShutdown     = document.getElementById('lobby-shutdown');
    elRestart      = document.getElementById('lobby-restart');
    elNotice       = document.getElementById('lobby-notice');
    elNoticeText   = document.getElementById('lobby-notice-text');
    elNoticeClose  = document.getElementById('lobby-notice-close');
    if (elNoticeClose) {
      elNoticeClose.addEventListener('click', () => {
        if (elNotice) elNotice.classList.add('hidden');
      });
    }

    // The ☰ control navigates back to the hub (lobby).  It is an anchor so
    // the browser can offer "open in new tab"; a plain click keeps the
    // existing behaviour (overlay on mobile, tab reuse on desktop).
    if (elSessionsBtn) {
      elSessionsBtn.addEventListener('click', (e) => {
        if (!_plainClick(e)) return;      // the user asked for a new tab
        e.preventDefault();
        openHub();
      });
    }
    if (elClose) elClose.addEventListener('click', hide);
    if (elShutdown) elShutdown.addEventListener('click', _shutdownServer);
    if (elRestart) elRestart.addEventListener('click', _restartServer);

    if (elNewBtn) {
      elNewBtn.addEventListener('click', () => {
        const cwd = (elNewCwd.value || '').trim();
        // Mobile: create the session in this tab (no tab to focus).
        if (_isMobile()) {
          _setBusyNotice('Starting new session\u2026');
          App.send(cwd ? { type: 'new', cwd: cwd } : { type: 'new' });
          return;
        }
        const url = cwd ? '/?new=1&cwd=' + encodeURIComponent(cwd) : '/?new=1';
        // A brand-new session has no stable identity yet, so always open a
        // fresh tab (unique name) rather than reusing any existing one.
        _openInTab(url, 'orch2-new-' + Date.now());
      });
    }
    if (elNewCwd) {
      elNewCwd.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); elNewBtn.click(); }
      });
    }

    // Delegated clicks for dynamically-rendered cards / recent items.  Each
    // opens the session in its own tab — or, if a tab is already showing that
    // session, focuses it — via a stable per-session window name.  The name
    // keys on the session_id so a session reached via "recent" (?open=<sid>)
    // and the same session later shown as a running card map to one tab.
    if (elRunning) {
      elRunning.addEventListener('click', (e) => {
        const card = e.target.closest('.lobby-card');
        if (!card) return;
        // The title is a real link so the browser can offer "open in new
        // tab"; a plain click must still take the in-place path rather than
        // navigating this tab twice.  A modified click (ctrl/cmd/middle/
        // shift) is the user asking for a new tab, so it is left alone.
        if (_plainClick(e)) e.preventDefault(); else return;
        const rid = card.getAttribute('data-rid');
        const sid = card.getAttribute('data-sid');
        // A session running in another hub has no rid here — there is no local
        // runtime to attach to. Route it exactly like a "recent" click: the
        // server answers `session_elsewhere`, which offers to go to the window
        // that holds it (or explains why it can't). Without this branch the
        // `!rid` guard below swallows the click and the card does nothing at
        // all, which is precisely the dead end this card exists to remove.
        if (!rid && card.getAttribute('data-foreign') && sid) {
          _openSessionById(sid,
                           card.getAttribute('data-cwd') || '',
                           card.getAttribute('data-account') || '');
          return;
        }
        if (!rid) return;
        // The × sits inside the card, so its click must be claimed *before*
        // the card's own "open this session" handling — otherwise closing a
        // session would also open it.
        if (e.target.closest('.lobby-card-close')) {
          e.stopPropagation();
          _closeSession(card, rid);
          return;
        }
        // Clicking the session this tab is already viewing: just close the
        // overlay.  Re-opening '/?rid=' into this same (stably-named) tab
        // would force a needless full WS reconnect + history re-send.
        // (In landing mode there's nothing behind the overlay, so hide() is a
        // no-op and we fall through to a real attach instead.)
        if (rid === _currentRid && _hasSession) { hide(); return; }
        // Mobile: no tab focusing available — swap this tab's session in place.
        if (_isMobile()) { _attachInPlace(rid); return; }
        const name = sid ? 'orch2-sess-' + sid : 'orch2-rid-' + rid;
        // Focus the session's existing tab without reloading it; only a
        // never-opened session gets a fresh navigation.
        _focusOrOpen('/?rid=' + encodeURIComponent(rid), name);
      });
    }
    if (elRecent) {
      elRecent.addEventListener('click', (e) => {
        const item = e.target.closest('.lobby-item');
        if (!item) return;
        if (_plainClick(e)) e.preventDefault(); else return;
        const sid = item.getAttribute('data-sid');
        const cwd = item.getAttribute('data-cwd') || '';
        const account = item.getAttribute('data-account') || '';
        if (!sid) return;
        _openSessionById(sid, cwd, account);
      });
    }

    // Esc closes the lobby (only when it's the frontmost overlay — the
    // detail modal handles its own Esc separately in app.js).
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && _visible) {
        const modal = document.getElementById('detail-modal');
        if (modal && !modal.classList.contains('hidden')) return;
        hide();
      }
    });

    // Eagerly show the lobby when the URL suggests we'll land there, so the
    // user sees the lobby overlay immediately instead of a blank chat
    // skeleton that flashes for ~1s while the WebSocket connects and the
    // server decides to send us to the lobby.  If the server auto-attaches
    // to a session instead (single-runtime standalone server), onAttached()
    // hides the lobby before any content renders — no visible flash.
    //
    // Use _showEarly() instead of show() because the WS isn't connected yet
    // — show() would try to send a lobby_watch message and fail.  The
    // server will send the session_list once the WS connects.
    const _params = new URLSearchParams(location.search);
    if (!_params.get('rid') && !_params.get('open') && !_params.get('new')) {
      _setLanding(true);
      _showEarly();
    }
  }

  // --- Navigation ------------------------------------------------------

  // Open the hub (lobby) in its own tab — or focus the existing lobby tab if
  // one is already open — via the stable ``orch2-lobby`` window name.  The
  // ``?lobby=1`` param forces the server to enter the lobby even when a
  // default session exists (without it, ``/`` may auto-attach a session).
  function openHub() {
    // Mobile: there's no separate lobby tab to focus, and opening one would
    // strand the user on a background tab.  Just raise the overlay over this
    // tab's chat — the tab stays attached to its session while browsing.
    if (_isMobile()) { open(); return; }
    _openInTab('/?lobby=1', 'orch2-lobby');
  }

  // --- Mobile vs desktop session switching -----------------------------
  //
  // The desktop model gives every session its own browser tab and switches by
  // focusing the tab with the matching window.name (see _focusOrOpen).  That
  // model is fundamentally unavailable on mobile:
  //
  //   * window.focus() cannot bring another tab to the foreground on Android
  //     Chrome / iOS Safari — it's a silent no-op.
  //   * window.open('', name) generally won't find a separately-opened tab
  //     (no browsing-context-group relationship), so it just makes a new blank
  //     one.
  //
  // The net effect was that tapping a session appeared to do nothing at all
  // (a stray background tab was created, the lobby stayed in front and kept
  // live-refreshing).  On mobile we therefore switch the *current* tab in
  // place over the WebSocket instead — the server's lobby protocol already
  // supports attach/open/new, and replies with `attached`, which swaps the
  // chat in without any reload or new tab.
  let _mobileCache = null;
  // A click the page should handle itself, as opposed to one the user aimed at
  // the browser.  Ctrl/Cmd-click, middle-click and shift-click all mean "open
  // this somewhere else"; swallowing them would take back the very capability
  // the anchors exist to provide.
  function _plainClick(e) {
    return !(e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button === 1);
  }

  function _isMobile() {
    if (_mobileCache !== null) return _mobileCache;
    let m = false;
    try {
      // Purpose-built signal (Chrome on Android reports this).
      if (navigator.userAgentData
          && typeof navigator.userAgentData.mobile === 'boolean') {
        m = navigator.userAgentData.mobile;
      } else {
        m = /Android|iPhone|iPad|iPod|Mobile|Silk|Kindle|BlackBerry|Opera Mini|IEMobile/i
              .test(navigator.userAgent || '');
      }
    } catch (e) { m = false; }
    _mobileCache = m;
    return m;
  }

  // Transient "working on it" text in the lobby's notice bar.  Cleared by
  // onAttached() (which hides the notice) once the swap completes.  If the
  // attach never lands — the session died between the list push and the tap,
  // or the runtime failed to start — the server answers with a chat-level
  // system_msg the lobby can't see, so a watchdog turns the notice into a
  // visible error rather than leaving "Opening session…" up forever.
  let _busyTimer = null;
  function _setBusyNotice(text) {
    if (elNoticeText) elNoticeText.textContent = text || '';
    if (elNotice) elNotice.classList.toggle('hidden', !text);
    if (_busyTimer) { clearTimeout(_busyTimer); _busyTimer = null; }
    if (text) {
      _busyTimer = setTimeout(() => {
        _busyTimer = null;
        if (elNoticeText) {
          elNoticeText.textContent =
            "That session didn't open — it may have stopped running. "
            + 'Pick another below.';
        }
        if (elNotice) elNotice.classList.remove('hidden');
      }, 12000);
    }
  }

  function _clearBusyNotice() {
    if (_busyTimer) { clearTimeout(_busyTimer); _busyTimer = null; }
  }

  // Switch this tab to an already-running session (no reload, no new tab).
  function _attachInPlace(rid) {
    _setBusyNotice('Opening session\u2026');
    App.send({ type: 'attach', rid: rid });
  }

  // Resume an on-disk session into this tab.  The server spins up (or reuses)
  // a runtime and answers with `attached`.
  function _openInPlace(sid, cwd, account) {
    _setBusyNotice('Opening session\u2026');
    const m = { type: 'open', session_id: sid };
    if (cwd) m.cwd = cwd;
    if (account) m.account = account;
    App.send(m);
  }

  // window.open(url, name): if a tab with *name* exists the browser focuses
  // (and navigates) it; otherwise it opens a new tab and stamps it with
  // *name*.  This gives "new tab, or activate the existing one" for free.
  // Falls back to same-tab navigation only if the popup was blocked.
  function _openInTab(url, name) {
    let w = null;
    try { w = window.open(url, name); } catch (e) { w = null; }
    if (w) { try { w.focus(); } catch (e) {} return; }
    // Popup blocked (rare for a user-gesture click) — navigate in place.
    window.location.href = url;
  }

  // Focus an existing per-session tab WITHOUT reloading it; only navigate if we
  // had to create a fresh tab.  `window.open('', name)` reuses a same-named tab
  // and returns it *without* navigating (so no WS reconnect / history re-send);
  // if no such tab exists it makes a blank one, which we then point at `url`.
  // This is what "activate the already-open session" should do — the plain
  // _openInTab re-navigates the target to `url`, forcing a needless full reload
  // (the bug when the lobby is its own tab and _currentRid can't short-circuit).
  function _focusOrOpen(url, name) {
    let w = null;
    try { w = window.open('', name); } catch (e) { w = null; }
    if (!w) { window.location.href = url; return; }   // popup blocked
    // **The tab we were asked to focus can be this one.**  Browsing-context
    // lookup by name checks the *current* context first, so a tab still
    // carrying `orch2-sess-<sid>` gets itself back -- and then the code below
    // finds it non-blank, calls focus() on the window that already has focus,
    // and the click does nothing at all.  Forever, because nothing about the
    // situation changes by clicking again.
    //
    // Reported 2026-09-16: a session tab whose session had gone was showing
    // the lobby, still named for that session; clicking it in Recents was a
    // no-op every time.  Navigating here is also the *right* answer -- "focus
    // the tab showing this session" when that tab is this one means show it.
    if (w === window) { window.location.href = url; return; }
    let blank = false;
    try { blank = !w.location.href || w.location.href === 'about:blank'; }
    catch (e) { blank = false; }   // cross-origin read blocked -> assume live
    if (blank) { try { w.location.href = url; } catch (e) {} }  // fresh tab
    try { w.focus(); } catch (e) {}
  }

  /** Stop ONE running session (the × on its card), leaving the server and
   *  every other session alone.
   *
   *  This is the counterpart to "Shut down server": until it existed, the
   *  only way to stop a session's `claude.exe` was to kill the whole hub —
   *  or, if you didn't know about /api/shutdown, to hunt for a PID in Task
   *  Manager.  Closing the browser tab does NOT stop a session: the server
   *  keeps it alive (and the CLI with it) so you can come back to it.
   *
   *  The confirm() spells out what is and isn't lost, because the two
   *  outcomes people fear are different: the session *file* is untouched and
   *  the session can be reopened from Recent, but an in-flight turn is gone.
   */
  function _closeSession(card, rid) {
    const titleEl = card.querySelector('.lobby-card-title');
    const name = titleEl ? titleEl.textContent.trim() : rid;
    const busy = !!card.querySelector('.lobby-dot.busy');
    let msg = `Close "${name}"?\n\n`
      + 'This stops the session\u2019s Claude process. Its history is kept on '
      + 'disk \u2014 you can reopen it later from Recent.';
    if (busy) {
      msg += '\n\nThis session is working right now; the in-flight turn will '
           + 'be lost.';
    }
    if (rid === _currentRid) {
      msg += '\n\nThis is the session this tab is viewing, so the tab will '
           + 'return to the session list.';
    }
    if (!window.confirm(msg)) return;
    // Immediate feedback: stopping a CLI can take a couple of seconds, and the
    // card only disappears when the server pushes the next session_list.
    card.classList.add('closing');
    const btn = card.querySelector('.lobby-card-close');
    if (btn) { btn.disabled = true; btn.textContent = '\u22ef'; }
    App.send({ type: 'close', rid: rid });
  }

  /** Shut down the entire server process (all sessions).  POSTs
   *  /api/shutdown, which stops every bridge and hard-exits the process. */
  async function _shutdownServer() {
    const n = (elRunning && elRunning.children.length) || 0;
    const msg = n > 1
      ? `Shut down the server?\n\nThis kills the whole orchestrator2 process and ends all ${n} running sessions. Unsaved in-flight turns will be lost.`
      : 'Shut down the server?\n\nThis kills the whole orchestrator2 process. Any in-flight turn will be lost.';
    if (!window.confirm(msg)) return;
    if (elShutdown) {
      elShutdown.disabled = true;
      elShutdown.textContent = 'Shutting down\u2026';
    }
    try {
      await fetch('/api/shutdown', { method: 'POST' });
    } catch (e) { /* the process may exit before the response arrives */ }
    // Replace the UI with a terminal notice; the socket will drop shortly.
    document.body.innerHTML =
      '<div style="display:flex;align-items:center;justify-content:center;'
      + 'height:100vh;font:16px system-ui,sans-serif;color:#888;text-align:center;'
      + 'padding:2rem">orchestrator2 server has shut down.<br>You can close this tab.</div>';
  }

  /** Restart the whole server: spawns a fresh process (picking up code
   *  changes), resumes the primary session, and reloads this tab once the
   *  new server is serving. */
  async function _restartServer() {
    const n = (elRunning && elRunning.children.length) || 0;
    const extra = n > 1
      ? `\n\nThe primary session resumes automatically; the other ${n - 1} running session(s) are not restored.`
      : '';
    if (!window.confirm('Restart the server?\n\nThis kills the process and starts '
        + 'a fresh one (loading any code changes). Any in-flight turn is lost.' + extra)) return;
    if (elRestart) { elRestart.disabled = true; elRestart.textContent = 'Restarting\u2026'; }
    if (elShutdown) elShutdown.disabled = true;
    let resp;
    try {
      resp = await fetch('/api/restart', { method: 'POST' });
    } catch (e) {
      // The process may exit before the response arrives — that's success.
      _showReloadingAndPoll();
      return;
    }
    let data = null;
    try { data = await resp.json(); } catch (e) {}
    if (data && data.ok === false) {
      // Replacement failed to start; the server stayed alive.
      const detail = data.detail ? '\n\n' + data.detail : '';
      window.alert('Restart failed: ' + (data.error || 'unknown error') + detail);
      if (elRestart) { elRestart.disabled = false; elRestart.textContent = '\u21bb Restart server'; }
      if (elShutdown) elShutdown.disabled = false;
      return;
    }
    _showReloadingAndPoll();
  }

  /** Full-page "restarting…" splash that polls /api/ready and reloads once
   *  the fresh server answers. */
  function _showReloadingAndPoll() {
    document.body.innerHTML =
      '<div style="display:flex;align-items:center;justify-content:center;'
      + 'flex-direction:column;gap:18px;height:100vh;font:15px system-ui,sans-serif;'
      + 'color:#9aa2c0;text-align:center;padding:2rem">'
      + '<div style="width:42px;height:42px;border-radius:50%;border:4px solid #23233a;'
      + 'border-top-color:#7aa2f7;animation:o2spin .8s linear infinite"></div>'
      + '<div>restarting orchestrator2\u2026</div></div>'
      + '<style>@keyframes o2spin{to{transform:rotate(360deg)}}</style>';
    let tries = 0;
    async function poll() {
      tries++;
      try {
        const r = await fetch('/api/ready', { cache: 'no-store' });
        if (r.ok) { location.reload(); return; }
      } catch (e) { /* server still down / restarting */ }
      // Give up after ~60s and reload anyway so the user isn't stuck.
      if (tries > 150) { location.reload(); return; }
      setTimeout(poll, 400);
    }
    // Small initial delay so the old process has a moment to exit.
    setTimeout(poll, 800);
  }

  // --- Visibility ------------------------------------------------------

  function open() {
    if (elNewCwd && !elNewCwd.value) elNewCwd.value = _currentCwd || '';
    show();
  }

  /** Reflect the "waiting for the first session_list" state: spinner instead
   *  of the empty placeholders until data (however empty) has arrived once. */
  function _reflectLoading() {
    if (_loadedOnce) return;
    if (elRecentLoading) elRecentLoading.classList.remove('hidden');
    if (elRecentEmpty) elRecentEmpty.classList.add('hidden');
    if (elRunningEmpty) elRunningEmpty.classList.add('hidden');
  }

  /** Show the overlay visually without sending a WS message (pre-connect). */
  function _showEarly() {
    if (_visible) return;
    _visible = true;
    elLobby.classList.remove('hidden');
    _reflectLoading();
  }

  function show() {
    if (_visible) return;
    _visible = true;
    elLobby.classList.remove('hidden');
    _reflectLoading();
    // Watch the lobby: the server sends an immediate snapshot and then keeps
    // pushing live updates (viewer counts, busy dots, sessions coming/going)
    // for as long as the overlay stays open.
    _watch(true);
  }

  function hide() {
    if (!_visible) return;
    // Landing mode: there's no session behind the overlay, so refuse to
    // close it (the × button is hidden and Esc is a no-op here).
    if (!_hasSession) return;
    _visible = false;
    elLobby.classList.add('hidden');
    _watch(false);
  }

  function isVisible() { return _visible; }

  // Toggle landing mode: hide the × close button so the lobby can't be
  // dismissed into a blank screen before any session is attached.  A landing
  // tab *is* the session browser, so title it "Sessions" (status.js sets the
  // per-session title once this tab attaches to one and landing mode ends).
  function _setLanding(on) {
    if (elClose) elClose.classList.toggle('hidden', on);
    if (on) { try { document.title = 'Sessions'; } catch (e) {} }
  }

  // Called from app.js when an `attached` message arrives — the tab is now
  // viewing session `meta.rid`, so record it and close the lobby.
  function onAttached(meta) {
    if (meta) {
      _currentRid = meta.rid || null;
      _currentCwd = meta.cwd || _currentCwd;
      // Stamp this tab with a stable per-session name so a later "open this
      // session" click from any lobby focuses *this* tab instead of opening a
      // duplicate — even when this tab was opened by typing the URL directly
      // (no opener to inherit the name from).
      try {
        if (meta.session_id) window.name = 'orch2-sess-' + meta.session_id;
      } catch (e) {}
    }
    _hasSession = true;
    _pendingAttach = false;
    _clearBusyNotice();
    if (elNotice) elNotice.classList.add('hidden');   // stale-session banner no longer applies
    _setLanding(false);
    hide();
  }

  // --- Rendering -------------------------------------------------------

  function render(msg) {
    // Two-phase landing: the server sends the running list immediately with
    // ``recent_pending`` set, then the full list (with recent) once its disk
    // scan finishes.  While pending, render the running sessions but keep the
    // "loading sessions…" spinner up instead of flashing an empty "recent"
    // placeholder.
    _renderStale(msg.stale);
    _renderMobileHint();
    _renderRunning(msg.running || []);
    if (msg.recent_pending) {
      if (elRecentLoading && !_loadedOnce) elRecentLoading.classList.remove('hidden');
      return _finishRenderLanding(msg);
    }
    // Full data has arrived — drop the loading spinner and let the empty
    // placeholders (if any list really is empty) take over from here.
    _loadedOnce = true;
    if (elRecentLoading) elRecentLoading.classList.add('hidden');
    _renderRecent(msg.recent || []);
    return _finishRenderLanding(msg);
  }

  // Mobile only: say that a session can be opened in its own tab.
  //
  // The capability is the browser's (long-press a link), but nothing on a
  // phone advertises it, and until 2026-09-14 it genuinely did not exist here
  // -- the cards were divs, so one tab was all you could have.  One sentence,
  // once, where the sessions are.
  function _renderMobileHint() {
    if (!_isMobile()) return;
    let el = document.getElementById('lobby-mobile-hint');
    if (el) return;
    const host = document.getElementById('lobby-running');
    if (!host || !host.parentNode) return;
    el = document.createElement('div');
    el.id = 'lobby-mobile-hint';
    el.className = 'lobby-hint';
    el.textContent =
      'Tip: long-press a session name to open it in its own tab — tapping '
      + 'switches this tab instead.';
    host.parentNode.insertBefore(el, host);
  }

  // "This hub is running code that has since changed on disk."
  //
  // Python is read once at import, so a hub goes on running whatever was there
  // when it started — for days, silently.  That cost a real misdiagnosis: a
  // bug was reported against a hub whose process predated its own fix by a
  // day, and the only evidence was a log line that no longer existed in the
  // source.  The notice sits next to the ⟳ Restart button, which is the whole
  // remedy, and is deliberately not dismissable: it is a fact about the code
  // you are looking at, and it stops being true the moment you act on it.
  function _renderStale(stale) {
    let el = document.getElementById('lobby-stale');
    const n = (stale && stale.count) || 0;
    if (!n) {
      if (el) el.remove();
      return;
    }
    if (!el) {
      el = document.createElement('div');
      el.id = 'lobby-stale';
      el.className = 'lobby-stale';
      const notice = document.getElementById('lobby-notice');
      if (notice && notice.parentNode) {
        notice.parentNode.insertBefore(el, notice);
      } else if (elLobby) {
        elLobby.appendChild(el);
      }
    }
    const files = (stale.files || []).join(', ');
    const age = stale.started_at ? _fmtAgo(stale.started_at) : '';
    el.innerHTML =
      '<b>This hub is running older code.</b> ' +
      _esc(String(n)) + ' loaded source file' + (n === 1 ? '' : 's') +
      ' changed on disk since it started' + (age ? ' ' + _esc(age) : '') +
      '. Restart to pick ' + (n === 1 ? 'it' : 'them') + ' up.' +
      (files ? '<span class="lobby-stale-files">' + _esc(files) + '</span>' : '');
    el.title = 'Python is loaded once at import, so a running server keeps '
      + 'executing the version it started with. Static files (JS/CSS) are '
      + 're-read per request — those only need a browser refresh.';
  }

  // Shared landing/visibility logic for both the pending and full renders.
  function _finishRenderLanding(msg) {
    // Landing mode: a session_list arrived but this tab isn't attached to any
    // session yet, so the lobby *is* the view.  Show it non-dismissably —
    // unless this tab was launched to open a specific session (?open/?new) and
    // is just waiting for its `attached`, in which case don't flash the lobby.
    if (!_hasSession && !_pendingAttach) {
      // This tab is a lobby tab — stamp it so ☰ Sessions clicks from other
      // tabs focus it instead of spawning a duplicate lobby.
      try { window.name = 'orch2-lobby'; } catch (e) {}
      _setLanding(true);
      // If the lobby was shown early (pre-connect), show() is a no-op
      // because _visible is already true — but we still need to subscribe
      // for live updates now that the WS is connected.  _watch() is idempotent,
      // so this doesn't re-fire on every session_list push (which would loop).
      if (_visible) {
        _watch(true);
      } else {
        show();
      }
    }
  }

  // Called from app.js when the session THIS tab is viewing has been closed
  // (from this tab's × or another tab's).  The server has already cleared the
  // chat and moved the socket back to the lobby; what's left is to forget the
  // dead session locally.  Dropping _hasSession is the important part: without
  // it the overlay stays dismissable (× / Esc) into an empty chat wired to a
  // session that no longer exists.
  function onSessionClosed(rid, message) {
    if (rid && _currentRid && rid !== _currentRid) return;   // not our session
    _currentRid = null;
    _hasSession = false;
    _pendingAttach = false;
    // This tab is a session browser again — let it be found as one.
    try { window.name = 'orch2-lobby'; } catch (e) {}
    _setLanding(true);
    showNotice(message || 'That session was closed.');
  }

  // Called from app.js when a tab is launched with ?open=/?new= — it will
  // attach shortly, so we shouldn't show the landing lobby in the meantime.
  function expectSession() {
    _pendingAttach = true;
  }

  // Show a visible banner on the lobby overlay (e.g. "this tab's session is no
  // longer running").  Forces the lobby open so the message is actually seen —
  // the tab was expecting to attach, so cancel that and reveal the browser.
  function showNotice(text) {
    _pendingAttach = false;
    // A real notice supersedes any in-flight "Opening session…" watchdog;
    // otherwise the watchdog would clobber this message 12s later.
    _clearBusyNotice();
    if (elNoticeText) elNoticeText.textContent = text || '';
    if (elNotice) elNotice.classList.toggle('hidden', !text);
    // The tab never attached (or its session vanished), so it's a landing tab.
    if (!_hasSession) _setLanding(true);
    // A notice forces the lobby in front, so whatever this tab was showing, it
    // is not showing it now.  Give up the per-session window name: keeping it
    // makes this tab the target of every *other* tab's "focus the tab with
    // this session", which would hand them a lobby instead.
    try { window.name = 'orch2-lobby'; } catch (e) {}
    if (_visible) {
      _watch(true);
    } else {
      show();
    }
  }

  function _renderRunning(list) {
    elRunningCount.textContent = String(list.length);
    elRunning.innerHTML = '';
    const empty = list.length === 0;
    elRunningEmpty.classList.toggle('hidden', !empty);
    elRunning.classList.toggle('hidden', empty);
    for (const m of list) {
      elRunning.appendChild(_runningCard(m));
    }
  }

  function _runningCard(m) {
    const card = document.createElement('div');
    card.className = 'lobby-card';
    // A foreign session has `rid: null` (there is nothing here to attach to),
    // and `_currentRid` is null in a tab that isn't attached to a session --
    // e.g. a standalone `?lobby=1` tab.  `null === null` is true, so every
    // foreign card in such a tab used to label itself "current", which is
    // also the exact opposite of the truth: a session running in *another*
    // hub is the one session this tab is definitely not attached to.
    const isCurrent = !!m.rid && m.rid === _currentRid && !m.foreign;
    if (isCurrent) card.classList.add('current');
    // A session running in *another* hub process. It belongs in this list —
    // it is running — but nothing here can attach to it or close it, so it is
    // marked and its × is withheld rather than offering actions that would
    // fail. Clicking it goes through the normal open path, which the server
    // answers with `session_elsewhere`.
    if (m.foreign) {
      card.classList.add('foreign');
      card.setAttribute('data-foreign', '1');
      if (m.port) card.setAttribute('data-port', String(m.port));
    }
    card.setAttribute('data-rid', m.rid || '');
    if (m.session_id) card.setAttribute('data-sid', m.session_id);
    if (m.cwd) card.setAttribute('data-cwd', m.cwd);
    if (m.account) card.setAttribute('data-account', m.account);

    const title = m.title || (m.session_id ? m.session_id.substring(0, 8)
                                           : '(new session)');
    // Three states, not two.  `busy` is null when the session belongs to a hub
    // we could not ask (see `_foreign_running_entry`), and rendering that as
    // the idle dot asserted "not working" about a session that might well be
    // mid-turn.  "I don't know" is a real answer and gets its own dot.
    const known = m.busy === true || m.busy === false;
    const dotCls = m.busy === true ? 'busy' : (known ? 'idle' : 'unknown');
    // The two ways of not knowing are worth telling apart when you are trying
    // to work out why: a hub that did not answer is a hub you can go look at,
    // while a bare `claude --resume` in a terminal has no API to answer with.
    const dotTitle = m.busy === true ? 'working'
      : (known ? 'idle'
               : (m.port
                    ? `Owned by the hub on port ${m.port}, which didn't answer `
                      + '— whether it is working right now is unknown'
                    : 'Held by a process that is not an orchestrator2 hub '
                      + '(e.g. `claude --resume` in a terminal), so its state '
                      + 'cannot be queried'));
    const activity = _fmtAgo(m.last_activity);
    const viewers = typeof m.viewers === 'number' ? m.viewers : null;
    const viewerLabel = viewers === 1 ? '1 viewer' : `${viewers} viewers`;
    // Show a short account tag for cross-account sessions.
    const acct = m.account
      ? m.account.replace(/\\/g, '/').split('/').pop() || ''
      : '';

    // Idle-teardown countdown: shown only when the server has armed a timer
    // (viewer-less session that will be closed when it expires).
    const dl = m.idle_deadline;
    const countdown = dl
      ? `<div class="lobby-countdown" data-deadline="${dl}"
             title="No viewers — this session will be closed when the timer runs out">
           closing in ${_esc(_fmtCountdown(dl))}
         </div>`
      : '';

    // The foreign marker lives on the meta line, not beside the title.  In a
    // three-across grid a card is ~275px and "other window :63978" is half of
    // it, so sharing the title row cost the title most of its width and
    // rendered "Good Photons" as "Goo…".  A card's title is its identity and
    // outranks every annotation on it; the dashed border and dimming (see
    // `.lobby-card.foreign`) already say "not yours" before any text is read,
    // so the tag is explanatory detail and belongs on the detail line.
    const foreignTag = m.foreign
      ? `<span class="lobby-foreign-tag" title="Running in another orchestrator2 window${
           m.port ? ' on port ' + m.port : ''}${
           m.started ? ', since ' + _esc(m.started) : ''
         }. This window can't drive it — open it there, or close it there to free it.">other window${
           m.port ? ' :' + m.port : ''}</span>`
      : '';

    // Assembled as a list so an absent part takes its separator with it: the
    // rid is empty for a foreign session, which used to leave a dangling "·".
    // The viewer count is omitted when it is null -- a session owned by a hub
    // we could not reach has no knowable viewer count, and "0 viewers" would
    // state as fact something we have no way to establish.
    //
    // The foreign tag is NOT one of these parts: it is a bordered pill, not a
    // field in a `a · b · c` run, and at ~120px it pushed the run past the
    // card width and wrapped -- stranding a separator at the end of the first
    // line with nothing after it. It gets its own row, like the countdown
    // badge below.
    const metaParts = [];
    if (viewers !== null) metaParts.push(`<span>${_esc(viewerLabel)}</span>`);
    metaParts.push(`<span>${activity ? 'active ' + activity : 'idle'}</span>`);
    if (m.rid) metaParts.push(`<span class="lobby-card-rid">${_esc(m.rid)}</span>`);
    if (acct) metaParts.push(`<span class="lobby-card-acct">${_esc(acct)}</span>`);

    card.innerHTML = `
      <div class="lobby-card-top">
        <span class="lobby-dot ${dotCls}" title="${_esc(dotTitle)}"></span>
        <a class="lobby-card-title" title="${_esc(title)}"
           href="${_esc(m.rid ? '/?rid=' + encodeURIComponent(m.rid)
                              : _sessionHref(m.session_id || '', m.cwd || '',
                                             m.account || ''))}"
           >${_esc(title)}</a>
        ${isCurrent ? '<span class="lobby-current-tag">current</span>' : ''}
        ${m.foreign ? '' : `<button class="lobby-card-close" type="button"
                aria-label="Close this session"
                title="Close this session (stops its Claude process; history is kept)"
                >&times;</button>`}
      </div>
      <div class="lobby-card-cwd" title="${_esc(m.cwd || '')}">${_esc(m.cwd || '--')}</div>
      ${foreignTag ? `<div class="lobby-card-tags">${foreignTag}</div>` : ''}
      <div class="lobby-card-meta">
        ${metaParts.join('<span class="lobby-card-sep">·</span>')}
      </div>
      ${countdown}`;
    return card;
  }

  function _renderRecent(list) {
    elRecentCount.textContent = String(list.length);
    elRecent.innerHTML = '';
    const empty = list.length === 0;
    elRecentEmpty.classList.toggle('hidden', !empty);
    elRecent.classList.toggle('hidden', empty);
    for (const s of list) {
      elRecent.appendChild(_recentItem(s));
    }
  }

  function _recentItem(s) {
    const item = document.createElement('div');
    item.className = 'lobby-item';
    item.setAttribute('data-sid', s.session_id || '');
    if (s.cwd) item.setAttribute('data-cwd', s.cwd);
    if (s.account) item.setAttribute('data-account', s.account);

    const title = s.title || s.first_user_msg || (
      s.session_id ? s.session_id.substring(0, 8) : '(session)');
    // Short account tag for cross-account recent sessions.
    const acct = s.account
      ? s.account.replace(/\\/g, '/').split('/').pop() || ''
      : '';
    item.innerHTML = `
      <div class="lobby-item-main">
        <a class="lobby-item-title"
           href="${_esc(_sessionHref(s.session_id || '', s.cwd || '',
                                     s.account || ''))}"
           >${_esc(title)}</a>
        <span class="lobby-item-age">${_esc(s.age || '')}</span>
      </div>
      <div class="lobby-item-cwd" title="${_esc(s.cwd || '')}">${_esc(s.cwd || '--')}</div>
      ${acct ? `<div class="lobby-item-acct">${_esc(acct)}</div>` : ''}`;
    return item;
  }

  // --- Helpers ---------------------------------------------------------

  // Remaining time until an idle-teardown deadline (epoch seconds).
  function _fmtCountdown(epochSecs) {
    const secs = Math.floor(epochSecs - Date.now() / 1000);
    if (secs <= 0) return 'now';
    return Util.formatDuration(secs, 'compact');
  }

  // While the lobby is open, tick the "closing in …" badges once a second so
  // they count down smoothly between the server's ~2s session_list pushes.
  function _startCountdownTick() {
    _stopCountdownTick();
    _countdownTimer = setInterval(() => {
      const spans = elLobby.querySelectorAll('.lobby-countdown[data-deadline]');
      spans.forEach((el) => {
        const dl = parseFloat(el.getAttribute('data-deadline'));
        if (!dl) return;
        el.textContent = 'closing in ' + _fmtCountdown(dl);
        if (dl - Date.now() / 1000 <= 0) el.classList.add('imminent');
      });
    }, 1000);
  }

  function _stopCountdownTick() {
    if (_countdownTimer) { clearInterval(_countdownTimer); _countdownTimer = null; }
  }

  function _fmtAgo(epochSecs) {
    if (!epochSecs) return '';
    const secs = Math.max(0, Math.floor(Date.now() / 1000 - epochSecs));
    if (secs < 5) return 'now';
    if (secs < 60) return `${secs}s ago`;
    const m = Math.floor(secs / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ago`;
    return `${Math.floor(h / 24)}d ago`;
  }

  function _esc(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // Open a session by id, from wherever it was clicked — a "recent" row or a
  // card for a session running in another hub. Shared so those two can't
  // drift: the foreign card used to be unclickable, and the fix is only
  // correct if it lands in exactly the same place a recent click does.
  // The URL a session lives at, as a real `href`.
  //
  // Reported 2026-09-14: "on mobile (android chrome at least) i seem to only be
  // able to have one orchestrator2 tab open at a time".  True, and the cause
  // was that the cards were `<div>`s with click handlers.  A click handler has
  // no "open in new tab": long-press offers nothing, and neither does
  // ctrl/middle-click on a desktop.  The *only* way to reach a session was the
  // in-place switch, so one tab was all you could have.
  //
  // Making the title an anchor hands that back to the browser, which already
  // has the affordance on every platform and does it better than any control
  // this page could invent.  Normal taps still go through the delegated
  // handler (which preventDefaults), so nothing about the existing flow
  // changes -- this is purely a capability that was missing.
  function _sessionHref(sid, cwd, account) {
    let url = '/?open=' + encodeURIComponent(sid);
    if (cwd) url += '&cwd=' + encodeURIComponent(cwd);
    if (account) url += '&account=' + encodeURIComponent(account);
    return url;
  }

  function _openSessionById(sid, cwd, account) {
    // Mobile: resume it into this tab rather than a tab we can't focus.
    if (_isMobile()) { _openInPlace(sid, cwd || '', account || ''); return; }
    let url = '/?open=' + encodeURIComponent(sid);
    if (cwd) url += '&cwd=' + encodeURIComponent(cwd);
    if (account) url += '&account=' + encodeURIComponent(account);
    // If this session is already open in a tab, focus it without a reload;
    // otherwise open it fresh via ?open.
    _focusOrOpen(url, 'orch2-sess-' + sid);
  }

  // Is a sibling hub on *port* actually reachable from this device?
  //
  // `/api/whoami` is unauthenticated for LAN clients and cheap. A cross-origin
  // fetch we can't read is still proof the port answered, so `mode: 'no-cors'`
  // is enough — we only need "did anything pick up", not the body. Any failure
  // (refused, not routed, timed out) answers no, which is the safe direction:
  // the worst case is an extra sentence of explanation, versus navigating the
  // user to a blank page.
  async function _hubReachable(port, timeoutMs) {
    const url = location.protocol + '//' + location.hostname + ':' + port
              + '/api/whoami';
    const ctl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    const timer = setTimeout(() => { try { ctl && ctl.abort(); } catch (e) {} },
                             timeoutMs || 2500);
    try {
      await fetch(url, { mode: 'no-cors', cache: 'no-store',
                         signal: ctl ? ctl.signal : undefined });
      return true;
    } catch (e) {
      return false;
    } finally {
      clearTimeout(timer);
    }
  }

  // Called from app.js when the server refuses to open a session here because
  // it is already live in another hub window.  Offer to go to that window
  // (a same-tab navigation to the other hub's ``?open=<sid>``, which attaches
  // to its existing runtime) instead of forking a duplicate agent.
  async function onSessionElsewhere(msg) {
    const sid = msg.session_id || '';
    const since = msg.started ? ' (running since ' + msg.started + ')' : '';
    // A port only helps if this device can actually *reach* it. Two ways it
    // can't, and both used to end in a silent dead end:
    //
    //   * on a phone, the "window that already has it" is on a different
    //     machine — there is nothing here to switch to; and
    //   * remote clients typically reach the hub through one forwarded port,
    //     so a sibling hub on another port is simply not routable.
    //
    // Reported 2026-09-03: from mobile, answering "yes" navigated to an
    // unreachable origin and nothing happened at all. So probe first, and if
    // it isn't reachable say so instead of steering into a void.
    if (msg.port && !_isMobile() && await _hubReachable(msg.port)) {
      let url = location.protocol + '//' + location.hostname + ':' + msg.port
              + '/?open=' + encodeURIComponent(sid);
      if (msg.cwd) url += '&cwd=' + encodeURIComponent(msg.cwd);
      if (msg.account) url += '&account=' + encodeURIComponent(msg.account);
      const go = window.confirm(
        'That session is already open in another orchestrator2 window' + since
        + '.\n\nTwo windows can’t drive one session at once. Go to the '
        + 'window that already has it?');
      if (go) { window.location.href = url; return; }
    } else if (msg.port) {
      window.alert(
        'That session is already open in another orchestrator2 window' + since
        + ', on port ' + msg.port + ' of the machine running the hub.\n\n'
        + (_isMobile()
            ? 'That window is on another device, so this one can’t switch to '
              + 'it.'
            : 'This device can’t reach that port — it’s most likely not '
              + 'forwarded to you.')
        + '\n\nClose the session in that window to free it, then open it here.');
    } else {
      // The holder isn't a hub window this page can focus — most likely a
      // `claude --resume` running in a terminal, or an orphaned process.
      window.alert(
        'That session is already open in another process (PID '
        + (msg.holder_pid || '?') + ')' + since + '.\n\n'
        + 'It isn’t an orchestrator2 window this page can open for you — '
        + 'close that process to resume the session here.');
    }
    // Declined, or nothing to focus: this tab never attached, so surface the
    // session list to pick something else.
    if (!_hasSession) _setLanding(true);
    show();
  }

  return { init, open, openHub, show, hide, isVisible,
           onAttached, onSessionClosed, render, expectSession, showNotice,
           onSessionElsewhere,
           onReconnect, showReloadingAndPoll: _showReloadingAndPoll };
})();
