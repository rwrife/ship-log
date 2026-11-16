"""Ranking + budgeting for ``shiplog brief`` (the headline feature).

``brief`` produces the token-efficient digest an agent pastes into context
*before* working: a short, high-signal slice of the log that leads with what NOT
to redo. The selection logic lives here -- pure functions over
:class:`~shiplog.models.Entry` lists -- so ordering and the size budget are
unit-testable without going through the CLI, and the markdown/JSON rendering stays
separate (see :mod:`shiplog.render`).

Selection model
---------------
The digest answers, in priority order:

1. **Dead-ends first.** "What's already been ruled out here?" is the log's whole
   reason to exist, so ``deadend`` entries lead.
2. **Then decisions**, then **attempts**, then **notes** -- rationale you should
   know before changing things, ranked above mere notes.
3. **Within each type, entries touching your focus files come first.** The focus
   set is the working tree (or an explicit ``--files`` list); an entry is
   *relevant* when any of its ``files`` equals or path-suffix-matches a focus
   path (reusing :func:`shiplog.filters._file_matches`).
4. **Newest-first** breaks remaining ties, so stale rationale sinks.

A single integer **budget** then caps how many entries land in the digest
(default tuned to keep the rendered markdown near ~40 lines). Ranking happens
*before* truncation, so the cap drops the least-relevant tail -- never a dead-end
in favor of an old note.
"""

from __future__ import annotations

from dataclasses import dataclass

from .filters import _file_matches
from .links import is_link
from .models import Entry, EntryType
from .resolutions import is_resolution, resolved_ids

# How many entries the digest includes by default. ``ls`` is unbounded; ``brief``
# is deliberately small so it drops straight into a prompt. Each entry renders to
# ~1-2 lines, so this keeps the default well under the ~40-line target from #5.
DEFAULT_BUDGET = 12

# Type ordering for the digest: dead-ends shout, notes whisper. Lower sorts first.
_TYPE_RANK: dict[str, int] = {
    EntryType.DEADEND.value: 0,
    EntryType.DECISION.value: 1,
    EntryType.ATTEMPT.value: 2,
    EntryType.NOTE.value: 3,
}


def is_relevant(entry: Entry, focus: list[str]) -> bool:
    """True if ``entry`` touches any path in the ``focus`` set.

    An empty focus set means "nothing in particular is in focus", so nothing is
    considered specifically relevant (every entry ranks purely by type + recency).
    Matching reuses the same exact-or-path-suffix rule as ``ls --file``.
    """
    if not focus:
        return False
    return any(_file_matches(entry.files, f) for f in focus if f.strip())


def _type_rank(entry: Entry) -> int:
    """Rank an entry by type: dead-end < decision < attempt < note (unknown last)."""
    return _TYPE_RANK.get(entry.type.value, len(_TYPE_RANK))


def rank_entries(entries: list[Entry], focus: list[str]) -> list[Entry]:
    """Return ``entries`` ordered for the digest (see module docstring).

    Stable across equal keys. Sorting is two stable passes so newest-first within
    a (type, relevance) bucket is preserved without depending on timestamp
    parseability:

    1. Sort by ``ts`` descending (newest first) -- the tie-breaker.
    2. Stable-sort by (type_rank, relevance_bucket) -- the primary buckets.

    Because step 2 is stable, the newest-first order from step 1 survives inside
    each bucket; entries with identical (type, relevance, ts) keep input order.
    """
    by_recency = sorted(entries, key=lambda e: e.ts, reverse=True)
    return sorted(
        by_recency,
        key=lambda e: (_type_rank(e), 0 if is_relevant(e, focus) else 1),
    )


@dataclass(slots=True)
class Brief:
    """A ranked, budgeted digest ready for markdown/JSON rendering.

    Attributes:
        entries: The selected entries, already ranked and truncated to ``budget``.
        focus: The focus file set used for relevance (working tree or ``--files``).
        total: How many entries existed before the budget cap (for "+N more").
        budget: The cap that was applied.
        deadend_count: Number of dead-ends in ``entries`` (the headline signal).
    """

    entries: list[Entry]
    focus: list[str]
    total: int
    budget: int
    deadend_count: int

    @property
    def truncated(self) -> int:
        """How many ranked entries were dropped by the budget (0 if none)."""
        return max(0, self.total - len(self.entries))


def build_brief(
    entries: list[Entry],
    *,
    focus: list[str] | None = None,
    budget: int = DEFAULT_BUDGET,
    include_resolved: bool = False,
) -> Brief:
    """Rank, then budget, ``entries`` into a :class:`Brief`.

    Args:
        entries: All log entries (any order; typically the full log).
        focus: Files to prioritize (working tree or explicit ``--files``). ``None``
            or empty means rank by type + recency only.
        budget: Max entries to include. ``<= 0`` means "no cap" (include all,
            still ranked) -- handy for ``--limit 0`` power users.
        include_resolved: When True, resolved dead-ends are kept in the digest
            (default drops them -- a resolved dead-end is no longer a tripwire).

    Returns:
        A :class:`Brief` with ranked, truncated entries and digest metadata.
    """
    focus = [f for f in (focus or []) if f.strip()]
    # Compute resolved dead-end ids from the *full* list before we strip the
    # resolution records themselves.
    resolved = set() if include_resolved else resolved_ids(entries)
    # Link/resolution records annotate other entries (they surface in `show`), so
    # they never appear as standalone bullets in the digest.
    entries = [e for e in entries if not is_link(e) and not is_resolution(e)]
    # A resolved dead-end has been paved over -- drop it from the tripwire digest
    # unless the caller explicitly wants the full history back.
    if resolved:
        entries = [
            e
            for e in entries
            if not (e.type == EntryType.DEADEND and e.id in resolved)
        ]
    ranked = rank_entries(entries, focus)
    total = len(ranked)
    selected = ranked if budget <= 0 else ranked[:budget]
    deadends = sum(1 for e in selected if e.type.value == EntryType.DEADEND.value)
    return Brief(
        entries=selected,
        focus=focus,
        total=total,
        budget=budget,
        deadend_count=deadends,
    )


def brief_to_dict(brief: Brief) -> dict:
    """Serialize a :class:`Brief` to a JSON-ready dict (the ``--json`` shape).

    Stable keys for agents: ``entries`` (array of entry objects, ranked),
    ``focus`` (the file set used), ``total``/``shown``/``truncated`` (size budget
    accounting), and ``deadends`` (the count of ruled-out paths up top).
    """
    return {
        "entries": [e.to_dict() for e in brief.entries],
        "focus": list(brief.focus),
        "total": brief.total,
        "shown": len(brief.entries),
        "truncated": brief.truncated,
        "deadends": brief.deadend_count,
    }
