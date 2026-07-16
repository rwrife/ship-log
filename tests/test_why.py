"""Tests for ``shiplog why <path>`` (#45).

Two layers, mirroring the rest of the suite:

* **Pure ranking** against :mod:`shiplog.why` -- exact vs suffix vs directory
  prefix matching, ``--depth`` capping, dead-end boosting, newest-first ordering
  within a bucket, the empty case, and the ``--json`` dict shape. Fast, no
  git/CLI.
* **CLI end-to-end** through Typer against a throwaway git repo -- the headline
  verdict, ``--json``, ``--type``/``--since``/``--depth`` filters, and the
  graceful "nothing touches this path" message.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shiplog.cli import app
from shiplog.models import Entry
from shiplog.why import (
    MATCH_EXACT,
    MATCH_PREFIX,
    MATCH_SUFFIX,
    _strip_linespec,
    why,
    why_to_dict,
)

runner = CliRunner()


def _entry(summary: str = "x", **kw: object) -> Entry:
    return Entry(summary=summary, **kw)  # type: ignore[arg-type]


# -- linespec stripping ---------------------------------------------------


def test_strip_linespec_range() -> None:
    assert _strip_linespec("shiplog/store.py:40-80") == "shiplog/store.py"


def test_strip_linespec_single_line() -> None:
    assert _strip_linespec("shiplog/store.py:42") == "shiplog/store.py"


def test_strip_linespec_plain_path_untouched() -> None:
    assert _strip_linespec("shiplog/store.py") == "shiplog/store.py"


def test_strip_linespec_non_numeric_suffix_kept() -> None:
    # A colon that isn't a line spec (e.g. a windows-y or scheme-y token) is left.
    assert _strip_linespec("weird:token") == "weird:token"


# -- match classification -------------------------------------------------


def test_exact_match_beats_suffix_label() -> None:
    e = _entry(files=["shiplog/store.py"])
    result = why([e], "shiplog/store.py")
    assert result.hits[0].match_kind == MATCH_EXACT


def test_suffix_match() -> None:
    e = _entry(files=["shiplog/store.py"])
    result = why([e], "store.py")
    assert len(result.hits) == 1
    assert result.hits[0].match_kind == MATCH_SUFFIX


def test_prefix_match_directory() -> None:
    e = _entry(files=["shiplog/store.py"])
    result = why([e], "shiplog")
    assert len(result.hits) == 1
    assert result.hits[0].match_kind == MATCH_PREFIX


def test_linespec_stripped_before_match() -> None:
    e = _entry(files=["shiplog/store.py:40-80"])
    result = why([e], "shiplog/store.py")
    assert len(result.hits) == 1
    assert result.hits[0].match_kind == MATCH_EXACT


def test_no_match_returns_empty() -> None:
    e = _entry(files=["shiplog/cli.py"])
    result = why([e], "shiplog/store.py")
    assert result.hits == []
    assert "nothing logged touching" in result.headline


def test_empty_path_matches_nothing() -> None:
    e = _entry(files=["shiplog/store.py"])
    assert why([e], "   ").hits == []


# -- depth ----------------------------------------------------------------


def test_depth_zero_disables_prefix() -> None:
    e = _entry(files=["shiplog/store.py"])
    # Directory query with depth 0 => no prefix match, nothing found.
    assert why([e], "shiplog", depth=0).hits == []


def test_depth_zero_still_allows_exact_and_suffix() -> None:
    e = _entry(files=["shiplog/store.py"])
    assert why([e], "shiplog/store.py", depth=0).hits[0].match_kind == MATCH_EXACT
    assert why([e], "store.py", depth=0).hits[0].match_kind == MATCH_SUFFIX


def test_depth_one_excludes_deeper_nesting() -> None:
    direct = _entry("direct", files=["shiplog/store.py"])
    nested = _entry("nested", files=["shiplog/sub/deep.py"])
    result = why([direct, nested], "shiplog", depth=1)
    summaries = [h.entry.summary for h in result.hits]
    assert "direct" in summaries
    assert "nested" not in summaries


def test_depth_none_matches_any_descendant() -> None:
    nested = _entry("nested", files=["shiplog/sub/deep.py"])
    assert len(why([nested], "shiplog").hits) == 1


# -- ordering / boosting --------------------------------------------------


def test_deadends_boosted_above_decisions_and_notes() -> None:
    note = _entry("note", type="note", files=["p.py"], ts="2026-07-16T12:00:00Z")
    decision = _entry("dec", type="decision", files=["p.py"], ts="2026-07-16T11:00:00Z")
    deadend = _entry("de", type="deadend", files=["p.py"], ts="2026-07-16T10:00:00Z")
    result = why([note, decision, deadend], "p.py")
    kinds = [h.entry.type.value for h in result.hits]
    # Dead-end first despite being oldest; decision before note.
    assert kinds == ["deadend", "decision", "note"]


def test_newest_first_within_bucket() -> None:
    old = _entry("old", type="decision", files=["p.py"], ts="2026-07-10T00:00:00Z")
    new = _entry("new", type="decision", files=["p.py"], ts="2026-07-16T00:00:00Z")
    result = why([old, new], "p.py")
    assert [h.entry.summary for h in result.hits] == ["new", "old"]


def test_missing_ts_sorts_last_in_bucket() -> None:
    dated = _entry("dated", type="decision", files=["p.py"], ts="2026-07-16T00:00:00Z")
    undated = _entry("undated", type="decision", files=["p.py"], ts="")
    result = why([undated, dated], "p.py")
    assert [h.entry.summary for h in result.hits] == ["dated", "undated"]


def test_limit_caps_hits() -> None:
    entries = [_entry(f"e{i}", type="note", files=["p.py"]) for i in range(5)]
    assert len(why(entries, "p.py", limit=2).hits) == 2


# -- headline / json shape ------------------------------------------------


def test_headline_counts() -> None:
    deadend = _entry(type="deadend", files=["store.py"])
    d1 = _entry(type="decision", files=["store.py"])
    d2 = _entry(type="decision", files=["store.py"])
    result = why([deadend, d1, d2], "store.py")
    assert result.headline == "1 dead-end, 2 decisions touching store.py"


def test_headline_uses_basename_for_dir() -> None:
    e = _entry(type="deadend", files=["shiplog/store.py"])
    result = why([e], "shiplog/")
    assert result.headline.endswith("touching shiplog")


def test_json_shape() -> None:
    deadend = _entry("de", type="deadend", files=["store.py"])
    result = why([deadend], "store.py")
    d = why_to_dict(result)
    assert d["path"] == "store.py"
    assert d["depth"] is None
    assert d["deadends"] == 1
    assert d["decisions"] == 0
    assert d["count"] == 1
    assert d["hits"][0]["match_kind"] == MATCH_EXACT
    assert d["hits"][0]["entry"]["summary"] == "de"
    assert "headline" in d


# -- CLI end-to-end -------------------------------------------------------


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
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(
        app,
        ["add", "deadend", "global cache went stale",
         "--files", "shiplog/store.py", "--why", "invalidation too hard"],
    ).exit_code == 0
    assert runner.invoke(
        app,
        ["add", "decision", "jsonl append only",
         "--files", "shiplog/store.py:1-40", "--why", "diff friendly"],
    ).exit_code == 0
    assert runner.invoke(
        app, ["add", "note", "cli uses typer", "--files", "shiplog/cli.py"]
    ).exit_code == 0


def test_cli_why_json_ranks_deadend_first(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["why", "shiplog/store.py", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["count"] == 2
    assert payload["deadends"] == 1
    assert payload["hits"][0]["entry"]["type"] == "deadend"
    # cli.py note is not part of a store.py rollup.
    summaries = [h["entry"]["summary"] for h in payload["hits"]]
    assert "cli uses typer" not in summaries


def test_cli_why_directory_prefix(repo: Path) -> None:
    _seed(repo)
    payload = json.loads(runner.invoke(app, ["why", "shiplog", "--json"]).output)
    # All three entries live under shiplog/.
    assert payload["count"] == 3


def test_cli_why_type_filter(repo: Path) -> None:
    _seed(repo)
    payload = json.loads(
        runner.invoke(app, ["why", "shiplog", "--type", "deadend", "--json"]).output
    )
    assert payload["count"] == 1
    assert payload["hits"][0]["entry"]["type"] == "deadend"


def test_cli_why_depth_zero_excludes_prefix(repo: Path) -> None:
    _seed(repo)
    payload = json.loads(
        runner.invoke(app, ["why", "shiplog", "--depth", "0", "--json"]).output
    )
    assert payload["count"] == 0


def test_cli_why_headline_rendered(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["why", "shiplog/store.py"])
    assert result.exit_code == 0, result.output
    assert "1 dead-end" in result.output
    assert "global cache went stale" in result.output


def test_cli_why_empty_is_graceful(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["why", "does/not/exist.py"])
    assert result.exit_code == 0
    assert "no log entries touch" in result.output


def test_cli_why_bad_type_errors(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["why", "shiplog", "--type", "bogus"])
    assert result.exit_code != 0


def test_cli_why_negative_depth_errors(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["why", "shiplog", "--depth", "-1"])
    assert result.exit_code != 0
