"""Human-facing markdown export for ship-log (``shiplog export``).

Where :mod:`shiplog.brief` is *ephemeral* and *agent-facing* (token-tuned digest
you paste into a prompt), ``export`` is *persistent* and *human-facing*: it turns
the append-only JSONL log into durable markdown artifacts you commit and ship in
release notes or a docs site. Two sub-formats:

* **ADR set** — one ``NNNN-slug.md`` per ``decision`` entry in the classic
  Architecture Decision Record shape, so the log becomes a browsable decision
  archive under e.g. ``docs/adr/``.
* **CHANGELOG digest** — a single markdown file grouping entries (decisions +
  dead-ends) by date, suitable for release notes.

The rendering logic lives here as **pure functions** (entries → markdown string /
``{filename: content}`` map) so it's unit-testable without touching disk, and so
the CLI owns the actual file writes. Filtering is *not* re-implemented here: the
CLI narrows entries via the shared :mod:`shiplog.filters` helpers and hands the
already-filtered list to these functions.

Determinism is a hard requirement: the same input entries must produce
byte-identical output every run, so committing the results yields a clean no-op
diff when nothing changed. That means:

* ADR numbering is derived from **append order** (chronological), not wall-clock
  or hash, so a given decision keeps its number as long as earlier decisions
  don't change.
* No "generated at <now>" stamps anywhere — only data drawn from the entries.
* Slugs are a pure function of the summary (+ the stable number for uniqueness).
"""

from __future__ import annotations

import re
from collections import OrderedDict

from .models import Entry, EntryType

# Supported export formats (kept as a constant so the CLI can validate + list
# them in one place).
ADR = "adr"
CHANGELOG = "changelog"
FORMATS = (ADR, CHANGELOG)

# Width of the zero-padded ADR sequence number (0001, 0002, …). Four digits is
# the ADR convention and comfortably covers any realistic decision count.
_ADR_NUM_WIDTH = 4

# Keep slugs readable and filesystem-safe; long summaries get truncated on a word
# boundary so filenames stay tidy.
_SLUG_MAX_LEN = 60


def slugify(text: str) -> str:
    """Return a filesystem-safe, lowercase, hyphenated slug for ``text``.

    Non-alphanumeric runs collapse to a single hyphen; leading/trailing hyphens
    are stripped. Purely deterministic (no randomness), so the same summary always
    yields the same slug. Empty/blank input yields ``"untitled"`` so a filename is
    never degenerate.
    """
    flat = (text or "").strip().lower()
    # Replace any run of non [a-z0-9] with a single hyphen.
    slug = re.sub(r"[^a-z0-9]+", "-", flat).strip("-")
    if len(slug) > _SLUG_MAX_LEN:
        # Trim to the last full hyphen-delimited word within the cap so we don't
        # cut a word in half; fall back to a hard cut if the first word is huge.
        cut = slug[:_SLUG_MAX_LEN]
        if "-" in cut:
            cut = cut.rsplit("-", 1)[0]
        slug = cut.strip("-") or slug[:_SLUG_MAX_LEN].strip("-")
    return slug or "untitled"


def _date_of(entry: Entry) -> str:
    """Return the ``YYYY-MM-DD`` date portion of an entry's timestamp.

    Timestamps are ISO-8601 UTC (``2026-06-19T12:00:00Z``); we take the leading
    date. A missing/short ``ts`` yields ``"unknown"`` so grouping still has a
    stable, non-crashing bucket.
    """
    ts = (entry.ts or "").strip()
    if len(ts) >= 10 and ts[4] == "-" and ts[7] == "-":
        return ts[:10]
    return "unknown"


def _clean(text: str) -> str:
    """Collapse internal whitespace/newlines to single spaces and strip.

    Summaries/why are single-line in practice, but be defensive so an entry that
    smuggled a newline can't break markdown structure (headings, front-matter).
    """
    return " ".join((text or "").split())


# -- ADR export -----------------------------------------------------------


def adr_filename(number: int, summary: str) -> str:
    """Build the stable ADR filename ``NNNN-slug.md`` for a decision.

    ``number`` is 1-based (first decision → ``0001``). The slug is derived purely
    from the summary, so re-exporting an unchanged log reproduces the same name.
    """
    return f"{number:0{_ADR_NUM_WIDTH}d}-{slugify(summary)}.md"


def render_adr(entry: Entry, number: int) -> str:
    """Render a single ``decision`` entry as one ADR markdown document.

    Layout: YAML front-matter (stable, machine-readable metadata including the
    source entry id so you can trace it back to the log) followed by a human-
    readable body — title, context/decision (``why``), affected files, and
    references. Deterministic: every field comes from the entry, with no
    generation-time data, so output is byte-stable across runs.
    """
    title = _clean(entry.summary) or "(no summary)"
    date = _date_of(entry)
    author = _clean(entry.author) or "unknown"

    # Front-matter: quote string values so colons/special chars in a summary can't
    # break the YAML. Lists are rendered as flow sequences for compactness.
    def _q(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines: list[str] = ["---"]
    lines.append(f"id: {number:0{_ADR_NUM_WIDTH}d}")
    lines.append(f"title: {_q(title)}")
    lines.append(f"date: {_q(date)}")
    lines.append("status: accepted")
    lines.append(f"author: {_q(author)}")
    lines.append(f"source_entry: {_q(entry.id)}")
    if entry.tags:
        tags_flow = ", ".join(_q(_clean(t)) for t in entry.tags)
        lines.append(f"tags: [{tags_flow}]")
    lines.append("---")
    lines.append("")

    # Body.
    lines.append(f"# {number:0{_ADR_NUM_WIDTH}d}. {title}")
    lines.append("")
    lines.append(f"- **Date:** {date}")
    lines.append("- **Status:** Accepted")
    lines.append(f"- **Author:** {author}")
    lines.append(f"- **Source entry:** `{entry.id}`")
    if entry.ref:
        lines.append(f"- **Reference:** {_clean(entry.ref)}")
    lines.append("")

    lines.append("## Decision")
    lines.append("")
    lines.append(title)
    lines.append("")

    lines.append("## Rationale")
    lines.append("")
    lines.append(_clean(entry.why) if entry.why else "_No rationale recorded._")
    lines.append("")

    if entry.files:
        lines.append("## Affected files")
        lines.append("")
        for f in entry.files:
            lines.append(f"- `{_clean(f)}`")
        lines.append("")

    # Exactly one trailing newline for a clean, POSIX-y file (no diff churn).
    return "\n".join(lines).rstrip("\n") + "\n"


def build_adr_set(entries: list[Entry]) -> OrderedDict[str, str]:
    """Render all ``decision`` entries to an ordered ``{filename: content}`` map.

    Only ``decision`` entries become ADRs (attempts/dead-ends/notes are not
    architecture decisions). Numbering is 1-based in **append order** (the input
    list is the log's chronological order), so a decision keeps its number as long
    as no earlier decision is added/removed — the property that makes committing
    the output safe.

    If two decisions slugify identically (same summary), the numeric prefix still
    makes the filenames unique, so no collision handling is needed.

    Returns an :class:`~collections.OrderedDict` in ascending ADR-number order so
    callers can write files (and tests can assert order) deterministically.
    """
    out: OrderedDict[str, str] = OrderedDict()
    number = 0
    for entry in entries:
        if entry.type != EntryType.DECISION:
            continue
        number += 1
        out[adr_filename(number, entry.summary)] = render_adr(entry, number)
    return out


# -- CHANGELOG export -----------------------------------------------------

# Types that belong in a human changelog and the label used for each. Attempts
# and notes are intentionally excluded: a release digest wants the durable
# "what we decided / what we ruled out" signal, not every scratch note.
_CHANGELOG_TYPES: OrderedDict[str, str] = OrderedDict(
    [
        (EntryType.DECISION.value, "Decisions"),
        (EntryType.DEADEND.value, "Dead-ends"),
    ]
)


def _changelog_bullet(entry: Entry) -> str:
    """Render one entry as a single changelog bullet line.

    Shape: ``- summary — why (`id`)``. The ``why`` and a code-spanned id are
    appended when present so a reader can trace back to the source entry, while
    keeping the line to one row for skimmability.
    """
    parts = [f"- {_clean(entry.summary)}"]
    if entry.why:
        parts.append(f" \u2014 {_clean(entry.why)}")
    if entry.ref:
        parts.append(f" ({_clean(entry.ref)})")
    parts.append(f" [`{entry.id}`]")
    return "".join(parts)


def render_changelog(entries: list[Entry], *, title: str = "Changelog") -> str:
    """Render entries to a single CHANGELOG-style markdown digest.

    Entries are grouped by **date** (newest date first), and within each date by
    type (decisions, then dead-ends), each as a one-line bullet. Only decisions
    and dead-ends are included (see :data:`_CHANGELOG_TYPES`). Deterministic: dates
    sort descending, entries within a (date, type) bucket keep their append order,
    and there are no generation-time stamps — so re-running with the same log is a
    byte-identical no-op.

    An empty (or filtered-to-nothing) input yields a minimal document with a
    friendly placeholder rather than an error, so callers can still write a file
    if they choose (the CLI opts to warn + skip instead).
    """
    lines: list[str] = [f"# {title}", ""]

    relevant = [e for e in entries if e.type.value in _CHANGELOG_TYPES]
    if not relevant:
        lines.append("_No decisions or dead-ends logged yet._")
        return "\n".join(lines).rstrip("\n") + "\n"

    # Bucket by date, preserving append order within each date.
    by_date: OrderedDict[str, list[Entry]] = OrderedDict()
    for e in relevant:
        by_date.setdefault(_date_of(e), []).append(e)

    # Newest date first; "unknown" (undated) sorts last so real dates lead.
    # Real dates descending, then an "unknown" bucket appended at the end.
    real_dates = sorted((d for d in by_date if d != "unknown"), reverse=True)
    ordered_dates = real_dates + (["unknown"] if "unknown" in by_date else [])

    for date in ordered_dates:
        day_entries = by_date[date]
        heading = date if date != "unknown" else "Undated"
        lines.append(f"## {heading}")
        lines.append("")
        for type_value, label in _CHANGELOG_TYPES.items():
            bucket = [e for e in day_entries if e.type.value == type_value]
            if not bucket:
                continue
            lines.append(f"### {label}")
            lines.append("")
            lines.extend(_changelog_bullet(e) for e in bucket)
            lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"
