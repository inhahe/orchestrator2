/* The agent's lists keep their nesting.
 *
 * A list item holds every line indented past its marker -- a sub-list, text
 * continuing it, a code block -- so lists nest, numbered and bulleted mixed,
 * and a numbered list keeps counting across the sub-items between its steps.
 * An item's lines keep their indentation relative to one another, and a code
 * block's lines are kept as written.  Driven through Chat.handleMessage, as
 * the page renders an assistant message.
 */

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const CHAT_JS = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'chat.js'), 'utf8');

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

/* The rendered message for assistant text *text*. */
function rendered(text) {
  const dom = new JSDOM(
    '<!doctype html><html><body><div id="messages"></div></body></html>',
    { runScripts: 'outside-only' });
  const win = dom.window;
  const pending = [];
  win.setTimeout = (fn) => { pending.push(fn); return pending.length; };
  win.clearTimeout = () => {};
  win.requestAnimationFrame = (fn) => { pending.push(() => fn(0)); return pending.length; };
  win.cancelAnimationFrame = () => {};
  win.console = Object.assign(Object.create(console), {
    warn: (...a) => { throw new Error('chat.js warned: ' + a.join(' ')); },
    error: (...a) => { throw new Error('chat.js errored: ' + a.join(' ')); },
    log: () => {},
  });
  win.eval(CHAT_JS + '\n;window.Chat = Chat;');
  win.Chat.init();
  win.Chat.handleMessage({ type: 'assistant_text', content: text, delta: false });
  for (let g = 0; pending.length && g < 200; g++) pending.shift()();
  const el = win.document.querySelector('.md-rendered');
  assert(el, 'nothing rendered');
  return el;
}

/* A list as a compact string: ul(a, b(ul(c))) -- each item its own text,
 * then any list inside it; ol carries a start other than 1 as ol@3(...). */
function shape(list) {
  const kind = list.nodeName.toLowerCase();
  const start = list.getAttribute('start');
  const items = [...list.children].map((li) => {
    const own = [...li.childNodes]
      .filter((n) => !(n.nodeName === 'UL' || n.nodeName === 'OL'))
      .map((n) => n.textContent).join('').replace(/\s+/g, ' ').trim();
    const subs = [...li.children].filter((c) => c.nodeName === 'UL' || c.nodeName === 'OL');
    return own + subs.map((s) => '(' + shape(s) + ')').join('');
  });
  return `${kind}${start ? '@' + start : ''}(${items.join(', ')})`;
}

/* Every top-level list of the message, in order. */
function lists(text) {
  return [...rendered(text).children]
    .filter((c) => c.nodeName === 'UL' || c.nodeName === 'OL')
    .map(shape);
}

function is(text, expected, what) {
  const got = lists(text).join(' | ');
  assert(got === expected, `${what}:\n          expected ${expected}\n          got      ${got}`);
}

console.log('\nmarkdown lists\n');

// ---- nesting ---------------------------------------------------------------

test('a list of lists keeps its nesting', () => {
  is('- fruit\n  - apple\n  - pear\n- veg\n  - leek',
     'ul(fruit(ul(apple, pear)), veg(ul(leek)))', 'two levels');
});

test('to any depth', () => {
  is('- a\n  - b\n    - c\n      - d\n- e',
     'ul(a(ul(b(ul(c(ul(d)))))), e)', 'four levels');
});

test('a numbered list keeps counting across the sub-items between its steps', () => {
  is('1. First\n   - detail a\n   - detail b\n2. Second\n3. Third',
     'ol(First(ul(detail a, detail b)), Second, Third)', 'steps with details');
});

test('numbered inside bulleted, and bulleted inside numbered', () => {
  is('- plan\n  1. one\n  2. two\n- done', 'ul(plan(ol(one, two)), done)', 'ol in ul');
  is('1. step\n   - a\n     1. deep\n2. next', 'ol(step(ul(a(ol(deep)))), next)', 'three kinds deep');
});

test('a sub-list indented less than its item\'s text is still the item\'s', () => {
  // The model often writes two spaces under "1. ", where the text is at 3.
  is('1. step\n  - detail\n2. next', 'ol(step(ul(detail)), next)', 'two-space sub-list');
});

test('nesting holds under items numbered 10 and up', () => {
  // Their text starts a column further in, which must not flatten what is
  // indented between the marker and the text.
  is('10. step\n  - a\n    - b\n11. next', 'ol@10(step(ul(a(ul(b)))), next)', 'step 10');
});

test('an item of the list\'s own kind indented less than the text is the next item', () => {
  is('- a\n - b', 'ul(a, b)', 'one space in');
  is(' 9. nine\n10. ten', 'ol@9(nine, ten)', 'right-aligned numbers');
  is('  - a\n- b', 'ul(a, b)', 'second item further left');
  is('- a\n    - b\n  - c', 'ul(a(ul(b, c)))', 'sub-items at two indentations');
});

test('a tab that reaches past an item\'s text keeps the columns beyond it', () => {
  is('- a\n  - b\n\t- c', 'ul(a(ul(b(ul(c)))))', 'tab under two spaces, under a');
});

test('tabs indent a sub-list too', () => {
  is('- a\n\t- b\n- c', 'ul(a(ul(b)), c)', 'tab-indented');
});

test('a tab is four columns, deeper than a two-space list', () => {
  is('  - a\n\t- b', 'ul(a(ul(b)))', 'tab under two spaces');
});

test('a blank line above a sub-list adds no gap of its own', () => {
  // The sub-list has its margin; a kept blank line would add a second gap.
  const el = rendered('- item\n\n  - sub');
  const li = el.querySelector('ul > li');
  assert(/^item<ul/.test(li.innerHTML), 'something between the item and its sub-list: '
         + JSON.stringify(li.innerHTML.slice(0, 40)));
});

// ---- what else an item holds -------------------------------------------------

test('a line continuing an item stays in it', () => {
  const el = rendered('- first line\n  second line\n- next');
  const [first] = el.querySelectorAll('ul > li');
  assert(first.textContent.includes('second line'), 'the continuation left the item');
  assert(el.querySelectorAll('ul').length === 1, 'the list was split');
});

test('a code block in an item stays in it', () => {
  const el = rendered('1. Run:\n   ```bash\n   cargo test\n   ```\n2. Done');
  const items = el.querySelectorAll('ol > li');
  assert(items.length === 2, `${items.length} items, not 2`);
  const code = items[0].querySelector('.code-block code');
  assert(code && code.textContent === 'cargo test', 'no code block in the first item');
});

test('a code block in an item keeps its lines as written', () => {
  // Blank lines, a run of them, and a line that looks like a list item.
  const el = rendered('1. Edit:\n   ```yaml\n   foo:\n\n   - bar\n\n\n   baz\n   ```\n2. Done');
  const code = el.querySelector('ol > li .code-block code');
  assert(code, 'no code block in the item');
  assert(code.textContent === 'foo:\n\n- bar\n\n\nbaz', JSON.stringify(code.textContent));
  assert(el.querySelectorAll('ol > li').length === 2, 'the code block split the list');
});

test('a code fence may be indented up to three spaces', () => {
  const el = rendered('1. Run:\n    ```bash\n    ls\n    ```\n2. next');
  const code = el.querySelector('ol > li .code-block code');
  assert(code && code.textContent === 'ls', 'fence four in under "1. " not taken');
  const top = rendered('   ```\n   x\n     y\n   ```');
  const c2 = top.querySelector('.code-block code');
  assert(c2 && c2.textContent === 'x\n  y', 'top-level fence three in: '
         + (c2 ? JSON.stringify(c2.textContent) : 'none'));
});

test('a paragraph after a blank line, indented under the item, is the item\'s', () => {
  const el = rendered('- item\n\n  more about it\n- next');
  const [first] = el.querySelectorAll('ul > li');
  assert(first.textContent.includes('more about it'), 'the paragraph left the item');
});

test('inline markdown in an item still renders', () => {
  const el = rendered('- **bold** and `code`\n  - *sub*');
  assert(el.querySelector('ul > li strong'), 'no bold');
  assert(el.querySelector('ul > li code'), 'no inline code');
  assert(el.querySelector('ul > li > ul > li em'), 'no italic in the sub-item');
});

// ---- where a list ends ---------------------------------------------------------

test('blank lines between items do not split a list', () => {
  is('- a\n\n- b\n\n\n- c', 'ul(a, b, c)', 'loose bullets');
  is('1. a\n\n2. b', 'ol(a, b)', 'loose numbers');
});

test('text at the margin ends the list', () => {
  const el = rendered('- a\n- b\nafter');
  assert(lists('- a\n- b\nafter').join() === 'ul(a, b)', 'the text joined the list');
  assert(el.textContent.includes('after'), 'the text was lost');
});

test('an item of the other kind at the list\'s indentation starts a new list', () => {
  is('1. one\n- bullet', 'ol(one) | ul(bullet)', 'ol then ul');
});

test('a numbered list starts at the number the model wrote', () => {
  is('3. three\n4. four', 'ol@3(three, four)', 'start at 3');
});

test('a dash that is not a marker is not a list', () => {
  assert(lists('-5 degrees, and -x').length === 0, 'made a list of a minus sign');
});

test('a rule written with spaces is a rule, not lists inside lists', () => {
  for (const r of ['* * *', '- - -', '_ _ _', '- - - - - - - -']) {
    const el = rendered('above\n' + r + '\nbelow');
    assert(el.querySelector('hr'), `${r}: no rule`);
    assert(!el.querySelector('ul'), `${r}: made a list`);
  }
});

test('markers nested thousands deep render, as text past a depth', () => {
  const el = rendered('- '.repeat(3000) + 'x');
  assert(el.textContent.includes('x'), 'the text was lost');
  assert(el.querySelectorAll('ul').length <= 32, 'not capped');
});

test('a message with Windows line endings still has its lists', () => {
  is('- a\r\n- b\r\n  - c\r\n', 'ul(a, b(ul(c)))', 'CRLF');
});

test('text around lists is kept, in order', () => {
  const el = rendered('Intro\n- a\n  - b\nOutro');
  const t = el.textContent;
  assert(t.indexOf('Intro') < t.indexOf('a') && t.indexOf('b') < t.indexOf('Outro'),
         'out of order: ' + t);
});

console.log(`\n${ran - failures}/${ran} passed`);
process.exit(failures ? 1 : 0);
