"""Typer CLI entrypoint for ship-log.

M1 wired the skeleton (``--version`` + ``hello``). M3 makes logging real: ``init``
creates ``.shiplog/`` and ``add`` appends a git-stamped entry. ``ls``/``show``/``brief``
land in later milestones.
"""

from __future__ import annotations

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
from .gitctx import GitContext
from .models import Entry, EntryType
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


if __name__ == "__main__":  # pragma: no cover
    app()
