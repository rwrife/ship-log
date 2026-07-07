"""Unit tests for the ``links`` module (pure functions, no CLI/git).

Covers link-record helpers directly so their behavior is pinned independent of
the ``shiplog link`` wiring: partitioning, summary shaping, target aggregation
(newest-first), the display projection, and the new-field round-trip through the
existing flat schema.
"""

from __future__ import annotations

from shiplog.links import (
    LinkView,
    is_link,
    links_for,
    make_link_summary,
    split_links,
)
from shiplog.models import Entry, EntryType


def _entry(**kw) -> Entry:
    kw.setdefault("summary", "x")
    return Entry(**kw)


def _link(target: str, kind: str, value: str, ts: str, note: str = "") -> Entry:
    return Entry(
        summary=make_link_summary(kind, value, note),
        type=EntryType.LINK,
        ts=ts,
        why=note,
        ref=value,
        link_target=target,
        link_kind=kind,
    )


def test_is_link_only_true_for_link_type():
    assert is_link(_entry(type=EntryType.LINK, link_target="a"))
    assert not is_link(_entry(type=EntryType.DECISION))
    assert not is_link(_entry(type=EntryType.NOTE))


def test_split_links_partitions_preserving_order():
    a = _entry(type=EntryType.DECISION, summary="a")
    b = _link("a", "commit", "sha1", "2026-01-01T00:00:00Z")
    c = _entry(type=EntryType.NOTE, summary="c")
    d = _link("a", "pr", "#1", "2026-01-02T00:00:00Z")
    primary, links = split_links([a, b, c, d])
    assert [e.summary for e in primary] == ["a", "c"]
    assert links == [b, d]


def test_make_link_summary_shapes():
    assert make_link_summary("commit", "abc1234") == "links commit abc1234"
    assert make_link_summary("pr", "#42") == "links PR #42"
    assert make_link_summary("ref", "http://x", "note") == "links ref http://x \u2014 note"


def test_links_for_filters_by_target_newest_first():
    links = [
        _link("A1", "commit", "old", "2026-01-01T00:00:00Z"),
        _link("A1", "pr", "new", "2026-03-01T00:00:00Z"),
        _link("B2", "ref", "other", "2026-02-01T00:00:00Z"),
    ]
    hits = links_for("A1", links)
    assert [h.value for h in hits] == ["new", "old"]  # newest-first
    assert all(isinstance(h, LinkView) for h in hits)


def test_links_for_is_case_insensitive_on_target():
    links = [_link("260101-ABCDEF", "commit", "v", "2026-01-01T00:00:00Z")]
    assert len(links_for("260101-abcdef", links)) == 1


def test_linkview_to_dict_is_stable_shape():
    lv = LinkView.from_entry(
        _link("A1", "commit", "sha", "2026-01-01T00:00:00Z", note="n")
    )
    d = lv.to_dict()
    assert set(d) == {"id", "kind", "value", "note", "ts", "author", "branch", "sha"}
    assert d["kind"] == "commit"
    assert d["value"] == "sha"
    assert d["note"] == "n"


def test_link_fields_roundtrip_through_json():
    e = _link("260101-TARGET", "pr", "#7", "2026-01-01T00:00:00Z", note="landed")
    restored = Entry.from_json(e.to_json())
    assert restored.type is EntryType.LINK
    assert restored.link_target == "260101-TARGET"
    assert restored.link_kind == "pr"
    assert restored.ref == "#7"
    assert restored.why == "landed"


def test_old_entries_without_link_fields_default_empty():
    # A pre-link entry line (no link_* keys) must still parse, with empty defaults.
    line = '{"summary":"old","type":"decision","id":"260101-AAAAAA","ts":"2026-01-01T00:00:00Z"}'
    e = Entry.from_json(line)
    assert e.link_target == ""
    assert e.link_kind == ""
