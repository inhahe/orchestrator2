"""copy_session_tui.py -- the full-screen textual wizards for copy_session.

Split out of ``copy_session.py`` so that importing the discovery/parsing half
costs nothing and, crucially, cannot fail.  ``copy_session.py`` has always
claimed its first half is "no TUI dependency -- importable & testable on its
own", but the ``from textual import ...`` at module scope made that untrue: on
an interpreter without ``textual`` installed, ``from copy_session import
discover_claude_dirs`` raised ImportError.  server.py calls that on every lobby
refresh inside a broad ``except Exception``, so the failure did not crash
anything -- it silently narrowed the lobby to a single account -- and ``/switch``
(no such guard) broke outright.  A missing TUI dependency should cost you the
TUI, and nothing else.

Everything here needs ``textual``.  Nothing in ``copy_session.py`` does.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from copy_session import (
    NO_HIDE_TITLE_PREFIX,
    _find_recent_for_cwd,
    _fmt_time,
    _projects_dir,
    _read_session_meta,
    _same_dir,
    copy_session_file,
    count_sessions,
    current_claude_dir,
    discover_claude_dirs,
    scan_sessions,
    sessions_with_id,
)

# ---------------------------------------------------------------------------
# TUI
# ---------------------------------------------------------------------------

from textual import work
from textual.app import App, ComposeResult
from textual.screen import Screen, ModalScreen
from textual.widgets import (
    Header, Footer, Static, ListView, ListItem, Label, Tree, Input, Button,
    LoadingIndicator,
)
from textual.containers import Vertical, Horizontal, Center


class _DirItem(ListItem):
    def __init__(self, path: Path, sess_count: int) -> None:
        super().__init__(
            Label(f"[b]{path.name}[/b]  [dim]{path}[/dim]  ·  {sess_count} sessions")
        )
        self.dir_path = path


class ClaudeDirScreen(Screen):
    """Pick a .claude directory."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, prompt: str, dirs: list[Path]) -> None:
        super().__init__()
        self._prompt = prompt
        self._dirs = dirs

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(self._prompt, id="prompt")
        if not self._dirs:
            yield Static(
                "[red]No .claude directories with sessions found.[/red]\n"
                "Press Esc to quit.",
                id="empty",
            )
        else:
            yield ListView(
                *[_DirItem(p, count_sessions(p)) for p in self._dirs]
            )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(Static).styles.margin = (1, 2)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(event.item.dir_path)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SessionScreen(Screen):
    """Pick a session from a tree grouped by project directory."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, claude_dir: Path, verb: str = "copy") -> None:
        super().__init__()
        self._claude_dir = claude_dir
        self._verb = verb

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(
            f"Sessions in [b]{self._claude_dir.name}[/b] — "
            f"select one to {self._verb} (Enter):",
            id="prompt",
        )
        # Scanning reads every session file (slow for big accounts), so it
        # runs in a worker thread; show a spinner + live count meanwhile.
        with Vertical(id="session-body"):
            yield LoadingIndicator(id="loading")
            yield Static("[dim]Scanning sessions…[/dim]", id="scan-status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#prompt", Static).styles.margin = (1, 2)
        self._scan()

    @work(thread=True, exclusive=True)
    def _scan(self) -> None:
        """Read session metadata off the UI thread, then build the tree."""
        import time

        status = self.query_one("#scan-status", Static)
        total = count_sessions(self._claude_dir)
        last = 0.0

        def progress(done: int) -> None:
            nonlocal last
            now = time.monotonic()
            # Throttle UI updates: only every ~100ms (and always the last one).
            if now - last >= 0.1 or done >= total:
                last = now
                self.app.call_from_thread(
                    status.update,
                    f"[dim]Scanning sessions… {done}/{total}[/dim]",
                )

        groups = scan_sessions(self._claude_dir, progress=progress)
        self.app.call_from_thread(self._populate, groups)

    def _populate(self, groups: list[dict[str, Any]]) -> None:
        body = self.query_one("#session-body", Vertical)
        try:
            self.query_one("#loading", LoadingIndicator).remove()
            self.query_one("#scan-status", Static).remove()
        except Exception:
            pass

        if not groups:
            body.mount(Static("[red]No sessions found here.[/red]"))
            return

        tree: Tree[Path] = Tree("projects")
        tree.root.expand()
        for g in groups:
            node = tree.root.add(
                f"[b]{g['label']}[/b]  [dim]({len(g['sessions'])})[/dim]",
                expand=True,
            )
            for s in g["sessions"]:
                node.add_leaf(
                    f"{_fmt_time(s['mtime'])}  {s['title']}  "
                    f"[dim]{s['id'][:8]}[/dim]",
                    data=s["path"],
                )
        body.mount(tree)
        self.call_after_refresh(tree.focus)

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        data = event.node.data
        if data is not None:
            self.dismiss(data)  # a jsonl Path

    def action_cancel(self) -> None:
        self.dismiss(None)


class NameScreen(Screen):
    """Enter the name (session id / filename stem) for the copy."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, default_name: str, dest_display: str) -> None:
        super().__init__()
        self._default = default_name
        self._dest_display = dest_display

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Vertical(
            Static(
                f"Copying to [b]{self._dest_display}[/b].\n\n"
                "Name for the copied session (its filename / resume id).\n"
                "[dim]Keep the original id for a byte-for-byte copy, or change "
                "it to mint a new resumable id.[/dim]",
                id="prompt",
            ),
            Input(value=self._default, id="name"),
            Horizontal(
                Button("Copy", variant="success", id="ok"),
                Button("Cancel", variant="error", id="cancel"),
                id="buttons",
            ),
            id="name-box",
        )
        yield Footer()

    def on_mount(self) -> None:
        inp = self.query_one(Input)
        inp.focus()

    def _submit(self) -> None:
        name = self.query_one(Input).value.strip()
        if not name:
            self.app.bell()
            return
        # Guard against path separators / illegal filename chars.
        if re.search(r'[\\/:*?"<>|]', name):
            self.query_one("#prompt", Static).update(
                "[red]Name can't contain \\ / : * ? \" < > | — try again.[/red]"
            )
            self.app.bell()
            return
        self.dismiss(name)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._submit()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen):
    """Yes/No modal (used for overwrite confirmation)."""

    BINDINGS = [
        ("escape", "no", "No"),
        ("n", "no", "No"),
        ("y", "yes", "Yes"),
    ]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        yield Center(
            Vertical(
                Static(self._message, id="confirm-msg"),
                Static(
                    "[dim]y = overwrite · n/Esc = cancel[/dim]",
                    id="confirm-hint",
                ),
                Horizontal(
                    Button("Yes", variant="warning", id="yes"),
                    Button("No", variant="primary", id="no"),
                    id="confirm-buttons",
                ),
                id="confirm-box",
            )
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class NoticeScreen(ModalScreen):
    """A single-button modal that shows a message until the user acknowledges.

    Used so a result (e.g. "copied into another account") stays on screen
    before the picker's own console window closes.
    """

    BINDINGS = [
        ("escape", "ok", "OK"),
        ("enter", "ok", "OK"),
        ("space", "ok", "OK"),
    ]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        yield Center(
            Vertical(
                Static(self._message, id="confirm-msg"),
                Static("[dim]Press Enter to continue[/dim]", id="confirm-hint"),
                Horizontal(
                    Button("OK", variant="primary", id="ok"),
                    id="confirm-buttons",
                ),
                id="confirm-box",
            )
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_ok(self) -> None:
        self.dismiss(None)


class FallbackOpenScreen(ModalScreen):
    """Ask what to open when a copy was cancelled/declined/cross-account.

    Dismisses with ``"recent"`` (open the most-recent session for the launch
    cwd — the default), ``"choose"`` (pick a session), or ``"none"`` (start a
    fresh, empty session).  When there's no recent session the "open last"
    option is omitted and the default becomes "choose".
    """

    BINDINGS = [
        ("escape", "none", "Fresh"),
        ("enter", "default", "Default"),
    ]

    def __init__(self, recent_title: str | None) -> None:
        super().__init__()
        self._recent_title = recent_title

    def compose(self) -> ComposeResult:
        buttons: list[Button] = []
        if self._recent_title:
            label = self._recent_title
            if len(label) > 44:
                label = label[:43] + "…"
            buttons.append(
                Button(f"Open last: {label}", variant="success", id="recent")
            )
        buttons.append(Button("Choose a session…", variant="primary", id="choose"))
        buttons.append(Button("Fresh session", id="none"))
        default_hint = "open last" if self._recent_title else "choose"
        yield Center(
            Vertical(
                Static(
                    "No session was opened. Open one now?", id="confirm-msg"
                ),
                Static(
                    f"[dim]Enter = {default_hint} · Esc = fresh session[/dim]",
                    id="confirm-hint",
                ),
                Vertical(*buttons, id="fallback-buttons"),
                id="confirm-box",
            )
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id)

    def action_none(self) -> None:
        self.dismiss("none")

    def action_default(self) -> None:
        self.dismiss("recent" if self._recent_title else "choose")


class CopyApp(App):
    TITLE = "Claude session copier"

    CSS = """
    #prompt { margin: 1 2; }
    #name-box { margin: 1 2; }
    #buttons { height: auto; margin-top: 1; }
    #buttons Button { margin-right: 2; }
    #confirm-box {
        width: 70; height: auto; padding: 1 2;
        border: round $warning; background: $surface;
    }
    #confirm-msg { margin-bottom: 1; }
    #confirm-buttons Button { margin-right: 2; }
    #fallback-buttons { height: auto; }
    #fallback-buttons Button { width: 100%; margin-bottom: 1; }
    Tree { margin: 0 2; }
    ListView { margin: 0 2; }
    #loading { height: 3; }
    #scan-status { margin: 0 2; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.result_summary: str | None = None

    def on_mount(self) -> None:
        self.run_worker(self._flow(), exclusive=True)

    async def _flow(self) -> None:
        dirs = discover_claude_dirs()

        src_dir = await self.push_screen_wait(
            ClaudeDirScreen("Copy FROM which account?", dirs)
        )
        if src_dir is None:
            self.exit()
            return

        src_session = await self.push_screen_wait(SessionScreen(src_dir))
        if src_session is None:
            self.exit()
            return

        dst_dir = await self.push_screen_wait(
            ClaudeDirScreen("Copy TO which account?", dirs)
        )
        if dst_dir is None:
            self.exit()
            return

        project_slug = src_session.parent.name
        new_name = await self.push_screen_wait(
            NameScreen(src_session.stem, dst_dir.name)
        )
        if new_name is None:
            self.exit()
            return

        dest_file = _projects_dir(dst_dir) / project_slug / f"{new_name}.jsonl"

        # Warn if this id already exists elsewhere under the destination
        # account (a different project slug).  Duplicate ids make
        # `claude --resume <id>` ambiguous.  The exact-path case is handled
        # by the overwrite confirm below, so exclude dest_file itself here.
        try:
            dest_resolved = dest_file.resolve()
        except OSError:
            dest_resolved = dest_file
        others = [
            p for p in sessions_with_id(dst_dir, new_name)
            if p.resolve() != dest_resolved
        ]
        if others:
            listed = "\n".join(f"  · {p.parent.name}" for p in others[:6])
            more = "" if len(others) <= 6 else f"\n  · … and {len(others) - 6} more"
            ok = await self.push_screen_wait(
                ConfirmScreen(
                    f"A session named [b]{new_name}[/b] already exists under "
                    f"[b]{dst_dir.name}[/b] in another project:\n{listed}{more}"
                    f"\n\nResuming by this id may pick the wrong one. "
                    f"Continue anyway?"
                )
            )
            if not ok:
                self.exit()
                return

        if dest_file.exists():
            ok = await self.push_screen_wait(
                ConfirmScreen(
                    f"[b]{dest_file}[/b]\n\nalready exists. Overwrite it?"
                )
            )
            if not ok:
                self.exit()
                return

        try:
            copy_session_file(src_session, dest_file, new_name)
        except OSError as exc:
            self.result_summary = f"ERROR: copy failed: {exc}"
            self.exit()
            return

        self.result_summary = (
            f"Copied '{src_session.stem}'  ->  {dest_file}\n"
            f"Resume it from the destination account with:\n"
            f"    claude --resume {new_name}"
        )
        self.exit()



# ---------------------------------------------------------------------------
# Launch-time picker — used by server.py for `--resume` / `--copy`
# ---------------------------------------------------------------------------


class LaunchPickerApp(App):
    """Full-screen picker shown at orchestrator2 startup.

    mode="resume": pick a project + session from the *current* account.
    mode="copy":   pick a source account + session, a destination account, and
                   a name; copy the session there.  When the destination is the
                   current account the copy is then resumed; otherwise it's just
                   copied (the current server can't resume another account).

    On success ``self.chosen_id`` holds the session id to resume and
    ``self.chosen_cwd`` the working directory that session belongs to (so the
    server can resume it under the right project — resume is cwd-scoped).
    """

    # Keep the no-hide prefix in the Textual title too: when Textual takes
    # over the terminal title it would otherwise overwrite the early
    # SetConsoleTitleW marker, and tray_minimizer re-checks the title on every
    # hide decision.
    TITLE = f"{NO_HIDE_TITLE_PREFIX} orchestrator2 — pick a session"

    CSS = CopyApp.CSS

    def __init__(
        self, mode: str, current_dir: Path, launch_cwd: str | None = None
    ) -> None:
        super().__init__()
        self.mode = mode
        self.current_dir = current_dir
        # The server's launch working directory, used to offer its most-recent
        # session as the default in the "what to open?" fallback.
        self.launch_cwd = launch_cwd
        self.chosen_id: str | None = None
        self.chosen_cwd: str | None = None
        self.status_message: str | None = None

    def on_mount(self) -> None:
        self.run_worker(self._flow(), exclusive=True)

    async def _flow(self) -> None:
        # The sub-flows only *set* chosen_id / status_message and return; a
        # single exit happens here so a cancelled/declined copy can fall
        # through to the "what to open?" fallback first.
        try:
            if self.mode == "copy":
                await self._copy_flow()
                if self.chosen_id is None and not self.status_message:
                    await self._offer_open_fallback()
            else:
                await self._resume_flow()
        finally:
            self.exit()

    async def _resume_flow(self) -> None:
        if count_sessions(self.current_dir) == 0:
            self.status_message = (
                f"No sessions found in {_projects_dir(self.current_dir)}"
            )
            return
        session = await self.push_screen_wait(
            SessionScreen(self.current_dir, verb="resume")
        )
        if session is None:
            return
        self.chosen_id = session.stem
        # Resume is cwd-scoped: the server must operate in the session's own
        # project directory or the CLI won't find it.  The picked session may
        # belong to a different project than the launch cwd, so hand its cwd
        # back for the server to adopt.
        self.chosen_cwd = _read_session_meta(session).get("cwd")

    async def _offer_open_fallback(self) -> None:
        """After a copy is cancelled/declined, ask what session to open.

        Offers the launch cwd's most-recent session as the default, a full
        picker, or a fresh empty session.  Leaves ``chosen_id`` ``None`` for
        the fresh-session choice (the server then starts with no resume).
        """
        recent = _find_recent_for_cwd(self.current_dir, self.launch_cwd)
        recent_title: str | None = None
        if recent is not None:
            recent_title = _read_session_meta(recent).get("title") or recent.stem
        choice = await self.push_screen_wait(FallbackOpenScreen(recent_title))
        if choice == "recent" and recent is not None:
            self.chosen_id = recent.stem
            self.chosen_cwd = (
                _read_session_meta(recent).get("cwd") or self.launch_cwd
            )
        elif choice == "choose":
            session = await self.push_screen_wait(
                SessionScreen(self.current_dir, verb="open")
            )
            if session is not None:
                self.chosen_id = session.stem
                self.chosen_cwd = _read_session_meta(session).get("cwd")
        # "none" (or a cancelled picker) leaves chosen_id None → fresh session.

    async def _copy_flow(self) -> None:
        dirs = discover_claude_dirs()

        # 1. Source account.
        src_dir = await self.push_screen_wait(
            ClaudeDirScreen("Copy a session FROM which account?", dirs)
        )
        if src_dir is None:
            return

        # 2. Source session.
        src_session = await self.push_screen_wait(
            SessionScreen(src_dir, verb="copy")
        )
        if src_session is None:
            return
        src_cwd = _read_session_meta(src_session).get("cwd")

        # 3. Destination account.
        dst_dir = await self.push_screen_wait(
            ClaudeDirScreen("Copy TO which account?", dirs)
        )
        if dst_dir is None:
            return

        # 4. Name for the copy.
        new_name = await self.push_screen_wait(
            NameScreen(src_session.stem, dst_dir.name)
        )
        if new_name is None:
            return

        project_slug = src_session.parent.name
        dest_file = _projects_dir(dst_dir) / project_slug / f"{new_name}.jsonl"
        try:
            dest_resolved = dest_file.resolve()
        except OSError:
            dest_resolved = dest_file
        src_resolved = src_session.resolve()

        # Warn if the id already exists elsewhere under the destination account.
        others = [
            p for p in sessions_with_id(dst_dir, new_name)
            if p.resolve() != dest_resolved
        ]
        if others:
            listed = "\n".join(f"  · {p.parent.name}" for p in others[:6])
            more = "" if len(others) <= 6 else f"\n  · … and {len(others) - 6} more"
            ok = await self.push_screen_wait(
                ConfirmScreen(
                    f"A session named [b]{new_name}[/b] already exists under "
                    f"[b]{dst_dir.name}[/b] in another project:\n{listed}{more}"
                    f"\n\nResuming by this id may pick the wrong one. "
                    f"Continue anyway?"
                )
            )
            if not ok:
                return

        # Overwrite guard (exact destination path already present).
        if dest_file.exists() and dest_resolved != src_resolved:
            ok = await self.push_screen_wait(
                ConfirmScreen(
                    f"[b]{dest_file}[/b]\n\nalready exists. Overwrite it?"
                )
            )
            if not ok:
                return

        # Perform the copy (skip if the source already IS the destination).
        if dest_resolved != src_resolved:
            try:
                copy_session_file(src_session, dest_file, new_name)
            except OSError as exc:
                self.status_message = f"copy failed: {exc}"
                return

        # Resume only when the copy landed in THIS server's account; the server
        # can't resume a session under a different CLAUDE_CONFIG_DIR.  Even then,
        # ask first — the user may just want the copy made, not opened.  Any
        # path that leaves chosen_id unset falls through to the "what to open?"
        # fallback in _flow().
        if _same_dir(dst_dir, self.current_dir):
            open_now = await self.push_screen_wait(
                ConfirmScreen(
                    f"Copied to [b]{new_name}[/b] under "
                    f"[b]{self.current_dir.name}[/b].\n\nOpen it now?"
                )
            )
            if open_now:
                self.chosen_id = new_name
                self.chosen_cwd = src_cwd
            return

        # Different account: confirm the copy on-screen (the picker console
        # closes right after).  Nothing to resume here, so the fallback then
        # asks what this server should open.
        await self.push_screen_wait(
            NoticeScreen(
                f"Copied to [b]{dest_file}[/b]\n\n"
                f"That's a different account than this server "
                f"([b]{self.current_dir.name}[/b]), so it won't be resumed "
                f"here.\nRelaunch orchestrator2 with that account's "
                f"CLAUDE_CONFIG_DIR and --resume to open it."
            )
        )



def pick_session_for_launch(
    mode: str, launch_cwd: str | None = None
) -> dict[str, Any]:
    """Run the launch-time picker TUI and return the chosen result.

    *mode* is ``"resume"`` or ``"copy"``.  Returns
    ``{"session_id": str | None, "cwd": str | None}`` — ``session_id`` is
    ``None`` when the user cancels and chooses a fresh session (the server then
    starts empty).  ``cwd`` is the chosen session's working directory so the
    server can resume it under the correct (cwd-scoped) project.  *launch_cwd*
    is the server's working directory, used to offer its most-recent session as
    the default in the "what to open?" fallback.  Requires an interactive
    terminal.
    """
    app = LaunchPickerApp(mode, current_claude_dir(), launch_cwd)
    app.run()
    if app.status_message:
        print(app.status_message)
    return {"session_id": app.chosen_id, "cwd": app.chosen_cwd}


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="copy_session.py",
        description="Copy/resume a Claude session via a full-screen TUI.",
    )
    ap.add_argument(
        "--pick",
        choices=["resume", "copy"],
        help=(
            "Run the launch-time picker in the given mode and write the chosen "
            "session id to --out (used internally by server.py when it has no "
            "terminal of its own)."
        ),
    )
    ap.add_argument(
        "--out",
        metavar="FILE",
        help=(
            "Where --pick writes its result as JSON "
            "({\"session_id\": ..., \"cwd\": ...})."
        ),
    )
    ap.add_argument(
        "--cwd",
        metavar="DIR",
        help=(
            "The server's launch working directory, used to offer its "
            "most-recent session as the default in the cancel/fallback prompt."
        ),
    )
    args, _ = ap.parse_known_args()

    if args.pick:
        result = pick_session_for_launch(args.pick, args.cwd)
        if args.out:
            try:
                with open(args.out, "w", encoding="utf-8") as f:
                    json.dump(result, f)
            except OSError:
                pass
        return 0

    # Default: the standalone copy wizard.
    app = CopyApp()
    app.run()
    if app.result_summary:
        print(app.result_summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
