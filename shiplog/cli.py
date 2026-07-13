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
from . import guard as guard_hook
from . import mcp as mcp_server
from . import merge as merge_driver
from .ask import ask_to_dict, build_ask
from .blame import blame as run_blame
from .blame import blame_to_dict, parse_target
from .brief import DEFAULT_BUDGET, brief_to_dict, build_brief
from .config import (
    CONFIG_FILENAME,
    Config,
    config_path_for_repo,
    default_config_text,
)
from .export import (
    ADR,
    FORMATS,
    HTML,
    build_adr_set,
    render_changelog,
    render_html,
)
from .filters import filter_entries, parse_since, sort_newest_first
from .gitctx import GitContext, working_tree_files
from .links import links_for, make_link_summary, split_links
from .models import Entry, EntryType
from .render import (
    ask_render,
    blame_render,
    brief_markdown,
    empty_note,
    entries_table,
    entry_panel,
    stats_render,
)
from .stats import DEFAULT_TOP_N, compute_stats, stats_to_dict
from .store import LOG_FILENAME, SHIPLOG_DIR, Store
from .tui import run_tui
from .verify import Severity
from .verify import verify as run_verify

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


@app.command()
def link(
    entry_id: str = typer.Argument(
        ...,
        metavar="ID",
        help="Id (full or unique prefix) of the entry to attach a link to.",
    ),
    commit: str = typer.Option(
        "",
        "--commit",
        help="A commit sha this entry shipped in (e.g. abc1234).",
    ),
    pr: str = typer.Option(
        "",
        "--pr",
        help="A pull request this entry landed in (e.g. #42 or a URL).",
    ),
    ref: str = typer.Option(
        "",
        "--ref",
        help="Any other reference (a ticket, doc, or URL) tying back to this entry.",
    ),
    note: str = typer.Option(
        "",
        "--note",
        "-m",
        help="Optional human note describing the link.",
    ),
) -> None:
    """Attach a commit / PR / ref to an existing entry — after the fact.

    You logged a decision *before* the code existed; later it lands in a commit or
    PR. Rather than rewrite the past, ``link`` appends a tiny ``link`` record that
    points back at ``ID`` (append-only — the original entry line is never touched).
    ``shiplog show <ID>`` then surfaces a **Links** section, newest-first.

    Exactly one of ``--commit`` / ``--pr`` / ``--ref`` is required.
    """
    # Exactly one link kind — collect what was given so we can name the offender(s).
    provided = [
        (kind, value.strip())
        for kind, value in (("commit", commit), ("pr", pr), ("ref", ref))
        if value.strip()
    ]
    if not provided:
        _fail(
            "one of [bold]--commit[/bold] / [bold]--pr[/bold] / [bold]--ref[/bold] "
            "is required (what does this entry link to?)."
        )
    if len(provided) > 1:
        given = ", ".join(f"--{k}" for k, _ in provided)
        _fail(f"give exactly one of --commit / --pr / --ref, not several ({given}).")
    kind, value = provided[0]

    store = _open_store_for_read()
    entries = store.read_all()
    try:
        target = _find_by_id(entries, entry_id)
    except LookupError as exc:
        _fail(str(exc))
    if target is None:
        _fail(
            f"no entry with id {entry_id!r} to link. Try [bold]shiplog ls[/bold] to find one."
        )

    ctx = GitContext.capture()
    config = Config.load(ctx.repo_root) if ctx.repo_root is not None else None
    author = (config.author if config else "") or ctx.author

    link_entry = Entry(
        summary=make_link_summary(kind, value, note),
        type=EntryType.LINK,
        author=author,
        branch=ctx.branch,
        sha=ctx.sha,
        why=note.strip(),
        ref=value,
        link_target=target.id,
        link_kind=kind,
    )
    store.append(link_entry)

    console.print(
        f"\u2693 linked [dim]{target.id}[/dim] \u2192 "
        f"[bold cyan]{kind}[/bold cyan] {value}"
    )
    if note.strip():
        console.print(f"  note: {note.strip()}", style="dim")
    console.print(
        f"  see it on [bold]shiplog show {target.id}[/bold].", style="dim"
    )


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
    all_entries = store.read_all()
    # Link records annotate other entries (surfaced in `show`), so they don't
    # clutter the main table as standalone rows -- unless explicitly asked for
    # via `--type link`.
    if type_.strip() == EntryType.LINK.value:
        source = all_entries
    else:
        source, _links = split_links(all_entries)
    entries = filter_entries(
        source,
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

    # Aggregate any links pointing back at this entry (newest-first).
    _primary, link_records = split_links(entries)
    resolved_links = links_for(entry.id, link_records)

    if as_json:
        payload = entry.to_dict()
        payload["links"] = [lv.to_dict() for lv in resolved_links]
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return

    console.print(entry_panel(entry, links=resolved_links))


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


DEFAULT_ASK_LIMIT = 5


@app.command()
def ask(
    question: str = typer.Argument(
        ...,
        metavar="QUESTION",
        help='The question to search the log for, e.g. "have we tried Redis?".',
    ),
    type_: str = typer.Option(
        "",
        "--type",
        "-t",
        help="Only search entries of this type (decision, attempt, deadend, note).",
    ),
    file: str = typer.Option(
        "",
        "--file",
        help="Only search entries referencing this path (suffix match, e.g. cli.py).",
    ),
    since: str = typer.Option(
        "",
        "--since",
        help="Only search entries at/after a time: relative (7d, 24h) or ISO date.",
    ),
    limit: int = typer.Option(
        DEFAULT_ASK_LIMIT,
        "--limit",
        "-n",
        help=f"Max matches to return (default {DEFAULT_ASK_LIMIT}; 0 = no cap).",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Emit a JSON object (verdict + scored, ranked hits) instead of text.",
    ),
) -> None:
    """Answer a specific question by lexically ranking matching log entries.

    Unlike ``brief`` (a fixed digest), ``ask`` retrieves the entries most relevant
    to *this* question -- pure local BM25-ish scoring, no LLM or network -- with
    dead-ends boosted and a one-line verdict ('Yes -- 2 dead-ends, 1 decision')
    for fast agent parsing. ``--type``/``--file``/``--since`` narrow the corpus;
    ``--json`` emits a stable, scored object.
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
    source, _links = split_links(store.read_all())
    entries = filter_entries(
        source,
        type_=type_,
        file=file,
        since=since_dt,
    )
    result = build_ask(entries, question, limit=limit)

    if as_json:
        console.print_json(json.dumps(ask_to_dict(result), ensure_ascii=False))
        return

    console.print(ask_render(result))


@app.command()
def stats(
    since: str = typer.Option(
        "",
        "--since",
        help="Only entries at/after a time: relative (7d, 24h, 4w) or ISO date.",
    ),
    top: int = typer.Option(
        DEFAULT_TOP_N,
        "--top",
        help=f"Rows per top-files/tags/authors list (default {DEFAULT_TOP_N}; 0 = all).",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Emit a JSON object of the figures instead of the dashboard.",
    ),
) -> None:
    """Summarize the whole log: totals, dead-end ratio, activity, and hotspots.

    The bird's-eye companion to ``brief``: totals by type, the **dead-end ratio**
    (deadends / decisions+attempts -- the 'how much did we thrash' number), recent
    activity (last 7/30 days + a per-week sparkline), and the top files / tags /
    authors. ``--since`` reuses the same time parsing as ``ls``/``brief`` (relative
    ``7d``/``24h`` or an ISO date); ``--json`` emits a stable object with the same
    figures (``by_type``, ``deadend_ratio``, ``recent``, ``per_week``, ``top_*``,
    ``first_ts``/``last_ts``). An empty log prints a friendly note (exit 0).
    """
    since_dt = None
    if since.strip():
        try:
            since_dt = parse_since(since)
        except ValueError as exc:
            _fail(str(exc))

    store = _open_store_for_read()
    entries = filter_entries(store.read_all(), since=since_dt)
    summary = compute_stats(entries, top_n=top)

    if as_json:
        console.print_json(json.dumps(stats_to_dict(summary), ensure_ascii=False))
        return

    if summary.is_empty:
        hint = (
            "no entries in that window."
            if since.strip()
            else "no entries yet -- log one with shiplog add."
        )
        console.print(empty_note(hint))
        return

    console.print(stats_render(summary))


@app.command()
def verify(
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Also fail on warnings (currently: non-monotonic timestamps).",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Emit structured findings (line, id, code) for agents/CI.",
    ),
) -> None:
    """Validate log integrity — a fast, read-only CI gate against corruption.

    Walks every line of ``.shiplog/log.jsonl`` and flags: unparseable JSON,
    non-object lines, missing required fields, unknown ``type``, duplicate ``id``,
    a ``schema_version`` this CLI can't read, and dangling ``link``/``ack``/``fix``
    references. With ``--strict`` it additionally warns on non-monotonic ``ts``.

    Complements the merge driver (which unions/dedupes) by *catching* a bad append
    instead of masking it. Exit **0** when clean, **1** on any error (or any
    warning under ``--strict``); ``--json`` emits a stable object of findings.
    """
    store = _open_store_for_read()
    report = run_verify(store, strict=strict)

    if as_json:
        console.print_json(json.dumps(report.to_dict(), ensure_ascii=False))
        raise typer.Exit(0 if report.ok else 1)

    if not report.findings:
        console.print(
            f"⚓ log clean — [green]{report.checked}[/green] entr"
            f"{'y' if report.checked == 1 else 'ies'} checked, no problems found."
        )
        raise typer.Exit(0)

    for f in report.findings:
        colour = "red" if f.severity == Severity.ERROR else "yellow"
        tag = f.severity.value.upper()
        loc = f"line {f.line}"
        if f.id:
            loc += f" ({f.id})"
        console.print(
            f"[bold {colour}]{tag}[/bold {colour}] {loc} [dim]{f.code.value}[/dim]: {f.message}"
        )

    n_err = len(report.errors)
    n_warn = len(report.warnings)
    summary = f"{n_err} error{'s' if n_err != 1 else ''}"
    if n_warn:
        summary += f", {n_warn} warning{'s' if n_warn != 1 else ''}"
    verdict = "green]pass" if report.ok else "red]FAIL"
    console.print(
        f"\n{report.checked} checked — {summary}. Result: [bold {verdict}[/bold "
        f"{'green' if report.ok else 'red'}]."
    )
    raise typer.Exit(0 if report.ok else 1)


def _filtered_for_export(
    store: Store,
    *,
    type_: str,
    tag: str,
    since: str,
    keep_links: bool = False,
) -> list[Entry]:
    """Read + filter entries for ``export`` using the shared filter helpers.

    Reuses :func:`shiplog.filters.filter_entries` (same ``--type``/``--tag``/
    ``--since`` semantics as ``ls``) so export never forks filtering. Entries stay
    in **append order** (chronological) — export ordering (ADR numbering,
    changelog date grouping) depends on that, so we deliberately do *not*
    newest-first sort here.

    ``keep_links``: the HTML viewer surfaces ``link`` records *on their target
    entry*, so a ``--type`` filter (e.g. ``decision``) must not silently drop the
    links that annotate the surviving entries. When set, link records bypass the
    ``--type`` filter but still honor ``--tag``/``--since`` and are re-merged in
    append order; :func:`shiplog.export.render_html` only renders a link when its
    target survived, so orphaned links simply do not show.
    """
    since_dt = None
    if since.strip():
        try:
            since_dt = parse_since(since)
        except ValueError as exc:
            _fail(str(exc))

    type_q = type_.strip()
    if type_q:
        try:
            type_q = EntryType.coerce(type_q).value
        except ValueError as exc:
            _fail(str(exc))

    all_entries = store.read_all()
    filtered = filter_entries(
        all_entries,
        type_=type_q or None,
        tag=tag or None,
        since=since_dt,
    )

    if not keep_links or not type_q:
        # No type filter (or caller does not need links preserved): pass through.
        return filtered

    # A ``--type`` filter was applied and the caller wants links preserved. Re-add
    # any link records the type filter removed, in append order, so they can attach
    # to whichever primary entries survived. ``--tag``/``--since`` still apply.
    kept = {id(e) for e in filtered}
    links_only = filter_entries(
        [e for e in all_entries if e.type == EntryType.LINK],
        tag=tag or None,
        since=since_dt,
    )
    merged = list(filtered) + [e for e in links_only if id(e) not in kept]
    order = {id(e): i for i, e in enumerate(all_entries)}
    merged.sort(key=lambda e: order.get(id(e), 0))
    return merged


def _write_if_changed(path: Path, content: str) -> bool:
    """Write ``content`` to ``path`` only if it differs; return True if written.

    Idempotency guard: a re-export with no new entries produces byte-identical
    content, so we skip the write entirely (no mtime churn, clean git status).
    Parent dirs are created as needed.
    """
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


@app.command()
def export(
    fmt: str = typer.Argument(
        ...,
        metavar="FORMAT",
        help="What to render: 'adr' (file per decision), 'changelog' (digest), or 'html' (viewer).",
    ),
    out: str = typer.Option(
        "",
        "--out",
        "-o",
        help=(
            "Output path. adr: a directory (one NNNN-slug.md per decision). "
            "changelog: a file (omit to print to stdout). "
            "html: a file (default shiplog.html; '-' prints to stdout)."
        ),
    ),
    since: str = typer.Option(
        "",
        "--since",
        help="Only entries at/after a time: relative (7d, 24h, 4w) or ISO date.",
    ),
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
    title: str = typer.Option(
        "ship-log",
        "--title",
        help="html only: page <title>/heading for the generated viewer.",
    ),
) -> None:
    """Render the log to durable, human-facing artifacts (ADR / CHANGELOG / HTML).

    Unlike ``brief`` (ephemeral, agent-facing), ``export`` writes persistent files
    you commit and ship at milestones:

    * ``shiplog export adr --out docs/adr/`` — one classic ``NNNN-slug.md`` per
      *decision* entry (stable numbering from log order), a browsable decision
      archive.
    * ``shiplog export changelog --out CHANGELOG.shiplog.md`` (or stdout if
      ``--out`` is omitted) — a single digest grouping decisions + dead-ends by
      date.
    * ``shiplog export html`` — a single self-contained ``shiplog.html`` viewer
      (CSS + JS inlined, no CDN) with type badges and a client-side filter box,
      ideal for publishing the log to GitHub Pages so humans can browse it without
      installing the CLI. ``--out -`` prints the HTML to stdout.

    Reuses the ``ls`` filters (``--since``/``--type``/``--tag``). Output is
    deterministic: re-running with no new entries rewrites nothing (byte-identical,
    safe to commit). An empty selection prints a friendly note and exits 0 without
    writing partial files.
    """
    fmt_norm = fmt.strip().lower()
    if fmt_norm not in FORMATS:
        _fail(
            f"unknown export format {fmt!r}; expected one of: {', '.join(FORMATS)}."
        )

    store = _open_store_for_read()
    # HTML surfaces link records on their target entries, so keep links even when
    # a --type filter is applied (they only render if their target survives).
    entries = _filtered_for_export(
        store, type_=type_, tag=tag, since=since, keep_links=(fmt_norm == HTML)
    )

    if fmt_norm == ADR:
        _export_adr(entries, out)
    elif fmt_norm == HTML:
        _export_html(entries, out, title=title)
    else:  # CHANGELOG
        _export_changelog(entries, out)


def _export_adr(entries: list[Entry], out: str) -> None:
    """Write the ADR set to the ``--out`` directory (required for adr)."""
    if not out.strip():
        _fail(
            "adr export needs an output directory: "
            "[bold]shiplog export adr --out docs/adr/[/bold]."
        )

    files = build_adr_set(entries)
    if not files:
        console.print(
            empty_note(
                "no decision entries to export. "
                'Log one: shiplog add decision "..." --why "...".'
            )
        )
        return

    out_dir = Path(out)
    written = 0
    for name, content in files.items():
        if _write_if_changed(out_dir / name, content):
            written += 1

    total = len(files)
    unchanged = total - written
    rel = out_dir.as_posix().rstrip("/") + "/"
    console.print(
        f"\u2693 exported [bold]{total}[/bold] ADR file{'' if total == 1 else 's'} "
        f"to [bold]{rel}[/bold] "
        f"([green]{written} written[/green], [dim]{unchanged} unchanged[/dim])."
    )


def _export_changelog(entries: list[Entry], out: str) -> None:
    """Render the changelog digest to ``--out`` (or stdout when omitted)."""
    markdown = render_changelog(entries)

    if not out.strip():
        # Stdout path: print verbatim (no Rich markup) so redirects stay clean.
        print(markdown, end="")
        return

    out_path = Path(out)
    changed = _write_if_changed(out_path, markdown)
    state = "[green]written[/green]" if changed else "[dim]unchanged[/dim]"
    console.print(
        f"\u2693 exported changelog to [bold]{out_path.as_posix()}[/bold] ({state})."
    )


# Default filename for the HTML viewer when ``--out`` is omitted. A file (not
# stdout) is the sensible default here: the artifact is meant to be committed /
# published (e.g. to GitHub Pages), unlike the changelog which often pipes.
_DEFAULT_HTML_OUT = "shiplog.html"


def _export_html(entries: list[Entry], out: str, *, title: str) -> None:
    """Render the self-contained HTML viewer to a file (or stdout on ``--out -``).

    Unlike changelog, html defaults to writing a file (:data:`_DEFAULT_HTML_OUT`)
    since the viewer is a publishable artifact; pass ``--out -`` to stream it to
    stdout instead. Output is deterministic, so a re-export with no new entries is
    byte-identical and rewrites nothing.
    """
    document = render_html(entries, title=title)

    if out.strip() == "-":
        # Explicit stdout: print verbatim (no Rich markup) so redirects stay clean.
        print(document, end="")
        return

    out_path = Path(out.strip() or _DEFAULT_HTML_OUT)
    changed = _write_if_changed(out_path, document)
    state = "[green]written[/green]" if changed else "[dim]unchanged[/dim]"
    count = sum(1 for e in entries if e.type != EntryType.LINK)
    console.print(
        f"\u2693 exported HTML viewer ([bold]{count}[/bold] "
        f"entr{'y' if count == 1 else 'ies'}) to "
        f"[bold]{out_path.as_posix()}[/bold] ({state})."
    )


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


@app.command()
def fix(
    check: bool = typer.Option(
        False,
        "--check",
        help="Exit non-zero if the log has dupes or is out of order; write nothing (CI-friendly).",
    ),
    write: bool = typer.Option(
        False,
        "--write",
        help="Rewrite the log in canonical form (dedupe by id + stable sort by ts,id).",
    ),
) -> None:
    """Repair a mangled log: dedupe by id and stable-sort by (ts, id). Content-safe.

    The manual companion to the union merge driver, for logs that got duplicated or
    reordered *before* ``shiplog install-merge-driver`` was in place (e.g. a
    hand-resolved append-region conflict). It only ever changes *ordering* and
    removes exact ``id`` duplicates — an entry's content is never touched, and
    ``link`` records (their own unique ids) are preserved.

    Two modes:

    * ``--check`` — read-only audit. Exits **1** if the log has duplicate ids or
      isn't in canonical order (great in CI to catch a bad merge), **0** if it's
      already clean. Writes nothing.
    * ``--write`` — rewrite the log in canonical form. Idempotent: a clean log is
      left byte-identical (no mtime churn).

    With neither flag, does a dry run: reports what ``--write`` *would* change
    without touching the file (so you can preview before committing to it).
    """
    if check and write:
        _fail("pass only one of --check / --write (or neither for a dry run).")

    store = _open_store_for_read()
    # Read raw lines (not parsed entries) so malformed lines are preserved verbatim
    # rather than exploding — corruption should be surfaced, not lost.
    raw = store.path.read_text(encoding="utf-8") if store.path.exists() else ""
    result = merge_driver.normalize_text(raw)

    def _summary() -> str:
        bits = []
        if result.duplicates:
            bits.append(
                f"{result.duplicates} duplicate"
                f"{'' if result.duplicates == 1 else 's'}"
            )
        if result.reordered:
            bits.append("out-of-order entries")
        if result.malformed:
            bits.append(
                f"{result.malformed} unparseable line"
                f"{'' if result.malformed == 1 else 's'} (kept, pinned to end)"
            )
        return ", ".join(bits) if bits else "nothing to fix"

    if check:
        if result.is_clean:
            console.print("⚓ log is clean: no duplicates, correctly ordered. ✅")
            raise typer.Exit(0)
        _fail(f"log needs normalizing: {_summary()}. Run [bold]shiplog fix --write[/bold].")

    if not write:
        # Dry run: report the diff-in-spirit without writing.
        if result.is_clean:
            console.print(
                "⚓ log is already canonical — [bold]--write[/bold] would change nothing."
            )
        else:
            console.print(
                f"would normalize the log ({_summary()}). "
                "Re-run with [bold]--write[/bold] to apply."
            )
        return

    # --write path.
    if result.is_clean:
        console.print("⚓ log already canonical — [dim]unchanged[/dim].")
        return
    store.path.write_text(result.text, encoding="utf-8")
    console.print(
        f"⚓ normalized the log ([green]{_summary()}[/green]); "
        f"{result.line_count} line{'' if result.line_count == 1 else 's'} written."
    )
    if result.malformed:
        console.print(
            "  ⚠️  some lines couldn't be parsed and were kept as-is at the end — "
            "inspect them by hand.",
            style="yellow",
        )


@app.command()
def tui() -> None:
    """Open a full-screen, filterable browser of the log — the cozy way to explore.

    Scroll entries newest-first in a Rich table, filter live by free-text search
    (summary/why/tags/files) and by type (``t`` cycles deadend/decision/…), and
    read full rationale in a detail pane as you move the selection. Keyboard-first:
    ``/`` search, ``t``/``T`` cycle type, ``Esc`` clear, ``q`` quit. Reuses the same
    store/filters/rendering as ``ls``/``show`` (no logic fork).

    Needs the optional ``textual`` dependency: install the ``tui`` extra with
    ``pip install "ship-log[tui]"`` (a clear hint is printed if it's missing).
    """
    store = _open_store_for_read()
    repo_root = _resolve_repo_root()
    entries = store.read_all()
    if not entries:
        console.print(
            empty_note("the log is empty — nothing to browse yet. Try shiplog add.")
        )
        return
    raise typer.Exit(run_tui(entries, repo_label=repo_root.name))


@app.command()
def mcp() -> None:
    """Start a stdio MCP server exposing add/brief/ls as Model Context Protocol tools.

    Lets agents call ship-log natively (``shiplog_add`` / ``shiplog_brief`` /
    ``shiplog_ls``) instead of shelling out and scraping text — backed by the exact
    same store/ranking/filters as the CLI. The server speaks newline-delimited
    JSON-RPC on stdin/stdout and operates on the repo it's launched in, so point a
    client at a repo by setting that repo as the server's working directory.

    Runs until stdin closes (EOF). See the README "MCP server mode" section for a
    client install snippet.
    """
    raise typer.Exit(mcp_server.serve())


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


# -- guard subcommands (enforcing pre-commit dead-end tripwire) ----------------

guard_app = typer.Typer(
    name="guard",
    help="Enforcing pre-commit tripwire: block commits that re-touch open dead-ends.",
    invoke_without_command=True,
    add_completion=False,
)
app.add_typer(guard_app)


@guard_app.callback(invoke_without_command=True)
def guard_main(
    ctx: typer.Context,
    ack: str = typer.Option(
        "",
        "--ack",
        help="Acknowledge a dead-end by id (or unique prefix); it stops blocking.",
    ),
    note: str = typer.Option(
        "",
        "--note",
        "-m",
        help="Optional note recorded with the acknowledgement.",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="With no subcommand: emit the blocking dead-ends as JSON (for agents).",
    ),
) -> None:
    """Manage / run the enforcing dead-end guard.

    With a subcommand (``install`` / ``uninstall`` / ``status``) this manages the
    ``pre-commit`` hook. With ``--ack <id>`` it records an acknowledgement that
    clears one dead-end. With no subcommand and no ``--ack`` it *reports* the
    dead-ends that would block a commit of your currently staged files (exit 0
    here — the actual blocking happens in the ``_check`` hook entrypoint).
    """
    if ctx.invoked_subcommand is not None:
        return

    if ack.strip():
        _guard_ack(ack.strip(), note.strip())
        return

    # No subcommand, no --ack: report current blocks for the staged set.
    repo_root = _resolve_repo_root()
    store = Store.for_repo(repo_root)
    if not store.exists():
        _fail('no ship-log here yet. Run [bold]shiplog init[/bold] first.')
    entries = store.read_all()
    touched = set(guard_hook.staged_files(repo_root))
    blocks = guard_hook.blocking_deadends(entries, touched)

    if as_json:
        payload = {"blocking": [b.to_dict() for b in blocks], "count": len(blocks)}
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return

    if not blocks:
        console.print(
            "⚓ guard: [green]clear[/green] — no open dead-ends touch your staged files."
        )
        return
    _print_blocks(blocks)


def _print_blocks(blocks: list[guard_hook.Block]) -> None:
    """Render blocking dead-ends to stderr in a human-friendly, actionable form."""
    n = len(blocks)
    err_console.print(
        f"[bold red]\u2693 guard: {n} open dead-end{'' if n == 1 else 's'} "
        f"block this commit:[/bold red]"
    )
    for b in blocks:
        err_console.print(
            f"  [bold]{b.entry.id}[/bold] — {b.entry.summary}"
        )
        if b.entry.why:
            err_console.print(f"      why: {b.entry.why}", style="dim")
        err_console.print(f"      files: {', '.join(b.files)}", style="dim")
    err_console.print(
        "  Acknowledge with [bold]shiplog guard --ack <id>[/bold], "
        "or override once with [bold]SHIPLOG_GUARD=off[/bold] "
        "(or git commit --no-verify).",
        style="dim",
    )


def _guard_ack(entry_id: str, note: str) -> None:
    """Record an ``ack`` entry that clears the dead-end named by ``entry_id``."""
    store = _open_store_for_read()
    entries = store.read_all()
    try:
        target = _find_by_id(entries, entry_id)
    except LookupError as exc:
        _fail(str(exc))
    if target is None:
        _fail(
            f"no entry with id {entry_id!r} to acknowledge. "
            "Try [bold]shiplog ls --type deadend[/bold] to find one."
        )
    if target.type != EntryType.DEADEND:
        _fail(
            f"{target.id} is a [bold]{target.type.value}[/bold], not a dead-end. "
            "Only dead-ends can be acknowledged."
        )

    gctx = GitContext.capture()
    config = Config.load(gctx.repo_root) if gctx.repo_root is not None else None
    author = (config.author if config else "") or gctx.author

    ack_entry = Entry(
        summary=f"ack dead-end {target.id}: {target.summary}",
        type=EntryType.ACK,
        author=author,
        branch=gctx.branch,
        sha=gctx.sha,
        why=note,
        link_target=target.id,
    )
    store.append(ack_entry)
    console.print(
        f"\u2693 acknowledged dead-end [dim]{target.id}[/dim] — "
        "it will no longer block commits."
    )
    if note:
        console.print(f"  note: {note}", style="dim")


@guard_app.command("install")
def guard_install(
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Append the guard block to a pre-existing, non-ship-log pre-commit hook.",
    ),
) -> None:
    """Install the enforcing pre-commit guard hook in this repo.

    The hook blocks a commit whose staged files overlap any open (un-acknowledged)
    dead-end. Idempotent; won't clobber a foreign pre-commit hook unless
    ``--force`` (which appends our block, keeping yours). Override any single
    commit with ``SHIPLOG_GUARD=off`` or ``git commit --no-verify``.
    """
    repo_root = _resolve_repo_root()
    try:
        result = guard_hook.install(repo_root, force=force)
    except FileExistsError as exc:
        _fail(str(exc))
    except RuntimeError as exc:
        _fail(str(exc))

    rel = _rel_to_repo(result.hook_file, repo_root)
    if result.action == "unchanged":
        console.print(f"\u2693 guard already installed at [bold]{rel}[/bold] (up to date).")
    else:
        verb = "installed" if result.action == "created" else "updated"
        console.print(f"\u2693 pre-commit guard [green]{verb}[/green] at [bold]{rel}[/bold].")
        console.print(
            "  It'll [bold]block[/bold] commits that re-touch an open dead-end. "
            "Clear one with [bold]shiplog guard --ack <id>[/bold]; "
            "remove with [bold]shiplog guard uninstall[/bold].",
            style="dim",
        )


@guard_app.command("uninstall")
def guard_uninstall() -> None:
    """Remove the guard hook (surgical + reversible).

    Deletes the hook if it's purely ours, or strips just our block if you've added
    your own pre-commit content alongside it. Leaves foreign hooks untouched.
    """
    repo_root = _resolve_repo_root()
    try:
        result = guard_hook.uninstall(repo_root)
    except RuntimeError as exc:
        _fail(str(exc))

    rel = _rel_to_repo(result.hook_file, repo_root)
    if result.action == "absent":
        console.print("no ship-log guard installed here \u2014 nothing to remove.", style="dim")
    elif result.action == "removed":
        console.print(f"\u2693 removed the guard hook ([bold]{rel}[/bold]).")
    else:  # stripped
        console.print(
            f"\u2693 stripped the guard block from [bold]{rel}[/bold] "
            "(your other pre-commit content was kept)."
        )


@guard_app.command("status")
def guard_status() -> None:
    """Report whether the enforcing guard hook is installed in this repo."""
    repo_root = _resolve_repo_root()
    if guard_hook.status(repo_root):
        console.print("\u2693 ship-log guard: [green]installed[/green].")
    else:
        console.print(
            "ship-log guard: [yellow]not installed[/yellow]. "
            "Add it with [bold]shiplog guard install[/bold]."
        )


@guard_app.command("_check", hidden=True)
def guard_check() -> None:
    """Internal: invoked by the installed pre-commit hook. Not for direct use.

    Scans the staged files against open dead-ends and exits non-zero (2) when any
    block, printing an actionable report to stderr. Exits 0 when clear, when the
    log is absent/uninitialized, or when the env override is set. Any unexpected
    error degrades to exit 0 so the guard can never wedge a repo.
    """
    if guard_hook.guard_disabled_via_env():
        raise typer.Exit(0)
    try:
        gctx = GitContext.capture()
        if gctx.repo_root is None:
            raise typer.Exit(0)
        store = Store.for_repo(gctx.repo_root)
        if not store.exists():
            raise typer.Exit(0)
        entries = store.read_all()
        touched = set(guard_hook.staged_files(gctx.repo_root))
        blocks = guard_hook.blocking_deadends(entries, touched)
    except typer.Exit:
        raise
    except Exception:
        # Never wedge a commit on an internal error.
        raise typer.Exit(0) from None

    if not blocks:
        raise typer.Exit(0)
    _print_blocks(blocks)
    raise typer.Exit(2)


# -- merge driver -------------------------------------------------------------


@app.command(name="install-merge-driver")
def install_merge_driver(
    uninstall: bool = typer.Option(
        False,
        "--uninstall",
        help="Remove the merge driver (strip .gitattributes block + git config).",
    ),
    show_status: bool = typer.Option(
        False,
        "--status",
        help="Report whether the merge driver is installed; change nothing.",
    ),
) -> None:
    """Register the union merge driver so ``.shiplog/log.jsonl`` never conflicts.

    Two branches both appending to the log would otherwise hit a git append-region
    conflict. This wires a git *merge driver* that instead takes the **union** of
    both sides, dedupes by entry id, and stable-sorts — so merges are conflict-free
    and both branches converge on byte-identical output. It writes two things:

    * a committed ``.gitattributes`` rule (``.shiplog/log.jsonl merge=shiplog``) so
      collaborators inherit the routing, and
    * a per-clone ``.git/config`` entry defining the driver command.

    Idempotent and safe (mirrors ``shiplog hook install``): a foreign
    ``.gitattributes`` is never clobbered — only our fenced block is added/updated.
    Each collaborator runs this once per clone (the ``.git/config`` half isn't
    committed). Use ``--uninstall`` to remove it, ``--status`` to check.
    """
    if uninstall and show_status:
        _fail("pass only one of --uninstall / --status.")

    repo_root = _resolve_repo_root()

    if show_status:
        try:
            st = merge_driver.status(repo_root)
        except RuntimeError as exc:
            _fail(str(exc))
        if st.fully_installed:
            console.print(
                "⚓ shiplog merge driver: [green]installed[/green] (attributes + config)."
            )
        elif st.attr_installed or st.driver_configured:
            # Half-installed: call it out — both halves are needed to actually route.
            attr = "[green]yes[/green]" if st.attr_installed else "[yellow]no[/yellow]"
            drv = "[green]yes[/green]" if st.driver_configured else "[yellow]no[/yellow]"
            console.print(
                f"shiplog merge driver: [yellow]partially installed[/yellow] "
                f"(.gitattributes: {attr}, .git/config: {drv}). "
                "Run [bold]shiplog install-merge-driver[/bold] to complete it."
            )
        else:
            console.print(
                "shiplog merge driver: [yellow]not installed[/yellow]. "
                "Add it with [bold]shiplog install-merge-driver[/bold]."
            )
        return

    if uninstall:
        try:
            un = merge_driver.uninstall(repo_root)
        except RuntimeError as exc:
            _fail(str(exc))
        if un.attr_action == "absent" and un.config_action == "absent":
            console.print(
                "no shiplog merge driver installed here — nothing to remove.", style="dim"
            )
            return
        rel = _rel_to_repo(un.attributes_file, repo_root)
        if un.attr_action == "removed":
            console.print(f"⚓ removed the merge driver ([bold]{rel}[/bold] deleted + git config).")
        elif un.attr_action == "stripped":
            console.print(
                f"⚓ stripped the shiplog block from [bold]{rel}[/bold] "
                "(your other attribute rules were kept) + removed the git config."
            )
        else:  # attr absent but config was present
            console.print("⚓ removed the shiplog merge driver from [bold].git/config[/bold].")
        return

    # Default: install.
    try:
        result = merge_driver.install(repo_root)
    except RuntimeError as exc:
        _fail(str(exc))

    rel = _rel_to_repo(result.attributes_file, repo_root)
    if result.attr_action == "unchanged" and result.config_action == "unchanged":
        console.print(
            f"⚓ merge driver already installed ([bold]{rel}[/bold] + git config, up to date)."
        )
    else:
        attr_verb = {
            "created": "added the rule to",
            "updated": "refreshed the rule in",
            "unchanged": "rule already in",
        }[result.attr_action]
        cfg_verb = (
            "configured the driver"
            if result.config_action == "configured"
            else "driver already configured"
        )
        console.print(
            f"⚓ union merge driver ready: {attr_verb} [bold]{rel}[/bold], "
            f"{cfg_verb} in git config."
        )
        console.print(
            "  Commit [bold].gitattributes[/bold] so collaborators inherit it; each runs "
            "[bold]shiplog install-merge-driver[/bold] once per clone. "
            "Repair old logs with [bold]shiplog fix --write[/bold].",
            style="dim",
        )


@app.command(name="_merge-driver", hidden=True)
def _merge_driver(
    ancestor: str = typer.Argument(..., help="Ancestor blob temp path (git %O)."),
    current: str = typer.Argument(..., help="Current/ours blob temp path (git %A)."),
    other: str = typer.Argument(..., help="Other/theirs blob temp path (git %B)."),
    path: str = typer.Argument("", help="Merged file's repo path (git %P); informational."),
) -> None:
    """Internal: git merge driver entrypoint. Not for direct use.

    Invoked by git as ``shiplog _merge-driver %O %A %B %P`` on a
    ``.shiplog/log.jsonl`` conflict. Takes the union of ours + theirs, dedupes by
    id, stable-sorts, and overwrites ``%A`` with the canonical bytes. Exits 0 (an
    append-only union is always resolvable).
    """
    _ = path  # accepted for git's %P; the union doesn't need the path
    code = merge_driver.run_merge_driver(current, other, ancestor)
    raise typer.Exit(code)


if __name__ == "__main__":  # pragma: no cover
    app()
