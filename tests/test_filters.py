"""Tests for M4 filtering + ``--since`` parsing (pure, no git/CLI).

These exercise :mod:`shiplog.filters` directly so the matching rules and the
relative/ISO ``--since`` grammar are covered fast and in isolation. CLI-level
behavior (tables, ``--json`` shape, error exits) lives in ``test_ls_show.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from shiplog.filters import (
    filter_entries,
    parse_since,
    sort_newest_first,
)
from shiplog.models import Entry


def _entry(**kw: object) -> Entry:
    """Build an Entry with an explicit ts/id so ordering is deterministic."""
    kw.setdefault("summary", "x")
    return Entry(**kw)  # type: ignore[arg-type]


# -- parse_since ----------------------------------------------------------


def test_parse_since_relative_units() -> None:
    now = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
    assert parse_since("1h", now=now) == datetime(2026, 6, 21, 11, 0, 0, tzinfo=UTC)
    assert parse_since("2d", now=now) == datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)
    assert parse_since("1w", now=now) == datetime(2026, 6, 14, 12, 0, 0, tzinfo=UTC)
    assert parse_since("30m", now=now) == datetime(2026, 6, 21, 11, 30, 0, tzinfo=UTC)
    # Whitespace + case tolerated.
    assert parse_since("  3D ", now=now) == datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)


def test_parse_since_iso_date_is_utc_midnight() -> None:
    assert parse_since("2026-06-01") == datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)


def test_parse_since_iso_datetime_with_z() -> None:
    assert parse_since("2026-06-01T08:30:00Z") == datetime(
        2026, 6, 1, 8, 30, 0, tzinfo=UTC
    )


def test_parse_since_naive_datetime_assumed_utc() -> None:
    assert parse_since("2026-06-01T08:30:00") == datetime(
        2026, 6, 1, 8, 30, 0, tzinfo=UTC
    )


@pytest.mark.parametrize("bad", ["", "yesterday", "5", "3 fortnights", "2026/06/01"])
def test_parse_since_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_since(bad)


# -- filter_entries -------------------------------------------------------


def test_filter_by_type_is_exact_and_case_insensitive() -> None:
    entries = [
        _entry(type="decision", summary="a"),
        _entry(type="deadend", summary="b"),
        _entry(type="note", summary="c"),
    ]
    out = filter_entries(entries, type_="DEADEND")
    assert [e.summary for e in out] == ["b"]


def test_filter_by_tag_case_insensitive() -> None:
    entries = [
        _entry(summary="a", tags=["Storage", "perf"]),
        _entry(summary="b", tags=["docs"]),
    ]
    assert [e.summary for e in filter_entries(entries, tag="storage")] == ["a"]
    assert [e.summary for e in filter_entries(entries, tag="DOCS")] == ["b"]
    assert filter_entries(entries, tag="missing") == []


def test_filter_by_file_exact_and_suffix() -> None:
    entries = [
        _entry(summary="a", files=["shiplog/store.py"]),
        _entry(summary="b", files=["shiplog/cli.py", "README.md"]),
    ]
    # Suffix (path-boundary) match.
    assert [e.summary for e in filter_entries(entries, file="store.py")] == ["a"]
    # Exact match.
    assert [e.summary for e in filter_entries(entries, file="README.md")] == ["b"]
    # A partial component that is not on a path boundary must NOT match.
    assert filter_entries(entries, file="tore.py") == []


def test_filter_by_since_drops_older_and_unparseable() -> None:
    cutoff = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
    entries = [
        _entry(summary="old", ts="2026-06-21T11:59:59Z"),
        _entry(summary="exactly", ts="2026-06-21T12:00:00Z"),
        _entry(summary="new", ts="2026-06-21T13:00:00Z"),
        _entry(summary="nots", ts=""),  # unparseable ts dropped under since
    ]
    out = filter_entries(entries, since=cutoff)
    assert [e.summary for e in out] == ["exactly", "new"]


def test_filters_are_and_combined() -> None:
    entries = [
        _entry(summary="match", type="deadend", tags=["perf"], files=["a/store.py"]),
        _entry(summary="wrong-type", type="note", tags=["perf"], files=["a/store.py"]),
        _entry(summary="wrong-tag", type="deadend", tags=["docs"], files=["a/store.py"]),
    ]
    out = filter_entries(entries, type_="deadend", tag="perf", file="store.py")
    assert [e.summary for e in out] == ["match"]


def test_empty_filters_are_noops() -> None:
    entries = [_entry(summary="a"), _entry(summary="b")]
    out = filter_entries(entries, type_="", tag="", file="", since=None)
    assert len(out) == 2


# -- sort_newest_first ----------------------------------------------------


def test_sort_newest_first_by_ts() -> None:
    entries = [
        _entry(summary="old", ts="2026-06-20T10:00:00Z", id="260620-AAAAAA"),
        _entry(summary="new", ts="2026-06-21T10:00:00Z", id="260621-AAAAAA"),
        _entry(summary="mid", ts="2026-06-20T18:00:00Z", id="260620-ZZZZZZ"),
    ]
    assert [e.summary for e in sort_newest_first(entries)] == ["new", "mid", "old"]


def test_sort_newest_first_same_ts_keeps_append_order() -> None:
    # Same-second writes must stay in their original (append) order, regardless
    # of the random id suffix — i.e. ordering is deterministic, not id-shuffled.
    entries = [
        _entry(summary="first", ts="2026-06-21T10:00:00Z", id="260621-ZZZZZZ"),
        _entry(summary="second", ts="2026-06-21T10:00:00Z", id="260621-AAAAAA"),
        _entry(summary="third", ts="2026-06-21T10:00:00Z", id="260621-MMMMMM"),
    ]
    # Newest-first view of equal timestamps = original append order preserved.
    assert [e.summary for e in sort_newest_first(entries)] == ["first", "second", "third"]
