"""Link records: attach a commit / PR / ref to an existing entry after the fact.

The honest version of "editing the past". You logged a decision *before* the
commit existed; later it lands in ``abc1234`` or PR #42. Rather than mutating the
original entry line (which would violate append-only), ``shiplog link`` appends a
tiny ``link`` entry that *points back* at the target id. Readers (``show``,
``--json``) then surface the accumulated links on the original entry.

A link is just an :class:`~shiplog.models.Entry` of type ``link`` that carries:

* ``link_target`` — the id of the entry it points at,
* ``link_kind``   — ``commit`` / ``pr`` / ``ref``,
* ``ref``         — the value (sha / PR url-or-number / free text),
* ``summary``     — a human one-liner (auto-derived, or the ``--note``).

Because it round-trips through the existing flat schema (two new optional fields,
no ``SCHEMA_VERSION`` bump), old readers simply ignore the extra fields and the
target entry's JSONL line is never touched.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Entry, EntryType

# The three kinds of thing a link can point at. ``commit`` and ``pr`` are the
# common cases; ``ref`` is the free-text escape hatch (a ticket, a doc, a URL).
LINK_KINDS = ("commit", "pr", "ref")

# Human labels for rendering the Links section.
_KIND_LABEL = {"commit": "commit", "pr": "PR", "ref": "ref"}


def is_link(entry: Entry) -> bool:
    """True if ``entry`` is a link record (points back at another entry)."""
    return entry.type == EntryType.LINK


def split_links(entries: list[Entry]) -> tuple[list[Entry], list[Entry]]:
    """Partition ``entries`` into (primary, links), preserving order in each.

    ``primary`` is everything you'd list/brief on; ``links`` are the follow-up
    linkage records that annotate them. The two shares one pass so ``ls``/``brief``
    (primary) and ``show`` (links) never disagree about what counts as a link.
    """
    primary: list[Entry] = []
    links: list[Entry] = []
    for e in entries:
        (links if is_link(e) else primary).append(e)
    return primary, links


def make_link_summary(kind: str, value: str, note: str = "") -> str:
    """Build the one-line summary stored on a link entry.

    Shape: ``links <kind> <value>`` (+ ``— <note>`` when given). Kept human and
    greppable; the structured truth lives in ``link_kind``/``ref``/``link_target``.
    """
    label = _KIND_LABEL.get(kind, kind)
    base = f"links {label} {value}".rstrip()
    note = (note or "").strip()
    return f"{base} \u2014 {note}" if note else base


@dataclass(slots=True)
class LinkView:
    """A resolved link pointing at some target entry (for rendering / JSON)."""

    id: str
    kind: str
    value: str
    note: str
    ts: str
    author: str
    branch: str
    sha: str

    @classmethod
    def from_entry(cls, entry: Entry) -> LinkView:
        """Project a ``link`` :class:`Entry` into a display-friendly view."""
        return cls(
            id=entry.id,
            kind=entry.link_kind,
            value=entry.ref,
            note=entry.why,
            ts=entry.ts,
            author=entry.author,
            branch=entry.branch,
            sha=entry.sha,
        )

    def to_dict(self) -> dict[str, str]:
        """Stable, agent-facing dict for ``show --json``'s ``links`` array."""
        return {
            "id": self.id,
            "kind": self.kind,
            "value": self.value,
            "note": self.note,
            "ts": self.ts,
            "author": self.author,
            "branch": self.branch,
            "sha": self.sha,
        }


def links_for(target_id: str, links: list[Entry]) -> list[LinkView]:
    """Return all links pointing at ``target_id``, **newest-first**.

    ``target_id`` is matched case-insensitively against each link's resolved
    ``link_target`` (the caller resolves a prefix to a full id before calling, so
    this is an exact-id compare). Newest-first mirrors ``show``'s reading order.
    """
    wanted = target_id.strip().lower()
    hits = [LinkView.from_entry(e) for e in links if e.link_target.strip().lower() == wanted]
    hits.sort(key=lambda lv: lv.ts, reverse=True)
    return hits
