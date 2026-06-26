"""Typer CLI entrypoint for ship-log.

M1 wired the skeleton (``--version`` + ``hello``). M3 made logging real (``init`` +
``add``). M4 added the read side: ``ls`` (filterable Rich table) and ``show <id>``
(full detail), both with ``--json``. M5 adds ``brief`` -- the token-efficient digest
an agent pastes into context before working.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console

from . import __version__, hooks
from .blame import blame as run_blame
from .blame import blame_to_dict, parse_target
from .brief import DEFAULT_BUDGET, brief_to_dict, build_brief
from .config import (
    CONFIG_FILENAME,
    Config,
    config_path_for_repo,
    default_config_text,
)
from .filters import filter_entries, parse_since, sort_newest_first
from .gitctx import GitContext, working_tree_files
from .models import Entry, EntryType
from .render import (
    blame_render,
    brief_markdown,
    empty_note,
    entries_table,
    entry_panel,
)
from .store import LOG_FILENAME, SHIPLOG_DIR, Store

app = typer.Typer(
    name="shiplog",
    help="A git-native captain's log for the multi-agent era. ⚓",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()
# Errors go to stderr so `--json`/piped stdout stays clean for agents.
err_console = Console(stderr=True)


def _fail(message: str, code: int = 1) -> None:
    """Print a friendly error to stderr and exit non-zero."""
    err_console.print(f"[bold red]error:[/bold red] {message}")
    raise typer.Exit(code)


def _resolve_repo_root() -> Path:
    """Return the repo root or exit with a friendly hint when outside git."""
    ctx = GitContext.capture()
    if ctx.repo_root is None:
        _fail(
            "not inside a git repository. Run shiplog from within your repo "
            "(or `git init` first)."
        )
    return ctx.repo_root  # type: ignore[return-value]


def _rel_to_repo(path: Path, repo_root: Path) -> str:
    """Render ``path`` relative to ``repo_root`` when possible (for tidy output).

    Falls back to the absolute path if ``path`` lives outside the repo (e.g. a
    ``core.hooksPath`` pointing elsewhere).
    """
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def _split_csv(value: str | None) -> list[str]:
    """Parse a comma-separated option into a clean list (drops blanks/dupes order-safe)."""
    if not value:
        return []
    seen: list[str] = []
    for part in value.split(","):
        item = part.strip()
        if item and item not in seen:
            seen.append(item)
    return seen


def _open_store_for_read() -> Store:
    """Resolve the repo's store for a read command, or exit with a friendly hint.

    Centralizes the "are we in a repo / has it been init'd" checks shared by
    ``ls`` and ``show`` so both fail identically and clearly.
    """
    repo_root = _resolve_repo_root()
    store = Store.for_repo(repo_root)
    if not store.exists():
        _fail('no ship-log here yet. Run [bold]shiplog init[/bold] first.')
    return store


def _find_by_id(entries: list[Entry], wanted: str) -> Entry | None:
    """Resolve an id to an entry: exact match first, then a unique prefix.

    Ids are case-insensitive here for ergonomics. An exact (case-insensitive)
    match always wins. Otherwise a single prefix hit resolves; multiple prefix
    hits raise :class:`LookupError` (ambiguous), and zero hits return ``None``.
    """
    q = wanted.strip().lower()
    for e in entries:
        if e.id.lower() == q:
            return e
    prefix_hits = [e for e in entries if e.id.lower().startswith(q)]
    if len(prefix_hits) == 1:
        return prefix_hits[0]
    if len(prefix_hits) > 1:
        ids = ", ".join(e.id for e in prefix_hits[:6])
        raise LookupError(f"id {wanted!r} is ambiguous; matches: {ids}")
    return None

_BANNER = r"""
      ___
     |   |  ship-log 🧭⚓
   __|___|__   the captain's log of your repo's voyage
  \_o_o_o_o_/  append-only · plain-text · agent-native
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"shiplog {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show the shiplog version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ship-log — log what's been tried so nobody re-litigates a dead-end."""


@app.command()
def hello(
    name: str = typer.Option(
        "sailor",
        "--name",
        "-n",
        help="Who to greet.",
    ),
) -> None:
    """Print a friendly banner — proof the install works."""
    console.print(_BANNER, style="cyan")
    console.print(f"Ahoy, {name}! ship-log v{__version__} is aboard. ⚓", style="bold")
    console.print(
        "Next stop: [bold]shiplog init[/bold], then [bold]shiplog add[/bold]. "
        "See PLAN.md for the voyage.",
        style="dim",
    )


@app.command()
def init(
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Rewrite config.toml even if it already exists.",
    ),
) -> None:
    """Create ``.shiplog/`` (log + config) in the current repo. Idempotent.

    Safe to run repeatedly: an existing log is never touched, and an existing
    config is left as-is unless ``--force`` is given.
    """
    repo_root = _resolve_repo_root()
    shiplog_dir = repo_root / SHIPLOG_DIR
    shiplog_dir.mkdir(parents=True, exist_ok=True)

    store = Store.for_repo(repo_root)
    log_created = not store.exists()
    if log_created:
        # Touch an empty log so the dir is a valid (if empty) store immediately.
        store.ensure_parent()
        store.path.touch()

    config_file = config_path_for_repo(repo_root)
    if config_file.exists() and not force:
        config_state = "kept"
    else:
        rewrote = config_file.exists()
        config_file.write_text(default_config_text(), encoding="utf-8")
        config_state = "rewritten" if rewrote else "created"

    rel = shiplog_dir.relative_to(repo_root)
    console.print(f"⚓ ship-log ready in [bold]{rel}/[/bold]")
    log_word = "created" if log_created else "present"
    console.print(f"  • {LOG_FILENAME}: [green]{log_word}[/green]")
    console.print(f"  • {CONFIG_FILENAME}: [green]{config_state}[/green]")
    if log_created:
        console.print(
            "Log a first decision: "
            '[bold]shiplog add decision "why we chose X" --why "..."[/bold]',
            style="dim",
        )


@app.command()
def add(
    type_: str = typer.Argument(
        ...,
        metavar="TYPE",
        help="One of: decision, attempt, deadend, note.",
    ),
    summary: str = typer.Argument(
        ...,
        help="One-line summary of the decision/attempt/dead-end/note.",
    ),
    why: str = typer.Option(
        "",
        "--why",
        "-w",
        help="The rationale — the whole point of the log.",
    ),
    files: str = typer.Option(
        "",
        "--files",
        help="Comma-separated paths this entry is about (a path may pin a line: file.py:40-80).",
    ),
    tags: str = typer.Option(
        "",
        "--tags",
        help="Comma-separated free-form labels for filtering.",
    ),
    ref: str = typer.Option(
        "",
        "--ref",
        help="Linked issue/PR reference (e.g. #42 or a URL).",
    ),
) -> None:
    """Append an entry to the log, auto-stamping git author/branch/sha/time.

    Run ``shiplog init`` first. The entry type is validated up front with a
    friendly error listing the valid set.
    """
    # Validate type early so a typo fails fast and clearly.
    try:
        entry_type = EntryType.coerce(type_)
    except ValueError as exc:
        _fail(str(exc))

    summary = summary.strip()
    if not summary:
        _fail("summary must not be empty.")

    ctx = GitContext.capture()
    if ctx.repo_root is None:
        _fail(
            "not inside a git repository. Run shiplog from within your repo "
            "(or `git init` first)."
        )

    store = Store.for_repo(ctx.repo_root)
    if not store.exists():
        _fail('no ship-log here yet. Run [bold]shiplog init[/bold] first.')

    # Config can override the author; otherwise use captured git author.
    config = Config.load(ctx.repo_root)
    author = config.author or ctx.author

    entry = Entry(
        summary=summary,
        type=entry_type,
        author=author,
        branch=ctx.branch,
        sha=ctx.sha,
        why=why.strip(),
        files=_split_csv(files),
        tags=_split_csv(tags),
        ref=ref.strip(),
    )
    store.append(entry)

    console.print(
        f"⚓ logged [bold cyan]{entry.type.value}[/bold cyan] "
        f"[dim]{entry.id}[/dim]: {entry.summary}"
    )
    if entry.why:
        console.print(f"  why: {entry.why}", style="dim")
    meta = entry.branch or "(no branch)"
    if entry.sha:
        meta += f" @ {entry.sha}"
    console.print(f"  {meta}", style="dim")


@app.command(name="ls")
def ls(
    type_: str = typer.Option(
        "",
        "--type",
        "-t",
        help="Only entries of this type (decision, attempt, deadend, note).",
    ),
    tag: str = typer.Option(
        "",
        "--tag",
        help="Only entries carrying this tag.",
    ),
    file: str = typer.Option(
        "",
        "--file",
        help="Only entries referencing this path (suffix match, e.g. cli.py).",
    ),
    since: str = typer.Option(
        "",
        "--since",
        help="Only entries at/after a time: relative (7d, 24h) or ISO date.",
    ),
    limit: int = typer.Option(
        0,
        "--limit",
        "-n",
        help="Show at most N entries (0 = no limit).",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Emit a JSON array instead of a table (for agents/pipes).",
    ),
) -> None:
    """List log entries newest-first, with optional filters.

    Filters are AND-combined. ``--type`` is validated up front; ``--since`` accepts
    a relative span (``7d``, ``24h``, ``2w``) or an ISO date/datetime. Use
    ``--json`` for a stable, parseable array (always emitted, even when empty).
    """
    if type_.strip():
        try:
            type_ = EntryType.coerce(type_).value
        except ValueError as exc:
            _fail(str(exc))

    since_dt = None
    if since.strip():
        try:
            since_dt = parse_since(since)
        except ValueError as exc:
            _fail(str(exc))

    store = _open_store_for_read()
    entries = filter_entries(
        store.read_all(),
        type_=type_,
        tag=tag,
        file=file,
        since=since_dt,
    )
    entries = sort_newest_first(entries)
    if limit and limit > 0:
        entries = entries[:limit]

    if as_json:
        payload = [e.to_dict() for e in entries]
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return

    if not entries:
        console.print(empty_note("no entries match. Try fewer filters, or shiplog add."))
        return

    count = len(entries)
    title = f"⚓ ship-log — {count} entr{'y' if count == 1 else 'ies'}"
    console.print(entries_table(entries, title=title))


@app.command()
def show(
    entry_id: str = typer.Argument(
        ...,
        metavar="ID",
        help="Entry id (full, or a unique prefix).",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Emit the entry as a JSON object instead of a panel.",
    ),
) -> None:
    """Show full detail for a single entry by id (or unique id prefix)."""
    store = _open_store_for_read()
    entries = store.read_all()
    try:
        entry = _find_by_id(entries, entry_id)
    except LookupError as exc:
        _fail(str(exc))

    if entry is None:
        _fail(f"no entry with id {entry_id!r}. Try [bold]shiplog ls[/bold] to find one.")

    if as_json:
        console.print_json(json.dumps(entry.to_dict(), ensure_ascii=False))
        return

    console.print(entry_panel(entry))


@app.command()
def brief(
    files: str = typer.Option(
        "",
        "--files",
        help="Comma-separated paths to focus on (default: the working tree).",
    ),
    limit: int = typer.Option(
        DEFAULT_BUDGET,
        "--limit",
        "-n",
        help=f"Max entries in the digest (default {DEFAULT_BUDGET}; 0 = no cap).",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Emit the digest as a JSON object instead of markdown.",
    ),
) -> None:
    """Print a token-efficient digest to drop into an agent's context.

    Leads with dead-ends (what NOT to redo), then decisions, prioritizing entries
    that touch files in your working tree -- or an explicit ``--files`` set. Kept
    short by ``--limit`` so it pastes straight into a prompt; ``--json`` emits a
    stable object (``entries`` ranked, plus ``focus``/``total``/``deadends``).
    """
    repo_root = _resolve_repo_root()
    store = Store.for_repo(repo_root)
    if not store.exists():
        _fail('no ship-log here yet. Run [bold]shiplog init[/bold] first.')

    # Explicit --files wins; otherwise focus on whatever's in the working tree so
    # the digest is automatically scoped to what the agent is about to touch. The
    # log's own .shiplog/ dir is filtered out -- it's storage, not a focus file.
    if files.strip():
        focus = _split_csv(files)
    else:
        focus = [
            f
            for f in working_tree_files(repo_root)
            if not f.rstrip("/").startswith(SHIPLOG_DIR)
        ]

    digest = build_brief(store.read_all(), focus=focus, budget=limit)

    if as_json:
        console.print_json(json.dumps(brief_to_dict(digest), ensure_ascii=False))
        return

    # Print the markdown verbatim (no Rich markup interpretation) so it's exactly
    # what lands in a prompt -- and clean when piped/redirected.
    print(brief_markdown(digest))


@app.command()
def blame(
    target: str = typer.Argument(
        ...,
        metavar="FILE[:LINE]",
        help="File (optionally :line or :start-end) to explain, e.g. shiplog/store.py:42.",
    ),
    limit: int = typer.Option(
        5,
        "--limit",
        "-n",
        help="Max matches to show (headline + alternates; 0 = no cap).",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Emit a JSON object instead of panels (for agents/pipes).",
    ),
) -> None:
    """Show the nearest logged decision/dead-end for a file line -- the "why" git blame lacks.

    Finds entries whose ``--files`` cover ``FILE`` and ranks them so the most
    line-relevant, most recent rationale leads, with the rest as alternates. Entries
    can pin a range by logging ``--files path:start-end``; a plain ``path`` covers
    the whole file. ``--json`` emits a stable object (``best`` + ``alternates``).
    """
    try:
        parsed = parse_target(target)
    except ValueError as exc:
        _fail(str(exc))

    store = _open_store_for_read()
    result = run_blame(store.read_all(), parsed, limit=limit)

    if as_json:
        console.print_json(json.dumps(blame_to_dict(result), ensure_ascii=False))
        return

    if result.best is None:
        where = parsed.path + (f":{parsed.line}" if parsed.line is not None else "")
        console.print(
            empty_note(
                f"no log entries touch {where}. "
                "Log one: shiplog add decision \"...\" --files "
                f"{parsed.path}"
                + (f":{parsed.line}" if parsed.line is not None else "")
                + "."
            )
        )
        return

    console.print(blame_render(result))


# -- hook subcommands ---------------------------------------------------------

hook_app = typer.Typer(
    name="hook",
    help="Manage the prepare-commit-msg nudge (reminds you to log decisions).",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(hook_app)


@hook_app.command("install")
def hook_install(
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite a pre-existing, non-ship-log prepare-commit-msg hook.",
    ),
) -> None:
    """Install the prepare-commit-msg nudge hook in this repo.

    The hook spots "interesting" commits (several files, or a decision-like
    message) and injects a *commented* reminder into the commit-message template.
    It never blocks a commit and never edits the final message. Idempotent: safe
    to run repeatedly. Won't clobber a foreign hook unless ``--force``.
    """
    repo_root = _resolve_repo_root()
    try:
        result = hooks.install(repo_root, force=force)
    except FileExistsError as exc:
        _fail(str(exc))
    except RuntimeError as exc:
        _fail(str(exc))

    rel = _rel_to_repo(result.hook_file, repo_root)
    if result.action == "unchanged":
        console.print(f"⚓ hook already installed at [bold]{rel}[/bold] (up to date).")
    else:
        verb = "installed" if result.action == "created" else "updated"
        console.print(f"⚓ prepare-commit-msg nudge [green]{verb}[/green] at [bold]{rel}[/bold].")
        console.print(
            "  It'll nudge you to [bold]shiplog add[/bold] on notable commits "
            "(never blocks). Remove with [bold]shiplog hook uninstall[/bold].",
            style="dim",
        )


@hook_app.command("uninstall")
def hook_uninstall() -> None:
    """Remove the ship-log nudge hook (surgical + reversible).

    Deletes the hook if it's purely ours, or strips just our block if you've added
    your own content alongside it. Leaves foreign hooks untouched.
    """
    repo_root = _resolve_repo_root()
    try:
        result = hooks.uninstall(repo_root)
    except RuntimeError as exc:
        _fail(str(exc))

    rel = _rel_to_repo(result.hook_file, repo_root)
    if result.action == "absent":
        console.print("no ship-log hook installed here — nothing to remove.", style="dim")
    elif result.action == "removed":
        console.print(f"⚓ removed the nudge hook ([bold]{rel}[/bold]).")
    else:  # stripped
        console.print(
            f"⚓ stripped the ship-log block from [bold]{rel}[/bold] "
            "(your other hook content was kept)."
        )


@hook_app.command("status")
def hook_status() -> None:
    """Report whether the ship-log nudge hook is installed in this repo."""
    repo_root = _resolve_repo_root()
    if hooks.status(repo_root):
        console.print("⚓ ship-log nudge hook: [green]installed[/green].")
    else:
        console.print(
            "ship-log nudge hook: [yellow]not installed[/yellow]. "
            "Add it with [bold]shiplog hook install[/bold]."
        )


@hook_app.command("_nudge", hidden=True)
def hook_nudge(
    msg_file: str = typer.Argument(..., help="Path to the commit message file (git $1)."),
    source: str = typer.Argument("", help="Commit source (git $2): message/merge/squash/..."),
) -> None:
    """Internal: invoked by the installed hook. Not for direct use.

    Appends a commented nudge to the commit-message file when the pending commit
    looks notable. Always exits 0 so it can never block a commit.
    """
    ctx = GitContext.capture()
    if ctx.repo_root is None:
        raise typer.Exit(0)
    threshold = Config.load(ctx.repo_root).hook_file_threshold
    try:
        hooks.run_nudge(ctx.repo_root, msg_file, source, file_threshold=threshold)
    except Exception:
        # Belt-and-suspenders: nothing here may ever fail a commit.
        pass
    raise typer.Exit(0)


if __name__ == "__main__":  # pragma: no cover
    app()
