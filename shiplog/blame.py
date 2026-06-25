"""Line-anchored lookup for ``shiplog blame <file>:<line>``.

``git blame`` answers *who/when* a line was last touched. ``shiplog blame``
answers the question that actually unblocks the next agent: *what was decided or
ruled out here, and why?* Given a ``path:line`` target it finds the log entries
whose ``files`` cover that path and ranks them so the most line-relevant, most
recent rationale is the headline -- with the rest offered as alternates.

The selection logic lives here -- pure functions over
:class:`~shiplog.models.Entry` lists -- so scoring is unit-testable without going
through git or the CLI, and markdown/Rich rendering stays in :mod:`shiplog.render`.

Line anchoring
--------------
Entries don't carry a dedicated line field (the on-disk schema stays flat and
stable), so a line range is encoded *in the file path itself*::

    shiplog add deadend "lock contention on append" \\
        --files shiplog/store.py:40-80

A file reference is therefore ``<path>`` or ``<path>:<line>`` or
``<path>:<start>-<end>``. ``blame`` parses these on read; a plain ``<path>``
reference is treated as covering the whole file. This is forward-compatible: any
existing entry (no ``:line``) still matches its file, just at lower priority than
an entry that pinned the exact range.

Ranking (best first)
---------------------
For a target ``path:line``:

1. **Path must match** -- exact or path-suffix (same rule as ``ls --file``).
   Non-matching entries are dropped entirely.
2. **Line containment beats nearby beats whole-file.** An entry whose range
   *contains* the target line is best; then entries whose range is *near* it
   (closer = better, by gap in lines); then whole-file (range-less) references;
   then ranges that don't overlap, by distance.
3. **Tighter ranges win ties.** A 5-line range that contains the target is more
   specific than a 500-line range that also contains it.
4. **Newest-first** breaks any remaining tie, so stale rationale sinks.

When the target omits a line (``blame path`` with no ``:line``), step 2 collapses
to "every file match is equally on-target" and ordering falls to recency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .filters import _entry_dt, _file_matches
from .models import Entry

# A file reference's optional trailing line spec: ":42" or ":40-80". Anchored so
# only a genuine numeric (range) suffix is treated as lines -- a path that merely
# contains a colon won't be misparsed.
_LINESPEC_RE = re.compile(r"^(?P<path>.*?):(?P<start>\d+)(?:-(?P<end>\d+))?$")


@dataclass(frozen=True, slots=True)
class LineRange:
    """An inclusive ``[start, end]`` line span (1-based); ``start == end`` for one line."""

    start: int
    end: int

    def contains(self, line: int) -> bool:
        """True if ``line`` falls within this inclusive range."""
        return self.start <= line <= self.end

    @property
    def span(self) -> int:
        """Number of lines covered (inclusive); always >= 1 for a valid range."""
        return self.end - self.start + 1

    def distance_to(self, line: int) -> int:
        """Line gap from this range to ``line`` (0 if contained)."""
        if line < self.start:
            return self.start - line
        if line > self.end:
            return line - self.end
        return 0


@dataclass(frozen=True, slots=True)
class BlameTarget:
    """A parsed ``blame`` argument: a path and an optional 1-based line."""

    path: str
    line: int | None


def _parse_linespec(ref: str) -> tuple[str, LineRange | None]:
    """Split a file reference into ``(path, range_or_None)``.

    ``"shiplog/store.py:40-80"`` -> ``("shiplog/store.py", LineRange(40, 80))``;
    ``"shiplog/store.py:42"`` -> ``(..., LineRange(42, 42))``; a plain path -> the
    path with ``None``. A reversed range (``80-40``) is normalized; a zero/garbage
    spec is ignored (treated as a plain path) so we never raise on odd input.
    """
    m = _LINESPEC_RE.match(ref.strip())
    if not m:
        return ref.strip(), None
    path = m.group("path")
    start = int(m.group("start"))
    end = int(m.group("end")) if m.group("end") is not None else start
    if start <= 0 or end <= 0:
        # Not a sensible 1-based line; keep the whole token as a path.
        return ref.strip(), None
    if end < start:
        start, end = end, start
    return path, LineRange(start, end)


def parse_target(arg: str) -> BlameTarget:
    """Parse a ``blame`` CLI argument into a :class:`BlameTarget`.

    Accepts ``path``, ``path:line``, or ``path:start-end`` (a range collapses to
    its start line for "where do I care" purposes). Raises on an empty path.

    Raises:
        ValueError: if ``arg`` has no path component.
    """
    path, rng = _parse_linespec(arg)
    if not path:
        raise ValueError("blame needs a file path (e.g. shiplog/store.py:42)")
    line = rng.start if rng is not None else None
    return BlameTarget(path=path, line=line)


# Coarse buckets so "contains" always outranks "near" outranks "whole-file"
# outranks "disjoint", independent of the fine-grained distance within a bucket.
_BUCKET_CONTAINS = 0
_BUCKET_WHOLE_FILE = 1
_BUCKET_DISJOINT = 2


@dataclass(frozen=True, slots=True)
class BlameHit:
    """One ranked match: the entry plus *why* it ranked where it did.

    Attributes:
        entry: The matching log entry.
        matched_path: The entry's file reference that matched (with any line spec).
        line_range: The parsed range from ``matched_path`` (``None`` = whole file).
        contains: True if ``line_range`` contains the target line.
        distance: Line gap to the target (0 if contained or no target line).
    """

    entry: Entry
    matched_path: str
    line_range: LineRange | None
    contains: bool
    distance: int


def _best_reference(entry: Entry, target: BlameTarget) -> BlameHit | None:
    """Return the best-matching file reference on ``entry`` for ``target``.

    An entry may list several files (some with ranges); we pick the single
    reference that matches the target path *and* sits closest to the target line,
    so one entry contributes at most one hit ranked by its strongest anchor.
    Returns ``None`` if no reference matches the target path.
    """
    best: BlameHit | None = None
    for ref in entry.files:
        path, rng = _parse_linespec(ref)
        if not _file_matches([path], target.path):
            continue

        if target.line is None or rng is None:
            # No target line, or a whole-file reference: it covers the file but
            # carries no line signal.
            contains = target.line is None
            distance = 0
        else:
            contains = rng.contains(target.line)
            distance = rng.distance_to(target.line)

        hit = BlameHit(
            entry=entry,
            matched_path=ref.strip(),
            line_range=rng,
            contains=contains,
            distance=distance,
        )
        if best is None or _ref_sort_key(hit, target) < _ref_sort_key(best, target):
            best = hit
    return best


def _bucket(hit: BlameHit, target: BlameTarget) -> int:
    """Coarse priority bucket for a hit relative to the target line."""
    if target.line is None:
        # No line in play: every file match is equally on-target.
        return _BUCKET_CONTAINS
    if hit.line_range is None:
        return _BUCKET_WHOLE_FILE
    if hit.contains:
        return _BUCKET_CONTAINS
    return _BUCKET_DISJOINT


def _ref_sort_key(hit: BlameHit, target: BlameTarget) -> tuple:
    """Sort key for a single reference (lower = better) -- used to pick per-entry best.

    Order: bucket, then distance (nearer first), then a tighter range (smaller
    span) first. When the target has no line, span/distance carry no signal, so
    they're zeroed and ordering falls purely to recency (applied later, across
    entries -- it is not part of this per-reference key).
    """
    if target.line is None:
        return (_bucket(hit, target), 0, 0)
    span = hit.line_range.span if hit.line_range is not None else 1_000_000
    return (_bucket(hit, target), hit.distance, span)


def _hit_sort_key(hit: BlameHit, target: BlameTarget) -> tuple:
    """Full ranking key across entries (lower = better).

    Combines the per-reference key with a newest-first recency tiebreak. Entries
    with an unparseable/empty ``ts`` sort last within their bucket (can't be
    proven recent), mirroring how ``--since`` treats them.
    """
    dt = _entry_dt(hit.entry)
    # Newest-first => sort by negative epoch; missing ts => +inf (sorts last).
    recency = -dt.timestamp() if dt is not None else float("inf")
    return (*_ref_sort_key(hit, target), recency)


@dataclass(slots=True)
class BlameResult:
    """The outcome of a blame lookup: best match plus ranked alternates.

    Attributes:
        target: The parsed target (path + optional line).
        hits: All matching hits, best first (``hits[0]`` is the headline).
    """

    target: BlameTarget
    hits: list[BlameHit]

    @property
    def best(self) -> BlameHit | None:
        """The top-ranked hit, or ``None`` when nothing matched."""
        return self.hits[0] if self.hits else None

    @property
    def alternates(self) -> list[BlameHit]:
        """All hits after the headline (possibly empty)."""
        return self.hits[1:]


def blame(entries: list[Entry], target: BlameTarget, *, limit: int = 5) -> BlameResult:
    """Rank ``entries`` against ``target`` and return the best match + alternates.

    Args:
        entries: All log entries (any order; typically the full log).
        target: The parsed ``path:line`` to explain.
        limit: Max hits to return (headline + alternates). ``<= 0`` means no cap.

    Returns:
        A :class:`BlameResult`; ``result.hits`` is empty when no entry's files
        cover the target path.
    """
    hits: list[BlameHit] = []
    for entry in entries:
        hit = _best_reference(entry, target)
        if hit is not None:
            hits.append(hit)
    hits.sort(key=lambda h: _hit_sort_key(h, target))
    if limit and limit > 0:
        hits = hits[:limit]
    return BlameResult(target=target, hits=hits)


def _hit_to_dict(hit: BlameHit) -> dict:
    """Serialize one hit to a JSON-ready dict (the ``--json`` shape)."""
    return {
        "entry": hit.entry.to_dict(),
        "matched_path": hit.matched_path,
        "line_range": (
            [hit.line_range.start, hit.line_range.end]
            if hit.line_range is not None
            else None
        ),
        "contains": hit.contains,
        "distance": hit.distance,
    }


def blame_to_dict(result: BlameResult) -> dict:
    """Serialize a :class:`BlameResult` to a stable JSON-ready dict.

    Keys for agents: ``target`` (``path``/``line``), ``best`` (the headline hit or
    ``null``), ``alternates`` (ranked array), and ``count`` (total hits returned).
    """
    return {
        "target": {"path": result.target.path, "line": result.target.line},
        "best": _hit_to_dict(result.best) if result.best is not None else None,
        "alternates": [_hit_to_dict(h) for h in result.alternates],
        "count": len(result.hits),
    }
