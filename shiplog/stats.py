"""Whole-log analytics for ``shiplog stats`` -- the voyage digest.

Where :mod:`shiplog.brief` answers "what should I know *before touching these
files*", ``stats`` zooms all the way out: a compact, skimmable health read of the
*entire* log. Are we deciding or thrashing (the dead-end ratio)? Which files are
decision hotspots? Who's actually logging, and how active is the log lately?

The aggregation lives here as a single pure function -- :func:`compute_stats`,
input a list of :class:`~shiplog.models.Entry`, output a :class:`Stats` value --
so the counting/ratio/top-N math is unit-testable without any git or CLI, and the
Rich rendering (see :mod:`shiplog.render`) and ``--json`` shape stay separate.

Design notes
------------
* **DRY time handling.** Timestamps are parsed with the same
  :func:`shiplog.filters._entry_dt` helper the reads use, and ``--since`` windowing
  is applied by the CLI via the existing :func:`shiplog.filters.parse_since` -- no
  bespoke date parser here.
* **Dead-end ratio** is ``deadends / (decisions + attempts)`` -- "how often did a
  thing we tried turn out to be a dead-end". Notes are excluded from the
  denominator (they're not attempts at anything), and an all-notes / empty log
  yields a ``None`` ratio rather than dividing by zero.
* **Top-N ordering is deterministic:** sort by count descending, then by key
  ascending so ties are stable and reproducible (important for tests and diffs).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .filters import _entry_dt
from .models import Entry, EntryType

# The type order we always report totals in (dead-ends first, notes last) so the
# human table and the JSON object read the same way every time.
_TYPE_ORDER: tuple[str, ...] = (
    EntryType.DEADEND.value,
    EntryType.DECISION.value,
    EntryType.ATTEMPT.value,
    EntryType.NOTE.value,
)

# Types that count as "something we tried" for the dead-end ratio denominator.
_ATTEMPTED_TYPES: frozenset[str] = frozenset(
    {EntryType.DECISION.value, EntryType.ATTEMPT.value}
)

# Default number of rows in each "top" list (files / tags / authors).
DEFAULT_TOP_N = 5

# Recent-activity windows, in days. Reported as simple counts ("entries in the
# last 7 / 30 days") -- the at-a-glance "are we still moving" signal.
_ACTIVITY_WINDOWS_DAYS: tuple[int, ...] = (7, 30)


@dataclass(slots=True)
class Stats:
    """Aggregated, render-ready analytics over a set of log entries.

    All figures describe the entries handed to :func:`compute_stats` (i.e. after
    any ``--since`` window the CLI applied), so ``total`` is the size of that set.

    Attributes:
        total: Number of entries considered.
        by_type: Count per entry type, in :data:`_TYPE_ORDER` (missing types -> 0).
        deadend_ratio: ``deadends / (decisions + attempts)`` as a float in
            ``[0, 1]``, or ``None`` when nothing was "tried" (no decisions/attempts).
        recent: Count of entries within each activity window, keyed by day count
            (e.g. ``{7: 3, 30: 11}``). Entries with an unparseable ``ts`` don't
            count toward any window.
        per_week: Entries-per-ISO-week as ``(year_week_label, count)`` pairs,
            oldest week first, for the log's active span. Empty when no entry has a
            parseable timestamp.
        top_files: Up to ``top_n`` ``(path, count)`` pairs -- the paths appearing in
            the most entries (decision hotspots). A single entry listing a path
            twice counts once for that entry.
        top_tags: Up to ``top_n`` ``(tag, count)`` pairs -- most-used tags.
        top_authors: Up to ``top_n`` ``(author, count)`` pairs -- who's logging.
        first_ts: ISO timestamp of the oldest entry (by ``ts``), or ``""`` if none.
        last_ts: ISO timestamp of the newest entry (by ``ts``), or ``""`` if none.
        top_n: The N used for the ``top_*`` lists (echoed for the JSON contract).
        dated: How many entries had a parseable timestamp (basis for activity/span).
    """

    total: int
    by_type: dict[str, int]
    deadend_ratio: float | None
    recent: dict[int, int]
    per_week: list[tuple[str, int]]
    top_files: list[tuple[str, int]]
    top_tags: list[tuple[str, int]]
    top_authors: list[tuple[str, int]]
    first_ts: str = ""
    last_ts: str = ""
    top_n: int = DEFAULT_TOP_N
    dated: int = 0

    @property
    def is_empty(self) -> bool:
        """True when there are no entries to summarize."""
        return self.total == 0


def _top_n(counter: Counter[str], n: int) -> list[tuple[str, int]]:
    """Return the ``n`` highest-count items, ties broken by key ascending.

    Deterministic on purpose: :meth:`Counter.most_common` leaves equal-count order
    unspecified, so we re-sort by ``(-count, key)`` to get stable, reproducible
    output for tests and clean diffs. ``n <= 0`` means "all items".
    """
    ordered = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    if n <= 0:
        return ordered
    return ordered[:n]


def _iso_week_label(dt: datetime) -> str:
    """Label a datetime by ISO year-week, e.g. ``2026-W27`` (sorts chronologically)."""
    iso = dt.isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


def _weeks_between(start: datetime, end: datetime) -> list[str]:
    """All ISO week labels from ``start``'s week through ``end``'s week, inclusive.

    Walks a week at a time so zero-activity weeks still appear (a gap in the log is
    itself signal). Both bounds are normalized to the Monday of their ISO week to
    avoid drifting when the span isn't an exact multiple of 7 days.
    """
    def week_monday(dt: datetime) -> datetime:
        # isoweekday(): Mon=1..Sun=7 -> back up to Monday, drop the time of day.
        monday = dt - timedelta(days=dt.isoweekday() - 1)
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)

    cur = week_monday(start)
    last = week_monday(end)
    labels: list[str] = []
    while cur <= last:
        labels.append(_iso_week_label(cur))
        cur += timedelta(days=7)
    return labels


def compute_stats(
    entries: list[Entry],
    *,
    now: datetime | None = None,
    top_n: int = DEFAULT_TOP_N,
) -> Stats:
    """Aggregate ``entries`` into a :class:`Stats` value (pure; no I/O).

    Args:
        entries: The entries to summarize (already ``--since``-filtered by the
            caller, if a window was requested).
        now: "Current time" for the recent-activity windows; defaults to
            :func:`datetime.now` in UTC. Injectable so tests are deterministic.
        top_n: How many rows to keep in each ``top_*`` list (``<= 0`` = all).

    Returns:
        A fully-populated :class:`Stats`. For an empty input every count is zero,
        the ratio is ``None``, and the timestamp/span fields are empty strings --
        the CLI turns that into a friendly "no entries yet" line.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    total = len(entries)

    # Totals by type, always emitted in the canonical order (missing -> 0).
    type_counts: Counter[str] = Counter(e.type.value for e in entries)
    by_type = {t: type_counts.get(t, 0) for t in _TYPE_ORDER}

    # Dead-end ratio: deadends / (decisions + attempts). None when nothing tried.
    attempted = sum(by_type[t] for t in _ATTEMPTED_TYPES)
    deadends = by_type[EntryType.DEADEND.value]
    deadend_ratio = (deadends / attempted) if attempted else None

    # Top files: de-dupe paths *within* an entry so one entry can't inflate a path.
    file_counts: Counter[str] = Counter()
    tag_counts: Counter[str] = Counter()
    author_counts: Counter[str] = Counter()
    for e in entries:
        for path in {f.strip() for f in e.files if f.strip()}:
            file_counts[path] += 1
        for tag in {t.strip() for t in e.tags if t.strip()}:
            tag_counts[tag] += 1
        author = (e.author or "").strip()
        if author:
            author_counts[author] += 1

    # Timestamp-derived figures: activity windows, per-week table, and span.
    dated: list[datetime] = []
    recent = {days: 0 for days in _ACTIVITY_WINDOWS_DAYS}
    week_counts: Counter[str] = Counter()
    for e in entries:
        dt = _entry_dt(e)
        if dt is None:
            continue
        dated.append(dt)
        week_counts[_iso_week_label(dt)] += 1
        for days in _ACTIVITY_WINDOWS_DAYS:
            if dt >= now - timedelta(days=days):
                recent[days] += 1

    per_week: list[tuple[str, int]] = []
    first_ts = ""
    last_ts = ""
    if dated:
        oldest = min(dated)
        newest = max(dated)
        # Report the raw ISO strings of the actual first/last entries for the span.
        first_ts = min(entries, key=lambda e: e.ts).ts if entries else ""
        last_ts = max(entries, key=lambda e: e.ts).ts if entries else ""
        per_week = [(label, week_counts.get(label, 0)) for label in _weeks_between(oldest, newest)]

    return Stats(
        total=total,
        by_type=by_type,
        deadend_ratio=deadend_ratio,
        recent=recent,
        per_week=per_week,
        top_files=_top_n(file_counts, top_n),
        top_tags=_top_n(tag_counts, top_n),
        top_authors=_top_n(author_counts, top_n),
        first_ts=first_ts,
        last_ts=last_ts,
        top_n=top_n,
        dated=len(dated),
    )


def stats_to_dict(stats: Stats) -> dict:
    """Serialize :class:`Stats` to the stable ``--json`` object.

    Keys (documented for agents in the ``stats`` help text / README):

    * ``total`` -- entries considered.
    * ``by_type`` -- ``{type: count}`` for every type (dead-end/decision/attempt/note).
    * ``deadend_ratio`` -- float in ``[0, 1]`` or ``null`` when nothing was tried.
    * ``recent`` -- ``{"7": n, "30": n}`` activity-window counts (keys are day
      counts as strings so the JSON object is valid).
    * ``per_week`` -- list of ``{"week": "YYYY-Www", "count": n}``, oldest first.
    * ``top_files`` / ``top_tags`` / ``top_authors`` -- lists of
      ``{"name": ..., "count": n}``, highest first.
    * ``first_ts`` / ``last_ts`` -- ISO span bounds ("" when no dated entries).
    * ``top_n`` -- the N applied to the top lists.
    * ``dated`` -- how many entries had a parseable timestamp.
    """
    def pairs(items: list[tuple[str, int]]) -> list[dict]:
        return [{"name": name, "count": count} for name, count in items]

    return {
        "total": stats.total,
        "by_type": dict(stats.by_type),
        "deadend_ratio": stats.deadend_ratio,
        "recent": {str(days): count for days, count in stats.recent.items()},
        "per_week": [{"week": label, "count": count} for label, count in stats.per_week],
        "top_files": pairs(stats.top_files),
        "top_tags": pairs(stats.top_tags),
        "top_authors": pairs(stats.top_authors),
        "first_ts": stats.first_ts,
        "last_ts": stats.last_ts,
        "top_n": stats.top_n,
        "dated": stats.dated,
    }
