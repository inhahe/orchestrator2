"""Cross-account agent identity, registry, messaging and halt.

Implements ``D:/visual studio projects/specs/agent-comms-spec.md``.

**The defect this exists to fix.** Several Claude Code sessions work one machine
and one repository at once, and cannot address each other at all, because the
session registry is scoped per ``CLAUDE_CONFIG_DIR`` and the sessions run under
different accounts.  ``ListAgents`` in one reports "no other Claude session is
running on this machine" while two others are demonstrably working.  So the
store here is deliberately **machine-wide and outside any config directory** --
that scoping *is* the bug (spec §4.1).

**What the spec insists on, and why each shows up below.**

* *Identity is assigned, not derived* (§3.1).  Every derivation breaks in some
  topology: by account, when two agents share one account; by cwd, when two
  share a directory; by pid, when a session restarts.  So it is a name, like a
  hostname, and :func:`resolve_identity` **refuses rather than guesses** when a
  fresh session cannot tell which of several identities it is (§3.3).  A wrong
  guess inherits another agent's queue and halt state, and neither agent can
  detect it -- the wrong one is told to stop and the right one never is, a
  failure with no symptom.
* *Delivery, not transport, is the hard part* (§1).  A file-based scheme already
  worked and still failed, because nothing delivered it: a message sat in an
  agent's own working tree for two days and still cost it a 7783-second wasted
  job.  Hence :func:`pending_messages` is consumed at a **turn boundary** by the
  bridge, and every delivery is recorded (§5.3) so a sender can tell an ignored
  message from an undelivered one.
* *Halt is not a message* (§6).  Informational input gets deprioritised by an
  agent mid-task, so the enforcement primitive is machine-readable shared state
  with a hard side: an expensive operation calls
  :func:`halt_in_force` before starting and refuses.  The system never tries to
  classify which processes are expensive (§6.2) -- the operation that is about
  to spend three hours is the one that knows, and it also knows where its safe
  abort points are.
* *A halt is lifted explicitly, never on a timer* (§6.4).  One that expired on
  its own would clear itself in the middle of the maintenance it protects.
  Note the deliberate asymmetry: registrations and messages **do** expire,
  because a crashed session that stayed listed forever would make identity
  auto-adoption unsafe (§4.3).

Storage is SQLite in WAL mode: concurrent writers from different accounts are a
hard requirement (§7.1), and this is the option that gets that right without a
daemon to supervise.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

__all__ = [
    "AgentCommsError", "IdentityRefused", "Agent", "Message", "Halt",
    "db_path", "connect", "repo_key", "norm_path",
    "resolve_identity", "register", "heartbeat", "deregister",
    "find_agent", "live_agents", "identities_for_cwd",
    "message_state",
    "send_message", "pending_messages", "mark_delivered", "message_status",
    "raise_halt", "lift_halt", "active_halt", "halt_in_force",
    "pending_halt_for", "mark_halt_delivered",
    "AGENT_TTL", "HEARTBEAT_INTERVAL", "MESSAGE_TTL",
    "mirror_peer_registries", "unmirror_all",
]

#: Seconds after its last heartbeat that a registration is considered dead.
#: Deliberately a small multiple of HEARTBEAT_INTERVAL: too tight and a busy
#: session flickers out of the registry mid-turn, too loose and a crashed one
#: blocks identity auto-adoption for minutes (§4.3).
AGENT_TTL = 90.0
HEARTBEAT_INTERVAL = 30.0

#: How long an undelivered message waits for an agent that is restarting (§5.3).
#: Long enough to outlive a restart, short enough that a week-old "stop now" is
#: not delivered to a session that has no idea what it refers to.
MESSAGE_TTL = 24 * 3600.0

_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class AgentCommsError(Exception):
    """Base class for this module's refusals."""


class IdentityRefused(AgentCommsError):
    """Raised when a session cannot tell which identity it is (spec §3.3).

    Carries the candidates so the caller can print them and ask.  This is a
    *refusal*, not a failure: the alternative is silently adopting another
    agent's identity, which is undetectable by either party.
    """

    def __init__(self, cwd: str, candidates: list[str]) -> None:
        self.cwd = cwd
        self.candidates = candidates
        super().__init__(
            f"several agent identities are registered for {cwd} and no name was "
            f"given: {', '.join(candidates)}. Re-launch with --agent-name <one "
            f"of those> (or a new name). Refusing to guess: adopting the wrong "
            f"identity would inherit another agent's messages and halt state, "
            f"and neither session could tell."
        )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def db_path() -> Path:
    """Where the registry lives.

    **Machine-wide, and deliberately not under any ``CLAUDE_CONFIG_DIR``** --
    per-account scoping is the defect the whole feature exists to fix (§4.1).
    ``ORCH2_AGENT_DB`` overrides it, which is how the tests get an isolated
    store without touching the real one.
    """
    override = os.environ.get("ORCH2_AGENT_DB")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else (Path.home() / ".local" / "share")
    return root / "orchestrator2" / "agents.db"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    identity     TEXT PRIMARY KEY,
    session_id   TEXT,
    cwd          TEXT NOT NULL,
    repo         TEXT,
    branch       TEXT,
    account      TEXT,
    pid          INTEGER,
    host         TEXT,
    started_at   REAL NOT NULL,
    heartbeat_at REAL NOT NULL,
    labels       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS agents_cwd  ON agents(cwd);
CREATE INDEX IF NOT EXISTS agents_repo ON agents(repo);
CREATE INDEX IF NOT EXISTS agents_sid  ON agents(session_id);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    to_identity   TEXT,
    scope_kind    TEXT NOT NULL,
    scope_value   TEXT NOT NULL DEFAULT '',
    from_identity TEXT NOT NULL,
    body          TEXT NOT NULL,
    created_at    REAL NOT NULL,
    expires_at    REAL NOT NULL,
    -- Whether anyone the message was addressed to was up when it was sent.
    -- Recorded, not merely returned, because the question a sender asks later
    -- ("was this ever going to arrive?") is asked *later* -- see
    -- message_state().  0 = nobody live, 1 = at least one.
    target_live   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS messages_to ON messages(to_identity);

CREATE TABLE IF NOT EXISTS deliveries (
    message_id   INTEGER NOT NULL,
    identity     TEXT NOT NULL,
    delivered_at REAL NOT NULL,
    PRIMARY KEY (message_id, identity)
);

CREATE TABLE IF NOT EXISTS halts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_kind  TEXT NOT NULL,
    scope_value TEXT NOT NULL DEFAULT '',
    raised_by   TEXT NOT NULL,
    raised_at   REAL NOT NULL,
    reason      TEXT NOT NULL,
    lifted_at   REAL
);

CREATE TABLE IF NOT EXISTS halt_deliveries (
    halt_id      INTEGER NOT NULL,
    identity     TEXT NOT NULL,
    delivered_at REAL NOT NULL,
    PRIMARY KEY (halt_id, identity)
);

-- The name and labels a session was given explicitly (--agent-name,
-- --agent-label), by session id; '' / '{}' for whichever it was not given.
-- Not columns of ``agents``: that row is deleted on clean exit (§4.2), and an
-- operator's choice has to outlive it -- a session reopened from the lobby,
-- or after a hub restart, is the same agent.  See name_session().
CREATE TABLE IF NOT EXISTS session_names (
    session_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    named_at   REAL NOT NULL,
    labels     TEXT NOT NULL DEFAULT '{}'
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the store, creating it if needed.

    WAL plus a generous ``busy_timeout``: writers here are separate *processes*
    under different accounts (§7.1), so lock contention is normal operation
    rather than an error to surface.
    """
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        # A store on a filesystem without WAL support still works, just with
        # coarser locking.  Never a reason to refuse to start.
        pass
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(_SCHEMA)
    # Additive migration.  A store predating `target_live` is otherwise fine,
    # and refusing to open it would take the whole feature down over a column
    # whose absence only costs one field of one report.
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
        if "target_live" not in cols:
            conn.execute("ALTER TABLE messages ADD COLUMN target_live "
                         "INTEGER NOT NULL DEFAULT 0")
    except sqlite3.DatabaseError:
        log.warning("could not migrate the messages table", exc_info=True)
    # Likewise a session_names table from before labels were remembered.
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(session_names)")}
        if "labels" not in cols:
            conn.execute("ALTER TABLE session_names ADD COLUMN labels "
                         "TEXT NOT NULL DEFAULT '{}'")
    except sqlite3.DatabaseError:
        log.warning("could not migrate the session_names table", exc_info=True)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Attributes
# ---------------------------------------------------------------------------

def norm_path(p: str | os.PathLike[str] | None) -> str:
    """Canonical form for path comparison.

    Windows paths are case-insensitive and reachable by several spellings, so a
    cwd index keyed on the raw string would silently split one directory into
    several -- and section 3.3's "how many identities are registered for this
    cwd" would then answer wrongly in the direction that *guesses*.
    """
    if not p:
        return ""
    try:
        return os.path.normcase(str(Path(p).resolve()))
    except OSError:
        return os.path.normcase(str(p))


def repo_key(cwd: str | os.PathLike[str] | None) -> str:
    """A stable key identifying the repository *including all its worktrees*.

    Uses git's **common** directory, not the toplevel: the motivating case is
    several lanes in separate worktrees of one repository, which must share a
    halt scope.  ``--show-toplevel`` would give each worktree its own key and
    the broadcast would reach nobody.

    Falls back to the normalised cwd when this is not a repository, so a
    non-git directory still has a usable scope rather than an empty one that
    would collide with every other non-git directory.
    """
    if not cwd:
        return ""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(cwd), capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return norm_path(cwd)
    if out.returncode != 0:
        return norm_path(cwd)
    common = Path(out.stdout.strip())
    if not common.is_absolute():
        common = Path(cwd) / common
    return norm_path(common)


def _branch(cwd: str | os.PathLike[str] | None) -> str:
    if not cwd:
        return ""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(cwd), capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class Agent:
    identity: str
    cwd: str
    repo: str = ""
    branch: str = ""
    account: str = ""
    session_id: str = ""
    pid: int = 0
    host: str = ""
    started_at: float = 0.0
    heartbeat_at: float = 0.0
    labels: dict[str, Any] = field(default_factory=dict)

    def is_live(self, now: float | None = None, ttl: float = AGENT_TTL) -> bool:
        return (now or time.time()) - self.heartbeat_at <= ttl


@dataclass
class Message:
    id: int
    body: str
    from_identity: str
    to_identity: str | None
    scope_kind: str
    scope_value: str
    created_at: float


@dataclass
class Halt:
    id: int
    scope_kind: str
    scope_value: str
    raised_by: str
    raised_at: float
    reason: str


def _agent_from_row(r: sqlite3.Row) -> Agent:
    try:
        labels = json.loads(r["labels"] or "{}")
    except ValueError:
        labels = {}
    return Agent(
        identity=r["identity"], cwd=r["cwd"], repo=r["repo"] or "",
        branch=r["branch"] or "", account=r["account"] or "",
        session_id=r["session_id"] or "", pid=r["pid"] or 0,
        host=r["host"] or "", started_at=r["started_at"],
        heartbeat_at=r["heartbeat_at"],
        labels=labels if isinstance(labels, dict) else {},
    )


# ---------------------------------------------------------------------------
# Identity (spec §3)
# ---------------------------------------------------------------------------

def _valid_identity(name: str) -> bool:
    return bool(_IDENTITY_RE.match(name or ""))


def _suggest_identity(cwd: str, conn: sqlite3.Connection) -> str:
    """A readable, unused name derived from the directory, like a hostname.

    Derivation is fine *here* -- this only seeds a fresh name, which is then
    persisted and never re-derived.  What §3.1 forbids is deriving identity on
    every startup, which is what makes it break when the topology changes.
    """
    stem = Path(cwd).name or "agent"
    stem = re.sub(r"[^A-Za-z0-9._-]", "-", stem).strip("-") or "agent"
    stem = stem[:40]
    taken = {r["identity"] for r in conn.execute("SELECT identity FROM agents")}
    if stem not in taken:
        return stem
    for n in range(2, 1000):
        cand = f"{stem}-{n}"
        if cand not in taken:
            return cand
    return f"{stem}-{int(time.time())}"


def resolve_identity(
    *,
    cwd: str,
    session_id: str | None = None,
    explicit: str | None = None,
    conn: sqlite3.Connection | None = None,
    now: float | None = None,
    ttl: float = AGENT_TTL,
) -> tuple[str, str]:
    """Decide this session's identity, per the table in spec §3.3.

    Returns ``(identity, how)`` where *how* is one of ``explicit``,
    ``resumed``, ``adopted``, ``created`` -- the caller logs it, because
    "adopted" in particular is a decision the operator should be able to see
    after the fact (§3.3).

    Raises :class:`IdentityRefused` when several identities are registered for
    this directory and nothing says which one this is.
    """
    own = conn is None
    conn = conn or connect()
    now = now if now is not None else time.time()
    # Blank means "not given", not "invalid": `--agent-name ""` and an unset
    # ORCH2_AGENT_NAME must mean the same thing, or the flag and the variable
    # disagree about the same input.
    explicit = (explicit or "").strip() or None
    try:
        if explicit:
            if not _valid_identity(explicit):
                raise AgentCommsError(
                    f"invalid agent name {explicit!r}: use letters, digits, "
                    f"dot, dash or underscore (1-64 chars)")
            return explicit, "explicit"

        # A resumed session is unambiguous by definition: the prior session id
        # names exactly one registration.
        if session_id:
            row = conn.execute(
                "SELECT identity FROM agents WHERE session_id = ?",
                (session_id,)).fetchone()
            if row:
                return row["identity"], "resumed"

        key = norm_path(cwd)
        rows = conn.execute(
            "SELECT identity, heartbeat_at FROM agents WHERE cwd = ?",
            (key,)).fetchall()
        if not rows:
            return _suggest_identity(cwd, conn), "created"
        if len(rows) == 1:
            ident = rows[0]["identity"]
            if now - rows[0]["heartbeat_at"] <= ttl:
                # It is *live*: adopting it would put two sessions on one
                # identity, so this is a fresh agent in a shared directory.
                return _suggest_identity(cwd, conn), "created"
            return ident, "adopted"
        # Several, and nothing distinguishes us.  Refuse (§3.3).
        live = [r["identity"] for r in rows if now - r["heartbeat_at"] <= ttl]
        dead = [r["identity"] for r in rows if now - r["heartbeat_at"] > ttl]
        if len(dead) == 1 and not live:
            return dead[0], "adopted"
        raise IdentityRefused(cwd, [r["identity"] for r in rows])
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# Registry (spec §4)
# ---------------------------------------------------------------------------

def register(
    identity: str,
    *,
    cwd: str,
    session_id: str | None = None,
    account: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    pid: int | None = None,
    labels: dict[str, Any] | None = None,
    conn: sqlite3.Connection | None = None,
    now: float | None = None,
) -> Agent:
    """Self-register this session (§4.2) and start its heartbeat clock."""
    own = conn is None
    conn = conn or connect()
    now = now if now is not None else time.time()
    key = norm_path(cwd)
    rep = repo if repo is not None else repo_key(cwd)
    br = branch if branch is not None else _branch(cwd)
    try:
        prior = conn.execute(
            "SELECT started_at FROM agents WHERE identity = ?",
            (identity,)).fetchone()
        started = prior["started_at"] if prior else now
        conn.execute(
            "INSERT INTO agents (identity, session_id, cwd, repo, branch, "
            "account, pid, host, started_at, heartbeat_at, labels) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(identity) DO UPDATE SET "
            "session_id=excluded.session_id, cwd=excluded.cwd, "
            "repo=excluded.repo, branch=excluded.branch, "
            "account=excluded.account, pid=excluded.pid, host=excluded.host, "
            "heartbeat_at=excluded.heartbeat_at, labels=excluded.labels",
            (identity, session_id or "", key, rep, br, account or "",
             pid if pid is not None else os.getpid(), socket.gethostname(),
             started, now, json.dumps(labels or {})),
        )
        conn.commit()
        return Agent(identity=identity, cwd=key, repo=rep, branch=br,
                     account=account or "", session_id=session_id or "",
                     pid=pid if pid is not None else os.getpid(),
                     host=socket.gethostname(), started_at=started,
                     heartbeat_at=now, labels=dict(labels or {}))
    finally:
        if own:
            conn.close()


def heartbeat(identity: str, *, conn: sqlite3.Connection | None = None,
              now: float | None = None, session_id: str | None = None) -> bool:
    """Refresh liveness.  Returns False if the identity is not registered."""
    own = conn is None
    conn = conn or connect()
    now = now if now is not None else time.time()
    try:
        if session_id:
            cur = conn.execute(
                "UPDATE agents SET heartbeat_at = ?, session_id = ? "
                "WHERE identity = ?", (now, session_id, identity))
        else:
            cur = conn.execute(
                "UPDATE agents SET heartbeat_at = ? WHERE identity = ?",
                (now, identity))
        conn.commit()
        return cur.rowcount > 0
    finally:
        if own:
            conn.close()


def deregister(identity: str, *, session_id: str | None = None,
               conn: sqlite3.Connection | None = None) -> bool:
    """Remove a registration on clean exit (§4.2).

    Given *session_id*, only while the registration is still that session's
    (or has none yet).  Two sessions given the same explicit name share one
    row, and the first to close used to delete it out from under the other --
    found 2026-09-24, when an idle teardown left the live ``Lane-A`` session out
    of the registry: unaddressable, deaf to halts, and with nothing to say so.
    """
    own = conn is None
    conn = conn or connect()
    try:
        if session_id is None:
            cur = conn.execute("DELETE FROM agents WHERE identity = ?",
                               (identity,))
        else:
            cur = conn.execute(
                "DELETE FROM agents WHERE identity = ? AND session_id IN (?, '')",
                (identity, session_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        if own:
            conn.close()


def name_session(session_id: str, name: str | None, *,
                 labels: dict[str, str] | None = None,
                 conn: sqlite3.Connection | None = None,
                 now: float | None = None) -> None:
    """Remember the name and labels *session_id* was explicitly given.

    ``--agent-name`` / ``--agent-label`` describe the session their launch
    opens, not the hub; a session reopened later -- from the lobby, after a
    hub restart -- comes back without the flags, and this is how it is still
    the same agent.  Any non-blank name is kept, including one the registry
    itself would refuse (``"Lane A"``): it is also the name ``ListAgents``
    shows, which allows it.  A session given only labels is remembered with
    an empty name.
    """
    sid = (session_id or "").strip()
    nm = (name or "").strip()
    lab = {str(k): str(v) for k, v in (labels or {}).items() if str(k).strip()}
    if not sid or not (nm or lab):
        return
    own = conn is None
    conn = conn or connect()
    now = now if now is not None else time.time()
    try:
        conn.execute(
            "INSERT INTO session_names (session_id, name, named_at, labels) "
            "VALUES (?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
            "name=excluded.name, named_at=excluded.named_at, "
            "labels=excluded.labels",
            (sid, nm, now, json.dumps(lab, sort_keys=True)))
        conn.commit()
    finally:
        if own:
            conn.close()


def session_naming(session_id: str, *, conn: sqlite3.Connection | None = None
                   ) -> tuple[str | None, dict[str, str]]:
    """``(name, labels)`` *session_id* was explicitly given -- ``(None, {})``
    for a session that never was."""
    sid = (session_id or "").strip()
    if not sid:
        return None, {}
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute(
            "SELECT name, labels FROM session_names WHERE session_id = ?",
            (sid,)).fetchone()
    finally:
        if own:
            conn.close()
    if not row:
        return None, {}
    try:
        labels = json.loads(row["labels"] or "{}")
    except (TypeError, ValueError):
        labels = {}
    if not isinstance(labels, dict):
        labels = {}
    return (row["name"] or None), {str(k): str(v) for k, v in labels.items()}


def session_name(session_id: str, *,
                 conn: sqlite3.Connection | None = None) -> str | None:
    """The name *session_id* was explicitly given, if it ever was."""
    return session_naming(session_id, conn=conn)[0]


def find_agent(identity: str, *, conn: sqlite3.Connection | None = None
               ) -> Agent | None:
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute("SELECT * FROM agents WHERE identity = ?",
                           (identity,)).fetchone()
        return _agent_from_row(row) if row else None
    finally:
        if own:
            conn.close()


def live_agents(*, repo: str | None = None, cwd: str | None = None,
                conn: sqlite3.Connection | None = None,
                now: float | None = None, ttl: float = AGENT_TTL
                ) -> list[Agent]:
    """Every registration whose heartbeat is inside the TTL (§4.3, §4.4)."""
    own = conn is None
    conn = conn or connect()
    now = now if now is not None else time.time()
    try:
        sql = "SELECT * FROM agents WHERE heartbeat_at >= ?"
        args: list[Any] = [now - ttl]
        if repo is not None:
            sql += " AND repo = ?"
            args.append(repo)
        if cwd is not None:
            sql += " AND cwd = ?"
            args.append(norm_path(cwd))
        sql += " ORDER BY identity"
        return [_agent_from_row(r) for r in conn.execute(sql, args)]
    finally:
        if own:
            conn.close()


def identities_for_cwd(cwd: str, *, conn: sqlite3.Connection | None = None,
                       now: float | None = None, ttl: float = AGENT_TTL
                       ) -> list[tuple[str, bool]]:
    """``[(identity, live)]`` for a directory -- the §3.3 startup lookup."""
    own = conn is None
    conn = conn or connect()
    now = now if now is not None else time.time()
    try:
        rows = conn.execute(
            "SELECT identity, heartbeat_at FROM agents WHERE cwd = ? "
            "ORDER BY identity", (norm_path(cwd),)).fetchall()
        return [(r["identity"], now - r["heartbeat_at"] <= ttl) for r in rows]
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# Messaging (spec §5)
# ---------------------------------------------------------------------------

_SCOPES = ("identity", "cwd", "repo", "machine")


def send_message(
    body: str,
    *,
    frm: str,
    to: str | None = None,
    scope_kind: str = "identity",
    scope_value: str = "",
    conn: sqlite3.Connection | None = None,
    now: float | None = None,
    ttl: float = MESSAGE_TTL,
) -> tuple[int, bool]:
    """Queue a one-way message (§5.3).  Returns ``(message_id, target_live)``.

    *target_live* is reported back to the sender because §5.3 requires knowing
    whether the addressee was up: a message queued for a restarting agent is
    fine, but the sender should be able to tell that from a delivered one.
    """
    if scope_kind not in _SCOPES:
        raise AgentCommsError(
            f"unknown scope {scope_kind!r}: expected one of {', '.join(_SCOPES)}")
    if scope_kind == "identity" and not to:
        raise AgentCommsError("a direct message needs a target identity")
    own = conn is None
    conn = conn or connect()
    now = now if now is not None else time.time()
    try:
        if scope_kind == "identity":
            live = bool(live_agents(conn=conn, now=now)) and any(
                a.identity == to for a in live_agents(conn=conn, now=now))
        elif scope_kind == "machine":
            live = bool([a for a in live_agents(conn=conn, now=now)
                         if a.identity != frm])
        else:
            kw = {scope_kind: scope_value}
            live = bool([a for a in live_agents(conn=conn, now=now, **kw)
                         if a.identity != frm])
        cur = conn.execute(
            "INSERT INTO messages (to_identity, scope_kind, scope_value, "
            "from_identity, body, created_at, expires_at, target_live) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (to if scope_kind == "identity" else None, scope_kind,
             norm_path(scope_value) if scope_kind in ("cwd", "repo") else scope_value,
             frm, body, now, now + ttl, 1 if live else 0),
        )
        conn.commit()
        return int(cur.lastrowid or 0), live
    finally:
        if own:
            conn.close()


def pending_messages(identity: str, *, repo: str = "", cwd: str = "",
                     conn: sqlite3.Connection | None = None,
                     now: float | None = None) -> list[Message]:
    """Messages this agent has not yet been shown.

    A broadcast is never returned to its own sender: an agent telling everyone
    "I am about to move the tree" does not need to be told by itself.
    """
    own = conn is None
    conn = conn or connect()
    now = now if now is not None else time.time()
    try:
        rows = conn.execute(
            "SELECT m.* FROM messages m "
            "WHERE m.expires_at > ? "
            "  AND m.from_identity != ? "
            "  AND ( m.to_identity = ? "
            "        OR (m.to_identity IS NULL AND ( "
            "              m.scope_kind = 'machine' "
            "              OR (m.scope_kind = 'repo' AND m.scope_value = ?) "
            "              OR (m.scope_kind = 'cwd'  AND m.scope_value = ?) )) ) "
            "  AND NOT EXISTS (SELECT 1 FROM deliveries d "
            "                  WHERE d.message_id = m.id AND d.identity = ?) "
            "ORDER BY m.id",
            (now, identity, identity, norm_path(repo) if repo else "",
             norm_path(cwd) if cwd else "", identity),
        ).fetchall()
        return [Message(id=r["id"], body=r["body"],
                        from_identity=r["from_identity"],
                        to_identity=r["to_identity"],
                        scope_kind=r["scope_kind"], scope_value=r["scope_value"],
                        created_at=r["created_at"]) for r in rows]
    finally:
        if own:
            conn.close()


def mark_delivered(message_id: int, identity: str, *,
                   conn: sqlite3.Connection | None = None,
                   now: float | None = None) -> None:
    """Record that *identity* was shown this message (§5.3).

    Without this a sender cannot tell an ignored message from an undelivered
    one -- the exact ambiguity that made the repository's own request tracking
    unreliable.
    """
    own = conn is None
    conn = conn or connect()
    now = now if now is not None else time.time()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO deliveries (message_id, identity, "
            "delivered_at) VALUES (?,?,?)", (message_id, identity, now))
        conn.commit()
    finally:
        if own:
            conn.close()


def message_status(message_id: int, *, conn: sqlite3.Connection | None = None
                   ) -> list[tuple[str, float]]:
    """``[(identity, delivered_at)]`` -- who has actually seen it."""
    own = conn is None
    conn = conn or connect()
    try:
        return [(r["identity"], r["delivered_at"]) for r in conn.execute(
            "SELECT identity, delivered_at FROM deliveries "
            "WHERE message_id = ? ORDER BY delivered_at", (message_id,))]
    finally:
        if own:
            conn.close()


def message_state(message_id: int, *, conn: sqlite3.Connection | None = None,
                  now: float | None = None) -> dict[str, Any]:
    """Everything a sender needs to tell *not yet seen* from *seen and ignored*.

    Improvements-checklist item 4.  A send returned ``success: true`` and then
    nothing happened for several minutes; the recipients had not taken a turn
    since being restarted, and messages drain at the receiver's next tool
    round.  From the sender's side that was indistinguishable from having been
    read and deprioritised -- **and the two call for opposite responses**: wait,
    versus escalate.

    So the report separates the three facts that were previously one boolean:

    ``state``
        ``queued`` (nobody has been shown it), ``delivered`` (someone has), or
        ``expired`` (its TTL ran out before either).
    ``target_live_at_send``
        Whether anyone it was addressed to was up when it was sent.  A message
        queued for a restarting agent is fine; one queued for an agent that was
        never there is a different problem, and only this distinguishes them.
    ``delivered_to``
        Per-recipient, with timestamps.

    There is deliberately no ``read`` state beyond ``delivered``.  Delivery is
    observable -- the message was put in front of the model; comprehension is
    not, and a field claiming it would be the sort of unverifiable signal item 5
    warns against.
    """
    own = conn is None
    conn = conn or connect()
    now = now if now is not None else time.time()
    try:
        row = conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if row is None:
            return {"id": message_id, "state": "unknown", "delivered_to": []}
        try:
            live_at_send = bool(row["target_live"])
        except (IndexError, KeyError):       # pre-migration row
            live_at_send = False
        got = [(r["identity"], r["delivered_at"]) for r in conn.execute(
            "SELECT identity, delivered_at FROM deliveries "
            "WHERE message_id = ? ORDER BY delivered_at", (message_id,))]
        if got:
            state = "delivered"
        elif row["expires_at"] <= now:
            state = "expired"
        else:
            state = "queued"
        return {
            "id": message_id,
            "state": state,
            "from": row["from_identity"],
            "to": row["to_identity"],
            "scope_kind": row["scope_kind"],
            "scope_value": row["scope_value"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "target_live_at_send": live_at_send,
            "delivered_to": got,
        }
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# Halt (spec §6)
# ---------------------------------------------------------------------------

def raise_halt(reason: str, *, by: str, scope_kind: str = "repo",
               scope_value: str = "", conn: sqlite3.Connection | None = None,
               now: float | None = None) -> int:
    """Raise a halt.  Returns its id."""
    if scope_kind not in ("repo", "machine"):
        raise AgentCommsError(
            f"halt scope must be 'repo' or 'machine', not {scope_kind!r}")
    own = conn is None
    conn = conn or connect()
    now = now if now is not None else time.time()
    try:
        cur = conn.execute(
            "INSERT INTO halts (scope_kind, scope_value, raised_by, raised_at, "
            "reason, lifted_at) VALUES (?,?,?,?,?,NULL)",
            (scope_kind, norm_path(scope_value) if scope_kind == "repo" else "",
             by, now, reason))
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        if own:
            conn.close()


def lift_halt(*, halt_id: int | None = None, scope_kind: str | None = None,
              scope_value: str = "", conn: sqlite3.Connection | None = None,
              now: float | None = None) -> int:
    """Lift a halt explicitly (§6.4).  Returns how many were lifted.

    There is deliberately no expiry anywhere near this: a halt that timed out
    would clear itself in the middle of the maintenance it exists to protect.
    """
    own = conn is None
    conn = conn or connect()
    now = now if now is not None else time.time()
    try:
        if halt_id is not None:
            cur = conn.execute(
                "UPDATE halts SET lifted_at = ? WHERE id = ? AND lifted_at IS NULL",
                (now, halt_id))
        elif scope_kind is not None:
            cur = conn.execute(
                "UPDATE halts SET lifted_at = ? WHERE lifted_at IS NULL "
                "AND scope_kind = ? AND scope_value = ?",
                (now, scope_kind,
                 norm_path(scope_value) if scope_kind == "repo" else ""))
        else:
            cur = conn.execute(
                "UPDATE halts SET lifted_at = ? WHERE lifted_at IS NULL", (now,))
        conn.commit()
        return cur.rowcount
    finally:
        if own:
            conn.close()


def active_halt(*, repo: str = "", conn: sqlite3.Connection | None = None
                ) -> Halt | None:
    """The halt in force for *repo*, or a machine-wide one.  Oldest first.

    A machine-wide halt covers every repository: the motivating case is a drive
    migration, which spans them (§7.4).
    """
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute(
            "SELECT * FROM halts WHERE lifted_at IS NULL AND ("
            "  scope_kind = 'machine' "
            "  OR (scope_kind = 'repo' AND scope_value = ?)) "
            "ORDER BY raised_at LIMIT 1",
            (norm_path(repo) if repo else "",)).fetchone()
        if not row:
            return None
        return Halt(id=row["id"], scope_kind=row["scope_kind"],
                    scope_value=row["scope_value"], raised_by=row["raised_by"],
                    raised_at=row["raised_at"], reason=row["reason"])
    finally:
        if own:
            conn.close()


def halt_in_force(*, repo: str = "", conn: sqlite3.Connection | None = None
                  ) -> bool:
    """The hard check an expensive operation calls before starting (§6.2)."""
    return active_halt(repo=repo, conn=conn) is not None


def pending_halt_for(identity: str, *, repo: str = "",
                     conn: sqlite3.Connection | None = None) -> Halt | None:
    """An in-force halt this agent has not yet been shown.

    Delivered *once* per agent per halt, not once per turn: a halt stays up
    until lifted, and re-announcing it at every turn boundary would be noise
    the agent learns to skip -- which is precisely the failure mode §6 exists
    to avoid.
    """
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute(
            "SELECT h.* FROM halts h WHERE h.lifted_at IS NULL AND ("
            "  h.scope_kind = 'machine' "
            "  OR (h.scope_kind = 'repo' AND h.scope_value = ?)) "
            "AND NOT EXISTS (SELECT 1 FROM halt_deliveries d "
            "                WHERE d.halt_id = h.id AND d.identity = ?) "
            "ORDER BY h.raised_at LIMIT 1",
            (norm_path(repo) if repo else "", identity)).fetchone()
        if not row:
            return None
        return Halt(id=row["id"], scope_kind=row["scope_kind"],
                    scope_value=row["scope_value"], raised_by=row["raised_by"],
                    raised_at=row["raised_at"], reason=row["reason"])
    finally:
        if own:
            conn.close()


def mark_halt_delivered(halt_id: int, identity: str, *,
                        conn: sqlite3.Connection | None = None,
                        now: float | None = None) -> None:
    own = conn is None
    conn = conn or connect()
    now = now if now is not None else time.time()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO halt_deliveries (halt_id, identity, "
            "delivered_at) VALUES (?,?,?)", (halt_id, identity, now))
        conn.commit()
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# Peer-registry mirror (addendum §1)
# ---------------------------------------------------------------------------
#
# The addendum is emphatic that messaging needs **no new tool and no
# documentation**: the description already shipped to every agent says
# ListAgents lists "other local Claude sessions on this machine", and an agent
# reached for it unprompted on 2026-09-06.  What failed was the implementation
# -- the call answered "no other Claude session is running on this machine"
# while two were demonstrably working.
#
# The cause, found by inspection rather than assumption: the CLI advertises
# itself in ``<CLAUDE_CONFIG_DIR>/sessions/<pid>.json`` (plus a
# ``<pid>.<hash>.key``), and peers then talk over a **machine-global** named
# pipe recorded in that file.  So the transport was never account-scoped --
# only the directory listing was.
#
# Mirroring each live session's advertisement into every other config
# directory therefore makes the existing tools simply start working, which is
# exactly what the addendum asks for: "if you find yourself writing 'agents
# should be told they can now message each other', the scope fix is
# incomplete."  Measured before and after on this machine: ListAgents went from
# 1 peer to 4, and a SendMessage from an account-c session to an account-b one
# was accepted for delivery.

#: Mirrored copies are recorded here so the reaper only ever removes files it
#: put there.  Deleting a *real* advertisement would unregister somebody else's
#: live session -- the one outcome worse than the bug being fixed.
MIRROR_MANIFEST = "mirrored-sessions.json"


def _config_dirs() -> list[Path]:
    """Every Claude config directory on this machine.

    Discovered rather than configured: a roster that has to be maintained goes
    stale, and a stale roster is indistinguishable from an accurate one until
    it misroutes something (§4.2 makes the same argument about the registry).
    """
    home = Path.home()
    out: list[Path] = []
    for p in sorted(home.glob(".claude*")):
        if p.is_dir() and (p / "sessions").is_dir():
            out.append(p)
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        e = Path(env)
        if e.is_dir() and e not in out:
            out.append(e)
    return out


def _manifest_path() -> Path:
    return db_path().parent / MIRROR_MANIFEST


def _load_manifest() -> dict[str, str]:
    try:
        with open(_manifest_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_manifest(m: dict[str, str]) -> None:
    p = _manifest_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(m, fh)
        os.replace(tmp, p)
    except OSError:
        pass


def _pid_is_a_live_claude(pid: int) -> bool:
    """True only for a *running claude process*.

    Both halves matter: pids are recycled, so "a process with this number
    exists" would keep a dead session advertised under a stranger's pid.
    """
    try:
        import psutil
        return psutil.pid_exists(pid) and "claude" in (
            psutil.Process(pid).name() or "").lower()
    except Exception:
        return False


def mirror_peer_registries(dirs: Iterable[Path] | None = None) -> tuple[int, int]:
    """Make every live session visible from every account.  ``(added, removed)``.

    Idempotent and safe to call on a timer.  Copies only *live* sessions, and
    removes only files this function previously wrote -- tracked in a manifest
    rather than inferred, because after a source disappears there is no way to
    tell a mirror from an original by inspection, and guessing wrong
    unregisters a working session.
    """
    dirs = list(dirs) if dirs is not None else _config_dirs()
    manifest = _load_manifest()
    added = removed = 0

    # --- reap first: a stale advertisement is worse than a missing one, since
    # --- a peer that cannot be reached looks like a hang rather than absence.
    for dest, src in list(manifest.items()):
        keep = os.path.exists(src)
        if keep and dest.endswith(".json"):
            try:
                with open(src, encoding="utf-8") as fh:
                    keep = _pid_is_a_live_claude(int(json.load(fh).get("pid", 0)))
            except (OSError, ValueError, TypeError):
                keep = False
        if not keep:
            try:
                os.unlink(dest)
                removed += 1
            except FileNotFoundError:
                pass
            except OSError:
                continue
            manifest.pop(dest, None)

    # --- then publish
    live: list[tuple[Path, Path]] = []      # (config_dir, session json)
    for d in dirs:
        for f in (d / "sessions").glob("*.json"):
            if str(f) in manifest:
                continue                     # a mirror, not an original
            try:
                with open(f, encoding="utf-8") as fh:
                    pid = int(json.load(fh).get("pid", 0))
            except (OSError, ValueError, TypeError):
                continue
            if _pid_is_a_live_claude(pid):
                live.append((d, f))

    for src_dir, src in live:
        stem = src.stem
        group = [src] + list((src_dir / "sessions").glob(f"{stem}.*.key"))
        for dest_dir in dirs:
            if dest_dir == src_dir:
                continue
            for g in group:
                dest = dest_dir / "sessions" / g.name
                if dest.exists():
                    continue
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(g, dest)
                except OSError:
                    continue
                manifest[str(dest)] = str(g)
                added += 1

    _save_manifest(manifest)
    return added, removed


def unmirror_all() -> int:
    """Remove every mirrored copy.  For turning the feature off cleanly."""
    manifest = _load_manifest()
    n = 0
    for dest in list(manifest):
        try:
            os.unlink(dest)
            n += 1
        except FileNotFoundError:
            pass
        except OSError:
            continue
        manifest.pop(dest, None)
    _save_manifest(manifest)
    return n
