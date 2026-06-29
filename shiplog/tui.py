"""Full-screen TUI browser for ship-log (``shiplog tui``).

The cozy way to explore a repo's history of intent: scroll the log, filter it
live, and read full rationale in a detail pane — keyboard-first, ``q`` to quit.

Design
------
This module is split into two layers so the *selection* logic stays unit-testable
without a terminal or the optional Textual dependency:

- **Pure layer** (no Textual import): :func:`select_entries` and
  :func:`detail_lines` turn a list of :class:`~shiplog.models.Entry` plus the
  current filter state into exactly what the UI shows. They reuse the same
  :func:`~shiplog.filters.filter_entries` / :func:`~shiplog.filters.sort_newest_first`
  as ``ls``/``brief`` (no logic fork) and add only a free-text search across
  summary/why/tags/files/id.
- **Textual layer** (:class:`ShipLogApp` + :func:`run_tui`): a thin view built on
  the pure layer. Textual is imported *lazily inside* :func:`run_tui` so the rest
  of the package (and CI) never needs it installed; ``shiplog tui`` prints a
  friendly "install the extra" hint when it's missing.

Install the optional dependency with ``pip install 'ship-log[tui]'`` (or
``uv pip install -e '.[tui]'`` from a clone).
"""

from __future__ import annotations

from dataclasses import dataclass

from .filters import filter_entries, sort_newest_first
from .models import Entry, EntryType

# Friendly message shown when Textual isn't installed. Kept as a constant so the
# CLI and any test can assert on it without launching anything.
TEXTUAL_MISSING_HINT = (
    "shiplog tui needs the optional 'textual' dependency, which isn't installed.\n"
    "  Install it with:  pip install 'ship-log[tui]'\n"
    "  (from a clone:    uv pip install -e '.[tui]')"
)

# The set of entry types the type-cycle control steps through. ``None`` means
# "all types"; the rest mirror EntryType so the filter reuses ls semantics.
TYPE_CYCLE: tuple[str | None, ...] = (
    None,
    EntryType.DEADEND.value,
    EntryType.DECISION.value,
    EntryType.ATTEMPT.value,
    EntryType.NOTE.value,
)


# -- pure selection layer (no Textual) -----------------------------------


@dataclass(slots=True)
class FilterState:
    """The TUI's current filter/search state.

    Attributes:
        query: Free-text search across id/summary/why/tags/files (case-insensitive,
            all whitespace-separated terms must match — AND).
        type_: Restrict to one :class:`EntryType` value, or ``None`` for all.
        tag: Restrict to entries carrying this tag (exact, case-insensitive).
        file: Restrict to entries referencing this path (suffix match), like
            ``ls --file``.
    """

    query: str = ""
    type_: str | None = None
    tag: str = ""
    file: str = ""


def _haystack(entry: Entry) -> str:
    """Build the lowercased free-text search blob for one entry."""
    parts = [
        entry.id,
        entry.summary,
        entry.why,
        entry.type.value,
        " ".join(entry.tags),
        " ".join(entry.files),
        entry.author,
        entry.branch,
        entry.ref,
    ]
    return " ".join(p for p in parts if p).lower()


def _matches_query(entry: Entry, query: str) -> bool:
    """True when every whitespace-separated term in ``query`` is a substring.

    Empty/blank query matches everything. Terms are AND-combined so typing more
    narrows the list, the way a fuzzy filter box is expected to behave.
    """
    terms = query.lower().split()
    if not terms:
        return True
    blob = _haystack(entry)
    return all(term in blob for term in terms)


def select_entries(entries: list[Entry], state: FilterState) -> list[Entry]:
    """Return the filtered, newest-first entries for the current ``state``.

    Reuses :func:`~shiplog.filters.filter_entries` for the structured
    type/tag/file filters (identical semantics to ``ls``), then applies the
    free-text ``query`` on top, then sorts newest-first for display.
    """
    structured = filter_entries(
        entries,
        type_=state.type_,
        tag=state.tag or None,
        file=state.file or None,
    )
    if state.query.strip():
        structured = [e for e in structured if _matches_query(e, state.query)]
    return sort_newest_first(structured)


def detail_lines(entry: Entry) -> list[str]:
    """Render a single entry's full detail as plain text lines (for the detail pane).

    Mirrors the fields shown by ``shiplog show`` so the TUI and CLI agree, but as
    simple ``label: value`` lines that are trivial to assert on in tests and easy
    for Textual to display (the app adds color via Rich markup separately).
    """
    lines = [
        f"id:      {entry.id}",
        f"type:    {entry.type.value}",
        f"summary: {entry.summary}",
    ]
    if entry.why:
        lines.append(f"why:     {entry.why}")
    lines.append(f"when:    {entry.ts or '(unknown)'}")
    lines.append(f"author:  {entry.author or '(unknown)'}")
    branch = entry.branch or "(none)"
    if entry.sha:
        branch += f" @ {entry.sha}"
    lines.append(f"branch:  {branch}")
    if entry.files:
        lines.append(f"files:   {', '.join(entry.files)}")
    if entry.tags:
        lines.append(f"tags:    {', '.join(entry.tags)}")
    if entry.ref:
        lines.append(f"ref:     {entry.ref}")
    return lines


def cycle_type(current: str | None, *, reverse: bool = False) -> str | None:
    """Return the next (or previous) type filter in :data:`TYPE_CYCLE`.

    Wraps around. Used by the ``t``/``T`` key to step the type filter through
    all → deadend → decision → attempt → note → all without leaving the keyboard.
    """
    try:
        idx = TYPE_CYCLE.index(current)
    except ValueError:
        idx = 0
    step = -1 if reverse else 1
    return TYPE_CYCLE[(idx + step) % len(TYPE_CYCLE)]


def status_summary(state: FilterState, *, shown: int, total: int) -> str:
    """Build the one-line status string shown under the table.

    Reports the active filters and the shown/total counts so the user always
    knows why the list is the size it is.
    """
    bits: list[str] = []
    bits.append(f"type: {state.type_ or 'all'}")
    if state.tag:
        bits.append(f"tag: {state.tag}")
    if state.file:
        bits.append(f"file: {state.file}")
    if state.query.strip():
        bits.append(f"search: {state.query.strip()}")
    return f"{shown}/{total} entries  ·  " + "  ·  ".join(bits)


# -- Textual app layer ----------------------------------------------------


def run_tui(entries: list[Entry], *, repo_label: str = "") -> int:
    """Launch the full-screen browser over ``entries``. Returns a process code.

    Textual is imported here (lazily) so importing :mod:`shiplog.tui` — and thus
    the whole package and its test suite — never requires Textual. When it's
    absent we print :data:`TEXTUAL_MISSING_HINT` to stderr and return ``1`` so the
    caller can exit cleanly with a helpful message instead of a traceback.
    """
    try:
        from .tui_app import ShipLogApp
    except ModuleNotFoundError as exc:  # textual (or a sub-dep) not installed
        if (exc.name or "").split(".")[0] != "textual":
            raise
        import sys

        print(TEXTUAL_MISSING_HINT, file=sys.stderr)
        return 1

    ShipLogApp(entries, repo_label=repo_label).run()
    return 0
