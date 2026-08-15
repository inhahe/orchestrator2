/* status.js — Status bar rendering
 *
 * Reads the status_update messages from the server and updates
 * the persistent status bar above the input field.
 */

const Status = (() => {
  // Element references (cached on init).
  let elIndicator, elState, elConfigDir, elAccount, elSession, elCwd, elTurns, elPlan;
  let elModel, elEffort, elThinking;
  let elContext, elRateLimit;
  let elCollapseCheck;

  // CSS class → colour mapping for the state text.
  const _stateColors = {
    working:      'var(--indicator-working)',
    connecting:   'var(--indicator-connecting)',
    reconnecting: 'var(--system-warning)',
    shutdown:     'var(--system-error)',
    error:        'var(--system-error)',
    'bg-wait':    'var(--indicator-bg-wait)',
    waiting:      'var(--system-waiting)',
    done:         'var(--system-done)',
  };

  function init() {
    elIndicator = document.getElementById('status-indicator');
    elState     = document.getElementById('status-state');
    elConfigDir = document.getElementById('status-configdir');
    elAccount   = document.getElementById('status-account');
    elSession   = document.getElementById('status-session');
    elCwd       = document.getElementById('status-cwd');
    elTurns     = document.getElementById('status-turns');
    elPlan      = document.getElementById('status-plan');
    elModel     = document.getElementById('status-model');
    elEffort    = document.getElementById('status-effort');
    elThinking  = document.getElementById('status-thinking');
    elContext   = document.getElementById('status-context');
    elRateLimit = document.getElementById('status-ratelimit');
    elCollapseCheck = document.getElementById('collapse-tools-check');
    if (elCollapseCheck) {
      // Restore persisted value.
      const savedCollapse = localStorage.getItem('collapse-tools');
      if (savedCollapse !== null) {
        const on = savedCollapse === 'true';
        elCollapseCheck.checked = on;
        Chat.setCollapseTools(on);
      }
      elCollapseCheck.addEventListener('change', () => {
        const on = elCollapseCheck.checked;
        Chat.setCollapseTools(on);
        localStorage.setItem('collapse-tools', String(on));
        App.send({ type: 'message', text: `/collapse ${on ? 'on' : 'off'}` });
      });
    }
  }

  // Change-detection cache: skip DOM writes when values haven't changed.
  let _prev = {};

  function _set(el, prop, val) {
    // Only touch the DOM if the value actually changed.
    if (el[prop] !== val) el[prop] = val;
  }

  // Local elapsed-time ticker for connecting/working states.
  // The backend ticker fires every ~2s and can stall when the SDK's
  // connect() blocks the event loop.  The frontend keeps its own 1s
  // interval so the displayed timer never freezes.
  let _localTimerInterval = null;
  let _localTimerClass = null;    // busy_class being timed
  let _localTimerStart = null;    // Date.now() when the state started

  function _startLocalTimer(cls) {
    _stopLocalTimer();
    _localTimerClass = cls;
    _localTimerStart = Date.now();
    const render = () => {
      if (!_localTimerClass) return;
      const secs = Math.round((Date.now() - _localTimerStart) / 1000);
      const label = _localTimerClass === 'connecting'
        ? `connecting (${secs}s)`
        : `working (${_fmtDuration(secs)})`;
      _set(elState, 'textContent', label);
    };
    // Paint the label right away so it flips in the SAME frame as the colour
    // change below — otherwise the colour turns green (working) while the text
    // still reads "idle" until the first 1s interval tick.
    render();
    _localTimerInterval = setInterval(render, 1000);
  }

  function _stopLocalTimer() {
    if (_localTimerInterval) {
      clearInterval(_localTimerInterval);
      _localTimerInterval = null;
    }
    _localTimerClass = null;
    _localTimerStart = null;
  }

  function _fmtDuration(totalSecs) {
    return Util.formatDuration(totalSecs, 'clock');
  }

  function update(status) {
    if (!status) return;

    // Indicator dot + state text.
    const cls = status.busy_class || 'idle';
    const stateLabel = status.busy_label || 'idle';
    const indicatorCls = 'indicator ' + cls;
    if (_prev.indicatorCls !== indicatorCls) {
      elIndicator.className = indicatorCls;
      _prev.indicatorCls = indicatorCls;
    }

    // Start/stop local timer for connecting and working states.
    const timedStates = ['connecting', 'working'];
    if (timedStates.includes(cls)) {
      if (_localTimerClass !== cls) _startLocalTimer(cls);
      // Don't overwrite the local timer's label — it updates every 1s.
    } else {
      _stopLocalTimer();
      _set(elState, 'textContent', stateLabel);
    }

    _set(elIndicator, 'title', stateLabel);
    _set(elState, 'title', stateLabel);
    const stateColor = _stateColors[cls] || '';
    if (_prev.stateColor !== stateColor) {
      elState.style.color = stateColor;
      _prev.stateColor = stateColor;
    }

    // Config dir (the CLAUDE_CONFIG_DIR / account store this session uses).
    // Show the folder name; full path in the tooltip.
    if (elConfigDir) _updateConfigDir(status.config_dir);

    // Account (email if available, else the config dir).  Static per session
    // but cheap; guarded by a change cache like the rest.
    if (elAccount) _updateAccount(status.account);

    // Session.
    const sid = status.session_id;
    const title = status.session_title;
    const sessionKey = (title || '') + '|' + (sid || '');
    if (_prev.sessionKey !== sessionKey) {
      if (title) {
        elSession.textContent = title;
        elSession.title = sid || '';
        document.title = title;
      } else if (sid) {
        elSession.textContent = sid.substring(0, 8);
        elSession.title = sid;
        document.title = 'orchestrator2';
      } else {
        elSession.textContent = '--';
        document.title = 'orchestrator2';
      }
      _prev.sessionKey = sessionKey;
    }

    // Working directory (full path).
    if (elCwd && _prev.cwd !== status.cwd) {
      const cwd = status.cwd || '--';
      elCwd.textContent = cwd;
      elCwd.title = cwd === '--' ? 'Working directory' : cwd;
      _prev.cwd = status.cwd;
    }

    // Turns.
    const turns = status.turns ?? 0;
    if (_prev.turns !== turns) {
      elTurns.innerHTML = '<span class="status-label">turns </span>' + turns;
      _prev.turns = turns;
    }

    // Plan / cost.
    const plan = status.plan_field || '--';
    _set(elPlan, 'textContent', plan);

    // Model.
    if (status.model) {
      let m = status.model.replace('claude-', '').replace('claude_', '');
      _set(elModel, 'textContent', m);
      _set(elModel, 'title', status.model);
    } else {
      _set(elModel, 'textContent', '--');
    }

    // Effort.
    _set(elEffort, 'textContent', status.effort || 'auto');

    // Thinking.
    _set(elThinking, 'textContent', status.thinking || '--');

    // Context tokens.
    if (status.context_tokens != null && status.context_tokens > 0) {
      const ctxText = _fmtTok(status.context_tokens);
      if (_prev.ctxText !== ctxText) {
        elContext.textContent = ctxText;
        elContext.title = status.context_tokens.toLocaleString() + ' tokens';
        _prev.ctxText = ctxText;
      }
    } else {
      _set(elContext, 'textContent', '--');
    }

    // Rate limits.
    _updateRateLimits(status.rate_limits);

    // Collapse-tools settings — localStorage takes precedence over backend.
    if (status.collapse_tools != null) {
      const saved = localStorage.getItem('collapse-tools');
      const val = saved !== null ? (saved === 'true') : status.collapse_tools;
      // Still re-read localStorage each time (that's what keeps a toggle made
      // in another tab in sync), but only touch the DOM when it changed.
      if (_prev.collapseVal !== val) {
        Chat.setCollapseTools(val);
        if (elCollapseCheck) elCollapseCheck.checked = val;
        _prev.collapseVal = val;
      }
    }

    // --show-thinking / "/show-thinking".  No localStorage override here (as
    // collapse-tools has): there's no checkbox for it, so the backend is the
    // only place it can be set and is therefore authoritative.
    if (status.show_thinking != null) {
      Chat.setShowThinking(status.show_thinking);
    }

    // DOM trim limit (0 = never trim).
    if (status.max_dom_messages != null && _prev.maxDom !== status.max_dom_messages) {
      Chat.setMaxDomMessages(status.max_dom_messages);
      _prev.maxDom = status.max_dom_messages;
    }
  }

  function _updateConfigDir(cfg) {
    cfg = cfg || '';
    // Display the trailing folder name (e.g. ".claude-account-b"); the full
    // path goes in the tooltip.
    let display = '--';
    if (cfg) {
      const parts = cfg.replace(/[\\/]+$/, '').split(/[\\/]/);
      display = parts[parts.length - 1] || cfg;
    }
    const key = display + '|' + cfg;
    if (_prev.configDirKey !== key) {
      elConfigDir.textContent = display;
      elConfigDir.title = cfg || 'Config dir';
      _prev.configDirKey = key;
    }
  }

  function _updateAccount(acct) {
    acct = acct || {};
    // Display: prefer the email; fall back to the config dir's folder name
    // (e.g. ".claude-account-b"), then to the raw config dir.
    let display = acct.email;
    const cfg = acct.config_dir || '';
    if (!display && cfg) {
      const parts = cfg.replace(/[\\/]+$/, '').split(/[\\/]/);
      display = parts[parts.length - 1] || cfg;
    }
    display = display || '--';

    // Tooltip: everything we could gather.
    const tip = [];
    if (acct.display_name) tip.push('name: ' + acct.display_name);
    if (acct.email) tip.push('email: ' + acct.email);
    if (acct.org_name) tip.push('org: ' + acct.org_name);
    if (acct.org_type) tip.push('org type: ' + acct.org_type);
    if (acct.org_role) tip.push('role: ' + acct.org_role);
    if (cfg) tip.push('config dir: ' + cfg);
    const tipText = tip.join('\n') || 'Account';

    const key = display + '|' + tipText;
    if (_prev.acctKey !== key) {
      elAccount.textContent = display;
      elAccount.title = tipText;
      _prev.acctKey = key;
    }
  }

  function _updateRateLimits(rl) {
    if (!rl || Object.keys(rl).length === 0) {
      _set(elRateLimit, 'textContent', '--');
      _set(elRateLimit, 'title', '');
      if (elRateLimit.style.color) elRateLimit.style.color = '';
      return;
    }
    const parts = [];
    const tipParts = [];
    for (const [key, info] of Object.entries(rl)) {
      const label = info.label || key;
      const pct = info.percent_used;
      if (pct != null) {
        const pctStr = Math.round(pct) + '%';
        parts.push(label + ' ' + pctStr);
        tipParts.push(`${label}: ${pctStr} used` +
          (info.reset_in ? ` (resets in ${info.reset_in})` : ''));
      }
    }
    _set(elRateLimit, 'textContent', parts.join(' | ') || '--');
    _set(elRateLimit, 'title', tipParts.join('\n'));

    // Color the rate limit based on max usage.
    const maxPct = Math.max(
      ...Object.values(rl).map(r => r.percent_used ?? 0)
    );
    const color = maxPct >= 90 ? 'var(--system-error)'
                : maxPct >= 70 ? 'var(--system-warning)'
                : '';
    if (elRateLimit.style.color !== color) elRateLimit.style.color = color;
  }

  function _fmtTok(n) {
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
    if (n >= 1_000) return (n / 1_000).toFixed(1) + 'k';
    return String(n);
  }

  return { init, update };
})();
