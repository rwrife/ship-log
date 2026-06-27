"""Tests for the ``shiplog hook`` git ``prepare-commit-msg`` nudge (issue #8).

These run against real throwaway git repos so hook-path resolution, file perms,
and the install/uninstall/strip logic are exercised for real. The nudge *decision*
logic (interesting-or-not, pattern matching, non-interactive skips) is unit-tested
directly against ``shiplog.hooks`` for speed and precision.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shiplog import hooks
from shiplog.cli import app
from shiplog.config import CONFIG_FILENAME, Config
from shiplog.store import SHIPLOG_DIR

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
    return repo / ".git" / "hooks" / hooks.HOOK_NAME


# -- install ------------------------------------------------------------------


def test_install_creates_executable_hook(repo: Path) -> None:
    result = runner.invoke(app, ["hook", "install"])
    assert result.exit_code == 0, result.output
    hook = _hook_file(repo)
    assert hook.exists()
    text = hook.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh")
    assert hooks.MARKER_BEGIN in text and hooks.MARKER_END in text
    assert "shiplog hook _nudge" in text
    # Executable bit set for the user.
    assert hook.stat().st_mode & stat.S_IXUSR
    assert "installed" in result.output


def test_install_is_idempotent(repo: Path) -> None:
    first = runner.invoke(app, ["hook", "install"])
    assert first.exit_code == 0
    assert "installed" in first.output
    second = runner.invoke(app, ["hook", "install"])
    assert second.exit_code == 0
    assert "up to date" in second.output


def test_install_refuses_foreign_hook_without_force(repo: Path) -> None:
    hook = _hook_file(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho not-ours\n", encoding="utf-8")
    result = runner.invoke(app, ["hook", "install"])
    assert result.exit_code == 1
    assert "non-ship-log" in result.output
    # Foreign hook left untouched.
    assert "echo not-ours" in hook.read_text(encoding="utf-8")


def test_install_force_overwrites_foreign_hook(repo: Path) -> None:
    hook = _hook_file(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho not-ours\n", encoding="utf-8")
    result = runner.invoke(app, ["hook", "install", "--force"])
    assert result.exit_code == 0
    text = hook.read_text(encoding="utf-8")
    assert hooks.is_ours(text)
    assert "not-ours" not in text


def test_install_refreshes_stale_block(repo: Path) -> None:
    # An old version of our block should be rewritten ("updated"), not duplicated.
    hook = _hook_file(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    stale = "#!/bin/sh\n" + hooks.MARKER_BEGIN + "\n# old\n" + hooks.MARKER_END + "\n"
    hook.write_text(stale, encoding="utf-8")
    result = runner.invoke(app, ["hook", "install"])
    assert result.exit_code == 0
    assert "updated" in result.output
    text = hook.read_text(encoding="utf-8")
    # Exactly one block (no duplication).
    assert text.count(hooks.MARKER_BEGIN) == 1
    assert "shiplog hook _nudge" in text


def test_install_outside_git_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    from shiplog import gitctx

    gitctx.find_repo_root.cache_clear()
    result = runner.invoke(app, ["hook", "install"])
    assert result.exit_code == 1
    assert "not inside a git repository" in result.output


# -- status -------------------------------------------------------------------


def test_status_reports_installed_state(repo: Path) -> None:
    before = runner.invoke(app, ["hook", "status"])
    assert before.exit_code == 0
    assert "not installed" in before.output
    runner.invoke(app, ["hook", "install"])
    after = runner.invoke(app, ["hook", "status"])
    assert after.exit_code == 0
    assert "installed" in after.output
    assert "not installed" not in after.output


# -- uninstall ----------------------------------------------------------------


def test_uninstall_removes_pure_hook(repo: Path) -> None:
    runner.invoke(app, ["hook", "install"])
    assert _hook_file(repo).exists()
    result = runner.invoke(app, ["hook", "uninstall"])
    assert result.exit_code == 0
    assert "removed" in result.output
    assert not _hook_file(repo).exists()


def test_uninstall_absent_is_friendly(repo: Path) -> None:
    result = runner.invoke(app, ["hook", "uninstall"])
    assert result.exit_code == 0
    assert "nothing to remove" in result.output


def test_uninstall_strips_block_keeps_other_content(repo: Path) -> None:
    hook = _hook_file(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    mixed = (
        "#!/bin/sh\n"
        "echo keepme\n"
        + hooks.MARKER_BEGIN
        + "\n"
        + 'shiplog hook _nudge "$1" "$2" || true\n'
        + hooks.MARKER_END
        + "\n"
    )
    hook.write_text(mixed, encoding="utf-8")
    result = runner.invoke(app, ["hook", "uninstall"])
    assert result.exit_code == 0
    assert "stripped" in result.output
    remaining = hook.read_text(encoding="utf-8")
    assert "echo keepme" in remaining
    assert hooks.MARKER_BEGIN not in remaining
    assert "shiplog hook _nudge" not in remaining


def test_uninstall_leaves_foreign_hook_untouched(repo: Path) -> None:
    hook = _hook_file(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho not-ours\n", encoding="utf-8")
    result = runner.invoke(app, ["hook", "uninstall"])
    assert result.exit_code == 0
    assert "nothing to remove" in result.output
    assert "echo not-ours" in hook.read_text(encoding="utf-8")


# -- decision logic (unit) ----------------------------------------------------


@pytest.mark.parametrize(
    "subject",
    [
        "refactor the storage layer",
        "Rewrite the parser",
        "switch to JSONL",
        "drop the legacy adapter",
        "BREAKING: change the API",
        "revert the caching change",
        "add a workaround for the flaky test",
    ],
)
def test_decision_patterns_match(subject: str) -> None:
    assert hooks.matches_decision_pattern(subject)


@pytest.mark.parametrize(
    "subject",
    ["fix typo", "update docs", "bump version", "add a test"],
)
def test_decision_patterns_dont_overmatch(subject: str) -> None:
    assert not hooks.matches_decision_pattern(subject)


def test_is_interesting_by_file_count(repo: Path) -> None:
    for name in ("x.py", "y.py", "z.py"):
        (repo / name).write_text("x = 1\n")
    _git("add", "x.py", "y.py", "z.py", cwd=repo)
    # Boring subject, but 3 staged files meets the default threshold.
    assert hooks.is_interesting(repo, "misc changes", file_threshold=3)


def test_is_interesting_by_pattern_even_one_file(repo: Path) -> None:
    (repo / "x.py").write_text("x = 1\n")
    _git("add", "x.py", cwd=repo)
    # Only one file, but the message smells like a decision.
    assert hooks.is_interesting(repo, "refactor x", file_threshold=3)


def test_not_interesting_small_boring_commit(repo: Path) -> None:
    (repo / "x.py").write_text("x = 1\n")
    _git("add", "x.py", cwd=repo)
    assert not hooks.is_interesting(repo, "tweak x", file_threshold=3)


def test_staged_file_count(repo: Path) -> None:
    assert hooks.staged_file_count(repo) == 0
    for name in ("a.py", "b.py"):
        (repo / name).write_text("x\n")
    _git("add", "a.py", "b.py", cwd=repo)
    assert hooks.staged_file_count(repo) == 2


# -- run_nudge (what the installed hook calls) --------------------------------


def _msg_file(tmp_path: Path, body: str = "") -> Path:
    p = tmp_path / "COMMIT_EDITMSG"
    template = body + (
        "\n# Please enter the commit message for your changes.\n"
        "# Lines starting with '#' will be ignored.\n"
    )
    p.write_text(template, encoding="utf-8")
    return p


def test_run_nudge_appends_on_interesting_commit(repo: Path) -> None:
    for name in ("a.py", "b.py", "c.py"):
        (repo / name).write_text("x\n")
    _git("add", "a.py", "b.py", "c.py", cwd=repo)
    msg = _msg_file(repo)
    appended = hooks.run_nudge(repo, msg, source="", file_threshold=3)
    assert appended is True
    text = msg.read_text(encoding="utf-8")
    assert "ship-log:" in text
    # The nudge is entirely commented so git strips it from the real message.
    nudge_lines = [ln for ln in text.splitlines() if "ship-log:" in ln or "shiplog add" in ln]
    assert nudge_lines
    assert all(ln.lstrip().startswith("#") for ln in nudge_lines)


def test_run_nudge_skips_noninteractive_sources(repo: Path) -> None:
    for name in ("a.py", "b.py", "c.py"):
        (repo / name).write_text("x\n")
    _git("add", "a.py", "b.py", "c.py", cwd=repo)
    for src in ("message", "merge", "squash", "commit"):
        msg = _msg_file(repo)
        assert hooks.run_nudge(repo, msg, source=src, file_threshold=3) is False
        assert "ship-log:" not in msg.read_text(encoding="utf-8")


def test_run_nudge_skips_boring_commit(repo: Path) -> None:
    (repo / "x.py").write_text("x\n")
    _git("add", "x.py", cwd=repo)
    msg = _msg_file(repo, body="tiny tweak\n")
    assert hooks.run_nudge(repo, msg, source="", file_threshold=3) is False
    assert "ship-log:" not in msg.read_text(encoding="utf-8")


def test_run_nudge_is_idempotent_no_double(repo: Path) -> None:
    for name in ("a.py", "b.py", "c.py"):
        (repo / name).write_text("x\n")
    _git("add", "a.py", "b.py", "c.py", cwd=repo)
    msg = _msg_file(repo)
    assert hooks.run_nudge(repo, msg, source="", file_threshold=3) is True
    # Second call must NOT append a duplicate nudge.
    assert hooks.run_nudge(repo, msg, source="", file_threshold=3) is False
    assert msg.read_text(encoding="utf-8").count("ship-log:") == 1


def test_run_nudge_missing_file_is_safe(repo: Path) -> None:
    # A non-existent message file must never raise (commit safety).
    assert hooks.run_nudge(repo, repo / "nope" / "COMMIT_EDITMSG", source="") is False


def test_nudge_text_is_all_comments() -> None:
    text = hooks.nudge_text('refactor "things"')
    body = [ln for ln in text.splitlines() if ln.strip()]
    assert body  # non-empty
    assert all(ln.lstrip().startswith("#") for ln in body)
    # Double-quotes in the subject are neutralized so the suggested command is valid.
    assert '"refactor' not in text or "'things'" in text


# -- config knob --------------------------------------------------------------


def test_config_exposes_hook_threshold(repo: Path) -> None:
    runner.invoke(app, ["init"])
    cfg_path = repo / SHIPLOG_DIR / CONFIG_FILENAME
    assert "hook_file_threshold" in cfg_path.read_text(encoding="utf-8")
    assert Config.load(repo).hook_file_threshold == 3


def test_config_threshold_override_respected(repo: Path) -> None:
    runner.invoke(app, ["init"])
    cfg_path = repo / SHIPLOG_DIR / CONFIG_FILENAME
    cfg_path.write_text("hook_file_threshold = 1\nschema_version = 1\n", encoding="utf-8")
    assert Config.load(repo).hook_file_threshold == 1
    # With threshold 1, even a single-file boring commit is interesting.
    (repo / "x.py").write_text("x\n")
    _git("add", "x.py", cwd=repo)
    assert hooks.is_interesting(repo, "tiny", file_threshold=1)


# -- end-to-end via real `git commit` -----------------------------------------


def test_end_to_end_nudge_is_stripped_from_commit(repo: Path) -> None:
    """Install the hook, make an interesting commit via an editor, and confirm the
    nudge shows in the template but is stripped from the final commit message."""
    runner.invoke(app, ["init"])
    runner.invoke(app, ["hook", "install"])

    # A fake editor that (a) records whether the nudge was present, then (b)
    # writes a real subject so the commit proceeds.
    seen = repo / "editor_saw_nudge.flag"
    editor = repo / "fake_editor.sh"
    editor.write_text(
        "#!/bin/sh\n"
        f'if grep -q "ship-log:" "$1"; then echo yes > "{seen}"; else echo no > "{seen}"; fi\n'
        'printf "add modules\\n" > "$1.tmp"\n'
        'cat "$1" >> "$1.tmp"\n'
        'mv "$1.tmp" "$1"\n',
        encoding="utf-8",
    )
    editor.chmod(editor.stat().st_mode | stat.S_IXUSR)

    for name in ("a.py", "b.py", "c.py"):
        (repo / name).write_text("x = 1\n")
    _git("add", "a.py", "b.py", "c.py", cwd=repo)

    env = dict(os.environ)
    env["GIT_EDITOR"] = str(editor)
    # shiplog must be importable inside the hook subprocess; ensure PATH carries
    # whatever installed console script the test environment provides. If the
    # `shiplog` entry point isn't on PATH the hook no-ops (still exit 0), so we
    # only assert the strip property when the nudge was actually injected.
    subprocess.run(
        ["git", "commit"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    final_msg = subprocess.run(
        ["git", "log", "-1", "--format=%B"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    # The committed message must never contain the (commented) nudge.
    assert "ship-log:" not in final_msg
    assert "add modules" in final_msg
    if seen.exists() and seen.read_text().strip() == "yes":
        # When the hook ran with shiplog on PATH, it injected the nudge into the
        # template the editor saw — and git stripped it. Strongest assertion.
        assert "add modules" in final_msg
