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
  const MAX_HISTORY = 200;
  let _promptHistory = [];   // oldest first
  let _historyIndex = -1;    // -1 = not navigating
  let _savedInput = '';       // current input saved when navigation starts

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

    // Load prompt history from localStorage.
    try {
      const saved = localStorage.getItem('prompt-history');
      if (saved) _promptHistory = JSON.parse(saved);
    } catch (_) {}

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

  function setBusy(busy) {
    if (busy) {
      elSendBtn.classList.add('hidden');
      elInterruptBtn.classList.remove('hidden');
    } else {
      elSendBtn.classList.remove('hidden');
      elInterruptBtn.classList.add('hidden');
    }
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

  function _pushHistory(text) {
    // Don't duplicate the last entry.
    if (_promptHistory.length > 0 && _promptHistory[_promptHistory.length - 1] === text) return;
    _promptHistory.push(text);
    if (_promptHistory.length > MAX_HISTORY) {
      _promptHistory = _promptHistory.slice(-MAX_HISTORY);
    }
    try {
      localStorage.setItem('prompt-history', JSON.stringify(_promptHistory));
    } catch (_) {}
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

  return { init, setCommands, setBusy, focus };
})();
