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

from .ask import AskResult
from .blame import BlameHit, BlameResult
from .brief import Brief
from .links import LinkView
from .models import Entry, EntryType
from .resolutions import ResolutionView
from .stats import Stats
from .why import WhyHit, WhyResult

# One accent color per type — dead-ends shout, notes whisper.
_TYPE_STYLE: dict[str, str] = {
    EntryType.DECISION.value: "bold green",
    EntryType.ATTEMPT.value: "yellow",
    EntryType.DEADEND.value: "bold red",
    EntryType.NOTE.value: "cyan",
    EntryType.LINK.value: "blue",
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


_LINK_KIND_LABEL = {"commit": "commit", "pr": "PR", "ref": "ref"}


def _links_grid(links: list[LinkView]) -> Table:
    """Render resolved links as a compact newest-first list for ``show``."""
    grid = Table.grid(padding=(0, 1))
    grid.add_column(justify="right", style="bold blue", no_wrap=True)
    grid.add_column(overflow="fold")
    for lv in links:
        label = _LINK_KIND_LABEL.get(lv.kind, lv.kind or "link")
        line = Text(lv.value or "", style="blue")
        if lv.note:
            line.append(f"  — {lv.note}", style="dim")
        when = _short_ts(lv.ts)
        if when:
            line.append(f"   [{when}]", style="dim")
        grid.add_row(f"{label}:", line)
    return grid


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


def watch_line(entry: Entry) -> Text:
    """A single glanceable line for a streamed entry: ``when  TYPE  id  summary``.

    Tuned for ``watch``'s live tail: compact, type-colored, one entry per line so
    a fast-moving multi-agent log stays readable. Tags are appended when present
    (dim/magenta) but never wrap the line off-screen; ``show`` still owns detail.
    """
    line = Text()
    line.append(_short_ts(entry.ts), style="dim")
    line.append("  ")
    line.append_text(type_text(entry.type.value))
    line.append("  ")
    line.append(entry.id, style="dim")
    line.append("  ")
    line.append(entry.summary)
    if entry.tags:
        line.append("  ")
        line.append(_join(entry.tags), style="magenta")
    return line


def entry_panel(
    entry: Entry,
    *,
    links: list[LinkView] | None = None,
    resolution: ResolutionView | None = None,
) -> Panel:
    """Build a full-detail panel for a single entry (``show <id>``).

    When ``links`` are supplied (follow-up ``link`` records pointing at this
    entry), a **Links** section is appended below the fields, newest-first. When
    ``resolution`` is supplied (this dead-end was resolved), a **Resolved**
    section is appended noting how.
    """
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

    content: object = body
    sections: list[object] = []
    if resolution:
        res_line = Text("resolved", style="bold green")
        if resolution.how:
            res_line = Text.assemble(
                Text("resolved", style="bold green"),
                Text(f" — {resolution.how}"),
            )
        sections.extend([Text(""), res_line])
    if links:
        sections.extend(
            [
                Text(""),
                Text(f"Links ({len(links)})", style="bold blue"),
                _links_grid(links),
            ]
        )
    if sections:
        content = Group(body, *sections)

    return Panel(
        content,
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


# -- why (single-path rationale rollup) ----------------------------------

_WHY_MATCH_LABEL: dict[str, str] = {
    "exact": "exact path",
    "suffix": "path suffix",
    "prefix": "under directory",
}


def _why_hit_panel(hit: WhyHit) -> Panel:
    """Render one ``why`` hit as a panel, colored + bordered by entry type."""
    e = hit.entry
    body = Table.grid(padding=(0, 1))
    body.add_column(justify="right", style="bold")
    body.add_column(overflow="fold")

    body.add_row("type:", type_text(e.type.value))
    body.add_row("summary:", Text(e.summary))
    if e.why:
        body.add_row("why:", Text(e.why))
    if e.files:
        body.add_row("files:", Text(", ".join(e.files), style="dim"))
    body.add_row("match:", Text(_WHY_MATCH_LABEL.get(hit.match_kind, hit.match_kind), style="dim"))
    meta = e.branch or "(no branch)"
    if e.sha:
        meta += f" @ {e.sha}"
    body.add_row("when:", Text(f"{_short_ts(e.ts)}  {meta}", style="dim"))

    return Panel(
        body,
        title=f"{e.type.value} \u00b7 {e.id}",
        title_align="left",
        border_style=_TYPE_STYLE.get(e.type.value, "white"),
        expand=False,
    )


def why_render(result: WhyResult) -> Group:
    """Render a :class:`~shiplog.why.WhyResult`: headline verdict + ranked panels.

    The caller handles the empty case (no hits) with a friendly note; this assumes
    at least one hit and leads with the one-line verdict, then dead-ends, then
    decisions, then the rest.
    """
    renderables: list[object] = [
        Text(f"why {result.path}", style="bold"),
        Text(result.headline, style="cyan"),
    ]
    renderables.extend(_why_hit_panel(h) for h in result.hits)
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


# -- stats (whole-log analytics digest) ----------------------------------

# Blocks for the tiny inline sparkline used by the per-week activity table. Eight
# levels from near-empty to full; index 0 is a low block so a nonzero-but-small
# week is still visible (an all-spaces sparkline would read as "no data").
_SPARK_BLOCKS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"


def _sparkline(counts: list[int]) -> str:
    """Render a list of counts as a unicode block sparkline (empty string if none).

    Zero always maps to a space (a genuine gap in the log reads as a gap); nonzero
    values scale across the block ramp relative to the max, so the busiest week is
    a full block and quieter weeks step down from there.
    """
    if not counts:
        return ""
    peak = max(counts)
    if peak <= 0:
        return " " * len(counts)
    out: list[str] = []
    top = len(_SPARK_BLOCKS) - 1
    for c in counts:
        if c <= 0:
            out.append(" ")
            continue
        # Scale 1..peak onto 0..top, keeping the smallest nonzero visible.
        idx = round((c / peak) * top)
        out.append(_SPARK_BLOCKS[max(1, idx) if c > 0 else 0])
    return "".join(out)


def _type_bar(count: int, peak: int, *, width: int = 12) -> Text:
    """A tiny proportional bar (``count`` relative to ``peak``) for the totals table."""
    if peak <= 0:
        return Text("")
    filled = 0 if count == 0 else max(1, round((count / peak) * width))
    return Text("\u2588" * filled, style="dim")


def _fmt_span(stats: Stats) -> str:
    """Human 'first -> last' span line, using the short-ts formatter.

    Collapses to a single stamp only for a genuine one-entry log (``total == 1``);
    several entries logged in the same second share a ``ts`` but must still read as
    a range, not '(single entry)'.
    """
    first = _short_ts(stats.first_ts) or "?"
    last = _short_ts(stats.last_ts) or "?"
    if stats.total <= 1 or first == last:
        return f"{first}" if stats.total <= 1 else f"{first}  (same day)"
    return f"{first}  \u2192  {last}"


def _totals_table(stats: Stats) -> Table:
    """Totals-by-type with a proportional bar and the headline dead-end ratio."""
    peak = max(stats.by_type.values(), default=0)
    table = Table(
        title="Totals by type",
        title_justify="left",
        header_style="bold",
        expand=False,
        pad_edge=False,
        box=None,
    )
    table.add_column("type", no_wrap=True)
    table.add_column("count", justify="right", no_wrap=True)
    table.add_column("", no_wrap=True)
    for type_value, count in stats.by_type.items():
        table.add_row(type_text(type_value), str(count), _type_bar(count, peak))
    ratio = (
        "[dim]n/a (nothing tried yet)[/dim]"
        if stats.deadend_ratio is None
        else f"[bold]{stats.deadend_ratio * 100:.0f}%[/bold]"
    )
    table.add_row(Text("dead-end ratio", style="dim"), "", Text.from_markup(ratio))
    return table


def _activity_table(stats: Stats) -> Table:
    """Recent-window counts (7d / 30d) plus a per-week sparkline + tail table."""
    table = Table.grid(padding=(0, 2))
    table.add_column(justify="right", style="bold")
    table.add_column(overflow="fold")
    for days in sorted(stats.recent):
        table.add_row(f"last {days}d:", str(stats.recent[days]))
    if stats.per_week:
        counts = [c for _, c in stats.per_week]
        spark = _sparkline(counts)
        weeks = len(stats.per_week)
        table.add_row(
            f"per week ({weeks}):",
            Text(f"{spark}  ", style="cyan") + Text(f"peak {max(counts)}/wk", style="dim"),
        )
    return table


def _top_table(title: str, rows: list[tuple[str, int]], *, empty: str) -> Table:
    """A small right-aligned-count 'top N' table (files / tags / authors)."""
    table = Table(
        title=title,
        title_justify="left",
        header_style="bold",
        expand=False,
        pad_edge=False,
        box=None,
    )
    table.add_column("", overflow="fold")
    table.add_column("n", justify="right", no_wrap=True, style="dim")
    if not rows:
        table.add_row(Text(empty, style="dim italic"), "")
        return table
    for name, count in rows:
        table.add_row(name, str(count))
    return table


def stats_render(stats: Stats) -> Group:
    """Render a :class:`~shiplog.stats.Stats` as a compact, skimmable dashboard.

    Leads with a one-line header (total + span), then totals-by-type with the
    dead-end ratio, a recent-activity block (7d/30d + per-week sparkline), and
    three 'top' tables (files, tags, authors). The caller handles the empty-log
    case with a friendly note, so this assumes at least one entry.
    """
    header = Text.assemble(
        ("\u2693 ship-log stats", "bold"),
        (f"  \u00b7  {stats.total} entr{'y' if stats.total == 1 else 'ies'}", "dim"),
        (f"  \u00b7  {_fmt_span(stats)}", "dim"),
    )

    tops = Table.grid(padding=(0, 3))
    tops.add_column()
    tops.add_column()
    tops.add_column()
    tops.add_row(
        _top_table("Top files", stats.top_files, empty="(no files logged)"),
        _top_table("Top tags", stats.top_tags, empty="(no tags)"),
        _top_table("Top authors", stats.top_authors, empty="(no authors)"),
    )

    return Group(
        header,
        Text(""),
        _totals_table(stats),
        Text(""),
        Panel(
            _activity_table(stats),
            title="Activity",
            title_align="left",
            border_style="cyan",
            expand=False,
        ),
        Text(""),
        tops,
    )


def _ask_hit_panel(hit, rank: int, *, headline: bool) -> Panel:
    """Render one search hit as a compact panel (headline = top match)."""
    e = hit.entry
    body = Table.grid(padding=(0, 1))
    body.add_column(justify="right", style="bold", no_wrap=True)
    body.add_column(overflow="fold")
    body.add_row("type:", type_text(e.type.value))
    body.add_row("summary:", Text(e.summary))
    if e.why:
        body.add_row("why:", Text(e.why))
    if e.files:
        body.add_row("files:", Text(_join(e.files), style="cyan"))
    if e.tags:
        body.add_row("tags:", Text(_join(e.tags), style="magenta"))
    meta = " ".join(p for p in (e.branch, e.sha) if p)
    body.add_row("when:", Text(f"{_short_ts(e.ts)}  {meta}".strip(), style="dim"))
    body.add_row("id:", Text(e.id, style="dim"))
    title = Text(f"#{rank}  ", style="bold")
    title.append(f"score {hit.score:.2f}", style="green" if headline else "dim")
    return Panel(
        body,
        title=title,
        title_align="left",
        border_style="green" if headline else "dim",
        expand=False,
    )


def ask_render(result: AskResult) -> Group:
    """Render an :class:`~shiplog.ask.AskResult`: verdict line, then ranked hits."""
    verdict = Text(result.verdict(), style="bold")
    if result.total_matches == 0:
        return Group(verdict)
    parts = [verdict, Text("")]
    for i, hit in enumerate(result.hits, start=1):
        parts.append(_ask_hit_panel(hit, i, headline=(i == 1)))
    if result.truncated:
        parts.append(
            Text(
                f"… +{result.truncated} more match"
                f"{'es' if result.truncated != 1 else ''} "
                "(raise --limit to see them).",
                style="dim italic",
            )
        )
    return Group(*parts)
