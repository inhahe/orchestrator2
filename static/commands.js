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
          _acceptAc(acFiltered[acIndex]);
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

    // Ctrl+C: interrupt if busy, clear input if not.
    if (e.key === 'c' && e.ctrlKey) {
      if (!elInterruptBtn.classList.contains('hidden')) {
        e.preventDefault();
        _interrupt();
      }
    }
  }

  function _onInput() {
    // Auto-resize textarea.
    elInput.style.height = 'auto';
    elInput.style.height = Math.min(elInput.scrollHeight, 200) + 'px';

    // Live autocomplete while typing a slash command.
    const val = elInput.value;
    if (val.startsWith('/') && !val.includes(' ') && val.length > 1) {
      _showAc(val);
    } else {
      _hideAc();
    }
  }

  // ----- Send / Interrupt -----

  function _send() {
    const text = elInput.value.trim();
    if (!text) return;
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
