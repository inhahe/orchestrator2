/* usage.js -- /usage: the account's plan limits, drawn as Claude Code draws them.
 *
 * Asked 2026-09-27: "the claude code TUI has a /usage command that tells you
 * in ascii all about your usage, including what percentage of the 5-hr limit
 * and 7-day limit's been used and when they reset."
 *
 * The hub fetches the limits (plan_usage.py) and sends two command_data
 * "usage" messages: {loading: true} at once, then the report or {error}.
 * This file turns the report into the text of the modal, following Claude
 * Code 2.1.258's Usage screen (Settings -> Usage):
 *
 *   Current session
 *   █████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 18% used
 *   Resets 2:10pm (America/Los_Angeles)
 *
 *   Current week (all models)
 *   ██████████████████████████▌░░░░░░░░░░░░░░░░░░░░░░░ 53% used
 *   Resets Oct 2, 5pm (America/Los_Angeles)
 *
 * It uses the same sections in the same order, the same "N% used" (rounded
 * down) and the same reset wording. When a reset is within a day, only the
 * time is shown. Past that, and always for a weekly limit, the date is shown
 * too. Times are in this browser's time zone. In a narrow modal (a phone) it
 * switches to the TUI's narrow layout, with the bar across the full width.
 *
 * Where it differs, and why:
 *  - The empty part of the bar is drawn with ░. The TUI draws it as spaces
 *    on a coloured background, which the modal's plain text cannot show.
 *  - Every per-model weekly limit the endpoint reports is shown. The TUI
 *    shows only the models named in a server-side feature flag, which the
 *    hub cannot read.
 *  - The account and plan head the report, because one hub runs sessions on
 *    several accounts.
 *  - "Current week by product" (the endpoint's seven_day_breakdown) is an
 *    addition. The TUI's "what's contributing" section is built by scanning
 *    local transcripts, which this does not do.
 */

const Usage = (() => {
  // The TUI's layout: a 50-cell bar once there are at least 62 columns to
  // lay out in; below that, a bar across the whole width.
  const WIDE_BAR = 50;
  const WIDE_AT = 62;
  const DEFAULT_COLS = 80;
  const FULL = '\u2588';
  const HALF = '\u258c';
  const EMPTY = '\u2591';
  const DOT = ' \u00b7 ';

  // True between the "loading" message and the answer.
  let _awaiting = false;

  /** A bar `width` cells wide with `ratio` (0..1) of it filled. */
  function bar(ratio, width) {
    const exact = Math.max(0, Number(ratio) || 0) * width;
    const whole = Math.floor(exact);
    if (whole >= width) return FULL.repeat(width);
    return FULL.repeat(whole) + (exact - whole >= 0.5 ? HALF : EMPTY)
      + EMPTY.repeat(width - whole - 1);
  }

  /** This browser's IANA time zone, e.g. "America/Los_Angeles". */
  function localZone() {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || 'local time';
    } catch (e) {
      return 'local time';
    }
  }

  function _part(date, timeZone, opts, type) {
    const fmt = new Intl.DateTimeFormat('en-US', Object.assign({ timeZone }, opts));
    const p = fmt.formatToParts(date).find((x) => x.type === type);
    return p ? p.value : '';
  }

  /** When a limit resets, worded as Claude Code words it (formatResetTime).
   *
   * `when` is an ISO string or epoch seconds.  Options: `now`, `timeZone`
   * (default: this browser's), `showTime` (default true), and `alwaysDate`
   * (show the date even when the reset is within a day). */
  function resetText(when, o) {
    o = o || {};
    const d = typeof when === 'number' ? new Date(when * 1000) : new Date(when);
    if (isNaN(d.getTime())) return null;
    const now = o.now || new Date();
    const tz = o.timeZone;
    const showTime = o.showTime !== false;
    const minute = Number(_part(d, tz, { minute: 'numeric' }, 'minute'));
    const hoursAway = (d.getTime() - now.getTime()) / 3600000;
    let s;
    if (o.alwaysDate || hoursAway > 24) {
      const f = { month: 'short', day: 'numeric', timeZone: tz };
      if (showTime) {
        f.hour = 'numeric';
        f.hour12 = true;
        if (minute !== 0) f.minute = '2-digit';
      }
      if (_part(d, tz, { year: 'numeric' }, 'year')
          !== _part(now, tz, { year: 'numeric' }, 'year')) {
        f.year = 'numeric';
      }
      s = d.toLocaleString('en-US', f);
    } else {
      const f = { hour: 'numeric', hour12: true, timeZone: tz };
      if (minute !== 0) f.minute = '2-digit';
      s = d.toLocaleTimeString('en-US', f);
    }
    // "5:30 PM" -> "5:30pm", as the TUI does.  (ICU puts a narrow no-break
    // space, U+202F, before the AM/PM.)
    s = s.replace(/[ \u202f]([AP]M)/i, (_m, ap) => ap.toLowerCase());
    return s + ' (' + (tz || localZone()) + ')';
  }

  /** One limit's lines, or null when it has no figure (the TUI skips it). */
  function limitBlock(title, limit, o) {
    if (!limit || typeof limit !== 'object') return null;
    const util = limit.utilization;
    if (typeof util !== 'number' || !isFinite(util)) return null;
    const used = Math.floor(util) + '% used';
    let sub = null;
    if (limit.resets_at) {
      const when = resetText(limit.resets_at, o);
      if (when) sub = 'Resets ' + when;
    }
    if (o.extra) sub = sub ? o.extra + DOT + sub : o.extra;
    if (o.override !== undefined) sub = o.override;
    if (o.maxWidth >= WIDE_AT) {
      const lines = [title, bar(util / 100, WIDE_BAR) + ' ' + used];
      if (sub) lines.push(sub);
      return lines.join('\n');
    }
    return [title + (sub ? DOT + sub : ''), bar(util / 100, o.maxWidth), used].join('\n');
  }

  /** Per-model weekly limits, from the endpoint's `limits` list. */
  function scopedWeeklies(limits, sonnetShown) {
    if (!Array.isArray(limits)) return [];
    const out = [];
    for (const l of limits) {
      const model = l && l.kind === 'weekly_scoped' && l.scope && l.scope.model;
      const name = model && model.display_name;
      if (typeof name !== 'string' || !name) continue;
      // Already shown, as "Current week (Sonnet only)".
      if (sonnetShown && name.toLowerCase() === 'sonnet') continue;
      out.push({
        title: 'Current week (' + name + ')',
        limit: { utilization: l.percent, resets_at: l.resets_at },
      });
    }
    return out;
  }

  /** An amount in minor units (cents), as Claude Code writes it. */
  function money(minor, currency) {
    const code = String(currency || 'USD').toUpperCase();
    let fmt = null;
    try {
      fmt = new Intl.NumberFormat('en-US', {
        style: 'currency', currency: code, currencyDisplay: 'narrowSymbol',
      });
    } catch (e) { /* not a currency Intl knows */ }
    // A currency without cents (yen) counts in whole units already.
    const whole = fmt && fmt.resolvedOptions().maximumFractionDigits === 0;
    const amount = whole ? Math.round(minor) : minor / 100;
    return fmt ? fmt.format(amount) : code + ' ' + amount.toFixed(2);
  }

  /** Midnight-ish on the 1st of next month, in the display time zone. */
  function _firstOfNextMonth(o) {
    const now = o.now || new Date();
    const y = Number(_part(now, o.timeZone, { year: 'numeric' }, 'year'));
    const m = Number(_part(now, o.timeZone, { month: 'numeric' }, 'month'));
    // Noon UTC, so the 1st is the 1st in any zone the date is shown in.
    // Date.UTC's month is 0-based, so the 1-based `m` is next month.
    return new Date(Date.UTC(y, m, 1, 12)).toISOString();
  }

  /** The one-time "Claude Code and Cowork credit" (the endpoint's cinder_cove). */
  function creditBlock(cc, o) {
    if (!cc || typeof cc !== 'object') return null;
    let note = 'One-time credit';
    const d = cc.resets_at ? new Date(cc.resets_at) : null;
    if (d && !isNaN(d.getTime())) {
      note += DOT + 'Expires ' + d.toLocaleDateString('en-US', {
        month: 'long', day: 'numeric', timeZone: o.timeZone,
      });
    }
    return limitBlock('Claude Code and Cowork credit', cc,
      Object.assign({}, o, { override: note }));
  }

  /** Usage credits (the endpoint's extra_usage), as the TUI decides to show them. */
  function creditsBlock(x, plan, o) {
    if (!x || typeof x !== 'object') return null;
    const proOrMax = plan === 'pro' || plan === 'max';
    if (!proOrMax && plan !== 'team' && plan !== 'enterprise') return null;
    const TITLE = 'Usage credits';
    if (!x.is_enabled) return proOrMax ? TITLE + '\nUsage credits are off' : null;
    const cur = x.currency || 'USD';
    if (x.monthly_limit === null || x.monthly_limit === undefined) {
      if (proOrMax) return TITLE + '\nUnlimited';
      if (typeof x.used_credits !== 'number') return null;
      return TITLE + '\n' + money(x.used_credits, cur) + ' spent';
    }
    if (typeof x.used_credits !== 'number') return null;
    let util = x.utilization;
    if (typeof util !== 'number') {
      util = x.monthly_limit > 0
        ? Math.max(0, Math.min(100, x.used_credits / x.monthly_limit * 100))
        : 100;
    }
    return limitBlock(TITLE, { utilization: util, resets_at: _firstOfNextMonth(o) },
      Object.assign({}, o, {
        showTime: false,
        alwaysDate: true,
        extra: money(x.used_credits, cur) + ' / ' + money(x.monthly_limit, cur) + ' spent',
      }));
  }

  /** Which products this week's usage went to (the endpoint's seven_day_breakdown). */
  function byProduct(b) {
    const rows = b && Array.isArray(b.rows) ? b.rows.filter((r) => r
      && typeof r.display_name === 'string' && r.display_name
      && typeof r.percent === 'number' && r.percent > 0) : [];
    if (!rows.length) return null;
    return 'Current week by product\n'
      + rows.map((r) => r.display_name + ' ' + Math.round(r.percent) + '%').join(DOT);
  }

  function planName(plan, tier) {
    if (typeof plan !== 'string' || !plan) return '';
    let name = 'Claude ' + plan.charAt(0).toUpperCase() + plan.slice(1);
    const m = typeof tier === 'string' && /_(\d+x)$/.exec(tier);
    if (m) name += ' ' + m[1];
    return name;
  }

  function _folder(p) {
    if (typeof p !== 'string' || !p) return '';
    const parts = p.split(/[\\/]+/).filter(Boolean);
    return parts.length ? parts[parts.length - 1] : p;
  }

  /** "someone@example.com · Claude Max 20x": whose limits these are. */
  function accountLine(data) {
    const a = data.account && typeof data.account === 'object' ? data.account : {};
    const who = (typeof a.email === 'string' && a.email) || _folder(a.config_dir);
    return [who, planName(data.subscription_type, data.rate_limit_tier)]
      .filter(Boolean).join(DOT);
  }

  /** The modal's text for a report.
   *
   * Options: `cols` (the width to lay out for; default 80), plus `now` and
   * `timeZone` as for resetText. */
  function render(data, opts) {
    const o = Object.assign({}, opts);
    // The TUI lays out for its terminal's width less 2.
    o.maxWidth = Math.max(10, (o.cols || DEFAULT_COLS) - 2);
    if (!data || typeof data !== 'object') return 'Error: no usage data came back.';
    const out = [];
    const head = accountLine(data);
    if (head) out.push(head);
    if (data.error) {
      out.push('Error: ' + data.error);
      return out.join('\n\n');
    }
    const u = data.usage && typeof data.usage === 'object' ? data.usage : {};
    const plan = typeof data.subscription_type === 'string' ? data.subscription_type : null;
    const weekly = Object.assign({}, o, { alwaysDate: true });
    const blocks = [];
    const add = (b) => { if (b) blocks.push(b); };
    add(limitBlock('Current session', u.five_hour, o));
    add(limitBlock('Current week (all models)', u.seven_day, weekly));
    // The TUI shows a Sonnet-only limit for Max and Team, and when the plan
    // is unknown; the other plans have none that differs from the weekly.
    let sonnet = null;
    if (plan === null || plan === 'max' || plan === 'team') {
      sonnet = limitBlock('Current week (Sonnet only)', u.seven_day_sonnet, weekly);
      add(sonnet);
    }
    for (const s of scopedWeeklies(u.limits, sonnet !== null)) {
      add(limitBlock(s.title, s.limit, weekly));
    }
    add(creditBlock(u.cinder_cove, o));
    add(creditsBlock(u.extra_usage, plan, o));
    add(byProduct(u.seven_day_breakdown));
    if (!blocks.length) blocks.push('No plan limits reported for this account.');
    return out.concat(blocks).join('\n\n');
  }

  /** How many monospace cells fit across `el`'s content box. */
  function columnsFor(el) {
    const probe = document.createElement('span');
    probe.textContent = '0'.repeat(10);
    probe.style.position = 'absolute';
    probe.style.visibility = 'hidden';
    probe.style.whiteSpace = 'pre';
    el.appendChild(probe);
    const cell = probe.getBoundingClientRect().width / 10;
    probe.remove();
    const cs = window.getComputedStyle(el);
    const inner = el.clientWidth
      - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0);
    // No layout to measure (jsdom, a hidden element): assume a wide screen.
    return cell > 0 && inner > 0 ? Math.floor(inner / cell) : DEFAULT_COLS;
  }

  /** Handle a command_data "usage" message.  Returns false when there is no
   * modal to show it in, so the caller can fall back to printing it. */
  function show(data) {
    const modal = document.getElementById('detail-modal');
    const title = document.getElementById('modal-title');
    const body = document.getElementById('modal-body');
    if (!modal || !title || !body
        || typeof App === 'undefined' || typeof App.openModal !== 'function') {
      return false;
    }
    if (data && data.loading) {
      _awaiting = true;
      App.openModal('Usage', 'Loading usage data\u2026');
      return true;
    }
    const awaited = _awaiting;
    _awaiting = false;
    // Closed, or replaced by another modal, while it loaded: leave it be.
    if (awaited && (modal.classList.contains('hidden') || title.textContent !== 'Usage')) {
      return true;
    }
    App.openModal('Usage', '');            // shown, so its width can be measured
    App.openModal('Usage', render(data, { cols: columnsFor(body) }));
    return true;
  }

  return { show, render, bar, resetText, money, columnsFor };
})();
