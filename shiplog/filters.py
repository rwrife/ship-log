"""Entry filtering + ``--since`` parsing for ship-log reads.

``ls`` (and later ``brief``) narrow the log by ``type``/``tag``/``file``/``since``.
The logic lives here — pure functions over :class:`~shiplog.models.Entry` lists —
so it's unit-testable without going through the CLI, and reusable by any future
read command.

Matching rules (all case-insensitive, all AND-combined):

- ``type``  — exact entry type (``decision``/``attempt``/``deadend``/``note``).
- ``tag``   — entry has a tag equal to the query.
- ``file``  — entry references a path equal to *or* suffix-matching the query, so
  ``--file cli.py`` finds an entry tagged ``shiplog/cli.py``. Exact matches win
  but a trailing path-component match is the common, forgiving case.
- ``since`` — entry timestamp is at/after the resolved cutoff (see
  :func:`parse_since`). Entries with an unparseable/empty ``ts`` are dropped when
  a ``since`` filter is active (they can't be proven recent).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from .models import Entry

# ``--since`` accepts either an ISO date/datetime or a compact relative span like
# ``7d`` / ``24h`` / ``2w``. Relative units are deliberately small + obvious.
_REL_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_REL_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


def parse_since(value: str, *, now: datetime | None = None) -> datetime:
    """Resolve a ``--since`` string to an aware UTC :class:`~datetime.datetime`.

    Accepts:
      * a relative span — ``<n><unit>`` where unit ∈ ``s,m,h,d,w`` (e.g. ``7d``),
        interpreted as "that long ago from now"; and
      * an ISO-8601 date (``2026-06-01``) or datetime (``2026-06-01T12:00:00Z``).

    A bare date is treated as midnight UTC. Naive datetimes are assumed UTC.

    Raises:
        ValueError: if ``value`` matches neither form.
    """
    text = value.strip()
    if not text:
        raise ValueError("--since value is empty")

    rel = _REL_RE.match(text)
    if rel:
        amount = int(rel.group(1))
        unit = rel.group(2).lower()
        seconds = amount * _REL_UNIT_SECONDS[unit]
        base = now or datetime.now(UTC)
        return base - timedelta(seconds=seconds)

    iso = text
    # ``datetime.fromisoformat`` handles ``Z`` only on 3.11+, but be defensive.
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise ValueError(
            f"--since {value!r} is not a relative span (e.g. 7d, 24h) "
            "or an ISO date/datetime (e.g. 2026-06-01)"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _entry_dt(entry: Entry) -> datetime | None:
    """Best-effort parse of an entry's ``ts`` to an aware UTC datetime."""
    ts = (entry.ts or "").strip()
    if not ts:
        return None
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _file_matches(entry_files: list[str], query: str) -> bool:
    """True if any of ``entry_files`` equals or path-suffix-matches ``query``."""
    q = query.strip().lower()
    if not q:
        return True
    for raw in entry_files:
        f = raw.strip().lower()
        if f == q:
            return True
        # Suffix match on a path boundary: "cli.py" matches "shiplog/cli.py".
        if f.endswith("/" + q):
            return True
    return False


def filter_entries(
    entries: list[Entry],
    *,
    type_: str | None = None,
    tag: str | None = None,
    file: str | None = None,
    since: datetime | None = None,
) -> list[Entry]:
    """Return the subset of ``entries`` matching every supplied filter.

    All filters are AND-combined; a ``None``/empty filter is a no-op. Input order
    is preserved (callers sort for display).
    """
    type_q = (type_ or "").strip().lower() or None
    tag_q = (tag or "").strip().lower() or None
    file_q = (file or "").strip() or None

    out: list[Entry] = []
    for e in entries:
        if type_q is not None and e.type.value != type_q:
            continue
        if tag_q is not None and tag_q not in {t.strip().lower() for t in e.tags}:
            continue
        if file_q is not None and not _file_matches(e.files, file_q):
            continue
        if since is not None:
            dt = _entry_dt(e)
            if dt is None or dt < since:
                continue
        out.append(e)
    return out


def sort_newest_first(entries: list[Entry]) -> list[Entry]:
    """Return entries newest-first by ``ts``, ties broken by *original order*.

    Entries are stored oldest-first (append order); reads default to newest-first
    for skimming. We want distinct timestamps ordered newest-first, but entries
    sharing the same timestamp second to keep their original append order (not be
    shuffled by the random id suffix). Python's ``sorted`` is stable, so a single
    stable sort by ``ts`` descending over the original list does exactly that:
    equal-``ts`` runs retain their input (append) order.
    """
    return sorted(entries, key=lambda e: e.ts, reverse=True)
