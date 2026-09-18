"""Tests for tools/repair_session_jsonl.py.

The tool rewrites session history in place, so the property that matters is not
"it removed the NULs" but "it removed *only* filler": every record that was
readable before must be byte-identical afterwards, in the same order.  These
tests build files with the exact shape observed on the damaged SlateOS session
(NUL run, then a complete 93-byte custom-title record, then normal records).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import repair_session_jsonl as R  # noqa: E402

TITLE = (b'{"type":"custom-title","customTitle":"OS",'
         b'"sessionId":"7baa01e0-32f8-4db9-878e-ea12794ffb4d"}')


def rec(uuid, parent=None, ts="2026-07-24T09:29:45.580Z"):
    p = f'"{parent}"' if parent else "null"
    return (f'{{"parentUuid":{p},"type":"assistant","message":{{"role":"assistant"}},'
            f'"uuid":"{uuid}","timestamp":"{ts}"}}').encode()


def build(tmp_path, lines, name="s.jsonl"):
    p = tmp_path / name
    p.write_bytes(b"".join(l + b"\n" for l in lines))
    return p


# --- scanning ---------------------------------------------------------------

def test_a_clean_file_has_no_holes(tmp_path):
    p = build(tmp_path, [rec("a"), rec("b", "a")])
    assert R.scan(p) == []


def test_the_observed_shape_is_recognised(tmp_path):
    """NUL run, then a complete record, on one line."""
    p = build(tmp_path, [rec("a"), b"\x00" * 500 + TITLE, rec("b", "a")])
    holes = R.scan(p)
    assert len(holes) == 1
    assert holes[0]["nuls"] == 500
    assert holes[0]["kept"] == len(TITLE)
    assert holes[0]["recoverable"] is True
    assert "custom-title" in holes[0]["preview"]


def test_a_truncated_record_is_flagged_unrecoverable(tmp_path):
    """Surviving bytes that aren't valid JSON mean real loss, not filler."""
    p = build(tmp_path, [rec("a"), b"\x00" * 100 + b'{"type":"assis', rec("b", "a")])
    holes = R.scan(p)
    assert holes[0]["recoverable"] is False


def test_every_hole_is_found_not_just_the_first(tmp_path):
    """The bug in the old detector: it only ever recorded one hole_at."""
    p = build(tmp_path, [rec("a"), b"\x00" * 10 + TITLE, rec("b", "a"),
                         b"\x00" * 20 + TITLE, rec("c", "b"),
                         b"\x00" * 30 + TITLE])
    assert len(R.scan(p)) == 3


# --- repairing --------------------------------------------------------------

def test_the_nuls_go_and_the_record_stays(tmp_path):
    p = build(tmp_path, [rec("a"), b"\x00" * 500 + TITLE, rec("b", "a")])
    out = tmp_path / "out.jsonl"
    stats = R.repair(p, out)

    data = out.read_bytes()
    assert b"\x00" not in data
    assert TITLE + b"\n" in data
    assert stats["nuls_removed"] == 500
    assert stats["lines_in"] == stats["lines_out"] == 3


def test_no_surviving_record_is_altered(tmp_path):
    """The whole safety case: only filler bytes may disappear."""
    before = [rec("a"), rec("b", "a"), b"\x00" * 77 + TITLE, rec("c", "b")]
    p = build(tmp_path, before)
    out = tmp_path / "out.jsonl"
    R.repair(p, out)

    got = out.read_bytes().splitlines()
    assert got == [rec("a"), rec("b", "a"), TITLE, rec("c", "b")]


def test_a_line_that_is_only_padding_is_dropped(tmp_path):
    p = build(tmp_path, [rec("a"), b"\x00" * 400, rec("b", "a")])
    out = tmp_path / "out.jsonl"
    stats = R.repair(p, out)
    assert stats["dropped"] == 1
    assert out.read_bytes().splitlines() == [rec("a"), rec("b", "a")]


def test_every_line_still_parses_as_json_afterwards(tmp_path):
    p = build(tmp_path, [rec("a"), b"\x00" * 900 + TITLE, rec("b", "a")])
    out = tmp_path / "out.jsonl"
    R.repair(p, out)
    for line in out.read_bytes().splitlines():
        json.loads(line.decode())


def test_a_huge_nul_run_is_handled(tmp_path):
    """The real file's largest hole was 124,834 bytes on one line."""
    p = build(tmp_path, [rec("a"), b"\x00" * 124834 + TITLE, rec("b", "a")])
    out = tmp_path / "out.jsonl"
    stats = R.repair(p, out)
    assert stats["nuls_removed"] == 124834
    assert b"\x00" not in out.read_bytes()


# --- verification -----------------------------------------------------------

def test_verify_passes_on_a_correct_repair(tmp_path):
    p = build(tmp_path, [rec("a"), b"\x00" * 500 + TITLE, rec("b", "a")])
    out = tmp_path / "out.jsonl"
    R.repair(p, out)
    assert R.verify(p, out) == []


def test_verify_catches_a_dropped_record(tmp_path):
    p = build(tmp_path, [rec("a"), rec("b", "a"), rec("c", "b")])
    out = build(tmp_path, [rec("a"), rec("c", "b")], name="out.jsonl")
    assert R.verify(p, out) != []


def test_verify_catches_leftover_nuls(tmp_path):
    p = build(tmp_path, [rec("a"), b"\x00" * 5 + TITLE])
    out = build(tmp_path, [rec("a"), b"\x00" * 5 + TITLE], name="out.jsonl")
    problems = R.verify(p, out)
    assert any("NUL" in x for x in problems)


def test_verify_catches_a_mangled_record(tmp_path):
    p = build(tmp_path, [rec("a"), rec("b", "a")])
    out = build(tmp_path, [rec("a"), rec("ZZ", "a")], name="out.jsonl")
    assert R.verify(p, out) != []


# --- the CLI wrapper --------------------------------------------------------

def test_a_dry_run_does_not_touch_the_file(tmp_path, monkeypatch, capsys):
    p = build(tmp_path, [rec("a"), b"\x00" * 500 + TITLE])
    before = p.read_bytes()
    monkeypatch.setattr(sys, "argv", ["repair", str(p)])
    assert R.main() == 0
    assert p.read_bytes() == before
    assert "dry run" in capsys.readouterr().out


def test_apply_rewrites_and_backs_up(tmp_path, monkeypatch, capsys):
    p = build(tmp_path, [rec("a"), b"\x00" * 500 + TITLE, rec("b", "a")])
    before = p.read_bytes()
    bdir = tmp_path / "backups"
    monkeypatch.setattr(sys, "argv",
                        ["repair", str(p), "--apply", "--backup-dir", str(bdir)])
    assert R.main() == 0

    assert b"\x00" not in p.read_bytes()
    backup = bdir / (p.name + ".prerepair.bak")
    assert backup.read_bytes() == before, "backup must be the pre-repair bytes"


def test_unrecoverable_loss_is_refused(tmp_path, monkeypatch, capsys):
    """Real data loss must not be silently 'repaired' into a smaller file."""
    p = build(tmp_path, [rec("a"), b"\x00" * 100 + b'{"type":"assis'])
    before = p.read_bytes()
    monkeypatch.setattr(sys, "argv", ["repair", str(p), "--apply"])
    assert R.main() == 1
    assert p.read_bytes() == before
    assert "Refusing to repair" in capsys.readouterr().out


def test_a_clean_file_is_a_no_op(tmp_path, monkeypatch, capsys):
    p = build(tmp_path, [rec("a"), rec("b", "a")])
    before = p.read_bytes()
    monkeypatch.setattr(sys, "argv", ["repair", str(p), "--apply"])
    assert R.main() == 0
    assert p.read_bytes() == before
    assert "nothing to repair" in capsys.readouterr().out


# --- relink -----------------------------------------------------------------
#
# The shape these reproduce is the one measured on the damaged SlateOS session:
# a long-abandoned node (the fork) that a broken reader kept re-attaching to,
# a healthy compaction segment written later, and finally a handful of records
# that landed back on the fork -- newest in the file, so they win the CLI's
# leaf election and the current segment is never read.

def urec(uuid, parent, ts, type_="user", **extra):
    r = {"parentUuid": parent, "isSidechain": False, "type": type_,
         "message": {"role": "user" if type_ == "user" else "assistant",
                     "content": [{"type": "text", "text": "hi"}]},
         "uuid": uuid, "timestamp": ts}
    r.update(extra)
    return json.dumps(r).encode()


def boundary(uuid, ts):
    return json.dumps({
        "parentUuid": None, "logicalParentUuid": "x", "isSidechain": False,
        "type": "system", "subtype": "compact_boundary",
        "content": "Conversation compacted", "uuid": uuid, "timestamp": ts,
    }).encode()


def meta(kind="custom-title"):
    return json.dumps({"type": kind, "customTitle": "OS"}).encode()


def damaged(tmp_path):
    """Old segment + fork, newer healthy segment, stray tail on the fork."""
    return build(tmp_path, [
        boundary("B0", "2026-07-24T09:21:34Z"),
        urec("f0", "B0", "2026-07-24T09:25:00Z"),
        urec("fork", "f0", "2026-07-24T09:29:45Z", "assistant"),
        # an old abandoned branch off the fork -- predates the live segment
        urec("old1", "fork", "2026-08-20T23:40:37Z"),
        urec("old2", "old1", "2026-08-21T00:09:32Z", "assistant"),
        # the healthy, current compaction segment
        boundary("B1", "2026-08-28T14:13:16Z"),
        urec("h1", "B1", "2026-08-28T14:14:00Z"),
        urec("h2", "h1", "2026-08-28T14:16:47Z", "assistant"),
        meta(),
        # ...and the stray tail that landed back on the fork
        urec("s1", "fork", "2026-08-28T15:36:50Z"),
        urec("s2", "s1", "2026-08-28T15:37:06Z", "assistant"),
        meta("last-prompt"),
    ])


def test_the_stale_resume_point_is_diagnosed(tmp_path):
    idx = R.index_records(damaged(tmp_path))
    assert idx.resume_leaf() == "s2"
    # Walking up from s2 reaches the July root, not the current segment.
    assert idx.chain("s2") == ["B0", "f0", "fork", "s1", "s2"]


def test_relink_moves_only_the_stray_tail(tmp_path):
    idx = R.index_records(damaged(tmp_path))
    plan = R.plan_relink(idx)
    assert plan["reason"] is None
    assert plan["boundary"] == "B1"
    assert plan["splice"] == "h2"
    assert [(e["head"], e["old_parent"], e["new_parent"]) for e in plan["edits"]] \
        == [("s1", "fork", "h2")]
    assert plan["stray_records"] == 2, "old1/old2 predate the segment; leave them"


def test_relink_rejoins_the_chain(tmp_path, monkeypatch, capsys):
    p = damaged(tmp_path)
    bdir = tmp_path / "b"
    before = p.read_bytes()
    monkeypatch.setattr(sys, "argv",
                        ["repair", str(p), "--relink", "--apply",
                         "--backup-dir", str(bdir)])
    assert R.main() == 0
    after = R.index_records(p)
    assert after.chain(after.resume_leaf()) == ["B1", "h1", "h2", "s1", "s2"]
    assert (bdir / (p.name + ".prerelink.bak")).read_bytes() == before


def test_relink_edits_exactly_one_field_and_nothing_else(tmp_path, monkeypatch):
    p = damaged(tmp_path)
    before = p.read_bytes().decode().splitlines()
    monkeypatch.setattr(sys, "argv",
                        ["repair", str(p), "--relink", "--apply",
                         "--backup-dir", str(tmp_path / "b")])
    assert R.main() == 0
    after = p.read_bytes().decode().splitlines()
    assert len(before) == len(after)
    differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert len(differing) == 1
    a, b = json.loads(before[differing[0]]), json.loads(after[differing[0]])
    assert a.pop("parentUuid") == "fork"
    assert b.pop("parentUuid") == "h2"
    assert a == b, "no other field may move"
    assert list(json.loads(before[differing[0]])) == list(
        json.loads(after[differing[0]])), "key order preserved"


def test_a_healthy_file_is_left_alone(tmp_path, monkeypatch, capsys):
    p = build(tmp_path, [
        boundary("B1", "2026-08-28T14:13:16Z"),
        urec("h1", "B1", "2026-08-28T14:14:00Z"),
        urec("h2", "h1", "2026-08-28T14:16:47Z", "assistant"),
    ])
    before = p.read_bytes()
    monkeypatch.setattr(sys, "argv", ["repair", str(p), "--relink", "--apply"])
    assert R.main() == 0
    assert p.read_bytes() == before
    assert "already inside the newest" in capsys.readouterr().out


def test_a_never_compacted_file_is_left_alone(tmp_path, monkeypatch, capsys):
    p = build(tmp_path, [urec("a", None, "2026-08-28T14:00:00Z"),
                         urec("b", "a", "2026-08-28T14:01:00Z", "assistant")])
    before = p.read_bytes()
    monkeypatch.setattr(sys, "argv", ["repair", str(p), "--relink", "--apply"])
    assert R.main() == 0
    assert p.read_bytes() == before
    assert "never been compacted" in capsys.readouterr().out


def test_a_stale_branch_older_than_the_segment_is_not_dragged_in(tmp_path,
                                                                 monkeypatch,
                                                                 capsys):
    """The abandoned branches are history, not pending work.

    Splicing thousands of five-week-old records into the live context would
    blow past the compaction threshold and the CLI would immediately compact
    away the current state.  Only records that postdate the segment move.
    """
    p = build(tmp_path, [
        boundary("B0", "2026-07-24T09:21:34Z"),
        urec("fork", "B0", "2026-07-24T09:29:45Z", "assistant"),
        urec("old1", "fork", "2026-08-20T23:40:37Z"),
        boundary("B1", "2026-08-28T14:13:16Z"),
        urec("h1", "B1", "2026-08-28T14:14:00Z"),
        urec("h2", "h1", "2026-08-28T14:16:47Z", "assistant"),
    ])
    before = p.read_bytes()
    monkeypatch.setattr(sys, "argv", ["repair", str(p), "--relink", "--apply"])
    assert R.main() == 0
    assert p.read_bytes() == before
    assert "nothing to relink" in capsys.readouterr().out
    idx = R.index_records(p)
    assert idx.recs["old1"]["parent"] == "fork", "the old branch stays where it is"
    assert idx.chain(idx.resume_leaf()) == ["B1", "h1", "h2"]


def test_a_leaf_tied_with_the_splice_point_is_not_moved(tmp_path):
    """Equal timestamps are not evidence of later work; refuse rather than guess.

    The two tied leaves are named so that file order (``aaa`` first) and sort
    order (``zzz`` first, descending) disagree.  ``findLatestMessage`` scans the
    insertion-ordered message map and only replaces its pick on a strict ``>``,
    so the answer is the *earlier* record -- picking by uuid instead would be
    both wrong and irreproducible.
    """
    p = build(tmp_path, [
        boundary("B0", "2026-07-24T09:21:34Z"),
        urec("fork", "B0", "2026-07-24T09:29:45Z", "assistant"),
        urec("aaa", "fork", "2026-08-28T14:16:47Z", "assistant"),
        boundary("B1", "2026-08-28T14:13:16Z"),
        urec("h1", "B1", "2026-08-28T14:14:00Z"),
        urec("zzz", "h1", "2026-08-28T14:16:47Z", "assistant"),
    ])
    idx = R.index_records(p)
    assert idx.resume_leaf() == "aaa", "ties resolve to the first record in the file"
    plan = R.plan_relink(idx)
    assert plan["splice"] == "zzz"
    assert plan["edits"] == []
    assert "nothing postdates" in plan["reason"]


def test_several_stray_tails_are_chained_in_write_order(tmp_path):
    p = build(tmp_path, [
        boundary("B0", "2026-07-24T09:21:34Z"),
        urec("fork", "B0", "2026-07-24T09:29:45Z", "assistant"),
        boundary("B1", "2026-08-28T14:13:16Z"),
        urec("h1", "B1", "2026-08-28T14:14:00Z"),
        urec("h2", "h1", "2026-08-28T14:16:47Z", "assistant"),
        urec("p1", "fork", "2026-08-28T15:00:00Z"),
        urec("p2", "p1", "2026-08-28T15:05:00Z", "assistant"),
        urec("q1", "fork", "2026-08-28T16:00:00Z"),
        urec("q2", "q1", "2026-08-28T16:05:00Z", "assistant"),
    ])
    idx = R.index_records(p)
    plan = R.plan_relink(idx)
    assert [(e["head"], e["new_parent"]) for e in plan["edits"]] == [
        ("p1", "h2"), ("q1", "p2")]
    out = tmp_path / "out.jsonl"
    R.apply_relink(p, out, idx, plan)
    after = R.index_records(out)
    assert after.chain(after.resume_leaf()) == \
        ["B1", "h1", "h2", "p1", "p2", "q1", "q2"]


def test_an_unanswered_tool_use_at_the_seam_is_warned_about(tmp_path):
    """Resuming onto a dangling tool_use is an API 400, so say so."""
    asst = json.dumps({
        "parentUuid": "h1", "isSidechain": False, "type": "assistant",
        "message": {"role": "assistant",
                    "content": [{"type": "tool_use", "id": "t1",
                                 "name": "Bash", "input": {}}]},
        "uuid": "h2", "timestamp": "2026-08-28T14:16:47Z"}).encode()
    p = build(tmp_path, [
        boundary("B0", "2026-07-24T09:21:34Z"),
        urec("fork", "B0", "2026-07-24T09:29:45Z", "assistant"),
        boundary("B1", "2026-08-28T14:13:16Z"),
        urec("h1", "B1", "2026-08-28T14:14:00Z"),
        asst,
        urec("s1", "fork", "2026-08-28T15:36:50Z"),
    ])
    plan = R.plan_relink(R.index_records(p))
    assert any("unanswered tool_use" in w for w in plan["warnings"])


def test_relink_refuses_when_the_parent_field_is_not_what_was_planned(tmp_path):
    """The byte patcher must never guess: a surprise means abort, not repair."""
    line = urec("s1", "fork", "2026-08-28T15:36:50Z")
    with pytest.raises(ValueError, match="not the planned"):
        R._reparent_line(line + b"\n", "somethingelse", "h2")


def test_a_nested_parentuuid_is_not_the_one_patched(tmp_path):
    """``"parentUuid"`` is not unique in the line, so position alone can't pick it.

    A record embedding *parsed* session records -- a ``toolUseResult`` from
    reading a JSONL, which is exactly the kind of thing this session does --
    carries the key in a nested object, unescaped, ahead of the real field.
    Text inside a string is safe (JSON escapes the quotes), but a nested object
    is not, and editing that one would rewrite the transcript instead of the
    link while leaving the amnesia in place.
    """
    line = json.dumps({
        "toolUseResult": {"records": [{"parentUuid": "fork", "uuid": "x"}]},
        "parentUuid": "fork", "type": "user", "uuid": "s1",
        "timestamp": "2026-08-28T15:36:50Z",
    }).encode()
    assert line.count(b'"parentUuid"') == 2, "the decoy must really be there"

    rec_out = json.loads(R._reparent_line(line + b"\n", "fork", "h2"))
    assert rec_out["parentUuid"] == "h2"
    assert rec_out["toolUseResult"]["records"][0]["parentUuid"] == "fork", \
        "the nested copy must be left exactly as it was"


def test_the_patcher_refuses_rather_than_corrupt_when_no_field_is_safe(tmp_path):
    """If every candidate would change something else, abort."""
    line = json.dumps({"parentUuid": "fork", "type": "user", "uuid": "s1"}).encode()
    original = R._reparent_line
    # A record with the field present but the whole-record check impossible to
    # satisfy is unreachable by construction, so drive the guard directly by
    # asking for a value the record does not hold.
    with pytest.raises(ValueError):
        original(line + b"\n", None, "h2")


def test_relink_leaves_the_original_when_verification_fails(tmp_path,
                                                            monkeypatch, capsys):
    p = damaged(tmp_path)
    before = p.read_bytes()
    monkeypatch.setattr(R, "apply_relink",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    monkeypatch.setattr(sys, "argv",
                        ["repair", str(p), "--relink", "--apply",
                         "--backup-dir", str(tmp_path / "b")])
    assert R.main() == 1
    assert p.read_bytes() == before
    assert "RELINK FAILED" in capsys.readouterr().out


def test_relink_dry_run_changes_nothing(tmp_path, monkeypatch, capsys):
    p = damaged(tmp_path)
    before = p.read_bytes()
    monkeypatch.setattr(sys, "argv", ["repair", str(p), "--relink"])
    assert R.main() == 0
    assert p.read_bytes() == before
    assert "dry run" in capsys.readouterr().out


def test_meta_records_without_a_uuid_survive_the_rewrite(tmp_path, monkeypatch):
    """queue-operation/custom-title lines have no uuid and must pass through."""
    p = damaged(tmp_path)
    monkeypatch.setattr(sys, "argv",
                        ["repair", str(p), "--relink", "--apply",
                         "--backup-dir", str(tmp_path / "b")])
    assert R.main() == 0
    kinds = [json.loads(l)["type"] for l in p.read_text().splitlines()]
    assert kinds.count("custom-title") == 1
    assert kinds.count("last-prompt") == 1
