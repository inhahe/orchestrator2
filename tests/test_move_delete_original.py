"""``/move`` can delete the original, and marks it when it does not.

Asked for 2026-10-23: "should we give the /move feature the option to delete
the old conversation after moving?" -- and, built with it, a mark on the
original left in the session list, which otherwise looks just as it did.

Deleting is only safe once the copy stands on its own, and a session is more
than its transcript: beside it the CLI keeps a folder (subagent transcripts,
tool output too large to keep inline -- which the transcript points into) and
a ``file-history`` folder.  So the move copies all of it, and points the
copy's transcripts at the copy's folder.  Then the original goes to the
Recycle Bin -- once the copy's CLI has resumed it, and only if nothing has
opened the original again and the copy holds all its records.  Otherwise it
is kept, marked as moved, and the user is told why.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import copy_session                                       # noqa: E402
import proc_guard                                         # noqa: E402
import send2trash                                         # noqa: E402
import server                                             # noqa: E402
import session                                            # noqa: E402
from copy_session import copy_whole_session, count_records  # noqa: E402
from session import load_moved_record, save_moved_record, trash_session  # noqa: E402
from test_move_stops_original import SID, move            # noqa: E402,F401

OLD = "aaaaaaaa-0000-0000-0000-000000000001"
NEW = "bbbbbbbb-0000-0000-0000-000000000002"
CWD = "D:\\work"


def _lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


@pytest.fixture
def sess(tmp_path):
    """A session with everything the CLI keeps of one, in account acct-a."""
    src_cfg, dst_cfg = tmp_path / "acct-a", tmp_path / "acct-b"
    jsonl = src_cfg / "projects" / "D--work" / f"{OLD}.jsonl"
    folder = jsonl.with_suffix("")
    saved = folder / "tool-results" / "b1.txt"
    _write(jsonl, [
        {"type": "user", "sessionId": OLD, "cwd": CWD,
         "message": {"role": "user", "content": "hi"}},
        {"type": "user", "sessionId": OLD, "cwd": CWD,
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1",
              "content": f"Output too large. Saved to {saved}\n\nPreview: ..."}]},
         "toolUseResult": {"persistedOutputPath": str(saved)}},
        {"type": "user", "sessionId": OLD, "isCompactSummary": True,
         "message": {"role": "user",
                     "content": f"read the full transcript at: {jsonl}"}},
        {"type": "assistant", "sessionId": OLD, "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": f"I read {CWD}\\main.rs"}]}},
    ])
    saved.parent.mkdir(parents=True)
    saved.write_text("the whole output", encoding="utf-8")
    _write(folder / "subagents" / "agent-a1.jsonl", [
        {"type": "user", "sessionId": OLD, "agentId": "a1", "cwd": CWD,
         "isSidechain": True,
         "message": {"role": "user", "content": f"see {saved}"}}])
    (folder / "subagents" / "agent-a1.meta.json").write_text(
        '{"agentType":"general-purpose"}', encoding="utf-8")
    (folder / "custom-title.json").write_text(
        '{"customTitle":"the original"}', encoding="utf-8")
    hist = src_cfg / "file-history" / OLD
    hist.mkdir(parents=True)
    (hist / "abc@v1").write_text("before the edit", encoding="utf-8")
    dest = dst_cfg / "projects" / "D--elsewhere" / f"{NEW}.jsonl"
    return {"src_cfg": src_cfg, "dst_cfg": dst_cfg, "jsonl": jsonl,
            "folder": folder, "saved": saved, "dest": dest,
            "new_folder": dest.with_suffix("")}


def _copy(s, **kw):
    copy_whole_session(s["jsonl"], s["dest"], NEW, src_cfg=s["src_cfg"],
                       dest_cfg=s["dst_cfg"], **kw)


# --------------------------------------------------------------------------
# The copy stands on its own
# --------------------------------------------------------------------------

def test_the_sessions_folder_comes_along(sess):
    _copy(sess)

    nf = sess["new_folder"]
    assert (nf / "tool-results" / "b1.txt").read_text(encoding="utf-8") == "the whole output"
    assert (nf / "subagents" / "agent-a1.meta.json").exists()
    assert (nf / "subagents" / "agent-a1.jsonl").exists()


def test_a_tools_saved_output_is_copied_as_it_is(sess):
    """JSON lines a command printed, saved by the CLI: the tool's data, not
    a transcript, and not to be rewritten."""
    out = sess["folder"] / "tool-results" / "b2.jsonl"
    raw = b'{"sessionId": "' + OLD.encode() + b'", "cwd": "/tool/own"}\r\nnot json\n'
    out.write_bytes(raw)
    _copy(sess, new_cwd="E:\\moved")

    assert (sess["new_folder"] / "tool-results" / "b2.jsonl").read_bytes() == raw


def test_but_not_the_originals_name(sess):
    """The CLI keeps the name there, and would stamp it back on the copy."""
    _copy(sess)

    assert not (sess["new_folder"] / "custom-title.json").exists()


def test_its_rewind_backups_come_along(sess):
    _copy(sess)

    assert (sess["dst_cfg"] / "file-history" / NEW / "abc@v1").read_text(
        encoding="utf-8") == "before the edit"


def test_its_subagents_transcripts_are_rewritten_like_its_own(sess):
    _copy(sess, new_cwd="E:\\moved")

    [rec] = _lines(sess["new_folder"] / "subagents" / "agent-a1.jsonl")
    assert rec["sessionId"] == NEW and rec["cwd"] == "E:\\moved"
    assert rec["agentId"] == "a1"


def test_paths_into_the_originals_folder_point_into_the_copys(sess):
    """Tool output kept outside the transcript: the model reads it from where
    the transcript says, which must still be there when the original is not."""
    _copy(sess)

    new_saved = str(sess["new_folder"] / "tool-results" / "b1.txt")
    recs = _lines(sess["dest"])
    assert recs[1]["message"]["content"][0]["content"].startswith(
        f"Output too large. Saved to {new_saved}\n")
    assert recs[1]["toolUseResult"]["persistedOutputPath"] == new_saved
    [sub] = _lines(sess["new_folder"] / "subagents" / "agent-a1.jsonl")
    assert sub["message"]["content"] == f"see {new_saved}"


def test_and_so_does_a_summarys_pointer_to_the_transcript(sess):
    _copy(sess)

    assert _lines(sess["dest"])[2]["message"]["content"] == \
        f"read the full transcript at: {sess['dest']}"


def test_paths_anywhere_else_are_left_as_they_happened(sess):
    _copy(sess)

    assert _lines(sess["dest"])[3]["message"]["content"][0]["text"] == f"I read {CWD}\\main.rs"


def test_a_path_written_with_the_other_separator_is_found_too(sess):
    other = str(sess["saved"]).replace("\\", "/")
    _write(sess["jsonl"], [{"type": "user", "sessionId": OLD,
                            "message": {"role": "user", "content": other}}])
    _copy(sess)

    got = _lines(sess["dest"])[0]["message"]["content"]
    assert got == str(sess["new_folder"] / "tool-results" / "b1.txt").replace("\\", "/")


@pytest.mark.skipif(os.name != "nt", reason="Windows paths ignore case")
def test_and_one_in_another_case(sess):
    """The account's folder, as the CLI was given it; the id it writes as is."""
    acct = str(sess["src_cfg"])
    shouted = acct.upper() + str(sess["saved"])[len(acct):]
    _write(sess["jsonl"], [{"type": "user", "sessionId": OLD, "message": {
        "role": "user", "content": shouted}}])
    _copy(sess)

    got = _lines(sess["dest"])[0]["message"]["content"]
    assert got.startswith(str(sess["new_folder"])), got


def test_a_path_inside_printed_json_stays_escaped(sess):
    """JSON a tool printed doubles its backslashes; a replacement with single
    ones would leave it unreadable as JSON."""
    printed = json.dumps({"file": str(sess["saved"])})
    _write(sess["jsonl"], [{"type": "user", "sessionId": OLD,
                            "message": {"role": "user", "content": printed}}])
    _copy(sess)

    got = _lines(sess["dest"])[0]["message"]["content"]
    assert json.loads(got) == {"file": str(sess["new_folder"] / "tool-results" / "b1.txt")}


def test_a_failed_copy_leaves_nothing_behind(sess, monkeypatch):
    """Half a copy is not one -- and must not sit in the list as a session."""
    def broken(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(shutil, "copytree", broken)

    with pytest.raises(OSError, match="disk full"):
        _copy(sess)

    assert not sess["dest"].exists()
    assert not sess["new_folder"].exists()
    assert not sess["dest"].with_name(sess["dest"].name + ".tmp").exists()
    assert not (sess["dst_cfg"] / "file-history" / NEW).exists()


def test_a_transcript_copy_that_fails_leaves_no_temporary_file(sess, monkeypatch):
    real_dumps = json.dumps
    calls = {"n": 0}

    def dumps_then_fail(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_dumps(*a, **k)
    monkeypatch.setattr(copy_session.json, "dumps", dumps_then_fail)

    with pytest.raises(OSError):
        copy_session.copy_session_file(sess["jsonl"], sess["dest"], NEW)

    assert list(sess["dest"].parent.iterdir()) == []


def test_records_are_its_non_blank_lines(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text('{"a":1}\n\n{"b":2}\n  \n{"c":3}', encoding="utf-8")
    assert count_records(f) == 3


# --------------------------------------------------------------------------
# Deleting a session
# --------------------------------------------------------------------------

@pytest.fixture
def recycle_bin(monkeypatch, tmp_path):
    """Where send2trash sends things: a folder of this test's, not the user's
    Recycle Bin."""
    bin_dir = tmp_path / "recycle-bin"
    bin_dir.mkdir()
    binned: list[str] = []

    def to_bin(path):
        p = Path(path)
        binned.append(str(p))
        shutil.move(str(p), str(bin_dir / f"{len(binned)}-{p.name}"))

    monkeypatch.setattr(send2trash, "send2trash", to_bin)
    return binned


def _hub_records(cfg, sid):
    return sorted(p.relative_to(session._orch2_state_dir(str(cfg))).as_posix()
                  for p in session._orch2_state_dir(str(cfg)).glob(f"*/*{sid}*.json"))


def test_a_session_goes_to_the_recycle_bin_whole(sess, recycle_bin):
    cfg = str(sess["src_cfg"])
    session.save_session_cost(OLD, {"key_spent": 1.0}, cfg)
    save_moved_record(OLD, {"session_id": NEW, "account": "x"}, cfg)
    env = sess["src_cfg"] / "session-env" / OLD
    env.mkdir(parents=True)

    records = sorted(str(p) for p in session._orch2_state_dir(cfg).glob(f"*/*{OLD}*.json"))

    left = trash_session(sess["jsonl"], cfg)

    assert left == []
    # The hub's records too, so a session restored from the bin keeps what
    # it cost, how it compacts and where it was moved.
    assert recycle_bin[:3] == [str(sess["jsonl"]), str(sess["folder"]),
                               str(sess["src_cfg"] / "file-history" / OLD)]
    assert sorted(recycle_bin[3:]) == records and len(records) == 2
    assert not env.exists()
    assert _hub_records(cfg, OLD) == []


def test_another_sessions_files_are_not_touched(sess, recycle_bin):
    cfg = str(sess["src_cfg"])
    other = "cccccccc-0000-0000-0000-000000000003"
    _write(sess["jsonl"].with_name(f"{other}.jsonl"), [{"sessionId": other}])
    session.save_session_cost(other, {"key_spent": 1.0}, cfg)

    trash_session(sess["jsonl"], cfg)

    assert sess["jsonl"].with_name(f"{other}.jsonl").exists()
    assert _hub_records(cfg, other) == [f"costs/{other}.json"]


def test_a_transcript_that_will_not_go_keeps_everything(sess, monkeypatch):
    def refuse(path):
        raise OSError("in use")
    monkeypatch.setattr(send2trash, "send2trash", refuse)

    with pytest.raises(OSError):
        trash_session(sess["jsonl"], str(sess["src_cfg"]))

    assert sess["folder"].exists()
    assert (sess["src_cfg"] / "file-history" / OLD).exists()


def test_what_will_not_go_is_said(sess, monkeypatch):
    def only_the_transcript(path):
        if str(path) != str(sess["jsonl"]):
            raise OSError("in use")
        Path(path).unlink()
    monkeypatch.setattr(send2trash, "send2trash", only_the_transcript)

    left = trash_session(sess["jsonl"], str(sess["src_cfg"]))

    assert len(left) == 2 and all("in use" in x for x in left)


def test_a_copy_nobody_has_seen_is_deleted_whole(sess, recycle_bin):
    """What a move discards when its copy will not start: everything, and
    none of it to the Recycle Bin, where it would only puzzle."""
    cfg = str(sess["src_cfg"])
    session.save_session_cost(OLD, {"key_spent": 1.0}, cfg)
    env = sess["src_cfg"] / "session-env" / OLD
    env.mkdir(parents=True)

    left = session.discard_session(sess["jsonl"], cfg)

    assert left == [] and recycle_bin == []
    for path in (sess["jsonl"], sess["folder"], sess["src_cfg"] / "file-history" / OLD, env):
        assert not path.exists(), f"{path} is still there"
    assert _hub_records(cfg, OLD) == []


# --------------------------------------------------------------------------
# The move
# --------------------------------------------------------------------------

@pytest.fixture
def mv(move, monkeypatch, recycle_bin):
    """``move`` (test_move_stops_original), its copies registered with the
    hub and up, unless a test says otherwise; and a run that waits for the
    deletion as well as the move."""
    inner = server._create_runtime
    copy_is = {"up": True, "starting": False}
    copies = {}

    async def create(**kw):
        rt = await inner(**kw)
        rt.told = []

        async def tell(msg, **_k):
            rt.told.append(msg)
        rt.broadcast = tell
        rt.bridge.client = object() if copy_is["up"] else None
        rt.state.connecting = copy_is["starting"]
        server.runtimes[rt.rid] = rt
        copies[rt.rid] = rt
        return rt

    monkeypatch.setattr(server, "_create_runtime", create)
    monkeypatch.setattr(server, "_MOVE_TRASH_POLL_S", 0.01)
    # Short, so a copy wrongly waited for fails a test rather than holding
    # it for the real fifteen minutes.
    monkeypatch.setattr(server, "_MOVE_TRASH_WAIT_S", 2.0)
    monkeypatch.setattr(proc_guard, "find_foreign_claude_for_session",
                        lambda sid, strict=False: [])

    def run(**extra):
        async def go():
            await server._do_move(move["mover"], {
                "config_dir": move["dst_cfg"], "new_name": "the copy", "cwd": "",
                **extra})
            await asyncio.gather(*server._move_trash_tasks)
        _drive(go())

    src_jsonl = next(Path(move["src_cfg"]).glob(f"projects/*/{SID}.jsonl"))
    return {**move, "run": run, "copy_is": copy_is, "copies": copies,
            "binned": recycle_bin, "src_jsonl": src_jsonl}


def _drive(coro) -> None:
    """Run *coro*, failing rather than hanging if it never finishes."""
    asyncio.run(asyncio.wait_for(coro, 10))


def _told(m) -> str:
    rt = m["copies"].get("copy")
    return " ".join((x.get("data") or {}).get("message", "") for x in (rt.told if rt else []))


def _said(m) -> str:
    return " ".join((x.get("data") or {}).get("message", "")
                    for x in m["sent"]["mover"] if x.get("type") == "system_msg")


def test_asked_to_the_original_goes_to_the_recycle_bin(mv):
    mv["run"](delete_original=True)

    assert str(mv["src_jsonl"]) in mv["binned"]
    assert not mv["src_jsonl"].exists()
    assert "went to the Recycle Bin" in _told(mv)
    assert "goes to the Recycle Bin once this copy is up" in _said(mv)


def test_and_leaves_no_mark_behind(mv):
    mv["run"](delete_original=True)

    assert load_moved_record(SID, mv["src_cfg"]) is None


def test_restored_from_the_bin_it_is_not_deleted_again(mv, monkeypatch):
    """The move's record goes to the Recycle Bin with the original, and comes
    back with it when the user restores it: still saying to delete it, the
    next start would."""
    binned_records = []
    to_bin = send2trash.send2trash      # the recycle_bin fixture's

    def reading_records(path):
        p = Path(path)
        if p.parent.name == "moved":
            binned_records.append(json.loads(p.read_text(encoding="utf-8")))
        to_bin(path)
    monkeypatch.setattr(send2trash, "send2trash", reading_records)

    mv["run"](delete_original=True)

    assert len(binned_records) == 1, mv["binned"]
    assert not binned_records[0].get("delete"), binned_records[0]


def test_the_deletion_is_recorded_before_it_starts(mv, monkeypatch):
    """The hub stopping while the deletion waits for the copy must not forget
    it: the record the move saves says the original is to go, for the next
    start to finish."""
    async def stopped_first(*_a, **_k):
        pass
    monkeypatch.setattr(server, "_trash_moved_original", stopped_first)

    mv["run"](delete_original=True)

    rec = load_moved_record(SID, mv["src_cfg"])
    assert rec is not None and rec["delete"]
    assert rec["deleter"] == server._this_hub(), "no hub named as doing it"
    assert mv["src_jsonl"].exists()


def test_files_left_behind_are_a_warning(mv, monkeypatch):
    """The original went, but some of what came with it would not: the
    user's to clear up, so said as a refusal is, not as news."""
    def only_the_transcript(path):
        if str(path) != str(mv["src_jsonl"]):
            raise OSError("in use")
        Path(path).unlink()
    monkeypatch.setattr(send2trash, "send2trash", only_the_transcript)

    mv["run"](delete_original=True)

    told = [m for m in mv["copies"]["copy"].told
            if "could not be removed" in (m.get("data") or {}).get("message", "")]
    assert told, _told(mv)
    assert told[0]["subtype"] == "warning", told[0]


def test_not_asked_it_is_kept_and_marked(mv):
    mv["run"]()

    assert mv["src_jsonl"].exists() and mv["binned"] == []
    rec = load_moved_record(SID, mv["src_cfg"])
    to = rec["to"]
    assert to["title"] == "the copy" and to["account"] == mv["dst_cfg"]
    assert to["session_id"] == next(kw["resume"] for kw in mv["created"])
    assert not rec["delete"], "a restart would delete what was to be kept"
    assert "marks it as moved" in _said(mv)


def test_only_a_real_yes_deletes(mv):
    """A client sending something else -- "false" as a string, say -- has not
    asked for a deletion."""
    mv["run"](delete_original="false")

    assert mv["src_jsonl"].exists() and mv["binned"] == []


def test_a_copy_that_comes_up_late_is_waited_for(mv):
    mv["copy_is"].update(up=False, starting=True)

    async def comes_up():
        while "copy" not in mv["copies"]:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        assert mv["binned"] == [], "deleted before the copy was up"
        rt = mv["copies"]["copy"]
        rt.bridge.client, rt.state.connecting = object(), False

    async def go():
        up = asyncio.create_task(comes_up())
        await server._do_move(mv["mover"], {
            "config_dir": mv["dst_cfg"], "new_name": "the copy", "cwd": "",
            "delete_original": True})
        await up
        await asyncio.gather(*server._move_trash_tasks)
    _drive(go())

    assert str(mv["src_jsonl"]) in mv["binned"]


def _kept(mv, why):
    assert mv["src_jsonl"].exists() and mv["binned"] == []
    rec = load_moved_record(SID, mv["src_cfg"])
    assert rec is not None, "not marked"
    assert not rec["delete"], "a restart would try the deletion again"
    told = _told(mv)
    assert "The original session was kept" in told and why in told, told


def test_a_copy_that_never_comes_up_keeps_the_original(mv, monkeypatch):
    monkeypatch.setattr(server, "_MOVE_TRASH_WAIT_S", 0.05)
    mv["copy_is"].update(up=False, starting=True)

    mv["run"](delete_original=True)

    _kept(mv, "had not started")


def test_a_copy_that_gave_up_keeps_the_original(mv):
    mv["copy_is"].update(up=False, starting=False)

    mv["run"](delete_original=True)

    _kept(mv, "did not start")


def test_a_copy_closed_before_it_came_up_keeps_the_original(mv):
    mv["copy_is"].update(up=False, starting=True)

    async def close_it():
        while "copy" not in mv["copies"]:
            await asyncio.sleep(0.01)
        server.runtimes.pop("copy")

    async def go():
        closing = asyncio.create_task(close_it())
        await server._do_move(mv["mover"], {
            "config_dir": mv["dst_cfg"], "new_name": "the copy", "cwd": "",
            "delete_original": True})
        await closing
        await asyncio.gather(*server._move_trash_tasks)
    _drive(go())

    _kept(mv, "closed before it had started")


def test_an_original_opened_again_is_kept(mv, monkeypatch):
    real = server._live_runtime_for
    monkeypatch.setattr(server, "_live_runtime_for",
                        lambda sid: object() if sid == SID else real(sid))

    mv["run"](delete_original=True)

    _kept(mv, "opened again")


def test_an_original_another_process_has_open_is_kept(mv, monkeypatch):
    monkeypatch.setattr(proc_guard, "find_foreign_claude_for_session",
                        lambda sid, strict=False: ["a claude"] if sid == SID else [])

    mv["run"](delete_original=True)

    _kept(mv, "another Claude process")


def test_an_original_no_scan_could_check_is_kept(mv, monkeypatch):
    """"Could not look" is not "nobody has it" -- not before a deletion."""
    def cannot_look(sid, strict=False):
        if strict:
            raise proc_guard.ForeignScanFailed("psutil is not installed")
        return []
    monkeypatch.setattr(proc_guard, "find_foreign_claude_for_session", cannot_look)

    mv["run"](delete_original=True)

    _kept(mv, "could not be checked")


def test_a_copy_short_of_records_keeps_the_original(mv, monkeypatch):
    real = copy_session.count_records
    monkeypatch.setattr(copy_session, "count_records",
                        lambda p: 0 if p != mv["src_jsonl"] else real(p))

    mv["run"](delete_original=True)

    _kept(mv, "the copy holds 0 records")


def test_a_recycle_bin_that_refuses_keeps_the_original(mv, monkeypatch):
    def refuse(path):
        raise OSError("access denied")
    monkeypatch.setattr(send2trash, "send2trash", refuse)

    mv["run"](delete_original=True)

    _kept(mv, "access denied")


# --------------------------------------------------------------------------
# The session list
# --------------------------------------------------------------------------

@pytest.fixture
def listing(monkeypatch, tmp_path):
    """Two accounts and an original in a, moved to a copy in b."""
    monkeypatch.setattr(session, "_title_index", {})
    monkeypatch.setattr(session, "_TITLE_INDEX_PATH", tmp_path / "titles.json")
    monkeypatch.setattr(session, "_rename_pins", {})
    monkeypatch.setattr(session, "_RENAME_PIN_PATH", tmp_path / "renames.json")
    a, b = tmp_path / "acct-a", tmp_path / "acct-b"
    for cfg, sid, title in ((a, OLD, "the original"), (b, NEW, "the copy, renamed")):
        _write(cfg / "projects" / "D--work" / f"{sid}.jsonl", [
            {"type": "user", "sessionId": sid, "cwd": CWD,
             "message": {"role": "user", "content": "hi"}},
            {"type": "custom-title", "customTitle": title, "sessionId": sid}])
    save_moved_record(OLD, {"session_id": NEW, "title": "the copy",
                            "cwd": CWD, "account": str(b)}, str(a))
    monkeypatch.setattr(copy_session, "discover_claude_dirs", lambda: [a, b])
    monkeypatch.setattr(server, "runtimes", {})
    monkeypatch.setattr(server, "_disk_sessions_cache", None)

    def listed():
        server._forget_disk_sessions()
        return {s["session_id"]: s for s in server._recent_disk_sessions()}
    return {"a": a, "b": b, "listed": listed}


def test_a_moved_original_points_at_its_copy(listing):
    got = listing["listed"]()

    assert got[OLD]["moved_to"] == {"session_id": NEW, "title": "the copy, renamed",
                                    "cwd": CWD, "account": str(listing["b"])}
    assert not got[NEW].get("moved_to")


def test_a_running_copy_is_named_as_it_is_now(listing, monkeypatch):
    """A running copy is not in the list's on-disk half, and its title may be
    newer than any the disk has."""
    class _Rt:
        class state:
            session_id = NEW
            session_title = "renamed while running"
    monkeypatch.setattr(server, "runtimes", {"copy": _Rt()})

    assert listing["listed"]()[OLD]["moved_to"]["title"] == "renamed while running"


def test_a_copy_that_is_gone_is_not_pointed_at(listing):
    (listing["b"] / "projects" / "D--work" / f"{NEW}.jsonl").unlink()

    assert not listing["listed"]()[OLD].get("moved_to")


def test_an_original_worked_in_since_is_its_own_session_again(listing):
    """Opened from "open the original" and carried on in: clicking it must
    not then lead to the copy, whose history stops at the move.  A turn of
    ours run in it is what says so."""
    from config import parse_args
    from sdk_bridge import SDKBridge
    from state import init_state_from_config

    cfg = parse_args(["--config-dir", str(listing["a"])])
    st = init_state_from_config(cfg)
    st.session_id = OLD

    async def bcast(_m):
        pass
    br = SDKBridge(config=cfg, state=st, broadcaster=bcast)

    class Client:
        async def query(self, prompt):
            br.turn_msg_queue.put_nowait(_turn_result())

        async def interrupt(self):  # pragma: no cover
            pass
    br.client = Client()

    asyncio.run(br.run_turn("carry on here"))

    assert not listing["listed"]()[OLD].get("moved_to")


def _turn_result():
    from claude_agent_sdk import ResultMessage
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                         is_error=False, num_turns=1, session_id=OLD)


def test_but_not_by_its_file_being_touched(listing):
    """Opening it to look writes to it, and so does a backup tool: neither is
    working in it."""
    original = listing["a"] / "projects" / "D--work" / f"{OLD}.jsonl"
    later = time.time() + 60
    os.utime(original, (later, later))

    assert listing["listed"]()[OLD]["moved_to"]["session_id"] == NEW


def test_a_copy_where_the_scan_does_not_look_is_found_where_it_is(listing, monkeypatch):
    """An account the session list does not cover -- a CLAUDE_CONFIG_DIR of
    its own -- still holds the copy."""
    monkeypatch.setattr(copy_session, "discover_claude_dirs", lambda: [listing["a"]])

    assert listing["listed"]()[OLD]["moved_to"]["session_id"] == NEW


# --------------------------------------------------------------------------
# A move that ends badly
# --------------------------------------------------------------------------

def test_a_copy_that_will_not_start_is_not_left_behind(move, monkeypatch):
    """The original is put back; the copy, which nobody has seen, would be a
    second session under the move's name."""
    move["fail"]["copy_runtime"] = True

    move["run"]()

    dst = Path(move["dst_cfg"])
    assert not list(dst.glob("projects/*/*.jsonl")), "the copy's transcript is still there"
    assert not list(dst.glob("orchestrator2/*/*.json")), "the hub's records of it too"


def test_a_session_being_deleted_cannot_be_opened(monkeypatch):
    monkeypatch.setattr(server, "_trashing_sids", {"gone-1"})

    with pytest.raises(RuntimeError, match="Recycle Bin"):
        asyncio.run(server._create_runtime(resume="gone-1"))


def test_nor_as_the_most_recent_session_in_its_directory(monkeypatch, tmp_path):
    """Opening a directory continues its most recent session -- which, while
    a move deletes it, is still on disk to be found."""
    from config import parse_args
    monkeypatch.setattr(server, "_trashing_sids", {"gone-1"})
    monkeypatch.setattr(server, "config", parse_args([]))
    monkeypatch.setattr(server, "_session_to_open", lambda *a, **k: "gone-1")

    with pytest.raises(RuntimeError, match="Recycle Bin"):
        asyncio.run(server._create_runtime(cwd=str(tmp_path)))


def test_a_deletion_cut_short_by_a_restart_is_finished(monkeypatch, tmp_path, recycle_bin):
    a, b = tmp_path / "acct-a", tmp_path / "acct-b"
    for cfg, sid in ((a, OLD), (b, NEW)):
        _write(cfg / "projects" / "D--work" / f"{sid}.jsonl",
               [{"type": "user", "sessionId": sid, "cwd": CWD}])
    save_moved_record(OLD, {"session_id": NEW, "account": str(b)}, str(a), delete=True)
    monkeypatch.setattr(copy_session, "discover_claude_dirs", lambda: [a, b])
    monkeypatch.setattr(server, "runtimes", {})
    monkeypatch.setattr(proc_guard, "find_foreign_claude_for_session",
                        lambda sid, strict=False: [])

    asyncio.run(server._finish_interrupted_deletions())

    assert str(a / "projects" / "D--work" / f"{OLD}.jsonl") in recycle_bin


def _cut_short(tmp_path, monkeypatch, deleter):
    """An original whose move's deletion *deleter* was doing; returns the
    original's transcript."""
    a, b = tmp_path / "acct-a", tmp_path / "acct-b"
    for cfg, sid in ((a, OLD), (b, NEW)):
        _write(cfg / "projects" / "D--work" / f"{sid}.jsonl",
               [{"type": "user", "sessionId": sid, "cwd": CWD}])
    save_moved_record(OLD, {"session_id": NEW, "account": str(b)}, str(a),
                      delete=True, deleter=deleter)
    monkeypatch.setattr(copy_session, "discover_claude_dirs", lambda: [a, b])
    monkeypatch.setattr(server, "runtimes", {})
    monkeypatch.setattr(proc_guard, "find_foreign_claude_for_session",
                        lambda sid, strict=False: [])
    return a / "projects" / "D--work" / f"{OLD}.jsonl"


def test_but_not_one_another_hub_is_doing_still(monkeypatch, tmp_path, recycle_bin):
    """A second hub started while the first waits for its copy: the copy may
    yet fail to start, and the original be wanted."""
    psutil = pytest.importorskip("psutil")
    parent = psutil.Process(os.getppid())
    original = _cut_short(tmp_path, monkeypatch,
                          {"pid": parent.pid, "started": parent.create_time()})

    asyncio.run(server._finish_interrupted_deletions())

    assert recycle_bin == [] and original.exists()
    assert load_moved_record(OLD, str(tmp_path / "acct-a"))["delete"], \
        "the hub doing it would find the record gone"


def test_a_hub_that_has_stopped_is_not_doing_it(monkeypatch, tmp_path, recycle_bin):
    """Its pid gone, or another process's now: the deletion is finished."""
    psutil = pytest.importorskip("psutil")
    parent = psutil.Process(os.getppid())
    for deleter in ({"pid": 2 ** 30, "started": 1.0},
                    {"pid": parent.pid, "started": parent.create_time() - 100}):
        recycle_bin.clear()
        original = _cut_short(tmp_path, monkeypatch, deleter)

        asyncio.run(server._finish_interrupted_deletions())

        assert str(original) in recycle_bin, deleter


def test_the_hub_finishes_them_when_it_starts(monkeypatch):
    """Driven through the hub's own startup, with what else it starts made
    inert: the finishing is only any use if a start runs it."""
    from config import parse_args
    ran = []

    async def finish():
        ran.append("finished")

    async def inert(*_a, **_k):
        pass
    monkeypatch.setattr(server, "_finish_interrupted_deletions", finish)
    for name in ("_status_ticker", "_resurrect_scheduled_wakeups", "_restore_limit_resumes",
                 "_model_cache_loop", "_deferred_bridge_startup", "_broadcast_shutdown"):
        monkeypatch.setattr(server, name, inert)
    monkeypatch.setattr(server, "_recent_disk_sessions", lambda: [])
    monkeypatch.setattr(server, "_attach_queue_persistence", lambda *a, **k: None)
    monkeypatch.setattr(server, "_launch_compaction_to_state", lambda *a, **k: None)
    monkeypatch.setattr(server, "_seed_primary_session", lambda c: (c, None, None))
    monkeypatch.setattr(proc_guard, "install_process_reaper", lambda: None)
    for name in ("config", "state", "theme", "bridge", "_default_runtime", "_ticker_task",
                 "_model_task", "_picker_mode", "_pending_resume_id"):
        monkeypatch.setattr(server, name, getattr(server, name, None))
    monkeypatch.setattr(server, "config", parse_args([]))
    monkeypatch.setattr(server, "bridge", None)
    monkeypatch.setattr(server, "runtimes", {})

    async def go():
        async with server.lifespan(server.app):
            for _ in range(20):
                await asyncio.sleep(0)
    _drive(go())

    assert ran == ["finished"]


def test_but_kept_if_the_copy_falls_short_and_not_tried_again(monkeypatch, tmp_path, recycle_bin):
    a, b = tmp_path / "acct-a", tmp_path / "acct-b"
    _write(a / "projects" / "D--work" / f"{OLD}.jsonl",
           [{"type": "user", "sessionId": OLD}, {"type": "user", "sessionId": OLD}])
    _write(b / "projects" / "D--work" / f"{NEW}.jsonl", [{"type": "user", "sessionId": NEW}])
    save_moved_record(OLD, {"session_id": NEW, "account": str(b)}, str(a), delete=True)
    monkeypatch.setattr(copy_session, "discover_claude_dirs", lambda: [a, b])
    monkeypatch.setattr(server, "runtimes", {})

    asyncio.run(server._finish_interrupted_deletions())

    assert recycle_bin == []
    rec = load_moved_record(OLD, str(a))
    assert rec is not None and not rec["delete"]


def test_the_wait_for_the_copy_holds_for_a_real_bridge_that_is_connecting(monkeypatch):
    """The wait reads state.connecting, which a real bridge sets as it
    starts: a copy still connecting is waited for, not taken for one that
    did not start."""
    import sdk_bridge
    from config import parse_args
    from session_runtime import SessionRuntime
    from state import init_state_from_config

    cfg = parse_args([])
    st = init_state_from_config(cfg)
    rt = SessionRuntime(config=cfg, state=st, rid="copy")

    async def bcast(m):
        pass
    br = sdk_bridge.SDKBridge(config=cfg, state=st, broadcaster=bcast)
    rt.bridge = br
    monkeypatch.setattr(server, "runtimes", {"copy": rt})
    monkeypatch.setattr(server, "_MOVE_TRASH_POLL_S", 0.01)

    async def connect(*a, **k):
        await asyncio.sleep(0.2)
        br.client = object()
        st.connecting = False
    monkeypatch.setattr(br, "connect", connect)

    async def go():
        await br.start()
        why = await asyncio.wait_for(server._wait_for_the_copy(rt), 5)
        br.stop_event.set()
        br._worker_task.cancel()
        return why

    assert asyncio.run(go()) is None
