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
  const MAX_RECONNECT_DELAY = 30000;

  function init() {
    // Init all modules.
    Status.init();
    Panels.init();
    Commands.init();
    Chat.init();

    // Modal.
    document.getElementById('modal-close').addEventListener('click', closeModal);
    document.querySelector('.modal-backdrop').addEventListener('click', closeModal);
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeModal();
    });

    // Sidebar resize.
    _initSidebarResize();

    // Connect WebSocket.
    _connect();

    // Focus input.
    Commands.focus();
  }

  // --- Sidebar resize ---

  function _initSidebarResize() {
    const sidebar = document.getElementById('sidebar');
    const handle = document.getElementById('sidebar-resize');
    const inputBar = document.getElementById('input-bar');
    if (!sidebar || !handle) return;

    // Restore persisted width.
    const saved = localStorage.getItem('sidebar-width');
    if (saved) {
      const w = parseInt(saved, 10);
      if (w >= 180 && w <= 600) {
        sidebar.style.width = w + 'px';
        inputBar.style.marginLeft = (w + 4) + 'px';  // +4 for handle
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
      inputBar.style.marginLeft = (newW + 4) + 'px';
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
    wsUrl = `${protocol}//${location.host}/ws`;

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      console.log('WebSocket connected');
      reconnectDelay = 1000;
      reconnectAttempt = 0;
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
    if (reconnectTimer) return;
    reconnectAttempt++;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      _connect();
      reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY);
    }, reconnectDelay);
  }

  function _showConnectionStatus(status) {
    if (status === 'disconnected') {
      const label = reconnectAttempt > 1
        ? `reconnecting (${reconnectAttempt})`
        : 'reconnecting';
      Status.update({ busy_label: label, busy_class: 'reconnecting' });
    }
  }

  function send(msg) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(msg));

      // Show user message immediately (optimistic) — but only when not
      // busy.  When busy, the prompt goes to the pending queue panel
      // and gets echoed to chat when it's actually processed.
      if (msg.type === 'message' && msg.text && !msg.text.startsWith('/') && !_isBusy) {
        Chat.handleMessage({ type: 'user_message', content: msg.text });
      }
    } else {
      Chat.handleMessage({
        type: 'system_msg',
        subtype: 'error',
        data: { message: 'Not connected to server.' }
      });
    }
  }

  // --- Message dispatch ---

  function _dispatch(msg) {
    const type = msg.type;

    // Status updates go to the status bar + panels.
    if (type === 'status_update') {
      if (msg.status) {
        Status.update(msg.status);
        // Track busy state for queue routing and input styling.
        _isBusy = msg.status.busy_class === 'working' ||
                  msg.status.busy_class === 'connecting';
        Commands.setBusy(_isBusy);
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

  function showToolDetail(toolUseId) {
    // For now, show the tool block's detail view.
    // In the future, could fetch via /api/show.
    const el = document.querySelector(`[data-tool-use-id="${toolUseId}"]`);
    if (el) {
      const detail = el.querySelector('.tool-detail');
      if (detail) {
        detail.classList.add('open');
        el.querySelector('.tool-expand-icon')?.classList.add('open');
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    }
  }

  function showBgDetail(taskId) {
    // Send /show command.
    send({ type: 'message', text: `/show b${taskId}` });
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

  return { send, showToolDetail, showBgDetail, openModal, closeModal };
})();
