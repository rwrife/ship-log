"""Tests for ``shiplog blame <file>:<line>`` (#7).

Two layers, mirroring the rest of the suite:

* **Pure ranking** against :mod:`shiplog.blame` -- target parsing, line
  containment vs. proximity vs. whole-file, tighter-range and recency tiebreaks,
  and the ``--json`` dict shape. Fast, no git/CLI.
* **CLI end-to-end** through Typer against a throwaway git repo -- the four
  acceptance criteria: covers-file matching, ranked top + alternates, ``--json``,
  and the graceful "nothing touches this file" message.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shiplog.blame import (
    BlameTarget,
    LineRange,
    blame,
    blame_to_dict,
    parse_target,
)
from shiplog.cli import app
from shiplog.models import Entry

runner = CliRunner()


def _entry(**kw: object) -> Entry:
    kw.setdefault("summary", "x")
    return Entry(**kw)  # type: ignore[arg-type]


# -- parse_target ---------------------------------------------------------


def test_parse_target_path_only() -> None:
    t = parse_target("shiplog/store.py")
    assert t == BlameTarget(path="shiplog/store.py", line=None)


def test_parse_target_with_line() -> None:
    assert parse_target("shiplog/store.py:42") == BlameTarget("shiplog/store.py", 42)


def test_parse_target_with_range_uses_start() -> None:
    # A range target collapses to its start line ("where do I care").
    assert parse_target("a/b.py:40-80").line == 40


def test_parse_target_empty_raises() -> None:
    with pytest.raises(ValueError):
        parse_target(":42")


def test_parse_target_zero_line_is_treated_as_path() -> None:
    # ":0" is not a sensible 1-based line, so it's kept as part of the path and
    # there is no target line.
    t = parse_target("weird:0")
    assert t.line is None
    assert t.path == "weird:0"


# -- LineRange ------------------------------------------------------------


def test_linerange_contains_and_distance() -> None:
    r = LineRange(40, 80)
    assert r.contains(40) and r.contains(80) and r.contains(60)
    assert not r.contains(39)
    assert r.distance_to(60) == 0
    assert r.distance_to(30) == 10
    assert r.distance_to(85) == 5
    assert r.span == 41


# -- blame ranking --------------------------------------------------------


def test_blame_drops_non_matching_paths() -> None:
    entries = [
        _entry(summary="other", files=["shiplog/cli.py"]),
        _entry(summary="hit", files=["shiplog/store.py:10-20"]),
    ]
    result = blame(entries, parse_target("shiplog/store.py:15"))
    assert [h.entry.summary for h in result.hits] == ["hit"]


def test_blame_containing_range_beats_disjoint() -> None:
    entries = [
        _entry(summary="far", files=["shiplog/store.py:200-210"], ts="2026-06-24T10:00:00Z"),
        _entry(summary="on", files=["shiplog/store.py:40-60"], ts="2026-06-20T10:00:00Z"),
    ]
    result = blame(entries, parse_target("shiplog/store.py:50"))
    # The containing range wins even though it is older than the disjoint one.
    assert result.best is not None
    assert result.best.entry.summary == "on"
    assert result.best.contains is True


def test_blame_nearer_disjoint_beats_farther() -> None:
    entries = [
        _entry(summary="near", files=["a.py:60-70"]),   # 10 away from line 50
        _entry(summary="far", files=["a.py:200-210"]),  # 150 away
    ]
    result = blame(entries, parse_target("a.py:50"))
    assert [h.entry.summary for h in result.hits] == ["near", "far"]


def test_blame_contains_beats_wholefile_beats_disjoint() -> None:
    entries = [
        _entry(summary="disjoint", files=["a.py:200-210"]),
        _entry(summary="wholefile", files=["a.py"]),
        _entry(summary="contains", files=["a.py:40-60"]),
    ]
    result = blame(entries, parse_target("a.py:50"))
    assert [h.entry.summary for h in result.hits] == [
        "contains",
        "wholefile",
        "disjoint",
    ]


def test_blame_tighter_range_wins_when_both_contain() -> None:
    entries = [
        _entry(summary="loose", files=["a.py:1-500"], ts="2026-06-24T10:00:00Z"),
        _entry(summary="tight", files=["a.py:48-52"], ts="2026-06-20T10:00:00Z"),
    ]
    result = blame(entries, parse_target("a.py:50"))
    # Both contain line 50; the tighter (more specific) range leads despite age.
    assert result.best is not None
    assert result.best.entry.summary == "tight"


def test_blame_recency_breaks_ties_within_same_bucket() -> None:
    entries = [
        _entry(summary="old", files=["a.py:40-60"], ts="2026-06-20T10:00:00Z"),
        _entry(summary="new", files=["a.py:40-60"], ts="2026-06-24T10:00:00Z"),
    ]
    result = blame(entries, parse_target("a.py:50"))
    # Identical anchor → newest first.
    assert [h.entry.summary for h in result.hits] == ["new", "old"]


def test_blame_no_target_line_ranks_by_recency() -> None:
    entries = [
        _entry(summary="old", files=["a.py:40-60"], ts="2026-06-20T10:00:00Z"),
        _entry(summary="new", files=["a.py"], ts="2026-06-24T10:00:00Z"),
    ]
    # No line → every file match is equally on-target; recency decides.
    result = blame(entries, parse_target("a.py"))
    assert [h.entry.summary for h in result.hits] == ["new", "old"]


def test_blame_suffix_path_match() -> None:
    entries = [_entry(summary="hit", files=["shiplog/store.py:40-60"])]
    # Target uses just the basename; suffix match (path boundary) still hits.
    result = blame(entries, parse_target("store.py:50"))
    assert result.best is not None
    assert result.best.entry.summary == "hit"


def test_blame_one_hit_per_entry_uses_strongest_anchor() -> None:
    # An entry listing the file twice (whole-file + a containing range) yields a
    # single hit, ranked by its strongest (containing) anchor.
    entries = [_entry(summary="multi", files=["a.py", "a.py:48-52"])]
    result = blame(entries, parse_target("a.py:50"))
    assert len(result.hits) == 1
    assert result.best is not None
    assert result.best.contains is True
    assert result.best.line_range == LineRange(48, 52)


def test_blame_limit_caps_hits() -> None:
    entries = [_entry(summary=f"e{i}", files=[f"a.py:{i}-{i}"]) for i in range(10)]
    result = blame(entries, parse_target("a.py:5"), limit=3)
    assert len(result.hits) == 3


def test_blame_empty_when_nothing_matches() -> None:
    entries = [_entry(summary="x", files=["b.py"])]
    result = blame(entries, parse_target("a.py:1"))
    assert result.hits == []
    assert result.best is None
    assert result.alternates == []


# -- blame_to_dict shape --------------------------------------------------


def test_blame_to_dict_shape() -> None:
    entries = [
        _entry(summary="best", files=["a.py:40-60"], ts="2026-06-24T10:00:00Z"),
        _entry(summary="alt", files=["a.py"], ts="2026-06-20T10:00:00Z"),
    ]
    payload = blame_to_dict(blame(entries, parse_target("a.py:50")))
    assert payload["target"] == {"path": "a.py", "line": 50}
    assert payload["count"] == 2
    assert payload["best"]["entry"]["summary"] == "best"
    assert payload["best"]["line_range"] == [40, 60]
    assert payload["best"]["contains"] is True
    assert payload["best"]["distance"] == 0
    assert [a["entry"]["summary"] for a in payload["alternates"]] == ["alt"]
    # Whole-file alternate reports a null range.
    assert payload["alternates"][0]["line_range"] is None


def test_blame_to_dict_empty() -> None:
    payload = blame_to_dict(blame([], parse_target("a.py:1")))
    assert payload["best"] is None
    assert payload["alternates"] == []
    assert payload["count"] == 0


# -- CLI end-to-end (acceptance criteria) ---------------------------------


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
    """init + entries anchored to store.py with different line ranges."""
    assert runner.invoke(app, ["init"]).exit_code == 0
    # Containing range for line 50.
    assert runner.invoke(
        app,
        ["add", "deadend", "lock contention on append",
         "--files", "shiplog/store.py:40-80", "--why", "threads thrash the lock"],
    ).exit_code == 0
    # Whole-file decision (covers, but no line signal).
    assert runner.invoke(
        app,
        ["add", "decision", "use jsonl not sqlite",
         "--files", "shiplog/store.py", "--why", "merge-friendly"],
    ).exit_code == 0
    # Unrelated file.
    assert runner.invoke(
        app, ["add", "note", "doc the cli", "--files", "shiplog/cli.py"]
    ).exit_code == 0


def test_cli_blame_finds_covering_entry_first(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["blame", "shiplog/store.py:50", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # The line-anchored dead-end (40-80 contains 50) is the headline; the
    # whole-file decision is an alternate.
    assert payload["best"]["entry"]["summary"] == "lock contention on append"
    assert payload["best"]["contains"] is True
    alt_summaries = [a["entry"]["summary"] for a in payload["alternates"]]
    assert "use jsonl not sqlite" in alt_summaries
    # The unrelated cli.py note is excluded entirely.
    assert "doc the cli" not in alt_summaries


def test_cli_blame_json_target_echoed(repo: Path) -> None:
    _seed(repo)
    payload = json.loads(
        runner.invoke(app, ["blame", "shiplog/store.py:50", "--json"]).output
    )
    assert payload["target"] == {"path": "shiplog/store.py", "line": 50}


def test_cli_blame_table_shows_headline(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["blame", "shiplog/store.py:50"])
    assert result.exit_code == 0, result.output
    assert "lock contention on append" in result.output
    assert "nearest rationale" in result.output


def test_cli_blame_suffix_basename_works(repo: Path) -> None:
    _seed(repo)
    payload = json.loads(
        runner.invoke(app, ["blame", "store.py:50", "--json"]).output
    )
    assert payload["best"]["entry"]["summary"] == "lock contention on append"


def test_cli_blame_no_match_is_graceful(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["blame", "does/not/exist.py:1"])
    assert result.exit_code == 0, result.output  # graceful, not an error
    assert "no log entries touch" in result.output


def test_cli_blame_no_match_json_is_null_best(repo: Path) -> None:
    _seed(repo)
    payload = json.loads(
        runner.invoke(app, ["blame", "does/not/exist.py:1", "--json"]).output
    )
    assert payload["best"] is None
    assert payload["count"] == 0


def test_cli_blame_before_init_fails(repo: Path) -> None:
    result = runner.invoke(app, ["blame", "shiplog/store.py:1"])
    assert result.exit_code == 1
    assert "shiplog init" in result.output


def test_cli_blame_limit_option(repo: Path) -> None:
    _seed(repo)
    # Both store.py entries match line 50; limit 1 keeps only the headline.
    payload = json.loads(
        runner.invoke(app, ["blame", "shiplog/store.py:50", "--limit", "1", "--json"]).output
    )
    assert payload["count"] == 1
    assert payload["alternates"] == []
