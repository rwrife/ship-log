"""Tests for ``shiplog guard`` — the enforcing pre-commit dead-end tripwire (#38).

Split like ``test_hooks``: the file-overlap / ack / blocking logic is unit-tested
directly against ``shiplog.guard`` for speed and precision, while install/uninstall
and the end-to-end ack/override flow run against real throwaway git repos through
the CLI so hook-path resolution, perms, and exit-code propagation are exercised for
real.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shiplog import guard
from shiplog.cli import app
from shiplog.models import Entry, EntryType

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
    (tmp_path / "seed.txt").write_text("hi\n")
    _git("add", "seed.txt", cwd=tmp_path)
    _git("commit", "-m", "init", cwd=tmp_path)
    monkeypatch.chdir(tmp_path)
    from shiplog import gitctx

    gitctx.find_repo_root.cache_clear()
    return tmp_path


def _hook_file(repo: Path) -> Path:
    return repo / ".git" / "hooks" / guard.HOOK_NAME


def _deadend(
    summary: str, files: list[str], *, id_: str = "d1", ts: str = "2026-01-01T00:00:00Z"
) -> Entry:
    return Entry(summary=summary, type=EntryType.DEADEND, id=id_, ts=ts, files=files)


def _ack(target: str, *, id_: str = "a1") -> Entry:
    return Entry(summary=f"ack {target}", type=EntryType.ACK, id=id_, link_target=target)


# -- unit: path matching ------------------------------------------------------


def test_path_part_strips_line_anchor() -> None:
    assert guard._path_part("db.py:40-80") == "db.py"
    assert guard._path_part("db.py:12") == "db.py"


def test_path_part_keeps_non_line_colons() -> None:
    # A trailing colon segment that isn't a line number is left intact.
    assert guard._path_part("a/b.py") == "a/b.py"
    assert guard._path_part("weird:name.py") == "weird:name.py"


def test_paths_overlap_exact_and_anchored() -> None:
    assert guard.paths_overlap(["db.py:40-80"], {"db.py", "x.py"}) == ["db.py"]
    assert guard.paths_overlap(["db.py"], {"other.py"}) == []


def test_paths_overlap_empty_files_never_matches() -> None:
    assert guard.paths_overlap([], {"db.py"}) == []


# -- unit: ack + blocking -----------------------------------------------------


def test_blocking_hit() -> None:
    entries = [_deadend("boom", ["db.py"])]
    blocks = guard.blocking_deadends(entries, {"db.py"})
    assert len(blocks) == 1
    assert blocks[0].entry.id == "d1"
    assert blocks[0].files == ["db.py"]


def test_blocking_miss_unrelated_file() -> None:
    entries = [_deadend("boom", ["db.py"])]
    assert guard.blocking_deadends(entries, {"api.py"}) == []


def test_blocking_suppressed_by_ack() -> None:
    entries = [_deadend("boom", ["db.py"], id_="d1"), _ack("d1")]
    assert guard.blocking_deadends(entries, {"db.py"}) == []


def test_fileless_deadend_never_blocks() -> None:
    entries = [_deadend("vague regret", [])]
    assert guard.blocking_deadends(entries, {"db.py"}) == []


def test_non_deadend_entries_ignored() -> None:
    entries = [Entry(summary="a decision", type=EntryType.DECISION, files=["db.py"])]
    assert guard.blocking_deadends(entries, {"db.py"}) == []


def test_blocks_sorted_newest_first() -> None:
    entries = [
        _deadend("old", ["db.py"], id_="d1", ts="2026-01-01T00:00:00Z"),
        _deadend("new", ["db.py"], id_="d2", ts="2026-06-01T00:00:00Z"),
    ]
    blocks = guard.blocking_deadends(entries, {"db.py"})
    assert [b.entry.id for b in blocks] == ["d2", "d1"]


# -- unit: env override -------------------------------------------------------


@pytest.mark.parametrize("val", ["off", "0", "false", "NO", "Skip"])
def test_env_override_disables(val: str) -> None:
    assert guard.guard_disabled_via_env({"SHIPLOG_GUARD": val}) is True


@pytest.mark.parametrize("val", ["", "on", "1", "please"])
def test_env_override_absent_or_on(val: str) -> None:
    assert guard.guard_disabled_via_env({"SHIPLOG_GUARD": val}) is False


# -- CLI: install / status / uninstall ----------------------------------------


def test_install_creates_executable_hook(repo: Path) -> None:
    result = runner.invoke(app, ["guard", "install"])
    assert result.exit_code == 0, result.output
    hook = _hook_file(repo)
    assert hook.exists()
    assert guard.is_ours(hook.read_text(encoding="utf-8"))
    # Executable bit set.
    assert hook.stat().st_mode & 0o111


def test_install_idempotent(repo: Path) -> None:
    runner.invoke(app, ["guard", "install"])
    result = runner.invoke(app, ["guard", "install"])
    assert result.exit_code == 0
    assert "already installed" in result.output


def test_status_reports_installed(repo: Path) -> None:
    runner.invoke(app, ["guard", "install"])
    result = runner.invoke(app, ["guard", "status"])
    assert "installed" in result.output


def test_uninstall_removes_pure_hook(repo: Path) -> None:
    runner.invoke(app, ["guard", "install"])
    result = runner.invoke(app, ["guard", "uninstall"])
    assert result.exit_code == 0
    assert not _hook_file(repo).exists()


def test_install_refuses_foreign_hook_without_force(repo: Path) -> None:
    hook = _hook_file(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    result = runner.invoke(app, ["guard", "install"])
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_install_force_appends_to_foreign_hook(repo: Path) -> None:
    hook = _hook_file(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    result = runner.invoke(app, ["guard", "install", "--force"])
    assert result.exit_code == 0
    text = hook.read_text(encoding="utf-8")
    assert "echo hi" in text and guard.is_ours(text)


def test_uninstall_strips_only_our_block(repo: Path) -> None:
    hook = _hook_file(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    runner.invoke(app, ["guard", "install", "--force"])
    runner.invoke(app, ["guard", "uninstall"])
    text = hook.read_text(encoding="utf-8")
    assert "echo hi" in text
    assert not guard.is_ours(text)


# -- CLI: end-to-end hit / ack / override -------------------------------------


def _init_and_deadend(repo: Path) -> str:
    runner.invoke(app, ["init"])
    runner.invoke(
        app,
        ["add", "deadend", "asyncpg deadlocks", "--why", "pool", "--files", "db.py"],
    )
    from shiplog.store import Store

    entries = Store.for_repo(repo).read_all()
    return entries[0].id


def test_check_blocks_and_json_report(repo: Path) -> None:
    _init_and_deadend(repo)
    (repo / "db.py").write_text("print(1)\n")
    _git("add", "db.py", cwd=repo)

    check = runner.invoke(app, ["guard", "_check"])
    assert check.exit_code == 2

    report = runner.invoke(app, ["guard", "--json"])
    assert report.exit_code == 0
    assert '"count": 1' in report.output


def test_ack_clears_the_block(repo: Path) -> None:
    dead_id = _init_and_deadend(repo)
    (repo / "db.py").write_text("print(1)\n")
    _git("add", "db.py", cwd=repo)

    ack = runner.invoke(app, ["guard", "--ack", dead_id])
    assert ack.exit_code == 0, ack.output

    check = runner.invoke(app, ["guard", "_check"])
    assert check.exit_code == 0


def test_ack_rejects_non_deadend(repo: Path) -> None:
    runner.invoke(app, ["init"])
    runner.invoke(app, ["add", "decision", "chose X", "--files", "db.py"])
    from shiplog.store import Store

    dec_id = Store.for_repo(repo).read_all()[0].id
    result = runner.invoke(app, ["guard", "--ack", dec_id])
    assert result.exit_code != 0
    assert "not a dead-end" in result.output


def test_real_commit_blocked_then_overridden(repo: Path) -> None:
    _init_and_deadend(repo)
    runner.invoke(app, ["guard", "install"])
    (repo / "db.py").write_text("print(1)\n")
    _git("add", "db.py", cwd=repo)

    # A normal commit must fail (hook exits non-zero).
    blocked = subprocess.run(
        ["git", "commit", "-m", "touch db"], cwd=repo, capture_output=True, text=True
    )
    assert blocked.returncode != 0

    # The env override lets it through.
    import os

    env = {**os.environ, "SHIPLOG_GUARD": "off"}
    ok = subprocess.run(
        ["git", "commit", "-m", "override"], cwd=repo, capture_output=True, text=True, env=env
    )
    assert ok.returncode == 0, ok.stderr


def test_check_clear_when_no_log(repo: Path) -> None:
    # No `shiplog init` yet — guard must not wedge the repo.
    result = runner.invoke(app, ["guard", "_check"])
    assert result.exit_code == 0
