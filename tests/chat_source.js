/* chat.js as the page loads it: after the markdown library it renders with
 * (static/vendor/markdown-it.min.js, a <script> of its own in index.html).
 * For test harnesses that eval chat.js into a jsdom window. */

const fs = require('fs');
const path = require('path');

const STATIC = path.join(__dirname, '..', 'static');

module.exports = fs.readFileSync(path.join(STATIC, 'vendor', 'markdown-it.min.js'), 'utf8')
  + '\n;' + fs.readFileSync(path.join(STATIC, 'chat.js'), 'utf8');
