"""Rich rendering for ship-log reads (``ls`` table, ``show`` detail).

Kept separate from :mod:`shiplog.cli` so the presentation can evolve (and be
tested) without touching command wiring, and so ``--json`` paths never import any
of this. Everything here takes already-filtered/sorted :class:`~shiplog.models.Entry`
objects and returns Rich renderables; the CLI owns the Console.

A small color map gives each entry type a glanceable hue (dead-ends in red, since
"what did we already rule out" is the log's whole reason to exist).
"""

from __future__ import annotations

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .models import Entry, EntryType

# One accent color per type — dead-ends shout, notes whisper.
_TYPE_STYLE: dict[str, str] = {
    EntryType.DECISION.value: "bold green",
    EntryType.ATTEMPT.value: "yellow",
    EntryType.DEADEND.value: "bold red",
    EntryType.NOTE.value: "cyan",
}


def type_text(type_value: str) -> Text:
    """A colorized :class:`~rich.text.Text` for an entry type value."""
    return Text(type_value, style=_TYPE_STYLE.get(type_value, "white"))


def _short_ts(ts: str) -> str:
    """Trim an ISO timestamp to ``YYYY-MM-DD HH:MM`` for compact tables."""
    t = (ts or "").strip()
    if not t:
        return ""
    t = t.replace("T", " ")
    if t.endswith("Z"):
        t = t[:-1]
    # Drop seconds (and any offset) for the table; full ts stays in `show`/json.
    return t[:16]


def _join(items: list[str], empty: str = "") -> str:
    return ", ".join(items) if items else empty


def entries_table(entries: list[Entry], *, title: str | None = None) -> Table:
    """Build a skimmable, newest-first table of entries for ``ls``.

    Columns are tuned for a one-screen glance: id, when, type, a one-line summary,
    and tags. ``files``/``why``/``ref`` live in ``show`` to keep rows short.
    """
    table = Table(
        title=title,
        title_justify="left",
        header_style="bold",
        expand=False,
        pad_edge=False,
        show_lines=False,
    )
    table.add_column("id", style="dim", no_wrap=True)
    table.add_column("when", style="dim", no_wrap=True)
    table.add_column("type", no_wrap=True)
    table.add_column("summary", overflow="fold")
    table.add_column("tags", style="magenta", overflow="fold")

    for e in entries:
        table.add_row(
            e.id,
            _short_ts(e.ts),
            type_text(e.type.value),
            e.summary,
            _join(e.tags),
        )
    return table


def entry_panel(entry: Entry) -> Panel:
    """Build a full-detail panel for a single entry (``show <id>``)."""
    body = Table.grid(padding=(0, 1))
    body.add_column(justify="right", style="bold")
    body.add_column(overflow="fold")

    def row(label: str, value: str | Text) -> None:
        body.add_row(f"{label}:", value)

    row("type", type_text(entry.type.value))
    row("summary", entry.summary)
    if entry.why:
        row("why", entry.why)
    row("when", entry.ts or "[dim](unknown)[/dim]")
    row("author", entry.author or "[dim](unknown)[/dim]")
    branch = entry.branch or "[dim](none)[/dim]"
    if entry.sha:
        branch += f" @ {entry.sha}"
    row("branch", branch)
    if entry.files:
        row("files", _join(entry.files))
    if entry.tags:
        row("tags", Text(_join(entry.tags), style="magenta"))
    if entry.ref:
        row("ref", entry.ref)

    return Panel(
        body,
        title=f"⚓ {entry.id}",
        title_align="left",
        border_style=_TYPE_STYLE.get(entry.type.value, "white"),
        expand=False,
    )


def empty_note(message: str) -> Text:
    """A dim one-liner shown when a read matches nothing."""
    return Text(message, style="dim italic")
