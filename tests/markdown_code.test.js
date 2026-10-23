/* Code in the agent's messages is shown as written.
 *
 * A code span is out of reach of the emphasis around it, and a code block
 * opens on any info string a model writes (```c++, ```c#, ```python title=x)
 * and closes on a fence at least as long as its own -- otherwise its closing
 * fence would open a block that swallowed the rest of the message.  Driven
 * through Chat.handleMessage, as the page renders an assistant message.
 */

const { rendered, suite } = require('./md_harness');

const { test, assert, done } = suite('markdown code');

function codeBlocks(el) {
  return [...el.querySelectorAll('.code-block')].map((b) => ({
    lang: b.getAttribute('data-lang') || '',
    code: b.querySelector('code').textContent,
  }));
}

// ---- code spans ---------------------------------------------------------------

test('a code span keeps its asterisks', () => {
  const el = rendered('Use `*args` and `**kwargs`, or `src/**/*.ts`.');
  assert(!el.querySelector('em') && !el.querySelector('strong'), el.innerHTML);
  const spans = [...el.querySelectorAll('code.md-code')].map((c) => c.textContent);
  assert(JSON.stringify(spans) === JSON.stringify(['*args', '**kwargs', 'src/**/*.ts']),
         JSON.stringify(spans));
});

test('emphasis around a code span still renders', () => {
  const el = rendered('**see `x`** and *`y`*');
  assert(el.querySelector('strong code.md-code'), 'no code in the bold');
  assert(el.querySelector('em code.md-code'), 'no code in the italic');
});

// ---- code blocks ---------------------------------------------------------------

test('a block opens on any info string, its first word the language', () => {
  for (const [info, lang] of [['c++', 'c++'], ['c#', 'c#'], ['objective-c++', 'objective-c++'],
                              ['python title="x.py"', 'python'], ['', '']]) {
    const el = rendered('```' + info + '\nint *p = a * b;\n```\nMore text');
    const blocks = codeBlocks(el);
    assert(blocks.length === 1, `${info}: ${blocks.length} blocks`);
    assert(blocks[0].code === 'int *p = a * b;', `${info}: ${JSON.stringify(blocks[0].code)}`);
    assert(blocks[0].lang === lang, `${info}: language ${JSON.stringify(blocks[0].lang)}`);
    const label = el.querySelector('.code-lang');
    assert(lang ? label && label.textContent === lang : !label,
           `${info}: labelled ${label ? JSON.stringify(label.textContent) : 'nothing'}`);
    assert(!el.querySelector('em'), `${info}: the code was taken for markdown`);
    assert(el.textContent.trim().endsWith('More text') && !el.querySelector('.code-block')
             .textContent.includes('More'), `${info}: the text after it was swallowed`);
  }
});

test('a backtick in the info string means it is not a fence', () => {
  // CommonMark: ``` followed by text with a backtick is inline code.
  const el = rendered('```foo`bar```\nafter');
  assert(codeBlocks(el).length === 0, 'made a code block');
});

test('a closing fence may be longer than the opening one', () => {
  const el = rendered('```\ncode\n````\nafter');
  const blocks = codeBlocks(el);
  assert(blocks.length === 1 && blocks[0].code === 'code', JSON.stringify(blocks));
  assert(!el.querySelector('.code-block').textContent.includes('after'), 'after was swallowed');
});

test('but not shorter, nor of the other character', () => {
  const el = rendered('````\na\n```\nb\n~~~~\nc\n````\nafter');
  const blocks = codeBlocks(el);
  assert(blocks.length === 1 && blocks[0].code === 'a\n```\nb\n~~~~\nc', JSON.stringify(blocks));
});

test('a fence of tildes is a fence too', () => {
  const el = rendered('~~~js\nlet a = `b`;\n~~~\nafter');
  const blocks = codeBlocks(el);
  assert(blocks.length === 1 && blocks[0].lang === 'js' && blocks[0].code === 'let a = `b`;',
         JSON.stringify(blocks));
});

test('a fence never closed runs to the end, and loses nothing', () => {
  const el = rendered('text\n```py\nx = 1\ny = 2');
  const blocks = codeBlocks(el);
  assert(blocks.length === 1 && blocks[0].code === 'x = 1\ny = 2', JSON.stringify(blocks));
});

test('a tab a code line starts with is kept', () => {
  const el = rendered('```\n\tindented\n```');
  const blocks = codeBlocks(el);
  assert(blocks.length === 1 && blocks[0].code === '\tindented', JSON.stringify(blocks));
});

test('an indented code block is a code block too', () => {
  const el = rendered('Text\n\n    let x = 1;\n    x *= 2;\n\nMore');
  const blocks = codeBlocks(el);
  assert(blocks.length === 1 && blocks[0].code === 'let x = 1;\nx *= 2;' && blocks[0].lang === '',
         JSON.stringify(blocks));
  assert(!el.querySelector('.code-lang'), 'a language label with no language');
});

test('the language is text, not markup', () => {
  // No spaces: only the info string's first word is the language.
  const lang = '"><img/src=x/onerror=alert(1)>';
  const el = rendered('```' + lang + '\nx\n```');
  assert(!el.querySelector('img'), 'made an element: ' + el.innerHTML);
  assert(codeBlocks(el)[0].lang === lang, el.innerHTML);
});

done();
