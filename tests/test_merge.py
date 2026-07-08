"""End-to-end tests for the union merge driver + ``fix`` CLI (issue #31).

Runs against real throwaway git repos so the whole path is exercised for real:
installing the driver (``.gitattributes`` + ``.git/config``), an actual
``git merge`` of two divergent logs producing a conflict-free deterministic union,
and the ``shiplog fix`` repair command's ``--check`` / ``--write`` behavior. The
pure normalization logic is unit-tested in ``test_merge_unit.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shiplog import merge
from shiplog.cli import app
from shiplog.models import Entry, EntryType
from shiplog.store import Store

runner = CliRunner()


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


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


def _init_shiplog(repo: Path) -> None:
    assert runner.invoke(app, ["init"]).exit_code == 0
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "shiplog init", cwd=repo)


# -- installer ----------------------------------------------------------------


def test_install_writes_gitattributes_and_config(repo: Path) -> None:
    result = runner.invoke(app, ["install-merge-driver"])
    assert result.exit_code == 0, result.output

    attrs = (repo / ".gitattributes").read_text(encoding="utf-8")
    assert merge.ATTR_BEGIN in attrs and merge.ATTR_END in attrs
    assert merge.ATTR_LINE in attrs

    driver = _git(
        "config", "--local", "--get", "merge.shiplog.driver", cwd=repo
    ).stdout.strip()
    assert driver == merge.DRIVER_COMMAND


def test_install_is_idempotent(repo: Path) -> None:
    first = runner.invoke(app, ["install-merge-driver"])
    assert first.exit_code == 0
    before = (repo / ".gitattributes").read_text(encoding="utf-8")
    second = runner.invoke(app, ["install-merge-driver"])
    assert second.exit_code == 0
    after = (repo / ".gitattributes").read_text(encoding="utf-8")
    assert before == after  # no churn on re-install
    assert "already installed" in second.output


def test_install_preserves_foreign_gitattributes(repo: Path) -> None:
    foreign = "*.png binary\n*.md text\n"
    (repo / ".gitattributes").write_text(foreign, encoding="utf-8")
    result = runner.invoke(app, ["install-merge-driver"])
    assert result.exit_code == 0
    text = (repo / ".gitattributes").read_text(encoding="utf-8")
    # Foreign rules kept; our fenced block appended.
    assert "*.png binary" in text
    assert "*.md text" in text
    assert merge.ATTR_LINE in text


def test_status_reports_installed_and_not(repo: Path) -> None:
    not_yet = runner.invoke(app, ["install-merge-driver", "--status"])
    assert not_yet.exit_code == 0
    assert "not installed" in not_yet.output

    runner.invoke(app, ["install-merge-driver"])
    installed = runner.invoke(app, ["install-merge-driver", "--status"])
    assert installed.exit_code == 0
    assert "installed" in installed.output.lower()


def test_uninstall_removes_block_and_config(repo: Path) -> None:
    runner.invoke(app, ["install-merge-driver"])
    result = runner.invoke(app, ["install-merge-driver", "--uninstall"])
    assert result.exit_code == 0

    # .gitattributes was purely ours -> removed entirely.
    assert not (repo / ".gitattributes").exists()
    # git config section gone.
    got = subprocess.run(
        ["git", "config", "--local", "--get", "merge.shiplog.driver"],
        cwd=repo, capture_output=True, text=True,
    )
    assert got.returncode != 0  # unset


def test_uninstall_strips_only_our_block(repo: Path) -> None:
    (repo / ".gitattributes").write_text("*.png binary\n", encoding="utf-8")
    runner.invoke(app, ["install-merge-driver"])
    runner.invoke(app, ["install-merge-driver", "--uninstall"])
    text = (repo / ".gitattributes").read_text(encoding="utf-8")
    assert "*.png binary" in text  # foreign rule survives
    assert merge.ATTR_BEGIN not in text  # our block gone


def test_uninstall_when_absent_is_graceful(repo: Path) -> None:
    result = runner.invoke(app, ["install-merge-driver", "--uninstall"])
    assert result.exit_code == 0
    assert "nothing to remove" in result.output


def test_status_and_uninstall_are_mutually_exclusive(repo: Path) -> None:
    result = runner.invoke(app, ["install-merge-driver", "--uninstall", "--status"])
    assert result.exit_code != 0


# -- real git merge (the headline) --------------------------------------------


def _add(repo: Path, type_: str, summary: str, why: str = "") -> None:
    args = ["add", type_, summary]
    if why:
        args += ["--why", why]
    assert runner.invoke(app, args).exit_code == 0


def test_divergent_branches_merge_conflict_free(repo: Path) -> None:
    _init_shiplog(repo)
    assert runner.invoke(app, ["install-merge-driver"]).exit_code == 0
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "install driver", cwd=repo)

    # Branch A appends two entries.
    _git("checkout", "-b", "feat-a", cwd=repo)
    _add(repo, "decision", "use sqlite", "simple")
    _add(repo, "deadend", "tried redis", "overkill")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "log on A", cwd=repo)

    # Branch B (from the driver-install commit) appends two different entries.
    _git("checkout", "main", cwd=repo)
    _git("checkout", "-b", "feat-b", cwd=repo)
    _add(repo, "decision", "use typer", "ergonomic")
    _add(repo, "note", "profiled startup")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "log on B", cwd=repo)

    # Merge A into B: must not conflict.
    merged = _git("merge", "--no-edit", "feat-a", cwd=repo)
    assert "CONFLICT" not in merged.stdout + merged.stderr

    log_path = repo / ".shiplog" / "log.jsonl"
    entries = Store(log_path).read_all()
    summaries = {e.summary for e in entries}
    assert summaries == {"use sqlite", "tried redis", "use typer", "profiled startup"}

    # No conflict markers leaked into the file.
    raw = log_path.read_text(encoding="utf-8")
    assert "<<<<<<<" not in raw and ">>>>>>>" not in raw

    # Result is canonical (fix --check passes).
    check = runner.invoke(app, ["fix", "--check"])
    assert check.exit_code == 0, check.output


def test_merge_result_is_deterministic_regardless_of_order(repo: Path) -> None:
    _init_shiplog(repo)
    runner.invoke(app, ["install-merge-driver"])
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "install driver", cwd=repo)
    base = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    _git("checkout", "-b", "side-a", cwd=repo)
    _add(repo, "decision", "alpha")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "a", cwd=repo)

    _git("checkout", base, cwd=repo)
    _git("checkout", "-b", "side-b", cwd=repo)
    _add(repo, "decision", "beta")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "b", cwd=repo)

    log_path = repo / ".shiplog" / "log.jsonl"

    # a <- b
    _git("checkout", "side-a", cwd=repo)
    _git("merge", "--no-edit", "side-b", cwd=repo)
    hash_ab = log_path.read_bytes()

    # b <- a (reset side-b first isn't needed; it never got the merge)
    _git("checkout", "side-b", cwd=repo)
    _git("merge", "--no-edit", "side-a", cwd=repo)
    hash_ba = log_path.read_bytes()

    assert hash_ab == hash_ba  # byte-identical union either way


def test_merge_dedupes_shared_history(repo: Path) -> None:
    # Both branches share the same pre-existing entry (committed before the split),
    # then each adds one. The shared line must not double after merge.
    _init_shiplog(repo)
    runner.invoke(app, ["install-merge-driver"])
    _add(repo, "decision", "shared base entry")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "shared + driver", cwd=repo)
    base = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    _git("checkout", "-b", "x", cwd=repo)
    _add(repo, "note", "x only")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "x", cwd=repo)

    _git("checkout", base, cwd=repo)
    _git("checkout", "-b", "y", cwd=repo)
    _add(repo, "note", "y only")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "y", cwd=repo)

    _git("merge", "--no-edit", "x", cwd=repo)
    entries = Store(repo / ".shiplog" / "log.jsonl").read_all()
    summaries = sorted(e.summary for e in entries)
    assert summaries == ["shared base entry", "x only", "y only"]  # base once


# -- fix ----------------------------------------------------------------------


def _write_log(repo: Path, entries: list[Entry]) -> Path:
    path = repo / ".shiplog" / "log.jsonl"
    path.write_text("".join(e.to_json() + "\n" for e in entries), encoding="utf-8")
    return path


def test_fix_check_exits_nonzero_on_dupes(repo: Path) -> None:
    _init_shiplog(repo)
    e = Entry(summary="dup", id="260708-AAA", ts="2026-07-08T09:00:00Z")
    _write_log(repo, [e, e])
    result = runner.invoke(app, ["fix", "--check"])
    assert result.exit_code == 1
    assert "duplicate" in result.output


def test_fix_check_exits_nonzero_when_out_of_order(repo: Path) -> None:
    _init_shiplog(repo)
    newer = Entry(summary="newer", id="260708-ZZZ", ts="2026-07-08T12:00:00Z")
    older = Entry(summary="older", id="260708-AAA", ts="2026-07-08T09:00:00Z")
    _write_log(repo, [newer, older])
    result = runner.invoke(app, ["fix", "--check"])
    assert result.exit_code == 1


def test_fix_check_passes_on_clean_log(repo: Path) -> None:
    _init_shiplog(repo)
    a = Entry(summary="a", id="260708-AAA", ts="2026-07-08T09:00:00Z")
    b = Entry(summary="b", id="260708-BBB", ts="2026-07-08T10:00:00Z")
    _write_log(repo, [a, b])
    result = runner.invoke(app, ["fix", "--check"])
    assert result.exit_code == 0


def test_fix_write_normalizes_and_is_idempotent(repo: Path) -> None:
    _init_shiplog(repo)
    newer = Entry(summary="newer", id="260708-ZZZ", ts="2026-07-08T12:00:00Z")
    older = Entry(summary="older", id="260708-AAA", ts="2026-07-08T09:00:00Z")
    path = _write_log(repo, [newer, older, older])  # out of order + dup

    result = runner.invoke(app, ["fix", "--write"])
    assert result.exit_code == 0
    entries = Store(path).read_all()
    assert [e.id for e in entries] == ["260708-AAA", "260708-ZZZ"]  # sorted, deduped

    # Idempotent: a second --write changes nothing and reports so.
    before = path.read_bytes()
    again = runner.invoke(app, ["fix", "--write"])
    assert again.exit_code == 0
    assert path.read_bytes() == before
    assert "unchanged" in again.output.lower()

    # And it's now clean by --check.
    assert runner.invoke(app, ["fix", "--check"]).exit_code == 0


def test_fix_preserves_link_records(repo: Path) -> None:
    _init_shiplog(repo)
    decision = Entry(
        summary="decide", id="260708-AAA", ts="2026-07-08T09:00:00Z",
        type=EntryType.DECISION,
    )
    link = Entry(
        summary="links commit abc123", id="260708-LNK", ts="2026-07-08T10:00:00Z",
        type=EntryType.LINK, link_target="260708-AAA", link_kind="commit", ref="abc123",
    )
    path = _write_log(repo, [link, decision])  # out of order
    assert runner.invoke(app, ["fix", "--write"]).exit_code == 0
    entries = Store(path).read_all()
    link_entry = next(e for e in entries if e.type == EntryType.LINK)
    assert link_entry.link_target == "260708-AAA"
    assert link_entry.ref == "abc123"


def test_fix_dry_run_writes_nothing(repo: Path) -> None:
    _init_shiplog(repo)
    newer = Entry(summary="newer", id="260708-ZZZ", ts="2026-07-08T12:00:00Z")
    older = Entry(summary="older", id="260708-AAA", ts="2026-07-08T09:00:00Z")
    path = _write_log(repo, [newer, older])
    before = path.read_bytes()
    result = runner.invoke(app, ["fix"])  # no flags = dry run
    assert result.exit_code == 0
    assert path.read_bytes() == before  # untouched
    assert "would normalize" in result.output


def test_fix_check_and_write_are_mutually_exclusive(repo: Path) -> None:
    _init_shiplog(repo)
    result = runner.invoke(app, ["fix", "--check", "--write"])
    assert result.exit_code != 0
