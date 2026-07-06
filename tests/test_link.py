"""Tests for ``shiplog link`` (#27): attach a commit / PR / ref after the fact.

End-to-end through Typer against a real throwaway git repo, mirroring the
``test_ls_show`` setup. Focus areas from the issue's acceptance criteria:

* the append-doesn't-mutate invariant (original JSONL line byte-identical),
* id resolution (exact + prefix + ambiguous), matching ``show``'s behavior,
* links surfacing in ``show`` (panel + ``--json`` ``links`` array), newest-first,
* required-/single-option validation,
* link records staying out of the default ``ls`` table / ``brief`` digest.
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


def _seed_one(repo: Path) -> str:
    """init + one decision entry; return its id."""
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert (
        runner.invoke(
            app,
            ["add", "decision", "use jsonl", "--why", "diffable",
             "--files", "shiplog/store.py"],
        ).exit_code
        == 0
    )
    return json.loads(runner.invoke(app, ["ls", "--json"]).output)[0]["id"]


def _log_path(repo: Path) -> Path:
    return repo / ".shiplog" / "log.jsonl"


# -- append-only invariant ------------------------------------------------


def test_link_does_not_mutate_original_entry_line(repo: Path) -> None:
    entry_id = _seed_one(repo)
    before = _log_path(repo).read_text(encoding="utf-8")
    assert before.count("\n") == 1  # exactly the one seeded entry

    result = runner.invoke(app, ["link", entry_id, "--commit", "abc1234"])
    assert result.exit_code == 0, result.output

    after_lines = _log_path(repo).read_text(encoding="utf-8").splitlines(keepends=True)
    # The original line is byte-identical and still first; a new line was appended.
    assert after_lines[0] == before
    assert len(after_lines) == 2


def test_link_record_roundtrips_through_store(repo: Path) -> None:
    entry_id = _seed_one(repo)
    runner.invoke(app, ["link", entry_id, "--pr", "#42", "--note", "landed"])

    from shiplog.store import Store

    entries = Store.for_repo(repo).read_all()
    link = next(e for e in entries if e.type.value == "link")
    assert link.link_target == entry_id
    assert link.link_kind == "pr"
    assert link.ref == "#42"
    assert link.why == "landed"
    assert link.schema_version == 1  # no bump needed


# -- required / single option validation ----------------------------------


def test_link_requires_a_kind(repo: Path) -> None:
    entry_id = _seed_one(repo)
    result = runner.invoke(app, ["link", entry_id])
    assert result.exit_code == 1
    assert "--commit" in result.output and "--ref" in result.output


def test_link_rejects_multiple_kinds(repo: Path) -> None:
    entry_id = _seed_one(repo)
    result = runner.invoke(app, ["link", entry_id, "--commit", "x", "--pr", "y"])
    assert result.exit_code == 1
    assert "exactly one" in result.output


# -- id resolution (mirrors show) -----------------------------------------


def test_link_resolves_unique_prefix(repo: Path) -> None:
    entry_id = _seed_one(repo)
    prefix = entry_id[:10]  # date + start of the random suffix → unique
    result = runner.invoke(app, ["link", prefix, "--commit", "abc1234"])
    assert result.exit_code == 0, result.output
    # The full id shows up in the confirmation.
    assert entry_id in result.output


def test_link_unknown_id_fails(repo: Path) -> None:
    _seed_one(repo)
    result = runner.invoke(app, ["link", "ZZZZZZ", "--commit", "x"])
    assert result.exit_code == 1
    assert "no entry with id" in result.output


def test_link_ambiguous_prefix_fails(repo: Path) -> None:
    entry_id = _seed_one(repo)
    # A second entry guarantees the shared YYMMDD date prefix is ambiguous.
    runner.invoke(app, ["add", "note", "second"])
    date_prefix = entry_id.split("-", 1)[0]
    result = runner.invoke(app, ["link", date_prefix, "--commit", "x"])
    assert result.exit_code == 1
    assert "ambiguous" in result.output


# -- links surface in show (panel + json) ---------------------------------


def test_show_json_includes_links_newest_first(repo: Path) -> None:
    entry_id = _seed_one(repo)
    # Three links; the JSON array should list them newest-first.
    runner.invoke(app, ["link", entry_id, "--commit", "abc1234"])
    runner.invoke(app, ["link", entry_id, "--pr", "#42", "--note", "landed here"])
    runner.invoke(app, ["link", entry_id, "--ref", "https://doc/x"])

    obj = json.loads(runner.invoke(app, ["show", entry_id, "--json"]).output)
    assert "links" in obj
    assert len(obj["links"]) == 3
    kinds = [link["kind"] for link in obj["links"]]
    values = {link["value"] for link in obj["links"]}
    assert set(kinds) == {"commit", "pr", "ref"}
    assert values == {"abc1234", "#42", "https://doc/x"}
    # Newest-first: timestamps are non-increasing down the array.
    stamps = [link["ts"] for link in obj["links"]]
    assert stamps == sorted(stamps, reverse=True)
    # The note round-trips on the pr link.
    pr = next(link for link in obj["links"] if link["kind"] == "pr")
    assert pr["note"] == "landed here"


def test_show_panel_renders_links_section(repo: Path) -> None:
    entry_id = _seed_one(repo)
    runner.invoke(app, ["link", entry_id, "--commit", "abc1234"])
    result = runner.invoke(app, ["show", entry_id])
    assert result.exit_code == 0
    assert "Links" in result.output
    assert "abc1234" in result.output


def test_show_without_links_has_no_links_key_content(repo: Path) -> None:
    entry_id = _seed_one(repo)
    obj = json.loads(runner.invoke(app, ["show", entry_id, "--json"]).output)
    # Key is always present (stable shape) but empty when nothing links here.
    assert obj["links"] == []


# -- link records don't clutter ls / brief --------------------------------


def test_ls_hides_link_records_by_default(repo: Path) -> None:
    entry_id = _seed_one(repo)
    runner.invoke(app, ["link", entry_id, "--commit", "abc1234"])
    data = json.loads(runner.invoke(app, ["ls", "--json"]).output)
    # Only the original decision shows; the link record is not a standalone row.
    assert [e["type"] for e in data] == ["decision"]


def test_ls_type_link_reveals_records(repo: Path) -> None:
    entry_id = _seed_one(repo)
    runner.invoke(app, ["link", entry_id, "--commit", "abc1234"])
    runner.invoke(app, ["link", entry_id, "--ref", "note"])
    data = json.loads(runner.invoke(app, ["ls", "--type", "link", "--json"]).output)
    assert len(data) == 2
    assert all(e["type"] == "link" for e in data)


def test_brief_excludes_link_records(repo: Path) -> None:
    entry_id = _seed_one(repo)
    runner.invoke(app, ["link", entry_id, "--commit", "abc1234"])
    digest = json.loads(runner.invoke(app, ["brief", "--json"]).output)
    assert all(e["type"] != "link" for e in digest["entries"])
