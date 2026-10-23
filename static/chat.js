/* chat.js — Chat message rendering
 *
 * Handles all message types from the WebSocket and renders them
 * into the #messages container.  Supports:
 * - User messages
 * - Assistant text (streaming deltas)
 * - Tool use / tool result (expandable blocks)
 * - Thinking blocks (expandable)
 * - System messages
 * - Turn boundaries
 * - Session history replay
 */

const Chat = (() => {
  let elMessages;
  // How many older history messages are still to be backfilled above the
  // first slice.  Non-zero means _replayHistory must not write the
  // "Session history" separator, because it is not the top yet.
  let _moreAbove = 0;
  // Messages rendered by the first history frame, so the separator the
  // backfill writes can report the true total.
  let _historyShown = 0;
  let _autoScroll = true;
  // Where following last put the view.  The scroll check in init() turns
  // following off only when the view has moved up from there.
  let _followedTop = Infinity;
  // A reconnecting tab's catch-up: what it missed while its socket was down,
  // held while _catchingUp and drawn in one go by endCatchUp(), during which
  // _drawingInBulk is true.
  let _catchingUp = false;
  let _drawingInBulk = false;
  // One-line thinking summaries waiting to be fitted together (_fitThinking).
  let _thinkingToFit = [];

  // --- Collapse gap (scroll-anchored collapse) ---
  // When a run of tools collapses into one line, the document suddenly
  // shrinks.  Instead of snapping to the new bottom (which lurches all the
  // on-screen text upward), we freeze the current scroll position and hold
  // the freed vertical space open as a temporary gap at the bottom of the
  // scroller (via padding-bottom).  Subsequent streamed lines fill that gap
  // from the top down; once real content would overflow the frozen viewport,
  // the gap is released and normal bottom-following resumes.
  let _gapActive = false;
  let _gapFrozenTop = 0;      // scrollTop to hold while the gap is open
  let _gapPad = 0;            // extra padding-bottom currently applied (px)
  const _GAP_BASE_PAD = 12;   // #messages base padding-bottom (styles.css)

  // --- Short-content collapse gap (no-scroll case) ---
  // #messages is bottom-justified (see styles.css: first child gets
  // margin-top:auto), so when the whole conversation fits on screen it sits at
  // the bottom.  Collapsing a run of tools then shrinks the content, and the
  // bottom-justify would pull the text above the collapse *downward* — the same
  // lurch the scroll-anchored gap avoids, but with no scrollbar to freeze.
  // Instead we hold the reclaimed space open as padding-bottom (which, under
  // bottom-justify, pushes the content back up so the top text stays put) and
  // shrink it as new lines stream in.
  let _shortGapActive = false;
  let _shortGapFreed = 0;        // px of bottom space currently held open
  let _shortGapBaseContentH = 0; // content height when the gap opened

  // Track the current streaming assistant text element.
  let _streamingEl = null;
  let _streamingText = '';
  let _streamRenderRaf = null;   // pending requestAnimationFrame for live md render

  // Map tool_use_id → DOM element for pairing results.
  const _toolBlocks = new Map();

  // --- Replay gating ---
  // While history replay is in progress (batched via setTimeout), queue
  // incoming real-time messages so they don't get interleaved mid-history.
  let _replayInProgress = false;
  let _pendingMessages = [];

  // --- Tool / thinking block collapsing ---
  // Tool-use and thinking blocks are shown individually as they stream in.
  // A run of them is wrapped into a single collapsed activity-group only
  // when the next boundary arrives — Claude text, a user message, or the
  // turn end (see _collapseActivity).  There is deliberately no eager
  // "collapse as they accumulate" path: that produced a second, competing
  // collapsed bar that grew while tools were still streaming.
  let _collapseTools = true;          // toggled via the "collapse activity" checkbox
  // --show-thinking / "/show-thinking on": render thinking blocks already
  // expanded.  The full text is always sent either way — this only picks the
  // starting state, so a block is still click-to-toggle afterwards.
  let _showThinking = false;

  function _trackToolBlock(el) {
    elMessages.appendChild(el);
  }

  function _trackThinkingBlock(el) {
    elMessages.appendChild(el);
  }

  // --- Activity-group collapsing ---
  // Wraps all non-text elements (tools, thinking, turn markers, system msgs)
  // between Claude's text messages into a single collapsible summary line.
  // Triggered when a new assistant text or user message arrives.

  function _collapseActivity() {
    if (!_collapseTools) return;

    const kids = elMessages.children;
    if (kids.length === 0) return;

    // Walk backwards from the end to find the last boundary — only the
    // tail matters, so we never touch the rest of the (potentially huge)
    // DOM list.
    //
    // Turn-end markers and harness-injected prompts are also boundaries
    // so they stay visible between groups (otherwise the activity group
    // spans across them and the summary counts a hidden "1 turn" that the
    // user can't see in the collapsed view).
    let boundaryNode = null;
    for (let i = kids.length - 1; i >= 0; i--) {
      const el = kids[i];
      if (el.classList.contains('msg-user') ||
          el.classList.contains('msg-peer') ||
          el.classList.contains('msg-assistant') ||
          el.classList.contains('msg-turn-end') ||
          el.classList.contains('msg-injected') ||
          el.classList.contains('activity-group') ||
          el.classList.contains('activity-boundary')) {
        boundaryNode = el;
        break;
      }
    }

    // Collect activity elements after the boundary using DOM siblings
    // (avoids copying the entire children list).
    const start = boundaryNode ? boundaryNode.nextElementSibling : kids[0];
    if (!start) return;
    const activityEls = [];
    for (let el = start; el; el = el.nextElementSibling) {
      activityEls.push(el);
    }
    if (activityEls.length === 0) return;

    // Threshold: usually we don't wrap a single element (a bare tool
    // between two real messages isn't noisy enough to need a collapse
    // toggle).  BUT when the prior boundary is itself an auto-generated
    // noise marker — an injected harness prompt or a turn-end divider
    // — even a single trailing element should be collapsible, otherwise
    // it sits stranded with no way to hide it.  This is the case the
    // user hits post-compaction: the injected-prompt boundary cuts the
    // wrap scope short and a single tool gets stranded without a
    // header.
    const noisyBoundary = boundaryNode && (
      boundaryNode.classList.contains('msg-injected') ||
      boundaryNode.classList.contains('msg-turn-end')
    );
    const minCount = noisyBoundary ? 1 : 2;
    if (activityEls.length < minCount) return;

    // Count items for summary.
    const counts = _countActivityItems(activityEls);
    const summary = _activitySummary(counts);
    if (!summary) return;

    // Snapshot scroll state *before* the DOM shrinks so we can anchor it.
    // Only engage the gap when the user is following at the bottom and we're
    // not bulk-replaying history (where a live gap would just churn).
    const _wasPinned = _autoScroll && !_replayInProgress && !_drawingInBulk;
    // Read only for that gap: each read forces a layout of the whole list, and
    // a catch-up or a history collapses hundreds of times in a row.
    const _beforeTop = _wasPinned ? elMessages.scrollTop : 0;
    const _beforeH = _wasPinned ? elMessages.scrollHeight : 0;
    const _beforeContentH = _wasPinned ? _contentHeight() : 0;

    // Create collapsible group.
    const group = document.createElement('div');
    group.className = 'activity-group collapsed';

    const toggle = document.createElement('div');
    toggle.className = 'activity-group-toggle';
    toggle.textContent = '\u25B6 ' + summary;

    const content = document.createElement('div');
    content.className = 'activity-group-content';

    // Insert before the first activity element, then move them all inside.
    activityEls[0].parentNode.insertBefore(group, activityEls[0]);
    for (const el of activityEls) {
      content.appendChild(el);
    }
    group.appendChild(toggle);
    group.appendChild(content);

    toggle.addEventListener('click', () => {
      group.classList.toggle('collapsed');
      if (group.classList.contains('collapsed')) {
        toggle.textContent = '\u25B6 ' + summary;
        _scrollToBottom();
      } else {
        toggle.textContent = '\u25BC ' + summary;
        fitThinking(content);
        toggle.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    });

    // If we were following at the bottom, anchor the collapse so the text
    // above the collapsed run stays put instead of lurching.
    if (_wasPinned) {
      const freedScroll = _beforeH - elMessages.scrollHeight;
      if (freedScroll > 2) {
        // Overflowing scroller: freeze scrollTop and hold the reclaimed space
        // open as a bottom gap (scroll-anchored).
        if (!_gapActive) {
          _gapActive = true;
          _gapFrozenTop = _beforeTop;
          _gapPad = 0;
        }
        _maintainGap();
      } else if (!_gapActive) {
        // Not overflowing (content fits on screen, bottom-justified).  The
        // scrollHeight didn't change, but the real content did — measure it
        // directly and hold a padding-bottom gap so the top text stays fixed.
        const freed = _beforeContentH - _contentHeight();
        if (freed > 2) {
          _shortGapActive = true;
          _shortGapFreed = freed;
          _shortGapBaseContentH = _contentHeight();
          _applyShortGap();
        }
      }
    }
  }

  // True rendered height of the message content (first child top → last child
  // bottom), independent of scroll clamping or the bottom-justify margin.
  function _contentHeight() {
    const first = elMessages.firstElementChild;
    const last = elMessages.lastElementChild;
    if (!first || !last) return 0;
    return last.getBoundingClientRect().bottom - first.getBoundingClientRect().top;
  }

  function _countActivityItems(elements) {
    // Note: msg-turn-end and msg-injected are activity boundaries (see
    // _collapseActivity) so they never appear inside a group.  Don't bother
    // counting them.
    let tools = 0, thinking = 0, system = 0;
    for (const el of elements) {
      if (el.classList.contains('msg-tool'))          tools++;
      else if (el.classList.contains('msg-thinking'))  thinking++;
      else if (el.classList.contains('msg-system') ||
               el.classList.contains('msg-command'))    system++;
    }
    return { tools, thinking, system };
  }

  function _activitySummary(counts) {
    const parts = [];
    if (counts.tools)    parts.push(`${counts.tools} tool${counts.tools !== 1 ? 's' : ''}`);
    if (counts.thinking) parts.push(`${counts.thinking} thinking`);
    if (counts.system)   parts.push(`${counts.system} system`);
    return parts.join(', ');
  }

  function init() {
    elMessages = document.getElementById('messages');

    _initAudioUnlock();

    // Frame-driven work stops dead while the window is hidden, so the
    // transition itself has to be an event we handle.  See _onVisibilityChange.
    document.addEventListener('visibilitychange', _onVisibilityChange);

    // Stop following the moment the user reaches for older text -- not when the
    // throttled check below gets round to it.  That check runs 60 ms after the
    // scroll, and a message landing inside those 60 ms scrolled back to the
    // bottom first, so the check then saw "at the bottom" and kept following:
    // while a backlog was arriving, every attempt to scroll up was undone.
    // Wheel up, a finger dragging the content down, the keys that page back, a
    // press on the scrollbar -- each is unambiguous intent, known at once.
    // (Coming back down re-arms following through the check below, as before.)
    const _stopFollowing = () => { _autoScroll = false; };
    elMessages.addEventListener('wheel', (e) => {
      if (e.deltaY < 0) _stopFollowing();
    }, { passive: true });
    let _touchY = null;
    elMessages.addEventListener('touchstart', (e) => {
      _touchY = e.touches && e.touches.length ? e.touches[0].clientY : null;
    }, { passive: true });
    elMessages.addEventListener('touchmove', (e) => {
      if (_touchY === null || !e.touches || !e.touches.length) return;
      if (e.touches[0].clientY > _touchY + 8) _stopFollowing();
    }, { passive: true });
    elMessages.addEventListener('keydown', (e) => {
      if (e.key === 'PageUp' || e.key === 'ArrowUp' || e.key === 'Home') {
        _stopFollowing();
      }
    });
    elMessages.addEventListener('mousedown', (e) => {
      // offsetX past the content box is the scrollbar.
      if (e.offsetX >= elMessages.clientWidth) _stopFollowing();
    });

    // Auto-scroll tracking: only scroll if user is near bottom.
    // Throttled to avoid layout thrashing on every pixel of scroll.
    let _scrollThrottle = null;
    elMessages.addEventListener('scroll', () => {
      if (_scrollThrottle) return;
      _scrollThrottle = setTimeout(() => {
        _scrollThrottle = null;
        // If the user scrolls up while a collapse gap is open, release the
        // gap so they can browse freely (our own scroll writes hold exactly
        // at _gapFrozenTop, so they don't trip this).
        if (_gapActive && elMessages.scrollTop < _gapFrozenTop - 4) {
          _cancelGap();
        }
        const threshold = 80;
        const atBottom = (elMessages.scrollHeight - elMessages.scrollTop - elMessages.clientHeight) < threshold;
        if (atBottom) {
          _autoScroll = true;
        } else if (elMessages.scrollTop < _followedTop - 2) {
          // The view moved up from where following put it: reading.
          _autoScroll = false;
        }
        // Otherwise the view is where following left it, and what lies below
        // arrived since: a burst of messages, not the user leaving.  Position
        // alone cannot tell the two apart: what a burst adds between a scroll
        // and this check looks like the user scrolling away, and a tab
        // following a catch-up would stop following partway through it.
      }, 60);
    });
  }

  // *keepHeld*: the next view is this session's own history (a /history), so
  // what is held back belongs after it, not with the view being wiped.
  function clear(keepHeld) {
    if (_streamRenderRaf != null) {
      cancelAnimationFrame(_streamRenderRaf);
      _streamRenderRaf = null;
    }
    elMessages.innerHTML = '';
    _streamingEl = null;
    _streamingText = '';
    _loadingEl = null;   // was a child of elMessages, just cleared
    // Held back for the view being wiped; drawn now, they would land in the
    // next one.
    if (!keepHeld) _pendingMessages = [];
    _catchingUp = false;
    _thinkingToFit = [];
    _toolBlocks.clear();
    _cancelGap();
    _cancelShortGap();
  }

  // Transient "loading session…" placeholder shown between attach and the
  // arrival of (potentially multi-second) history for a large session, so an
  // opened tab shows progress instead of a blank pane.  Driven by the server's
  // ``session_loading`` messages and cleared when history replay begins.
  let _loadingEl = null;
  function _setSessionLoading(on) {
    if (on) {
      if (_loadingEl) return;
      _loadingEl = document.createElement('div');
      _loadingEl.className = 'msg msg-system session-loading';
      _loadingEl.innerHTML =
        '<span class="session-loading-spin"></span> loading session\u2026';
      elMessages.appendChild(_loadingEl);
      _scrollToBottom();
    } else if (_loadingEl) {
      _loadingEl.remove();
      _loadingEl = null;
    }
  }

  // --- Scrolling ---

  function _scrollToBottom() {
    // Reading scrollHeight (and the gap maintenance below, which reads more)
    // forces a synchronous layout of the whole message list.  In a hidden
    // window nothing is painted, so that cost buys nothing — and it is paid
    // once per arriving message, over a list that is growing the entire time.
    // Defer to the moment the window is shown again, where one scroll write
    // settles the whole backlog.  Trimming still runs: keeping the list capped
    // is exactly what stops the catch-up being expensive.
    if (_hidden()) {
      _scrollPendingOnShow = true;
      _maybeTrimOldMessages();
      return;
    }
    // Visible: once per *frame*, not once per message.  Only the last scroll
    // before a paint is ever seen, and each one is that same forced layout of
    // the whole list.  Per message, it is what made a backlog visibly drip in:
    // a background tab the browser has frozen is handed everything that
    // happened meanwhile the moment it is shown, and it processed that one
    // layout at a time -- the transcript scrolling line by line, the status bar
    // counting through old values, and the view pinned to the bottom the whole
    // way ("i can't read the backscroll because it keeps stealing the scroll").
    // A scroll, unlike the trim, may wait for a frame: it matters only when
    // something is painted, which is precisely when frames exist.
    if (!_scrollFrame) {
      _scrollFrame = true;
      // Somewhere with no frames at all, scroll now rather than never.
      if (typeof requestAnimationFrame === 'function') requestAnimationFrame(_scrollNow);
      else _scrollNow();
    }
    _maybeTrimOldMessages();
  }

  // True while a _scrollNow frame is requested and has not run.
  let _scrollFrame = false;

  function _scrollNow() {
    _scrollFrame = false;
    if (_gapActive) {
      _maintainGap();
      return;
    }
    if (_shortGapActive) {
      _maintainShortGap();
      return;
    }
    if (_autoScroll) _followToBottom();
  }

  // Scroll to the bottom for following, and remember where that put the view.
  function _followToBottom() {
    elMessages.scrollTop = elMessages.scrollHeight;
    _followedTop = elMessages.scrollTop;
  }

  // Re-size the bottom gap so the frozen viewport top stays put while new
  // content streams in.  When real content grows enough to fill (or exceed)
  // the frozen viewport, the gap is released and normal following resumes.
  function _maintainGap() {
    // Height of real content, excluding the padding we added.
    const baseH = elMessages.scrollHeight - _gapPad;
    const needed = _gapFrozenTop + elMessages.clientHeight - baseH;
    if (needed <= 0) {
      // Content now fills past the frozen viewport — hand back to normal
      // bottom-following.
      _cancelGap();
      if (_autoScroll) _followToBottom();
      return;
    }
    _gapPad = needed;
    elMessages.style.paddingBottom = (_GAP_BASE_PAD + needed) + 'px';
    elMessages.scrollTop = _gapFrozenTop;
  }

  function _cancelGap() {
    if (!_gapActive) return;
    _gapActive = false;
    _gapPad = 0;
    elMessages.style.paddingBottom = '';
  }

  // Push the bottom-justified content up by the held-open gap so the text above
  // the collapse doesn't drop.  (Content height + gap == pre-collapse height,
  // so this never introduces a scrollbar.)
  function _applyShortGap() {
    elMessages.style.paddingBottom = (_GAP_BASE_PAD + _shortGapFreed) + 'px';
  }

  // As new lines stream in, shrink the gap 1:1 with content growth.  When the
  // gap closes (content refilled the reclaimed space), release it and let the
  // normal bottom-justify take over again.
  function _maintainShortGap() {
    const growth = _contentHeight() - _shortGapBaseContentH;
    const remaining = _shortGapFreed - growth;
    if (remaining <= 0) {
      _cancelShortGap();
      if (_autoScroll) _followToBottom();
      return;
    }
    _shortGapFreed = remaining;
    _shortGapBaseContentH = _contentHeight();
    _applyShortGap();
  }

  function _cancelShortGap() {
    if (!_shortGapActive) return;
    _shortGapActive = false;
    _shortGapFreed = 0;
    elMessages.style.paddingBottom = '';
  }

  // --- Hidden windows ---
  //
  // From a report: *"sometimes the window isn't updated while it's not in
  // focus, and once it's put into focus, it takes a long time to get up to
  // speed because it only adds a few lines per second."*
  //
  // A hidden window — another window covering this one, minimised, or a
  // background tab — gets **no animation frames at all**, and its timers are
  // clamped to one second (then to one *minute* once Chrome's "intensive
  // throttling" starts, five minutes in). WebSocket delivery is not throttled,
  // so messages keep arriving and keep being appended while everything
  // scheduled on a frame is frozen. Any "already scheduled, skip" latch guarding
  // an rAF therefore stays latched for as long as the window stays hidden.
  //
  // That is the whole bug. `_trimPending` latched true, its callback never ran,
  // and the transcript stopped being capped — so the message list grew without
  // bound while hidden, and every append kept doing `scrollTop = scrollHeight`,
  // which forces a synchronous layout of the entire list. Layout is O(nodes),
  // so the append rate decayed as the list grew and *stayed* decayed after
  // focus, because the DOM was still enormous. Hence "a few lines per second".
  //
  // It also explains the "sometimes": a window that is merely unfocused but
  // still visible keeps getting frames, so none of this happens — which is the
  // case where the transcript is already up to date on focus.
  //
  // The rule this establishes: **nothing that must keep working may be gated on
  // a frame.** None of the callbacks below are animations; they are bookkeeping.
  function _hidden() {
    return typeof document !== 'undefined' && document.hidden === true;
  }

  // Run *fn* on the next frame when there is one, and immediately when there
  // is not.  A hidden window has no next frame, so scheduling onto one is
  // indistinguishable from dropping the work.
  function _onNextFrame(fn) {
    if (_hidden()) { fn(); return null; }
    return requestAnimationFrame(fn);
  }

  // Scroll writes deferred because the window was hidden when they were asked
  // for.  Collapsed into a single catch-up when it is shown again.
  let _scrollPendingOnShow = false;

  function _onVisibilityChange() {
    if (_hidden()) {
      // The collapse gaps exist to hold a *visible* reading position steady;
      // with nothing painted they hold nothing.  They must not be left open,
      // because an open gap disables the trimmer — which is precisely the
      // unbounded-growth case this whole section is about.
      _cancelGap();
      _cancelShortGap();
      // Likewise a trim that was already waiting on a frame: that frame is now
      // never coming.  See _reclaimStrandedTrim.
      _reclaimStrandedTrim();
      // And a scroll waiting on one.  Released rather than left latched --
      // while _scrollFrame stays true no later scroll is ever requested -- and
      // carried over to the catch-up scroll on show.  Should the orphaned frame
      // still fire, _scrollNow is harmless to run twice.
      if (_scrollFrame) {
        _scrollFrame = false;
        _scrollPendingOnShow = true;
      }
      return;
    }
    // One catch-up scroll for the whole backlog, instead of the one-per-message
    // the hidden path skipped.
    if (_scrollPendingOnShow) {
      _scrollPendingOnShow = false;
      if (_autoScroll) _followToBottom();
    }
  }

  // --- DOM trimming ---
  // Remove oldest messages when the container exceeds a threshold to
  // prevent unbounded DOM growth from degrading performance.
  // 0 = never trim.  Configurable via --max-dom-messages.
  let _maxDomChildren = 2000;
  const _TRIM_BATCH = 400;    // remove this many at a time
  let _trimPending = false;

  function setMaxDomMessages(n) { _maxDomChildren = n; }

  // Idempotent on purpose: it re-reads the list and re-checks the cap, so it is
  // safe to run twice, and safe to run from a frame that was scheduled long
  // ago.  That is what lets _reclaimStrandedTrim below simply run it early and
  // leave the orphaned frame to fire harmlessly, instead of tracking rAF ids.
  // Without the re-check a late frame would compute a positive removal count
  // against a list that is already under the cap and delete live messages.
  function _trimNow() {
    _trimPending = false;
    const n = elMessages.children.length;
    if (n <= _maxDomChildren) return;
    const remove = Math.min(n - _maxDomChildren + _TRIM_BATCH, n - 1);
    // One range deletion, not `remove` separate removeChild calls.  Under the
    // old bug this list could reach tens of thousands of nodes, and removing
    // them one at a time is what turns the catch-up into a visible freeze.
    const range = document.createRange();
    range.setStartBefore(elMessages.firstChild);
    range.setEndBefore(elMessages.children[remove]);
    range.deleteContents();
    // Removing what is above the view lowers scrollTop by as much: the browser
    // keeps what is on screen in place.  That is not the user scrolling away.
    if (_autoScroll) _followedTop = elMessages.scrollTop;
  }

  function _maybeTrimOldMessages() {
    if (_trimPending) return;
    if (_gapActive) return;             // don't move the frozen scroll anchor
    if (_shortGapActive) return;        // gap math depends on stable children
    if (_maxDomChildren === 0) return;  // trimming disabled
    if (elMessages.children.length <= _maxDomChildren) return;
    _trimPending = true;
    // Deliberately not a bare requestAnimationFrame: see _onNextFrame.  The cap
    // has to hold while the window is hidden, because that is when the DOM
    // grows fastest and with nobody watching it.
    _onNextFrame(_trimNow);
  }

  // Being hidden is not the only way to strand the trimmer.  A trim scheduled
  // while the window was *visible* is waiting on a frame; hide the window
  // before that frame is delivered and it never arrives, so `_trimPending`
  // stays true and `_maybeTrimOldMessages` returns early for the whole hidden
  // period — the original unbounded-growth bug, entered through the door of
  // having been visible at the moment the cap was crossed.  Run the trim now
  // instead; the orphaned frame, if it is ever delivered, is a no-op (see
  // _trimNow).
  function _reclaimStrandedTrim() {
    if (_trimPending) _trimNow();
  }

  // --- Message dispatch ---

  // What builds the view itself, and so is never held back behind it.
  const _VIEW_MESSAGES = new Set(
    ['history', 'history_prepend', 'session_loading', 'clear_screen']);

  function handleMessage(msg) {
    // Queue real-time messages while history replay is still rendering
    // (batched via setTimeout) so they don't get interleaved mid-history --
    // and while the history has yet to arrive at all ("loading session..."),
    // so they don't land above it.  A large session's history waits behind
    // its CLI's own read of the same file, so what the connect says arrives
    // first; drawn then, it ended up above the history, scrolled out of sight.
    // Found 2026-09-29, with the notice that a session's last turn was cut
    // off and is waiting -- the one thing the user must see on opening it.
    if ((_replayInProgress || _loadingEl || _catchingUp)
        && !_VIEW_MESSAGES.has(msg.type)) {
      _pendingMessages.push(msg);
      return;
    }

    _dispatchMessage(msg);
  }

  // Draw what was held back, once nothing is being drawn in front of it.
  function _flushPending() {
    if (_replayInProgress || _loadingEl || _catchingUp) return;
    const pending = _pendingMessages;
    _pendingMessages = [];
    // Each fails alone, as it did when it was drawn in a task of its own: one
    // that throws must not lose the rest of a catch-up, which the resume
    // position has already moved past.
    for (const m of pending) {
      try {
        _dispatchMessage(m);
      } catch (e) {
        console.error('[chat] a held', m && m.type, 'message failed to draw:', e);
      }
    }
  }

  // A tab that reconnects is sent what it missed back to back, and app.js
  // knows where that ends.  Drawn as it came, a message per task, the browser
  // would repaint between messages and each message would force a layout or
  // two, so a backlog of a thousand-odd messages takes seconds.  So it is
  // held, and drawn in one go without the layout reads (_drawingInBulk).
  function beginCatchUp() { _catchingUp = true; }

  function endCatchUp() {
    if (!_catchingUp) return;
    _catchingUp = false;
    _drawingInBulk = true;
    try {
      _flushPending();
    } finally {
      _drawingInBulk = false;
    }
    _fitThinking(_takeThinkingToFit());
    // Followed now, not at the next frame.  Collapsing the runs it ended can
    // move the view up, and a scroll check that came due before that frame
    // would read the move as the user scrolling away.
    if (_hidden()) _scrollToBottom(); else _scrollNow();
  }

  function _takeThinkingToFit() {
    const waiting = _thinkingToFit;
    _thinkingToFit = [];
    return waiting;
  }

  // Show each waiting summary's text where it fits, else its size hint.
  // Measured all together rather than one at a time, so it forces a couple of
  // layouts however many there are.  One hidden in a collapsed group keeps its
  // hint and its data-fit, and is fitted when the group is opened.  One
  // without data-fit was fitted already, by a group opened before the rest
  // were drawn, and has no text left to try.
  function _fitThinking(summaries) {
    const visible = summaries.filter(s => 'fit' in s.dataset && s.offsetParent !== null);
    if (!visible.length) return;
    const hints = visible.map(s => s.textContent);
    visible.forEach((s) => { s.textContent = s.dataset.fit; });
    const tooWide = visible.map(s => s.scrollWidth > s.clientWidth);
    visible.forEach((s, i) => {
      if (tooWide[i]) s.textContent = hints[i];
      delete s.dataset.fit;
    });
  }

  // Fit the summaries inside *root* that were hidden when the rest were
  // fitted: a collapsed group, on being opened (app.js opens one to show a
  // tool the panel was asked for).
  function fitThinking(root) {
    _fitThinking(Array.from(root.querySelectorAll('.thinking-summary[data-fit]')));
  }

  function _dispatchMessage(msg) {
    switch (msg.type) {
      case 'user_message':    _addUserMessage(msg.content, msg.local === true); break;
      case 'peer_message':    _addPeerMessage(msg); break;
      case 'injected_prompt': _addInjectedPrompt(msg); break;
      case 'assistant_text':  _addAssistantText(msg); break;
      case 'tool_use':        _addToolUse(msg); break;
      case 'tool_result':     _addToolResult(msg); break;
      case 'thinking':        _addThinking(msg); break;
      case 'turn_start':      _addTurnStart(msg); break;
      case 'turn_end':        _addTurnEnd(msg); break;
      case 'system_msg':      _addSystemMsg(msg); break;
      // A /btw exchange is live-only: the fork writes to its own throwaway
      // session, so nothing of it is ever in this session's history to replay.
      case 'btw_start':       _addBtwStart(msg); break;
      case 'btw_delta':       _addBtwDelta(msg); break;
      case 'btw_end':         _addBtwEnd(msg); break;
      case 'bg_started':      _addBgStarted(msg); break;
      case 'bg_complete':     _addBgComplete(msg); break;
      case 'clear_screen':    clear(); break;
      case 'session_loading':
        _setSessionLoading(msg.on);
        if (!msg.on) _flushPending();   // no history is coming after all
        break;
      case 'command_data':    _addCommandData(msg); break;
      case 'modal':           _openModal(msg); break;
      case 'bell':            _playBell(); break;
      case 'history':
        // A /history takes the place of what is shown, keeping what is held.
        if (msg.replace) clear(true);
        _replayHistory(msg.messages, msg.more_above);
        break;
      case 'history_prepend': _prependHistory(msg.messages); break;
      default:
        console.warn('[chat] unhandled message type:', msg.type, msg);
        break;
    }
  }

  // --- User messages ---

  function _addUserMessage(content, local, midTurn) {
    _flushStreaming();
    _collapseActivity();
    const el = document.createElement('div');
    el.className = 'msg msg-user';
    // *midTurn*: history of a prompt the session was handed during a turn
    // that was already running, rather than given a turn of its own.  Placed
    // where the model saw it, so the reply around it reads in order.
    const label = midTurn
      ? 'You <span class="msg-midturn" title="Sent while the session was '
        + 'working: it was given to the turn already running, not a turn of '
        + 'its own.">\u00b7 sent while it was working</span>'
      : 'You';
    el.innerHTML = `<div class="msg-label">${label}</div>
                    <div class="msg-content">${_esc(content)}</div>`;
    elMessages.appendChild(el);
    // Force scroll for a prompt the user just sent from *this* tab -- their own
    // message should always be visible.  (The normal _autoScroll flag may be
    // false due to the input textarea expanding on paste, which shifts the
    // layout and trips the scroll listener before the message is appended.)
    //
    // Only that one: app.js marks its echo `local`.  Every other user_message
    // -- a queued prompt starting, a loop wakeup, a peer's message, a prompt
    // typed in another tab, and all of those replayed from a backlog -- used to
    // force the scroll too, dragging back down a user who had scrolled up to
    // read.  Those follow the bottom only if the user already was.
    if (local || _autoScroll) {
      // Release any open collapse gap so the new turn settles at the true
      // bottom.
      _cancelGap();
      _cancelShortGap();
    }
    if (local) _autoScroll = true;
    _scrollToBottom();
  }

  // --- A message relayed from another session ---
  //
  // Shown as the sender's, by name.  It starts a turn of its own, and the reply
  // that follows ("Reply sent.") means nothing without it.  It used to be shown
  // live not at all -- its text has no harness prefix, so it was taken for the
  // user's own and skipped as an echo -- and in history as the user's own.

  function _addPeerMessage(msg) {
    const body = (msg && msg.body) || '';
    if (!body) return;
    _flushStreaming();
    _collapseActivity();
    const el = document.createElement('div');
    el.className = 'msg msg-peer';
    el.innerHTML = '<div class="msg-label"></div><div class="msg-content"></div>';
    el.querySelector('.msg-label').textContent =
      '\u2709 From ' + ((msg && msg.name) || 'another session');
    el.querySelector('.msg-content').textContent = body;
    elMessages.appendChild(el);
    _scrollToBottom();
  }

  // --- Injected prompts (synthetic prompts from the CLI harness) ---
  //
  // Collapsed by default — these are typically long, frequent and
  // repetitive (autonomous-loop ticks, /loop reschedules, post-compact
  // nudges, system reminders).  Header shows a one-line summary; click
  // to expand the full body.

  function _addInjectedPrompt(msg) {
    const text = (msg && msg.content) || '';
    if (!text) return;
    _flushStreaming();
    // Collapse any activity that ran before this injected prompt.  The
    // injected marker is a boundary in _collapseActivity, so a later
    // message won't reach back past it — without collapsing here, tools
    // that preceded the injection would be stranded uncollapsed.
    _collapseActivity();

    const lines = text.split('\n');
    const nLines = lines.length;
    const nChars = text.length;

    // One-line summary: first non-empty line, truncated.
    let firstLine = '';
    for (const l of lines) {
      const t = l.trim();
      if (t) { firstLine = t; break; }
    }
    if (firstLine.length > 120) firstLine = firstLine.slice(0, 117) + '...';
    const sizeHint = nLines > 1
      ? `(${nChars} chars, ${nLines} lines)`
      : `(${nChars} chars)`;

    const el = document.createElement('div');
    el.className = 'msg msg-injected';
    el.innerHTML = `
      <div class="injected-header">
        <span class="injected-label">Injected (harness)</span>
        <span class="injected-summary"></span>
        <span class="injected-size">${sizeHint}</span>
        <span class="tool-expand-icon">\u25B8</span>
      </div>
      <div class="injected-body">${_esc(text)}</div>`;

    el.querySelector('.injected-summary').textContent = firstLine;

    const header = el.querySelector('.injected-header');
    const body = el.querySelector('.injected-body');
    const icon = el.querySelector('.tool-expand-icon');
    header.addEventListener('click', () => {
      body.classList.toggle('open');
      icon.classList.toggle('open');
      _scrollToBottom();
    });

    elMessages.appendChild(el);
    _scrollToBottom();
  }

  // --- Assistant text (streaming) ---

  // Signature of a tool call that Claude emitted as literal text instead of
  // actually invoking the tool (an intermittent Anthropic-side error).  When
  // this happens the tool never ran, so we surface a warning prompting the
  // user to nudge Claude to retry.
  const _TOOL_CALL_TEXT_RE = /<(?:antml:)?invoke\s+name=|<(?:antml:)?parameter\s+name=|<function_calls>/i;

  function _maybeWarnToolCallText(text) {
    if (!text || !_TOOL_CALL_TEXT_RE.test(text)) return;
    const warn = document.createElement('div');
    warn.className = 'msg msg-system warning';
    warn.textContent = '\u26A0 Claude emitted a tool call as text \u2014 it didn\u2019t actually run. Nudge it to retry.';
    elMessages.appendChild(warn);
  }

  function _addAssistantText(msg) {
    if (msg.delta) {
      // Streaming delta — append to current element and render markdown
      // live (debounced via rAF) so bold/italic/etc. show formatted as the
      // text arrives instead of appearing as raw "**text**" until the next
      // scroll event flushes it.  _md() leaves any unclosed marker raw until
      // its closer arrives, so partial markdown renders gracefully.
      if (!_streamingEl) {
        _collapseActivity();
        _streamingEl = document.createElement('div');
        _streamingEl.className = 'msg msg-assistant';
        _streamingEl.innerHTML = '<div class="msg-content md-rendered"></div>';
        elMessages.appendChild(_streamingEl);
        _streamingText = '';
      }
      _streamingText += msg.content;
      _scheduleStreamRender();
    } else {
      // Non-delta: full text block (history or async).
      _flushStreaming();
      _collapseActivity();
      if (!msg.content) return;  // skip empty blocks
      const el = document.createElement('div');
      el.className = 'msg msg-assistant';
      el.innerHTML = `<div class="msg-content md-rendered">${_md(msg.content)}</div>`;
      elMessages.appendChild(el);
      _maybeWarnToolCallText(msg.content);
    }
    _scrollToBottom();
  }

  // Debounced live markdown render for the in-flight streaming element.
  // Coalesces bursts of deltas arriving in the same frame into one render.
  //
  // This one stays a bare requestAnimationFrame — do not "fix" it to match the
  // trimmer.  It latches while hidden exactly like the trimmer did, but it is
  // harmless there and _onNextFrame would make it worse: it re-renders the
  // *entire* accumulated `_streamingText`, so the single frame that runs when
  // the window is shown again produces the fully caught-up text, whereas
  // running it eagerly would re-render the whole markdown body once per delta
  // for nobody.  (`_flushStreaming` cancels it and renders synchronously, so a
  // turn that ends while hidden still lands its text.)
  function _scheduleStreamRender() {
    if (_streamRenderRaf != null) return;
    _streamRenderRaf = requestAnimationFrame(() => {
      _streamRenderRaf = null;
      if (!_streamingEl) return;
      const content = _streamingEl.querySelector('.msg-content');
      if (content) content.innerHTML = _md(_streamingText);
      _scrollToBottom();
    });
  }

  function _flushStreaming() {
    if (_streamRenderRaf != null) {
      cancelAnimationFrame(_streamRenderRaf);
      _streamRenderRaf = null;
    }
    if (_streamingEl) {
      if (!_streamingText.trim()) {
        // Remove empty streaming element.
        _streamingEl.remove();
      } else {
        // Final markdown render now that the full text is available.
        const content = _streamingEl.querySelector('.msg-content');
        if (content) content.innerHTML = _md(_streamingText);
        _maybeWarnToolCallText(_streamingText);
      }
    }
    _streamingEl = null;
    _streamingText = '';
  }

  // --- Tool use blocks ---

  function _addToolUse(msg) {
    _flushStreaming();
    const h = msg.header || {};
    const name = msg.name || h.name || '?';
    // AskUserQuestion needs the user's attention: render it expanded and keep
    // it out of the collapsed activity group (mark it an activity boundary so
    // _collapseActivity never sweeps it in with the other tools).
    const isAsk = name === 'AskUserQuestion';

    const el = document.createElement('div');
    el.className = 'msg msg-tool' + (isAsk ? ' msg-ask activity-boundary' : '');
    el.dataset.toolUseId = msg.tool_use_id;

    const summary = _toolHeaderSummary(h, msg.input);
    const statusIcon = msg.status === 'running' ? '\u25B6' :
                       msg.status === 'error' ? '\u2717' : '\u2713';
    const statusCls = msg.status || 'running';
    const openCls = isAsk ? ' open' : '';

    el.innerHTML = `
      <div class="tool-header">
        <span class="tool-status-icon ${statusCls}">${statusIcon}</span>
        <span class="tool-name">${_esc(name)}</span>
        <span class="tool-summary">${summary}</span>
        <span class="tool-size-hint" data-tool-use-id="${msg.tool_use_id || ''}"></span>
        <span class="tool-expand-icon${openCls}">\u25B8</span>
      </div>
      <div class="tool-detail${openCls}">
        ${_toolDetailHtml(name, msg.input || {}, msg)}
      </div>`;

    // Click to toggle expand.
    const header = el.querySelector('.tool-header');
    const detail = el.querySelector('.tool-detail');
    const expandIcon = el.querySelector('.tool-expand-icon');
    header.addEventListener('click', () => {
      detail.classList.toggle('open');
      expandIcon.classList.toggle('open');
      // Wire up synchronized scrolling for diff viewers on first open.
      if (detail.classList.contains('open') && !detail._diffWired) {
        detail._diffWired = true;
        detail.querySelectorAll('.diff-viewer').forEach(dv => Diff.syncScroll(dv));
      }
      _scrollToBottom();
    });

    _trackToolBlock(el);
    _toolBlocks.set(msg.tool_use_id, el);
    _scrollToBottom();
  }

  function _addToolResult(msg) {
    const isErr = msg.is_error || msg.status === 'error';
    const el = _toolBlocks.get(msg.tool_use_id);
    if (el) {
      // Update status icon (▶ → ✓ or ✗).
      const iconEl = el.querySelector('.tool-status-icon');
      if (iconEl) {
        iconEl.textContent = isErr ? '\u2717' : '\u2713';
        iconEl.className = 'tool-status-icon ' + (isErr ? 'error' : 'complete');
      }

      // On error, tint the tool name + summary red.
      if (isErr) {
        const nameEl = el.querySelector('.tool-name');
        if (nameEl) nameEl.style.color = 'var(--red)';
        const sumEl = el.querySelector('.tool-summary');
        if (sumEl) sumEl.style.color = 'var(--red)';
      }

      // Show size hint in the header.
      if (msg.size_hint) {
        const hintEl = el.querySelector('.tool-size-hint');
        if (hintEl) hintEl.textContent = msg.size_hint;
      }

      // Add result content to the expandable detail area.
      const detail = el.querySelector('.tool-detail');
      if (detail && msg.content) {
        const resultDiv = document.createElement('div');
        resultDiv.innerHTML = `
          <div class="detail-label">Result${isErr ? ' (error)' : ''}</div>
          <pre>${_esc(msg.content)}</pre>`;
        detail.appendChild(resultDiv);
      }
    } else {
      // Orphaned result — render minimal inline.
      const standalone = document.createElement('div');
      standalone.className = 'msg msg-tool';
      standalone.innerHTML = `
        <div class="tool-header">
          <span class="tool-status-icon ${isErr ? 'error' : 'complete'}">${isErr ? '\u2717' : '\u2713'}</span>
          <span class="tool-name" ${isErr ? 'style="color:var(--red)"' : ''}>${_esc(msg.name || '?')}</span>
          <span class="tool-size-hint">${_esc(msg.size_hint || '')}</span>
        </div>`;
      elMessages.appendChild(standalone);
    }
    _scrollToBottom();
  }

  // --- Thinking blocks ---

  function _addThinking(msg) {
    _flushStreaming();
    const text = msg.content || '';
    const lines = text.split('\n');
    const nLines = lines.length;
    const nChars = text.length;
    // If single-line and short enough, show the full text inline;
    // otherwise show a size hint like tool headers do.
    const isSingleLine = nLines === 1;

    // Models newer than opus-4-6 return *encrypted* reasoning: the block has
    // a signature but no text, and no thinking_delta ever streams — so no
    // client can show the content.  Render a marker instead of nothing, so
    // it's clear the model reasoned rather than looking like it never did.
    const encrypted = !!msg.encrypted && !text;

    const el = document.createElement('div');
    el.className = 'msg msg-thinking';
    if (encrypted) {
      el.classList.add('msg-thinking-encrypted');
      el.innerHTML = `
        <div class="thinking-header">
          <span>\u{1F4AD}</span>
          <span>Thinking</span>
          <span class="thinking-summary" style="color:var(--text-muted); font-size:11px;"
                title="This model returns encrypted reasoning \u2014 the text is not sent to any client.">(hidden by model)</span>
        </div>`;
      _trackThinkingBlock(el);
      _scrollToBottom();
      return;
    }
    // Build the summary span — actual content chosen after DOM insertion
    // so we can measure available width.
    el.innerHTML = `
      <div class="thinking-header">
        <span>\u{1F4AD}</span>
        <span>Thinking</span>
        <span class="thinking-summary" style="color:var(--text-muted); font-size:11px;"></span>
        <span class="tool-expand-icon">\u25B8</span>
      </div>
      <div class="thinking-body">${_esc(text)}</div>`;

    const header = el.querySelector('.thinking-header');
    const body = el.querySelector('.thinking-body');
    const icon = el.querySelector('.tool-expand-icon');
    const summaryEl = el.querySelector('.thinking-summary');
    if (_showThinking) {
      body.classList.add('open');
      icon.classList.add('open');
    }
    header.addEventListener('click', () => {
      body.classList.toggle('open');
      icon.classList.toggle('open');
      _scrollToBottom();
    });

    _trackThinkingBlock(el);

    // Decide summary text after insertion so we can measure width.
    // If the element is hidden (inside a collapsed group), skip
    // measurement and use the size-hint fallback directly.
    // Asking whether it is visible is itself a layout, so only a summary that
    // might show its text asks.
    if (isSingleLine && nChars > 0 && (_drawingInBulk || _replayInProgress)) {
      // Many arriving at once (a catch-up, a history): measured together once
      // they are all in, in one layout rather than one each (_fitThinking).
      summaryEl.textContent = `(${nChars} chars)`;
      summaryEl.dataset.fit = text;
      _thinkingToFit.push(summaryEl);
    } else if (isSingleLine && nChars > 0 && el.offsetParent !== null) {
      // Try fitting the full text; measure against the header's available space.
      summaryEl.textContent = text;
      if (summaryEl.scrollWidth > summaryEl.clientWidth) {
        // Doesn't fit — fall back to size hint.
        summaryEl.textContent = `(${nChars} chars)`;
      }
    } else if (nChars > 0) {
      summaryEl.textContent = `(${nChars} chars${nLines > 1 ? `, ${nLines} lines` : ''})`;
    }

    _scrollToBottom();
  }

  // --- Background task events ---

  function _addBgStarted(msg) {
    const el = document.createElement('div');
    const name = _esc(msg.name || msg.task_type || '');
    const cmd = msg.command;

    el.className = 'msg msg-tool';
    let detailHtml = '';
    if (cmd) {
      detailHtml = `<div class="detail-label">Command</div>
                    <pre>${_esc(cmd)}</pre>`;
    }
    el.innerHTML = `
      <div class="tool-header">
        <span class="tool-status-icon running">\u25B6</span>
        <span class="tool-name">Background task started</span>
        <span class="tool-summary">${name}</span>
        ${detailHtml ? '<span class="tool-expand-icon">\u25B8</span>' : ''}
      </div>
      ${detailHtml ? `<div class="tool-detail">${detailHtml}</div>` : ''}`;
    if (detailHtml) {
      const header = el.querySelector('.tool-header');
      const detail = el.querySelector('.tool-detail');
      const expandIcon = el.querySelector('.tool-expand-icon');
      header.addEventListener('click', () => {
        detail.classList.toggle('open');
        expandIcon.classList.toggle('open');
        _scrollToBottom();
      });
    }

    if (msg.task_id) el.dataset.bgTaskId = msg.task_id;
    elMessages.appendChild(el);
    _scrollToBottom();
  }

  function _addBgComplete(msg) {
    const el = document.createElement('div');
    const isErr = msg.status === 'error' || msg.status === 'failed';
    const name = msg.name ? _esc(msg.name) : '';
    const cmd = msg.command;
    const summary = msg.summary || '';       // model's task summary
    const output = msg.output || '';         // actual captured stdout
    const icon = isErr ? '\u2717' : '\u2713';
    const statusCls = isErr ? 'error' : 'complete';
    const label = isErr ? 'Background task failed' : 'Background task completed';

    el.className = 'msg msg-tool';
    // Show command, model summary, and actual output as three distinct
    // sections (skip a summary/output that's just the command repeated).
    const hasSummary = summary && summary !== cmd;
    const hasOutput = output && output !== cmd;
    let detailHtml = '';
    if (cmd) {
      detailHtml += `<div class="detail-label">Command</div>
                     <pre>${_esc(cmd)}</pre>`;
    }
    if (hasSummary) {
      detailHtml += `<div class="detail-label">Summary</div>
                     <div class="detail-field">${_esc(summary)}</div>`;
    }
    if (hasOutput) {
      detailHtml += `<div class="detail-label">Output</div>
                     <pre>${_esc(output)}</pre>`;
    }
    // Header one-liner: prefer the summary, else a snippet of the output.
    const rawHeader = hasSummary ? summary : (hasOutput ? output : '');
    const headerOneLine = rawHeader.replace(/\s+/g, ' ').trim();
    const summaryClean = headerOneLine
      ? _esc(headerOneLine.length > 120 ? headerOneLine.slice(0, 120) + '\u2026' : headerOneLine)
      : '';
    const displayText = name && summaryClean
      ? `${name} \u2014 ${summaryClean}`
      : name || summaryClean || '';
    el.innerHTML = `
      <div class="tool-header">
        <span class="tool-status-icon ${statusCls}">${icon}</span>
        <span class="tool-name">${label}</span>
        <span class="tool-summary">${displayText}</span>
        ${detailHtml ? '<span class="tool-expand-icon">\u25B8</span>' : ''}
      </div>
      ${detailHtml ? `<div class="tool-detail">${detailHtml}</div>` : ''}`;
    if (detailHtml) {
      const header = el.querySelector('.tool-header');
      const detail = el.querySelector('.tool-detail');
      const expandIcon = el.querySelector('.tool-expand-icon');
      header.addEventListener('click', () => {
        detail.classList.toggle('open');
        expandIcon.classList.toggle('open');
        _scrollToBottom();
      });
    }
    if (isErr) el.style.borderColor = 'var(--red)';
    if (msg.task_id) el.dataset.bgTaskId = msg.task_id;

    elMessages.appendChild(el);
    _scrollToBottom();
  }

  // --- Turn boundaries ---

  function _addTurnStart(msg) {
    _flushStreaming();
    // Mostly silent — could add a divider.
  }

  function _addTurnEnd(msg) {
    _flushStreaming();
    // Collapse any pending activity (tools, thinking) accumulated during
    // the turn before appending the turn-end marker.  Without this, a
    // turn that ends with tool calls (no trailing assistant text) leaves
    // the tools uncollapsed because no later message triggers the
    // wrap.  Doing it here also lets the marker itself act as a clean
    // boundary between turns.
    _collapseActivity();
    const el = document.createElement('div');
    el.className = 'msg-turn-end';
    const _sub = msg.subtype || '';
    if (_sub === 'error' || _sub === 'interrupted') el.className += ' error';
    el.textContent = `Turn ${msg.turns ?? '?'} completed (${msg.duration || '?'}) — ${_sub}`;
    elMessages.appendChild(el);
    _scrollToBottom();
  }

  // --- System messages ---

  function _addSystemMsg(msg) {
    const subtype = msg.subtype || '';
    const data = msg.data || {};

    // Suppress noisy / internal-only subtypes that have no user-visible content.
    const _silent = new Set([
      'init', 'session_state_changed', 'connected',
      'unknown', 'worker_stopped',
    ]);
    if (_silent.has(subtype) && !data.message) return;

    let text = data.message || data.content || msg.content || '';
    if (data.error) text = `API retry: ${data.error}`;

    // Don't render empty system messages (they show as blank dark rectangles).
    if (!text) return;

    const el = document.createElement('div');
    let cls = 'msg msg-system';
    if (subtype === 'error') cls += ' error';
    else if (subtype === 'warning') cls += ' warning';
    else if (subtype === 'done') cls += ' done';
    else if (subtype === 'waiting') cls += ' waiting';

    el.className = cls;
    el.textContent = text;

    elMessages.appendChild(el);
    _scrollToBottom();
  }

  // --- /btw: a side exchange, answered in a fork of this conversation ---
  //
  // Rendered inline but visually apart, because that is exactly what it is: the
  // question and its answer belong in the reading order where they were asked,
  // while nothing in them is part of the conversation the main turn is having.
  // A fork's answer folded into the transcript unmarked would be indisputably
  // worse than not having the feature -- you would be reading replies to a
  // question the session itself never saw.

  const _btwEls = new Map();   // id -> {answer element}

  function _addBtwStart(msg) {
    const id = msg.id || '';
    const el = document.createElement('div');
    el.className = 'msg msg-btw pending';
    el.dataset.btwId = id;
    el.innerHTML =
      `<div class="btw-head">
         <span class="btw-tag">/btw</span>
         <span class="btw-note">side question — forked conversation, not seen by this session</span>
       </div>
       <div class="btw-question">${_esc(msg.question || '')}</div>
       <div class="btw-answer"></div>`;
    elMessages.appendChild(el);
    _btwEls.set(id, el.querySelector('.btw-answer'));
    _scrollToBottom();
  }

  function _addBtwDelta(msg) {
    const target = _btwEls.get(msg.id || '');
    if (!target) return;
    // Append rather than replace: the fork streams in blocks, and replacing
    // would show only the last one.
    target.textContent += msg.text || '';
    _scrollToBottom();
  }

  function _addBtwEnd(msg) {
    const id = msg.id || '';
    const target = _btwEls.get(id);
    _btwEls.delete(id);
    const el = elMessages.querySelector(`.msg-btw[data-btw-id="${window.CSS && CSS.escape ? CSS.escape(id) : id}"]`);
    if (el) el.classList.remove('pending');
    if (!msg.error) return;
    // A failed fork must say so in place.  Silently leaving an empty answer
    // block would read as "it had nothing to say".
    if (target && !target.textContent) {
      target.textContent = `(no answer — ${msg.error})`;
      target.classList.add('btw-failed');
    } else if (el) {
      const warn = document.createElement('div');
      warn.className = 'btw-failed';
      warn.textContent = `(ended early — ${msg.error})`;
      el.appendChild(warn);
    }
    _scrollToBottom();
  }

  // --- Modal (opened by backend immediate commands like /help) ---

  function _openModal(msg) {
    const title = msg.title || 'Detail';
    const content = msg.content || '';
    if (typeof App !== 'undefined' && typeof App.openModal === 'function') {
      App.openModal(title, content);
    } else {
      // Fallback: render inline as a system message so it isn't lost.
      _addSystemMsg({ subtype: 'info', data: { message: content } });
    }
  }

  // --- Command data (from immediate commands) ---

  function _addCommandData(msg) {
    // /usage is drawn in the modal, as Claude Code draws it (static/usage.js).
    if (msg.label === 'usage' && typeof Usage !== 'undefined' && Usage.show(msg.data)) {
      return;
    }

    const el = document.createElement('div');
    el.className = 'msg msg-command';

    const data = msg.data;
    const label = msg.label;

    // Model picker: current model + clickable list.
    if (label === 'model' && data && typeof data === 'object' && data.models) {
      _renderModelPicker(el, data);
      elMessages.appendChild(el);
      _scrollToBottom();
      return;
    }

    if (typeof data === 'string') {
      el.textContent = data;
    } else if (data && typeof data === 'object') {
      el.textContent = JSON.stringify(data, null, 2);
    } else {
      el.textContent = String(data);
    }
    elMessages.appendChild(el);
    _scrollToBottom();
  }

  function _renderModelPicker(el, data) {
    const current = data.current || '(unknown)';
    // Extract plain model id from e.g. "claude-opus-4-8 (pinned)".
    const currentId = current.split(/\s+\(/)[0].trim();

    let html = `<div class="model-picker">`;
    html += `<div class="model-picker-header">`;
    html += `<div class="model-picker-current">current model: <strong>${_esc(current)}</strong></div>`;
    html += `<button class="model-picker-close" title="Close" aria-label="Close">\u00d7</button>`;
    html += `</div>`;
    html += `<div class="model-picker-list">`;
    for (const m of data.models) {
      const active = m.id === currentId;
      const cls = 'model-picker-item' + (active ? ' active' : '');
      html += `<div class="${cls}" data-model-id="${_esc(m.id)}">`;
      html += `<span class="model-picker-name">${_esc(m.id)}</span>`;
      if (m.description) {
        html += `<span class="model-picker-desc">${_esc(m.description)}</span>`;
      }
      if (active) html += `<span class="model-picker-check">\u2713</span>`;
      html += `</div>`;
    }
    html += `</div>`;
    // The server couldn't reach the model API, so this is the built-in
    // fallback list and may be missing recently-released models.  Say so —
    // otherwise a missing model just looks like it doesn't exist.  Any id
    // still works when typed directly (`/model <id>`).
    // Two different failures deserve two different warnings. "Showing the
    // built-in list" is alarming and correct when the API has never answered;
    // it is simply wrong when we have a real list from four minutes ago, and
    // saying it anyway would train the reader to ignore the banner.
    if (data.source === 'builtin') {
      html += `<div class="model-picker-stale">`
            + `couldn\u2019t reach the model API \u2014 showing the built-in list, `
            + `which may be out of date. Any model id still works if you type it: `
            + `<code>/model &lt;id&gt;</code>`
            + `</div>`;
    } else if (data.source === 'cache') {
      const mins = Math.round((data.age_s || 0) / 60);
      const ago = mins < 1 ? 'less than a minute ago'
                : mins === 1 ? 'a minute ago'
                : mins + ' minutes ago';
      html += `<div class="model-picker-stale">`
            + `couldn\u2019t refresh just now \u2014 this list was fetched ${_esc(ago)}, `
            + `so anything released since is missing. Any model id still works `
            + `if you type it: <code>/model &lt;id&gt;</code>`
            + `</div>`;
    }
    html += `</div>`;
    el.innerHTML = html;

    el.addEventListener('click', (e) => {
      if (e.target.closest('.model-picker-close')) {
        el.remove();
        return;
      }
      const item = e.target.closest('.model-picker-item');
      if (!item) return;
      const id = item.getAttribute('data-model-id');
      if (id) {
        App.send({ type: 'message', text: '/model ' + id });
        el.remove();
      }
    });
  }

  // --- History replay ---

  // One history record -> one rendered message.  Extracted so the initial
  // replay and the older-message backfill cannot drift apart in what they
  // know how to draw.
  function _renderHistoryMessage(m) {
    const type = m.type || m.role;
    if (type === 'user' || type === 'human') {
      _addUserMessage(m.content || m.text || '', false, m.mid_turn === true);
    } else if (type === 'peer_message') {
      _addPeerMessage(m);
    } else if (type === 'injected_prompt') {
      _addInjectedPrompt({ content: m.content || m.text || '' });
    } else if (type === 'assistant') {
      _addAssistantText({ content: m.content || m.text || '', delta: false });
    } else if (type === 'tool_use') {
      _addToolUse(m);
    } else if (type === 'tool_result') {
      _addToolResult(m);
    } else if (type === 'thinking') {
      _addThinking(m);
    } else if (type === 'system' || type === 'system_msg') {
      _addSystemMsg(m);
    }
  }

  function _replayHistory(messages, moreAbove) {
    _moreAbove = moreAbove || 0;
    _historyShown = (messages && messages.length) || 0;
    console.log('[history] _replayHistory called, messages:', messages ? messages.length : 0);
    _setSessionLoading(false);   // history is here — drop the placeholder
    if (!messages || !messages.length) { _flushPending(); return; }

    _replayInProgress = true;

    // Add history separator (marked as boundary so activity-collapse
    // doesn't swallow it into a group).
    // Only label the top when this really is the top.  When older messages
    // are still coming, _prependHistory writes the separator above them.
    if (!_moreAbove) {
      const sep = document.createElement('div');
      sep.className = 'msg msg-system activity-boundary';
      sep.textContent = `--- Session history (${messages.length} messages) ---`;
      elMessages.appendChild(sep);
    }

    // Render in batches via setTimeout so the browser stays responsive
    // and WebSocket messages (status ticker, etc.) can still be processed.
    const BATCH = 50;
    let idx = 0;

    function _renderBatch() {
      // Batching exists to keep the browser responsive *for someone watching*.
      // A hidden window clamps setTimeout to one second — one minute once
      // intensive throttling starts — which would stretch a few thousand
      // history messages into an hour of drip-feed, and is the second way the
      // reported "a few lines per second" could happen. With nothing painting,
      // run straight through instead; live messages arriving meanwhile are
      // queued in _pendingMessages either way. Written as a loop, not
      // recursion: a large history would otherwise blow the stack.
      do {
        const end = Math.min(idx + BATCH, messages.length);
        for (; idx < end; idx++) {
          _renderHistoryMessage(messages[idx]);
        }
      } while (idx < messages.length && _hidden());

      if (idx < messages.length) {
        // Yield to the event loop, then continue.
        setTimeout(_renderBatch, 0);
      } else {
        // Done — add end separator and restore state.
        const endSep = document.createElement('div');
        endSep.className = 'msg msg-system activity-boundary';
        endSep.textContent = '--- End of history ---';
        elMessages.appendChild(endSep);

        // The thinking summaries the history held back, then the messages that
        // arrived during replay.
        _fitThinking(_takeThinkingToFit());
        _replayInProgress = false;
        _flushPending();

        _scrollToBottom();
      }
    }

    _renderBatch();
  }

  // Insert older history *above* what is already rendered.
  //
  // The first frame carries only the newest slice so the user sees their
  // conversation immediately; this fills in the rest behind it.
  //
  // The message builders all append to ``elMessages``, so rather than
  // duplicating seventeen of them for a different parent, we point that
  // variable at a detached container for the duration and move the finished
  // nodes across.
  //
  // This runs to completion synchronously, which is the only reason the swap
  // is safe: nothing else can observe the detached container in between. (An
  // earlier version also raised ``_replayInProgress`` here "so live messages
  // queue instead of landing in the scratch node" -- but a live message cannot
  // arrive mid-function in a single-threaded runtime, and a mutation sweep
  // duly showed no test could tell the flag from its absence. Removed rather
  // than kept as reassuring but inert.)
  function _prependHistory(messages) {
    if (!messages || !messages.length) { _moreAbove = 0; return; }
    const real = elMessages;
    const scratch = document.createElement('div');
    elMessages = scratch;
    try {
      const sep = document.createElement('div');
      sep.className = 'msg msg-system activity-boundary';
      sep.textContent = `--- Session history (${messages.length + _historyShown} messages) ---`;
      scratch.appendChild(sep);
      for (const m of messages) _renderHistoryMessage(m);
    } finally {
      elMessages = real;
      _moreAbove = 0;
    }

    // Anchor the viewport: inserting content above shifts everything down by
    // exactly the height added, so add that back to scrollTop or the user's
    // position jumps to older text they were not reading.
    const before = real.scrollHeight;
    const atBottom = _autoScroll;
    real.insertBefore(scratch.firstChild ? _drain(scratch) : document.createDocumentFragment(),
                      real.firstChild);
    if (atBottom) {
      _scrollToBottom();
    } else {
      real.scrollTop += (real.scrollHeight - before);
    }
    _maybeTrimOldMessages();
  }

  // Move every child of *node* into a fragment (leaves *node* empty).
  function _drain(node) {
    const frag = document.createDocumentFragment();
    while (node.firstChild) frag.appendChild(node.firstChild);
    return frag;
  }

  // --- Bell ---

  // Shared AudioContext.  Browsers' autoplay policy starts an AudioContext
  // in the "suspended" state until it is resumed inside a user gesture, so a
  // context created lazily inside _playBell() (which fires from a WS message,
  // not a user gesture) stays silent.  We create one shared context and
  // resume it on the first real user interaction so later bells can play.
  let _audioCtx = null;

  function _getAudioCtx() {
    if (!_audioCtx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      try { _audioCtx = new AC(); } catch (e) { return null; }
    }
    return _audioCtx;
  }

  function _initAudioUnlock() {
    const unlock = () => {
      const ctx = _getAudioCtx();
      if (ctx && ctx.state === 'suspended') {
        ctx.resume().catch(() => {});
      }
    };
    // { once: true } per event so listeners clean themselves up after the
    // first gesture of each type.
    window.addEventListener('pointerdown', unlock, { once: true });
    window.addEventListener('keydown', unlock, { once: true });
  }

  function _playBell() {
    // Simple audio bell if available, otherwise just flash the title.
    try {
      const ctx = _getAudioCtx();
      if (ctx) {
        // Resume if the autoplay policy left it suspended; play once ready.
        const fire = () => {
          const osc = ctx.createOscillator();
          const gain = ctx.createGain();
          osc.connect(gain);
          gain.connect(ctx.destination);
          osc.frequency.value = 800;
          gain.gain.value = 0.1;
          osc.start();
          osc.stop(ctx.currentTime + 0.15);
        };
        if (ctx.state === 'suspended') {
          ctx.resume().then(fire).catch(() => {});
        } else {
          fire();
        }
      }
    } catch (e) { /* ignore */ }

    // Flash the document title with a bell for 2s.
    _flashTitleBell();
  }

  // Prefix stripped/re-applied so overlapping bells can't stack up.  The old
  // code captured `document.title` as the restore target; a second bell within
  // the 2s window captured the already-prefixed title and, on restore, baked a
  // bell permanently into the tab name (the count grew over time).  Here we
  // always strip any existing bell prefix first, keep a single shared timer,
  // and on restore strip again from the *current* title (so a session-title
  // change mid-flash is preserved).
  let _titleBellTimer = null;
  const _BELL_PREFIX_RE = /^(?:\u{1F514}\s*)+/u;

  function _flashTitleBell() {
    const base = document.title.replace(_BELL_PREFIX_RE, '');
    document.title = '\u{1F514} ' + base;
    if (_titleBellTimer) clearTimeout(_titleBellTimer);
    _titleBellTimer = setTimeout(() => {
      _titleBellTimer = null;
      document.title = document.title.replace(_BELL_PREFIX_RE, '');
    }, 2000);
  }

  // --- Helpers ---

  // Tool header summaries — render full content and let CSS
  // text-overflow: ellipsis on .tool-summary handle truncation.

  function _toolHeaderSummary(h, input) {
    if (!h) return '';
    const inp = input || {};

    if (h.name === 'Bash')  return _bashSummary(inp);
    if (h.name === 'Edit')  return `<span class="sum-path">${_esc(inp.file_path || '')}</span>`;
    if (h.name === 'Write') {
      const lines = (inp.content || '').split('\n').length;
      return `<span class="sum-path">${_esc(inp.file_path || '')}</span> <span class="sum-dim">(${lines} lines)</span>`;
    }
    if (h.name === 'Read')  return _readSummary(inp);
    if (h.name === 'Grep')  return _grepSummary(inp);
    if (h.name === 'Glob')  return `<span class="sum-pattern">${_esc(inp.pattern || '')}</span>`;
    if (h.name === 'WebSearch') return `<span class="sum-pattern">${_esc(inp.query || '')}</span>`;
    if (h.name === 'WebFetch')  return `<span class="sum-path">${_esc(inp.url || '')}</span>`;
    if (h.name === 'Agent' || h.name === 'Task') {
      return `<span class="sum-dim">${_esc(inp.description || '')}</span>`;
    }
    if (h.name === 'TodoWrite') {
      const n = Array.isArray(inp.todos) ? inp.todos.length : 0;
      return `<span class="sum-dim">${n} items</span>`;
    }
    if (h.name === 'AskUserQuestion') {
      const qs = Array.isArray(inp.questions) ? inp.questions : [];
      const first = qs.length ? (qs[0].question || qs[0].header || '') : '';
      const count = qs.length !== 1 ? `${qs.length} questions` : '1 question';
      return `<span class="sum-dim">${count}</span>` +
             (first ? ` <span class="sum-cmd">${_esc(first)}</span>` : '');
    }
    return '';
  }

  /** Bash: show command if single-line; multi-line → size hint. */
  function _bashSummary(inp) {
    const cmd = inp.command || '';
    const lines = cmd.split('\n');
    if (lines.length === 1) {
      return `<span class="sum-cmd">${_esc((lines[0] || '').trim())}</span>`;
    }
    return `<span class="sum-dim">(${cmd.length} chars, ${lines.length} lines)</span>`;
  }

  /** Read: full path + offset/limit params if present. */
  function _readSummary(inp) {
    const params = [];
    if (inp.offset != null) params.push(`from ${inp.offset}`);
    if (inp.limit != null) params.push(`${inp.limit} lines`);
    const paramStr = params.length ? ` <span class="sum-dim">(${params.join(', ')})</span>` : '';
    return `<span class="sum-path">${_esc(inp.file_path || '')}</span>${paramStr}`;
  }

  /** Grep: full pattern + full path. CSS truncates if needed. */
  function _grepSummary(inp) {
    const pattern = inp.pattern || '';
    const rawPath = inp.path || '.';
    return `<span class="sum-pattern">"${_esc(pattern)}"</span> <span class="sum-dim">in</span> <span class="sum-path">${_esc(rawPath)}</span>`;
  }

  function _toolDetailHtml(name, input, msg) {
    let html = '';

    if (name === 'Bash') {
      html += `<div class="detail-label">Command</div>
               <pre>${_esc(input.command || '')}</pre>`;
      if (input.description) {
        html += `<div class="detail-label">Description</div>
                 <div class="detail-field">${_esc(input.description)}</div>`;
      }
    } else if (name === 'Edit') {
      html += `<div class="detail-label">File</div>
               <div class="detail-field">${_esc(input.file_path || '')}</div>`;
      if (input.old_string || input.new_string) {
        // Side-by-side diff view.
        html += Diff.render(
          input.old_string || '',
          input.new_string || '',
          input.file_path,
        );
      }
    } else if (name === 'Write') {
      html += `<div class="detail-label">File</div>
               <div class="detail-field">${_esc(input.file_path || '')}</div>`;
      html += `<div class="detail-label">Content (${(input.content || '').split('\n').length} lines)</div>
               <pre>${_esc(input.content || '')}</pre>`;
    } else if (name === 'Read') {
      html += `<div class="detail-label">File</div>
               <div class="detail-field">${_esc(input.file_path || '')}</div>`;
    } else if (name === 'Grep') {
      html += `<div class="detail-label">Pattern</div>
               <div class="detail-field">${_esc(input.pattern || '')}</div>`;
      html += `<div class="detail-label">Path</div>
               <div class="detail-field">${_esc(input.path || '.')}</div>`;
    } else if (name === 'AskUserQuestion') {
      html += _askQuestionDetailHtml(input);
    } else {
      // Generic: show JSON.
      html += `<div class="detail-label">Input</div>
               <pre>${_esc(JSON.stringify(input, null, 2))}</pre>`;
    }

    return html;
  }

  /** AskUserQuestion: render each question with its selectable options. */
  function _askQuestionDetailHtml(input) {
    const qs = Array.isArray(input.questions) ? input.questions : [];
    if (!qs.length) {
      return `<div class="detail-label">Input</div>
              <pre>${_esc(JSON.stringify(input, null, 2))}</pre>`;
    }
    let html = '';
    for (const q of qs) {
      const header = q.header ? `<span class="ask-q-chip">${_esc(q.header)}</span>` : '';
      const multi = q.multiSelect ? ` <span class="sum-dim">(select multiple)</span>` : '';
      html += `<div class="ask-question">
        <div class="ask-q-text">${header}${_esc(q.question || '')}${multi}</div>`;
      const opts = Array.isArray(q.options) ? q.options : [];
      if (opts.length) {
        html += `<ol class="ask-options">`;
        for (const o of opts) {
          const desc = o.description
            ? `<span class="ask-opt-desc">${_esc(o.description)}</span>` : '';
          html += `<li><span class="ask-opt-label">${_esc(o.label || '')}</span>${desc}</li>`;
        }
        html += `<li class="ask-opt-other"><span class="ask-opt-label">Other…</span>` +
                `<span class="ask-opt-desc">provide a custom answer</span></li>`;
        html += `</ol>`;
      }
      html += `</div>`;
    }
    return html;
  }

  function _esc(s) {
    if (!s) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  /**
   * Markdown → HTML for what the model writes, by markdown-it
   * (static/vendor/markdown-it.min.js, loaded before this file): CommonMark,
   * which is what models write, rendered with the chat's own markup for code,
   * lists, tables, headings and rules (_markdown).
   *
   * Where it differs from plain CommonMark, it is on purpose:
   * - a line break the model wrote is a line break (``breaks``), so a
   *   "**Label**" line over its paragraph stays two lines;
   * - lists are read as models write them (_asMeant): a sub-list written
   *   short of its item's text still nests, text at the margin after a list
   *   ends it, and a numbered sub-list may start past 1;
   * - a link goes to the web or an address only; any other target is shown
   *   as its text, the target in a tooltip (_neuterLinks);
   * - raw HTML is shown as text, and an image as a link to it, never loaded.
   *
   * Should the library be missing, the text is shown as it is, escaped, its
   * lines kept.
   */
  function _md(raw) {
    if (!raw) return '';
    const md = _markdown();
    const text = String(raw).replace(/\r\n?/g, '\n');
    if (!md) return _esc(text).replace(/\n/g, '<br>');
    const starts = new Map();
    return md.render(_asMeant(text, starts, _codeLines(md, text)), { starts }).trimEnd();
  }

  /** The lines of *text* that markdown-it reads as code, by index: a fenced
   *  or indented block's -- but for a list item's own line its code starts
   *  on ("- ```sh", "-      x"), which is the item's, for _asMeant to read. */
  function _codeLines(md, text) {
    const tokens = [];
    md.block.parse(text, md, {}, tokens);
    const code = new Set();
    tokens.forEach((t, k) => {
      if (t.type !== 'fence' && t.type !== 'code_block') return;
      const before = tokens[k - 1];
      const own = before && before.type === 'list_item_open' && before.map[0] === t.map[0];
      for (let i = t.map[0] + (own ? 1 : 0); i < t.map[1]; i++) code.add(i);
    });
    return code;
  }

  let _markdownIt = null;
  let _markdownMissing = false;

  /** The configured markdown-it instance; null, said once, if the library
   *  did not load. */
  function _markdown() {
    if (_markdownIt) return _markdownIt;
    if (typeof markdownit !== 'function') {
      if (!_markdownMissing) {
        _markdownMissing = true;
        console.warn('markdown-it is not loaded: messages are shown as plain text');
      }
      return null;
    }
    const md = markdownit({ html: false, linkify: true, breaks: true, typographer: false });
    // Links written out, not guessed: "server.py" is not a web address.
    md.linkify.set({ fuzzyLink: false, fuzzyEmail: false });
    // A link to an image stays a link; nothing is fetched because a model wrote it.
    md.disable('image');
    // Every link is parsed, so that one refused below keeps its text.
    md.validateLink = () => true;
    md.core.ruler.push('neuter_links', _neuterLinks);
    md.core.ruler.push('list_starts', _listStarts);
    md.inline.ruler.before('escape', 'windows_path', _windowsPath);
    md.inline.ruler.before('emphasis', 'dunder', _dunder);
    const r = md.renderer.rules;
    const esc = md.utils.escapeHtml;
    r.code_inline = (tokens, i) => `<code class="md-code">${esc(tokens[i].content)}</code>`;
    const codeBlock = (code, lang) => {
      const l = esc(lang || '');
      return `<div class="code-block"${l ? ` data-lang="${l}"` : ''}>`
        + (l ? `<span class="code-lang">${l}</span>` : '')
        + `<pre><code>${esc(code.replace(/\n$/, ''))}</code></pre></div>\n`;
    };
    // The info string's first word is the language: "python title=x" is python.
    r.fence = (tokens, i) => codeBlock(tokens[i].content, tokens[i].info.trim().split(/\s+/)[0]);
    r.code_block = (tokens, i) => codeBlock(tokens[i].content, '');
    r.heading_open = (tokens, i) => `<div class="md-h md-${tokens[i].tag}">`;
    r.heading_close = () => '</div>\n';
    for (const [type, cls] of [['bullet_list_open', 'md-list'], ['ordered_list_open', 'md-list'],
                               ['table_open', 'md-table'], ['hr', 'md-hr'],
                               ['blockquote_open', 'md-quote']]) {
      r[type] = (tokens, i, opts, env, self) => {
        tokens[i].attrJoin('class', cls);
        return self.renderToken(tokens, i, opts);
      };
    }
    _markdownIt = md;
    return md;
  }

  // A link target that can only navigate: http(s) or mailto, and nothing in
  // front of the scheme that a browser would strip (spaces, control bytes).
  const SAFE_HREF = /^(https?:|mailto:)/i;

  /**
   * A markdown-it core rule: a link to the web or an address opens in a new
   * tab; any other becomes its text, the target in a tooltip.  A model can be
   * led by what it reads into writing [text](javascript:...), which would run
   * in this page when clicked, and a file path would resolve against the hub.
   */
  function _neuterLinks(state) {
    for (const block of state.tokens) {
      if (block.type !== 'inline' || !block.children) continue;
      let inert = false;     // the link open is refused (links do not nest)
      for (const t of block.children) {
        if (t.type === 'link_open') {
          const href = t.attrGet('href') || '';
          inert = !SAFE_HREF.test(href);
          if (inert) {
            t.type = 'link_inert_open';
            t.tag = 'span';
            // Readable: "D:\x", not the "D:%5Cx" a link's target is kept as.
            t.attrs = [['class', 'md-link-inert'], ['title', state.md.normalizeLinkText(href)]];
          } else {
            t.attrSet('target', '_blank');
            t.attrSet('rel', 'noopener');
          }
        } else if (t.type === 'link_close' && inert) {
          t.type = 'link_inert_close';
          t.tag = 'span';
        }
      }
    }
  }

  /** A markdown-it core rule: each numbered list _asMeant wrote to start at
   *  1 starts where the model wrote it to (env.starts: line → number). */
  function _listStarts(state) {
    const starts = state.env.starts;
    for (const t of state.tokens) {
      if (t.type === 'ordered_list_open' && starts.has(t.map[0])) {
        t.attrSet('start', String(starts.get(t.map[0])));
      }
    }
  }

  // A Windows path, from its first "\" to the end of its last segment.  A
  // segment may hold spaces where another "\" follows it ("D:\visual studio
  // projects\x"), and none of what a Windows name may not (: * ? " < > |),
  // nor a bracket or a backtick: so it stops at the next drive, at markup,
  // and at the end of a link's text.
  const WIN_PATH = /^\\(?:[^\s\\`[\]<>:*?"|]+(?: [^\s\\`[\]<>:*?"|]+)*\\)*[^\s\\`[\]<>:*?"|]*/;
  // What closes around a path or ends its sentence, not the path's.
  const PATH_TAIL = /[.,;!?')_]+$/;

  /**
   * A markdown-it inline rule: a Windows path in prose is text as written --
   * after a drive's "C:", or a share's "\\server\".  CommonMark would take
   * the "\." in "C:\Users\me\.claude" for an escaped ".", and the "_"s of
   * "C:\my_dir\x_y" for emphasis.  *state* and *silent* are markdown-it's.
   */
  function _windowsPath(state, silent) {
    const { src, pos } = state;
    if (src.charCodeAt(pos) !== 0x5C) return false;
    const drive = /(?:^|[^A-Za-z0-9])[A-Za-z]:$/.test(src.slice(Math.max(0, pos - 3), pos));
    const share = !drive && /^\\\\[\w.$-]+\\/.test(src.slice(pos, pos + 260))
      && (pos === 0 || /[\s(["']/.test(src[pos - 1]));
    if (!drive && !share) return false;
    const from = share ? pos + 1 : pos;
    const path = src.slice(pos, from)
      + src.slice(from).match(WIN_PATH)[0].replace(PATH_TAIL, '');
    if (!silent) state.pending += path;
    state.pos += path.length;
    return true;
  }

  // A "dunder" name: "__init__" is a name, not "init" in bold.
  const DUNDER = /^__[A-Za-z0-9]\w*?__(?![A-Za-z0-9])/;

  /** A markdown-it inline rule: a dunder name in prose is text as written. */
  function _dunder(state, silent) {
    const { src, pos } = state;
    if (src.charCodeAt(pos) !== 0x5F) return false;
    const m = src.slice(pos, pos + 200).match(DUNDER);
    if (!m) return false;
    if (!silent) state.pending += m[0];
    state.pos += m[0].length;
    return true;
  }

  // A list item's first line: [line, indentation, marker, the gap after it,
  // undefined for an empty item].
  const LIST_ITEM = /^( *)(\d{1,9}[.)]|[-*+])(?:( +)\S| *$)/;
  // A line's indentation and the list markers on it, with the gaps after them.
  const LEAD = /^[ \t]*(?:(?:\d{1,9}[.)]|[-*+])[ \t]+)*/;
  // Lists stacked deeper than this on one line ("- - - - x") are text from
  // there on: markdown-it drops what is nested past its own limit.  [the
  // line up to the marker past the limit, and that marker's digits].  The
  // indentation is matched once, so no two parts compete for the same spaces.
  const MAX_STACKED = 16;
  const STACKED = new RegExp(
    `^ *(?:(?:[-*+]|\\d{1,9}[.)]) +){${MAX_STACKED}}\\d{0,9}(?=[-*+.)](?: |$))`);

  /**
   * *src* as the model meant it, where CommonMark reads it otherwise.
   *
   * - A sub-list written short of its item's text nests.  CommonMark nests a
   *   line under an item only if it reaches the item's text -- three columns
   *   under "1. ", four under "10. " -- and a sub-list two spaces in ends the
   *   list instead.  A list item indented at least two past its parent's
   *   marker but short of its text is moved in, with everything under it.
   * - Text at the margin straight after a list ends the list.  CommonMark
   *   reads it as the last item's paragraph going on ("lazy continuation"),
   *   a convenience for typing by hand that models do not mean; a blank line
   *   is put before it.
   * - A numbered sub-list starting past 1 starts.  CommonMark lets only a
   *   list starting at 1 break into its item's text (which keeps a "1987."
   *   from starting a list), so it is written to start at 1 -- "01." for
   *   "10.", keeping its width -- and *starts* (output line → number) has
   *   _listStarts put its number back.
   * - Markers stacked past MAX_STACKED on one line are text from there on.
   *
   * Tabs in a line's indentation, and after its markers, are read as the
   * spaces they stand for, to the next multiple of four.  The lines of
   * *code* (their indexes, from _codeLines) are left as written, only moved
   * in with a sub-list they are under.
   */
  function _asMeant(src, starts, code) {
    const out = [];
    const items = [];        // the open items: {at, text}, marker and text columns
    const shifts = [];       // short sub-lists being moved in: {from, by}, innermost last
    const lines = src.split('\n');
    for (let i = 0; i < lines.length; i++) {
      const raw = lines[i];
      let line = _untab(raw);
      const ind = _indent(line);
      // Moved in by every short sub-list it is under: those begun at or left
      // of where it starts, as written.
      const by = shifts.reduce((n, s) => n + (s.from <= ind ? s.by : 0), 0);
      if (code.has(i)) {
        // Its tabs too: a Makefile's recipe line is a tab.
        out.push(' '.repeat(by) + raw);
        continue;
      }
      if (line.trim() === '') { out.push(line); continue; }
      while (shifts.length && ind < shifts[shifts.length - 1].from) shifts.pop();
      line = ' '.repeat(by) + line;
      const at = _indent(line);
      let m = line.match(LIST_ITEM);
      // Not after code: a blank line would be the code's, in a block this
      // line ends by ending its item.
      if (!m && items.length && at <= items[0].at && out[out.length - 1].trim() !== ''
          && !code.has(i - 1)) {
        out.push('');
      }
      while (items.length && at <= items[items.length - 1].at) items.pop();
      if (m) {
        const parent = items[items.length - 1];
        if (parent && at >= parent.at + 2 && at < parent.text) {
          shifts.push({ from: ind, by: parent.text - at });
          line = ' '.repeat(parent.text - at) + line;
          m = line.match(LIST_ITEM);
        }
        // Wherever it needs to or not -- after a blank line, or an item
        // after the first, whose number does not count -- for the same result.
        // Not an empty item: it may not break into text at all, and its
        // number is shown as written.
        const n = parseInt(m[2], 10);
        if (parent && m[3] && /^\d/.test(m[2]) && n !== 1) {
          const digits = m[2].length - 1;
          starts.set(out.length, n);
          line = m[1] + '1'.padStart(digits, '0') + line.slice(m[1].length + digits);
        }
        // Its text one past the marker when there is none, or when five
        // spaces or more follow it: they begin a code block in the item.
        const gap = m[3] && m[3].length <= 4 ? m[3].length : 1;
        items.push({ at: m[1].length, text: m[1].length + m[2].length + gap });
        // An escaped marker is no marker: "\-", "12\.".
        const stacked = line.match(STACKED);
        if (stacked) line = stacked[0] + '\\' + line.slice(stacked[0].length);
      }
      out.push(line);
    }
    return out.join('\n');
  }

  /** How many spaces *line* starts with. */
  function _indent(line) {
    return line.match(/^ */)[0].length;
  }

  /** *line* with the tabs in its LEAD as the spaces they stand for, to the
   *  next multiple of four, as CommonMark reads them. */
  function _untab(line) {
    const lead = line.match(LEAD)[0];
    if (!lead.includes('\t')) return line;
    let col = 0;
    let out = '';
    for (const ch of lead) {
      const next = ch === '\t' ? col + 4 - (col % 4) : col + 1;
      out += ch === '\t' ? ' '.repeat(next - col) : ch;
      col = next;
    }
    return out + line.slice(lead.length);
  }

  function setCollapseTools(v) { _collapseTools = !!v; }
  function getCollapseTools() { return _collapseTools; }

  function setShowThinking(v) { _showThinking = !!v; }
  function getShowThinking() { return _showThinking; }

  function forceScrollToBottom() {
    _autoScroll = true;
    _followToBottom();
  }

  return { init, clear, handleMessage, setCollapseTools, getCollapseTools,
           setShowThinking, getShowThinking, setMaxDomMessages, forceScrollToBottom,
           beginCatchUp, endCatchUp, fitThinking };
})();
