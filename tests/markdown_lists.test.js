/* The agent's lists keep their nesting.
 *
 * A list item holds every line indented past its marker -- a sub-list, text
 * continuing it, a code block -- so lists nest, numbered and bulleted mixed,
 * and a numbered list keeps counting across the sub-items between its steps.
 * An item's lines keep their indentation relative to one another, and a code
 * block's lines are kept as written.  Driven through Chat.handleMessage, as
 * the page renders an assistant message.
 */

const { rendered, suite } = require('./md_harness');

const { test, assert, done } = suite('markdown lists');

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

test('so does a sub-list of the list\'s own kind, written short', () => {
  // The flattening that was reported, in the numbered form.
  is('1. step\n  1. sub a\n  2. sub b\n2. next', 'ol(step(ol(sub a, sub b)), next)',
     'two spaces under "1. "');
  is('10. step\n   1. sub\n11. next', 'ol@10(step(ol(sub)), next)', 'three under "10. "');
  is('- a\n  - b\n- c', 'ul(a(ul(b)), c)', 'bullets');
});

test('numbered lists nest in numbered lists, to any depth', () => {
  // As CommonMark writes them: under the item's text.
  is('1. a\n   1. a1\n   2. a2\n2. b\n   1. b1', 'ol(a(ol(a1, a2)), b(ol(b1)))', 'two levels');
  is('1. a\n   1. b\n      1. c\n2. d', 'ol(a(ol(b(ol(c)))), d)', 'three levels');
  is('10. a\n    1. b\n11. c', 'ol@10(a(ol(b)), c)', 'under 10');
  is('- a\n  1. x\n  2. y', 'ul(a(ol(x, y)))', 'under a bullet');
  // And short, as models can write them: two under "1. " at every level.
  is('1. a\n  1. b\n    1. c\n2. d', 'ol(a(ol(b(ol(c)))), d)', 'three levels, short');
  is('1. a\n  1. b\n    1. c\n      1. e\n2. d', 'ol(a(ol(b(ol(c(ol(e)))))), d)', 'four levels, short');
});

test('a sub-list written short moves in only what is under it', () => {
  is('1. a\n  - b\n2. c\n  - d', 'ol(a(ul(b)), c(ul(d)))', 'the next item, and its own sub-list');
  // Moved four, under "1000. ": "  - d" moved as far would be code under "- c".
  is('1000. a\n  - b\n- c\n  - d', 'ol@1000(a(ul(b))) | ul(c(ul(d)))', 'a list after it');
  // Measured against the item it is under, not the deeper one before it.
  is('1. a\n   - b\n     - c\n  - d', 'ol(a(ul(b(ul(c)), d)))', 'after a deeper item');
});

test('a numbered sub-list keeps the number it starts at', () => {
  // CommonMark lets only a list starting at 1 interrupt an item's text.
  is('1. a\n   3. x\n   4. y\n2. b', 'ol(a(ol@3(x, y)), b)', 'a sub-list from 3');
  is('1. a\n  12) x\n  13) y\n2. b', 'ol(a(ol@12(x, y)), b)', 'from 12, written short');
  is('- a\n  - b\n  3. c\n  4. d', 'ul(a(ul(b))(ol@3(c, d)))', 'after a sub-list of the other kind');
  is('1. a\n   5. x\n      - deep\n   6. y\n   7. z', 'ol(a(ol@5(x(ul(deep)), y, z)))',
     'back from a deeper list');
  assert(lists('Intro\n3. not a list').length === 0,
         'a number at the margin after text started a list');
});

test('a sub-list renumbered keeps its width', () => {
  // "12." written as "1." would put "y" four columns past x's text: code.
  const el = rendered('1. a\n   12. x\n\n          y');
  assert(!el.querySelector('.code-block'), el.innerHTML);
  assert(el.querySelector('ol[start="12"] > li').textContent.includes('y'), el.innerHTML);
});

test('and the lists around it stay tight', () => {
  // Spaced apart, every item's text would be a paragraph of its own.
  for (const text of ['1. a\n   3. x\n   4. y\n2. b', '1. a\n   5. x\n      - deep\n   6. y',
                      '- a\n  - b\n\t- c', '-\ta\n-\tb\n\t-\tc', '- first line\n  second line\n- next']) {
    const el = rendered(text);
    assert(!el.querySelector('li > p'), `${JSON.stringify(text)}: ${el.innerHTML}`);
  }
});

test('an empty item is an item', () => {
  is('- a\n-\n- b', 'ul(a, , b)', 'an empty bullet');
  assert(!rendered('- a\n-\n- b').querySelector('li > p'), 'the list was spaced apart');
  // Under an item, an empty numbered one keeps its number, and is not a list.
  assert(rendered('1. a\n   2023.').textContent.includes('2023.'), 'its number was rewritten');
});

test('five spaces after a marker begin code in the item, its text one past it', () => {
  // "- b" two in is the item's, not moved four further into its code.
  const el = rendered('-\t\ta\n  - b');
  assert(el.querySelector('ul > li .code-block'), el.innerHTML);
  assert(el.querySelector('ul > li > ul > li') && el.querySelectorAll('.code-block').length === 1,
         el.innerHTML);
  // Only the item's own line: code under it on the next is code.
  const below = rendered('-\n      3. x').querySelector('.code-block code');
  assert(below && below.textContent === '3. x', below ? JSON.stringify(below.textContent) : 'no code');
  // And a sub-list written short of that text is moved in to it, not past.
  const ten = rendered('10.      x\n  - b');
  assert(ten.querySelector('ol > li > ul > li') && ten.querySelectorAll('.code-block').length === 1,
         ten.innerHTML);
});

test('a rule one space under an item ends the list', () => {
  const el = rendered('- a\n * * *');
  assert(el.querySelector('ul + hr') && !el.querySelector('li hr'), el.innerHTML);
});

test('a tab after a marker is a gap like any other', () => {
  is('-\ta\n-\tb\n\t-\tc', 'ul(a, b(ul(c)))', 'tabs after the markers');
  is('1.\ta\n  - b', 'ol(a(ul(b)))', 'a tab after a number, a sub-list short of its text');
});

test('the next item is at the marker\'s column or one off it', () => {
  is('- a\n - b', 'ul(a, b)', 'one space in');
  is(' 9. nine\n10. ten', 'ol@9(nine, ten)', 'right-aligned numbers');
  is('  - a\n- b', 'ul(a, b)', 'second item further left');
  is('- a\n    - b\n  - c', 'ul(a(ul(b, c)))', 'sub-items at two indentations');
});

test('an item further left than the first keeps what is indented under it', () => {
  is('  - a\n- b\n  - c', 'ul(a, b(ul(c)))', 'a sub-list under the second item');
  is('- a\n    - b\n  - c\n    - d', 'ul(a(ul(b, c(ul(d)))))', 'the same, one level down');
});

test('what follows a sub-list written short goes back to the item', () => {
  // The code block under step 1, not inside "needs node".
  const el = rendered('1. Install:\n  - needs node\n   ```bash\n   npm install\n   ```\n2. Run');
  const step = el.querySelector('ol > li');
  const code = step.querySelector(':scope > .code-block code');
  assert(code && code.textContent === 'npm install', 'the code block left step 1: '
         + step.innerHTML.slice(0, 160));
  assert(!step.querySelector('ul li .code-block'), 'the code block went into the sub-item');
  // A paragraph after a blank line, likewise.
  const el2 = rendered('1. a\n  - sub\n\n   more about a\n2. b');
  const a = el2.querySelector('ol > li');
  assert(!a.querySelector('ul li').textContent.includes('more'), 'the paragraph went into the sub-item');
  assert(a.textContent.includes('more about a'), 'the paragraph left the item');
});

test('a line continuing an item, written short, carries no stray indent', () => {
  const el = rendered('1. first\n  continued\n  - sub\n2. next');
  const li = el.querySelector('ol > li');
  assert(/^first<br>\s*continued\s*<ul/.test(li.innerHTML), JSON.stringify(li.innerHTML.slice(0, 40)));
  assert(!/<br>\n? +continued/.test(li.innerHTML), 'a stray indent');
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
  assert(!/<br>/.test(li.innerHTML) && !/<p>\s*<\/p>/.test(li.innerHTML),
         'something between the item and its sub-list: ' + JSON.stringify(li.innerHTML.slice(0, 60)));
  assert(li.querySelector(':scope > ul'), 'the sub-list left the item');
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
  // A sub-list written short, in code, is code.
  for (const fence of ['```', '~~~']) {
    const c = rendered(`- a\n  ${fence}\n  1. x\n    - y\n  ${fence}`).querySelector('.code-block code');
    assert(c && c.textContent === '1. x\n  - y', `${fence}: ${c ? JSON.stringify(c.textContent) : 'no code'}`);
  }
});

test('a code block in a sub-list written short stays in it', () => {
  const el = rendered('1. a\n  - b\n    ```\n    x\n    ```\n2. c');
  const code = el.querySelector('ol > li > ul > li .code-block code');
  assert(code && code.textContent === 'x', el.innerHTML);
  assert(el.querySelectorAll(':scope > ol > li').length === 2, 'the list was split');
});

test('a code block in an item does not end it, nor take its next item', () => {
  // Every line here is past the margin, so only the code block's end lets
  // " - b" be seen as the next item.
  const el = rendered('- a\n  ```\n  x\n  ```\n - b');
  const items = el.querySelectorAll(':scope > ul > li');
  assert(items.length === 2, `${items.length} items, not 2`);
  const code = items[0].querySelector('.code-block code');
  assert(code && code.textContent === 'x', 'no code block in a');
  assert(items[1].textContent === 'b', JSON.stringify(items[1].textContent));
});

test('nor does one opened on the item\'s own line', () => {
  const el = rendered('- ```bash\n  ls\n  ```\n - b');
  const items = el.querySelectorAll(':scope > ul > li');
  assert(items.length === 2, `${items.length} items, not 2`);
  const code = items[0].querySelector('.code-block code');
  assert(code && code.textContent === 'ls', code ? JSON.stringify(code.textContent) : 'no code');
});

test('a code line\'s tabs are kept, under a sub-list written short too', () => {
  // A Makefile's recipe line starts with a tab, and fails without one.
  const el = rendered('1. Build\n  - with make:\n    ```make\n    all:\n    \tgo build\n    ```');
  const code = el.querySelector('ul li .code-block code');
  assert(code && code.textContent === 'all:\n\tgo build', code ? JSON.stringify(code.textContent) : el.innerHTML);
});

test('an indented code block is code, not list lines', () => {
  const el = rendered('Example:\n\n    1. foo\n       2. bar\n    text');
  const code = el.querySelector('.code-block code');
  assert(code && code.textContent === '1. foo\n   2. bar\ntext', code ? JSON.stringify(code.textContent) : el.innerHTML);
});

test('a code block ended by its item\'s end is over there', () => {
  // The fence at the margin opens a block of its own, holding "more".
  const el = rendered('1. x\n   ```\n   a\n```\nmore');
  const codes = [...el.querySelectorAll('.code-block code')].map((c) => c.textContent);
  assert(JSON.stringify(codes) === JSON.stringify(['a', 'more']), JSON.stringify(codes));
  // Text there likewise: no blank line goes into the code before it.
  const text = rendered('1. x\n   ```\n   a\nmore');
  const code = text.querySelector('.code-block code');
  assert(code && code.textContent === 'a', code ? JSON.stringify(code.textContent) : text.innerHTML);
});

test('lists after a code block keep their nesting, however the block was written', () => {
  // Each a code block CommonMark closes, or never opens, where a simpler
  // reading would not: the short sub-list after it must still nest.
  const after = '\n1. a\n  - b\n2. c';
  is('```\n```js\n```' + after, 'ol(a(ul(b)), c)', 'a fence with a language inside one');
  is('```x``` is a code span' + after, 'ol(a(ul(b)), c)', 'three backticks around a word');
  is('~~~\n~~~ not the end\n~~~' + after, 'ol(a(ul(b)), c)', 'tildes with words after them');
  is('````\n```\n````' + after, 'ol(a(ul(b)), c)', 'a shorter fence inside a longer one');
  is('~~~\n```\n~~~' + after, 'ol(a(ul(b)), c)', 'backticks inside tildes');
  is('1. ```\n   steps:\n     - run: x\n   ```\n2. b\n  - c',
     'ol(steps: - run: x, b(ul(c)))', 'a block opened on an item\'s own line');
  is('1. a\n  - ```\n    x\n    ```\n2. b', 'ol(a(ul(x)), b)',
     'opened on the line of a sub-item written short');
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

test('so it is after a list, and between items', () => {
  const el = rendered('- one\n- two\n\n* * *\n\nAfter');
  assert(el.querySelector('hr'), 'no rule after the list');
  assert(lists('- one\n- two\n\n* * *\n\nAfter').join(' | ') === 'ul(one, two)',
         'the rule joined the list');
  const el2 = rendered('- a\n- - -\n- b');
  assert(el2.querySelector('hr'), 'no rule between the items');
  assert(lists('- a\n- - -\n- b').join(' | ') === 'ul(a) | ul(b)', lists('- a\n- - -\n- b').join(' | '));
});

test('an item of thousands of lines renders', () => {
  // Not built with a call taking one argument per line, which an engine
  // refuses past its limit.
  const el = rendered('- the log:\n' + '  line\n'.repeat(200000) + '- next');
  assert(el.querySelectorAll('ul > li').length === 2, 'not two items');
});

test('markers nested thousands deep render, as text past a depth', () => {
  const el = rendered('- '.repeat(3000) + 'x');
  assert(el.textContent.includes('x'), 'the text was lost');
  assert(el.querySelectorAll('ul').length <= 32, 'not capped');
  const numbered = rendered('1. '.repeat(40) + 'x');
  assert(numbered.textContent.includes('1. 1. x') && !numbered.textContent.includes('\\'),
         'numbered markers past the depth: ' + JSON.stringify(numbered.textContent.slice(-40)));
});

test('markers up to that depth are lists, and leave no mark', () => {
  const el = rendered('- '.repeat(16) + 'x');
  assert(el.querySelectorAll('ul').length === 16, `${el.querySelectorAll('ul').length} lists`);
  assert(!el.textContent.includes('\\'), JSON.stringify(el.textContent));
});

test('a line of markers spaced wide is read in no time', () => {
  // Read by trying every split of every gap, it took minutes.
  const started = Date.now();
  rendered(Array(12).fill('-').join('      ') + '\n\n' + Array(12).fill('1.').join('      '));
  assert(Date.now() - started < 1000, `${Date.now() - started} ms`);
});

test('a message with Windows line endings still has its lists', () => {
  is('- a\r\n- b\r\n  - c\r\n', 'ul(a, b(ul(c)))', 'CRLF');
  is('1. a\r  - b\r2. c', 'ol(a(ul(b)), c)', 'a carriage return alone, a sub-list short');
});

test('text around lists is kept, in order', () => {
  const el = rendered('Intro\n- a\n  - b\nOutro');
  const t = el.textContent;
  assert(t.indexOf('Intro') < t.indexOf('a') && t.indexOf('b') < t.indexOf('Outro'),
         'out of order: ' + t);
});

done();
