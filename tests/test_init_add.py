"""Tests for M3: ``init`` (idempotency) + ``add`` (git-stamped entries).

These exercise the CLI end-to-end against a real throwaway git repo so the
git-context capture (author/branch/sha) is covered for real, not mocked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shiplog.cli import app
from shiplog.config import CONFIG_FILENAME, Config
from shiplog.models import EntryType
from shiplog.store import LOG_FILENAME, SHIPLOG_DIR, Store

runner = CliRunner()


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh git repo (one commit) with cwd set to it; clears gitctx cache."""
    _git("init", cwd=tmp_path)
    _git("config", "user.name", "Test Captain", cwd=tmp_path)
    _git("config", "user.email", "cap@ship.log", cwd=tmp_path)
    _git("checkout", "-b", "main", cwd=tmp_path)
    (tmp_path / "a.txt").write_text("hi\n")
    _git("add", "a.txt", cwd=tmp_path)
    _git("commit", "-m", "init", cwd=tmp_path)
    monkeypatch.chdir(tmp_path)
    # find_repo_root is lru_cache'd; drop entries so each temp repo is seen fresh.
    from shiplog import gitctx

    gitctx.find_repo_root.cache_clear()
    return tmp_path


# -- init -----------------------------------------------------------------


def test_init_creates_dir_log_and_config(repo: Path) -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    shiplog_dir = repo / SHIPLOG_DIR
    assert shiplog_dir.is_dir()
    assert (shiplog_dir / LOG_FILENAME).exists()
    assert (shiplog_dir / CONFIG_FILENAME).exists()
    assert "created" in result.output


def test_init_is_idempotent(repo: Path) -> None:
    first = runner.invoke(app, ["init"])
    assert first.exit_code == 0
    # Write an entry, then re-init: the log must be left intact.
    log = repo / SHIPLOG_DIR / LOG_FILENAME
    log.write_text('{"summary":"x","type":"note"}\n', encoding="utf-8")
    before = log.read_text(encoding="utf-8")

    second = runner.invoke(app, ["init"])
    assert second.exit_code == 0
    assert log.read_text(encoding="utf-8") == before  # log untouched
    assert "present" in second.output  # log reported as already there
    assert "kept" in second.output  # config left as-is


def test_init_force_rewrites_config_but_not_log(repo: Path) -> None:
    runner.invoke(app, ["init"])
    log = repo / SHIPLOG_DIR / LOG_FILENAME
    log.write_text('{"summary":"keep me","type":"note"}\n', encoding="utf-8")
    config = repo / SHIPLOG_DIR / CONFIG_FILENAME
    config.write_text('default_type = "decision"\n', encoding="utf-8")

    result = runner.invoke(app, ["init", "--force"])
    assert result.exit_code == 0
    assert "rewritten" in result.output
    # Forcing rewrites config back to defaults but never the log.
    assert 'default_type = "note"' in config.read_text(encoding="utf-8")
    assert "keep me" in log.read_text(encoding="utf-8")


def test_init_outside_git_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    from shiplog import gitctx

    gitctx.find_repo_root.cache_clear()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 1
    assert "not inside a git repository" in result.output


# -- add ------------------------------------------------------------------


def test_add_writes_entry_with_git_context(repo: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(
        app,
        [
            "add",
            "decision",
            "use JSONL for storage",
            "--why",
            "diffable + merge-friendly",
            "--files",
            "shiplog/store.py,shiplog/models.py",
            "--tags",
            "storage,arch",
            "--ref",
            "#2",
        ],
    )
    assert result.exit_code == 0, result.output

    entries = Store.for_repo(repo).read_all()
    assert len(entries) == 1
    e = entries[0]
    assert e.type == EntryType.DECISION
    assert e.summary == "use JSONL for storage"
    assert e.why == "diffable + merge-friendly"
    assert e.files == ["shiplog/store.py", "shiplog/models.py"]
    assert e.tags == ["storage", "arch"]
    assert e.ref == "#2"
    # Git context auto-captured.
    assert e.author == "Test Captain <cap@ship.log>"
    assert e.branch == "main"
    assert e.sha  # short HEAD sha, non-empty (there is a commit)
    assert e.ts.endswith("Z")
    assert e.id  # generated id present


def test_add_minimal_note_defaults(repo: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["add", "note", "just a quick reminder"])
    assert result.exit_code == 0, result.output
    e = Store.for_repo(repo).read_all()[0]
    assert e.type == EntryType.NOTE
    assert e.summary == "just a quick reminder"
    assert e.why == ""
    assert e.files == []
    assert e.tags == []


def test_add_appends_multiple_in_order(repo: Path) -> None:
    runner.invoke(app, ["init"])
    runner.invoke(app, ["add", "note", "first"])
    runner.invoke(app, ["add", "attempt", "second"])
    entries = Store.for_repo(repo).read_all()
    assert [e.summary for e in entries] == ["first", "second"]


def test_add_unknown_type_is_friendly_error(repo: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["add", "wibble", "x"])
    assert result.exit_code == 1
    assert "unknown entry type" in result.output
    # Nothing should have been written.
    assert Store.for_repo(repo).count() == 0


def test_add_empty_summary_rejected(repo: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["add", "note", "   "])
    assert result.exit_code == 1
    assert "summary must not be empty" in result.output


def test_add_before_init_fails(repo: Path) -> None:
    # Repo exists but no `shiplog init` was run.
    result = runner.invoke(app, ["add", "note", "x"])
    assert result.exit_code == 1
    assert "shiplog init" in result.output


def test_add_respects_config_author_override(repo: Path) -> None:
    runner.invoke(app, ["init"])
    config = repo / SHIPLOG_DIR / CONFIG_FILENAME
    config.write_text('author = "Override Bot"\nschema_version = 1\n', encoding="utf-8")
    # Sanity: config loader picks up the override.
    assert Config.load(repo).author == "Override Bot"

    runner.invoke(app, ["add", "note", "who am I"])
    e = Store.for_repo(repo).read_all()[0]
    assert e.author == "Override Bot"
