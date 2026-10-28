/* commands.js — Input handling and slash-command autocomplete
 *
 * Manages the input textarea: auto-resize, Enter/Shift+Enter,
 * slash-command tab-completion, and sends messages to the server.
 */

const Commands = (() => {
  let elInput, elSendBtn, elInterruptBtn, elHint, elInputWrap;
  let commandList = [];
  let acDropdown = null;
  let acIndex = -1;
  let acFiltered = [];

  // --- Prompt history (Ctrl+Up / Ctrl+Down) ---
  //
  // The session's own: what was typed in it, from any tab, kept in
  // localStorage under its session id (HISTORY_KEY), so it outlives the tab
  // and a reload.  A tab that switches session switches history with it.
  // Read again before it is added to or recalled from, so two tabs on one
  // session each have what the other typed.
  // Asked 2026-10-27: "make ctrl+up history specific to that session/window,
  // not global to all sessions".
  const MAX_HISTORY = 200;
  // Histories kept, the least recently used dropped past it -- or sooner, when
  // the browser's storage is full, but never more than MAX_EVICTIONS for one
  // save: storage full of something else is no reason to empty it of these.
  // SESSIONS_KEY orders them.
  const MAX_SESSIONS = 100;
  const MAX_EVICTIONS = 5;
  const HISTORY_KEY = (sid) => 'prompt-history:' + sid;
  const SESSIONS_KEY = 'prompt-history-sessions';
  let _promptHistory = [];   // oldest first
  let _historyIndex = -1;    // -1 = not navigating
  let _savedInput = '';       // current input saved when navigation starts
  // The runtime this tab shows, and its session's id once known: a prompt
  // typed before then is kept with what the session has when it comes.
  let _rid = null;
  let _sid = null;

  function init() {
    elInput        = document.getElementById('input-box');
    elSendBtn      = document.getElementById('send-btn');
    elInterruptBtn = document.getElementById('interrupt-btn');
    elHint         = document.getElementById('input-hint');
    elInputWrap    = document.getElementById('input-wrap');

    // Create autocomplete dropdown.
    acDropdown = document.createElement('div');
    acDropdown.id = 'autocomplete';
    acDropdown.className = 'hidden';
    elInputWrap.style.position = 'relative';
    elInputWrap.appendChild(acDropdown);

    // Event listeners.
    elInput.addEventListener('keydown', _onKeyDown);
    elInput.addEventListener('input', _onInput);
    elSendBtn.addEventListener('click', _send);
    elInterruptBtn.addEventListener('click', _interrupt);

    // Click outside autocomplete to close.
    document.addEventListener('click', (e) => {
      if (!acDropdown.contains(e.target) && e.target !== elInput) {
        _hideAc();
      }
    });

    // Global keyboard shortcuts (work regardless of focus).
    document.addEventListener('keydown', (e) => {
      // Ctrl+C: interrupt (unless text is selected or input is focused).
      if (e.key === 'c' && e.ctrlKey && !e.shiftKey) {
        if (document.activeElement === elInput) return;  // handled by _onKeyDown
        if (window.getSelection().toString()) return;    // let them copy
        if (!elInterruptBtn.classList.contains('hidden')) {
          e.preventDefault();
          _interrupt();
        }
      }

      // Ctrl+End: scroll chat to bottom and re-enable auto-scroll — but only
      // when the user isn't editing text.  Inside a textarea/input (e.g. the
      // main prompt box or a queued-prompt editor) Ctrl+End / Ctrl+Home must
      // keep their native "jump to start/end of the text" behavior, so bail
      // out here rather than swallowing the key.
      const ae = document.activeElement;
      const editing = ae && (
        ae.tagName === 'TEXTAREA' ||
        ae.tagName === 'INPUT' ||
        ae.isContentEditable
      );
      if (e.key === 'End' && e.ctrlKey && !editing) {
        e.preventDefault();
        Chat.forceScrollToBottom();
      }
    });
  }

  function setCommands(cmds) {
    commandList = cmds || [];
  }

  // Last value applied, so a repeated setBusy() with the same state doesn't
  // touch the DOM at all.  `classList.add`/`remove` re-serialise and re-set the
  // class attribute even when the token set is unchanged, which fires a
  // mutation and forces a style recalc + layout of the *whole* document — and
  // the status ticker calls this on every update, so on an idle session it was
  // the only thing still repainting the tab.
  let _busyApplied = null;

  function setBusy(busy) {
    busy = !!busy;
    if (busy === _busyApplied) return;
    _busyApplied = busy;
    elSendBtn.classList.toggle('hidden', busy);
    elInterruptBtn.classList.toggle('hidden', !busy);
  }

  function focus() {
    elInput.focus();
  }

  // ----- Key handling -----

  function _onKeyDown(e) {
    // Autocomplete navigation.
    if (!acDropdown.classList.contains('hidden')) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        acIndex = Math.min(acIndex + 1, acFiltered.length - 1);
        _renderAc();
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        acIndex = Math.max(acIndex - 1, 0);
        _renderAc();
        return;
      }
      if (e.key === 'Tab' || e.key === 'Enter') {
        if (acFiltered.length > 0 && acIndex >= 0) {
          e.preventDefault();
          const selected = acFiltered[acIndex];
          if (e.key === 'Enter' && elInput.value.trim() === selected) {
            // Command already fully typed — send it.
            _hideAc();
            _send();
          } else {
            _acceptAc(selected);
          }
          return;
        }
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        _hideAc();
        return;
      }
    }

    // Tab: trigger autocomplete if at start of slash command.
    if (e.key === 'Tab' && !e.shiftKey) {
      const val = elInput.value;
      if (val.startsWith('/') && !val.includes(' ')) {
        e.preventDefault();
        _showAc(val);
        return;
      }
    }

    // Enter: send (unless Shift+Enter for newline).
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      _send();
      return;
    }

    // Ctrl+Up / Ctrl+Down: prompt history navigation.
    if (e.ctrlKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
      e.preventDefault();
      _navigateHistory(e.key === 'ArrowUp' ? -1 : 1);
      return;
    }

    // Ctrl+C: interrupt if busy — but only when nothing is selected,
    // so the user can still copy text normally.
    if (e.key === 'c' && e.ctrlKey) {
      if (window.getSelection().toString()) return;
      if (elInput.selectionStart !== elInput.selectionEnd) return;
      if (!elInterruptBtn.classList.contains('hidden')) {
        e.preventDefault();
        _interrupt();
      }
    }
  }

  function _onInput() {
    _autoResize();

    // Reset history navigation when the user types.
    _historyIndex = -1;

    // Live autocomplete while typing a slash command.
    const val = elInput.value;
    if (val.startsWith('/') && !val.includes(' ') && val.length > 1) {
      _showAc(val);
    } else {
      _hideAc();
    }
  }

  // ----- Prompt history -----

  function _navigateHistory(dir) {
    if (_historyIndex === -1) _refreshHistory();
    if (_promptHistory.length === 0) return;

    if (_historyIndex === -1) {
      // Starting navigation — save current input.
      _savedInput = elInput.value;
      if (dir === -1) {
        _historyIndex = _promptHistory.length - 1;
      } else {
        return;  // already at the end, nothing to do
      }
    } else {
      _historyIndex += dir;
    }

    if (_historyIndex < 0) {
      _historyIndex = 0;
      return;
    }
    if (_historyIndex >= _promptHistory.length) {
      // Past the end — restore saved input.
      _historyIndex = -1;
      elInput.value = _savedInput;
      _autoResize();
      return;
    }

    elInput.value = _promptHistory[_historyIndex];
    _autoResize();
  }

  // A line that may hold an API key: /apikey with anything but "off" (a key
  // is never typed there, but may be pasted in hope), or text shaped like an
  // Anthropic key.  Such a line is still sent, for the hub to refuse, but
  // never goes into the history, which localStorage keeps on disk.
  function _mayHoldKey(text) {
    return /^\/apikey\s+(?!off$)/i.test(text) || /sk-ant-/i.test(text);
  }

  function _pushHistory(text) {
    _refreshHistory();
    if (_append(text)) _saveHistory();
  }

  // The stored history, which another tab on this session may have added to.
  function _refreshHistory() {
    if (!_sid) return;
    const stored = _loadHistory(_sid);
    if (stored) _promptHistory = _merged(stored, _promptHistory);
  }

  // *stored*, then what of *held* it does not have: a line this tab could not
  // save (the storage full), or carried from the id before -- unless another
  // tab on the session carried it over first.
  function _merged(stored, held) {
    const out = stored.slice();
    for (const t of held) if (!out.includes(t)) out.push(t);
    return out.slice(-MAX_HISTORY);
  }

  // Add *text* to the history in memory; whether it was added.
  function _append(text) {
    if (_mayHoldKey(text)) return false;
    // Don't duplicate the last entry.
    if (_promptHistory.length > 0 && _promptHistory[_promptHistory.length - 1] === text) return false;
    _promptHistory.push(text);
    if (_promptHistory.length > MAX_HISTORY) {
      _promptHistory = _promptHistory.slice(-MAX_HISTORY);
    }
    return true;
  }

  // Session *sid*'s stored history, less any line that may hold a key; null
  // if the storage cannot be read.
  function _loadHistory(sid) {
    try {
      const all = JSON.parse(localStorage.getItem(HISTORY_KEY(sid)) || '[]');
      return Array.isArray(all)
        ? all.filter((x) => typeof x === 'string' && !_mayHoldKey(x)).slice(-MAX_HISTORY)
        : [];
    } catch (_) {
      return null;
    }
  }

  // Keep the history under the session's id, once it has one, as the most
  // recently used; drop the least recently used past MAX_SESSIONS, and while
  // the storage is full.
  function _saveHistory() {
    if (!_sid) return;
    try {
      let order = JSON.parse(localStorage.getItem(SESSIONS_KEY) || '[]');
      if (!Array.isArray(order)) order = [];
      order = order.filter((s) => s !== _sid);
      while (order.length >= MAX_SESSIONS) localStorage.removeItem(HISTORY_KEY(order.shift()));
      for (let evicted = 0; ; evicted++) {
        try {
          localStorage.setItem(HISTORY_KEY(_sid), JSON.stringify(_promptHistory));
          break;
        } catch (e) {
          if (!order.length || evicted >= MAX_EVICTIONS) throw e;
          localStorage.removeItem(HISTORY_KEY(order.shift()));
        }
      }
      order.push(_sid);
      localStorage.setItem(SESSIONS_KEY, JSON.stringify(order));
    } catch (_) { /* in memory, then, for this tab */ }
  }

  /** The tab shows runtime *rid*, on session *sessionId* if it has one yet
   *  (app.js, on `attached`): another runtime's history is not its, nor
   *  another session's -- /resume and /cwd open one in the same runtime.
   *  Its own comes with its session id (setSessionId).  *carry*: the
   *  conversation goes on there -- a /move's copy -- so this tab's history
   *  goes with it. */
  function setRuntime(rid, { carry = false, sessionId = null } = {}) {
    if (rid === _rid && !(sessionId && _sid && sessionId !== _sid)) return;
    _rid = rid;
    _sid = null;
    if (!carry) _promptHistory = [];
    _historyIndex = -1;
  }

  /** The tab's session has id *sid* (status.js, from each snapshot): its
   *  stored history, then what this tab holds -- typed before the id came, or
   *  under the id it had before: the same conversation after a context trim,
   *  a /clear in this tab, a /move's copy (setRuntime's *carry*). */
  function setSessionId(sid) {
    if (!sid || sid === _sid) return;
    _sid = sid;
    _promptHistory = _merged(_loadHistory(sid) || [], _promptHistory);
    _historyIndex = -1;
    _saveHistory();
  }

  function _autoResize() {
    elInput.style.height = 'auto';
    elInput.style.height = Math.min(elInput.scrollHeight, 200) + 'px';
  }

  // ----- Send / Interrupt -----

  function _send() {
    const text = elInput.value.trim();
    if (!text) return;
    _pushHistory(text);
    _historyIndex = -1;
    elInput.value = '';
    elInput.style.height = 'auto';
    _hideAc();
    // `/move` is a client-driven overlay (account picker → destination →
    // copy), not a backend slash command, so intercept it here.  An argument
    // is taken as the destination *directory* and prefills that field —
    // `/move D:\some\project` is the fast path for the common case of moving
    // a session to another project without changing account.
    //
    // The `=== '/move' || startsWith('/move ')` shape rather than a bare
    // prefix test: `/moved` is a different command (an unknown one, which the
    // server should say so about) and must not be swallowed here.
    if (text === '/move' || text.startsWith('/move ')) {
      Move.open(text.slice('/move'.length).trim());
      return;
    }
    // `/connect` (`/reconnect`) must work even when the browser↔server socket
    // is down — that's exactly when the user reaches for it.  Route it through
    // App.reconnect(), which revives a dead socket client-side or, if the
    // socket is alive, forwards the command for a server-side SDK reconnect.
    if (text === '/connect' || text === '/reconnect') {
      App.reconnect();
      return;
    }
    App.send({ type: 'message', text: text });
  }

  function _interrupt() {
    App.send({ type: 'interrupt' });
  }

  // ----- Autocomplete -----

  function _showAc(prefix) {
    acFiltered = commandList.filter(c => c.startsWith(prefix));
    if (acFiltered.length === 0) {
      _hideAc();
      return;
    }
    acIndex = 0;
    _renderAc();
    acDropdown.classList.remove('hidden');
  }

  function _renderAc() {
    let html = '';
    for (let i = 0; i < acFiltered.length; i++) {
      const cls = i === acIndex ? 'ac-item selected' : 'ac-item';
      html += `<div class="${cls}" data-idx="${i}">${_esc(acFiltered[i])}</div>`;
    }
    acDropdown.innerHTML = html;

    // Click handlers.
    acDropdown.querySelectorAll('.ac-item').forEach(el => {
      el.addEventListener('click', () => {
        _acceptAc(acFiltered[parseInt(el.dataset.idx)]);
      });
    });
  }

  function _acceptAc(cmd) {
    elInput.value = cmd + ' ';
    elInput.focus();
    _hideAc();
  }

  function _hideAc() {
    acDropdown.classList.add('hidden');
    acIndex = -1;
    acFiltered = [];
  }

  function _esc(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  return { init, setCommands, setBusy, focus, setRuntime, setSessionId };
})();
