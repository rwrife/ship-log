"""Append-only JSONL store for ship-log.

The store is deliberately tiny: no DB, no daemon. Entries are appended one JSON
object per line to ``.shiplog/log.jsonl``. Appends take an advisory file lock so
two near-simultaneous writers (two agents on one repo) can't interleave partial
lines and corrupt the file.

Design notes:
- Open in ``"a"`` mode so the OS positions every write at EOF.
- Hold an exclusive :mod:`fcntl` lock for the duration of the write, then flush +
  ``fsync`` so a crash can't leave a half-written line behind.
- A single ``write()`` of ``json + "\\n"`` keeps each record atomic-enough on
  POSIX for the small line sizes we produce.
- ``fcntl`` is POSIX-only; on platforms without it (e.g. Windows) the lock
  degrades to a no-op so the store still works single-writer.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from .models import Entry

try:  # POSIX advisory locking; absent on Windows.
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - exercised only on non-POSIX
    _HAVE_FCNTL = False

# Default log location relative to a repo's .shiplog directory.
SHIPLOG_DIR = ".shiplog"
LOG_FILENAME = "log.jsonl"


@contextmanager
def _locked(fh: IO[str], exclusive: bool) -> Iterator[None]:
    """Hold an advisory lock on ``fh`` for the duration of the block.

    Exclusive (write) locks serialize appends; shared (read) locks let readers
    proceed concurrently while still excluding an in-progress write. No-op when
    :mod:`fcntl` is unavailable.
    """
    if not _HAVE_FCNTL:
        yield
        return
    flag = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    fcntl.flock(fh.fileno(), flag)
    try:
        yield
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class Store:
    """A handle to one append-only JSONL log file.

    Args:
        path: Full path to the ``log.jsonl`` file.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    # -- construction helpers --------------------------------------------

    @classmethod
    def for_repo(cls, repo_root: str | os.PathLike[str]) -> Store:
        """Return the store at ``<repo_root>/.shiplog/log.jsonl``."""
        return cls(Path(repo_root) / SHIPLOG_DIR / LOG_FILENAME)

    def ensure_parent(self) -> None:
        """Create the ``.shiplog/`` directory if it does not exist."""
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def exists(self) -> bool:
        """True if the log file is present on disk."""
        return self.path.exists()

    # -- writes ----------------------------------------------------------

    def append(self, entry: Entry) -> Entry:
        """Append a single entry as one JSON line and return it.

        The write holds an exclusive lock and ``fsync``s before releasing, so
        concurrent appenders never interleave and a crash can't truncate a line.
        """
        self.ensure_parent()
        line = entry.to_json()
        # Newline-terminate so each record is its own line even after a crash.
        with open(self.path, "a", encoding="utf-8") as fh:
            with _locked(fh, exclusive=True):
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        return entry

    def append_many(self, entries: list[Entry]) -> int:
        """Append several entries under a single lock acquisition.

        More efficient (and still atomic per line) than calling :meth:`append`
        in a loop. Returns the number of entries written.
        """
        entries = list(entries)
        if not entries:
            return 0
        self.ensure_parent()
        payload = "".join(e.to_json() + "\n" for e in entries)
        with open(self.path, "a", encoding="utf-8") as fh:
            with _locked(fh, exclusive=True):
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
        return len(entries)

    # -- reads -----------------------------------------------------------

    def read_all(self) -> list[Entry]:
        """Read every entry, oldest first (file order).

        A missing log is treated as empty. Blank lines are skipped so a trailing
        newline never produces a phantom entry. Malformed lines raise via
        :meth:`Entry.from_json` so corruption is loud, not silent.
        """
        if not self.path.exists():
            return []
        entries: list[Entry] = []
        with open(self.path, encoding="utf-8") as fh:
            with _locked(fh, exclusive=False):
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    entries.append(Entry.from_json(line))
        return entries

    def iter_entries(self) -> Iterator[Entry]:
        """Yield entries one at a time (oldest first) without buffering all.

        Useful for large logs where you only need a streaming scan. Note: the
        shared lock is held for the lifetime of the generator, so fully consume
        or close it promptly.
        """
        if not self.path.exists():
            return
        with open(self.path, encoding="utf-8") as fh:
            with _locked(fh, exclusive=False):
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    yield Entry.from_json(line)

    def count(self) -> int:
        """Return the number of non-blank entries in the log."""
        if not self.path.exists():
            return 0
        n = 0
        with open(self.path, encoding="utf-8") as fh:
            with _locked(fh, exclusive=False):
                for line in fh:
                    if line.strip():
                        n += 1
        return n
