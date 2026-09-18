"""Cross-account session-title read/write tests.

Session JSONL files live under ``CLAUDE_CONFIG_DIR/projects/<slug>/``.  A hub
process runs under *one* account's config dir but hosts runtimes bound to
*other* accounts (each ``Config`` carries its own ``config_dir``).  These tests
pin down the account-scoping contract that a previous bug got wrong:

* ``title_from_jsonl`` reads the exact file it's handed (account-correct by
  construction) and prefers ``custom-title`` over ``ai-title``.
* ``read_session_title`` / ``write_session_title`` honour their ``config_dir``
  argument, so a title written to account B lands in account B's tree and is
  read back from there — not from the hub process's own account.
* ``_parse_session_info`` (which feeds the lobby session list) takes its title
  from the file it parses, so account-B sessions show account-B titles.

The bug: reads/writes ignored ``config_dir`` and re-resolved by session id
against the active env account, so renames of another account's session either
failed or wrote to the wrong place, and the lobby showed stale AI summaries.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import session as session_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_title_index(monkeypatch, tmp_path):
    """Keep the title index *and the rename pins* per-test, so tests never read
    or write the real ``~/.orchestrator2_title_index.json`` /
    ``~/.orchestrator2_renames.json``."""
    monkeypatch.setattr(session_mod, "_title_index", {}, raising=False)
    monkeypatch.setattr(session_mod, "_title_index_dirty", False, raising=False)
    monkeypatch.setattr(
        session_mod, "_TITLE_INDEX_PATH", tmp_path / "_title_index.json",
        raising=False,
    )
    monkeypatch.setattr(session_mod, "_rename_pins", {}, raising=False)
    monkeypatch.setattr(
        session_mod, "_RENAME_PIN_PATH", tmp_path / "_renames.json",
        raising=False,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_account(root: Path, name: str) -> Path:
    """Create a fake ``CLAUDE_CONFIG_DIR`` with an empty projects tree."""
    cfg = root / name
    (cfg / "projects").mkdir(parents=True, exist_ok=True)
    return cfg


def _write_session(cfg_dir: Path, slug: str, session_id: str,
                   records: list[dict]) -> Path:
    proj = cfg_dir / "projects" / slug
    proj.mkdir(parents=True, exist_ok=True)
    jsonl = proj / f"{session_id}.jsonl"
    with jsonl.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return jsonl


# ---------------------------------------------------------------------------
# title_from_jsonl
# ---------------------------------------------------------------------------

def test_title_from_jsonl_prefers_custom_over_ai(tmp_path: Path) -> None:
    jsonl = _write_session(tmp_path, "slug", "sid1", [
        {"type": "ai-title", "aiTitle": "auto summary"},
        {"type": "custom-title", "customTitle": "my rename"},
    ])
    assert session_mod.title_from_jsonl(jsonl) == "my rename"


def test_title_from_jsonl_falls_back_to_ai(tmp_path: Path) -> None:
    jsonl = _write_session(tmp_path, "slug", "sid1", [
        {"type": "ai-title", "aiTitle": "auto summary"},
    ])
    assert session_mod.title_from_jsonl(jsonl) == "auto summary"


def test_title_from_jsonl_last_custom_wins(tmp_path: Path) -> None:
    jsonl = _write_session(tmp_path, "slug", "sid1", [
        {"type": "custom-title", "customTitle": "first"},
        {"type": "custom-title", "customTitle": "second"},
    ])
    assert session_mod.title_from_jsonl(jsonl) == "second"


def test_title_from_jsonl_missing_file_returns_none(tmp_path: Path) -> None:
    assert session_mod.title_from_jsonl(tmp_path / "nope.jsonl") is None


# ---------------------------------------------------------------------------
# Incremental title index — a rename buried mid-file (renamed early, then used
# heavily) must still resolve to the custom title, and a *new* rename appended
# after the file was first indexed must be picked up on the next resolve.
# ---------------------------------------------------------------------------

def _append(jsonl: Path, rec: dict) -> None:
    with jsonl.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def test_index_picks_up_rename_appended_after_first_scan(tmp_path: Path) -> None:
    jsonl = _write_session(tmp_path, "slug", "sid1", [
        {"type": "ai-title", "aiTitle": "auto summary"},
        {"type": "user", "message": {"content": "hi"}},
    ])
    # First resolve indexes the file at its current size -> ai title.
    assert session_mod._resolve_title(jsonl) == "auto summary"
    # User renames later; the custom-title lands in the appended region.
    _append(jsonl, {"type": "custom-title", "customTitle": "my rename"})
    assert session_mod._resolve_title(jsonl) == "my rename"


def test_index_retains_buried_custom_after_more_activity(tmp_path: Path) -> None:
    jsonl = _write_session(tmp_path, "slug", "sid1", [
        {"type": "ai-title", "aiTitle": "auto summary"},
        {"type": "custom-title", "customTitle": "my rename"},
    ])
    assert session_mod._resolve_title(jsonl) == "my rename"
    # Heavy subsequent use buries the custom-title far from head and tail.
    for i in range(50):
        _append(jsonl, {"type": "user", "message": {"content": f"msg {i}"}})
    # The index remembers it — no re-scan of the whole file needed.
    assert session_mod._resolve_title(jsonl) == "my rename"
    # A second rename is still caught.
    _append(jsonl, {"type": "custom-title", "customTitle": "renamed again"})
    assert session_mod._resolve_title(jsonl) == "renamed again"


def test_index_persists_and_reloads(tmp_path: Path, monkeypatch) -> None:
    """flush_title_index writes to disk; a fresh in-memory index reloads it."""
    idx_path = tmp_path / "idx.json"
    monkeypatch.setattr(session_mod, "_TITLE_INDEX_PATH", idx_path)
    monkeypatch.setattr(session_mod, "_title_index", {})
    jsonl = _write_session(tmp_path, "slug", "sid1", [
        {"type": "custom-title", "customTitle": "persisted title"},
    ])
    assert session_mod._resolve_title(jsonl) == "persisted title"
    session_mod.flush_title_index()
    assert idx_path.exists()
    # Simulate a fresh process: drop the in-memory index, reload from disk.
    monkeypatch.setattr(session_mod, "_title_index", None)
    reloaded = session_mod._load_title_index()
    assert str(jsonl) in reloaded
    assert reloaded[str(jsonl)]["custom"] == "persisted title"


# ---------------------------------------------------------------------------
# read/write with config_dir scoping
# ---------------------------------------------------------------------------

def test_read_write_scoped_to_account(tmp_path: Path, monkeypatch) -> None:
    """A title written to account B is read back from account B — even when the
    process env points at account A and account A has a same-id session."""
    acct_a = _make_account(tmp_path, "account-a")
    acct_b = _make_account(tmp_path, "account-b")
    # Hub process runs under account A.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(acct_a))

    sid = "11111111-1111-1111-1111-111111111111"
    # Same session id exists in *both* accounts (different content).
    _write_session(acct_a, "slug", sid, [
        {"type": "ai-title", "aiTitle": "A auto"},
    ])
    _write_session(acct_b, "slug", sid, [
        {"type": "ai-title", "aiTitle": "B auto"},
    ])

    # Reads honour config_dir.
    assert session_mod.read_session_title(sid, str(acct_a)) == "A auto"
    assert session_mod.read_session_title(sid, str(acct_b)) == "B auto"

    # Write to account B; must not touch account A.
    session_mod.write_session_title(sid, "B renamed", str(acct_b))
    assert session_mod.read_session_title(sid, str(acct_b)) == "B renamed"
    assert session_mod.read_session_title(sid, str(acct_a)) == "A auto"


def test_write_to_other_account_skips_sdk(tmp_path: Path, monkeypatch) -> None:
    """When config_dir names a non-env account, the SDK rename helper (which is
    env-scoped and would misfire) must be bypassed in favour of a direct
    account-correct append."""
    acct_a = _make_account(tmp_path, "account-a")
    acct_b = _make_account(tmp_path, "account-b")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(acct_a))

    sid = "22222222-2222-2222-2222-222222222222"
    _write_session(acct_b, "slug", sid, [
        {"type": "ai-title", "aiTitle": "B auto"},
    ])

    called = {"sdk": False}

    def _boom(*_a, **_k):
        called["sdk"] = True
        raise AssertionError("SDK rename_session must not run for other account")

    monkeypatch.setattr(session_mod, "rename_session", _boom, raising=False)
    # Also guard the import site: inject a fake module attr the function imports.
    import types
    fake_sdk = types.ModuleType("claude_agent_sdk")
    fake_sdk.rename_session = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    session_mod.write_session_title(sid, "B renamed", str(acct_b))
    assert called["sdk"] is False
    assert session_mod.read_session_title(sid, str(acct_b)) == "B renamed"


# ---------------------------------------------------------------------------
# _parse_session_info (lobby list) reads the file it parses
# ---------------------------------------------------------------------------

def test_parse_session_info_uses_file_title(tmp_path: Path, monkeypatch) -> None:
    """The lobby list must show the parsed file's own title, not one
    re-resolved by id against the process env account."""
    acct_a = _make_account(tmp_path, "account-a")
    acct_b = _make_account(tmp_path, "account-b")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(acct_a))

    sid = "33333333-3333-3333-3333-333333333333"
    # Account A holds a stale AI title for the same id; account B holds the
    # user's rename.  Parsing B's file must yield B's rename.
    _write_session(acct_a, "slug", sid, [
        {"type": "ai-title", "aiTitle": "stale A summary"},
        {"type": "user", "message": {"content": "hi"}, "cwd": "/x"},
    ])
    jsonl_b = _write_session(acct_b, "slug", sid, [
        {"type": "custom-title", "customTitle": "B rename"},
        {"type": "user", "message": {"content": "hi"}, "cwd": "/x"},
    ])

    info = session_mod._parse_session_info(jsonl_b, "slug")
    assert info is not None
    assert info["title"] == "B rename"


# ---------------------------------------------------------------------------
# Sticky renames: the CLI re-stamps its cached title at EOF forever
# ---------------------------------------------------------------------------
#
# `reAppendSessionMetadata()` re-appends the CLI's in-memory title on every
# compaction and on resume, and only absorbs an external rename if that record
# is still inside the last 64 KiB.  On a busy session it isn't, so the stale
# title keeps landing at EOF and "last record wins" hands it the vote.  These
# tests use a helper that fakes exactly that: append the *old* title again.

def _cli_restamp(jsonl: Path, title: str) -> None:
    """Simulate the CLI re-appending its (stale) cached title at EOF."""
    with jsonl.open("a", encoding="utf-8") as f:
        f.write(json.dumps(
            {"type": "custom-title", "customTitle": title},
            separators=(",", ":"),
        ) + "\n")


def _acct(tmp_path: Path, monkeypatch) -> Path:
    cfg = _make_account(tmp_path, "account-a")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    # Force the manual-append path; the SDK helper isn't importable offline
    # anyway, but be explicit so the test doesn't depend on that.
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    return cfg


def test_rename_survives_a_stale_cli_restamp(tmp_path: Path, monkeypatch) -> None:
    """The reported bug: /rename OSc, then the title reverts to OS on reload."""
    cfg = _acct(tmp_path, monkeypatch)
    sid = "44444444-4444-4444-4444-444444444444"
    jsonl = _write_session(cfg, "slug", sid, [
        {"type": "custom-title", "customTitle": "OS"},
    ])

    session_mod.write_session_title(sid, "OSc", str(cfg))
    assert session_mod.read_session_title(sid, str(cfg)) == "OSc"

    # The CLI compacts (or is resumed) and re-appends its stale cached title.
    _cli_restamp(jsonl, "OS")
    assert session_mod.title_from_jsonl(jsonl) == "OSc"

    # ...and keeps doing it, forever.
    for _ in range(20):
        _cli_restamp(jsonl, "OS")
    assert session_mod.read_session_title(sid, str(cfg)) == "OSc"


def test_a_chain_of_renames_treats_every_earlier_title_as_stale(
    tmp_path: Path, monkeypatch,
) -> None:
    """A → B → C: a re-stamp of *either* A or B must not resurrect."""
    cfg = _acct(tmp_path, monkeypatch)
    sid = "55555555-5555-5555-5555-555555555555"
    jsonl = _write_session(cfg, "slug", sid, [
        {"type": "custom-title", "customTitle": "A"},
    ])

    session_mod.write_session_title(sid, "B", str(cfg))
    session_mod.write_session_title(sid, "C", str(cfg))
    assert session_mod.title_from_jsonl(jsonl) == "C"

    _cli_restamp(jsonl, "A")
    assert session_mod.title_from_jsonl(jsonl) == "C"
    _cli_restamp(jsonl, "B")
    assert session_mod.title_from_jsonl(jsonl) == "C"


def test_a_genuine_third_party_rename_wins_and_drops_the_pin(
    tmp_path: Path, monkeypatch,
) -> None:
    """A title we've never written or seen is a real newer intent (someone typed
    /rename inside the CLI).  We must yield to it, and stop overriding."""
    cfg = _acct(tmp_path, monkeypatch)
    sid = "66666666-6666-6666-6666-666666666666"
    jsonl = _write_session(cfg, "slug", sid, [
        {"type": "custom-title", "customTitle": "OS"},
    ])
    session_mod.write_session_title(sid, "OSc", str(cfg))

    _cli_restamp(jsonl, "renamed in the CLI")
    assert session_mod.title_from_jsonl(jsonl) == "renamed in the CLI"

    # The pin is gone, so even a later stamp of the pre-rename value stands.
    _cli_restamp(jsonl, "OS")
    assert session_mod.title_from_jsonl(jsonl) == "OS"


def test_the_pin_does_not_leak_across_sessions(tmp_path: Path, monkeypatch) -> None:
    """Pins are keyed by JSONL path, so renaming one session must not retitle
    another — including the same session id under a different account."""
    cfg_a = _make_account(tmp_path, "account-a")
    cfg_b = _make_account(tmp_path, "account-b")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg_a))
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)

    sid = "77777777-7777-7777-7777-777777777777"
    _write_session(cfg_a, "slug", sid, [{"type": "custom-title", "customTitle": "OS"}])
    jsonl_b = _write_session(cfg_b, "slug", sid,
                             [{"type": "custom-title", "customTitle": "OS"}])

    session_mod.write_session_title(sid, "OSc", str(cfg_a))
    assert session_mod.read_session_title(sid, str(cfg_a)) == "OSc"
    assert session_mod.title_from_jsonl(jsonl_b) == "OS"


def test_the_pin_survives_a_restart(tmp_path: Path, monkeypatch) -> None:
    """The pin is the only record of the user's intent, so it must be written
    through to disk at rename time — not held in memory until some flush."""
    cfg = _acct(tmp_path, monkeypatch)
    sid = "88888888-8888-8888-8888-888888888888"
    jsonl = _write_session(cfg, "slug", sid, [
        {"type": "custom-title", "customTitle": "OS"},
    ])
    session_mod.write_session_title(sid, "OSc", str(cfg))
    _cli_restamp(jsonl, "OS")

    # Drop every in-process cache, as a fresh `python server.py` would.
    monkeypatch.setattr(session_mod, "_rename_pins", None, raising=False)
    monkeypatch.setattr(session_mod, "_title_index", {}, raising=False)
    assert session_mod.title_from_jsonl(jsonl) == "OSc"


def test_the_manual_append_is_compact_like_the_cli(tmp_path: Path, monkeypatch) -> None:
    """The CLI's external-writer check is
    ``line.startsWith('{"type":"custom-title"')`` — whitespace-sensitive — so a
    record serialised with default separators could never be noticed by it."""
    cfg = _acct(tmp_path, monkeypatch)
    sid = "99999999-9999-9999-9999-999999999999"
    jsonl = _write_session(cfg, "slug", sid, [
        {"type": "user", "message": {"content": "hi"}},
    ])
    session_mod.write_session_title(sid, "OSc", str(cfg))

    last = jsonl.read_bytes().splitlines()[-1]
    assert last.startswith(b'{"type":"custom-title"'), last


# ---------------------------------------------------------------------------
# Appending after a killed CLI — the corruption this whole module caused
# ---------------------------------------------------------------------------
#
# Three NUL runs (124,834 / 13,564 / 893 bytes) were found buried in a 1.29 GB
# session file, and every one of them sat immediately before a 93-byte
# custom-title record — the only thing this process ever appends.  The CLI had
# been killed mid-write, leaving the file size extended past its valid data, so
# the tail read as zeros; our O_APPEND then wrote *after* the zeros and moved
# them from the end of the file into the middle of it.  From then on the CLI's
# reader stopped at the hole on every resume, re-attached to the last record it
# could parse and grew a sibling branch: weeks of silent amnesia.
#
# Note these are NOT fixed by routing the write through the SDK's
# ``rename_session`` — that helper is the same O_APPEND of the same 93 bytes.

def test_a_nul_tail_is_removed_before_the_title_is_appended(tmp_path: Path) -> None:
    jsonl = _write_session(tmp_path, "slug", "sid1",
                           [{"type": "user", "uuid": "a"}])
    good = jsonl.read_bytes()
    with jsonl.open("ab") as f:            # the killed CLI's phantom extension
        f.write(b"\x00" * 4096)

    session_mod.heal_tail_before_append(jsonl)

    assert jsonl.read_bytes() == good, "the NUL run must be gone, nothing else"


def test_the_appended_title_never_lands_after_a_hole(tmp_path: Path) -> None:
    """The end-to-end property: no NUL may ever end up mid-file."""
    cfg = _make_account(tmp_path, "acct")
    sid = "11111111-2222-3333-4444-555555555555"
    jsonl = _write_session(cfg, "slug", sid, [{"type": "user", "uuid": "a"}])
    with jsonl.open("ab") as f:
        f.write(b"\x00" * 1024)

    session_mod.write_session_title(sid, "OS", str(cfg))

    raw = jsonl.read_bytes()
    assert b"\x00" not in raw
    assert raw.splitlines()[-1].startswith(b'{"type":"custom-title"')


def test_a_half_written_record_is_dropped_not_glued_to(tmp_path: Path) -> None:
    """A fragment with no newline would otherwise be fused with our record."""
    jsonl = _write_session(tmp_path, "slug", "sid1",
                           [{"type": "user", "uuid": "a"}])
    good = jsonl.read_bytes()
    with jsonl.open("ab") as f:
        f.write(b'{"type":"assistant","uu')      # killed mid-record

    session_mod.heal_tail_before_append(jsonl)
    assert jsonl.read_bytes() == good


def test_a_complete_last_record_is_never_touched(tmp_path: Path) -> None:
    jsonl = _write_session(tmp_path, "slug", "sid1",
                           [{"type": "user", "uuid": "a"},
                            {"type": "assistant", "uuid": "b"}])
    before = jsonl.read_bytes()
    assert session_mod.heal_tail_before_append(jsonl) == {"nuls": 0, "partial": 0}
    assert jsonl.read_bytes() == before


def test_a_missing_final_newline_is_added_not_swallowed(tmp_path: Path) -> None:
    """A complete record that merely lost its newline keeps its content."""
    jsonl = tmp_path / "s.jsonl"
    jsonl.write_bytes(b'{"type":"user","uuid":"a"}')   # valid, but unterminated
    session_mod.heal_tail_before_append(jsonl)
    assert jsonl.read_bytes() == b'{"type":"user","uuid":"a"}\n'


def test_an_all_nul_file_is_left_alone(tmp_path: Path) -> None:
    """Refuse rather than truncate someone's history to nothing."""
    jsonl = tmp_path / "s.jsonl"
    jsonl.write_bytes(b"\x00" * 512)
    session_mod.heal_tail_before_append(jsonl)
    assert jsonl.stat().st_size == 512


def test_a_nul_run_longer_than_one_chunk_is_fully_removed(tmp_path: Path) -> None:
    """The worst run observed was 124 KB, well past the 64 KB scan chunk."""
    jsonl = _write_session(tmp_path, "slug", "sid1",
                           [{"type": "user", "uuid": "a"}])
    good = jsonl.read_bytes()
    with jsonl.open("ab") as f:
        f.write(b"\x00" * 200_000)
    session_mod.heal_tail_before_append(jsonl)
    assert jsonl.read_bytes() == good
