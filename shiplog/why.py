"""Single-path rationale rollup for ``shiplog why <path>``.

``blame`` is line-anchored ("what was decided *here*, at this line?") and
``brief`` is a working-tree digest ("what's the state of the whole repo?"). There
was no single-path lens between them: *what do we already know about this one
file (or directory)?* That's the question an agent actually asks right before it
edits ``shiplog/store.py`` — and ``shiplog why shiplog/store.py`` answers it in
one shot.

``why`` gathers every entry whose ``--files`` touch the target path — matched
**exactly**, by trailing path-component (the forgiving ``ls --file`` rule), or by
**directory prefix** (``shiplog/`` covers ``shiplog/store.py``) — and ranks them
so the things that block an edit surface first:

Ranking (best first)
--------------------
1. **Dead-ends are boosted** to the top: an agent must see the graveyard before
   re-walking into it.
2. **Decisions next, newest-first** — the live rationale, freshest on top.
3. **Everything else** (attempts / notes / links) last, newest-first.

The selection logic lives here as pure functions over
:class:`~shiplog.models.Entry` lists, so ranking is unit-testable without going
through git or the CLI; Rich/markdown rendering stays in :mod:`shiplog.render`.

Path matching (``--depth``)
---------------------------
A file reference may carry a ``:line`` spec (``store.py:40-80``); ``why`` strips
it before matching (it cares about the *path*, not the line). Matching succeeds
when any of the entry's referenced paths:

* equals the target (case-insensitive), or
* trailing path-component matches it (``why store.py`` finds ``shiplog/store.py``
  — same forgiveness as ``ls --file``), or
* sits **under** the target treated as a directory prefix
  (``why shiplog`` finds ``shiplog/store.py`` and ``shiplog/cli.py``).

``--depth`` caps how many path segments *below* the target prefix still count as
a match: ``--depth 1`` matches direct children only (``shiplog/store.py`` but not
``shiplog/sub/deep.py``); ``--depth 0`` disables prefix matching entirely
(exact/suffix only). The default is unbounded (any descendant matches).
"""

from __future__ import annotations

from dataclasses import dataclass

from .filters import _entry_dt, _file_matches
from .models import Entry, EntryType

# How a matched entry connected to the target path — surfaced in output so an
# agent can see *why* a directory-level hit came back for a file query.
MATCH_EXACT = "exact"
MATCH_SUFFIX = "suffix"
MATCH_PREFIX = "prefix"


def _strip_linespec(ref: str) -> str:
    """Drop a trailing ``:line`` / ``:start-end`` spec from a file reference.

    ``why`` is path-scoped, not line-scoped, so ``shiplog/store.py:40-80`` and a
    plain ``shiplog/store.py`` are the same file to us. Only a genuine numeric
    (range) suffix is stripped — a path that merely contains a colon is left
    intact. Mirrors :func:`shiplog.blame._parse_linespec` without importing its
    regex object (kept local so the two commands can evolve independently).
    """
    raw = ref.strip()
    head, sep, tail = raw.rpartition(":")
    if not sep:
        return raw
    # ``tail`` must be a line or a-b line range to be a real linespec.
    span = tail.split("-", 1)
    if all(part.isdigit() and int(part) > 0 for part in span if part != ""):
        if any(part for part in span):
            return head or raw
    return raw


def _norm_segments(path: str) -> list[str]:
    """Split a path into normalized, lowercased, non-empty segments."""
    return [seg for seg in path.strip().lower().replace("\\", "/").split("/") if seg]


def _prefix_match(entry_path: str, target: str, *, depth: int | None) -> bool:
    """True if ``entry_path`` sits under ``target`` treated as a directory prefix.

    ``depth`` caps how many segments *below* the target still count:
    ``depth=1`` = direct children only; ``depth=0`` disables prefix matching;
    ``None`` = any descendant. The target itself matching exactly is handled by
    the caller (suffix/exact rules), so here we require *strictly deeper*.
    """
    if depth is not None and depth <= 0:
        return False
    tgt = _norm_segments(target)
    ep = _norm_segments(_strip_linespec(entry_path))
    if not tgt or len(ep) <= len(tgt):
        return False
    if ep[: len(tgt)] != tgt:
        return False
    below = len(ep) - len(tgt)
    if depth is not None and below > depth:
        return False
    return True


def _classify_match(entry: Entry, target: str, *, depth: int | None) -> str | None:
    """Return how ``entry`` matched ``target`` (exact/suffix/prefix) or ``None``.

    Exact/suffix (the ``ls --file`` rule) win over a directory-prefix match; the
    strongest classification for any of the entry's files is returned so one
    entry yields at most one hit with its best match kind.
    """
    stripped = [_strip_linespec(f) for f in entry.files]
    tgt = target.strip().lower()

    # Exact/suffix first — most specific.
    if _file_matches(stripped, target):
        for f in stripped:
            if f.strip().lower() == tgt:
                return MATCH_EXACT
        return MATCH_SUFFIX

    # Then directory-prefix containment.
    for f in entry.files:
        if _prefix_match(f, target, depth=depth):
            return MATCH_PREFIX
    return None


# Rank buckets (lower = surfaced first). Dead-ends boosted above decisions above
# the rest, so an edit sees blockers before rationale before chatter.
_BUCKET_DEADEND = 0
_BUCKET_DECISION = 1
_BUCKET_OTHER = 2


def _bucket_for(entry: Entry) -> int:
    """Coarse rank bucket for an entry by type (dead-ends first, then decisions)."""
    if entry.type == EntryType.DEADEND:
        return _BUCKET_DEADEND
    if entry.type == EntryType.DECISION:
        return _BUCKET_DECISION
    return _BUCKET_OTHER


@dataclass(frozen=True, slots=True)
class WhyHit:
    """One ranked entry that touches the target path.

    Attributes:
        entry: The matching log entry.
        match_kind: How it matched — :data:`MATCH_EXACT` / :data:`MATCH_SUFFIX` /
            :data:`MATCH_PREFIX`.
    """

    entry: Entry
    match_kind: str


def _sort_key(hit: WhyHit) -> tuple:
    """Full ranking key (lower = better): bucket, then newest-first within it.

    Entries with an unparseable/empty ``ts`` sort last within their bucket (can't
    be proven recent) — mirroring how ``--since`` treats them elsewhere.
    """
    dt = _entry_dt(hit.entry)
    recency = -dt.timestamp() if dt is not None else float("inf")
    return (_bucket_for(hit.entry), recency)


@dataclass(slots=True)
class WhyResult:
    """The outcome of a ``why`` rollup: the target plus ranked hits.

    Attributes:
        path: The queried path (as given).
        depth: The prefix-depth cap in effect (``None`` = unbounded).
        hits: Matching hits, ranked (dead-ends first, then newest decisions, …).
    """

    path: str
    depth: int | None
    hits: list[WhyHit]

    @property
    def deadend_count(self) -> int:
        """How many ranked hits are dead-ends (the headline blocker count)."""
        return sum(1 for h in self.hits if h.entry.type == EntryType.DEADEND)

    @property
    def decision_count(self) -> int:
        """How many ranked hits are decisions."""
        return sum(1 for h in self.hits if h.entry.type == EntryType.DECISION)

    @property
    def headline(self) -> str:
        """A one-line verdict, e.g. ``2 dead-ends, 3 decisions touching store.py``."""
        tail = self.path.rstrip("/").rsplit("/", 1)[-1] or self.path
        if not self.hits:
            return f"nothing logged touching {tail}"
        de = self.deadend_count
        dec = self.decision_count
        other = len(self.hits) - de - dec
        parts: list[str] = []
        if de:
            parts.append(f"{de} dead-end{'' if de == 1 else 's'}")
        if dec:
            parts.append(f"{dec} decision{'' if dec == 1 else 's'}")
        if other:
            parts.append(f"{other} other{'' if other == 1 else 's'}")
        if not parts:  # pragma: no cover - hits implies at least one bucket
            parts.append(f"{len(self.hits)} entries")
        return f"{', '.join(parts)} touching {tail}"


def why(
    entries: list[Entry],
    path: str,
    *,
    depth: int | None = None,
    limit: int = 0,
) -> WhyResult:
    """Roll up every entry touching ``path`` and rank blockers-first.

    Args:
        entries: All log entries (any order; typically the full log).
        path: The file or directory path to explain.
        depth: Prefix-depth cap for directory matching (``None`` = any descendant,
            ``0`` = exact/suffix only). See module docstring.
        limit: Max hits to return (``<= 0`` = no cap).

    Returns:
        A :class:`WhyResult`; ``result.hits`` is empty when nothing touches the
        path. Ordering: dead-ends first, then decisions newest-first, then the
        rest newest-first.
    """
    query = (path or "").strip()
    hits: list[WhyHit] = []
    if query:
        for entry in entries:
            kind = _classify_match(entry, query, depth=depth)
            if kind is not None:
                hits.append(WhyHit(entry=entry, match_kind=kind))
    hits.sort(key=_sort_key)
    if limit and limit > 0:
        hits = hits[:limit]
    return WhyResult(path=query, depth=depth, hits=hits)


def _hit_to_dict(hit: WhyHit) -> dict:
    """Serialize one hit to a JSON-ready dict (the ``--json`` shape)."""
    return {"entry": hit.entry.to_dict(), "match_kind": hit.match_kind}


def why_to_dict(result: WhyResult) -> dict:
    """Serialize a :class:`WhyResult` to a stable JSON-ready dict.

    Keys for agents: ``path``, ``depth`` (``null`` = unbounded), ``headline``
    (the one-line verdict), ``deadends``/``decisions`` counts, ``count`` (total
    hits), and ``hits`` (ranked array of ``{entry, match_kind}``).
    """
    return {
        "path": result.path,
        "depth": result.depth,
        "headline": result.headline,
        "deadends": result.deadend_count,
        "decisions": result.decision_count,
        "count": len(result.hits),
        "hits": [_hit_to_dict(h) for h in result.hits],
    }
