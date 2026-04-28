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
    if (panels.bg_tasks != null || panels.completed_bg != null) {
      const running = (panels.bg_tasks || []).map(t => ({
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
      const name = t.name || '(unnamed)';
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

  let _queueEditing = false;  // true while user is editing a queue item

  function _renderQueue(queue) {
    const n = queue.length;
    elQueueCount.textContent = n;
    elQueueCount.classList.toggle('active', n > 0);

    // Don't clobber the DOM while the user is editing a queue item.
    if (_queueEditing) return;

    // Show/hide the entire panel section based on whether there are items.
    elQueueSection.classList.toggle('hidden', n === 0);

    if (n === 0) {
      elQueueBody.innerHTML = '';
      return;
    }

    // Render newest-first (stack order).
    let html = '';
    for (let i = n - 1; i >= 0; i--) {
      const item = queue[i];
      const text = item.text || '';
      html += `<div class="queue-item" data-index="${item.index}">
        <div class="queue-item-text" title="${_esc(text)}">${_esc(text)}</div>
        <div class="queue-item-actions">
          <button class="queue-edit-btn" data-index="${item.index}" title="Edit">\u270E</button>
          <button class="queue-delete-btn" data-index="${item.index}" title="Delete">\u2715</button>
        </div>
      </div>`;
    }
    elQueueBody.innerHTML = html;

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

  function _exitEditing() {
    _queueEditing = false;
  }

  function _startEditQueueItem(index) {
    const item = elQueueBody.querySelector(`.queue-item[data-index="${index}"]`);
    if (!item) return;
    const textEl = item.querySelector('.queue-item-text');
    const actionsEl = item.querySelector('.queue-item-actions');
    // Find the full text from the title attribute.
    const fullText = textEl.title || textEl.textContent;

    _queueEditing = true;

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

    item.querySelector('.queue-save-btn').addEventListener('click', async () => {
      const newText = textarea.value.trim();
      if (!newText) { _exitEditing(); return; }
      try {
        const resp = await fetch('/api/queue/edit', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({index, text: newText}),
        });
        const result = await resp.json();
        if (!result.ok) console.warn('queue edit failed:', result.error);
      } catch (err) {
        console.error('queue edit error:', err);
      }
      _exitEditing();
    });

    item.querySelector('.queue-cancel-btn').addEventListener('click', () => {
      _exitEditing();
      item.innerHTML = original;
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
    const editBtn = item.querySelector('.queue-edit-btn');
    const deleteBtn = item.querySelector('.queue-delete-btn');
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
      let check;
      if (status === 'completed') check = '\u2611';
      else if (status === 'in_progress') check = '\u25B6';
      else check = '\u2610';

      html += `<div class="todo-item ${status}">
        <span class="todo-check">${check}</span>
        <span class="todo-text">${_esc(t.content || '')}</span>
      </div>`;
    }
    elTodosBody.innerHTML = html;
  }

  // ----- Helpers -----

  function _esc(s) {
    if (!s) return '';
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  return { init, update, updateQueue };
})();
