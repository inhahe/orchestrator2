"""``/move`` moves a session to another account *and/or* another directory.

Reported 2026-09-06:

    "i don't think there's a /command to copy a session to another project
    dir, it should probably be integrated with the command to copy a session
    to another account, so that you can do it to another account AND dir if
    you want"

Copying to another *account* was already there; copying to another
*directory* is the harder half, because a Claude session's directory is not
merely where it runs -- it is encoded in three places that must agree:

1. **the project slug** the JSONL lives under (``<cfg>/projects/<slug>/``),
   which the CLI derives from the cwd, so a copy filed under the old slug is
   invisible to ``--resume`` from the new directory;
2. **the ``cwd`` field on every record**, which ``sniff_session_cwd`` reads to
   decide which directory a session belongs to -- and which the hub's
   "switch cwd to match the session's recorded cwd" step on resume would use
   to drag the session straight back where it came from;
3. **``gitBranch``**, which described a branch in a tree the copy has now left.

So the interesting tests here are not "does it copy" but "does everything that
names the old directory get renamed, and does everything that merely *mentions*
it get left alone".  A transcript is a record of what happened: that a Read
really did read a file under the old path is a fact, not a stale pointer, and
rewriting it would falsify the conversation rather than relocate it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from copy_session import _sanitize_cwd, copy_session_file       # noqa: E402
from session import normalize_path_for_compare                  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

OLD_CWD = r"D:\projects\alpha"
NEW_CWD = r"D:\projects\beta"


def _records(sid: str, cwd: str = OLD_CWD) -> list[dict]:
    """A miniature but representative transcript.

    Deliberately mixed: a record with no ``cwd`` at all (queue-operation
    records really are like this), one whose nested payload mentions the old
    directory, and one already carrying a *different* cwd (what a mid-session
    ``/cwd`` leaves behind).
    """
    return [
        {"type": "queue-operation", "operation": "enqueue", "sessionId": sid},
        {"type": "user", "sessionId": sid, "cwd": cwd, "gitBranch": "main",
         "message": {"role": "user", "content": f"look at {cwd}\\notes.md"}},
        {"type": "assistant", "sessionId": sid, "cwd": cwd, "gitBranch": "main",
         "toolUseResult": {"filePath": f"{cwd}\\notes.md", "content": "hi"}},
        {"type": "user", "sessionId": sid, "cwd": r"D:\projects\detour",
         "gitBranch": "side"},
    ]


def _write_jsonl(path: Path, recs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# copy_session_file — the rewrite itself
# ---------------------------------------------------------------------------

def test_the_copy_lands_in_the_new_directory(tmp_path):
    src = tmp_path / "old" / "s.jsonl"
    _write_jsonl(src, _records("s"))
    dst = tmp_path / "new" / "n.jsonl"
    copy_session_file(src, dst, "n", new_cwd=NEW_CWD, new_branch="")
    cwds = {r["cwd"] for r in _read_jsonl(dst) if "cwd" in r}
    assert cwds == {NEW_CWD}, "records still name the old directory"


def test_every_record_agrees_on_the_directory(tmp_path):
    """Including the one that was already somewhere else.

    A session that used ``/cwd`` mid-flight has records from two directories.
    The copy lands in exactly *one* project dir, so leaving the odd one out
    would put the session half in a directory it does not live in --
    ``sniff_session_cwd`` reads only the first few records, so which one wins
    would come down to record order.
    """
    src = tmp_path / "old" / "s.jsonl"
    _write_jsonl(src, _records("s"))
    dst = tmp_path / "new" / "n.jsonl"
    copy_session_file(src, dst, "n", new_cwd=NEW_CWD)
    recs = _read_jsonl(dst)
    assert any(r.get("cwd") for r in recs)
    assert all(r["cwd"] == NEW_CWD for r in recs if "cwd" in r)


def test_a_record_without_a_cwd_does_not_grow_one(tmp_path):
    """Rewriting is not the same as annotating.  A queue-operation record has
    no cwd because it does not need one."""
    src = tmp_path / "old" / "s.jsonl"
    _write_jsonl(src, _records("s"))
    dst = tmp_path / "new" / "n.jsonl"
    copy_session_file(src, dst, "n", new_cwd=NEW_CWD)
    first = _read_jsonl(dst)[0]
    assert first["type"] == "queue-operation"
    assert "cwd" not in first


def test_the_transcript_is_not_falsified(tmp_path):
    """Paths *inside* the conversation are history, not configuration.

    That the model was asked about ``D:\\projects\\alpha\\notes.md``, and that a
    tool really read that file, remain true after the session is copied
    somewhere else.  Rewriting them would produce a transcript of a
    conversation that never happened.
    """
    src = tmp_path / "old" / "s.jsonl"
    _write_jsonl(src, _records("s"))
    dst = tmp_path / "new" / "n.jsonl"
    copy_session_file(src, dst, "n", new_cwd=NEW_CWD, new_branch="")
    recs = _read_jsonl(dst)
    assert OLD_CWD in recs[1]["message"]["content"]
    assert recs[2]["toolUseResult"]["filePath"].startswith(OLD_CWD)


def test_the_branch_follows_the_directory(tmp_path):
    src = tmp_path / "old" / "s.jsonl"
    _write_jsonl(src, _records("s"))
    dst = tmp_path / "new" / "n.jsonl"
    copy_session_file(src, dst, "n", new_cwd=NEW_CWD, new_branch="develop")
    branches = {r["gitBranch"] for r in _read_jsonl(dst) if "gitBranch" in r}
    assert branches == {"develop"}


def test_a_destination_that_is_not_a_repo_clears_the_branch(tmp_path):
    """``""`` is a real answer, not "leave it alone" -- carrying ``main`` into a
    directory with no git at all would be a claim about a tree that has none."""
    src = tmp_path / "old" / "s.jsonl"
    _write_jsonl(src, _records("s"))
    dst = tmp_path / "new" / "n.jsonl"
    copy_session_file(src, dst, "n", new_cwd=NEW_CWD, new_branch="")
    branches = {r["gitBranch"] for r in _read_jsonl(dst) if "gitBranch" in r}
    assert branches == {""}


def test_staying_put_leaves_the_branch_alone(tmp_path):
    src = tmp_path / "old" / "s.jsonl"
    _write_jsonl(src, _records("s"))
    dst = tmp_path / "new" / "n.jsonl"
    copy_session_file(src, dst, "n")
    branches = {r["gitBranch"] for r in _read_jsonl(dst) if "gitBranch" in r}
    assert branches == {"main", "side"}


def test_the_session_id_is_still_rewritten(tmp_path):
    src = tmp_path / "old" / "s.jsonl"
    _write_jsonl(src, _records("s"))
    dst = tmp_path / "new" / "n.jsonl"
    copy_session_file(src, dst, "n", new_cwd=NEW_CWD)
    assert {r["sessionId"] for r in _read_jsonl(dst)} == {"n"}


def test_a_move_under_the_same_id_still_rewrites(tmp_path):
    """The plain-copy shortcut is keyed on "nothing to rewrite", not on the id.

    Before the directory half existed the shortcut was ``new_id == old_id``,
    which would now silently skip the cwd rewrite for any copy that kept its
    id -- producing a file in the new project dir that still claims the old
    directory.
    """
    src = tmp_path / "old" / "s.jsonl"
    _write_jsonl(src, _records("s"))
    dst = tmp_path / "new" / "s.jsonl"
    copy_session_file(src, dst, "s", new_cwd=NEW_CWD)
    assert {r["cwd"] for r in _read_jsonl(dst) if "cwd" in r} == {NEW_CWD}


def test_a_plain_copy_is_still_byte_identical(tmp_path):
    """No rewrite asked for, no rewrite done -- a re-serialised JSONL would
    churn key order and formatting for nothing."""
    src = tmp_path / "old" / "s.jsonl"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text('{"sessionId":"s",   "cwd":"x"}\n', encoding="utf-8")
    dst = tmp_path / "new" / "s.jsonl"
    copy_session_file(src, dst, "s")
    assert dst.read_bytes() == src.read_bytes()


def test_an_unparseable_line_survives_a_move(tmp_path):
    src = tmp_path / "old" / "s.jsonl"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text('{"sessionId":"s","cwd":"' + OLD_CWD.replace("\\", "\\\\")
                   + '"}\nnot json at all\n', encoding="utf-8")
    dst = tmp_path / "new" / "n.jsonl"
    copy_session_file(src, dst, "n", new_cwd=NEW_CWD)
    text = dst.read_text(encoding="utf-8")
    assert "not json at all" in text


# ---------------------------------------------------------------------------
# _do_move — slug, validation, and where the runtime starts
# ---------------------------------------------------------------------------

class _FakeCfg:
    def __init__(self, cwd, config_dir):
        self.cwd = cwd
        self.config_dir = config_dir


class _FakeState:
    def __init__(self, sid):
        self.session_id = sid
        self.session_title = "original"


class _FakeRt:
    rid = "rt-1"

    def __init__(self, cwd, config_dir, sid):
        self.config = _FakeCfg(cwd, config_dir)
        self.state = _FakeState(sid)


class _Ws:
    pass


def _switch(monkeypatch, tmp_path, *, src_cwd, want_cwd, same_account=True,
            src_slug=None):
    """Run ``_do_move`` against a real on-disk source session.

    Returns ``(errors, created)`` where *created* records the ``_create_runtime``
    call the switch made (or ``None`` if it never got that far).
    """
    import server

    sid = "11111111-1111-1111-1111-111111111111"
    src_cfg = tmp_path / "acct-a"
    dst_cfg = src_cfg if same_account else (tmp_path / "acct-b")
    # *src_slug* overrides the on-disk project-dir name.  Passing one that is
    # NOT ``_sanitize_cwd(src_cwd)`` reproduces a session located by the
    # cwd-sniffing fallback -- the case where "recompute the slug" and "keep
    # the source dir's name" give different answers.
    src_proj = src_cfg / "projects" / (src_slug or _sanitize_cwd(src_cwd))
    _write_jsonl(src_proj / f"{sid}.jsonl", _records(sid, cwd=src_cwd))

    rt = _FakeRt(src_cwd, str(src_cfg), sid)
    ws = _Ws()

    errors: list[str] = []
    created: dict = {}

    async def fake_send_to(_ws, m):
        if m.get("type") == "move_error":
            errors.append(m.get("message", ""))

    async def fake_create_runtime(**kw):
        created.update(kw)
        new_rt = _FakeRt(kw["cwd"], kw["config_dir"], kw["resume"])
        new_rt.state.session_title = None
        return new_rt

    async def fake_attach(_ws, _rt):
        return None

    monkeypatch.setattr(server, "send_to", fake_send_to)
    monkeypatch.setattr(server, "_create_runtime", fake_create_runtime)
    monkeypatch.setattr(server, "_attach_ws", fake_attach)
    monkeypatch.setattr(server, "write_session_title", lambda *a, **k: None)
    monkeypatch.setattr(server, "_ws_runtime", {ws: rt})
    monkeypatch.setattr(
        server, "find_session_dir",
        lambda s, cfg=None: src_proj if s == sid else None)
    # git is not the subject here, and the destination is a bare temp dir.
    monkeypatch.setattr("copy_session.git_branch_for", lambda cwd: "")

    asyncio.run(server._do_move(ws, {
        "config_dir": str(dst_cfg),
        "new_name": "the copy",
        "cwd": want_cwd,
    }))
    return errors, created, dst_cfg


def test_a_move_files_the_copy_under_the_new_slug(monkeypatch, tmp_path):
    """The slug is the only place ``claude --resume`` looks from the new
    directory, so filing the copy under the old one hides it completely."""
    src_cwd = str(tmp_path / "alpha")
    dest = tmp_path / "beta"
    dest.mkdir()
    (tmp_path / "alpha").mkdir()
    errors, created, dst_cfg = _switch(
        monkeypatch, tmp_path, src_cwd=src_cwd, want_cwd=str(dest))
    assert not errors, errors
    expect = dst_cfg / "projects" / _sanitize_cwd(str(dest.resolve()))
    assert list(expect.glob("*.jsonl")), f"nothing filed under {expect}"


def test_a_move_starts_the_runtime_in_the_new_directory(monkeypatch, tmp_path):
    src_cwd = str(tmp_path / "alpha")
    dest = tmp_path / "beta"
    dest.mkdir()
    (tmp_path / "alpha").mkdir()
    errors, created, _ = _switch(
        monkeypatch, tmp_path, src_cwd=src_cwd, want_cwd=str(dest))
    assert not errors, errors
    assert normalize_path_for_compare(created["cwd"]) \
        == normalize_path_for_compare(str(dest.resolve()))


def test_a_move_rewrites_the_copied_records(monkeypatch, tmp_path):
    src_cwd = str(tmp_path / "alpha")
    dest = tmp_path / "beta"
    dest.mkdir()
    (tmp_path / "alpha").mkdir()
    errors, _created, dst_cfg = _switch(
        monkeypatch, tmp_path, src_cwd=src_cwd, want_cwd=str(dest))
    assert not errors, errors
    proj = dst_cfg / "projects" / _sanitize_cwd(str(dest.resolve()))
    copied = next(iter(proj.glob("*.jsonl")))
    cwds = {r["cwd"] for r in _read_jsonl(copied) if "cwd" in r}
    assert cwds == {str(dest.resolve())}


def test_staying_put_keeps_the_source_project_dir(monkeypatch, tmp_path):
    """A session found by the cwd-sniffing fallback can live under a slug that
    no longer matches its cwd.  Recomputing the slug when nothing moved would
    relocate such a session as a side effect of an account switch.

    The odd slug is the whole point: with the project dir named exactly
    ``_sanitize_cwd(cwd)`` the two implementations agree, and the test proves
    nothing.
    """
    src_cwd = str(tmp_path / "alpha")
    (tmp_path / "alpha").mkdir()
    odd = "legacy-slug-from-an-older-layout"
    assert odd != _sanitize_cwd(src_cwd)
    errors, created, dst_cfg = _switch(
        monkeypatch, tmp_path, src_cwd=src_cwd, want_cwd="",
        same_account=False, src_slug=odd)
    assert not errors, errors
    assert list((dst_cfg / "projects" / odd).glob("*.jsonl")), \
        "the copy did not keep the source project dir's name"
    assert not list((dst_cfg / "projects" / _sanitize_cwd(src_cwd))
                    .glob("*.jsonl")), "relocated a session nobody moved"
    assert created["cwd"] == src_cwd


def test_staying_put_does_not_touch_the_recorded_cwd(monkeypatch, tmp_path):
    """An account switch is not a move, so the mid-session ``/cwd`` record must
    survive it -- flattening every cwd on a plain account switch would quietly
    rewrite history nobody asked to change."""
    src_cwd = str(tmp_path / "alpha")
    (tmp_path / "alpha").mkdir()
    errors, _created, dst_cfg = _switch(
        monkeypatch, tmp_path, src_cwd=src_cwd, want_cwd="",
        same_account=False)
    assert not errors, errors
    proj = dst_cfg / "projects" / _sanitize_cwd(src_cwd)
    copied = next(iter(proj.glob("*.jsonl")))
    cwds = {r["cwd"] for r in _read_jsonl(copied) if "cwd" in r}
    assert r"D:\projects\detour" in cwds


def test_the_same_directory_spelled_differently_is_not_a_move(monkeypatch, tmp_path):
    """Trailing slashes and separator style are spelling, not intent.

    The session's recorded cwd is the odd spelling and the *typed* one is
    canonical, because ``Path.resolve()`` already canonicalises whatever the
    operator types -- so a plain ``dest_cwd != cwd`` string comparison only
    ever differs on the side ``resolve()`` does not touch.

    Observable difference: a spurious "move" would rewrite the cwd on every
    record, flattening the mid-session ``/cwd`` one.
    """
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    src_cwd = str(alpha).replace("\\", "/") + "/"     # same dir, odd spelling
    errors, created, dst_cfg = _switch(
        monkeypatch, tmp_path, src_cwd=src_cwd, want_cwd=str(alpha))
    assert not errors, errors
    copied = next(iter((dst_cfg / "projects").rglob("*.jsonl")))
    cwds = {r["cwd"] for r in _read_jsonl(copied) if "cwd" in r}
    assert r"D:\projects\detour" in cwds, \
        "rewrote the records for a move that never happened"


def test_a_move_works_through_the_real_session_lookup(monkeypatch, tmp_path):
    """End-to-end over the real on-disk layout, with nothing stubbed but the
    runtime.

    Every other test here stubs ``find_session_dir`` so it can control the
    source project dir.  This one lets the real one scan
    ``<cfg>/projects/*``, so the fixture's layout is checked against the
    function the server actually calls -- a switch that works only against a
    stub would be a switch that works only in tests.
    """
    import server

    sid = "22222222-2222-2222-2222-222222222222"
    src_cfg = tmp_path / "acct-a"
    dst_cfg = tmp_path / "acct-b"
    alpha, beta = tmp_path / "alpha", tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()
    src_proj = src_cfg / "projects" / _sanitize_cwd(str(alpha))
    _write_jsonl(src_proj / f"{sid}.jsonl", _records(sid, cwd=str(alpha)))

    rt = _FakeRt(str(alpha), str(src_cfg), sid)
    ws = _Ws()
    errors: list[str] = []
    created: dict = {}

    async def fake_send_to(_ws, m):
        if m.get("type") == "move_error":
            errors.append(m.get("message", ""))

    async def fake_create_runtime(**kw):
        created.update(kw)
        r = _FakeRt(kw["cwd"], kw["config_dir"], kw["resume"])
        r.state.session_title = None
        return r

    async def fake_attach(_ws, _rt):
        return None

    monkeypatch.setattr(server, "send_to", fake_send_to)
    monkeypatch.setattr(server, "_create_runtime", fake_create_runtime)
    monkeypatch.setattr(server, "_attach_ws", fake_attach)
    monkeypatch.setattr(server, "_ws_runtime", {ws: rt})
    monkeypatch.setattr("copy_session.git_branch_for", lambda cwd: "")
    # find_session_dir and write_session_title are the real ones.
    asyncio.run(server._do_move(ws, {
        "config_dir": str(dst_cfg), "new_name": "moved", "cwd": str(beta)}))

    assert not errors, errors
    landed = list((dst_cfg / "projects" / _sanitize_cwd(str(beta.resolve())))
                  .glob("*.jsonl"))
    assert landed, "the real lookup did not lead to a filed copy"
    recs = _read_jsonl(landed[0])
    assert {r["cwd"] for r in recs if "cwd" in r} == {str(beta.resolve())}
    assert {r["sessionId"] for r in recs} == {created["resume"]}
    assert created["resume"] != sid, "reused the source session id"
    # The original is untouched where it was.
    assert (src_proj / f"{sid}.jsonl").exists()
    assert {r["cwd"] for r in _read_jsonl(src_proj / f"{sid}.jsonl")
            if "cwd" in r} == {str(alpha), r"D:\projects\detour"}


def test_a_nonexistent_destination_is_refused(monkeypatch, tmp_path):
    """Told plainly, and *before* anything is copied.

    Creating it would be guessing at intent -- a typo'd path and a directory
    that genuinely does not exist yet look identical from here.
    """
    src_cwd = str(tmp_path / "alpha")
    (tmp_path / "alpha").mkdir()
    ghost = tmp_path / "does-not-exist"
    errors, created, dst_cfg = _switch(
        monkeypatch, tmp_path, src_cwd=src_cwd, want_cwd=str(ghost))
    assert errors and "No such directory" in errors[0]
    assert not created, "started a runtime for a refused switch"
    assert not list((dst_cfg / "projects").rglob("*.jsonl")) \
        or dst_cfg == tmp_path / "acct-a"


def test_a_file_is_not_a_directory(monkeypatch, tmp_path):
    src_cwd = str(tmp_path / "alpha")
    (tmp_path / "alpha").mkdir()
    afile = tmp_path / "afile.txt"
    afile.write_text("x", encoding="utf-8")
    errors, created, _ = _switch(
        monkeypatch, tmp_path, src_cwd=src_cwd, want_cwd=str(afile))
    assert errors and "No such directory" in errors[0]
    assert not created


def test_a_missing_name_is_still_refused(monkeypatch, tmp_path):
    """The directory half must not have made the name optional by accident."""
    import server
    ws = _Ws()
    rt = _FakeRt(str(tmp_path), str(tmp_path / "acct"), "sid")
    errors: list[str] = []

    async def fake_send_to(_ws, m):
        if m.get("type") == "move_error":
            errors.append(m.get("message", ""))

    monkeypatch.setattr(server, "send_to", fake_send_to)
    monkeypatch.setattr(server, "_ws_runtime", {ws: rt})
    asyncio.run(server._do_move(ws, {
        "config_dir": str(tmp_path / "acct"), "new_name": "  ",
        "cwd": str(tmp_path)}))
    assert errors and "name" in errors[0].lower()


# ---------------------------------------------------------------------------
# Telling the copy that it moved  (improvements checklist, item 2)
# ---------------------------------------------------------------------------
#
# A copy keeps every absolute path in its transcript, and on a same-machine
# move those paths still *resolve* -- the source tree is still there.  Nothing
# about the copy reveals it moved and the model has no reason to suspect it.
# Observed on a drive migration: the same one-line fix made twice (committed on
# the new drive, left uncommitted on the old), and an agent answering a peer's
# question by running git against the stale copy, where the answers agreeing
# was luck.

def test_a_move_tells_the_copy_where_it_went(monkeypatch, tmp_path):
    src_cwd = str(tmp_path / "alpha")
    dest = tmp_path / "beta"
    dest.mkdir()
    (tmp_path / "alpha").mkdir()
    errors, created, _ = _switch(
        monkeypatch, tmp_path, src_cwd=src_cwd, want_cwd=str(dest))
    assert not errors, errors
    note = created.get("session_note") or ""
    assert src_cwd in note, note
    assert str(dest.resolve()) in note, note


def test_the_note_warns_that_the_old_paths_still_resolve(monkeypatch, tmp_path):
    """The dangerous part is not that the paths are broken -- it is that they
    are not.  A note that only said "you moved" would leave the reader with no
    reason to re-check anything."""
    src_cwd = str(tmp_path / "alpha")
    dest = tmp_path / "beta"
    dest.mkdir()
    (tmp_path / "alpha").mkdir()
    _errors, created, _ = _switch(
        monkeypatch, tmp_path, src_cwd=src_cwd, want_cwd=str(dest))
    note = created.get("session_note") or ""
    assert "still exists" in note, note
    assert "still be running" in note, "did not mention the original session"


def test_an_account_only_move_is_still_worth_saying(monkeypatch, tmp_path):
    """Same directory, different account: the tree is shared, so two live
    sessions can now be working the same files."""
    src_cwd = str(tmp_path / "alpha")
    (tmp_path / "alpha").mkdir()
    _errors, created, _ = _switch(
        monkeypatch, tmp_path, src_cwd=src_cwd, want_cwd="", same_account=False)
    note = created.get("session_note") or ""
    assert "acct-b" in note, note


def test_a_copy_that_went_nowhere_gets_no_note(monkeypatch, tmp_path):
    """Same account, same directory. There is nothing to correct, and a note
    that fires when nothing changed is noise that teaches the reader to skip
    the next one."""
    src_cwd = str(tmp_path / "alpha")
    (tmp_path / "alpha").mkdir()
    _errors, created, _ = _switch(
        monkeypatch, tmp_path, src_cwd=src_cwd, want_cwd=src_cwd)
    assert not created.get("session_note"), created.get("session_note")


def test_the_note_reaches_the_model_and_only_once():
    """It is delivered as a prompt prefix, not a system message: the browser is
    not the audience -- the model is the one holding stale paths.  And it is
    news, not a standing instruction; repeating it every turn would train the
    reader to skip it."""
    import sdk_bridge
    from config import parse_args
    from state import init_state_from_config

    cfg = parse_args(["--session-note", "moved from A to B"])
    st = init_state_from_config(cfg)

    async def bcast(_m):
        return None

    br = sdk_bridge.SDKBridge(config=cfg, state=st, broadcaster=bcast)
    first = br._with_session_note("do the thing")
    second = br._with_session_note("do the next thing")
    assert "moved from A to B" in first
    assert first.endswith("do the thing"), first
    assert second == "do the next thing", second


def test_a_session_with_no_note_is_untouched():
    import sdk_bridge
    from config import parse_args
    from state import init_state_from_config

    cfg = parse_args([])
    st = init_state_from_config(cfg)

    async def bcast(_m):
        return None

    br = sdk_bridge.SDKBridge(config=cfg, state=st, broadcaster=bcast)
    assert br._with_session_note("hello") == "hello"


# ---------------------------------------------------------------------------
# copy_session's import contract
# ---------------------------------------------------------------------------

_NO_TEXTUAL_PROBE = '''
import sys
from importlib.abc import MetaPathFinder


class _Block(MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "textual" or name.startswith("textual."):
            raise ImportError("textual is not installed (simulated)")
        return None


sys.meta_path.insert(0, _Block())
for m in [m for m in sys.modules if m.startswith("textual")]:
    del sys.modules[m]

import copy_session
copy_session.discover_claude_dirs()
copy_session.copy_session_file
copy_session.git_branch_for
print("PURE-OK")
print("MAIN-RC", copy_session.main())
'''


def test_the_pure_half_imports_without_textual(tmp_path):
    """``copy_session``'s discovery half must not need the TUI's dependency.

    It said so in a section header for a long time while a module-scope
    ``from textual import ...`` made it false.  server.py imports
    ``discover_claude_dirs`` on every lobby refresh inside a broad
    ``except Exception``: with ``textual`` missing the lobby silently fell back
    to a single account, and ``/move`` -- which has no such guard -- failed
    outright.  Both were invisible until the hub moved to an interpreter that
    happened not to have ``textual`` installed.
    """
    import subprocess
    probe = tmp_path / "probe.py"
    probe.write_text(_NO_TEXTUAL_PROBE, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    out = subprocess.run([sys.executable, str(probe)], cwd=str(ROOT), env=env,
                         capture_output=True, text=True, timeout=120,
                         stdin=subprocess.DEVNULL)
    assert "PURE-OK" in out.stdout, (out.stdout, out.stderr)


def test_the_wizard_says_what_is_missing_rather_than_crashing(tmp_path):
    """A traceback would be a worse answer than a sentence naming the package."""
    import subprocess
    probe = tmp_path / "probe.py"
    probe.write_text(_NO_TEXTUAL_PROBE, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    out = subprocess.run([sys.executable, str(probe)], cwd=str(ROOT), env=env,
                         capture_output=True, text=True, timeout=120,
                         stdin=subprocess.DEVNULL)
    assert "MAIN-RC 2" in out.stdout, (out.stdout, out.stderr)
    assert "textual" in out.stderr, "did not name the missing package"
    assert "pip install" in out.stderr, "did not say how to fix it"


# ---------------------------------------------------------------------------
# _move_dirs_payload — what the picker offers
# ---------------------------------------------------------------------------

def test_the_directory_list_drops_directories_that_are_gone(monkeypatch, tmp_path):
    """A picker entry is an endorsement.  Offering a directory the copy would
    then refuse turns one clear error into two confusing ones."""
    import server
    alive = tmp_path / "alive"
    alive.mkdir()
    monkeypatch.setattr("copy_session.discover_claude_dirs", lambda: [tmp_path])
    monkeypatch.setattr(server, "list_projects", lambda config_dir=None: [
        {"cwd": str(alive), "newest_mtime": 2},
        {"cwd": str(tmp_path / "vanished"), "newest_mtime": 3},
    ])
    paths = [d["path"] for d in server._move_dirs_payload()]
    assert paths == [str(alive)]


def test_the_directory_list_is_newest_first(monkeypatch, tmp_path):
    import server
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    monkeypatch.setattr("copy_session.discover_claude_dirs", lambda: [tmp_path])
    monkeypatch.setattr(server, "list_projects", lambda config_dir=None: [
        {"cwd": str(a), "newest_mtime": 1},
        {"cwd": str(b), "newest_mtime": 9},
    ])
    assert [d["path"] for d in server._move_dirs_payload()] \
        == [str(b), str(a)]


def test_a_directory_used_by_two_accounts_is_offered_once(monkeypatch, tmp_path):
    """Deduped on the *path*, keeping the newest sighting -- otherwise the same
    directory appears once per account and the ordering reflects scan order
    rather than when it was last worked in."""
    import server
    a = tmp_path / "shared"
    a.mkdir()
    seen: list = []

    # The *first* account scanned holds the newer sighting, so "last one wins"
    # and "newest one wins" give different answers -- with them the other way
    # round the test passes either way and proves nothing.
    def fake_list(config_dir=None):
        seen.append(config_dir)
        return [{"cwd": str(a), "newest_mtime": 50 if config_dir == "c1" else 5}]

    monkeypatch.setattr("copy_session.discover_claude_dirs",
                        lambda: [Path("c1"), Path("c2")])
    monkeypatch.setattr(server, "list_projects", fake_list)
    out = server._move_dirs_payload()
    assert len(seen) == 2, "did not scan every account"
    assert len(out) == 1
    assert out[0]["mtime"] == 50, "kept the last sighting rather than the newest"


def test_a_project_with_no_recorded_cwd_is_skipped(monkeypatch, tmp_path):
    import server
    monkeypatch.setattr("copy_session.discover_claude_dirs", lambda: [tmp_path])
    monkeypatch.setattr(server, "list_projects", lambda config_dir=None: [
        {"cwd": None, "newest_mtime": 1},
    ])
    assert server._move_dirs_payload() == []


def test_one_unreadable_account_does_not_lose_the_others(monkeypatch, tmp_path):
    """Scanning is best-effort: an account whose projects dir cannot be read
    should cost that account, not the picker."""
    import server
    good = tmp_path / "good"
    good.mkdir()

    def fake_list(config_dir=None):
        if config_dir == "bad":
            raise OSError("nope")
        return [{"cwd": str(good), "newest_mtime": 1}]

    monkeypatch.setattr("copy_session.discover_claude_dirs",
                        lambda: [Path("bad"), Path("ok")])
    monkeypatch.setattr(server, "list_projects", fake_list)
    assert [d["path"] for d in server._move_dirs_payload()] == [str(good)]


def test_the_move_list_reply_carries_the_directories(monkeypatch, tmp_path):
    """The overlay cannot offer what it is not sent, and cannot tell "same
    directory" from "different" without the current one."""
    import server
    ws = _Ws()
    rt = _FakeRt(str(tmp_path), str(tmp_path / "acct"), "sid")
    sent: list[dict] = []

    async def fake_send_to(_ws, m):
        sent.append(m)

    monkeypatch.setattr(server, "send_to", fake_send_to)
    monkeypatch.setattr(server, "_ws_runtime", {ws: rt})
    monkeypatch.setattr(server, "_move_accounts_payload", lambda cfg: [])
    monkeypatch.setattr(server, "_move_dirs_payload",
                        lambda: [{"path": "X", "mtime": 1}])
    handled = asyncio.run(server._handle_lobby_message(
        ws, {"type": "move_list"}))
    assert handled
    assert sent and sent[0]["type"] == "move_accounts"
    assert sent[0]["dirs"] == [{"path": "X", "mtime": 1}]
    assert sent[0]["current_cwd"] == str(tmp_path)
