/* chat.js — Chat message rendering
 *
 * Handles all message types from the WebSocket and renders them
 * into the #messages container.  Supports:
 * - User messages
 * - Assistant text (streaming deltas)
 * - Tool use / tool result (expandable blocks)
 * - Thinking blocks (expandable)
 * - System messages
 * - Turn boundaries
 * - Session history replay
 */

const Chat = (() => {
  let elMessages;
  let _autoScroll = true;

  // Track the current streaming assistant text element.
  let _streamingEl = null;
  let _streamingText = '';

  // Map tool_use_id → DOM element for pairing results.
  const _toolBlocks = new Map();

  // --- Tool-block collapsing ---
  // Track consecutive tool blocks.  When more than _collapseThreshold
  // appear in a row (without intervening assistant text or user messages),
  // ALL of them are collapsed behind a "show N tools" toggle.
  let _collapseTools = true;          // toggled via /collapse or checkbox
  let _collapseThreshold = 3;         // configurable via dropdown
  let _consecutiveToolCount = 0;
  let _toolGroupContainer = null;  // wrapper div for collapsible group
  let _toolGroupToggle = null;     // the "show N more" element
  let _toolRunElements = [];       // tool elements appended before threshold

  function _resetToolRun() {
    _consecutiveToolCount = 0;
    _toolGroupContainer = null;
    _toolGroupToggle = null;
    _toolRunElements = [];
  }

  function _trackToolBlock(el) {
    if (!_collapseTools) {
      elMessages.appendChild(el);
      return;
    }
    _consecutiveToolCount++;

    if (_consecutiveToolCount <= _collapseThreshold) {
      // Below threshold: append normally but track the elements.
      _toolRunElements.push(el);
      elMessages.appendChild(el);
      return;
    }

    // Hit the threshold: create the collapsible group and retroactively
    // move ALL prior tool blocks from this run into it.
    if (!_toolGroupContainer) {
      _toolGroupContainer = document.createElement('div');
      _toolGroupContainer.className = 'tool-collapse-group collapsed';

      _toolGroupToggle = document.createElement('div');
      _toolGroupToggle.className = 'tool-collapse-toggle';

      // Capture references in closure — _resetToolRun() nulls the
      // module-level vars before the user ever clicks.
      const container = _toolGroupContainer;
      const toggle = _toolGroupToggle;
      toggle.addEventListener('click', () => {
        container.classList.toggle('collapsed');
        if (container.classList.contains('collapsed')) {
          const n = container.children.length;
          toggle.textContent = `\u22EF show ${n} tool${n !== 1 ? 's' : ''}`;
        } else {
          toggle.textContent = `\u22EF collapse`;
          // Scroll the expanded group into view.
          toggle.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      });

      // Insert toggle before the first tool element of this run,
      // then move all prior elements into the container.
      const firstEl = _toolRunElements[0];
      firstEl.parentNode.insertBefore(_toolGroupToggle, firstEl);
      for (const prev of _toolRunElements) {
        _toolGroupContainer.appendChild(prev);
      }
      _toolRunElements = [];
      elMessages.appendChild(_toolGroupContainer);
    }

    _toolGroupContainer.appendChild(el);
    const n = _toolGroupContainer.children.length;
    _toolGroupToggle.textContent = `\u22EF show ${n} tool${n !== 1 ? 's' : ''}`;
  }

  function init() {
    elMessages = document.getElementById('messages');

    // Auto-scroll tracking: only scroll if user is near bottom.
    elMessages.addEventListener('scroll', () => {
      const threshold = 80;
      const atBottom = (elMessages.scrollHeight - elMessages.scrollTop - elMessages.clientHeight) < threshold;
      _autoScroll = atBottom;
    });
  }

  function clear() {
    elMessages.innerHTML = '';
    _streamingEl = null;
    _streamingText = '';
    _toolBlocks.clear();
    _resetToolRun();
  }

  // --- Scrolling ---

  function _scrollToBottom() {
    if (_autoScroll) {
      elMessages.scrollTop = elMessages.scrollHeight;
    }
  }

  // --- Message dispatch ---

  function handleMessage(msg) {
    switch (msg.type) {
      case 'user_message':    _addUserMessage(msg.content); break;
      case 'assistant_text':  _addAssistantText(msg); break;
      case 'tool_use':        _addToolUse(msg); break;
      case 'tool_result':     _addToolResult(msg); break;
      case 'thinking':        _addThinking(msg); break;
      case 'turn_start':      _addTurnStart(msg); break;
      case 'turn_end':        _addTurnEnd(msg); break;
      case 'system_msg':      _addSystemMsg(msg); break;
      case 'bg_started':      _addBgStarted(msg); break;
      case 'bg_complete':     _addBgComplete(msg); break;
      case 'clear_screen':    clear(); break;
      case 'command_data':    _addCommandData(msg); break;
      case 'bell':            _playBell(); break;
      case 'history':         _replayHistory(msg.messages); break;
      default:
        console.warn('[chat] unhandled message type:', msg.type, msg);
        break;
    }
  }

  // --- User messages ---

  function _addUserMessage(content) {
    _flushStreaming();
    _resetToolRun();
    const el = document.createElement('div');
    el.className = 'msg msg-user';
    el.innerHTML = `<div class="msg-label">You</div>
                    <div class="msg-content">${_esc(content)}</div>`;
    elMessages.appendChild(el);
    // Force scroll — the user's own message should always be visible.
    // (The normal _autoScroll flag may be false due to the input textarea
    // expanding on paste, which shifts the layout and trips the scroll
    // listener before the message is appended.)
    _autoScroll = true;
    _scrollToBottom();
  }

  // --- Assistant text (streaming) ---

  function _addAssistantText(msg) {
    _resetToolRun();
    if (msg.delta) {
      // Streaming delta — append to current element.
      // Use plain text during streaming (fast), render markdown on flush.
      if (!_streamingEl) {
        _streamingEl = document.createElement('div');
        _streamingEl.className = 'msg msg-assistant';
        _streamingEl.innerHTML = '<div class="msg-content"></div>';
        elMessages.appendChild(_streamingEl);
        _streamingText = '';
      }
      _streamingText += msg.content;
      _streamingEl.querySelector('.msg-content').textContent = _streamingText;
    } else {
      // Non-delta: full text block (history or async).
      _flushStreaming();
      if (!msg.content) return;  // skip empty blocks
      const el = document.createElement('div');
      el.className = 'msg msg-assistant';
      el.innerHTML = `<div class="msg-content md-rendered">${_md(msg.content)}</div>`;
      elMessages.appendChild(el);
    }
    _scrollToBottom();
  }

  function _flushStreaming() {
    if (_streamingEl) {
      if (!_streamingText.trim()) {
        // Remove empty streaming element.
        _streamingEl.remove();
      } else {
        // Re-render with markdown now that the full text is available.
        const content = _streamingEl.querySelector('.msg-content');
        if (content) {
          content.innerHTML = _md(_streamingText);
          content.classList.add('md-rendered');
        }
      }
    }
    _streamingEl = null;
    _streamingText = '';
  }

  // --- Tool use blocks ---

  function _addToolUse(msg) {
    _flushStreaming();
    const el = document.createElement('div');
    el.className = 'msg msg-tool';
    el.dataset.toolUseId = msg.tool_use_id;

    const h = msg.header || {};
    const name = msg.name || h.name || '?';
    const summary = _toolHeaderSummary(h, msg.input);
    const statusIcon = msg.status === 'running' ? '\u25B6' :
                       msg.status === 'error' ? '\u2717' : '\u2713';
    const statusCls = msg.status || 'running';

    el.innerHTML = `
      <div class="tool-header">
        <span class="tool-status-icon ${statusCls}">${statusIcon}</span>
        <span class="tool-name">${_esc(name)}</span>
        <span class="tool-summary">${summary}</span>
        <span class="tool-size-hint" data-tool-use-id="${msg.tool_use_id || ''}"></span>
        <span class="tool-expand-icon">\u25B8</span>
      </div>
      <div class="tool-detail">
        ${_toolDetailHtml(name, msg.input || {}, msg)}
      </div>`;

    // Click to toggle expand.
    const header = el.querySelector('.tool-header');
    const detail = el.querySelector('.tool-detail');
    const expandIcon = el.querySelector('.tool-expand-icon');
    header.addEventListener('click', () => {
      detail.classList.toggle('open');
      expandIcon.classList.toggle('open');
      // Wire up synchronized scrolling for diff viewers on first open.
      if (detail.classList.contains('open') && !detail._diffWired) {
        detail._diffWired = true;
        detail.querySelectorAll('.diff-viewer').forEach(dv => Diff.syncScroll(dv));
      }
    });

    _trackToolBlock(el);
    _toolBlocks.set(msg.tool_use_id, el);
    _scrollToBottom();
  }

  function _addToolResult(msg) {
    const isErr = msg.is_error || msg.status === 'error';
    const el = _toolBlocks.get(msg.tool_use_id);
    if (el) {
      // Update status icon (▶ → ✓ or ✗).
      const iconEl = el.querySelector('.tool-status-icon');
      if (iconEl) {
        iconEl.textContent = isErr ? '\u2717' : '\u2713';
        iconEl.className = 'tool-status-icon ' + (isErr ? 'error' : 'complete');
      }

      // On error, tint the tool name + summary red.
      if (isErr) {
        const nameEl = el.querySelector('.tool-name');
        if (nameEl) nameEl.style.color = 'var(--red)';
        const sumEl = el.querySelector('.tool-summary');
        if (sumEl) sumEl.style.color = 'var(--red)';
      }

      // Show size hint in the header.
      if (msg.size_hint) {
        const hintEl = el.querySelector('.tool-size-hint');
        if (hintEl) hintEl.textContent = msg.size_hint;
      }

      // Add result content to the expandable detail area.
      const detail = el.querySelector('.tool-detail');
      if (detail && msg.content) {
        const resultDiv = document.createElement('div');
        resultDiv.innerHTML = `
          <div class="detail-label">Result${isErr ? ' (error)' : ''}</div>
          <pre>${_esc(msg.content)}</pre>`;
        detail.appendChild(resultDiv);
      }
    } else {
      // Orphaned result — render minimal inline.
      const standalone = document.createElement('div');
      standalone.className = 'msg msg-tool';
      standalone.innerHTML = `
        <div class="tool-header">
          <span class="tool-status-icon ${isErr ? 'error' : 'complete'}">${isErr ? '\u2717' : '\u2713'}</span>
          <span class="tool-name" ${isErr ? 'style="color:var(--red)"' : ''}>${_esc(msg.name || '?')}</span>
          <span class="tool-size-hint">${_esc(msg.size_hint || '')}</span>
        </div>`;
      elMessages.appendChild(standalone);
    }
    _scrollToBottom();
  }

  // --- Thinking blocks ---

  function _addThinking(msg) {
    _flushStreaming();
    const text = msg.content || '';
    const lines = text.split('\n');
    const nLines = lines.length;
    const nChars = text.length;
    // If single-line and short enough, show the full text inline;
    // otherwise show a size hint like tool headers do.
    const isSingleLine = nLines === 1;

    const el = document.createElement('div');
    el.className = 'msg msg-thinking';
    // Build the summary span — actual content chosen after DOM insertion
    // so we can measure available width.
    el.innerHTML = `
      <div class="thinking-header">
        <span>\u{1F4AD}</span>
        <span>Thinking</span>
        <span class="thinking-summary" style="color:var(--text-muted); font-size:11px;"></span>
        <span class="tool-expand-icon">\u25B8</span>
      </div>
      <div class="thinking-body">${_esc(text)}</div>`;

    const header = el.querySelector('.thinking-header');
    const body = el.querySelector('.thinking-body');
    const icon = el.querySelector('.tool-expand-icon');
    const summaryEl = el.querySelector('.thinking-summary');
    header.addEventListener('click', () => {
      body.classList.toggle('open');
      icon.classList.toggle('open');
    });

    elMessages.appendChild(el);

    // Decide summary text after insertion so we can measure width.
    if (isSingleLine && nChars > 0) {
      // Try fitting the full text; measure against the header's available space.
      summaryEl.textContent = text;
      if (summaryEl.scrollWidth > summaryEl.clientWidth) {
        // Doesn't fit — fall back to size hint.
        summaryEl.textContent = `(${nChars} chars)`;
      }
    } else if (nChars > 0) {
      summaryEl.textContent = `(${nChars} chars, ${nLines} lines)`;
    }

    _scrollToBottom();
  }

  // --- Background task events ---

  function _addBgStarted(msg) {
    const el = document.createElement('div');
    const name = _esc(msg.name || msg.task_type || 'unknown');
    const cmd = msg.command;

    if (cmd) {
      // Expandable: click to show the full command.
      el.className = 'msg msg-tool';
      el.innerHTML = `
        <div class="tool-header">
          <span class="tool-status-icon running">\u25B6</span>
          <span class="tool-name">Background task started</span>
          <span class="tool-summary">${name}</span>
          <span class="tool-expand-icon">\u25B8</span>
        </div>
        <div class="tool-detail">
          <div class="detail-label">Command</div>
          <pre>${_esc(cmd)}</pre>
        </div>`;
      const header = el.querySelector('.tool-header');
      const detail = el.querySelector('.tool-detail');
      const expandIcon = el.querySelector('.tool-expand-icon');
      header.addEventListener('click', () => {
        detail.classList.toggle('open');
        expandIcon.classList.toggle('open');
      });
    } else {
      el.className = 'msg msg-system';
      el.textContent = `\u25B6 Background task started: ${name}`;
    }

    if (msg.task_id) el.dataset.bgTaskId = msg.task_id;
    elMessages.appendChild(el);
    _scrollToBottom();
  }

  function _addBgComplete(msg) {
    const el = document.createElement('div');
    const isErr = msg.status === 'error' || msg.status === 'failed';
    const name = _esc(msg.name || 'unknown');
    const cmd = msg.command;
    const summary = msg.summary || '';
    const icon = isErr ? '\u2717' : '\u2713';
    const statusCls = isErr ? 'error' : 'complete';
    const label = isErr ? 'Background task failed' : 'Background task completed';

    if (cmd || summary) {
      // Expandable: click to show command and/or output.
      el.className = 'msg msg-tool';
      let detailHtml = '';
      if (cmd) {
        detailHtml += `<div class="detail-label">Command</div>
                       <pre>${_esc(cmd)}</pre>`;
      }
      if (summary) {
        detailHtml += `<div class="detail-label">Output</div>
                       <pre>${_esc(summary)}</pre>`;
      }
      const summaryText = summary
        ? ` — ${_esc(summary.length > 120 ? summary.slice(0, 120) + '\u2026' : summary)}`
        : '';
      el.innerHTML = `
        <div class="tool-header">
          <span class="tool-status-icon ${statusCls}">${icon}</span>
          <span class="tool-name">${label}</span>
          <span class="tool-summary">${name}${summaryText}</span>
          <span class="tool-expand-icon">\u25B8</span>
        </div>
        <div class="tool-detail">
          ${detailHtml}
        </div>`;
      const header = el.querySelector('.tool-header');
      const detail = el.querySelector('.tool-detail');
      const expandIcon = el.querySelector('.tool-expand-icon');
      header.addEventListener('click', () => {
        detail.classList.toggle('open');
        expandIcon.classList.toggle('open');
      });
    } else {
      el.className = 'msg msg-system';
      el.innerHTML = `${icon} ${label}: ${name}`;
      if (isErr) el.style.color = 'var(--red)';
    }

    elMessages.appendChild(el);
    _scrollToBottom();
  }

  // --- Turn boundaries ---

  function _addTurnStart(msg) {
    _flushStreaming();
    // Mostly silent — could add a divider.
  }

  function _addTurnEnd(msg) {
    _flushStreaming();
    _resetToolRun();
    const el = document.createElement('div');
    el.className = 'msg-turn-end';
    el.textContent = `Turn ${msg.turns || '?'} completed (${msg.duration || '?'}) — ${msg.subtype || ''}`;
    elMessages.appendChild(el);
    _scrollToBottom();
  }

  // --- System messages ---

  function _addSystemMsg(msg) {
    const subtype = msg.subtype || '';
    const data = msg.data || {};

    // Suppress noisy / internal-only subtypes that have no user-visible content.
    const _silent = new Set([
      'init', 'session_state_changed', 'connected',
      'unknown', 'worker_stopped',
    ]);
    if (_silent.has(subtype) && !data.message) return;

    let text = data.message || data.content || msg.content || '';
    if (data.error) text = `API retry: ${data.error}`;

    // Don't render empty system messages (they show as blank dark rectangles).
    if (!text) return;

    const el = document.createElement('div');
    let cls = 'msg msg-system';
    if (subtype === 'error') cls += ' error';
    else if (subtype === 'warning') cls += ' warning';
    else if (subtype === 'done') cls += ' done';
    else if (subtype === 'waiting') cls += ' waiting';

    el.className = cls;
    el.textContent = text;

    elMessages.appendChild(el);
    _scrollToBottom();
  }

  // --- Command data (from immediate commands) ---

  function _addCommandData(msg) {
    const el = document.createElement('div');
    el.className = 'msg msg-command';

    const data = msg.data;
    if (typeof data === 'string') {
      el.textContent = data;
    } else if (data && typeof data === 'object') {
      el.textContent = JSON.stringify(data, null, 2);
    } else {
      el.textContent = String(data);
    }
    elMessages.appendChild(el);
    _scrollToBottom();
  }

  // --- History replay ---

  function _replayHistory(messages) {
    if (!messages || !messages.length) return;

    // Disable tool-collapse during history replay.
    const savedCollapse = _collapseTools;
    _collapseTools = false;

    // Add history separator.
    const sep = document.createElement('div');
    sep.className = 'msg msg-system';
    sep.textContent = `--- Session history (${messages.length} messages) ---`;
    elMessages.appendChild(sep);

    // Render in batches via setTimeout so the browser stays responsive
    // and WebSocket messages (status ticker, etc.) can still be processed.
    const BATCH = 50;
    let idx = 0;

    function _renderBatch() {
      const end = Math.min(idx + BATCH, messages.length);
      for (; idx < end; idx++) {
        const m = messages[idx];
        const type = m.type || m.role;
        if (type === 'user' || type === 'human') {
          _addUserMessage(m.content || m.text || '');
        } else if (type === 'assistant') {
          _addAssistantText({ content: m.content || m.text || '', delta: false });
        } else if (type === 'tool_use') {
          _addToolUse(m);
        } else if (type === 'tool_result') {
          _addToolResult(m);
        } else if (type === 'thinking') {
          _addThinking(m);
        } else if (type === 'system' || type === 'system_msg') {
          _addSystemMsg(m);
        }
      }

      if (idx < messages.length) {
        // Yield to the event loop, then continue.
        setTimeout(_renderBatch, 0);
      } else {
        // Done — add end separator and restore state.
        const endSep = document.createElement('div');
        endSep.className = 'msg msg-system';
        endSep.textContent = '--- End of history ---';
        elMessages.appendChild(endSep);

        _collapseTools = savedCollapse;
        _resetToolRun();
        _scrollToBottom();
      }
    }

    _renderBatch();
  }

  // --- Bell ---

  function _playBell() {
    // Simple audio bell if available, otherwise just flash the title.
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.frequency.value = 800;
      gain.gain.value = 0.1;
      osc.start();
      osc.stop(ctx.currentTime + 0.15);
    } catch (e) { /* ignore */ }

    // Flash document title.
    const orig = document.title;
    document.title = '\u{1F514} ' + orig;
    setTimeout(() => { document.title = orig; }, 2000);
  }

  // --- Helpers ---

  // Tool header summaries — render full content and let CSS
  // text-overflow: ellipsis on .tool-summary handle truncation.

  function _toolHeaderSummary(h, input) {
    if (!h) return '';
    const inp = input || {};

    if (h.name === 'Bash')  return _bashSummary(inp);
    if (h.name === 'Edit')  return `<span class="sum-path">${_esc(inp.file_path || '')}</span>`;
    if (h.name === 'Write') {
      const lines = (inp.content || '').split('\n').length;
      return `<span class="sum-path">${_esc(inp.file_path || '')}</span> <span class="sum-dim">(${lines} lines)</span>`;
    }
    if (h.name === 'Read')  return _readSummary(inp);
    if (h.name === 'Grep')  return _grepSummary(inp);
    if (h.name === 'Glob')  return `<span class="sum-pattern">${_esc(inp.pattern || '')}</span>`;
    if (h.name === 'WebSearch') return `<span class="sum-pattern">${_esc(inp.query || '')}</span>`;
    if (h.name === 'WebFetch')  return `<span class="sum-path">${_esc(inp.url || '')}</span>`;
    if (h.name === 'Agent' || h.name === 'Task') {
      return `<span class="sum-dim">${_esc(inp.description || '')}</span>`;
    }
    if (h.name === 'TodoWrite') {
      const n = Array.isArray(inp.todos) ? inp.todos.length : 0;
      return `<span class="sum-dim">${n} items</span>`;
    }
    return '';
  }

  /** Bash: show command if single-line; multi-line → size hint. */
  function _bashSummary(inp) {
    const cmd = inp.command || '';
    const lines = cmd.split('\n');
    if (lines.length === 1) {
      return `<span class="sum-cmd">${_esc((lines[0] || '').trim())}</span>`;
    }
    return `<span class="sum-dim">(${cmd.length} chars, ${lines.length} lines)</span>`;
  }

  /** Read: full path + offset/limit params if present. */
  function _readSummary(inp) {
    const params = [];
    if (inp.offset != null) params.push(`from ${inp.offset}`);
    if (inp.limit != null) params.push(`${inp.limit} lines`);
    const paramStr = params.length ? ` <span class="sum-dim">(${params.join(', ')})</span>` : '';
    return `<span class="sum-path">${_esc(inp.file_path || '')}</span>${paramStr}`;
  }

  /** Grep: full pattern + full path. CSS truncates if needed. */
  function _grepSummary(inp) {
    const pattern = inp.pattern || '';
    const rawPath = inp.path || '.';
    return `<span class="sum-pattern">"${_esc(pattern)}"</span> <span class="sum-dim">in</span> <span class="sum-path">${_esc(rawPath)}</span>`;
  }

  function _toolDetailHtml(name, input, msg) {
    let html = '';

    if (name === 'Bash') {
      html += `<div class="detail-label">Command</div>
               <pre>${_esc(input.command || '')}</pre>`;
      if (input.description) {
        html += `<div class="detail-label">Description</div>
                 <div class="detail-field">${_esc(input.description)}</div>`;
      }
    } else if (name === 'Edit') {
      html += `<div class="detail-label">File</div>
               <div class="detail-field">${_esc(input.file_path || '')}</div>`;
      if (input.old_string || input.new_string) {
        // Side-by-side diff view.
        html += Diff.render(
          input.old_string || '',
          input.new_string || '',
          input.file_path,
        );
      }
    } else if (name === 'Write') {
      html += `<div class="detail-label">File</div>
               <div class="detail-field">${_esc(input.file_path || '')}</div>`;
      html += `<div class="detail-label">Content (${(input.content || '').split('\n').length} lines)</div>
               <pre>${_esc(input.content || '')}</pre>`;
    } else if (name === 'Read') {
      html += `<div class="detail-label">File</div>
               <div class="detail-field">${_esc(input.file_path || '')}</div>`;
    } else if (name === 'Grep') {
      html += `<div class="detail-label">Pattern</div>
               <div class="detail-field">${_esc(input.pattern || '')}</div>`;
      html += `<div class="detail-label">Path</div>
               <div class="detail-field">${_esc(input.path || '.')}</div>`;
    } else {
      // Generic: show JSON.
      html += `<div class="detail-label">Input</div>
               <pre>${_esc(JSON.stringify(input, null, 2))}</pre>`;
    }

    return html;
  }

  function _esc(s) {
    if (!s) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  /**
   * Lightweight markdown → HTML for assistant text.
   * Handles: fenced code blocks, inline code, bold, italic, headers,
   * bullet/numbered lists, horizontal rules, and links.
   */
  function _md(raw) {
    if (!raw) return '';
    const text = _esc(raw);
    const lines = text.split('\n');
    const out = [];
    let i = 0;

    while (i < lines.length) {
      const line = lines[i];

      // --- Fenced code block ---
      const fenceMatch = line.match(/^(`{3,})([\w-]*)\s*$/);
      if (fenceMatch) {
        const fence = fenceMatch[1];
        const lang = fenceMatch[2];
        const codeLines = [];
        i++;
        while (i < lines.length && !lines[i].match(new RegExp('^' + fence + '\\s*$'))) {
          codeLines.push(lines[i]);
          i++;
        }
        i++;  // skip closing fence
        const langAttr = lang ? ` data-lang="${lang}"` : '';
        const langLabel = lang ? `<span class="code-lang">${lang}</span>` : '';
        out.push(`<div class="code-block"${langAttr}>${langLabel}<pre><code>${codeLines.join('\n')}</code></pre></div>`);
        continue;
      }

      // --- Heading ---
      const headingMatch = line.match(/^(#{1,4})\s+(.+)$/);
      if (headingMatch) {
        const level = headingMatch[1].length;
        const content = _inlineMd(headingMatch[2]);
        out.push(`<div class="md-h md-h${level}">${content}</div>`);
        i++;
        continue;
      }

      // --- Horizontal rule ---
      if (/^[-*_]{3,}\s*$/.test(line)) {
        out.push('<hr class="md-hr">');
        i++;
        continue;
      }

      // --- Bullet list ---
      if (/^[\s]*[-*+]\s/.test(line)) {
        const items = [];
        while (i < lines.length && /^[\s]*[-*+]\s/.test(lines[i])) {
          items.push(_inlineMd(lines[i].replace(/^[\s]*[-*+]\s/, '')));
          i++;
        }
        out.push('<ul class="md-list">' + items.map(t => `<li>${t}</li>`).join('') + '</ul>');
        continue;
      }

      // --- Numbered list ---
      if (/^[\s]*\d+[.)]\s/.test(line)) {
        const items = [];
        while (i < lines.length && /^[\s]*\d+[.)]\s/.test(lines[i])) {
          items.push(_inlineMd(lines[i].replace(/^[\s]*\d+[.)]\s/, '')));
          i++;
        }
        out.push('<ol class="md-list">' + items.map(t => `<li>${t}</li>`).join('') + '</ol>');
        continue;
      }

      // --- Regular paragraph line ---
      out.push(_inlineMd(line));
      i++;
    }

    return out.join('\n');
  }

  /** Inline markdown: bold, italic, inline code, links. */
  function _inlineMd(s) {
    return s
      // Inline code (must come before bold/italic to avoid conflicts)
      .replace(/`([^`]+?)`/g, '<code class="md-code">$1</code>')
      // Bold+italic
      .replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>')
      // Bold
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      // Italic
      .replace(/\*(.+?)\*/g, '<em>$1</em>')
      // Links
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  }

  function setCollapseTools(v) { _collapseTools = !!v; }
  function getCollapseTools() { return _collapseTools; }
  function setCollapseThreshold(n) { _collapseThreshold = Math.max(1, parseInt(n, 10) || 3); }
  function getCollapseThreshold() { return _collapseThreshold; }

  return { init, clear, handleMessage, setCollapseTools, getCollapseTools, setCollapseThreshold, getCollapseThreshold };
})();
