"""Tests for ``shiplog ask``: lexical retrieval that answers a specific question.

Two layers, mirroring ``test_brief``:

* **Pure** (`shiplog.ask`) -- deterministic scoring/ranking over hand-built
  :class:`~shiplog.models.Entry` lists: ranking order, the dead-end boost,
  empty-result behavior, and the verdict/JSON contract. No git/CLI needed.
* **End-to-end** (Typer) -- the ``ask`` command against a throwaway git repo,
  asserting rendered output, ``--json``, ``--limit``, and filter interaction.

Covers issue #40 acceptance criteria: top-k relevant entries with dead-ends
surfaced/boosted, pure-stdlib scoring, ``--type``/``--file``/``--since`` filters,
a one-line verdict, ``--json`` scored results, and ``--limit`` capping.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shiplog.ask import ask_to_dict, build_ask, score_entries, tokenize
from shiplog.cli import app
from shiplog.models import Entry, EntryType

runner = CliRunner()


# -- pure scoring / ranking ------------------------------------------------


def _entry(
    summary: str,
    type_: str,
    *,
    ts: str = "2026-06-01T00:00:00Z",
    why: str = "",
    files: list[str] | None = None,
    tags: list[str] | None = None,
) -> Entry:
    return Entry(
        summary=summary,
        type=EntryType.coerce(type_),
        ts=ts,
        why=why,
        files=files or [],
        tags=tags or [],
    )


def test_tokenize_lowercases_and_splits_alnum() -> None:
    assert tokenize("Redis Cache-Layer v3!") == ["redis", "cache", "layer", "v3"]


def test_only_matching_entries_are_returned() -> None:
    entries = [
        _entry("Tried Redis for caching", "attempt"),
        _entry("Switched to Postgres", "decision"),
    ]
    hits = score_entries(entries, "redis")
    assert [h.entry.summary for h in hits] == ["Tried Redis for caching"]


def test_deadend_boost_outranks_equally_relevant_note() -> None:
    # Identical text so raw BM25 is equal; the type boost must float the deadend up.
    entries = [
        _entry("redis cache layer", "note"),
        _entry("redis cache layer", "deadend"),
    ]
    hits = score_entries(entries, "redis cache layer")
    assert hits[0].entry.type.value == "deadend"
    assert hits[0].score > hits[1].score


def test_higher_term_overlap_ranks_first() -> None:
    entries = [
        _entry("redis", "note"),
        _entry("redis cache eviction policy", "note", why="redis ttl tuning"),
    ]
    hits = score_entries(entries, "redis cache eviction")
    assert hits[0].entry.summary == "redis cache eviction policy"


def test_why_files_tags_are_searchable() -> None:
    entries = [
        _entry("cache work", "decision", why="chose redis", tags=["infra"]),
        _entry("layout tweak", "note", files=["redis_client.py"]),
    ]
    assert len(score_entries(entries, "redis")) == 2


def test_empty_query_and_empty_corpus_return_no_hits() -> None:
    assert score_entries([_entry("redis", "note")], "") == []
    assert score_entries([], "redis") == []


def test_build_ask_counts_and_verdict() -> None:
    entries = [
        _entry("redis cache deadend", "deadend"),
        _entry("redis cache decision", "decision"),
        _entry("redis cache attempt", "attempt"),
        _entry("unrelated postgres note", "note"),
    ]
    result = build_ask(entries, "redis cache", limit=5)
    assert result.total_matches == 3  # postgres note excluded
    assert result.deadends == 1
    assert result.decisions == 1
    v = result.verdict()
    assert v.startswith("Yes")
    assert "1 dead-end" in v and "1 decision" in v


def test_build_ask_limit_caps_but_counts_are_full() -> None:
    entries = [_entry(f"redis attempt {i}", "attempt") for i in range(6)]
    entries.append(_entry("redis deadend", "deadend"))
    result = build_ask(entries, "redis", limit=2)
    assert len(result.hits) == 2
    assert result.total_matches == 7
    assert result.truncated == 5
    # Even capped, the deadend (boosted) is the top hit.
    assert result.hits[0].entry.type.value == "deadend"


def test_build_ask_limit_zero_no_cap() -> None:
    entries = [_entry(f"redis {i}", "note") for i in range(10)]
    result = build_ask(entries, "redis", limit=0)
    assert len(result.hits) == 10
    assert result.truncated == 0


def test_build_ask_empty_result_verdict() -> None:
    result = build_ask([_entry("postgres only", "note")], "kubernetes", limit=5)
    assert result.total_matches == 0
    assert result.hits == []
    assert "No matches" in result.verdict()


def test_link_entries_excluded() -> None:
    link = Entry(summary="linked", type=EntryType.LINK, link_target="x", ref="redis")
    result = build_ask([link, _entry("redis note", "note")], "redis", limit=5)
    assert result.total_matches == 1
    assert result.hits[0].entry.type.value == "note"


def test_ask_to_dict_shape() -> None:
    result = build_ask([_entry("redis cache deadend", "deadend")], "redis", limit=5)
    obj = ask_to_dict(result)
    for key in ("query", "verdict", "hits", "total", "shown", "truncated",
                "deadends", "decisions"):
        assert key in obj
    assert obj["hits"][0]["entry"]["type"] == "deadend"
    assert isinstance(obj["hits"][0]["score"], float)


# -- end-to-end CLI --------------------------------------------------------


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
    runner.invoke(app, ["add", "deadend", "Redis cache added ops overhead",
                        "--why", "single-node redis complicated deploys",
                        "--files", "cache.py", "--tags", "cache"])
    runner.invoke(app, ["add", "decision", "Use in-process LRU cache",
                        "--files", "cache.py", "--tags", "cache"])
    runner.invoke(app, ["add", "note", "Docs need a caching section",
                        "--files", "README.md"])


def test_ask_ranks_deadend_first(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["ask", "have we tried redis for cache?"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Redis cache added ops overhead" in out
    # Verdict leads; deadend body precedes the decision body.
    assert out.index("Redis cache added ops overhead") < out.index("in-process LRU")


def test_ask_json_shape_and_ranking(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["ask", "redis cache", "--json"])
    assert result.exit_code == 0, result.output
    obj = json.loads(result.output)
    assert obj["hits"][0]["entry"]["type"] == "deadend"
    assert obj["deadends"] == 1
    assert obj["shown"] == len(obj["hits"])
    assert obj["verdict"].startswith("Yes")


def test_ask_limit_caps_hits(repo: Path) -> None:
    runner.invoke(app, ["init"])
    for i in range(6):
        runner.invoke(app, ["add", "note", f"redis note {i}"])
    obj = json.loads(runner.invoke(app, ["ask", "redis", "--limit", "2", "--json"]).output)
    assert obj["shown"] == 2
    assert obj["total"] == 6
    assert obj["truncated"] == 4


def test_ask_type_filter(repo: Path) -> None:
    _seed(repo)
    obj = json.loads(
        runner.invoke(app, ["ask", "cache", "--type", "decision", "--json"]).output
    )
    assert obj["total"] == 1
    assert obj["hits"][0]["entry"]["type"] == "decision"


def test_ask_file_filter(repo: Path) -> None:
    _seed(repo)
    obj = json.loads(
        runner.invoke(app, ["ask", "caching", "--file", "README.md", "--json"]).output
    )
    # Only the README note references that file.
    assert all("README.md" in h["entry"]["files"] for h in obj["hits"])
    assert obj["total"] == 1


def test_ask_no_matches_is_friendly(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["ask", "kubernetes helm"])
    assert result.exit_code == 0
    assert "No matches" in result.output


def test_ask_before_init_fails(repo: Path) -> None:
    result = runner.invoke(app, ["ask", "redis"])
    assert result.exit_code == 1


def test_ask_bad_type_fails(repo: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["ask", "redis", "--type", "bogus"])
    assert result.exit_code == 1
