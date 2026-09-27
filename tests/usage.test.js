/* /usage draws the account's plan limits as Claude Code's Usage screen does.
 *
 * Asked 2026-09-27: "the claude code TUI has a /usage command that tells you
 * in ascii all about your usage, including what percentage of the 5-hr limit
 * and 7-day limit's been used and when they reset. can you implement /usage
 * in orchestrator2?"
 *
 * These drive static/usage.js on its own, against the endpoint's reply as it
 * came back on 2026-09-27.  The hub's half is tests/test_usage.py.  That the
 * page opens the modal, and leaves it closed when you closed it, is checked
 * in reconnect_on_show.test.js against the real page.
 */

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const USAGE_JS = fs.readFileSync(path.join(__dirname, '..', 'static', 'usage.js'), 'utf8');

let failures = 0;
let ran = 0;

function test(name, fn) {
  ran++;
  try {
    fn();
    console.log(`  ok    ${name}`);
  } catch (e) {
    failures++;
    console.log(`  FAIL  ${name}\n        ${e && e.message}`);
  }
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg || 'assertion failed');
}

function eq(got, want, what) {
  assert(got === want, `${what || 'value'}:\n          got  ${JSON.stringify(got)}\n          want ${JSON.stringify(want)}`);
}

const dom = new JSDOM('<!doctype html><html><body></body></html>', { runScripts: 'outside-only' });
dom.window.eval(USAGE_JS + '\n;globalThis.__Usage = Usage;');
const U = dom.window.__Usage;

const FULL = '\u2588', HALF = '\u258c', EMPTY = '\u2591';
const TZ = 'America/Los_Angeles';
// 11:45am in Los Angeles on the day the endpoint was first asked.
const NOW = new Date('2026-09-27T18:45:00Z');
const AT = { now: NOW, timeZone: TZ };

function report(over) {
  const usage = {
    five_hour: { utilization: 18.0, resets_at: '2026-09-27T21:10:00.132120+00:00' },
    seven_day: { utilization: 53.0, resets_at: '2026-10-03T00:00:00.132145+00:00' },
    seven_day_sonnet: null,
    extra_usage: { is_enabled: false, monthly_limit: null, used_credits: null,
                   utilization: null, currency: null },
    limits: [
      { kind: 'session', percent: 18, resets_at: '2026-09-27T21:10:00.132120+00:00', scope: null },
      { kind: 'weekly_all', percent: 53, resets_at: '2026-10-03T00:00:00.132145+00:00', scope: null },
      { kind: 'weekly_scoped', percent: 0, resets_at: '2026-10-03T00:00:00+00:00',
        scope: { model: { id: null, display_name: 'Fable' }, surface: null } },
    ],
    seven_day_breakdown: { rows: [
      { key: 'claude_code', display_name: 'Claude Code', percent: 100 },
      { key: 'chat', display_name: 'Chats', percent: 0 },
    ] },
  };
  const data = {
    usage, subscription_type: 'max', rate_limit_tier: 'default_claude_max_20x',
    account: { email: 'someone@example.com', config_dir: 'D:/accounts/.claude-work' },
  };
  return over ? over(data) || data : data;
}

function blocks(text) { return text.split('\n\n'); }
function block(text, title) {
  return blocks(text).find((b) => b.split('\n')[0].split(' \u00b7 ')[0] === title) || null;
}

console.log('\nusage\n');

/* ---- the whole screen ---------------------------------------------------- */

test('the screen reads as Claude Code\'s does', () => {
  eq(U.render(report(), AT), [
    'someone@example.com \u00b7 Claude Max 20x',
    '',
    'Current session',
    FULL.repeat(9) + EMPTY.repeat(41) + ' 18% used',
    'Resets 2:10pm (America/Los_Angeles)',
    '',
    'Current week (all models)',
    FULL.repeat(26) + HALF + EMPTY.repeat(23) + ' 53% used',
    'Resets Oct 2, 5pm (America/Los_Angeles)',
    '',
    'Current week (Fable)',
    EMPTY.repeat(50) + ' 0% used',
    'Resets Oct 2, 5pm (America/Los_Angeles)',
    '',
    'Usage credits',
    'Usage credits are off',
    '',
    'Current week by product',
    'Claude Code 100%',
  ].join('\n'), 'screen');
});

test('a narrow modal gets the narrow layout, the bar across it', () => {
  const s = U.render(report(), Object.assign({ cols: 44 }, AT));
  eq(block(s, 'Current session'), [
    'Current session \u00b7 Resets 2:10pm (America/Los_Angeles)',
    FULL.repeat(7) + HALF + EMPTY.repeat(34),          // 42 = 44 - 2
    '18% used',
  ].join('\n'), 'narrow block');
});

test('the wide layout starts where the TUI\'s does', () => {
  // The TUI lays out for its width less 2, and goes wide at 62.
  const wide = U.render(report(), Object.assign({ cols: 64 }, AT));
  const narrow = U.render(report(), Object.assign({ cols: 63 }, AT));
  assert(block(wide, 'Current session').split('\n')[0] === 'Current session', 'not wide at 64');
  assert(block(narrow, 'Current session').startsWith('Current session \u00b7 '), 'not narrow at 63');
  eq(block(narrow, 'Current session').split('\n')[1].length, 61, 'narrow bar width');
});

test('a very narrow modal still gets a bar of some width', () => {
  const s = U.render(report(), Object.assign({ cols: 4 }, AT));
  eq(block(s, 'Current session').split('\n')[1].length, 10, 'bar width');
});

test('without a width it lays out for 80 columns', () => {
  eq(U.render(report(), AT), U.render(report(), Object.assign({ cols: 80 }, AT)), 'default');
});

/* ---- "N% used" ------------------------------------------------------------ */

test('the percentage is rounded down, as the TUI does', () => {
  const s = U.render(report((d) => { d.usage.seven_day.utilization = 97.9; }), AT);
  assert(block(s, 'Current week (all models)').includes(' 97% used'), s);
});

test('a limit with no figure is left out', () => {
  const s = U.render(report((d) => {
    d.usage.five_hour.utilization = null;
    d.usage.seven_day = { resets_at: '2026-10-03T00:00:00Z' };
  }), AT);
  assert(!s.includes('Current session'), 'shown with a null figure');
  assert(!s.includes('Current week (all models)'), 'shown with no figure');
  assert(s.includes('Current week (Fable)'), 'took the others with it');
});

test('a limit with no reset time says nothing about resetting', () => {
  // null is what the endpoint sends; read as a time it would be 1970.
  for (const none of [undefined, null, '']) {
    const s = U.render(report((d) => { d.usage.five_hour.resets_at = none; }), AT);
    eq(block(s, 'Current session'),
       'Current session\n' + FULL.repeat(9) + EMPTY.repeat(41) + ' 18% used', String(none));
    // Nor an empty line where it would have gone.
    assert(!s.includes('\n\n\n'), 'an empty line after the bar:\n' + s);
  }
});

test('nor in the narrow layout', () => {
  const s = U.render(report((d) => { d.usage.five_hour.resets_at = null; }),
                     Object.assign({ cols: 44 }, AT));
  eq(block(s, 'Current session'),
     'Current session\n' + FULL.repeat(7) + HALF + EMPTY.repeat(34) + '\n18% used', 'narrow');
});

test('an unreadable reset time says nothing about resetting', () => {
  const s = U.render(report((d) => { d.usage.five_hour.resets_at = 'soon'; }), AT);
  assert(!block(s, 'Current session').includes('Resets'), s);
});

/* ---- the bar --------------------------------------------------------------- */

test('the bar fills in proportion, a half cell when it is closer to half', () => {
  eq(U.bar(0, 10), EMPTY.repeat(10), 'empty');
  eq(U.bar(1, 10), FULL.repeat(10), 'full');
  eq(U.bar(0.5, 10), FULL.repeat(5) + EMPTY.repeat(5), 'half');
  eq(U.bar(0.54, 10), FULL.repeat(5) + EMPTY.repeat(5), 'just under a half cell more');
  eq(U.bar(0.55, 10), FULL.repeat(5) + HALF + EMPTY.repeat(4), 'a half cell more');
  eq(U.bar(0.99, 10), FULL.repeat(9) + HALF, 'nearly full');
});

test('the bar never over- or under-runs its width', () => {
  eq(U.bar(1.7, 10), FULL.repeat(10), 'over 100%');
  eq(U.bar(-0.2, 10), EMPTY.repeat(10), 'below 0');
  eq(U.bar(NaN, 10), EMPTY.repeat(10), 'not a number');
});

/* ---- "Resets ..." ---------------------------------------------------------- */

test('a reset within a day is a time; the TUI\'s lower-case am/pm', () => {
  eq(U.resetText('2026-09-27T21:10:00Z', AT), '2:10pm (America/Los_Angeles)', 'time');
});

test('am/pm after a narrow no-break space is lower-cased too', () => {
  // ICU 72+ puts U+202F before the AM/PM, and some engines pass it on (this
  // Node does not, so it is made to here).  The TUI handles both.
  const proto = dom.window.Date.prototype;
  const realTime = proto.toLocaleTimeString;
  const realDate = proto.toLocaleString;
  proto.toLocaleTimeString = function (...a) {
    return realTime.apply(this, a).replace(' PM', ' PM');
  };
  proto.toLocaleString = function (...a) {
    return realDate.apply(this, a).replace(' PM', ' PM');
  };
  try {
    eq(U.resetText('2026-09-27T21:10:00Z', AT), '2:10pm (America/Los_Angeles)', 'time');
    eq(U.resetText('2026-09-29T00:30:00Z', AT), 'Sep 28, 5:30pm (America/Los_Angeles)', 'date');
  } finally {
    proto.toLocaleTimeString = realTime;
    proto.toLocaleString = realDate;
  }
});

test('on the hour, the minutes are left off', () => {
  eq(U.resetText('2026-09-27T21:00:00Z', AT), '2pm (America/Los_Angeles)', 'hour');
});

test('a single-digit minute keeps its zero', () => {
  eq(U.resetText('2026-09-27T21:05:00Z', AT), '2:05pm (America/Los_Angeles)', 'minute');
});

test('a morning reset says am', () => {
  eq(U.resetText('2026-09-28T16:30:00Z', AT), '9:30am (America/Los_Angeles)', 'am');
});

test('more than a day away, the date is shown too', () => {
  eq(U.resetText('2026-09-29T00:30:00Z', AT), 'Sep 28, 5:30pm (America/Los_Angeles)', 'date');
});

test('exactly a day away is still within the day', () => {
  eq(U.resetText('2026-09-28T18:45:00Z', AT), '11:45am (America/Los_Angeles)', 'boundary');
});

test('a weekly limit always shows the date, even when it resets today', () => {
  const s = U.render(report((d) => { d.usage.seven_day.resets_at = '2026-09-27T21:00:00Z'; }), AT);
  assert(block(s, 'Current week (all models)').endsWith('Resets Sep 27, 2pm (America/Los_Angeles)'), s);
  eq(U.resetText('2026-09-27T21:00:00Z', Object.assign({ alwaysDate: true }, AT)),
     'Sep 27, 2pm (America/Los_Angeles)', 'alwaysDate');
});

test('the session limit does not', () => {
  const s = U.render(report(), AT);
  assert(block(s, 'Current session').endsWith('Resets 2:10pm (America/Los_Angeles)'), s);
});

test('a reset in another year says which', () => {
  const at = { now: new Date('2026-12-30T18:00:00Z'), timeZone: TZ };
  eq(U.resetText('2027-01-02T01:00:00Z', at), 'Jan 1, 2027, 5pm (America/Los_Angeles)', 'year');
});

test('the year is the year where it is shown', () => {
  // 1am on Jan 1 in UTC is still the old year in Los Angeles.
  const at = { now: new Date('2026-12-28T18:00:00Z'), timeZone: TZ };
  eq(U.resetText('2027-01-01T01:00:00Z', at), 'Dec 31, 5pm (America/Los_Angeles)', 'same year');
});

test('epoch seconds are read as well as ISO', () => {
  eq(U.resetText(Date.parse('2026-09-27T21:10:00Z') / 1000, AT),
     '2:10pm (America/Los_Angeles)', 'epoch');
});

test('an unreadable time is no time', () => {
  eq(U.resetText('not a date', AT), null, 'garbage');
});

test('without a zone it is this browser\'s, and says which', () => {
  const zone = dom.window.Intl.DateTimeFormat().resolvedOptions().timeZone;
  const s = U.resetText('2026-09-27T21:10:00Z', { now: NOW });
  assert(s.endsWith(' (' + zone + ')'), s);
});

test('on the hour is on the hour where it is shown', () => {
  // Half past in UTC is on the hour in India, so no minutes there.
  eq(U.resetText('2026-09-27T21:30:00Z', { now: NOW, timeZone: 'Asia/Kolkata' }),
     '3am (Asia/Kolkata)', 'Kolkata');
});

test('it is the time where it is shown, not in UTC', () => {
  // +5:30: past midnight there, but still within the day, so still a time.
  eq(U.resetText('2026-09-27T21:10:00Z', { now: NOW, timeZone: 'Asia/Kolkata' }),
     '2:40am (Asia/Kolkata)', 'Kolkata');
});

/* ---- which limits ---------------------------------------------------------- */

test('Max, Team and an unknown plan get the Sonnet-only limit', () => {
  for (const plan of ['max', 'team', null]) {
    const s = U.render(report((d) => {
      d.subscription_type = plan;
      d.usage.seven_day_sonnet = { utilization: 40, resets_at: '2026-10-03T00:00:00Z' };
    }), AT);
    assert(block(s, 'Current week (Sonnet only)'), 'missing for ' + plan);
  }
});

test('Pro and Enterprise do not', () => {
  for (const plan of ['pro', 'enterprise']) {
    const s = U.render(report((d) => {
      d.subscription_type = plan;
      d.usage.seven_day_sonnet = { utilization: 40, resets_at: '2026-10-03T00:00:00Z' };
    }), AT);
    assert(!s.includes('Sonnet only'), 'shown for ' + plan);
  }
});

test('the Sonnet-only limit comes after the weekly one and says when it resets', () => {
  const s = U.render(report((d) => {
    d.usage.seven_day_sonnet = { utilization: 40.2, resets_at: '2026-10-03T00:00:00Z' };
  }), AT);
  const titles = blocks(s).map((b) => b.split('\n')[0]);
  eq(titles.indexOf('Current week (Sonnet only)'), titles.indexOf('Current week (all models)') + 1, 'order');
  assert(block(s, 'Current week (Sonnet only)').endsWith(' 40% used\nResets Oct 2, 5pm (America/Los_Angeles)'), s);
});

test('each per-model weekly limit gets its own bar', () => {
  const s = U.render(report((d) => {
    d.usage.limits.push({ kind: 'weekly_scoped', percent: 12, resets_at: '2026-10-03T00:00:00Z',
                          scope: { model: { display_name: 'Opus' } } });
  }), AT);
  assert(block(s, 'Current week (Fable)'), 'Fable missing');
  assert(block(s, 'Current week (Opus)').includes(' 12% used'), s);
});

test('and, being weekly, always says the date', () => {
  const s = U.render(report((d) => { d.usage.limits[2].resets_at = '2026-09-27T21:00:00Z'; }), AT);
  assert(block(s, 'Current week (Fable)').endsWith('Resets Sep 27, 2pm (America/Los_Angeles)'), s);
});

test('the other kinds in the limits list are not shown twice', () => {
  const s = U.render(report(), AT);
  eq(blocks(s).filter((b) => b.includes('18% used')).length, 1, 'session shown');
  eq(blocks(s).filter((b) => b.includes('53% used')).length, 1, 'weekly shown');
});

test('a per-model limit without a model is skipped', () => {
  const s = U.render(report((d) => {
    d.usage.limits = [
      { kind: 'weekly_scoped', percent: 5, scope: { surface: 'cowork' } },
      { kind: 'weekly_scoped', percent: 5, scope: { model: { display_name: '' } } },
      { kind: 'weekly_scoped', percent: 5, scope: null },
      null,
    ];
  }), AT);
  assert(!s.includes('Current week ('.concat(')')) && !s.includes(' 5% used'), s);
});

test('a model-scoped limit of another kind is not a weekly one', () => {
  const s = U.render(report((d) => {
    d.usage.limits.push({ kind: 'session_scoped', percent: 7, resets_at: '2026-09-27T21:10:00Z',
                          scope: { model: { display_name: 'Opus' } } });
  }), AT);
  assert(!s.includes('Opus') && !s.includes(' 7% used'), s);
});

test('a Sonnet per-model limit is not shown twice', () => {
  const sonnet = { kind: 'weekly_scoped', percent: 40, resets_at: '2026-10-03T00:00:00Z',
                   scope: { model: { display_name: 'Sonnet' } } };
  const both = U.render(report((d) => {
    d.usage.seven_day_sonnet = { utilization: 40, resets_at: '2026-10-03T00:00:00Z' };
    d.usage.limits.push(sonnet);
  }), AT);
  assert(!block(both, 'Current week (Sonnet)'), 'shown twice');
  assert(block(both, 'Current week (Fable)'), 'took the other models with it');
  // ...but it is the only place a Pro plan's would show.
  const pro = U.render(report((d) => {
    d.subscription_type = 'pro';
    d.usage.seven_day_sonnet = { utilization: 40, resets_at: '2026-10-03T00:00:00Z' };
    d.usage.limits.push(sonnet);
  }), AT);
  assert(block(pro, 'Current week (Sonnet)'), 'lost with no Sonnet-only bar');
});

test('the one-time credit shows when it expires', () => {
  const s = U.render(report((d) => {
    d.usage.cinder_cove = { utilization: 25, resets_at: '2026-10-05T12:00:00Z' };
  }), AT);
  eq(block(s, 'Claude Code and Cowork credit'), [
    'Claude Code and Cowork credit',
    FULL.repeat(12) + HALF + EMPTY.repeat(37) + ' 25% used',
    'One-time credit \u00b7 Expires October 5',
  ].join('\n'), 'credit');
});

test('a one-time credit with no expiry says only that', () => {
  for (const when of [undefined, null, 'soon']) {
    const s = U.render(report((d) => {
      d.usage.cinder_cove = { utilization: 25, resets_at: when };
    }), AT);
    assert(block(s, 'Claude Code and Cowork credit').endsWith('\nOne-time credit'), s);
  }
});

/* ---- usage credits --------------------------------------------------------- */

function credits(plan, extra) {
  const s = U.render(report((d) => {
    d.subscription_type = plan;
    d.usage.extra_usage = Object.assign({ is_enabled: true, currency: 'USD' }, extra);
  }), AT);
  return block(s, 'Usage credits');
}

test('usage credits that are off say so, on Pro and Max', () => {
  eq(credits('max', { is_enabled: false }), 'Usage credits\nUsage credits are off', 'max');
  eq(credits('pro', { is_enabled: false }), 'Usage credits\nUsage credits are off', 'pro');
});

test('on Team and Enterprise, off is not mentioned', () => {
  eq(credits('team', { is_enabled: false }), null, 'team');
  eq(credits('enterprise', { is_enabled: false }), null, 'enterprise');
});

test('but credits they have are shown', () => {
  for (const plan of ['team', 'enterprise']) {
    const b = credits(plan, { monthly_limit: 10000, used_credits: 5000 });
    assert(b && b.includes(' 50% used'), plan + ': ' + b);
  }
});

test('a plan without usage credits shows none', () => {
  eq(credits('free', { monthly_limit: 1000, used_credits: 5 }), null, 'free');
  eq(credits(null, { monthly_limit: 1000, used_credits: 5 }), null, 'unknown');
});

test('credits with a monthly limit are a bar, with what is spent', () => {
  eq(credits('max', { monthly_limit: 10000, used_credits: 1234, utilization: 12.34 }), [
    'Usage credits',
    FULL.repeat(6) + EMPTY.repeat(44) + ' 12% used',
    '$12.34 / $100.00 spent \u00b7 Resets Oct 1 (America/Los_Angeles)',
  ].join('\n'), 'bar');
});

test('without a figure from the endpoint it is worked out', () => {
  assert(credits('max', { monthly_limit: 10000, used_credits: 2500, utilization: null })
    .includes(' 25% used'), 'not worked out');
  assert(credits('max', { monthly_limit: 10000, used_credits: 99999 })
    .includes(' 100% used'), 'over the limit is not capped');
  assert(credits('max', { monthly_limit: 0, used_credits: 0 })
    .includes(' 100% used'), 'a zero limit is not full');
});

test('credits with no limit: unlimited on Pro and Max, what is spent on Team', () => {
  eq(credits('max', { monthly_limit: null, used_credits: 1234 }), 'Usage credits\nUnlimited', 'max');
  eq(credits('pro', { used_credits: 1234 }), 'Usage credits\nUnlimited', 'no limit given at all');
  eq(credits('team', { monthly_limit: null, used_credits: 1234 }), 'Usage credits\n$12.34 spent', 'team');
  eq(credits('team', { monthly_limit: null, used_credits: null }), null, 'team, nothing spent');
});

test('credits with no spend figure are left out', () => {
  eq(credits('max', { monthly_limit: 10000, used_credits: null }), null, 'no figure');
});

test('credits reset on the 1st of next month, even in December', () => {
  const s = U.render(report((d) => {
    d.usage.extra_usage = { is_enabled: true, monthly_limit: 10000, used_credits: 0, currency: 'USD' };
  }), { now: new Date('2026-12-15T18:00:00Z'), timeZone: TZ });
  assert(block(s, 'Usage credits').endsWith('Resets Jan 1, 2027 (America/Los_Angeles)'), s);
});

test('money is written as Claude Code writes it', () => {
  eq(U.money(1234, 'USD'), '$12.34', 'dollars');
  eq(U.money(123456789, 'usd'), '$1,234,567.89', 'thousands, lower-case code');
  eq(U.money(5, 'USD'), '$0.05', 'cents');
  eq(U.money(1234, 'JPY'), '\u00a51,234', 'a currency without cents');
  eq(U.money(1234, null), '$12.34', 'no currency: dollars');
  eq(U.money(1230, 'nope'), 'NOPE 12.30', 'not a currency code');
});

/* ---- whose, and when it went wrong ----------------------------------------- */

test('it says whose limits these are', () => {
  eq(U.render(report(), AT).split('\n')[0], 'someone@example.com \u00b7 Claude Max 20x', 'head');
});

test('without an email, the account folder', () => {
  for (const dir of ['D:/accounts/.claude-work', 'D:\\accounts\\.claude-work\\']) {
    const s = U.render(report((d) => { d.account = { email: null, config_dir: dir }; }), AT);
    eq(s.split('\n')[0], '.claude-work \u00b7 Claude Max 20x', dir);
  }
});

test('the plan is named from the login', () => {
  const head = (plan, tier) => U.render(report((d) => {
    d.subscription_type = plan; d.rate_limit_tier = tier; d.account = {};
  }), AT).split('\n')[0];
  eq(head('pro', 'default_claude_pro'), 'Claude Pro', 'pro');
  eq(head('max', 'default_claude_max_5x'), 'Claude Max 5x', 'max 5x');
  eq(head('team', null), 'Claude Team', 'team');
  eq(head('enterprise', undefined), 'Claude Enterprise', 'enterprise');
  eq(head('ultra', null), 'Claude Ultra', 'a plan it does not know');
});

test('with neither, it goes straight to the limits', () => {
  const s = U.render(report((d) => { d.account = null; d.subscription_type = null; }), AT);
  eq(s.split('\n')[0], 'Current session', 'first line');
});

test('an error is shown instead of the limits, under whose they were', () => {
  eq(U.render({ error: 'This account\'s sign-in token has expired.',
                account: { email: 'someone@example.com' } }, AT),
     'someone@example.com\n\nError: This account\'s sign-in token has expired.', 'error');
});

test('an error with no account is just the error', () => {
  eq(U.render({ error: 'Failed to load usage data (see the hub log).' }, AT),
     'Error: Failed to load usage data (see the hub log).', 'bare');
});

test('an empty reply says there is nothing to show', () => {
  eq(U.render({ usage: {} }, AT), 'No plan limits reported for this account.', 'empty');
  eq(U.render({}, AT), 'No plan limits reported for this account.', 'no usage at all');
});

test('no reply at all is an error', () => {
  eq(U.render(null, AT), 'Error: no usage data came back.', 'null');
});

test('only the products this week\'s usage went to are listed', () => {
  const s = U.render(report((d) => {
    d.usage.seven_day_breakdown.rows = [
      { display_name: 'Claude Code', percent: 81.6 },
      { display_name: 'Chats', percent: 18.4 },
      { display_name: 'Cowork', percent: 0 },
      { display_name: '', percent: 50 },
      { percent: 50 },
    ];
  }), AT);
  eq(block(s, 'Current week by product'),
     'Current week by product\nClaude Code 82% \u00b7 Chats 18%', 'products');
});

test('with none, there is no product line', () => {
  const s = U.render(report((d) => { d.usage.seven_day_breakdown.rows[0].percent = 0; }), AT);
  assert(!s.includes('by product'), s);
  const t = U.render(report((d) => { d.usage.seven_day_breakdown = { rows: 'x' }; }), AT);
  assert(!t.includes('by product'), t);
});

/* ---- the modal's width ------------------------------------------------------ */

test('with no layout to measure, the width is taken to be 80', () => {
  // jsdom lays nothing out; neither does a hidden element.
  const el = dom.window.document.createElement('div');
  dom.window.document.body.appendChild(el);
  eq(U.columnsFor(el), 80, 'columns');
  eq(el.childNodes.length, 0, 'the probe was left behind');
});

test('it counts the cells across the content box', () => {
  const win = dom.window;
  const el = win.document.createElement('div');
  win.document.body.appendChild(el);
  Object.defineProperty(el, 'clientWidth', { value: 432 });
  const realCS = win.getComputedStyle;
  const realRect = win.HTMLElement.prototype.getBoundingClientRect;
  win.getComputedStyle = () => ({ paddingLeft: '16px', paddingRight: '16px' });
  win.HTMLElement.prototype.getBoundingClientRect = function () {
    return { width: this.textContent === '0000000000' ? 72 : 0 };   // 7.2px a cell
  };
  try {
    eq(U.columnsFor(el), 55, 'columns');      // (432 - 32) / 7.2 = 55.5
  } finally {
    win.getComputedStyle = realCS;
    win.HTMLElement.prototype.getBoundingClientRect = realRect;
  }
});

console.log(`\n${ran - failures}/${ran} passed`);
if (failures) process.exit(1);
