"""Tests for ``shiplog stats``: aggregation math + the ``--json`` contract.

Two layers, mirroring ``test_brief.py``:

* **Pure** (`shiplog.stats.compute_stats`) -- deterministic counts, ratios, and
  top-N ordering (incl. ties) over hand-built :class:`~shiplog.models.Entry`
  lists, with an injected ``now`` so the activity windows are stable. No git/CLI.
* **End-to-end** (Typer) -- the ``stats`` command against a throwaway git repo,
  asserting the rendered dashboard, the ``--json`` shape, ``--since`` windowing,
  and the empty-log / before-init friendly paths.

Required by #25: totals-by-type, dead-end ratio, recent activity, top files/tags/
authors, log span, ``--since`` reuse, ``--json`` shape, and the empty-log path
(no traceback / no divide-by-zero).
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shiplog.cli import app
from shiplog.models import Entry, EntryType
from shiplog.stats import compute_stats, stats_to_dict

runner = CliRunner()

# A fixed "now" so 7d/30d windows are deterministic across machines/clocks.
NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=UTC)


def _entry(
    summary: str,
    type_: str,
    *,
    ts: str,
    files: list[str] | None = None,
    tags: list[str] | None = None,
    author: str = "",
) -> Entry:
    """Build an Entry with explicit fields so aggregation is deterministic."""
    return Entry(
        summary=summary,
        type=EntryType.coerce(type_),
        ts=ts,
        files=files or [],
        tags=tags or [],
        author=author,
    )


# -- pure aggregation -----------------------------------------------------


def test_totals_by_type_in_canonical_order_with_missing_zeroed() -> None:
    entries = [
        _entry("d1", "decision", ts="2026-06-20T00:00:00Z"),
        _entry("d2", "decision", ts="2026-06-21T00:00:00Z"),
        _entry("de", "deadend", ts="2026-06-22T00:00:00Z"),
    ]
    stats = compute_stats(entries, now=NOW)
    # Order is always deadend, decision, attempt, note; absent types are 0.
    assert list(stats.by_type.keys()) == ["deadend", "decision", "attempt", "note"]
    assert stats.by_type == {"deadend": 1, "decision": 2, "attempt": 0, "note": 0}
    assert stats.total == 3


def test_deadend_ratio_is_deadends_over_decisions_plus_attempts() -> None:
    entries = [
        _entry("dec", "decision", ts="2026-06-20T00:00:00Z"),
        _entry("att", "attempt", ts="2026-06-20T00:00:00Z"),
        _entry("de1", "deadend", ts="2026-06-20T00:00:00Z"),
        _entry("note", "note", ts="2026-06-20T00:00:00Z"),  # excluded from denom
    ]
    stats = compute_stats(entries, now=NOW)
    # 1 deadend / (1 decision + 1 attempt) = 0.5; the note doesn't count.
    assert stats.deadend_ratio == pytest.approx(0.5)


def test_deadend_ratio_is_none_when_nothing_was_tried() -> None:
    # Only notes (and even a lone deadend) -> denominator is 0 -> ratio is None,
    # never a ZeroDivisionError.
    only_notes = [_entry("n", "note", ts="2026-06-20T00:00:00Z")]
    assert compute_stats(only_notes, now=NOW).deadend_ratio is None
    lone_deadend = [_entry("de", "deadend", ts="2026-06-20T00:00:00Z")]
    assert compute_stats(lone_deadend, now=NOW).deadend_ratio is None


def test_recent_activity_windows_count_only_entries_in_range() -> None:
    entries = [
        _entry("today", "note", ts="2026-06-30T09:00:00Z"),   # in 7d + 30d
        _entry("wk", "note", ts="2026-06-25T00:00:00Z"),       # in 7d + 30d
        _entry("mo", "note", ts="2026-06-10T00:00:00Z"),       # in 30d only
        _entry("old", "note", ts="2026-01-01T00:00:00Z"),      # in neither
    ]
    stats = compute_stats(entries, now=NOW)
    assert stats.recent[7] == 2
    assert stats.recent[30] == 3


def test_top_files_dedupes_within_an_entry_and_ranks_by_count() -> None:
    entries = [
        _entry("a", "decision", ts="2026-06-20T00:00:00Z", files=["x.py", "x.py"]),
        _entry("b", "decision", ts="2026-06-21T00:00:00Z", files=["x.py"]),
        _entry("c", "decision", ts="2026-06-22T00:00:00Z", files=["y.py"]),
    ]
    stats = compute_stats(entries, now=NOW)
    # x.py appears in 2 entries (the duplicate within entry `a` counts once).
    assert stats.top_files[0] == ("x.py", 2)
    assert ("y.py", 1) in stats.top_files


def test_top_n_ties_break_by_key_ascending_and_respect_limit() -> None:
    # All tags count 1 -> ties resolved alphabetically; --top caps the list.
    entries = [
        _entry("a", "note", ts="2026-06-20T00:00:00Z", tags=["zebra"]),
        _entry("b", "note", ts="2026-06-20T00:00:00Z", tags=["alpha"]),
        _entry("c", "note", ts="2026-06-20T00:00:00Z", tags=["mango"]),
    ]
    top2 = compute_stats(entries, now=NOW, top_n=2).top_tags
    assert top2 == [("alpha", 1), ("mango", 1)]  # zebra dropped by the cap
    # top_n <= 0 means "all", still deterministically ordered.
    all_tags = compute_stats(entries, now=NOW, top_n=0).top_tags
    assert [t for t, _ in all_tags] == ["alpha", "mango", "zebra"]


def test_top_authors_ranks_who_logs_most() -> None:
    entries = [
        _entry("a", "note", ts="2026-06-20T00:00:00Z", author="Ann"),
        _entry("b", "note", ts="2026-06-20T00:00:00Z", author="Ann"),
        _entry("c", "note", ts="2026-06-20T00:00:00Z", author="Bob"),
    ]
    stats = compute_stats(entries, now=NOW)
    assert stats.top_authors[0] == ("Ann", 2)
    assert ("Bob", 1) in stats.top_authors


def test_span_and_per_week_track_the_active_window() -> None:
    entries = [
        _entry("first", "note", ts="2026-06-01T00:00:00Z"),
        _entry("last", "note", ts="2026-06-20T00:00:00Z"),
    ]
    stats = compute_stats(entries, now=NOW)
    assert stats.first_ts == "2026-06-01T00:00:00Z"
    assert stats.last_ts == "2026-06-20T00:00:00Z"
    # per_week spans every ISO week from the first entry's week to the last's,
    # inclusive (zero-activity weeks in the middle still appear).
    labels = [label for label, _ in stats.per_week]
    assert labels[0] == "2026-W23"  # week of 2026-06-01
    assert labels[-1] == "2026-W25"  # week of 2026-06-20
    assert sum(count for _, count in stats.per_week) == 2


def test_entries_with_unparseable_ts_dont_break_activity_or_span() -> None:
    entries = [
        _entry("good", "note", ts="2026-06-20T00:00:00Z"),
        _entry("bad", "note", ts="not-a-date"),
    ]
    stats = compute_stats(entries, now=NOW)
    assert stats.total == 2       # both counted in totals
    assert stats.dated == 1       # only one had a usable timestamp
    assert stats.recent[30] == 1  # the undated one can't be proven recent


def test_empty_log_is_all_zero_none_and_empty_no_division() -> None:
    stats = compute_stats([], now=NOW)
    assert stats.is_empty
    assert stats.total == 0
    assert stats.deadend_ratio is None
    assert stats.recent == {7: 0, 30: 0}
    assert stats.per_week == []
    assert stats.top_files == stats.top_tags == stats.top_authors == []
    assert stats.first_ts == "" and stats.last_ts == ""


def test_stats_to_dict_shape_is_stable() -> None:
    entries = [
        _entry(
            "dec", "decision", ts="2026-06-20T00:00:00Z",
            files=["x.py"], tags=["t"], author="Ann",
        ),
        _entry("de", "deadend", ts="2026-06-21T00:00:00Z", files=["x.py"]),
    ]
    obj = stats_to_dict(compute_stats(entries, now=NOW))
    for key in (
        "total", "by_type", "deadend_ratio", "recent", "per_week",
        "top_files", "top_tags", "top_authors", "first_ts", "last_ts",
        "top_n", "dated",
    ):
        assert key in obj
    # recent keys are stringified day counts (valid JSON object keys).
    assert set(obj["recent"]) == {"7", "30"}
    # top_* are lists of {name, count}.
    assert obj["top_files"] == [{"name": "x.py", "count": 2}]
    assert obj["per_week"][0]["week"].startswith("2026-W")


# -- end-to-end CLI -------------------------------------------------------


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fresh git repo (one commit), cwd set to it, gitctx cache cleared."""
    _git("init", cwd=tmp_path)
    _git("config", "user.name", "Test Captain", cwd=tmp_path)
    _git("config", "user.email", "cap@ship.log", cwd=tmp_path)
    _git("checkout", "-b", "main", cwd=tmp_path)
    (tmp_path / "a.txt").write_text("hi\n")
    _git("add", "a.txt", cwd=tmp_path)
    _git("commit", "-m", "init", cwd=tmp_path)
    monkeypatch.chdir(tmp_path)
    from shiplog import gitctx

    gitctx.find_repo_root.cache_clear()
    return tmp_path


def _seed(repo: Path) -> None:
    """init + a spread of types/files/tags so stats has something to chew on."""
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(
        app, ["add", "decision", "use jsonl", "--why", "diffable",
               "--files", "shiplog/store.py", "--tags", "storage"]
    ).exit_code == 0
    assert runner.invoke(
        app, ["add", "attempt", "try sqlite", "--files", "shiplog/store.py",
               "--tags", "storage"]
    ).exit_code == 0
    assert runner.invoke(
        app, ["add", "deadend", "sqlite locks", "--files", "shiplog/store.py",
               "--tags", "storage"]
    ).exit_code == 0
    assert runner.invoke(
        app, ["add", "note", "tidy readme", "--tags", "docs"]
    ).exit_code == 0


def test_stats_human_dashboard_renders_key_figures(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["stats"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "ship-log stats" in out
    assert "Totals by type" in out
    assert "dead-end ratio" in out
    # 1 deadend / (1 decision + 1 attempt) = 50%.
    assert "50%" in out
    assert "Activity" in out
    assert "Top files" in out and "Top tags" in out and "Top authors" in out
    assert "shiplog/store.py" in out


def test_stats_json_shape_and_numbers(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["stats", "--json"])
    assert result.exit_code == 0, result.output
    obj = json.loads(result.output)
    assert isinstance(obj, dict)
    for key in (
        "total", "by_type", "deadend_ratio", "recent", "per_week",
        "top_files", "top_tags", "top_authors", "first_ts", "last_ts",
    ):
        assert key in obj
    assert obj["total"] == 4
    assert obj["by_type"] == {"deadend": 1, "decision": 1, "attempt": 1, "note": 1}
    assert obj["deadend_ratio"] == pytest.approx(1 / 2)
    # store.py is the hotspot (3 entries reference it).
    assert obj["top_files"][0] == {"name": "shiplog/store.py", "count": 3}


def test_stats_since_filters_the_window(repo: Path) -> None:
    _seed(repo)
    # Everything was just logged, so a 1h window keeps them all...
    wide = json.loads(runner.invoke(app, ["stats", "--since", "1h", "--json"]).output)
    assert wide["total"] == 4
    # ...and a far-future ISO date excludes everything -> friendly empty note.
    narrow = runner.invoke(app, ["stats", "--since", "2999-01-01"])
    assert narrow.exit_code == 0
    assert "no entries in that window" in narrow.output


def test_stats_top_option_caps_lists(repo: Path) -> None:
    _seed(repo)
    obj = json.loads(runner.invoke(app, ["stats", "--top", "1", "--json"]).output)
    assert len(obj["top_tags"]) == 1
    assert obj["top_n"] == 1


def test_stats_bad_since_is_a_friendly_error(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["stats", "--since", "not-a-date"])
    assert result.exit_code == 1
    assert "--since" in result.output


def test_stats_empty_log_is_friendly_no_traceback(repo: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["stats"])
    assert result.exit_code == 0
    assert "no entries yet" in result.output
    # And the JSON path on an empty log is a clean, non-crashing object.
    obj = json.loads(runner.invoke(app, ["stats", "--json"]).output)
    assert obj["total"] == 0 and obj["deadend_ratio"] is None


def test_stats_before_init_fails_with_hint(repo: Path) -> None:
    result = runner.invoke(app, ["stats"])
    assert result.exit_code == 1
    assert "shiplog init" in result.output
