"""Tests for ``shiplog watch`` -- the follow/tail read primitive (issue #44).

The core follow logic lives in :mod:`shiplog.watch` and is pure I/O over a file
path, so we test it directly: new-entry detection, replay-then-follow ordering,
filter application, cursor arithmetic across blank lines and truncation. A thin
CLI smoke test covers NDJSON shape and clean SIGINT exit via a bounded stream.
"""

from __future__ import annotations

from pathlib import Path

from shiplog.models import Entry, EntryType
from shiplog.store import Store
from shiplog.watch import follow, new_entries, read_lines


def _append(path: Path, entry: Entry) -> None:
    """Append one JSONL entry exactly like the store does (trailing newline)."""
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(entry.to_json() + "\n")


def _entry(summary: str, **kw: object) -> Entry:
    return Entry(summary=summary, **kw)  # type: ignore[arg-type]


# -- read_lines / new_entries (cursor arithmetic) -------------------------


def test_read_lines_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_lines(tmp_path / "nope.jsonl") == []


def test_new_entries_only_returns_lines_after_cursor(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    _append(log, _entry("first"))
    _append(log, _entry("second"))

    entries, cursor = new_entries(log, 0)
    assert [e.summary for e in entries] == ["first", "second"]
    assert cursor == 2

    # Nothing new yet.
    entries, cursor = new_entries(log, cursor)
    assert entries == []
    assert cursor == 2

    # Append -> only the new one surfaces.
    _append(log, _entry("third"))
    entries, cursor = new_entries(log, cursor)
    assert [e.summary for e in entries] == ["third"]
    assert cursor == 3


def test_new_entries_skips_blank_lines_but_counts_them(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    _append(log, _entry("real"))
    # A stray blank line shouldn't produce a phantom entry, but must advance cursor.
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("\n")
    entries, cursor = new_entries(log, 0)
    assert [e.summary for e in entries] == ["real"]
    assert cursor == 2  # one real + one blank


def test_new_entries_rereads_when_file_shrinks(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    _append(log, _entry("a"))
    _append(log, _entry("b"))
    _, cursor = new_entries(log, 0)
    assert cursor == 2

    # Truncate/replace the file (cursor now past EOF): re-read from top.
    log.write_text("", encoding="utf-8")
    _append(log, _entry("fresh"))
    entries, cursor = new_entries(log, cursor)
    assert [e.summary for e in entries] == ["fresh"]
    assert cursor == 1


# -- follow (since-now default, replay, filters) --------------------------


def test_follow_since_now_ignores_backlog(tmp_path: Path) -> None:
    log = tmp_path / ".shiplog" / "log.jsonl"
    log.parent.mkdir(parents=True)
    store = Store(log)
    _append(log, _entry("old"))

    appended = {"done": False}

    def fake_sleep(_secs: float) -> None:
        # On the first poll cycle, append a new entry so follow() can catch it.
        if not appended["done"]:
            _append(log, _entry("new"))
            appended["done"] = True

    got = list(
        follow(store, replay=False, max_iterations=1, sleep=fake_sleep)
    )
    assert [e.summary for e in got] == ["new"]


def test_follow_replay_emits_backlog_then_follows(tmp_path: Path) -> None:
    log = tmp_path / ".shiplog" / "log.jsonl"
    log.parent.mkdir(parents=True)
    store = Store(log)
    _append(log, _entry("one"))
    _append(log, _entry("two"))

    def fake_sleep(_secs: float) -> None:
        _append(log, _entry("three"))

    got = list(
        follow(store, replay=True, max_iterations=1, sleep=fake_sleep)
    )
    # Backlog first (in order), then the newly appended one.
    assert [e.summary for e in got] == ["one", "two", "three"]


def test_follow_applies_predicate(tmp_path: Path) -> None:
    log = tmp_path / ".shiplog" / "log.jsonl"
    log.parent.mkdir(parents=True)
    store = Store(log)
    _append(log, _entry("keep", type=EntryType.DEADEND))
    _append(log, _entry("drop", type=EntryType.NOTE))

    def only_deadends(e: Entry) -> bool:
        return e.type == EntryType.DEADEND

    got = list(
        follow(
            store,
            predicate=only_deadends,
            replay=True,
            max_iterations=1,
            sleep=lambda _s: None,
        )
    )
    assert [e.summary for e in got] == ["keep"]


def test_follow_waits_for_missing_log(tmp_path: Path) -> None:
    # Log doesn't exist yet: follow should not crash, and should pick up the
    # first entry once it appears.
    log = tmp_path / ".shiplog" / "log.jsonl"
    log.parent.mkdir(parents=True)
    store = Store(log)

    def fake_sleep(_secs: float) -> None:
        if not log.exists():
            _append(log, _entry("born"))

    got = list(
        follow(store, replay=False, max_iterations=1, sleep=fake_sleep)
    )
    assert [e.summary for e in got] == ["born"]


def test_follow_stops_after_max_iterations(tmp_path: Path) -> None:
    log = tmp_path / ".shiplog" / "log.jsonl"
    log.parent.mkdir(parents=True)
    store = Store(log)
    calls = {"n": 0}

    def counting_sleep(_secs: float) -> None:
        calls["n"] += 1

    list(follow(store, max_iterations=3, sleep=counting_sleep))
    assert calls["n"] == 3
