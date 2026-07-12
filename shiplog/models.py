"""Entry model + (de)serialization for ship-log.

An :class:`Entry` is one append-only record in the captain's log. It serializes
to a single JSON line (JSONL) so the log stays trivially diffable, greppable, and
merge-friendly. The on-disk shape is intentionally flat and stable; bump
``SCHEMA_VERSION`` if the wire format ever changes incompatibly.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

# Bump only on incompatible on-disk format changes. Readers may use this to
# migrate or warn on entries written by a newer/older shiplog.
SCHEMA_VERSION = 1

# Crockford's base32 (no I, L, O, U) — unambiguous, URL/clipboard safe, and
# sorts the same as ASCII for the random suffix.
_ID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ID_SUFFIX_LEN = 6


class EntryType(StrEnum):
    """What kind of log record this is.

    As a :class:`~enum.StrEnum`, each member *is* its string value, so it
    serializes directly and compares equal to the plain string
    (``EntryType.NOTE == "note"``).
    """

    DECISION = "decision"
    ATTEMPT = "attempt"
    DEADEND = "deadend"
    NOTE = "note"
    LINK = "link"
    ACK = "ack"

    @classmethod
    def coerce(cls, value: EntryType | str) -> EntryType:
        """Return an ``EntryType`` from a member or its string value.

        Raises:
            ValueError: if ``value`` is not a known entry type.
        """
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:  # re-raise with the valid set for friendlier CLIs
            valid = ", ".join(t.value for t in cls)
            raise ValueError(
                f"unknown entry type {value!r}; expected one of: {valid}"
            ) from exc


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string with a trailing ``Z``.

    Second precision is plenty for a human-scale log and keeps ids/lines tidy.
    """
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def generate_id(now: datetime | None = None) -> str:
    """Generate a short, roughly-sortable id: ``<yymmdd>-<6 base32 chars>``.

    The date prefix gives day-granular lexical sorting (newest entries sort last
    within a file, oldest first overall); the random suffix avoids collisions for
    many entries on the same day. Uses :mod:`secrets` so concurrent writers don't
    coordinate on a shared counter.

    Example: ``260619-K3F9Q2``.
    """
    when = now or datetime.now(UTC)
    date_part = when.strftime("%y%m%d")
    suffix = "".join(secrets.choice(_ID_ALPHABET) for _ in range(_ID_SUFFIX_LEN))
    return f"{date_part}-{suffix}"


# Field names that map 1:1 between the dataclass and the JSON line. Kept as a
# module constant so (de)serialization and round-trip tests share one source.
_LIST_FIELDS = ("files", "tags")


@dataclass(slots=True)
class Entry:
    """A single append-only ship-log record.

    Attributes:
        summary: One-line description of the decision/attempt/dead-end/note.
        type: One of :class:`EntryType` (stored as its string value).
        id: Short sortable id; auto-generated when omitted.
        ts: ISO-8601 UTC timestamp; auto-set to "now" when omitted.
        author: Who logged it (usually from git config).
        branch: Git branch at write time.
        sha: Short HEAD sha at write time.
        why: Optional rationale — the whole point of the log.
        files: Paths this entry is about (for line/file anchoring later).
        tags: Free-form labels for filtering.
        ref: Linked issue/PR reference.
        link_target: For ``link`` entries, the id of the entry this points back at
            (empty on ordinary entries). A ``link`` entry never mutates its target;
            it's an append-only follow-up fact ("this decision shipped in <sha>").
        link_kind: For ``link`` entries, what kind of thing ``ref`` names --
            ``commit`` / ``pr`` / ``ref`` (empty on ordinary entries).
        schema_version: On-disk format version (see :data:`SCHEMA_VERSION`).
    """

    summary: str
    type: EntryType = EntryType.NOTE
    id: str = field(default_factory=generate_id)
    ts: str = field(default_factory=utcnow_iso)
    author: str = ""
    branch: str = ""
    sha: str = ""
    why: str = ""
    files: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    ref: str = ""
    link_target: str = ""
    link_kind: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        # Normalize the type so callers may pass a plain string.
        self.type = EntryType.coerce(self.type)
        # Defensive: never let a None slip into a list field.
        if self.files is None:
            self.files = []
        if self.tags is None:
            self.tags = []

    # -- serialization ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a plain, JSON-ready dict (``type`` as its string value)."""
        data = asdict(self)
        data["type"] = self.type.value
        return data

    def to_json(self) -> str:
        """Serialize to a single compact JSON line (no embedded newlines).

        Keys are sorted for stable, diff-friendly output; ``ensure_ascii`` is off
        so unicode in summaries/why survives as-is.
        """
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Entry:
        """Build an :class:`Entry` from a decoded dict.

        Unknown keys are ignored (forward-compatible with newer writers); missing
        keys fall back to dataclass defaults.
        """
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        if "summary" not in kwargs:
            raise ValueError("entry is missing required field 'summary'")
        return cls(**kwargs)

    @classmethod
    def from_json(cls, line: str) -> Entry:
        """Parse one JSONL line into an :class:`Entry`.

        Raises:
            ValueError: if the line is not a JSON object or lacks ``summary``.
        """
        obj = json.loads(line)
        if not isinstance(obj, dict):
            raise ValueError(f"expected a JSON object per line, got {type(obj).__name__}")
        return cls.from_dict(obj)
