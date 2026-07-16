"""Follow/tail the ship-log — the ``push`` half of the read side.

Everything else in ship-log is *pull* (``ls``, ``show``, ``brief``). ``watch``
is the missing *push*: it tails ``.shiplog/log.jsonl`` and emits new entries as
they're appended, so a human (or a supervising agent) sees a dead-end the moment
another agent logs it, without re-running ``ls``.

The mechanism is deliberately boring: poll the file, tracking how many raw lines
we've already consumed. The log is strictly append-only (see :mod:`shiplog.store`),
so "count lines, skip the ones we've seen, parse the rest" is correct and portable
(no inotify/kqueue dependency). Blank lines are counted-but-skipped exactly like
:meth:`Store.read_all`, so a trailing newline never desyncs the cursor.

The core is factored into small, testable pieces:

- :func:`read_lines` — the raw, blank-preserving line list (cursor arithmetic).
- :func:`new_entries` — given a prior line-count, return ``(entries, new_count)``.
- :func:`follow` — a generator that yields fresh :class:`Entry` objects forever
  (or until the caller stops iterating), honouring a filter callable.

The CLI layer owns rendering and SIGINT handling; this module stays I/O-light and
free of Rich/typer so it's trivial to unit-test.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path

from .models import Entry
from .store import Store

# Default poll cadence. Small enough to feel live, large enough to be cheap.
DEFAULT_INTERVAL = 0.5

# A filter is any predicate over an Entry. ``None`` inside the CLI means "all".
EntryFilter = Callable[[Entry], bool]


def read_lines(path: Path) -> list[str]:
    """Return the log's raw lines (newlines stripped), missing file -> ``[]``.

    Blank lines are *kept* here so the count matches on-disk line positions; the
    parsing step is what skips them. Keeping blanks in the cursor means a log that
    grows a blank line (shouldn't happen, but be defensive) never causes us to
    replay or drop a real entry.
    """
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as fh:
        return fh.read().splitlines()


def new_entries(
    path: Path,
    seen_lines: int,
) -> tuple[list[Entry], int]:
    """Return ``(entries, total_lines)`` for lines after ``seen_lines``.

    ``seen_lines`` is a raw line cursor (blanks included). Only lines beyond it are
    parsed; blank lines are skipped during parsing but still advance the returned
    cursor. If the file shrank or was replaced (cursor now past EOF), we treat the
    whole file as new rather than silently emitting nothing — a truncated log is
    unusual, and re-emitting is safer than going dark.
    """
    lines = read_lines(path)
    total = len(lines)
    start = seen_lines
    if start > total:
        # File shrank/rotated: re-read from the top.
        start = 0
    fresh: list[Entry] = []
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            continue
        fresh.append(Entry.from_json(stripped))
    return fresh, total


def follow(
    store: Store,
    *,
    predicate: EntryFilter | None = None,
    replay: bool = False,
    interval: float = DEFAULT_INTERVAL,
    max_iterations: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[Entry]:
    """Yield entries as they're appended, oldest-first, filtered by ``predicate``.

    Args:
        store: The repo's :class:`Store` (may point at a not-yet-created file).
        predicate: Keep only entries where ``predicate(entry)`` is true. ``None``
            keeps everything.
        replay: When true, emit the existing backlog first (in file order), then
            follow. When false (default), start from *now* — the current file end
            is treated as already-seen, so only genuinely new entries surface.
        interval: Seconds between polls while following.
        max_iterations: Stop after this many *poll cycles* (test hook / bounded
            runs). ``None`` means follow forever. Note this counts polls, not
            entries: one poll can yield several entries.
        sleep: Injectable sleep (tests pass a no-op / counter).

    Yields:
        :class:`Entry` objects passing ``predicate``, in append order.

    The generator holds no file handle between polls, so appends by other
    processes are always visible on the next cycle. Clean shutdown is the caller's
    job: stop iterating (e.g. on ``KeyboardInterrupt``) and the generator ends.
    """
    path = store.path

    def keep(entry: Entry) -> bool:
        return predicate is None or predicate(entry)

    if replay:
        # Emit the whole current backlog, then continue from its end.
        backlog, cursor = new_entries(path, 0)
        for entry in backlog:
            if keep(entry):
                yield entry
    else:
        # Start from now: adopt the current end as the cursor without emitting.
        cursor = len(read_lines(path))

    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        sleep(interval)
        fresh, cursor = new_entries(path, cursor)
        for entry in fresh:
            if keep(entry):
                yield entry
