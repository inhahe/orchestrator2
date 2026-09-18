"""Opening a session must not cost more the longer the session is.

Asked 2026-09-06: "when does that 41 s read happen? is that why it takes so
long between launching a session and the tab opening?"

Measured: the tab's shell appeared in ~5 s, but the transcript stayed blank for
another ~42 s. ``_tail_read_jsonl`` is supposed to read only the tail of a
transcript, but it derived the average line size *from the file size*::

    avg_line_bytes = file_size // (max_records * 8)      # 2000 * 8 = 16000
    seek_bytes     = max_records * avg_line_bytes * 4

which is ``file_size * 2000 * 4 / 16000`` -- **exactly half the file, whatever
its size**::

    1448 MB transcript -> 724 MB read      603 MB -> 302 MB
     588 MB transcript -> 294 MB read      ... always 50%

It also undershot its own target: 294 MB of one file yielded 693 records
against a ``max_records`` of 2000, because these transcripts carry enormous
tool results. So the estimate is kept as a lower bound and capped absolutely.

Measured after (machine idle, same files): 1448 MB -> 3.0 s, 603 MB -> 1.4 s,
588 MB -> 1.3 s, against 4.9 s for the *smallest* of them before.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import session as S  # noqa: E402


def _big_transcript(tmp_path, *, records: int, pad: int) -> Path:
    """A JSONL whose records are *pad* bytes of filler each."""
    p = Path(tmp_path) / "big.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for i in range(records):
            fh.write(json.dumps({
                "type": "user",
                "message": {"role": "user", "content": f"m{i} " + "x" * pad},
            }) + "\n")
    return p


# ---------------------------------------------------------------------------
# The cap
# ---------------------------------------------------------------------------

def test_the_cap_exists_and_is_sane():
    """Big enough to hold a useful backscroll, small enough to bound the read.
    Asserted as absolute bytes, not in terms of the estimate it overrides --
    a bound expressed in terms of the thing it bounds is not a bound."""
    assert 1024 * 1024 <= S.MAX_TAIL_BYTES <= 128 * 1024 * 1024


def test_a_huge_transcript_is_not_read_in_full(tmp_path, monkeypatch):
    """The property that matters: cost stops scaling with history length.

    Asserted on *records returned*, not on bytes read.  An earlier version of
    this test wrapped ``fh.read`` to count bytes -- but the reader iterates the
    file object line by line, which goes through buffered reads and never
    touches that wrapper, so it counted almost nothing and passed either way.
    A mutation sweep caught it: removing the cap entirely left the test green.

    Records are a faithful proxy here because the fixture's records are a fixed
    size, so "how many came back" is "how far back it seeked" divided by a
    constant.
    """
    p = _big_transcript(tmp_path, records=4000, pad=4000)   # ~16 MB
    size = p.stat().st_size
    assert size > 8 * 1024 * 1024, "fixture too small to be meaningful"

    # Uncapped, the estimate reads half the file -- ~2000 of the 4000 records.
    uncapped, _ = S._tail_read_jsonl(p, 2000)

    monkeypatch.setattr(S, "MAX_TAIL_BYTES", 1024 * 1024)
    capped, _ = S._tail_read_jsonl(p, 2000)

    assert capped, "the cap read nothing at all"
    assert len(capped) < len(uncapped) / 4, (
        f"cap made little difference: {len(capped)} records vs "
        f"{len(uncapped)} uncapped -- the bound is not binding")
    # ~1 MB of 4 KB records.
    assert len(capped) < 400, f"read back {len(capped)} records from a 1 MB cap"


def test_the_newest_records_are_the_ones_kept(tmp_path):
    """A bounded read is only acceptable if it keeps the *end* of the file."""
    p = _big_transcript(tmp_path, records=3000, pad=3000)
    records, _ = S._tail_read_jsonl(p, 2000)
    contents = [r["message"]["content"].split()[0] for r in records]
    assert contents[-1] == "m2999", "the tail read did not reach the end"


def test_a_small_transcript_is_still_read_whole(tmp_path):
    """The cap must not disturb ordinary sessions."""
    p = _big_transcript(tmp_path, records=20, pad=10)
    records, skipped = S._tail_read_jsonl(p, 2000)
    assert len(records) == 20
    assert skipped == 0


def test_a_useful_amount_of_history_still_survives(tmp_path):
    """A cap tight enough to bound the read must still leave a backscroll: a
    fast attach that shows three messages has solved the wrong problem."""
    p = _big_transcript(tmp_path, records=5000, pad=2000)   # ~10 MB, ~2 KB/rec
    records, _ = S._tail_read_jsonl(p, 2000)
    assert len(records) >= 500, (
        f"only {len(records)} records survived the cap")


def test_the_estimate_is_still_a_lower_bound(tmp_path):
    """The cap replaces an over-estimate, not the estimate itself: a file whose
    lines are small must not be truncated to the cap for no reason."""
    p = _big_transcript(tmp_path, records=300, pad=100)
    records, _ = S._tail_read_jsonl(p, 2000)
    assert len(records) == 300


# ---------------------------------------------------------------------------
# Still honest about what it returns
# ---------------------------------------------------------------------------

def test_records_are_parsed_not_truncated(tmp_path):
    """Seeking into the middle of a line must not yield a half-record."""
    p = _big_transcript(tmp_path, records=2000, pad=5000)
    records, _ = S._tail_read_jsonl(p, 2000)
    for r in records:
        assert isinstance(r, dict) and "message" in r


def test_render_still_produces_messages_and_todos(tmp_path):
    """End to end through the capped read."""
    p = Path(tmp_path) / "s.jsonl"
    recs = [{"type": "user", "message": {"role": "user", "content": "x" * 500}}
            for _ in range(200)]
    recs.append({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "TodoWrite", "id": "t",
         "input": {"todos": [{"content": "plan", "status": "pending"}]}}]}})
    p.write_text("\n".join(json.dumps(r) for r in recs), encoding="utf-8")
    count, messages, _orphans, todos = S.render_session_history(p)
    assert count > 0 and messages
    assert [t["content"] for t in todos] == ["plan"]
