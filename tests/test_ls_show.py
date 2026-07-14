"""Tests for M4 CLI: ``ls`` (filters + table) and ``show`` (detail + ``--json``).

End-to-end through Typer against a real throwaway git repo, mirroring the M3 test
setup. Focus areas required by #4: filtering behavior, ``--json`` output *shape*
(array for ``ls``, object for ``show``), id/prefix resolution, and friendly error
exits.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shiplog.cli import app

runner = CliRunner()


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


def _seed(repo: Path, monkeypatch: pytest.MonkeyPatch | None = None) -> None:
    """init + a few entries spanning types/tags/files for filter tests.

    When ``monkeypatch`` is supplied the entry clock is frozen so all seeded
    entries share an identical timestamp second. Otherwise ordering across a
    second boundary is non-deterministic (see test_ls_json_is_array_newest_first).
    """
    if monkeypatch is not None:
        from datetime import UTC, datetime

        from shiplog import models

        frozen = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)

        class _FrozenDT(datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                return frozen if tz is None else frozen.astimezone(tz)

        monkeypatch.setattr(models, "datetime", _FrozenDT)
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(
        app,
        ["add", "decision", "use jsonl", "--tags", "storage,core",
         "--files", "shiplog/store.py", "--why", "diffable"],
    ).exit_code == 0
    assert runner.invoke(
        app,
        ["add", "deadend", "global cache", "--tags", "perf",
         "--files", "shiplog/store.py"],
    ).exit_code == 0
    assert runner.invoke(app, ["add", "note", "doc the cli", "--tags", "docs"]).exit_code == 0


# -- ls: json shape + filters --------------------------------------------


def test_ls_json_is_array_newest_first(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Freeze the clock so all three entries share one timestamp second: the
    # ordering guarantee under test is "ties preserve append order", which is
    # only exercised when the timestamps actually tie. Relying on wall-clock
    # ties is flaky on slow runners that straddle a second boundary.
    _seed(repo, monkeypatch)
    result = runner.invoke(app, ["ls", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert len(data) == 3
    # Timestamps tie; newest-first then preserves append order (stable sort),
    # i.e. the first-added ("use jsonl") leads and the last-added ("doc the cli")
    # trails. Cross-second ordering is covered in test_filters.py.
    assert data[0]["summary"] == "use jsonl"
    assert data[-1]["summary"] == "doc the cli"
    # Stable entry shape (keys present for agents).
    for key in ("id", "type", "summary", "ts", "author", "branch", "tags", "files"):
        assert key in data[0]


def test_ls_empty_json_is_empty_array(repo: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["ls", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == []


def test_ls_filter_by_type(repo: Path) -> None:
    _seed(repo)
    data = json.loads(runner.invoke(app, ["ls", "--type", "deadend", "--json"]).output)
    assert [e["summary"] for e in data] == ["global cache"]


def test_ls_filter_by_tag(repo: Path) -> None:
    _seed(repo)
    data = json.loads(runner.invoke(app, ["ls", "--tag", "storage", "--json"]).output)
    assert [e["summary"] for e in data] == ["use jsonl"]


def test_ls_filter_by_file_suffix(repo: Path) -> None:
    _seed(repo)
    data = json.loads(runner.invoke(app, ["ls", "--file", "store.py", "--json"]).output)
    # Both the decision and the deadend reference shiplog/store.py.
    assert {e["summary"] for e in data} == {"use jsonl", "global cache"}


def test_ls_limit(repo: Path) -> None:
    _seed(repo)
    data = json.loads(runner.invoke(app, ["ls", "--limit", "2", "--json"]).output)
    assert len(data) == 2


def test_ls_bad_type_is_friendly_error(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["ls", "--type", "wat"])
    assert result.exit_code == 1
    assert "unknown entry type" in result.output


def test_ls_bad_since_is_friendly_error(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["ls", "--since", "yesterday"])
    assert result.exit_code == 1
    assert "--since" in result.output


def test_ls_before_init_fails(repo: Path) -> None:
    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 1
    assert "shiplog init" in result.output


def test_ls_table_lists_entries(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0
    # The table shows summaries (the exact id varies by run).
    assert "use jsonl" in result.output
    assert "global cache" in result.output


# -- show: json object + id resolution -----------------------------------


def test_show_json_is_object(repo: Path) -> None:
    _seed(repo)
    listed = json.loads(runner.invoke(app, ["ls", "--json"]).output)
    target = listed[0]["id"]
    result = runner.invoke(app, ["show", target, "--json"])
    assert result.exit_code == 0, result.output
    obj = json.loads(result.output)
    assert isinstance(obj, dict)
    assert obj["id"] == target


def test_show_resolves_unique_prefix(repo: Path) -> None:
    _seed(repo)
    full = json.loads(runner.invoke(app, ["ls", "--json"]).output)[0]["id"]
    # Use a long-enough prefix to be unique (the 6-char random suffix start).
    prefix = full[:10]
    result = runner.invoke(app, ["show", prefix, "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["id"] == full


def test_show_missing_id_fails(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["show", "ZZZZZZ"])
    assert result.exit_code == 1
    assert "no entry with id" in result.output


def test_show_ambiguous_prefix_fails(repo: Path) -> None:
    _seed(repo)
    # All ids share the YYMMDD date prefix → ambiguous on its own.
    full = json.loads(runner.invoke(app, ["ls", "--json"]).output)[0]["id"]
    date_prefix = full.split("-", 1)[0]
    result = runner.invoke(app, ["show", date_prefix])
    assert result.exit_code == 1
    assert "ambiguous" in result.output


def test_show_detail_contains_why_and_branch(repo: Path) -> None:
    _seed(repo)
    # The decision entry has a why + branch; find it via the type filter.
    dec = json.loads(runner.invoke(app, ["ls", "--type", "decision", "--json"]).output)[0]
    result = runner.invoke(app, ["show", dec["id"]])
    assert result.exit_code == 0
    assert "diffable" in result.output  # why
    assert "main" in result.output  # branch
