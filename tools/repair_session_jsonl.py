"""Repair a session JSONL whose parse was stopped by a NUL run.

WHAT THIS FIXES
---------------
A session file can acquire a run of NUL bytes in the middle of a line::

    {...previous record...}\\n
    <NUL x 124834>{"type":"custom-title","customTitle":"OS","sessionId":"..."}\\n
    {"type":"queue-operation",...}\\n

NTFS zero-fills a gap when a write lands past a file's valid data length, so
the NULs are *inserted*, not overwritten: verified on the 1.29 GB SlateOS
session, where 442,371 parent links contained only 4 dangling references and
none of them were at a hole.  Nothing is lost -- but the CLI's reader stops at
the corrupt line, so on every resume it sees only the records *before* it,
re-attaches there, and starts a new sibling branch.  That file had accumulated
15 branches off one 2026-07-24 node while the browser, which reads the file
linearly, kept showing the full transcript.

So stripping the NULs is a lossless repair, and it is the one that matters:
it restores the CLI's ability to read to the end of the file.

--relink: PUTTING THE SESSION BACK ON ITS CURRENT SEGMENT
--------------------------------------------------------
Stripping the NULs restores the *reader*.  It does not undo the branches the
broken reader already created, and one of those branches is not merely
cosmetic: it is where the newest records live, so it is what the CLI resumes
into.

How the CLI chooses (verified against the Claude Code source,
``sessionStorage.ts``: ``loadTranscriptFile`` -> leaf computation ->
``findLatestMessage`` -> ``buildConversationChain``):

  * a *leaf* is a record no other record's ``parentUuid`` points at, walked
    back to its nearest ``user``/``assistant`` ancestor;
  * the resume point is the **newest non-sidechain leaf by timestamp**;
  * context is that leaf's ``parentUuid`` chain, walked up to a root.

A compaction writes ``{"type":"system","subtype":"compact_boundary"}`` with
``parentUuid: null``, so each compaction starts a fresh, disjoint component and
the chain stops there.  That is by design: the live context is the newest
compaction segment, and the older ones are history.

The failure mode is a *misattached tail*: records written after the newest
compact boundary that hang off some ancient node instead of off the current
segment.  They are the newest thing in the file, so they win the leaf election,
and the CLI resumes into a five-week-old summary while the current segment --
sitting right there, complete -- is never read.  That is the amnesia.

``--relink`` reattaches exactly those records.  It finds the newest
compact_boundary, takes the newest leaf of *its* component as the splice point,
collects every record newer than that leaf which is not already in the segment,
and re-parents each such subtree's head onto the splice point in timestamp
order.  Nothing is deleted, reordered, or rewritten except one ``parentUuid``
field per head.

It deliberately does NOT chain up the older abandoned branches.  Those predate
the current compact boundary, so splicing them in would push thousands of stale
records into the live context -- and the first thing the CLI would do is
compact them away, taking the current state with them.  They stay in the file
and stay readable; they just are not the conversation any more.

SAFETY
------
Never edit a session file while its CLI is running -- a live writer appending
to a file being rewritten underneath it will corrupt it far worse than the
hole did.  Shut the orchestrator down first (``POST /api/shutdown``).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


def scan(path: Path) -> list[dict]:
    """Describe every NUL-bearing line: offset, sizes, and the surviving text."""
    holes: list[dict] = []
    off = 0
    with path.open("rb") as f:
        for lineno, line in enumerate(f):
            if b"\x00" in line:
                body = line.strip(b"\x00\n")
                ok = True
                try:
                    json.loads(body.decode("utf-8"))
                except Exception:
                    ok = False
                holes.append({
                    "lineno": lineno, "offset": off, "line_len": len(line),
                    "nuls": line.count(b"\x00"), "kept": len(body),
                    "recoverable": ok,
                    "preview": body[:100].decode("utf-8", "replace"),
                })
            off += len(line)
    return holes


def repair(path: Path, out: Path) -> dict:
    """Stream *path* to *out*, stripping NUL padding from corrupt lines."""
    stats = {"lines_in": 0, "lines_out": 0, "holes": 0,
             "nuls_removed": 0, "dropped": 0}
    with path.open("rb") as src, out.open("wb") as dst:
        for line in src:
            stats["lines_in"] += 1
            if b"\x00" not in line:
                dst.write(line)
                stats["lines_out"] += 1
                continue
            stats["holes"] += 1
            stats["nuls_removed"] += line.count(b"\x00")
            body = line.strip(b"\x00\n")
            if not body:
                # Pure filler: the whole line was padding.  Nothing to keep.
                stats["dropped"] += 1
                continue
            dst.write(body + b"\n")
            stats["lines_out"] += 1
    return stats


def verify(original: Path, repaired: Path) -> list[str]:
    """Re-read both files and confirm the repair removed only filler.

    The check that matters: every *record* present before must still be
    present, byte-identical, in the same order.  A line that had NULs is
    compared on its surviving body only.
    """
    problems: list[str] = []
    with original.open("rb") as a, repaired.open("rb") as b:
        bl = iter(b)
        n_a = n_b = 0
        for line in a:
            n_a += 1
            want = line.strip(b"\x00\n") if b"\x00" in line else line.rstrip(b"\n")
            if b"\x00" in line and not want:
                continue  # dropped filler line, expected
            got = next(bl, None)
            if got is None:
                problems.append(f"repaired file ended early at input line {n_a}")
                break
            n_b += 1
            if got.rstrip(b"\n") != want:
                problems.append(
                    f"line {n_a} differs:\n  want {want[:120]!r}\n  got  "
                    f"{got.rstrip(chr(10).encode())[:120]!r}")
                if len(problems) > 5:
                    break
        if next(bl, None) is not None:
            problems.append("repaired file has extra trailing lines")
    with repaired.open("rb") as f:
        while chunk := f.read(1 << 20):
            if b"\x00" in chunk:
                problems.append("repaired file still contains NUL bytes")
                break
    return problems


# --------------------------------------------------------------------------
# --relink
# --------------------------------------------------------------------------

CONVO_TYPES = ("user", "assistant")


class Index:
    """Everything ``--relink`` needs, from one streaming pass.

    Deliberately does not keep the raw lines: the file can be well over a
    gigabyte, and a relink rewrites a handful of ``parentUuid`` fields, so the
    apply pass re-streams instead.
    """

    __slots__ = ("recs", "order", "children", "n_lines", "n_no_uuid")

    def __init__(self) -> None:
        self.recs: dict[str, dict] = {}
        self.order: list[str] = []
        self.children: dict[str, list[str]] = {}
        self.n_lines = 0
        self.n_no_uuid = 0

    def parent(self, uuid: str) -> str | None:
        """The parent *that is present in the file* (dangling reads as root)."""
        p = self.recs[uuid]["parent"]
        return p if p in self.recs else None

    def chain(self, uuid: str) -> list[str]:
        """Root-to-leaf chain, exactly as ``buildConversationChain`` walks it."""
        out: list[str] = []
        seen: set[str] = set()
        cur: str | None = uuid
        while cur is not None and cur not in seen:
            seen.add(cur)
            out.append(cur)
            cur = self.parent(cur)
        out.reverse()
        return out

    def subtree(self, uuid: str) -> list[str]:
        out, stack = [], [uuid]
        while stack:
            u = stack.pop()
            out.append(u)
            stack.extend(self.children.get(u, ()))
        return out

    def leaves(self) -> set[str]:
        """The CLI's leaf set: terminal records resolved to a user/assistant."""
        parented = {p for u in self.order if (p := self.recs[u]["parent"]) is not None}
        found: set[str] = set()
        for u in self.order:
            if u in parented:
                continue
            cur: str | None = u
            seen: set[str] = set()
            while cur is not None and cur not in seen:
                seen.add(cur)
                if self.recs[cur]["type"] in CONVO_TYPES:
                    found.add(cur)
                    break
                cur = self.parent(cur)
        return found

    def newest(self, candidates: set[str]) -> str | None:
        """Newest by timestamp, ties going to the first record in the file.

        ``findLatestMessage`` scans the message Map -- insertion-ordered, i.e.
        file order -- and only replaces its pick on a strict ``>``.  Selecting
        with ``max()`` over a *set* would instead break ties on hash order,
        which is not reproducible between runs, so this mirrors the CLI.
        """
        best: str | None = None
        best_ts = ""
        for u in self.order:
            if u not in candidates:
                continue
            ts = self.recs[u]["ts"]
            if best is None or ts > best_ts:
                best, best_ts = u, ts
        return best

    def resume_leaf(self) -> str | None:
        """Newest non-sidechain leaf -- where the CLI would resume today."""
        return self.newest({u for u in self.leaves()
                            if not self.recs[u]["sidechain"]})


def index_records(path: Path) -> Index:
    """One pass over the file, keeping only the graph fields."""
    idx = Index()
    with path.open("rb") as f:
        for lineno, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            idx.n_lines += 1
            try:
                r = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            uuid = r.get("uuid")
            if not uuid:
                idx.n_no_uuid += 1
                continue
            idx.recs[uuid] = {
                "line": lineno,
                "parent": r.get("parentUuid"),
                "type": r.get("type"),
                "subtype": r.get("subtype"),
                "ts": r.get("timestamp") or "",
                "sidechain": bool(r.get("isSidechain")),
                "tool_use": _block_ids(r, "tool_use", "id"),
                "tool_result": _block_ids(r, "tool_result", "tool_use_id"),
            }
            idx.order.append(uuid)
    for u in idx.order:
        p = idx.recs[u]["parent"]
        if p in idx.recs:
            idx.children.setdefault(p, []).append(u)
    return idx


def _block_ids(record: dict, block_type: str, key: str) -> list[str]:
    content = (record.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [b[key] for b in content
            if isinstance(b, dict) and b.get("type") == block_type and b.get(key)]


def _pending_tool_uses(idx: Index, chain: list[str]) -> list[str]:
    """tool_use ids on *chain* that no record on the chain answers.

    Resuming onto a chain that ends mid-tool-call makes the next request carry
    a ``tool_use`` with no matching ``tool_result``, which the API rejects with
    a 400.  A splice point must be clean.
    """
    answered = {t for u in chain for t in idx.recs[u]["tool_result"]}
    return [t for u in chain for t in idx.recs[u]["tool_use"] if t not in answered]


def plan_relink(idx: Index) -> dict:
    """Decide what (if anything) to re-parent.  Never mutates."""
    plan: dict = {"edits": [], "reason": None}

    boundaries = [u for u in idx.order
                  if idx.recs[u]["type"] == "system"
                  and idx.recs[u]["subtype"] == "compact_boundary"]
    if not boundaries:
        plan["reason"] = ("no compact_boundary record: this session has never "
                          "been compacted, so there is no segment to rejoin")
        return plan

    boundary = idx.newest(set(boundaries))
    segment = set(idx.subtree(boundary))
    plan["boundary"] = boundary
    plan["boundary_ts"] = idx.recs[boundary]["ts"]
    plan["segment_size"] = len(segment)

    splice = idx.newest({u for u in idx.leaves()
                         if u in segment and not idx.recs[u]["sidechain"]})
    if splice is None:
        plan["reason"] = ("the newest compaction segment has no non-sidechain "
                          "leaf; nothing to splice onto")
        return plan
    plan["splice"] = splice
    plan["splice_ts"] = idx.recs[splice]["ts"]

    current = idx.resume_leaf()
    plan["current_leaf"] = current
    plan["chain_before"] = len(idx.chain(current)) if current else 0
    if current is not None and current in segment:
        plan["reason"] = ("the newest leaf is already inside the newest "
                          "compaction segment: nothing to relink")
        return plan

    # Records that postdate the splice point but are not part of the segment:
    # work written after the last good compaction that landed somewhere else.
    cutoff = idx.recs[splice]["ts"]
    stray = {u for u in idx.order
             if u not in segment and idx.recs[u]["ts"] > cutoff}
    if not stray:
        plan["reason"] = ("nothing postdates the newest compaction segment; "
                          "the stale leaf is older than the segment and would "
                          "not be resumed into")
        return plan

    heads = sorted((u for u in stray if idx.recs[u]["parent"] not in stray),
                   key=lambda u: idx.recs[u]["ts"])
    plan["stray_records"] = len(stray)

    anchor = splice
    for head in heads:
        moved = [u for u in idx.subtree(head) if u in stray]
        plan["edits"].append({
            "head": head,
            "head_ts": idx.recs[head]["ts"],
            "head_type": idx.recs[head]["type"],
            "old_parent": idx.recs[head]["parent"],
            "new_parent": anchor,
            "moved": len(moved),
        })
        # Chain the next stray tail onto this one's own newest record, so
        # several strays come back in the order they were written.
        anchor = idx.newest(set(moved))

    plan["warnings"] = _relink_warnings(idx, plan)
    return plan


def _relink_warnings(idx: Index, plan: dict) -> list[str]:
    """Seam problems worth printing but not worth refusing over."""
    out: list[str] = []
    splice = plan["splice"]
    pending = _pending_tool_uses(idx, idx.chain(splice))
    if pending:
        out.append(f"splice point {splice[:8]} has {len(pending)} unanswered "
                   f"tool_use id(s); resuming there may be rejected by the API")
    if idx.recs[splice]["type"] != "assistant":
        out.append(f"splice point {splice[:8]} is a {idx.recs[splice]['type']} "
                   f"record, so the seam is not assistant -> user")
    for e in plan["edits"]:
        if e["head_type"] != "user":
            out.append(f"re-parented head {e['head'][:8]} is a "
                       f"{e['head_type']} record, not a user turn")
    return out


def _reparent_line(line: bytes, old: str | None, new: str) -> bytes:
    """Swap one record's ``parentUuid`` without reformatting the rest.

    Re-serialising the record would be simpler and would also silently rewrite
    every other field to Python's idea of JSON spacing, so this edits the one
    value in place and re-parses to prove the result is still the same record.
    """
    text = line.rstrip(b"\n").decode("utf-8")
    before = json.loads(text)
    if before.get("parentUuid") != old:
        raise ValueError(f"record's parentUuid is {before.get('parentUuid')!r}, "
                         f"not the planned {old!r}")
    want = "null" if old is None else json.dumps(old)
    key = '"parentUuid"'

    # ``"parentUuid"`` is not necessarily unique in the line.  These transcripts
    # quote JSON all the time -- a message discussing session files contains the
    # literal text of a record -- so the first occurrence can easily be inside a
    # content string.  Try each occurrence and keep the one that re-parses to
    # the same record with only parentUuid moved; that check is what tells the
    # decoy from the field.
    rejected = 0
    at = text.find(key)
    while at >= 0:
        cur = at + len(key)
        while cur < len(text) and text[cur] in " \t":
            cur += 1
        if cur < len(text) and text[cur] == ":":
            cur += 1
            while cur < len(text) and text[cur] in " \t":
                cur += 1
            if text.startswith(want, cur):
                patched = text[:cur] + json.dumps(new) + text[cur + len(want):]
                try:
                    after = json.loads(patched)
                except ValueError:
                    after = None
                if (after is not None and after.get("parentUuid") == new
                        and {k: v for k, v in after.items() if k != "parentUuid"}
                        == {k: v for k, v in before.items() if k != "parentUuid"}):
                    return patched.encode("utf-8") + b"\n"
                rejected += 1
        at = text.find(key, at + 1)
    raise ValueError(
        f"no parentUuid field in this record could be rewritten safely "
        f"({rejected} candidate(s) rejected because patching them changed "
        f"another field)")


def apply_relink(path: Path, out: Path, idx: Index, plan: dict) -> dict:
    """Stream *path* to *out*, re-parenting the planned heads."""
    by_line = {idx.recs[e["head"]]["line"]: e for e in plan["edits"]}
    stats = {"lines": 0, "edited": 0}
    with path.open("rb") as src, out.open("wb") as dst:
        for lineno, line in enumerate(src):
            edit = by_line.get(lineno)
            if edit is not None and line.strip():
                line = _reparent_line(line, edit["old_parent"], edit["new_parent"])
                stats["edited"] += 1
            dst.write(line)
            stats["lines"] += 1
    if stats["edited"] != len(plan["edits"]):
        raise ValueError(f"expected {len(plan['edits'])} edits, made "
                         f"{stats['edited']}")
    return stats


def verify_relink(original: Path, repaired: Path, idx: Index,
                  plan: dict) -> tuple[list[str], dict]:
    """Confirm only the planned parent links moved, and the chain now lands."""
    problems: list[str] = []
    edited_lines = {idx.recs[e["head"]]["line"] for e in plan["edits"]}
    with original.open("rb") as a, repaired.open("rb") as b:
        n = 0
        for n, (la, lb) in enumerate(zip(a, b)):
            if la == lb:
                continue
            if n not in edited_lines:
                problems.append(f"line {n:,} changed but was not in the plan")
                if len(problems) > 5:
                    break
                continue
            ra, rb = json.loads(la.strip()), json.loads(lb.strip())
            if {k: v for k, v in ra.items() if k != "parentUuid"} != \
                    {k: v for k, v in rb.items() if k != "parentUuid"}:
                problems.append(f"line {n:,} changed more than parentUuid")
        if a.read(1) or b.read(1):
            problems.append("files differ in length")
    after = index_records(repaired)
    leaf = after.resume_leaf()
    chain = after.chain(leaf) if leaf else []
    result = {
        "leaf": leaf,
        "chain_after": len(chain),
        "root": chain[0] if chain else None,
        "segment_covered": sum(1 for u in chain if u in set(after.subtree(plan["boundary"]))),
    }
    if leaf is None:
        problems.append("repaired file has no resume leaf")
    elif chain[0] != plan["boundary"]:
        problems.append(
            f"chain roots at {chain[0][:8]} ({after.recs[chain[0]]['ts']}), not "
            f"at the newest compact boundary {plan['boundary'][:8]}")
    return problems, result


def _print_plan(idx: Index, plan: dict) -> None:
    print(f"  {idx.n_lines:,} records ({len(idx.recs):,} with a uuid, "
          f"{idx.n_no_uuid:,} meta)")
    if plan.get("boundary"):
        print(f"  newest compact boundary: {plan['boundary'][:8]} "
              f"{plan['boundary_ts']} -- segment of {plan['segment_size']:,} records")
    if plan.get("splice"):
        print(f"  newest leaf of that segment: {plan['splice'][:8]} "
              f"{plan['splice_ts']}")
    if plan.get("current_leaf"):
        cur = plan["current_leaf"]
        print(f"  CLI would resume at: {cur[:8]} {idx.recs[cur]['ts']} "
              f"-- chain of {plan['chain_before']:,} records back to "
              f"{idx.chain(cur)[0][:8]} ({idx.recs[idx.chain(cur)[0]]['ts']})")
    if plan["reason"]:
        print(f"  -> {plan['reason']}")
        return
    print(f"  {plan['stray_records']:,} record(s) postdate that segment "
          f"but sit outside it")
    for e in plan["edits"]:
        print(f"    re-parent {e['head'][:8]} ({e['head_ts']}, {e['head_type']}, "
              f"{e['moved']} record(s) with it)")
        old = e["old_parent"]
        old_ts = idx.recs[old]["ts"] if old in idx.recs else "not in file"
        print(f"        from {(old or 'ROOT')[:8]} ({old_ts})"
              f"  ->  {e['new_parent'][:8]} ({plan['splice_ts']})")
    for w in plan.get("warnings", ()):
        print(f"  WARNING: {w}")


def run_relink(path: Path, apply: bool, backup_dir: Path) -> int:
    print(f"{path}\n  {path.stat().st_size:,} bytes")
    idx = index_records(path)
    plan = plan_relink(idx)
    _print_plan(idx, plan)
    if plan["reason"]:
        return 0
    if not apply:
        print("\n(dry run -- pass --apply to rewrite)")
        return 0

    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / (path.name + ".prerelink.bak")
    if not backup.exists():
        shutil.copy2(path, backup)
        print(f"\nbackup -> {backup}")
    else:
        print(f"\nbackup already exists -> {backup} (kept)")

    tmp = path.with_suffix(path.suffix + ".relink.tmp")
    try:
        stats = apply_relink(path, tmp, idx, plan)
        print(f"  {stats['lines']:,} lines written, {stats['edited']} re-parented")
        problems, result = verify_relink(path, tmp, idx, plan)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        print(f"\nRELINK FAILED -- original left untouched: {exc}")
        return 1
    if problems:
        print("\nVERIFY FAILED -- original left untouched:")
        for p in problems:
            print("  " + p)
        tmp.unlink(missing_ok=True)
        return 1
    os.replace(tmp, path)
    print(f"  verified; replaced.")
    print(f"  CLI now resumes at {result['leaf'][:8]} with a chain of "
          f"{result['chain_after']:,} records "
          f"(was {plan['chain_before']:,}), rooted at the "
          f"{plan['boundary_ts']} compact boundary")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path, help="session .jsonl to repair")
    ap.add_argument("--apply", action="store_true",
                    help="actually replace the file (default: report only)")
    ap.add_argument("--backup-dir", type=Path,
                    default=Path.home() / ".claude-session-backups")
    ap.add_argument("--relink", action="store_true",
                    help="re-parent records written after the newest compaction "
                         "segment back onto it (fixes resume-time amnesia)")
    args = ap.parse_args()

    path: Path = args.path
    if not path.is_file():
        print(f"not a file: {path}")
        return 2

    if args.relink:
        return run_relink(path, args.apply, args.backup_dir)

    holes = scan(path)
    size = path.stat().st_size
    print(f"{path}\n  {size:,} bytes, {len(holes)} NUL-bearing line(s)")
    for h in holes:
        flag = "" if h["recoverable"] else "   <-- SURVIVING TEXT IS NOT VALID JSON"
        print(f"  - line {h['lineno']:,} at byte {h['offset']:,}: "
              f"{h['nuls']:,} NULs + {h['kept']} bytes kept{flag}")
        print(f"      {h['preview']}")
    if not holes:
        print("  nothing to repair")
        return 0
    if any(not h["recoverable"] and h["kept"] for h in holes):
        print("\nRefusing to repair: a corrupt line's surviving bytes are not a\n"
              "complete JSON record, so this is genuine data loss rather than\n"
              "filler and needs to be looked at by hand.")
        return 1
    if not args.apply:
        print("\n(dry run -- pass --apply to rewrite)")
        return 0

    args.backup_dir.mkdir(parents=True, exist_ok=True)
    backup = args.backup_dir / (path.name + ".prerepair.bak")
    if not backup.exists():
        shutil.copy2(path, backup)
        print(f"\nbackup -> {backup}")
    else:
        print(f"\nbackup already exists -> {backup} (kept)")

    tmp = path.with_suffix(path.suffix + ".repair.tmp")
    stats = repair(path, tmp)
    print(f"  {stats['lines_in']:,} lines in -> {stats['lines_out']:,} out; "
          f"{stats['nuls_removed']:,} NUL bytes removed from "
          f"{stats['holes']} line(s); {stats['dropped']} empty line(s) dropped")

    problems = verify(path, tmp)
    if problems:
        print("\nVERIFY FAILED -- original left untouched:")
        for p in problems:
            print("  " + p)
        tmp.unlink(missing_ok=True)
        return 1

    os.replace(tmp, path)
    print(f"  verified; replaced. {path.stat().st_size:,} bytes "
          f"({size - path.stat().st_size:,} smaller)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
