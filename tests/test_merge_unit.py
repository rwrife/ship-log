"""Unit tests for the ``merge`` module's normalization core (issue #31).

Pins the pure logic \u2014 dedupe by id, stable sort by (ts, id), malformed-line
preservation, byte-stable output \u2014 independent of git/CLI wiring. The real
end-to-end git merge + installer + ``fix`` CLI live in ``test_merge.py``.
"""

from __future__ import annotations

from shiplog.merge import normalize_lines, normalize_text
from shiplog.models import Entry, EntryType


def _entry(id_: str, ts: str, summary: str = "x", **kw) -> Entry:
    return Entry(summary=summary, id=id_, ts=ts, **kw)


def _line(id_: str, ts: str, **kw) -> str:
    return _entry(id_, ts, **kw).to_json()


# -- sorting ------------------------------------------------------------------


def test_stable_sort_by_ts_then_id():
    lines = [
        _line("260708-CCC", "2026-07-08T12:00:00Z"),
        _line("260708-AAA", "2026-07-08T09:00:00Z"),
        _line("260708-BBB", "2026-07-08T09:00:00Z"),  # same ts as AAA -> id breaks tie
    ]
    result = normalize_lines(lines)
    ids = [Entry.from_json(ln).id for ln in result.text.splitlines()]
    assert ids == ["260708-AAA", "260708-BBB", "260708-CCC"]
    assert result.reordered is True
    assert result.duplicates == 0
    assert result.malformed == 0


def test_already_sorted_is_clean():
    lines = [
        _line("260708-AAA", "2026-07-08T09:00:00Z"),
        _line("260708-BBB", "2026-07-08T10:00:00Z"),
    ]
    result = normalize_lines(lines)
    assert result.is_clean
    assert result.reordered is False
    assert result.duplicates == 0


# -- dedupe -------------------------------------------------------------------


def test_dedupe_by_id_first_wins():
    dup = _line("260708-AAA", "2026-07-08T09:00:00Z", summary="first")
    lines = [dup, dup, _line("260708-BBB", "2026-07-08T10:00:00Z")]
    result = normalize_lines(lines)
    out = result.text.splitlines()
    assert len(out) == 2
    assert result.duplicates == 1
    ids = [Entry.from_json(ln).id for ln in out]
    assert ids == ["260708-AAA", "260708-BBB"]


def test_dedupe_collapses_identical_ids_even_when_content_differs():
    # Two lines share an id; first occurrence wins (append-only ids are unique in
    # practice, so this is a belt-and-suspenders guarantee).
    a = _line("260708-AAA", "2026-07-08T09:00:00Z", summary="one")
    b = _line("260708-AAA", "2026-07-08T09:00:00Z", summary="two")
    result = normalize_lines([a, b])
    out = result.text.splitlines()
    assert len(out) == 1
    assert Entry.from_json(out[0]).summary == "one"
    assert result.duplicates == 1


# -- union / determinism ------------------------------------------------------


def test_union_is_order_independent():
    a = _line("260708-AAA", "2026-07-08T09:00:00Z")
    b = _line("260708-BBB", "2026-07-08T10:00:00Z")
    c = _line("260708-CCC", "2026-07-08T11:00:00Z")
    # Two "sides" of a merge with a shared line (b) in both.
    side1 = [a, b]
    side2 = [b, c]
    forward = normalize_lines(side1 + side2).text
    backward = normalize_lines(side2 + side1).text
    assert forward == backward  # deterministic regardless of merge order
    assert forward.count("\n") == 3  # a, b, c \u2014 b not duplicated


def test_output_is_byte_stable_and_canonical():
    # Non-canonical key order on input still yields canonical (sorted-key) output.
    messy = '{"summary":"x","id":"260708-AAA","ts":"2026-07-08T09:00:00Z","type":"note"}'
    result = normalize_lines([messy])
    out = result.text.splitlines()[0]
    # Canonical form == Entry round-trip via to_json (sorted keys, compact).
    assert out == Entry.from_json(messy).to_json()
    assert result.text.endswith("\n")


# -- link records survive -----------------------------------------------------


def test_link_records_preserved_through_normalize():
    decision = _line("260708-AAA", "2026-07-08T09:00:00Z", type=EntryType.DECISION)
    link = _entry(
        "260708-LNK",
        "2026-07-08T10:00:00Z",
        summary="links commit abc123",
        type=EntryType.LINK,
        link_target="260708-AAA",
        link_kind="commit",
        ref="abc123",
    ).to_json()
    result = normalize_lines([link, decision])
    out = [Entry.from_json(ln) for ln in result.text.splitlines()]
    assert len(out) == 2
    link_entry = next(e for e in out if e.type == EntryType.LINK)
    assert link_entry.link_target == "260708-AAA"
    assert link_entry.link_kind == "commit"
    assert link_entry.ref == "abc123"


# -- malformed lines ----------------------------------------------------------


def test_malformed_lines_preserved_and_pinned_to_end():
    good = _line("260708-BBB", "2026-07-08T10:00:00Z")
    junk = "this is not json"
    result = normalize_lines([junk, good])
    out = result.text.splitlines()
    assert len(out) == 2
    # Good (parseable) entry sorts first; junk pinned to the end, verbatim.
    assert Entry.from_json(out[0]).id == "260708-BBB"
    assert out[1] == junk
    assert result.malformed == 1
    assert not result.is_clean  # corruption is never "clean"


def test_blank_lines_dropped():
    good = _line("260708-AAA", "2026-07-08T09:00:00Z")
    result = normalize_lines(["", good, "   ", ""])
    assert result.text.count("\n") == 1
    assert result.malformed == 0


def test_empty_input_yields_empty_clean_result():
    result = normalize_lines([])
    assert result.text == ""
    assert result.line_count == 0
    assert result.is_clean


def test_normalize_text_wrapper_matches_lines():
    body = _line("260708-AAA", "2026-07-08T09:00:00Z") + "\n"
    assert normalize_text(body).text == normalize_lines(body.splitlines()).text
