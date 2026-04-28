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
  let elCollapseThreshold;

  // CSS class → colour mapping for the state text.
  const _stateColors = {
    working:    'var(--indicator-working)',
    connecting: 'var(--indicator-connecting)',
    error:      'var(--system-error)',
    'bg-wait':  'var(--indicator-bg-wait)',
    waiting:    'var(--system-waiting)',
    done:       'var(--system-done)',
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
    elCollapseThreshold = document.getElementById('collapse-threshold-select');
    if (elCollapseThreshold) {
      // Restore persisted value.
      const savedThreshold = localStorage.getItem('collapse-threshold');
      if (savedThreshold !== null) {
        const n = parseInt(savedThreshold, 10);
        if (n >= 1) {
          elCollapseThreshold.value = String(n);
          Chat.setCollapseThreshold(n);
        }
      }
      elCollapseThreshold.addEventListener('change', () => {
        const val = parseInt(elCollapseThreshold.value, 10);
        Chat.setCollapseThreshold(val);
        localStorage.setItem('collapse-threshold', String(val));
        App.send({ type: 'message', text: `/collapse-threshold ${val}` });
      });
    }
  }

  function update(status) {
    if (!status) return;

    // Indicator dot.
    const cls = status.busy_class || 'idle';
    elIndicator.className = 'indicator ' + cls;
    elIndicator.title = status.busy_label || 'idle';

    // State text (working, idle, rate-limited, etc.).
    const stateLabel = status.busy_label || 'idle';
    elState.textContent = stateLabel;
    elState.title = stateLabel;
    elState.style.color = _stateColors[cls] || '';

    // Session.
    const sid = status.session_id;
    const title = status.session_title;
    if (title) {
      elSession.textContent = title;
      elSession.title = sid || '';
    } else if (sid) {
      elSession.textContent = sid.substring(0, 8);
      elSession.title = sid;
    } else {
      elSession.textContent = '--';
    }

    // Turns — "turns" label then number.
    const turns = status.turns ?? 0;
    elTurns.innerHTML = '<span class="status-label">turns </span>' + turns;

    // Plan / cost.
    if (status.plan_field) {
      elPlan.textContent = status.plan_field;
    } else {
      elPlan.textContent = '--';
    }

    // Model.
    if (status.model) {
      let m = status.model;
      // Shorten common prefixes (backend already strips "claude-",
      // but guard against double-prefix).
      m = m.replace('claude-', '').replace('claude_', '');
      elModel.textContent = m;
      elModel.title = status.model;
    } else {
      elModel.textContent = '--';
    }

    // Effort.
    elEffort.textContent = status.effort || 'auto';

    // Thinking.
    elThinking.textContent = status.thinking || '--';

    // Context tokens.
    if (status.context_tokens != null && status.context_tokens > 0) {
      elContext.textContent = _fmtTok(status.context_tokens);
      elContext.title = status.context_tokens.toLocaleString() + ' tokens';
    } else {
      elContext.textContent = '--';
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
    if (status.collapse_threshold != null) {
      const saved = localStorage.getItem('collapse-threshold');
      const val = saved !== null ? parseInt(saved, 10) : status.collapse_threshold;
      Chat.setCollapseThreshold(val);
      if (elCollapseThreshold) elCollapseThreshold.value = String(val);
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
