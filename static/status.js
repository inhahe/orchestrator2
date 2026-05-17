/* status.js — Status bar rendering
 *
 * Reads the status_update messages from the server and updates
 * the persistent status bar above the input field.
 */

const Status = (() => {
  // Element references (cached on init).
  let elIndicator, elState, elSession, elTurns, elPlan;
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
    elSession   = document.getElementById('status-session');
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
    _set(elIndicator, 'title', stateLabel);
    _set(elState, 'textContent', stateLabel);
    elState.title = stateLabel;
    const stateColor = _stateColors[cls] || '';
    if (_prev.stateColor !== stateColor) {
      elState.style.color = stateColor;
      _prev.stateColor = stateColor;
    }

    // Session.
    const sid = status.session_id;
    const title = status.session_title;
    const sessionKey = (title || '') + '|' + (sid || '');
    if (_prev.sessionKey !== sessionKey) {
      if (title) {
        elSession.textContent = title;
        elSession.title = sid || '';
        document.title = 'orchestrator2 - ' + title;
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
      elModel.title = status.model;
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
      Chat.setCollapseTools(val);
      if (elCollapseCheck) elCollapseCheck.checked = val;
    }

    // DOM trim limit (0 = never trim).
    if (status.max_dom_messages != null && _prev.maxDom !== status.max_dom_messages) {
      Chat.setMaxDomMessages(status.max_dom_messages);
      _prev.maxDom = status.max_dom_messages;
    }
  }

  function _updateRateLimits(rl) {
    if (!rl || Object.keys(rl).length === 0) {
      elRateLimit.textContent = '--';
      elRateLimit.title = '';
      elRateLimit.style.color = '';
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
    elRateLimit.textContent = parts.join(' | ') || '--';
    elRateLimit.title = tipParts.join('\n');

    // Color the rate limit based on max usage.
    const maxPct = Math.max(
      ...Object.values(rl).map(r => r.percent_used ?? 0)
    );
    if (maxPct >= 90) elRateLimit.style.color = 'var(--system-error)';
    else if (maxPct >= 70) elRateLimit.style.color = 'var(--system-warning)';
    else elRateLimit.style.color = '';
  }

  function _fmtTok(n) {
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
    if (n >= 1_000) return (n / 1_000).toFixed(1) + 'k';
    return String(n);
  }

  return { init, update };
})();
