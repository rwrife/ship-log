"""Resolution records: close out a dead-end so it stops nagging.

Dead-ends are append-only and forever -- but sometimes a dead-end gets genuinely
fixed ("the global cache was a dead-end… until we added invalidation"). Rather
than mutate the original ``deadend`` line (which would violate append-only),
``shiplog resolve <id> --why '<how>'`` appends a tiny ``resolve`` entry that
*points back* at the dead-end (reusing the same ``link_target`` machinery as
``link``/``ack``).

A resolved dead-end is treated as **inactive** by the readers that surface
tripwires -- ``brief`` (drops it from the digest), ``guard`` (no longer blocks a
commit), and ``ask``/``ls`` (filterable on resolution state) -- while the full
history (both the dead-end and its resolution) is preserved on disk and
re-surfacable with ``--include-resolved``.

A resolution is just an :class:`~shiplog.models.Entry` of type ``resolve`` that
carries:

* ``link_target`` -- the id of the dead-end it resolves,
* ``why``         -- *how* it was resolved (the whole point),
* ``summary``     -- a human one-liner (auto-derived from the target).

Because it round-trips through the existing flat schema (no new fields, no
``SCHEMA_VERSION`` bump), old readers simply treat it as an unknown-but-harmless
entry and the dead-end's JSONL line is never touched. ``verify`` already checks
``link_target`` for dangling references, so a resolution pointing at a missing id
is caught for free.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Entry, EntryType


def is_resolution(entry: Entry) -> bool:
    """True if ``entry`` is a resolution record (closes out a dead-end)."""
    return entry.type == EntryType.RESOLVE


def resolved_ids(entries: list[Entry]) -> set[str]:
    """Return the set of dead-end ids that carry a ``resolve`` entry.

    Once a dead-end has a matching resolution, ``brief``/``guard``/``ask`` treat
    it as inactive by default (re-surfaced with ``--include-resolved``).
    """
    return {
        e.link_target
        for e in entries
        if e.type == EntryType.RESOLVE and e.link_target
    }


def make_resolution_summary(target: Entry) -> str:
    """Build the one-line summary stored on a resolution entry.

    Shape: ``resolved dead-end <id>: <target summary>`` -- greppable and clear in
    a raw log; the structured truth lives in ``link_target`` + ``why``.
    """
    return f"resolved dead-end {target.id}: {target.summary}"


@dataclass(slots=True)
class ResolutionView:
    """A resolution pointing at some dead-end (for rendering / JSON)."""

    id: str
    how: str
    ts: str
    author: str
    branch: str
    sha: str

    @classmethod
    def from_entry(cls, entry: Entry) -> ResolutionView:
        """Project a ``resolve`` :class:`Entry` into a display-friendly view."""
        return cls(
            id=entry.id,
            how=entry.why,
            ts=entry.ts,
            author=entry.author,
            branch=entry.branch,
            sha=entry.sha,
        )

    def to_dict(self) -> dict[str, str]:
        """Stable, agent-facing dict for ``show --json``'s ``resolution`` field."""
        return {
            "id": self.id,
            "how": self.how,
            "ts": self.ts,
            "author": self.author,
            "branch": self.branch,
            "sha": self.sha,
        }


def resolution_for(target_id: str, entries: list[Entry]) -> ResolutionView | None:
    """Return the newest resolution pointing at ``target_id``, or ``None``.

    ``target_id`` is matched case-insensitively against each resolution's
    ``link_target``. If a dead-end were somehow resolved twice, the newest wins.
    """
    wanted = target_id.strip().lower()
    hits = [
        ResolutionView.from_entry(e)
        for e in entries
        if e.type == EntryType.RESOLVE
        and e.link_target.strip().lower() == wanted
    ]
    if not hits:
        return None
    hits.sort(key=lambda rv: rv.ts, reverse=True)
    return hits[0]
