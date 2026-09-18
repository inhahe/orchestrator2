"""A session whose history the model can no longer reach must say so.

From a report, right after a `/model` switch: "claude seemed not to remember
any of the conversation that just happened."

It hadn't.  A session JSONL is a *tree* linked by ``parentUuid``, and its two
readers disagree about what a session is:

* we render history by walking the file top-to-bottom, so the browser shows
  every record in it;
* the CLI rebuilds the model's context from the last message backwards along
  ``parentUuid``.

On 2026-07-24 a lost write left a run of NUL bytes in the middle of two
different session files, three minutes apart (the file size was committed but
the data never reached disk).  The record in the hole was a ``tool_result``, so
the chain became unusable from there and every later resume silently restarted
at the last intact node and grew a *new branch* off it.  Three weeks later the
raytracer session had 27 branches hanging off one July-24 node, a live chain of
287 records, and 112,022 (99.7%) unreachable — while the browser still showed
the whole transcript, which is why nobody noticed for three weeks.

These tests use small synthetic files with the same shapes.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from session import (  # noqa: E402
    analyze_session_chain,
    describe_integrity_problem,
    find_nul_hole,
)


def _rec(uuid: str, parent: str | None, *, typ: str = "assistant",
         ts: str = "2026-07-24T09:00:00.000Z", sidechain: bool = False,
         payload: str = "") -> bytes:
    """A record in the CLI's *real* field order — which differs by record type.

    This matters, and an earlier version of this helper got it wrong in a way
    that hid a live bug.  It emitted ``"type"`` before ``"message"`` for every
    record; the CLI only does that for ``user`` records.  On an **assistant**
    record the message object comes first and carries its own
    ``"type":"message"``, and its content blocks carry ``"type":"text"`` /
    ``"tool_use"`` / ``"thinking"`` — so a scanner taking the first
    ``"type":"…"`` on the line reads "message" and never "assistant".  Written
    the old way, these tests passed against a parser that could not identify a
    single assistant record in a real session file.

    Layouts verified against the two damaged files:
      user:      parentUuid, isSidechain, promptId, type, message, uuid, timestamp
      assistant: parentUuid, isSidechain, message, requestId, type, uuid, timestamp
    """
    msg: dict = {"role": typ,
                 "content": [{"type": "text", "text": payload}]}
    if typ == "assistant":
        msg = {"model": "claude-opus-4-8", "id": "msg_01", "type": "message",
               **msg}
        body = {"parentUuid": parent, "isSidechain": sidechain, "message": msg,
                "requestId": "req_01", "type": typ,
                "uuid": uuid, "timestamp": ts}
    else:
        body = {"parentUuid": parent, "isSidechain": sidechain,
                "promptId": "p_01", "type": typ, "message": msg,
                "uuid": uuid, "timestamp": ts}
    return json.dumps(body, separators=(",", ":")).encode() + b"\n"


def _u(n: int) -> str:
    return f"{n:08d}-0000-4000-8000-000000000000"


def _write(path, lines: list[bytes]) -> None:
    with open(path, "wb") as f:
        for ln in lines:
            f.write(ln)


# --------------------------------------------------------------------------
# The NUL hole itself
# --------------------------------------------------------------------------

def test_a_healthy_file_has_no_hole(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, [_rec(_u(i), _u(i - 1) if i else None) for i in range(20)])
    assert find_nul_hole(p) is None


def test_a_lost_write_is_found_at_its_offset(tmp_path):
    p = tmp_path / "s.jsonl"
    head = [_rec(_u(i), _u(i - 1) if i else None) for i in range(5)]
    offset = sum(len(x) for x in head)
    _write(p, head + [b"\x00" * 4096 + _rec(_u(99), _u(4))])
    assert find_nul_hole(p) == offset


def test_the_scan_can_resume_from_a_known_clean_offset(tmp_path):
    """Corruption lives in the prefix of an append-only file, so a range once
    known clean never has to be read again — that's what keeps this cheap on a
    600 MB session."""
    p = tmp_path / "s.jsonl"
    head = [_rec(_u(i), _u(i - 1) if i else None) for i in range(5)]
    offset = sum(len(x) for x in head)
    _write(p, head + [b"\x00" * 64 + _rec(_u(99), _u(4))])
    assert find_nul_hole(p, start=offset + 4096) is None
    assert find_nul_hole(p, start=0) == offset


# --------------------------------------------------------------------------
# Shape 1 — the raytracer: resumes re-attach to the last intact node
# --------------------------------------------------------------------------

def _forked_session(tmp_path, branches: int = 3, per_branch: int = 4):
    """Old history, a break, then N branches all rooted at the last good node."""
    p = tmp_path / "s.jsonl"
    lines = [_rec(_u(i), _u(i - 1) if i else None,
                  ts=f"2026-07-24T09:{i:02d}:00.000Z") for i in range(10)]
    join = _u(9)
    lines.append(b"\x00" * 512 + b"\n")          # the lost write
    n = 100
    for b in range(branches):
        prev = join                              # every resume re-attaches here
        for k in range(per_branch):
            lines.append(_rec(_u(n), prev,
                              typ="user" if k == 0 else "assistant",
                              ts=f"2026-08-{10 + b:02d}T12:{k:02d}:00.000Z"))
            prev = _u(n)
            n += 1
    _write(p, lines)
    return p


def test_the_branch_point_is_where_history_stops(tmp_path):
    r = analyze_session_chain(_forked_session(tmp_path))

    assert r["rooted"] is True, "this shape still reaches the real root"
    assert r["branches"] == 3, "each resume left a branch at the same node"
    assert r["truncated_at"] == "2026-07-24T09:09:00.000Z", \
        "history stops at the last node before the lost write"


def test_most_of_the_session_is_unreachable(tmp_path):
    r = analyze_session_chain(_forked_session(tmp_path, branches=3, per_branch=4))

    assert r["total"] == 22          # 10 old + 3 branches x 4
    assert r["chain"] == 14          # 10 old + the newest branch only
    assert r["stranded"] == 8, "the other branches' work is invisible to the model"
    assert r["corrupt_lines"] == 1


def test_the_summary_names_the_cliff(tmp_path):
    msg = describe_integrity_problem(analyze_session_chain(_forked_session(tmp_path)))
    assert "2026-07-24T09:09:00.000Z" in msg
    assert "unreachable" in msg


# --------------------------------------------------------------------------
# Shape 2 — SlateOS: the chain's own root is gone
# --------------------------------------------------------------------------

def test_an_orphaned_chain_reports_where_the_model_starts(tmp_path):
    """Here the break swallowed the parent record itself, so the live chain
    dangles instead of rejoining old history."""
    p = tmp_path / "s.jsonl"
    lines = [_rec(_u(i), _u(i - 1) if i else None) for i in range(10)]
    lines.append(b"\x00" * 256 + b"\n")
    prev = _u(500)                                # a uuid that is nowhere in the file
    for k in range(4):
        lines.append(_rec(_u(600 + k), prev, typ="user" if k == 0 else "assistant",
                          ts=f"2026-08-16T21:{37 + k:02d}:00.000Z"))
        prev = _u(600 + k)
    _write(p, lines)

    r = analyze_session_chain(p)
    assert r["rooted"] is False, "the chain's parent record is missing entirely"
    assert r["chain"] == 4
    assert r["stranded"] == 10
    assert r["chain_starts_at"] == "2026-08-16T21:37:00.000Z"
    assert "nothing before 2026-08-16T21:37:00.000Z" in describe_integrity_problem(r)


# --------------------------------------------------------------------------
# Things that must not be mistaken for damage
# --------------------------------------------------------------------------

def test_a_healthy_session_reports_nothing_wrong(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, [_rec(_u(i), _u(i - 1) if i else None) for i in range(30)])

    r = analyze_session_chain(p)
    assert r["rooted"] is True
    assert r["chain"] == r["total"] == 30
    assert r["stranded"] == 0
    assert r["branches"] == 0


def test_a_subagent_sidechain_is_not_the_leaf(tmp_path):
    """Sidechains carry uuids and form their own short chains, so ending a file
    with one would make us measure a subagent rather than the conversation.
    Defensive: neither damaged session contains a sidechain record — unlike the
    trailing ``custom-title`` case below, which is real."""
    p = tmp_path / "s.jsonl"
    lines = [_rec(_u(i), _u(i - 1) if i else None) for i in range(20)]
    prev = _u(19)
    for k in range(3):                            # subagent transcript at the tail
        lines.append(_rec(_u(700 + k), prev, sidechain=True))
        prev = _u(700 + k)
    _write(p, lines)

    r = analyze_session_chain(p)
    assert r["chain"] == 20, "measured the subagent instead of the conversation"
    assert r["stranded"] == 3


def test_a_uuid_inside_a_tool_result_is_not_the_records_identity(tmp_path):
    """The record's own uuid is the *last* one on the line, so a payload that
    quotes one can't hijack the record's identity and truncate the walk there.

    Written with the quotes *unescaped* on purpose.  A real payload goes through
    ``json.dumps`` and comes out ``\\"uuid\\":\\"…\\"``, which cannot match — that
    is why no line in either damaged session (431 k records) has two uuid
    matches, and why this guard is defensive rather than a fix for something
    observed.  Escaping it here would make the test pass either way and assert
    nothing."""
    p = tmp_path / "s.jsonl"
    lines = [_rec(_u(i), _u(i - 1) if i else None) for i in range(10)]
    hijack = b'"uuid":"deadbeef-0000-4000-8000-000000000000"'
    lines.append(_rec(_u(10), _u(9)).replace(b'"text":""', b'"text":"x",' + hijack))
    lines.append(_rec(_u(11), _u(10)))
    _write(p, lines)

    r = analyze_session_chain(p)
    assert r["chain"] == 12 and r["stranded"] == 0


def test_a_trailing_non_message_record_is_not_the_leaf(tmp_path):
    """`custom-title` / `queue-operation` records land after the last message."""
    p = tmp_path / "s.jsonl"
    lines = [_rec(_u(i), _u(i - 1) if i else None) for i in range(12)]
    lines.append(json.dumps(
        {"type": "custom-title", "customTitle": "Good Photons",
         "sessionId": "x"}, separators=(",", ":")).encode() + b"\n")
    _write(p, lines)

    assert analyze_session_chain(p)["chain"] == 12


def test_a_compaction_is_not_a_loss(tmp_path):
    """The mistake this whole metric was rewritten to stop making.

    A compaction writes its summary with ``parentUuid: null`` — a brand-new
    root — so every record before it is off-chain *by design*.  Measuring loss
    as ``total - chain`` therefore calls a healthy, heavily-compacted session a
    near-total loss.  Measured on the real files: it reported 112,022 records
    (99.7%) lost on a session that had abandoned 1,581, and would have raised a
    318,916-record alarm on a session that was fine.
    """
    p = tmp_path / "s.jsonl"
    lines = [_rec(_u(i), _u(i - 1) if i else None) for i in range(500)]
    # compaction: a fresh root, then the conversation continues from it
    lines.append(_rec(_u(900), None, typ="user",
                      ts="2026-08-16T21:37:29.517Z"))
    prev = _u(900)
    for k in range(10):
        lines.append(_rec(_u(901 + k), prev, ts="2026-08-16T21:40:00.000Z"))
        prev = _u(901 + k)
    _write(p, lines)

    r = analyze_session_chain(p)
    assert r["chain"] == 11, "the chain is only the post-compaction segment"
    assert r["stranded"] == 500, "…and that raw number is not damage"
    assert r["abandoned_branches"] == 0
    assert r["abandoned_records"] == 0, "nothing was abandoned; nothing is lost"


def test_a_hole_that_compaction_healed_is_not_reported(tmp_path):
    """SlateOS's shape: damaged in July, compacted since, sound today.

    The live chain starts at the later compaction root and never passes through
    the fork, so the abandoned branches — real, but historical — cost the model
    nothing now.  Warning here would be a false alarm.
    """
    p = tmp_path / "s.jsonl"
    lines = [_rec(_u(i), _u(i - 1) if i else None) for i in range(10)]
    lines.append(b"\x00" * 512 + b"\n")
    n = 100                                    # branches off the old node
    for b in range(4):
        prev = _u(9)
        for k in range(3):
            lines.append(_rec(_u(n), prev, typ="user" if k == 0 else "assistant",
                              ts=f"2026-07-2{4 + b}T10:0{k}:00.000Z"))
            prev = _u(n)
            n += 1
    lines.append(_rec(_u(900), None, typ="user",     # compaction re-roots
                      ts="2026-08-16T21:37:29.517Z"))
    prev = _u(900)
    for k in range(5):
        lines.append(_rec(_u(901 + k), prev, ts="2026-08-16T21:40:00.000Z"))
        prev = _u(901 + k)
    _write(p, lines)

    r = analyze_session_chain(p)
    assert r["corrupt_lines"] == 1, "the hole is still there"
    assert r["abandoned_records"] == 0, "but the chain no longer forks off it"
    assert describe_integrity_problem  # the reporter is simply never reached


def test_the_report_counts_abandoned_work_not_pre_compaction_history(tmp_path):
    r = analyze_session_chain(_forked_session(tmp_path, branches=3, per_branch=4))
    assert r["abandoned_branches"] == 2, "3 children, 1 of them on the chain"
    assert r["abandoned_records"] == 8
    assert r["abandoned_from"] == "2026-08-10T12:00:00.000Z"
    assert r["abandoned_to"] == "2026-08-11T12:03:00.000Z"

    msg = describe_integrity_problem(r)
    assert "2 branches" in msg and "8 messages" in msg
    assert "22" not in msg, "the misleading total must not appear"


def test_abandoned_work_is_counted_apart_from_pre_compaction_history(tmp_path):
    """The two numbers must be able to disagree, or the test proves nothing.

    Every other fixture here has no pre-compaction history, so ``total - chain``
    and the abandoned count come out equal by coincidence and a parser using
    the wrong one still passes.  (Confirmed: swapping the metric back left all
    19 other tests green.)  This session has both — 300 records behind a
    compaction *and* two abandoned branches — so only the correct metric gives
    8 rather than 308.
    """
    p = tmp_path / "s.jsonl"
    lines = [_rec(_u(i), _u(i - 1) if i else None,
                  ts="2026-07-01T09:00:00.000Z") for i in range(300)]
    lines.append(_rec(_u(900), None, typ="user",        # compaction re-roots
                      ts="2026-08-01T09:00:00.000Z"))
    prev = _u(900)
    for k in range(5):
        lines.append(_rec(_u(901 + k), prev, ts="2026-08-01T09:0%d:00.000Z" % k))
        prev = _u(901 + k)
    fork = prev
    n = 950
    for b in range(3):                                  # then it forks
        prev = fork
        for k in range(4):
            lines.append(_rec(_u(n), prev,
                              typ="user" if k == 0 else "assistant",
                              ts=f"2026-08-{10 + b:02d}T12:{k:02d}:00.000Z"))
            prev = _u(n)
            n += 1
    _write(p, lines)

    r = analyze_session_chain(p)
    assert r["total"] == 318
    assert r["chain"] == 10, "compaction root + 5 + the newest branch"
    assert r["stranded"] == 308, "raw off-chain count, mostly pre-compaction"
    assert r["abandoned_records"] == 8, "only the two abandoned branches"
    assert r["abandoned_branches"] == 2
    assert r["stranded"] != r["abandoned_records"], \
        "if these are equal the fixture cannot tell the metrics apart"


def test_an_assistant_record_is_recognised_as_the_leaf(tmp_path):
    """Guards the field-order trap: on a real assistant record the *message*
    object comes first and carries ``"type":"message"``, so a scanner taking the
    first ``"type":"…"`` on the line never sees "assistant" and silently falls
    back to the last *user* record — shortening every chain that ends in an
    assistant turn, which is most of them."""
    p = tmp_path / "s.jsonl"
    lines = []
    for i in range(20):
        lines.append(_rec(_u(i), _u(i - 1) if i else None,
                          typ="user" if i % 2 == 0 else "assistant"))
    _write(p, lines)                       # ends on an assistant record

    r = analyze_session_chain(p)
    assert r["chain"] == 20, "the last assistant turn is the leaf"
    assert r["stranded"] == 0


def test_an_empty_file_does_not_explode(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, [])
    r = analyze_session_chain(p)
    assert r["total"] == 0 and r["chain"] == 0


# --------------------------------------------------------------------------
# The bridge says it once, and doesn't wedge the connect
# --------------------------------------------------------------------------

def _bridge():
    import asyncio  # noqa: F401
    from config import parse_args
    from sdk_bridge import SDKBridge
    from state import init_state_from_config

    cfg = parse_args([])
    st = init_state_from_config(cfg)
    st.session_id = "8d532b0a-a05f-469f-94e4-da7b494255fc"
    sent: list[dict] = []

    async def bcast(msg: dict) -> None:
        sent.append(msg)

    return SDKBridge(config=cfg, state=st, broadcaster=bcast), st, sent


def _errors(sent):
    return [m for m in sent if m.get("subtype") == "error"]


def test_a_broken_session_is_announced_once_not_per_reconnect(monkeypatch):
    """`/model` reconnects, and the condition can't be fixed mid-session — so
    repeating it on every reconnect would just bury the transcript."""
    import asyncio

    import sdk_bridge

    monkeypatch.setattr(sdk_bridge, "check_session_integrity",
                        lambda *a, **k: {"total": 100, "chain": 3, "stranded": 97,
                                         "truncated_at": "2026-07-24T09:32:03.064Z",
                                         "rooted": True, "branches": 27})
    br, st, sent = _bridge()
    asyncio.run(br._report_session_integrity())
    asyncio.run(br._report_session_integrity())

    errs = _errors(sent)
    assert len(errs) == 1
    assert "2026-07-24T09:32:03.064Z" in errs[0]["data"]["message"]


def test_a_healthy_session_says_nothing(monkeypatch):
    import asyncio

    import sdk_bridge

    monkeypatch.setattr(sdk_bridge, "check_session_integrity", lambda *a, **k: None)
    br, st, sent = _bridge()
    asyncio.run(br._report_session_integrity())
    assert sent == []


def test_a_failing_check_never_breaks_the_connect(monkeypatch):
    """It's a diagnostic; it must not be able to take the session down."""
    import asyncio

    import sdk_bridge

    def boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(sdk_bridge, "check_session_integrity", boom)
    br, st, sent = _bridge()
    asyncio.run(br._report_session_integrity())      # must not raise
    assert sent == []


# ---------------------------------------------------------------------------
# The detector must be able to change its mind
# ---------------------------------------------------------------------------
#
# Two defects, both of which made a repair invisible to the warning:
#
#   * a stored *positive* was returned without re-walking, on the reasoning
#     that abandoned work cannot un-abandon itself.  True of the records, false
#     of the verdict — repairing the file is exactly when the finding goes
#     stale, and there was no way to clear it short of deleting the index.
#   * only one ``hole_at`` was ever stored, and the NUL scan resumed from the
#     last scanned offset.  Remove the first hole and the scan skipped straight
#     past the second and third, so a still-damaged file reported clean.
#
# A relink also rewrites one ``parentUuid`` in place, and both uuids are 36
# characters — the file is repaired without changing size by a byte — so size
# alone cannot be the cache key.

import session as session_mod  # noqa: E402
from session import check_session_integrity, find_nul_holes  # noqa: E402


def _isolate(monkeypatch, tmp_path, jsonl):
    monkeypatch.setattr(session_mod, "_INTEGRITY_INDEX_PATH",
                        tmp_path / "_integrity.json", raising=False)
    monkeypatch.setattr(session_mod, "find_session_dir",
                        lambda sid, cd=None: jsonl.parent, raising=False)


def test_every_nul_run_is_found_not_just_the_first(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_bytes(b"aaa" + b"\x00" * 10 + b"bbb" + b"\x00" * 5 + b"ccc"
                  + b"\x00" * 3 + b"ddd")
    assert find_nul_holes(p) == [3, 16, 24]


def test_adjacent_chunks_do_not_split_one_run_into_two(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_bytes(b"a" + b"\x00" * (1 << 23) + b"b")
    assert find_nul_holes(p) == [1]


def test_a_run_at_end_of_file_is_reported(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_bytes(b"abc" + b"\x00" * 8)
    assert find_nul_holes(p) == [3]


def _damaged(tmp_path):
    """A file that really is amnesiac: hole, plus a fork with abandoned work."""
    sid = "11111111-2222-3333-4444-555555555555"
    p = tmp_path / f"{sid}.jsonl"
    rows = [
        _rec("a" * 36, None, typ="user"),
        _rec("b" * 36, "a" * 36),
        # abandoned branch off b
        _rec("c" * 36, "b" * 36, typ="user", ts="2026-07-25T09:00:00.000Z"),
        _rec("d" * 36, "c" * 36, ts="2026-07-25T09:01:00.000Z"),
        # the live chain re-attaches at b
        _rec("e" * 36, "b" * 36, typ="user", ts="2026-07-26T09:00:00.000Z"),
    ]
    p.write_bytes(b"\n".join(rows) + b"\n" + b"\x00" * 64 + b"\n")
    return sid, p


def test_a_positive_finding_is_rechecked_once_the_file_changes(monkeypatch,
                                                               tmp_path):
    sid, p = _damaged(tmp_path)
    _isolate(monkeypatch, tmp_path, p)
    assert check_session_integrity(sid) is not None, "should warn while broken"

    # Repair it exactly as the tools do: drop the NUL run.
    body = p.read_bytes().replace(b"\x00", b"")
    p.write_bytes(body)
    os.utime(p, ns=(0, 10 ** 9))                 # a rewrite moves the mtime

    assert check_session_integrity(sid) is None, \
        "a repaired file must stop warning"


def test_an_in_place_repair_that_keeps_the_size_still_invalidates(monkeypatch,
                                                                 tmp_path):
    """The relink case: one parentUuid swapped, file length identical."""
    sid, p = _damaged(tmp_path)
    _isolate(monkeypatch, tmp_path, p)
    assert check_session_integrity(sid) is not None
    size = p.stat().st_size

    body = p.read_bytes().replace(b"c" * 36, b"e" * 36).replace(b"\x00", b" ")
    assert len(body) == size, "this test is only meaningful at equal size"
    p.write_bytes(body)
    os.utime(p, ns=(0, 10 ** 9))

    assert check_session_integrity(sid) is None


def test_an_untouched_file_is_not_rewalked(monkeypatch, tmp_path):
    """The cache still has to earn its keep on a 1.29 GB file."""
    sid, p = _damaged(tmp_path)
    _isolate(monkeypatch, tmp_path, p)
    assert check_session_integrity(sid) is not None

    calls = []
    monkeypatch.setattr(session_mod, "analyze_session_chain",
                        lambda path: calls.append(path) or {})
    assert check_session_integrity(sid) is not None
    assert calls == [], "unchanged file must be answered from the index"


def test_holes_after_the_first_are_not_skipped_by_the_incremental_scan(
        monkeypatch, tmp_path):
    sid, p = _damaged(tmp_path)
    _isolate(monkeypatch, tmp_path, p)
    check_session_integrity(sid)

    # Grow the file (an append) *and* leave a second hole behind.
    with p.open("ab") as f:
        f.write(b"\x00" * 16 + b"\n")
    report = check_session_integrity(sid)
    assert report is not None
    assert len(report["holes"]) == 2, report["holes"]


def test_several_holes_in_a_single_scan_are_all_recorded(monkeypatch, tmp_path):
    """The SlateOS file had three, and only the first was ever stored.

    An earlier version asserted this by *appending* a second hole, which a
    first-hole-only scanner also passes -- it finds one per scan region.  The
    property that actually failed is several holes found in one pass.
    """
    sid, p = _damaged(tmp_path)
    _isolate(monkeypatch, tmp_path, p)
    body = p.read_bytes()
    p.write_bytes(body[:40] + b"\x00" * 8 + body[40:])   # a second, earlier run

    report = check_session_integrity(sid)
    assert report is not None
    assert len(report["holes"]) == 2, report["holes"]
