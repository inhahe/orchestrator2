/* app.js — Application entry point and WebSocket manager
 *
 * Connects to the server's WebSocket, dispatches incoming messages
 * to the appropriate module, and provides the global App namespace
 * for cross-module communication.
 */

const App = (() => {
  let ws = null;
  let wsUrl = null;
  let reconnectTimer = null;
  let reconnectDelay = 1000;
  let reconnectAttempt = 0;
  let _isBusy = false;
  let _serverShutdown = false;
  let _didLaunchRequest = false;   // sent the one-shot ?open/?new request?
  let _promptWatchdog = null;      // detects a prompt that got no server reply
  let _pendingSends = [];          // user messages typed while disconnected
  const MAX_RECONNECT_DELAY = 30000;
  const MAX_RECONNECT_ATTEMPTS = 20;
  const PROMPT_WATCHDOG_MS = 8000; // ~how long a turn should take to start

  function init() {
    // Init all modules.
    Status.init();
    Panels.init();
    Commands.init();
    Chat.init();
    Lobby.init();

    // Modal.
    document.getElementById('modal-close').addEventListener('click', closeModal);
    document.querySelector('.modal-backdrop').addEventListener('click', closeModal);
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeModal();
    });

    // Sidebar resize.
    _initSidebarResize();

    // Sidebar toggle (mobile).
    _initSidebarToggle();

    // Connect WebSocket.
    _connect();

    // Focus input.
    Commands.focus();
  }

  // --- Sidebar toggle (mobile) ---

  function _initSidebarToggle() {
    const btn = document.getElementById('sidebar-toggle');
    const sidebar = document.getElementById('sidebar');
    if (!btn || !sidebar) return;

    // Create a backdrop overlay for closing the sidebar on mobile.
    const backdrop = document.createElement('div');
    backdrop.id = 'sidebar-backdrop';
    document.body.appendChild(backdrop);

    function toggleSidebar() {
      const visible = sidebar.classList.toggle('mobile-visible');
      backdrop.classList.toggle('visible', visible);
    }
    function closeSidebar() {
      sidebar.classList.remove('mobile-visible');
      backdrop.classList.remove('visible');
    }

    btn.addEventListener('click', toggleSidebar);
    backdrop.addEventListener('click', closeSidebar);
  }

  // --- Sidebar resize ---

  function _initSidebarResize() {
    const sidebar = document.getElementById('sidebar');
    const handle = document.getElementById('sidebar-resize');
    if (!sidebar || !handle) return;

    // Restore persisted width.
    const saved = localStorage.getItem('sidebar-width');
    if (saved) {
      const w = parseInt(saved, 10);
      if (w >= 180 && w <= 600) {
        sidebar.style.width = w + 'px';
      }
    }

    let dragging = false;
    let startX = 0;
    let startW = 0;

    handle.addEventListener('mousedown', (e) => {
      e.preventDefault();
      dragging = true;
      startX = e.clientX;
      startW = sidebar.offsetWidth;
      handle.classList.add('dragging');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
    });

    document.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      const delta = e.clientX - startX;
      let newW = Math.max(180, Math.min(600, startW + delta));
      sidebar.style.width = newW + 'px';
    });

    document.addEventListener('mouseup', () => {
      if (!dragging) return;
      dragging = false;
      handle.classList.remove('dragging');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      // Persist.
      localStorage.setItem('sidebar-width', String(sidebar.offsetWidth));
    });
  }

  // --- WebSocket ---

  function _connect() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const params = new URLSearchParams(location.search);
    // Forward a ?rid=<rid> from the page URL so a tab launched into a running
    // hub attaches to that specific session instead of the hub's default one.
    const rid = params.get('rid');
    // ?open=<sid> / ?new[=1] launch a tab straight into a disk session or a
    // fresh one: the tab lands in the lobby, then sends open/new on connect.
    const openSid = params.get('open');
    const newFlag = params.get('new');
    const cwd = params.get('cwd');
    const account = params.get('account');
    const lobbyFlag = params.get('lobby');
    wsUrl = `${protocol}//${location.host}/ws`;
    if (rid) wsUrl += `?rid=${encodeURIComponent(rid)}`;
    else if (lobbyFlag) wsUrl += `?lobby=1`;

    // Don't flash the landing lobby while we wait for the open/new to attach.
    if (!rid && (openSid || newFlag)) Lobby.expectSession();

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      console.log('WebSocket connected');
      reconnectDelay = 1000;
      reconnectAttempt = 0;
      // The server drops this ws from its lobby-watcher set on disconnect, so
      // forget any prior subscription — the next render()/show() must re-send
      // lobby_watch to resume live updates on this fresh socket.
      if (window.Lobby && Lobby.onReconnect) Lobby.onReconnect();
      // Flush any prompts the user typed while the socket was down.
      _flushPending();
      // Drive an open/new request once (only on the first connect, not on
      // reconnects — by then the tab has a ?rid= URL and re-attaches normally).
      if (!rid && !_didLaunchRequest) {
        _didLaunchRequest = true;
        if (openSid) {
          const openMsg = { type: 'open', session_id: openSid };
          if (cwd) openMsg.cwd = cwd;
          if (account) openMsg.account = account;
          send(openMsg);
        } else if (newFlag) {
          send(cwd ? { type: 'new', cwd } : { type: 'new' });
        }
      }
    };

    ws.onclose = (e) => {
      console.log('WebSocket closed:', e.code, e.reason);
      _showConnectionStatus('disconnected');
      _scheduleReconnect();
    };

    ws.onerror = (e) => {
      console.error('WebSocket error:', e);
    };

    ws.onmessage = (e) => {
      let msg;
      try {
        msg = JSON.parse(e.data);
      } catch (err) {
        console.error('Invalid JSON from server:', e.data);
        return;
      }
      _dispatch(msg);
    };
  }

  function _scheduleReconnect() {
    if (_serverShutdown) return;
    if (reconnectTimer) return;
    reconnectAttempt++;

    if (reconnectAttempt > MAX_RECONNECT_ATTEMPTS) {
      _serverShutdown = true;
      Status.update({ busy_label: 'disconnected', busy_class: 'shutdown' });
      Chat.handleMessage({
        type: 'system_msg',
        subtype: 'error',
        data: { message: 'Lost connection to server (not running). '
                         + 'Type /connect to retry once it\u2019s back up.' },
      });
      return;
    }

    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      _connect();
      reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY);
    }, reconnectDelay);
  }

  function _showConnectionStatus(status) {
    if (_serverShutdown) return;
    if (status === 'disconnected') {
      // Immediate drop reads "disconnected"; once retries are under way show
      // the attempt counter so the user sees it's actively reconnecting.
      const label = reconnectAttempt > 1
        ? `disconnected — reconnecting (${reconnectAttempt}/${MAX_RECONNECT_ATTEMPTS})`
        : 'disconnected';
      Status.update({ busy_label: label, busy_class: 'reconnecting' });
    }
  }

  function send(msg) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      // Show user message immediately (optimistic) — but only when not
      // busy.  When busy, the prompt goes to the pending queue panel
      // and gets echoed to chat by the backend when it's actually
      // processed.  Tag the wire payload with the echo decision so the
      // backend knows whether it needs to echo too — without this flag
      // the backend can't tell whether a stale ``_isBusy=true`` caused
      // us to skip the echo (in which case it must echo) or whether
      // we already did it (in which case it must not, to avoid a
      // duplicate).
      const willEcho = (
        msg.type === 'message' &&
        msg.text &&
        !msg.text.startsWith('/') &&
        !_isBusy
      );
      if (msg.type === 'message') {
        msg = Object.assign({}, msg, { client_echoed: !!willEcho });
      }
      ws.send(JSON.stringify(msg));
      if (willEcho) {
        Chat.handleMessage({ type: 'user_message', content: msg.text });
        // Watchdog: a real prompt should draw *some* server reply quickly
        // (turn goes "working", or a queue/"still starting" notice arrives).
        // If nothing comes back, the socket is likely half-open (server died
        // but the browser still reports OPEN, so send() silently succeeds) or
        // the backend is wedged — either way the user must be told rather than
        // left staring at an idle bar.  Any inbound message clears this.
        _armPromptWatchdog();
      }
    } else if (msg.type === 'message') {
      // Socket is down — don't drop the user's prompt.  Queue it and flush on
      // the next successful connect (ws.onopen → _flushPending), and kick a
      // fresh reconnect in case auto-reconnect had latched off.
      _pendingSends.push(msg);
      Chat.handleMessage({
        type: 'system_msg',
        subtype: 'info',
        data: { message: 'Not connected \u2014 queued; will send on reconnect: '
                         + msg.text },
      });
      reconnect();
    } else {
      // Non-prompt control messages (interrupt, permission_response) are stale
      // after a drop, so there's nothing useful to queue — just report.
      Chat.handleMessage({
        type: 'system_msg',
        subtype: 'error',
        data: { message: 'Not connected to server.' }
      });
    }
  }

  // Flush prompts queued while disconnected, in order.  Re-runs them through
  // send() so the normal optimistic-echo / watchdog logic applies once.
  function _flushPending() {
    if (!_pendingSends.length) return;
    const queued = _pendingSends.slice();
    _pendingSends = [];
    for (const m of queued) send(m);
  }

  // Re-establish the browser\u2194server WebSocket on demand (e.g. from /connect).
  // If the socket is alive this is a *server-side* SDK-bridge reconnect, so
  // forward the command as usual.  If it's down \u2014 possibly latched off after
  // auto-reconnect gave up \u2014 clear the latch and rebuild the socket so the
  // user isn't stuck needing a manual page reload.
  function reconnect() {
    if (ws && ws.readyState === WebSocket.OPEN) {
      send({ type: 'message', text: '/connect' });
      return;
    }
    _serverShutdown = false;
    reconnectAttempt = 0;
    reconnectDelay = 1000;
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    // A socket already handshaking will flush the queue on its own onopen;
    // don't open a second one that would orphan the first.
    if (ws && ws.readyState === WebSocket.CONNECTING) return;
    Status.update({ busy_label: 'reconnecting', busy_class: 'reconnecting' });
    _connect();
  }

  function _armPromptWatchdog() {
    _clearPromptWatchdog();
    _promptWatchdog = setTimeout(() => {
      _promptWatchdog = null;
      if (_isBusy || _serverShutdown) return;   // turn started / already handled
      Chat.handleMessage({
        type: 'system_msg',
        subtype: 'error',
        data: { message:
          'No response from the server \u2014 it may have stopped. '
          + 'Reconnecting\u2026' },
      });
      // Force the socket to notice it's dead: closing triggers onclose, which
      // flips the status bar to "reconnecting" and drives the reconnect loop.
      // If the server is actually alive, the reconnect re-attaches cleanly.
      try { if (ws) ws.close(); } catch (e) {}
    }, PROMPT_WATCHDOG_MS);
  }

  function _clearPromptWatchdog() {
    if (_promptWatchdog) { clearTimeout(_promptWatchdog); _promptWatchdog = null; }
  }

  // --- Message dispatch ---

  function _dispatch(msg) {
    const type = msg.type;

    // Any message from the server proves it's alive and responsive, so a
    // pending prompt is being handled — cancel the "no response" watchdog.
    _clearPromptWatchdog();

    // Lobby: the server's list of running + recent sessions.
    if (type === 'session_list') {
      Lobby.render(msg);
      return;
    }

    // Lobby: a visible banner (e.g. "this tab's session is no longer running").
    // Forces the lobby open so the message isn't hidden behind the chat.
    if (type === 'lobby_notice') {
      Lobby.showNotice(msg.message || '');
      return;
    }

    // Lobby: this tab is now attached to (viewing) a specific session.
    // Clears any leftover chat before the fresh history/status arrives.
    if (type === 'attached') {
      // Update the URL to ?rid=<rid> so a refresh re-attaches directly
      // (instead of re-opening, which would fork the session).
      const s = msg.session || {};
      if (s.rid) {
        try {
          const u = new URL(location.href);
          u.searchParams.delete('open');
          u.searchParams.delete('new');
          u.searchParams.delete('cwd');
          u.searchParams.delete('account');
          u.searchParams.set('rid', s.rid);
          history.replaceState(null, '', u.toString());
        } catch (_) {}
      }
      Lobby.onAttached(msg.session);
      // A /switch that finished lands here (server re-attached this socket to
      // the copied session's runtime) — close the switch overlay.
      if (window.Switch) Switch.close();
      return;
    }

    // /switch: account list for the picker overlay.
    if (type === 'switch_accounts') {
      if (window.Switch) Switch.renderAccounts(msg);
      return;
    }

    // /switch: the copy/switch failed — show it in the overlay.
    if (type === 'switch_error') {
      if (window.Switch) Switch.error(msg);
      return;
    }

    // /switch: server accepted the copy; `attached` will follow and close the
    // overlay.  No action needed here (kept for protocol clarity).
    if (type === 'switch_done') {
      return;
    }

    // Status updates go to the status bar + panels.
    if (type === 'status_update') {
      if (msg.status) {
        Status.update(msg.status);
        // Track busy state for queue routing and input styling.
        _isBusy = msg.status.busy_class === 'working' ||
                  msg.status.busy_class === 'connecting';
        Commands.setBusy(_isBusy);
        Panels.setBusy(_isBusy);
      }
      if (msg.panels) {
        Panels.update(msg.panels);
      }
      return;
    }

    // Panel updates (standalone).
    if (type === 'panel_update') {
      Panels.update(msg.panels || msg);
      return;
    }

    // Queue update (pending prompts changed).
    if (type === 'queue_update') {
      Panels.updateQueue(msg.queue || []);
      return;
    }

    // Completion list.
    if (type === 'completion_list') {
      Commands.setCommands(msg.commands || []);
      return;
    }

    // Permission request — show a dialog.
    if (type === 'permission_request') {
      _showPermissionDialog(msg);
      return;
    }

    // Server-initiated page navigation (e.g. /resume opening the picker).
    if (type === 'navigate') {
      window.location.href = msg.url;
      return;
    }

    // Server is restarting — show a "restarting…" splash and reload once the
    // fresh process is serving (rather than treating it as a hard shutdown).
    if (type === 'server_restart') {
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
      _serverShutdown = true;  // suppress the normal auto-reconnect
      if (window.Lobby && Lobby.showReloadingAndPoll) Lobby.showReloadingAndPoll();
      return;
    }

    // Server is shutting down — show permanent status and stop reconnecting.
    if (type === 'server_shutdown') {
      _serverShutdown = true;
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      Status.update({
        busy_label: 'server stopped',
        busy_class: 'shutdown',
      });
      Chat.handleMessage({
        type: 'system_msg',
        subtype: 'info',
        data: { message: msg.reason || 'Server shut down.' },
      });
      return;
    }

    // Everything else → chat rendering.
    Chat.handleMessage(msg);
  }

  // --- Permission dialog ---

  function _showPermissionDialog(msg) {
    const toolName = msg.tool_name || 'Unknown tool';
    const toolInput = msg.tool_input || {};

    const body = document.getElementById('modal-body');
    const title = document.getElementById('modal-title');
    title.textContent = `Permission: ${toolName}`;

    let inputHtml = '';
    if (toolName === 'Bash') {
      inputHtml = `<div style="margin-bottom:8px;"><strong>Command:</strong></div>
                   <pre style="background:#0a0a1a; padding:8px; border-radius:4px; margin-bottom:12px;">${_esc(toolInput.command || '')}</pre>`;
    } else {
      inputHtml = `<pre style="background:#0a0a1a; padding:8px; border-radius:4px; margin-bottom:12px;">${_esc(JSON.stringify(toolInput, null, 2))}</pre>`;
    }

    body.innerHTML = `
      <div style="margin-bottom:12px;">
        <strong>${_esc(toolName)}</strong> wants to execute:
      </div>
      ${inputHtml}
      <div style="display:flex; gap:12px; margin-top:16px;">
        <button id="perm-allow" style="flex:1; padding:8px 16px; background:var(--green-dim); color:#fff; border:none; border-radius:4px; cursor:pointer; font-size:14px;">Allow</button>
        <button id="perm-deny" style="flex:1; padding:8px 16px; background:var(--red-dim); color:#fff; border:none; border-radius:4px; cursor:pointer; font-size:14px;">Deny</button>
      </div>`;

    document.getElementById('perm-allow').addEventListener('click', () => {
      send({ type: 'permission_response', allow: true });
      closeModal();
    });
    document.getElementById('perm-deny').addEventListener('click', () => {
      send({ type: 'permission_response', allow: false });
      closeModal();
    });

    document.getElementById('detail-modal').classList.remove('hidden');
  }

  // --- Modal ---

  // If `el` lives inside a collapsed activity group, expand that group so
  // the element is actually visible before we scroll to it.  Without this,
  // clicking a panel item whose scroll block was folded into an activity
  // group would scroll to a `display:none` element and show nothing.
  function _revealInScroll(el) {
    const group = el.closest('.activity-group.collapsed');
    if (group) {
      group.classList.remove('collapsed');
      const toggle = group.querySelector('.activity-group-toggle');
      if (toggle) toggle.textContent = toggle.textContent.replace(/^\u25B6/, '\u25BC');
    }
  }

  function showToolDetail(toolUseId) {
    // For now, show the tool block's detail view.
    // In the future, could fetch via /api/show.
    const el = document.querySelector(`[data-tool-use-id="${toolUseId}"]`);
    if (el) {
      _revealInScroll(el);
      const detail = el.querySelector('.tool-detail');
      if (detail) {
        detail.classList.add('open');
        el.querySelector('.tool-expand-icon')?.classList.add('open');
      }
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }

  function showBgDetail(taskId) {
    // Find the bg task element in chat (prefer bg_complete over bg_started).
    const all = document.querySelectorAll(`[data-bg-task-id="${taskId}"]`);
    const el = all.length > 0 ? all[all.length - 1] : null;
    if (el) {
      _revealInScroll(el);
      const detail = el.querySelector('.tool-detail');
      if (detail) {
        detail.classList.add('open');
        el.querySelector('.tool-expand-icon')?.classList.add('open');
      }
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }

  function openModal(title, content) {
    document.getElementById('modal-title').textContent = title;
    document.getElementById('modal-body').textContent = content;
    document.getElementById('detail-modal').classList.remove('hidden');
  }

  function closeModal() {
    document.getElementById('detail-modal').classList.add('hidden');
  }

  function _esc(s) {
    if (!s) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  // --- Boot ---

  document.addEventListener('DOMContentLoaded', init);

  return { send, reconnect, showToolDetail, showBgDetail, openModal, closeModal };
})();
