"""One small JSON record per session, kept by the hub across its restarts.

The mechanics shared by the hub's per-session schedules -- a loop's wakeup
(``wakeup_store``) and a resume after a usage limit (``limit_resume``): where a
record lives, writing it atomically, reading it back only into the session that
wrote it, and moving it when the session's id changes.

**A record is keyed by the session's directory and id, and must sit in its own
slot.**  A record found under another session's name has been moved or edited,
and acting on it would run a turn in the wrong conversation, so it is ignored.
What a record *means*, and whether one is still good, is the caller's to say
(*valid*).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable


def _sanitize(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", s)


class RecordStore:
    """The records in the directory *dir_fn* names, each judged by *valid*.

    *dir_fn* is called each time rather than once, so the directory follows
    the account the hub adopts at its start, and a test can point it
    elsewhere.
    """

    def __init__(self, dir_fn: Callable[[], Path],
                 valid: Callable[[Any], bool]) -> None:
        self._dir_fn = dir_fn
        self._valid = valid

    def path_for(self, cwd: str, session_id: str) -> Path:
        """The file for *session_id* in *cwd*."""
        try:
            resolved = str(Path(cwd).resolve(strict=False))
        except OSError:
            resolved = cwd
        return self._dir_fn() / f"{_sanitize(resolved)}__{_sanitize(session_id)}.json"

    def save(self, cwd: str, session_id: str, fields: dict[str, Any]) -> None:
        """Write *fields* as the record for *session_id* in *cwd*, replacing
        any.  ``cwd``, ``session_id`` and ``saved_at`` are added.  Failures
        are swallowed: keeping a record must never break what it records."""
        path = self.path_for(cwd, session_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({**fields, "cwd": cwd, "session_id": session_id,
                           "saved_at": time.time()}, f)
            os.replace(tmp, path)
        except OSError:
            pass

    def load(self, cwd: str, session_id: str | None) -> dict | None:
        """The record for *session_id* in *cwd*, if there is a valid one."""
        if not session_id:
            return None
        try:
            rec = json.loads(self.path_for(cwd, session_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return rec if self._valid(rec) else None

    def clear(self, cwd: str, session_id: str | None) -> None:
        """Forget the record for *session_id* in *cwd*, if there is one."""
        if not session_id:
            return
        try:
            self.path_for(cwd, session_id).unlink()
        except (OSError, FileNotFoundError):
            pass

    def records(self) -> list[dict]:
        """Every valid record that sits in its own slot.  One unreadable file
        costs only itself."""
        try:
            paths = sorted(self._dir_fn().glob("*.json"))
        except OSError:
            return []
        out = []
        for path in paths:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue
            if not self._valid(rec):
                continue
            if self.path_for(rec["cwd"], rec["session_id"]).name != path.name:
                continue
            out.append(rec)
        return out

    def rekey(self, cwd: str, old_id: str | None, new_id: str | None) -> bool:
        """Move *old_id*'s record to *new_id*: the same session, under the id
        its CLI now gives it (the rolling context trim starts a new
        transcript).  Left under the old id, the record would be cleared by
        nothing -- every clear names the current id -- and acted on at the
        next hub start, in the transcript the session has left.  True if one
        was moved."""
        if not old_id or not new_id or old_id == new_id:
            return False
        rec = self.load(cwd, old_id)
        if rec is None:
            return False
        fields = {k: v for k, v in rec.items()
                  if k not in ("cwd", "session_id", "saved_at")}
        self.save(cwd, new_id, fields)
        self.clear(cwd, old_id)
        return True
