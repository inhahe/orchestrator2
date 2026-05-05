/* panels.js — Sidebar panel rendering
 *
 * Renders the four sidebar panels: Tools, Background Tasks, Pending Queue,
 * and Todos.
 * Updated whenever a panel_update or status_update arrives via WebSocket.
 */

const Panels = (() => {
  let elToolsBody, elToolsCount;
  let elBgBody, elBgCount;
  let elQueueSection, elQueueBody, elQueueCount;
  let elTodosBody, elTodosCount;
  let _busy = false;

  function init() {
    elToolsBody  = document.getElementById('panel-tools-body');
    elToolsCount = document.getElementById('panel-tools-count');
    elBgBody     = document.getElementById('panel-bg-body');
    elBgCount    = document.getElementById('panel-bg-count');
    elQueueSection = document.getElementById('panel-queue');
    elQueueBody  = document.getElementById('panel-queue-body');
    elQueueCount = document.getElementById('panel-queue-count');
    elTodosBody  = document.getElementById('panel-todos-body');
    elTodosCount = document.getElementById('panel-todos-count');

    // Toggle panel collapse on title click.
    document.querySelectorAll('.panel-title').forEach(el => {
      el.addEventListener('click', () => {
        const body = el.nextElementSibling;
        body.classList.toggle('hidden');
      });
    });
  }

  function update(panels) {
    if (!panels) return;

    // Merge active + grace-period completed tools.
    if (panels.active_tools != null || panels.completed_tools != null) {
      const active = (panels.active_tools || []).map(t => ({
        ...t, status: 'running', elapsed: _elapsed(t.started_at)
      }));
      const grace = (panels.completed_tools || []).map(t => ({
        ...t, status: t.is_error ? 'error' : 'complete'
      }));
      _renderTools([...active, ...grace]);
    }

    // Merge running + grace-period completed bg tasks.
    if (panels.background_tasks != null || panels.completed_bg != null) {
      const running = (panels.background_tasks || []).map(t => ({
        ...t, status: 'running', elapsed: _elapsed(t.started_at)
      }));
      const grace = (panels.completed_bg || []).map(t => ({
        ...t, status: t.is_error ? 'error' : 'complete'
      }));
      _renderBg([...running, ...grace]);
    }

    if (panels.queued_prompts != null) _renderQueue(panels.queued_prompts);
    if (panels.todos != null) _renderTodos(panels.todos);
  }

  /** Called directly by App when a queue_update WS message arrives. */
  function updateQueue(queue) {
    _renderQueue(queue || []);
  }

  function _elapsed(startedAt) {
    // startedAt is a monotonic timestamp — server sends elapsed strings
    // in some cases; if not, we just show a placeholder.
    return '';
  }

  // ----- Tools panel -----

  function _renderTools(tools) {
    const running = tools.filter(t => t.status === 'running');
    const grace = tools.filter(t => t.status !== 'running');

    elToolsCount.textContent = running.length;
    elToolsCount.classList.toggle('active', running.length > 0);

    if (tools.length === 0) {
      elToolsBody.innerHTML = '<div class="panel-empty">No active tools</div>';
      return;
    }

    let html = '';
    for (const t of tools) {
      const statusCls = t.status || 'running';
      const icon = statusCls === 'running' ? '\u25B6' :
                   statusCls === 'error' ? '\u2717' : '\u2713';
      const name = t.name || '?';
      const summary = _toolSummary(t);
      const dur = t.elapsed || t.duration || '';

      html += `<div class="panel-item ${statusCls}" data-tool-id="${_esc(t.tool_use_id || '')}"
                    title="${_esc(JSON.stringify(t.input || {}).substring(0, 200))}">
        <span class="item-icon">${icon}</span>
        <span class="item-name">${_esc(name)} ${_esc(summary)}</span>
        <span class="item-time">${_esc(dur)}</span>
      </div>`;
    }
    elToolsBody.innerHTML = html;

    // Click to open detail in modal.
    elToolsBody.querySelectorAll('.panel-item').forEach(el => {
      el.addEventListener('click', () => {
        const tid = el.dataset.toolId;
        if (tid) App.showToolDetail(tid);
      });
    });
  }

  function _toolSummary(t) {
    if (!t.header) return '';
    const h = t.header;
    if (h.name === 'Bash') return h.command_preview || h.description || '';
    if (h.name === 'Edit' || h.name === 'Write' || h.name === 'Read')
      return h.file_path || '';
    if (h.name === 'Grep') return h.pattern || '';
    if (h.name === 'Glob') return h.pattern || '';
    if (h.name === 'WebSearch') return h.query || '';
    if (h.name === 'WebFetch') return h.url || '';
    if (h.name === 'Agent' || h.name === 'Task') return h.description || '';
    return '';
  }

  // ----- Background tasks panel -----

  function _renderBg(tasks) {
    const running = tasks.filter(t => t.status === 'running');
    elBgCount.textContent = running.length;
    elBgCount.classList.toggle('active', running.length > 0);

    if (tasks.length === 0) {
      elBgBody.innerHTML = '<div class="panel-empty">No background tasks</div>';
      return;
    }

    let html = '';
    for (const t of tasks) {
      const isRunning = t.status === 'running';
      const icon = isRunning ? '\u25B6' : '\u2713';
      const cls = isRunning ? 'running' : 'complete';
      const name = t.name || t.command || '(unnamed)';
      const dur = t.elapsed || t.duration || '';

      html += `<div class="panel-item ${cls}" data-bg-id="${_esc(t.task_id || '')}"
                    title="${_esc(t.summary || t.task_type || '')}">
        <span class="item-icon">${icon}</span>
        <span class="item-name">${_esc(name)}</span>
        <span class="item-time">${_esc(dur)}</span>
      </div>`;
    }
    elBgBody.innerHTML = html;

    elBgBody.querySelectorAll('.panel-item').forEach(el => {
      el.addEventListener('click', () => {
        const bid = el.dataset.bgId;
        if (bid) App.showBgDetail(bid);
      });
    });
  }

  // ----- Pending prompt queue panel -----

  let _editingIndex = -1;         // index being edited, or -1
  let _editingOriginalText = '';  // original text for matching after queue shifts

  function _renderQueue(queue) {
    const n = queue.length;
    elQueueCount.textContent = n;
    elQueueCount.classList.toggle('active', n > 0);

    // If the user is editing an item, check whether it's still in the queue.
    if (_editingIndex >= 0) {
      const newIdx = queue.findIndex(q => q.text === _editingOriginalText);
      if (newIdx < 0) {
        // The item was sent or deleted — close the editor.
        _editingIndex = -1;
        _editingOriginalText = '';
        // Clear server-side editing lock too.
        fetch('/api/queue/editing', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({index: null}),
        }).catch(() => {});
        // Flash a notice so the user knows why the editor closed.
        const item = elQueueBody.querySelector('.queue-item-editing');
        if (item) {
          item.innerHTML = '<div class="queue-sent-notice">Prompt was already sent</div>';
          setTimeout(() => _renderQueue(queue), 1500);
          return;
        }
      } else {
        // Still in queue — update tracked index in case it shifted.
        _editingIndex = newIdx;
        return;  // don't clobber the editor DOM
      }
    }

    // Show/hide the entire panel section based on whether there are items.
    elQueueSection.classList.toggle('hidden', n === 0);

    if (n === 0) {
      elQueueBody.innerHTML = '';
      return;
    }

    // Render in queue order (first-in at top, next to execute).
    let html = '';
    for (let i = 0; i < n; i++) {
      const item = queue[i];
      const text = item.text || '';
      const sendDisabled = _busy && i === 0;
      const disabledAttr = sendDisabled ? ' disabled' : '';
      const disabledCls = sendDisabled ? ' disabled' : '';
      const sendTitle = sendDisabled ? 'Will send after current turn' : 'Send now';
      html += `<div class="queue-item" data-index="${item.index}">
        <div class="queue-item-text" title="${_esc(text)}">${_esc(text)}</div>
        <div class="queue-item-actions">
          <button class="queue-send-btn${disabledCls}" data-index="${item.index}" title="${sendTitle}"${disabledAttr}>\u25B6</button>
          <button class="queue-edit-btn" data-index="${item.index}" title="Edit">\u270E</button>
          <button class="queue-delete-btn" data-index="${item.index}" title="Delete">\u2715</button>
        </div>
      </div>`;
    }
    // Merge button (only when 2+ items).
    if (n >= 2) {
      html += `<div class="queue-merge-row">
        <button class="queue-merge-btn" title="Combine all prompts into one">Merge all</button>
      </div>`;
    }

    elQueueBody.innerHTML = html;

    // Bind merge button.
    const mergeBtn = elQueueBody.querySelector('.queue-merge-btn');
    if (mergeBtn) {
      mergeBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        _mergeQueue();
      });
    }

    // Bind send buttons.
    elQueueBody.querySelectorAll('.queue-send-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        _sendQueueItem(parseInt(btn.dataset.index));
      });
    });

    // Bind delete buttons.
    elQueueBody.querySelectorAll('.queue-delete-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        _deleteQueueItem(parseInt(btn.dataset.index));
      });
    });

    // Bind edit buttons.
    elQueueBody.querySelectorAll('.queue-edit-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        _startEditQueueItem(parseInt(btn.dataset.index));
      });
    });
  }

  async function _mergeQueue() {
    try {
      const resp = await fetch('/api/queue/merge', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
      });
      const result = await resp.json();
      if (!result.ok) console.warn('queue merge failed:', result.error);
    } catch (err) {
      console.error('queue merge error:', err);
    }
  }

  async function _sendQueueItem(index) {
    try {
      const resp = await fetch('/api/queue/send', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({index}),
      });
      const result = await resp.json();
      if (!result.ok) console.warn('queue send failed:', result.error);
    } catch (err) {
      console.error('queue send error:', err);
    }
  }

  async function _deleteQueueItem(index) {
    try {
      const resp = await fetch('/api/queue/delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({index}),
      });
      const result = await resp.json();
      if (!result.ok) console.warn('queue delete failed:', result.error);
    } catch (err) {
      console.error('queue delete error:', err);
    }
  }

  function _exitEditing(notifyServer = true) {
    _editingIndex = -1;
    _editingOriginalText = '';
    if (notifyServer) {
      // Tell the server we're done editing so it can send the prompt.
      fetch('/api/queue/editing', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({index: null}),
      }).catch(() => {});
    }
  }

  function _notifyEditingStart(index) {
    fetch('/api/queue/editing', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({index}),
    }).catch(() => {});
  }

  function _startEditQueueItem(index) {
    const item = elQueueBody.querySelector(`.queue-item[data-index="${index}"]`);
    if (!item) return;
    const textEl = item.querySelector('.queue-item-text');
    // Find the full text from the title attribute.
    const fullText = textEl.title || textEl.textContent;

    _editingIndex = index;
    _editingOriginalText = fullText;
    item.classList.add('queue-item-editing');
    _notifyEditingStart(index);

    // Replace with a textarea + save/cancel.
    const original = item.innerHTML;
    item.innerHTML = `
      <textarea class="queue-edit-textarea" rows="3">${_esc(fullText)}</textarea>
      <div class="queue-edit-controls">
        <button class="queue-save-btn" title="Save">\u2713</button>
        <button class="queue-cancel-btn" title="Cancel">\u2715</button>
      </div>`;

    const textarea = item.querySelector('.queue-edit-textarea');
    textarea.focus();
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);

    item.querySelector('.queue-save-btn').addEventListener('click', () => {
      const newText = textarea.value.trim();
      if (!newText) { _exitEditing(); return; }
      const currentIndex = _editingIndex;
      // Close editor and show the new text immediately (optimistic).
      // Don't notify server here — /api/queue/edit handles the lock.
      _exitEditing(false);
      item.classList.remove('queue-item-editing');
      const textEl = document.createElement('div');
      textEl.className = 'queue-item-text';
      textEl.title = newText;
      textEl.textContent = newText;
      item.innerHTML = '';
      item.style.flexWrap = 'wrap';
      item.appendChild(textEl);

      // Animated dots indicator while the edit API call is in flight.
      const indicator = document.createElement('div');
      indicator.className = 'queue-saving-indicator';
      indicator.textContent = '.';
      item.appendChild(indicator);
      let dotCount = 1;
      const dotInterval = setInterval(() => {
        if (!indicator.isConnected) { clearInterval(dotInterval); return; }
        dotCount = (dotCount % 3) + 1;
        indicator.textContent = '.'.repeat(dotCount);
      }, 350);

      // Fire API call in the background — server broadcast will re-render.
      fetch('/api/queue/edit', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({index: currentIndex, text: newText}),
      }).catch(err => {
        console.error('queue edit error:', err);
      }).finally(() => {
        clearInterval(dotInterval);
        if (indicator.isConnected) indicator.remove();
      });
    });

    item.querySelector('.queue-cancel-btn').addEventListener('click', () => {
      _exitEditing();
      item.innerHTML = original;
      item.classList.remove('queue-item-editing');
      // Re-bind the buttons since we replaced innerHTML.
      _rebindQueueItem(item, index);
    });

    // Save on Enter (without Shift).
    textarea.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        item.querySelector('.queue-save-btn').click();
      }
      if (e.key === 'Escape') {
        item.querySelector('.queue-cancel-btn').click();
      }
    });
  }

  function _rebindQueueItem(item, index) {
    const sendBtn = item.querySelector('.queue-send-btn');
    const editBtn = item.querySelector('.queue-edit-btn');
    const deleteBtn = item.querySelector('.queue-delete-btn');
    if (sendBtn) {
      sendBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        _sendQueueItem(index);
      });
    }
    if (editBtn) {
      editBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        _startEditQueueItem(index);
      });
    }
    if (deleteBtn) {
      deleteBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        _deleteQueueItem(index);
      });
    }
  }

  // ----- Todos panel -----

  function _renderTodos(todos) {
    const pending = todos.filter(t => t.status !== 'completed');
    elTodosCount.textContent = todos.length;
    elTodosCount.classList.toggle('active', pending.length > 0);

    if (todos.length === 0) {
      elTodosBody.innerHTML = '<div class="panel-empty">No plan yet</div>';
      return;
    }

    let html = '';
    for (const t of todos) {
      const status = t.status || 'pending';
      let mark;
      if (status === 'completed') mark = '\u2713';       // ✓
      else if (status === 'in_progress') mark = '\u25B6'; // ▶
      else mark = '\u2022';                               // •

      html += `<div class="todo-item ${status}">
        <span class="todo-check">${mark}</span>
        <span class="todo-text">${_esc(t.content || '')}</span>
      </div>`;
    }

    // Show "Clear done" button when any items are completed.
    const done = todos.length - pending.length;
    if (done > 0) {
      html += `<div class="todo-clear-row">
        <button class="todo-clear-btn" title="Clear completed items">Clear done</button>
      </div>`;
    }

    elTodosBody.innerHTML = html;

    const clearBtn = elTodosBody.querySelector('.todo-clear-btn');
    if (clearBtn) {
      clearBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        _clearTodos();
      });
    }
  }

  async function _clearTodos() {
    try {
      const resp = await fetch('/api/todos/clear', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
      });
      const result = await resp.json();
      if (!result.ok) console.warn('todo clear failed:', result.error);
    } catch (err) {
      console.error('todo clear error:', err);
    }
  }

  // ----- Helpers -----

  function _esc(s) {
    if (!s) return '';
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function setBusy(busy) {
    _busy = busy;
    // Update send-button state on existing queue items.
    elQueueBody.querySelectorAll('.queue-send-btn').forEach((btn, i) => {
      btn.disabled = _busy && i === 0;
      btn.classList.toggle('disabled', _busy && i === 0);
    });
  }

  return { init, update, updateQueue, setBusy };
})();
