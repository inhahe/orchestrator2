/* move.js — the `/move` overlay: copy this session somewhere else
 *
 * Named `Move` from when the command could only change *account*.  It is now
 * `/move` (with `/move` kept as an alias, since it is in muscle memory and
 * in older notes).  This module, its file, its CSS classes and the `switch_*`
 * wire messages keep the old name deliberately: they are internal, and
 * renaming them would churn a lot of code for no user-visible gain.
 *
 * The `/move` command copies the tab's *current* session to another Claude
 * account and/or another project directory, and continues it in the same
 * window.  This overlay drives that flow:
 *   1. list the Claude accounts (name + signed-in email) and pick one — the
 *      current one included, since "same account, different directory" is a
 *      perfectly ordinary switch,
 *   2. choose the destination directory (free text, with the directories
 *      Claude already knows about offered as one-click fills) and name the
 *      new session,
 *   3. the server copies the conversation JSONL into that account under the
 *      destination directory's project slug (fresh id, cwd rewritten), titles
 *      it, starts a runtime resuming the copy, and re-attaches THIS socket to
 *      it — so the chat seamlessly continues in its new home.
 *
 * Directory and account are chosen independently: leaving either as it is
 * gives you the two single-axis moves, and changing both does them together.
 *
 * Wire protocol
 *   send:    move_list, move_do {config_dir, new_name, cwd}   (see note above)
 *   receive: move_accounts {accounts:[{config_dir,name,email,is_current}],
 *                             dirs:[{path,mtime}], has_session,
 *                             current_title, current_cwd},
 *            move_error {message}, move_done {rid}
 * The final `attached` (handled by app.js) closes this overlay.
 */

const Move = (() => {
  let elRoot;                 // overlay container (built lazily)
  let elBody;                 // step content host
  let _visible = false;
  let _accounts = [];
  let _dirs = [];             // known project dirs [{path, mtime}]
  let _currentTitle = '';
  let _currentCwd = '';
  let _prefillCwd = '';       // from `/move <path>`
  let _selected = null;       // chosen account {config_dir, name, email}

  function _build() {
    if (elRoot) return;
    elRoot = document.createElement('div');
    elRoot.id = 'move-overlay';
    elRoot.className = 'move-overlay hidden';
    elRoot.innerHTML =
      '<div class="move-inner">' +
      '  <header class="move-header">' +
      '    <h2>Move session</h2>' +
      '    <button class="move-close" title="Cancel">&times;</button>' +
      '  </header>' +
      '  <div class="move-body"></div>' +
      '</div>';
    document.body.appendChild(elRoot);
    elBody = elRoot.querySelector('.move-body');

    elRoot.querySelector('.move-close').addEventListener('click', close);
    // Click on the dark backdrop (outside the panel) cancels.
    elRoot.addEventListener('mousedown', (e) => {
      if (e.target === elRoot) close();
    });
    document.addEventListener('keydown', (e) => {
      if (_visible && e.key === 'Escape') { e.preventDefault(); close(); }
    });
  }

  // *prefillCwd* comes from `/move <path>` — it only fills the directory
  // field, it does not skip the account step, because a path argument says
  // nothing about which account the caller wants.
  function open(prefillCwd) {
    _build();
    _selected = null;
    _prefillCwd = (prefillCwd || '').trim();
    _visible = true;
    elRoot.classList.remove('hidden');
    _renderLoading();
    App.send({ type: 'move_list' });
  }

  function close() {
    _visible = false;
    if (elRoot) elRoot.classList.add('hidden');
  }

  function isVisible() { return _visible; }

  function _renderLoading() {
    elBody.innerHTML =
      '<div class="move-loading">' +
      '  <div class="move-spin"></div><span>Loading accounts\u2026</span>' +
      '</div>';
  }

  // Step 1 — account list.
  function renderAccounts(msg) {
    if (!_visible) return;
    _accounts = Array.isArray(msg.accounts) ? msg.accounts : [];
    // `dirs`/`current_cwd` only arrive on the initial move_list reply; the
    // Back button re-renders from cached state and must not wipe them.
    if (Array.isArray(msg.dirs)) _dirs = msg.dirs;
    if (msg.current_cwd != null) _currentCwd = msg.current_cwd;
    _currentTitle = msg.current_title || '';
    if (!msg.has_session) {
      elBody.innerHTML =
        '<div class="move-error">This tab has no active session to ' +
        'move. Open or start a session first.</div>';
      return;
    }
    if (_accounts.length === 0) {
      elBody.innerHTML =
        '<div class="move-error">No Claude account directories found.</div>';
      return;
    }
    let html =
      '<p class="move-intro">Copy this conversation to another account ' +
      'and/or another directory, and continue it here. Pick the destination ' +
      'account — or the current one, to change only the directory:</p>' +
      '<div class="move-accounts">';
    _accounts.forEach((a, i) => {
      const email = a.email
        ? _esc(a.email)
        : '<span class="move-noemail">(not signed in)</span>';
      const cur = a.is_current
        ? ' <span class="move-badge">current</span>' : '';
      html +=
        '<button class="move-account" data-idx="' + i + '">' +
        '<span class="move-acct-name">' + _esc(a.name) + cur + '</span>' +
        '<span class="move-acct-email">' + email + '</span>' +
        '</button>';
    });
    html += '</div>';
    elBody.innerHTML = html;
    elBody.querySelectorAll('.move-account').forEach((btn) => {
      btn.addEventListener('click', () => {
        const idx = parseInt(btn.getAttribute('data-idx'), 10);
        _selected = _accounts[idx];
        _renderNameStep();
      });
    });
  }

  // Step 2 — destination directory + name for the new session.  One screen,
  // because they are two halves of the same answer ("where does this go?")
  // and splitting them would put a page break in the middle of a sentence.
  function _renderNameStep() {
    const a = _selected || {};
    const dest = _esc(a.name || '');
    const suggestions = _dirs.filter((d) => !_samePath(d.path, _currentCwd));
    const email = a.email ? '  \u00b7  ' + _esc(a.email) : '';
    let html =
      '<p class="move-intro">Copying to <b>' + dest + '</b>' + email +
      '.</p>' +
      '<label class="move-label" for="move-cwd">Directory</label>' +
      '<input id="move-cwd" class="move-cwd" type="text" ' +
      'spellcheck="false" placeholder="' +
      _esc(_currentCwd || 'working directory') + '">' +
      '<div class="move-hint">Leave it alone to keep the session where it ' +
      'is. Any existing directory works, whether or not Claude has been run ' +
      'there.</div>';
    if (suggestions.length) {
      html += '<div class="move-dirs">';
      suggestions.slice(0, 12).forEach((d, i) => {
        html += '<button class="move-dir" data-idx="' + i + '" title="' +
          _esc(d.path) + '">' + _esc(_shortPath(d.path)) + '</button>';
      });
      html += '</div>';
    }
    html +=
      '<label class="move-label" for="move-name">Name for the new ' +
      'session</label>' +
      '<input id="move-name" class="move-name" type="text" ' +
      'placeholder="e.g. ' + _esc(_currentTitle || 'my session') + '">' +
      '<div class="move-actions">' +
      '  <button class="move-back">&larr; Back</button>' +
      '  <button class="move-go">Copy &amp; continue</button>' +
      '</div>' +
      '<div class="move-msg hidden"></div>';
    elBody.innerHTML = html;

    const cwdInput = elBody.querySelector('#move-cwd');
    cwdInput.value = _prefillCwd || _currentCwd || '';
    const input = elBody.querySelector('#move-name');
    input.value = _currentTitle || '';

    // `/move <path>` already said which directory, so the remaining
    // decision is the name; otherwise the directory is the first field.
    if (_prefillCwd) { input.focus(); input.select(); }
    else { cwdInput.focus(); cwdInput.select(); }

    elBody.querySelectorAll('.move-dir').forEach((btn) => {
      btn.addEventListener('click', () => {
        const idx = parseInt(btn.getAttribute('data-idx'), 10);
        cwdInput.value = suggestions[idx].path;
        input.focus();
        input.select();
      });
    });
    elBody.querySelector('.move-back')
      .addEventListener('click', () => renderAccounts({
        accounts: _accounts, has_session: true, current_title: _currentTitle,
      }));
    const go = () => _submit(input.value, cwdInput.value);
    elBody.querySelector('.move-go').addEventListener('click', go);
    [input, cwdInput].forEach((el) => {
      el.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); go(); }
      });
    });
  }

  function _submit(name, cwd) {
    name = (name || '').trim();
    cwd = (cwd || '').trim();
    const msgEl = elBody.querySelector('.move-msg');
    if (!name) {
      if (msgEl) {
        msgEl.textContent = 'Please enter a name.';
        msgEl.classList.remove('hidden');
      }
      return;
    }
    if (!_selected) return;
    const moving = !!cwd && !_samePath(cwd, _currentCwd);
    // Busy state — the copy can take a moment for large conversations.
    elBody.innerHTML =
      '<div class="move-loading">' +
      '  <div class="move-spin"></div>' +
      '  <span>Copying conversation' +
      (moving ? ' to ' + _esc(_shortPath(cwd)) : '') + '\u2026</span>' +
      '</div>';
    App.send({
      type: 'move_do',
      config_dir: _selected.config_dir,
      new_name: name,
      cwd: cwd,
    });
  }

  // Case- and separator-insensitive path comparison, mirroring the server's
  // `normalize_path_for_compare`.  Used only to decide what to *show* (which
  // suggestions are redundant, whether to name the destination); the server
  // re-derives the real answer from the real paths.
  function _samePath(a, b) {
    const norm = (s) => String(s || '').replace(/\\/g, '/')
      .replace(/\/+$/, '').toLowerCase();
    return norm(a) === norm(b);
  }

  // Last two path segments -- enough to tell one project from another without
  // letting a deep path blow out the button row.
  function _shortPath(p) {
    const parts = String(p || '').replace(/\\/g, '/')
      .replace(/\/+$/, '').split('/').filter(Boolean);
    if (parts.length <= 2) return String(p || '');
    return '\u2026/' + parts.slice(-2).join('/');
  }

  // Server reported a failure — surface it and let the user retry.
  function error(msg) {
    if (!_visible) return;
    const text = (msg && msg.message) || 'Move failed.';
    elBody.innerHTML =
      '<div class="move-error">' + _esc(text) + '</div>' +
      '<div class="move-actions">' +
      '  <button class="move-back">&larr; Back</button>' +
      '</div>';
    elBody.querySelector('.move-back')
      .addEventListener('click', () => renderAccounts({
        accounts: _accounts, has_session: true, current_title: _currentTitle,
      }));
  }

  function _esc(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  return { open, close, isVisible, renderAccounts, error };
})();
