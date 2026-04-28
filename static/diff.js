/* diff.js — Side-by-side diff viewer (UltraCompare-style)
 *
 * Computes line-level and character-level diffs, renders a
 * synchronized side-by-side view with inline highlights.
 */

const Diff = (() => {

  // ── LCS-based line diff ──────────────────────────────────────

  /**
   * Compute the longest common subsequence table for two arrays.
   * Returns a 2D array where lcs[i][j] = length of LCS of a[0..i-1], b[0..j-1].
   */
  function _lcsTable(a, b) {
    const m = a.length, n = b.length;
    const dp = Array.from({ length: m + 1 }, () => new Uint16Array(n + 1));
    for (let i = 1; i <= m; i++) {
      for (let j = 1; j <= n; j++) {
        if (a[i - 1] === b[j - 1]) {
          dp[i][j] = dp[i - 1][j - 1] + 1;
        } else {
          dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
        }
      }
    }
    return dp;
  }

  /**
   * Backtrack the LCS table to produce a list of diff ops.
   * Each op: { type: 'equal'|'delete'|'insert'|'change', left, right }
   * where left/right are line strings (or null for gaps).
   */
  function _diffLines(aLines, bLines) {
    const dp = _lcsTable(aLines, bLines);
    const ops = [];
    let i = aLines.length, j = bLines.length;

    // Backtrack to collect raw equal/delete/insert ops.
    const raw = [];
    while (i > 0 || j > 0) {
      if (i > 0 && j > 0 && aLines[i - 1] === bLines[j - 1]) {
        raw.push({ type: 'equal', left: aLines[i - 1], right: bLines[j - 1] });
        i--; j--;
      } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
        raw.push({ type: 'insert', left: null, right: bLines[j - 1] });
        j--;
      } else {
        raw.push({ type: 'delete', left: aLines[i - 1], right: null });
        i--;
      }
    }
    raw.reverse();

    // Merge adjacent delete+insert pairs into 'change' ops.
    let k = 0;
    while (k < raw.length) {
      if (raw[k].type === 'delete') {
        // Collect consecutive deletes.
        const dels = [];
        while (k < raw.length && raw[k].type === 'delete') {
          dels.push(raw[k].left);
          k++;
        }
        // Collect consecutive inserts that follow.
        const ins = [];
        while (k < raw.length && raw[k].type === 'insert') {
          ins.push(raw[k].right);
          k++;
        }
        // Pair them as changes; excess are pure delete/insert.
        const paired = Math.min(dels.length, ins.length);
        for (let p = 0; p < paired; p++) {
          ops.push({ type: 'change', left: dels[p], right: ins[p] });
        }
        for (let p = paired; p < dels.length; p++) {
          ops.push({ type: 'delete', left: dels[p], right: null });
        }
        for (let p = paired; p < ins.length; p++) {
          ops.push({ type: 'insert', left: null, right: ins[p] });
        }
      } else {
        ops.push(raw[k]);
        k++;
      }
    }

    return ops;
  }

  // ── Character-level inline diff ──────────────────────────────

  /**
   * Given two strings, return { left: html, right: html } with
   * <span class="diff-char"> wrapped around the differing segments.
   */
  function _inlineDiff(a, b) {
    // Tokenise into words + whitespace to get meaningful chunks.
    const tokA = _tokenize(a);
    const tokB = _tokenize(b);
    const dp = _lcsTable(tokA, tokB);

    // Backtrack.
    const opsRaw = [];
    let i = tokA.length, j = tokB.length;
    while (i > 0 || j > 0) {
      if (i > 0 && j > 0 && tokA[i - 1] === tokB[j - 1]) {
        opsRaw.push({ type: 'eq', a: tokA[i - 1], b: tokB[j - 1] });
        i--; j--;
      } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
        opsRaw.push({ type: 'ins', b: tokB[j - 1] });
        j--;
      } else {
        opsRaw.push({ type: 'del', a: tokA[i - 1] });
        i--;
      }
    }
    opsRaw.reverse();

    let leftHtml = '', rightHtml = '';
    for (const op of opsRaw) {
      if (op.type === 'eq') {
        leftHtml  += _esc(op.a);
        rightHtml += _esc(op.b);
      } else if (op.type === 'del') {
        leftHtml += `<span class="diff-char-changed">${_esc(op.a)}</span>`;
      } else if (op.type === 'ins') {
        rightHtml += `<span class="diff-char-changed">${_esc(op.b)}</span>`;
      }
    }

    return { left: leftHtml, right: rightHtml };
  }

  /** Split a string into tokens (words and whitespace runs). */
  function _tokenize(s) {
    const tokens = [];
    const re = /(\S+|\s+)/g;
    let m;
    while ((m = re.exec(s)) !== null) tokens.push(m[0]);
    return tokens;
  }

  function _esc(s) {
    if (!s) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  // ── Render ───────────────────────────────────────────────────

  /**
   * Render a side-by-side diff view.
   * @param {string} oldText - The original text.
   * @param {string} newText - The modified text.
   * @param {string} [fileName] - Optional file path for the header.
   * @returns {string} HTML string for the diff viewer.
   */
  function render(oldText, newText, fileName) {
    const aLines = (oldText || '').split('\n');
    const bLines = (newText || '').split('\n');
    const ops = _diffLines(aLines, bLines);

    let leftNum = 0, rightNum = 0;
    const leftRows = [];
    const rightRows = [];

    for (const op of ops) {
      if (op.type === 'equal') {
        leftNum++;
        rightNum++;
        const text = _esc(op.left);
        leftRows.push(_row(leftNum, text, ''));
        rightRows.push(_row(rightNum, text, ''));

      } else if (op.type === 'change') {
        leftNum++;
        rightNum++;
        const inline = _inlineDiff(op.left, op.right);
        leftRows.push(_row(leftNum, inline.left, 'diff-line-changed'));
        rightRows.push(_row(rightNum, inline.right, 'diff-line-changed'));

      } else if (op.type === 'delete') {
        leftNum++;
        const text = `<span class="diff-char-del">${_esc(op.left)}</span>`;
        leftRows.push(_row(leftNum, text, 'diff-line-del'));
        rightRows.push(_row('', '', 'diff-line-gap'));

      } else if (op.type === 'insert') {
        rightNum++;
        const text = `<span class="diff-char-ins">${_esc(op.right)}</span>`;
        leftRows.push(_row('', '', 'diff-line-gap'));
        rightRows.push(_row(rightNum, text, 'diff-line-ins'));
      }
    }

    const headerLeft = fileName ? `<div class="diff-file-header">${_esc(fileName)} (old)</div>` : '';
    const headerRight = fileName ? `<div class="diff-file-header">${_esc(fileName)} (new)</div>` : '';

    return `
      <div class="diff-viewer">
        <div class="diff-pane diff-pane-left">
          ${headerLeft}
          <div class="diff-scroll">${leftRows.join('')}</div>
        </div>
        <div class="diff-gutter"></div>
        <div class="diff-pane diff-pane-right">
          ${headerRight}
          <div class="diff-scroll">${rightRows.join('')}</div>
        </div>
      </div>`;
  }

  function _row(num, content, cls) {
    return `<div class="diff-row ${cls}"><span class="diff-num">${num}</span><span class="diff-text">${content || '&nbsp;'}</span></div>`;
  }

  // ── Synchronized scrolling (called after DOM insertion) ──────

  /**
   * Wire up synchronized scrolling on a diff viewer element.
   * Call this after inserting the HTML into the DOM.
   * @param {HTMLElement} container - The .diff-viewer element.
   */
  function syncScroll(container) {
    const panes = container.querySelectorAll('.diff-scroll');
    if (panes.length < 2) return;
    let syncing = false;

    function handler(source, target) {
      return () => {
        if (syncing) return;
        syncing = true;
        target.scrollTop = source.scrollTop;
        target.scrollLeft = source.scrollLeft;
        syncing = false;
      };
    }

    panes[0].addEventListener('scroll', handler(panes[0], panes[1]));
    panes[1].addEventListener('scroll', handler(panes[1], panes[0]));
  }

  return { render, syncScroll };
})();
