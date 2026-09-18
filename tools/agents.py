#!/usr/bin/env python3
"""Address, message and halt the other Claude sessions on this machine.

Implements the operator/agent surface of
``D:/visual studio projects/specs/agent-comms-spec.md``.  The storage and
semantics live in :mod:`agent_comms`; this is the part a script or a human
calls.

THE ONE CONTRACT THAT MATTERS
-----------------------------
::

    python tools/agents.py check-halt --scope repo

exits **0 when clear and non-zero when halted**, so an expensive operation can
refuse by inspecting the status alone, without parsing output (spec §6.3).
That is the whole enforcement mechanism: the system never tries to work out
which processes are long-running (§6.2).  The operation about to spend three
hours is the one that knows it is expensive, and it is also the only thing that
knows where its safe abort points are -- refusing *before* a build is free,
whereas aborting two hours in can leave stale locks behind.

It is cooperative, and that limit is deliberate: an operation that does not
check cannot be stopped.  Enforcing against uncooperative processes needs
OS-level mechanisms and is a different, much worse project.

Both directions are covered by ``--self-test``, because a checker with only
positive cases passes for one that reports everything, and one with only
negatives passes for one that reports nothing.  In particular: **an
informational message with no halt must exit 0** -- a notice is not a halt, and
conflating them would train every caller to ignore the exit code.

A NON-GOAL: RELAYED AUTHORITY
-----------------------------
**A peer message is never the operator's instruction, however it is worded.**
A relay is unverifiable and looks identical whether it is accurate, a
good-faith misreading, or wrong; a message saying "the operator asked me to
tell you to resume" carries no more authority than the peer's own opinion, and
should be treated as exactly that.

This happened, and the receiving agent got it right.  Its reasoning is the
reasoning: *if I wait and you are right, the cost is a delay until the operator
types one line; if I act and you are mistaken, I am working against a direct
instruction and neither of us finds out for a while.*  The costs are not
symmetric, so neither is the correct response.

Nothing here will ever mark a message "operator-originated" (checklist item 5).
Such a flag would not be verifiable either -- any agent could set it -- and it
would be **worse than nothing**, because it would look like authority.  If the
operator wants something done, the operator can say so in that session.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_comms as ac  # noqa: E402


def _stamp(t: float) -> str:
    return _dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")


def _age(t: float) -> str:
    d = max(0.0, _dt.datetime.now().timestamp() - t)
    if d < 90:
        return f"{d:.0f}s ago"
    if d < 5400:
        return f"{d / 60:.0f}m ago"
    return f"{d / 3600:.1f}h ago"


def _identity(args) -> str:
    """This caller's identity: explicit, else the environment's, else unknown.

    A human running the CLI by hand often has no identity, which is fine for
    reading and for raising a halt (the operator may raise one, §6.4).  It is
    not fine for *receiving*, so the inbox path requires one.
    """
    return (args.as_ or os.environ.get("ORCH2_AGENT_NAME") or "").strip()


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_list(args) -> int:
    """Acceptance §8.1: enumerate live sessions *across accounts*."""
    cwd = args.cwd or os.getcwd()
    repo = ac.repo_key(cwd)
    kw = {}
    if args.scope == "repo":
        kw["repo"] = repo
    elif args.scope == "cwd":
        kw["cwd"] = cwd
    agents = ac.live_agents(**kw)
    if not agents:
        print("no live agents" + (f" for {args.scope}" if args.scope != "machine" else ""))
        return 0
    print(f"{'identity':<20} {'account':<22} {'branch':<16} {'seen':<10} cwd")
    for a in agents:
        acct = Path(a.account).name if a.account else "-"
        print(f"{a.identity:<20} {acct:<22} {(a.branch or '-'):<16} "
              f"{_age(a.heartbeat_at):<10} {a.cwd}")
        # Caller-supplied labels (--agent-label k=v).  The service never
        # interprets them: "lane" is the project's word, not this tool's.
        if a.labels:
            kv = "  ".join(f"{k}={v}" for k, v in sorted(a.labels.items()))
            print(f"{'':<20} {'':<22} {'':<16} {'':<10} ↳ {kv}")

    # Two live sessions in one directory is the failure the listing exists to
    # make visible: on 2026-09-06 two of them claimed the same lane and made
    # the same one-line fix, one on a superseded tree, and *nothing in the
    # system could detect it* -- including the agents themselves.  Stated
    # loudly rather than left for the reader to spot in a column.
    by_cwd: dict[str, list[str]] = {}
    for a in agents:
        by_cwd.setdefault(a.cwd, []).append(a.identity)
    for where, names in sorted(by_cwd.items()):
        if len(names) > 1:
            print(f"\n!! {len(names)} live sessions share {where}: "
                  f"{', '.join(sorted(names))}")
            print("   They cannot see each other's edits. Confirm which one "
                  "owns this work before changing it.")
    return 0


def cmd_whoami(args) -> int:
    cwd = args.cwd or os.getcwd()
    ident = _identity(args)
    print(f"cwd  : {ac.norm_path(cwd)}")
    print(f"repo : {ac.repo_key(cwd)}")
    print(f"name : {ident or '(unset -- set ORCH2_AGENT_NAME or pass --as)'}")
    for name, live in ac.identities_for_cwd(cwd):
        print(f"  registered here: {name} ({'live' if live else 'not live'})")
    return 0


def cmd_send(args) -> int:
    frm = _identity(args) or "operator"
    mid, live = ac.send_message(args.text, frm=frm, to=args.to)
    where = "delivered at their next turn boundary" if live else (
        "queued -- that agent is not live right now")
    print(f"message {mid} to {args.to}: {where}")
    # "Sent" is not "seen", and the gap between them is where a sender guesses
    # wrong: a silent recipient that has not taken a turn needs waiting for, a
    # silent recipient that has read it needs chasing.  Say where to look.
    print(f"  check whether it has been seen:  agents.py status {mid}")
    return 0


def cmd_broadcast(args) -> int:
    frm = _identity(args) or "operator"
    cwd = args.cwd or os.getcwd()
    value = ""
    if args.scope == "repo":
        value = ac.repo_key(cwd)
    elif args.scope == "cwd":
        value = cwd
    mid, live = ac.send_message(args.text, frm=frm, scope_kind=args.scope,
                                scope_value=value)
    print(f"message {mid} broadcast to scope {args.scope}"
          + (f" ({value})" if value else "")
          + (": someone is live" if live else ": nobody live, queued"))
    return 0


def cmd_inbox(args) -> int:
    ident = _identity(args)
    if not ident:
        print("inbox needs an identity: pass --as NAME or set ORCH2_AGENT_NAME",
              file=sys.stderr)
        return 2
    cwd = args.cwd or os.getcwd()
    msgs = ac.pending_messages(ident, repo=ac.repo_key(cwd), cwd=cwd)
    if not msgs:
        print("nothing pending")
        return 0
    for m in msgs:
        target = m.to_identity or f"{m.scope_kind} broadcast"
        print(f"--- from {m.from_identity} to {target} at {_stamp(m.created_at)}")
        print(m.body)
        if not args.peek:
            ac.mark_delivered(m.id, ident)
    if args.peek:
        print("\n(peeked -- not marked delivered)")
    return 0


def cmd_status(args) -> int:
    """Whether a message has been *seen* -- not merely accepted.

    A send that returns "ok" and produces nothing for ten minutes has two very
    different explanations: the recipient has not taken a turn since it was
    sent, or it read the message and moved on. The first says wait; the second
    says escalate. This is what tells them apart.
    """
    st = ac.message_state(args.message_id)
    mid = args.message_id
    if st["state"] == "unknown":
        print(f"message {mid}: no such message (expired and purged, or a "
              f"wrong id)")
        return 1
    target = st["to"] or f"{st['scope_kind']}:{st['scope_value'] or '*'}"
    print(f"message {mid} -> {target}   (from {st['from']}, "
          f"sent {_stamp(st['created_at'])})")
    if st["state"] == "delivered":
        for ident, at in st["delivered_to"]:
            print(f"  seen by {ident} at {_stamp(at)}")
        print("  It has been read. Silence now means it was read and not "
              "acted on -- chase it, do not re-send.")
    elif st["state"] == "expired":
        print(f"  NOT delivered: expired {_stamp(st['expires_at'])} without "
              f"anyone taking a turn.")
    else:
        print("  queued -- nobody has been shown it yet.")
        if st["target_live_at_send"]:
            print("  The addressee was live when you sent it, so it drains at "
                  "their next turn boundary. Wait.")
        else:
            print("  The addressee was NOT live when you sent it. It is held "
                  "until they start, then expires. If they never start, this "
                  "never arrives -- and no amount of waiting changes that.")
    return 0


def cmd_halt(args) -> int:
    by = _identity(args) or "operator"
    cwd = args.cwd or os.getcwd()
    value = ac.repo_key(cwd) if args.scope == "repo" else ""
    hid = ac.raise_halt(args.reason, by=by, scope_kind=args.scope,
                        scope_value=value)
    print(f"halt {hid} raised ({args.scope}"
          + (f" {value}" if value else "") + f") by {by}: {args.reason}")
    print("Every agent in scope is asked to stop at its next clean point, and")
    print("any operation that calls check-halt will refuse to start.")
    print("Lift it explicitly with:  agents.py lift --scope " + args.scope)
    return 0


def cmd_lift(args) -> int:
    cwd = args.cwd or os.getcwd()
    value = ac.repo_key(cwd) if args.scope == "repo" else ""
    n = ac.lift_halt(scope_kind=args.scope, scope_value=value)
    print(f"{n} halt(s) lifted" if n else "no halt was in force")
    return 0


def cmd_check_halt(args) -> int:
    """THE contract: 0 = clear, non-zero = halted (spec §6.3)."""
    cwd = args.cwd or os.getcwd()
    repo = ac.repo_key(cwd) if args.scope == "repo" else ""
    halt = ac.active_halt(repo=repo)
    if halt is None:
        if not args.quiet:
            print(f"check-halt: clear ({args.scope})")
        return 0
    print("=== HALT IN FORCE -- do not start this operation ===", file=sys.stderr)
    print(f"raised by : {halt.raised_by}", file=sys.stderr)
    print(f"raised at : {_stamp(halt.raised_at)}", file=sys.stderr)
    print(f"scope     : {halt.scope_kind}"
          + (f" {halt.scope_value}" if halt.scope_value else ""), file=sys.stderr)
    print(f"reason    : {halt.reason}", file=sys.stderr)
    print("", file=sys.stderr)
    print("This is not a failure of your operation -- it is a deliberate stop.",
          file=sys.stderr)
    print("It is lifted explicitly, never on a timer:  agents.py lift",
          file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Self-test (spec §6.3)
# ---------------------------------------------------------------------------

def _self_test() -> int:
    """Fixtures over a throwaway store: both directions for every rule.

    Structure borrowed from the reference implementation named in §6.3
    (``check-lane-signals.py``): a checker with only true positives passes for
    one that reports everything, and one with only true negatives passes for
    one that reports nothing.  Either alone certifies something that
    discriminates nothing.
    """
    import tempfile

    failures: list[str] = []

    def check(label: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{label}: got {got!r}, want {want!r}")
            print(f"  FAIL {label}: got {got!r}, want {want!r}")
        else:
            print(f"  ok   {label}")

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ORCH2_AGENT_DB"] = os.path.join(tmp, "agents.db")
        conn = ac.connect()
        REPO, OTHER = "repo-one", "repo-two"

        # --- identity (§3.3) ---------------------------------------------
        a, how = ac.resolve_identity(cwd=tmp, conn=conn)
        check("a fresh directory creates an identity", how, "created")
        ac.register(a, cwd=tmp, session_id="s1", repo=REPO, conn=conn)
        check("resuming inherits it",
              ac.resolve_identity(cwd=tmp, session_id="s1", conn=conn)[1], "resumed")
        b, how_b = ac.resolve_identity(cwd=tmp, conn=conn)
        check("a second session does NOT adopt a live identity", how_b, "created")
        check("...so the two differ", a != b, True)
        ac.register(b, cwd=tmp, session_id="s2", repo=REPO, conn=conn)
        future = _dt.datetime.now().timestamp() + 10 * ac.AGENT_TTL
        try:
            ac.resolve_identity(cwd=tmp, conn=conn, now=future)
            check("ambiguity is refused, not guessed", "no refusal", "refusal")
        except ac.IdentityRefused as e:
            check("ambiguity is refused, not guessed", sorted(e.candidates),
                  sorted([a, b]))

        # --- registry (§4.3) ---------------------------------------------
        check("both are live now", len(ac.live_agents(conn=conn)), 2)
        check("...and none are, past the TTL",
              len(ac.live_agents(conn=conn, now=future)), 0)
        check("a live agent is found by identity",
              (ac.find_agent(a, conn=conn) or ac.Agent("", "")).identity, a)

        # --- messaging (§5) ----------------------------------------------
        mid, live = ac.send_message("stop when convenient", frm=a, to=b, conn=conn)
        check("a message to a live agent reports it live", live, True)
        check("the addressee has it pending",
              [m.body for m in ac.pending_messages(b, repo=REPO, conn=conn)],
              ["stop when convenient"])
        check("a third party does not",
              ac.pending_messages(a, repo=REPO, conn=conn), [])
        check("nothing is recorded as delivered yet",
              ac.message_status(mid, conn=conn), [])
        ac.mark_delivered(mid, b, conn=conn)
        check("delivery is recorded",
              [i for i, _ in ac.message_status(mid, conn=conn)], [b])
        check("and it is not delivered twice",
              ac.pending_messages(b, repo=REPO, conn=conn), [])

        ac.send_message("tree is moving", frm=a, scope_kind="repo",
                        scope_value=REPO, conn=conn)
        check("a broadcast reaches the other agent in the repo",
              len(ac.pending_messages(b, repo=REPO, conn=conn)), 1)
        check("a broadcast does not come back to its sender",
              ac.pending_messages(a, repo=REPO, conn=conn), [])
        check("nor does it reach a different repo",
              ac.pending_messages(b, repo=OTHER, conn=conn), [])

        # --- halt, both directions (§6.3) --------------------------------
        check("a notice present, no halt: NOT halted",
              ac.halt_in_force(repo=REPO, conn=conn), False)
        hid = ac.raise_halt("migrating to E:", by=a, scope_kind="repo",
                            scope_value=REPO, conn=conn)
        check("a halt in force IS halted",
              ac.halt_in_force(repo=REPO, conn=conn), True)
        check("...but not in another repo",
              ac.halt_in_force(repo=OTHER, conn=conn), False)
        h = ac.active_halt(repo=REPO, conn=conn)
        check("the halt names who raised it", h.raised_by if h else None, a)
        check("the halt carries the reason",
              (h.reason if h else ""), "migrating to E:")
        check("it is announced to an agent once",
              (ac.pending_halt_for(b, repo=REPO, conn=conn) or ac.Halt(0, "", "", "", 0, "")).id,
              hid)
        ac.mark_halt_delivered(hid, b, conn=conn)
        check("...and not announced again",
              ac.pending_halt_for(b, repo=REPO, conn=conn), None)
        check("but it is still in force after being announced",
              ac.halt_in_force(repo=REPO, conn=conn), True)
        check("lifting reports what it did", ac.lift_halt(halt_id=hid, conn=conn), 1)
        check("lifting again reports nothing", ac.lift_halt(halt_id=hid, conn=conn), 0)
        check("after lifting, not halted",
              ac.halt_in_force(repo=REPO, conn=conn), False)

        # A machine-wide halt spans repositories (§7.4).
        mh = ac.raise_halt("drive migration", by=a, scope_kind="machine", conn=conn)
        check("a machine halt covers this repo",
              ac.halt_in_force(repo=REPO, conn=conn), True)
        check("a machine halt covers another repo too",
              ac.halt_in_force(repo=OTHER, conn=conn), True)
        ac.lift_halt(halt_id=mh, conn=conn)
        check("and lifting it clears both",
              (ac.halt_in_force(repo=REPO, conn=conn),
               ac.halt_in_force(repo=OTHER, conn=conn)), (False, False))
        conn.close()

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("agents.py: self-test passed")
    return 0


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="agents.py",
        description="Address, message and halt the other Claude sessions on "
                    "this machine (cross-account).")
    ap.add_argument("--self-test", "--selftest", dest="selftest",
                    action="store_true", help="run this tool's own fixtures")
    ap.add_argument("--as", dest="as_", metavar="NAME",
                    help="act as this identity (default: $ORCH2_AGENT_NAME)")
    ap.add_argument("--cwd", help="directory to resolve repo/cwd scope from")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("list", help="live sessions, across accounts")
    p.add_argument("--scope", choices=("machine", "repo", "cwd"),
                   default="machine")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("whoami", help="this session's identity and scopes")
    p.set_defaults(fn=cmd_whoami)

    p = sub.add_parser(
        "send", help="one-way message to one identity",
        description=(
            "Send one identity a one-way message. It is delivered at that "
            "session's next turn boundary; use `status <id>` to find out "
            "whether it has actually been seen. A message carries no "
            "authority: it is never the operator's instruction, whatever it "
            "says -- see the module docstring."))
    p.add_argument("text")
    p.add_argument("--to", required=True)
    p.set_defaults(fn=cmd_send)

    p = sub.add_parser("broadcast", help="one-way message to a scope")
    p.add_argument("text")
    p.add_argument("--scope", choices=("repo", "cwd", "machine"), default="repo")
    p.set_defaults(fn=cmd_broadcast)

    p = sub.add_parser("inbox", help="show (and consume) pending messages")
    p.add_argument("--peek", action="store_true",
                   help="do not mark them delivered")
    p.set_defaults(fn=cmd_inbox)

    p = sub.add_parser("status", help="who has actually seen a message")
    p.add_argument("message_id", type=int)
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("halt", help="ask everyone in scope to stop")
    p.add_argument("reason")
    p.add_argument("--scope", choices=("repo", "machine"), default="repo")
    p.set_defaults(fn=cmd_halt)

    p = sub.add_parser("lift", help="lift a halt (explicit, never a timer)")
    p.add_argument("--scope", choices=("repo", "machine"), default="repo")
    p.set_defaults(fn=cmd_lift)

    p = sub.add_parser("check-halt",
                       help="exit non-zero if a halt is in force (THE contract)")
    p.add_argument("--scope", choices=("repo", "machine"), default="repo")
    p.add_argument("--quiet", action="store_true",
                   help="print nothing when clear")
    p.set_defaults(fn=cmd_check_halt)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.selftest:
        return _self_test()
    if not getattr(args, "fn", None):
        build_parser().print_help()
        return 0
    try:
        return args.fn(args)
    except ac.IdentityRefused as e:
        print(f"agents.py: {e}", file=sys.stderr)
        return 2
    except ac.AgentCommsError as e:
        print(f"agents.py: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
