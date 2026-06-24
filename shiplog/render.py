"""Rich rendering for ship-log reads (``ls`` table, ``show`` detail).

Kept separate from :mod:`shiplog.cli` so the presentation can evolve (and be
tested) without touching command wiring, and so ``--json`` paths never import any
of this. Everything here takes already-filtered/sorted :class:`~shiplog.models.Entry`
objects and returns Rich renderables; the CLI owns the Console.

A small color map gives each entry type a glanceable hue (dead-ends in red, since
"what did we already rule out" is the log's whole reason to exist).
"""

from __future__ import annotations

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .blame import BlameHit, BlameResult
from .brief import Brief
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


# -- blame (line-anchored lookup) ----------------------------------------


def _anchor_label(hit: BlameHit) -> str:
    """Human label for what a hit matched: ``store.py:40-80`` or ``store.py (file)``."""
    if hit.line_range is None:
        return f"{hit.matched_path} (whole file)"
    return hit.matched_path


def _relevance_text(hit: BlameHit, target_line: int | None) -> Text:
    """A short colorized note on why a hit ranked: contains / N lines away / file."""
    if target_line is None or hit.line_range is None:
        return Text("file match", style="dim")
    if hit.contains:
        return Text(f"covers line {target_line}", style="green")
    return Text(f"{hit.distance} line{'' if hit.distance == 1 else 's'} away", style="yellow")


def _blame_hit_panel(hit: BlameHit, target_line: int | None, *, headline: bool) -> Panel:
    """Render one blame hit as a panel (the headline gets a brighter border)."""
    e = hit.entry
    body = Table.grid(padding=(0, 1))
    body.add_column(justify="right", style="bold")
    body.add_column(overflow="fold")

    body.add_row("type:", type_text(e.type.value))
    body.add_row("summary:", Text(e.summary))
    if e.why:
        body.add_row("why:", Text(e.why))
    body.add_row("anchor:", Text(_anchor_label(hit)))
    body.add_row("match:", _relevance_text(hit, target_line))
    meta = e.branch or "(no branch)"
    if e.sha:
        meta += f" @ {e.sha}"
    body.add_row("when:", Text(f"{_short_ts(e.ts)}  {meta}", style="dim"))

    title = ("⚓ nearest rationale" if headline else "alternate") + f" · {e.id}"
    return Panel(
        body,
        title=title,
        title_align="left",
        border_style=_TYPE_STYLE.get(e.type.value, "white") if headline else "dim",
        expand=False,
    )


def blame_render(result: BlameResult) -> Group:
    """Render a :class:`~shiplog.blame.BlameResult`: headline hit + alternates.

    The caller has already handled the empty case (no hits) with a friendly note;
    this assumes at least one hit and leads with the strongest match.
    """
    line = result.target.line
    where = result.target.path + (f":{line}" if line is not None else "")
    renderables: list[object] = [
        Text(f"blame {where}", style="bold"),
        _blame_hit_panel(result.best, line, headline=True),
    ]
    alts = result.alternates
    if alts:
        renderables.append(
            Text(f"{len(alts)} more match{'' if len(alts) == 1 else 'es'}:", style="dim")
        )
        renderables.extend(_blame_hit_panel(h, line, headline=False) for h in alts)
    return Group(*renderables)


# -- brief (markdown digest) ---------------------------------------------

# Section headers in digest order. Anything not deadend/decision falls into a
# trailing "Recent" bucket so attempts/notes still surface, just lower.
_BRIEF_SECTIONS: list[tuple[str, str]] = [
    (EntryType.DEADEND.value, "Dead-ends (do NOT redo)"),
    (EntryType.DECISION.value, "Decisions"),
]
_BRIEF_OTHER_HEADING = "Recent (attempts / notes)"

# Keep each bullet's free-text short so the whole digest stays paste-able.
_WHY_MAX = 100


def _trim(text: str, limit: int) -> str:
    """Collapse whitespace and clip ``text`` to ``limit`` chars with an ellipsis."""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "\u2026"


def _brief_bullet(entry: Entry) -> str:
    """Render one entry as a single compact markdown bullet line.

    Shape: ``- `id` summary -- why _(files)_``. The id is code-spanned so it's
    copy-pasteable into ``shiplog show``; ``why`` and ``files`` are included only
    when present and trimmed to keep the line short.
    """
    parts = [f"- `{entry.id}` {_trim(entry.summary, 120)}"]
    if entry.why:
        parts.append(f" \u2014 {_trim(entry.why, _WHY_MAX)}")
    if entry.files:
        parts.append(f" _({_trim(', '.join(entry.files), 80)})_")
    return "".join(parts)


def brief_markdown(brief: Brief) -> str:
    """Render a :class:`~shiplog.brief.Brief` to a compact markdown digest.

    Leads with a one-line header (focus + dead-end count), then a **Dead-ends**
    section, then **Decisions**, then a trailing **Recent** bucket for
    attempts/notes -- each entry a single bullet. A final ``+N more`` line is
    added when the budget truncated the log. Designed to drop straight into an
    agent's context; the default budget keeps it near ~40 lines.
    """
    lines: list[str] = ["# ship-log brief"]

    # Header: what's in focus + the headline dead-end count.
    if brief.focus:
        shown = ", ".join(brief.focus[:4])
        if len(brief.focus) > 4:
            shown += f", +{len(brief.focus) - 4} more"
        focus_note = f"focus: {shown}"
    else:
        focus_note = "focus: whole repo"
    lines.append(
        f"_{focus_note} \u00b7 {brief.deadend_count} dead-end"
        f"{'' if brief.deadend_count == 1 else 's'} "
        f"\u00b7 {len(brief.entries)} of {brief.total} entries_"
    )

    if not brief.entries:
        lines.append("")
        lines.append("_(log is empty -- nothing tried here yet.)_")
        return "\n".join(lines)

    # Partition once, preserving the ranked order within each bucket.
    by_type: dict[str, list[Entry]] = {}
    for e in brief.entries:
        by_type.setdefault(e.type.value, []).append(e)

    rendered_types: set[str] = set()
    for type_value, heading in _BRIEF_SECTIONS:
        bucket = by_type.get(type_value, [])
        if not bucket:
            continue
        rendered_types.add(type_value)
        lines.append("")
        lines.append(f"## {heading}")
        lines.extend(_brief_bullet(e) for e in bucket)

    # Everything else (attempts/notes/unknown), in ranked order.
    others = [e for e in brief.entries if e.type.value not in rendered_types]
    if others:
        lines.append("")
        lines.append(f"## {_BRIEF_OTHER_HEADING}")
        lines.extend(_brief_bullet(e) for e in others)

    if brief.truncated:
        lines.append("")
        lines.append(f"_+{brief.truncated} more in `shiplog ls`._")

    return "\n".join(lines)
