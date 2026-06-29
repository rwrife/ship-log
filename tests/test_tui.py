"""Tests for ``shiplog tui`` — the full-screen browser (#10).

Two layers, matching the module split:

- The **pure selection layer** (:mod:`shiplog.tui`) is tested directly: filtering,
  free-text search, type cycling, the status line, and detail rendering. No
  terminal or Textual needed.
- The **Textual app** (:mod:`shiplog.tui_app`) is driven through Textual's
  ``run_test()`` pilot to verify the real key bindings re-filter the list and the
  detail pane tracks the selection. These are skipped automatically if Textual
  isn't installed (it's in the ``[dev]``/``[tui]`` extras, so CI runs them).
"""

from __future__ import annotations

import asyncio

import pytest

from shiplog.models import Entry, EntryType
from shiplog.tui import (
    TEXTUAL_MISSING_HINT,
    TYPE_CYCLE,
    FilterState,
    cycle_type,
    detail_lines,
    run_tui,
    select_entries,
    status_summary,
)


def _entry(summary: str, type_: str, **kw: object) -> Entry:
    return Entry(summary=summary, type=type_, **kw)  # type: ignore[arg-type]


@pytest.fixture
def sample() -> list[Entry]:
    """Three entries spanning types/files/tags with distinct, sortable timestamps."""
    return [
        _entry(
            "Use JSONL not SQLite",
            "decision",
            id="260101-AAAAAA",
            ts="2026-01-01T00:00:00Z",
            files=["shiplog/store.py"],
            tags=["storage"],
            why="merge-friendly + greppable",
            author="ada",
            branch="main",
            sha="deadbee",
        ),
        _entry(
            "Tried threading the append loop",
            "deadend",
            id="260102-BBBBBB",
            ts="2026-01-02T00:00:00Z",
            files=["shiplog/store.py"],
            why="lock contention",
        ),
        _entry(
            "Note about the cli wiring",
            "note",
            id="260103-CCCCCC",
            ts="2026-01-03T00:00:00Z",
            files=["shiplog/cli.py"],
            tags=["meta"],
        ),
    ]


# -- pure selection layer -------------------------------------------------


def test_select_default_is_newest_first(sample: list[Entry]) -> None:
    out = select_entries(sample, FilterState())
    assert [e.id for e in out] == ["260103-CCCCCC", "260102-BBBBBB", "260101-AAAAAA"]


def test_select_type_filter(sample: list[Entry]) -> None:
    out = select_entries(sample, FilterState(type_=EntryType.DEADEND.value))
    assert [e.id for e in out] == ["260102-BBBBBB"]


def test_select_tag_filter(sample: list[Entry]) -> None:
    out = select_entries(sample, FilterState(tag="storage"))
    assert [e.id for e in out] == ["260101-AAAAAA"]


def test_select_file_filter_suffix_match(sample: list[Entry]) -> None:
    # Suffix match reuses ls semantics: "store.py" matches "shiplog/store.py".
    out = select_entries(sample, FilterState(file="store.py"))
    assert {e.id for e in out} == {"260101-AAAAAA", "260102-BBBBBB"}


def test_search_single_term_matches_summary(sample: list[Entry]) -> None:
    out = select_entries(sample, FilterState(query="threading"))
    assert [e.id for e in out] == ["260102-BBBBBB"]


def test_search_matches_why_and_tags_and_id(sample: list[Entry]) -> None:
    assert [e.id for e in select_entries(sample, FilterState(query="greppable"))] == [
        "260101-AAAAAA"
    ]  # from why
    assert [e.id for e in select_entries(sample, FilterState(query="meta"))] == [
        "260103-CCCCCC"
    ]  # from tags
    assert [e.id for e in select_entries(sample, FilterState(query="bbbbbb"))] == [
        "260102-BBBBBB"
    ]  # from id (case-insensitive)


def test_search_terms_are_anded(sample: list[Entry]) -> None:
    # Both terms must appear (in summary "Use JSONL not SQLite").
    assert [e.id for e in select_entries(sample, FilterState(query="use sqlite"))] == [
        "260101-AAAAAA"
    ]
    # Mismatched combo matches nothing.
    assert select_entries(sample, FilterState(query="use threading")) == []


def test_search_is_case_insensitive(sample: list[Entry]) -> None:
    assert select_entries(sample, FilterState(query="JSONL")) == select_entries(
        sample, FilterState(query="jsonl")
    )


def test_blank_query_matches_all(sample: list[Entry]) -> None:
    assert len(select_entries(sample, FilterState(query="   "))) == len(sample)


def test_filters_combine_structured_and_search(sample: list[Entry]) -> None:
    # file=store.py narrows to 2, then search "threading" narrows to 1.
    out = select_entries(sample, FilterState(file="store.py", query="threading"))
    assert [e.id for e in out] == ["260102-BBBBBB"]


# -- type cycling ---------------------------------------------------------


def test_cycle_type_forward_wraps() -> None:
    seen = []
    cur: str | None = None
    for _ in range(len(TYPE_CYCLE)):
        cur = cycle_type(cur)
        seen.append(cur)
    assert seen == list(TYPE_CYCLE[1:]) + [TYPE_CYCLE[0]]
    assert seen[-1] is None  # wrapped back to "all"


def test_cycle_type_reverse() -> None:
    assert cycle_type(None, reverse=True) == TYPE_CYCLE[-1]
    assert cycle_type(TYPE_CYCLE[1], reverse=True) is None


def test_cycle_type_unknown_resets_to_first_step() -> None:
    # An unrecognized current value is treated as index 0 → first real step.
    assert cycle_type("bogus") == TYPE_CYCLE[1]


# -- status line + detail -------------------------------------------------


def test_status_summary_reports_counts_and_filters() -> None:
    s = status_summary(
        FilterState(type_="deadend", tag="storage", file="store.py", query="x"),
        shown=2,
        total=9,
    )
    assert s.startswith("2/9 entries")
    assert "type: deadend" in s
    assert "tag: storage" in s
    assert "file: store.py" in s
    assert "search: x" in s


def test_status_summary_minimal() -> None:
    s = status_summary(FilterState(), shown=3, total=3)
    assert "3/3 entries" in s
    assert "type: all" in s
    assert "tag:" not in s and "search:" not in s


def test_detail_lines_core_fields(sample: list[Entry]) -> None:
    lines = detail_lines(sample[0])
    joined = "\n".join(lines)
    assert "id:      260101-AAAAAA" in joined
    assert "type:    decision" in joined
    assert "summary: Use JSONL not SQLite" in joined
    assert "why:     merge-friendly + greppable" in joined
    assert "branch:  main @ deadbee" in joined
    assert "tags:    storage" in joined


def test_detail_lines_omits_empty_optionals(sample: list[Entry]) -> None:
    # The note entry has no why/ref; those labels must not appear.
    lines = detail_lines(sample[2])
    joined = "\n".join(lines)
    assert "why:" not in joined
    assert "ref:" not in joined
    assert "files:   shiplog/cli.py" in joined


# -- launcher fallback when Textual is missing ----------------------------


def test_run_tui_missing_textual_prints_hint(capsys) -> None:
    """If the Textual app can't import textual, the launcher prints the install
    hint and returns 1 instead of crashing.

    Simulated by evicting the cached app module and mapping ``textual`` to ``None``
    in :data:`sys.modules` (which makes ``import textual`` raise
    :class:`ModuleNotFoundError`), then restoring the import system afterward."""
    import sys

    saved = {
        name: mod
        for name, mod in list(sys.modules.items())
        if name == "textual" or name.startswith("textual.") or name == "shiplog.tui_app"
    }
    try:
        for name in saved:
            sys.modules.pop(name, None)
        sys.modules["textual"] = None  # type: ignore[assignment]
        code = run_tui([], repo_label="x")
        assert code == 1
        err = capsys.readouterr().err
        assert "ship-log[tui]" in err
        assert err.strip() == TEXTUAL_MISSING_HINT.strip()
    finally:
        sys.modules.pop("textual", None)
        sys.modules.pop("shiplog.tui_app", None)
        sys.modules.update(saved)


def test_run_tui_reraises_unrelated_import_error(monkeypatch) -> None:
    """A non-textual import failure must propagate (don't mask real bugs).

    The launcher only swallows ModuleNotFoundError whose top-level package is
    ``textual``. We stub ``shiplog.tui_app`` so ``from .tui_app import ShipLogApp``
    raises a ModuleNotFoundError for a *different* package, and assert it isn't
    converted into the friendly hint."""
    import sys
    import types

    broken = types.ModuleType("shiplog.tui_app")

    def _module_getattr(name: str):
        # Triggered by `from .tui_app import ShipLogApp` resolving the name.
        raise ModuleNotFoundError("No module named 'something_else'", name="something_else")

    broken.__getattr__ = _module_getattr  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "shiplog.tui_app", broken)

    with pytest.raises(ModuleNotFoundError) as exc:
        run_tui([], repo_label="x")
    assert (exc.value.name or "") != "textual"


# -- Textual app (driven via the pilot) -----------------------------------

textual = pytest.importorskip("textual")  # skip the app tests if textual absent


def _make_app(entries: list[Entry]):
    from shiplog.tui_app import ShipLogApp

    return ShipLogApp(entries, repo_label="ship-log")


def test_app_mounts_and_lists_all(sample: list[Entry]) -> None:
    from textual.widgets import DataTable

    async def scenario() -> None:
        app = _make_app(sample)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#list", DataTable)
            assert table.row_count == len(sample)
            # Detail pane is populated for the initial selection.
            assert app._visible[0].id == "260103-CCCCCC"

    asyncio.run(scenario())


def test_app_type_cycle_keys_filter_list(sample: list[Entry]) -> None:
    from textual.widgets import DataTable

    async def scenario() -> None:
        app = _make_app(sample)
        async with app.run_test() as pilot:
            table = app.query_one("#list", DataTable)
            await pilot.press("t")  # all -> deadend
            await pilot.pause()
            assert app._state.type_ == EntryType.DEADEND.value
            assert table.row_count == 1
            await pilot.press("T")  # deadend -> all (reverse)
            await pilot.pause()
            assert app._state.type_ is None
            assert table.row_count == len(sample)

    asyncio.run(scenario())


def test_app_live_search_then_escape_clears(sample: list[Entry]) -> None:
    from textual.widgets import DataTable, Input

    async def scenario() -> None:
        app = _make_app(sample)
        async with app.run_test() as pilot:
            table = app.query_one("#list", DataTable)
            app.query_one("#search", Input).focus()
            await pilot.pause()
            for ch in "threading":
                await pilot.press(ch)
            await pilot.pause()
            assert app._state.query == "threading"
            assert table.row_count == 1
            await pilot.press("escape")
            await pilot.pause()
            assert app._state.query == ""
            assert table.row_count == len(sample)

    asyncio.run(scenario())


def test_app_detail_tracks_selection(sample: list[Entry]) -> None:
    from textual.widgets import DataTable

    async def scenario() -> None:
        app = _make_app(sample)
        async with app.run_test() as pilot:
            app.query_one("#list", DataTable)
            await pilot.pause()
            # Move down one row; the detail pane should follow to the 2nd entry.
            await pilot.press("down")
            await pilot.pause()
            assert app._visible[1].id == "260102-BBBBBB"

    asyncio.run(scenario())
