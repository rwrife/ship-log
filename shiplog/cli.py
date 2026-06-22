"""Typer CLI entrypoint for ship-log.

M1 wired the skeleton (``--version`` + ``hello``). M3 made logging real (``init`` +
``add``). M4 adds the read side: ``ls`` (filterable Rich table) and ``show <id>``
(full detail), both with ``--json`` for agents. ``brief`` lands in M5.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console

from . import __version__
from .config import (
    CONFIG_FILENAME,
    Config,
    config_path_for_repo,
    default_config_text,
)
from .filters import filter_entries, parse_since, sort_newest_first
from .gitctx import GitContext
from .models import Entry, EntryType
from .render import empty_note, entries_table, entry_panel
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
        help="Comma-separated paths this entry is about.",
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


if __name__ == "__main__":  # pragma: no cover
    app()
