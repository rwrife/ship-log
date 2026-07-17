"""Tests for ``shiplog resolve`` — closing out a dead-end so it stops nagging (#46).

Unit-tests the resolution helpers + brief/guard suppression directly for speed and
precision, and drives the CLI end-to-end (resolve appends a linked record, brief /
guard / ls / ask honour resolution state, invalid targets error) against a real
throwaway git repo.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shiplog import guard
from shiplog.brief import build_brief
from shiplog.cli import app
from shiplog.models import Entry, EntryType
from shiplog.resolutions import (
    is_resolution,
    make_resolution_summary,
    resolution_for,
    resolved_ids,
)

runner = CliRunner()


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


def _deadend(
    summary: str, files: list[str] | None = None, *, id_: str = "d1",
    ts: str = "2026-01-01T00:00:00Z",
) -> Entry:
    return Entry(
        summary=summary,
        type=EntryType.DEADEND,
        id=id_,
        ts=ts,
        files=files or [],
    )


def _resolution(target: str, *, id_: str = "r1", how: str = "fixed it",
                ts: str = "2026-01-01T00:00:00Z") -> Entry:
    return Entry(
        summary=f"resolved dead-end {target}",
        type=EntryType.RESOLVE,
        id=id_,
        link_target=target,
        why=how,
        ts=ts,
    )


# -- unit: helpers ------------------------------------------------------------


def test_resolved_ids_collects_targets() -> None:
    entries = [_deadend("a", id_="d1"), _resolution("d1")]
    assert resolved_ids(entries) == {"d1"}


def test_resolved_ids_ignores_empty_and_non_resolve() -> None:
    entries = [
        _deadend("a", id_="d1"),
        Entry(summary="ack", type=EntryType.ACK, link_target="d1"),
        Entry(summary="stray resolve", type=EntryType.RESOLVE, link_target=""),
    ]
    assert resolved_ids(entries) == set()


def test_is_resolution() -> None:
    assert is_resolution(_resolution("d1"))
    assert not is_resolution(_deadend("a"))


def test_make_resolution_summary() -> None:
    d = _deadend("cache breaks writes", id_="d9")
    assert make_resolution_summary(d) == "resolved dead-end d9: cache breaks writes"


def test_resolution_for_returns_newest() -> None:
    entries = [
        _deadend("a", id_="d1"),
        _resolution("d1", id_="r1", how="first"),
        Entry(
            summary="resolved dead-end d1",
            type=EntryType.RESOLVE,
            id="r2",
            link_target="d1",
            why="second",
            ts="2026-06-01T00:00:00Z",
        ),
    ]
    rv = resolution_for("d1", entries)
    assert rv is not None
    assert rv.how == "second"


def test_resolution_for_none_when_unresolved() -> None:
    assert resolution_for("d1", [_deadend("a", id_="d1")]) is None


# -- unit: brief / guard suppression ------------------------------------------


def test_brief_drops_resolved_deadend() -> None:
    entries = [_deadend("cache", id_="d1"), _resolution("d1")]
    b = build_brief(entries)
    assert [e.id for e in b.entries] == []
    assert b.deadend_count == 0


def test_brief_include_resolved_resurfaces() -> None:
    entries = [_deadend("cache", id_="d1"), _resolution("d1")]
    b = build_brief(entries, include_resolved=True)
    assert [e.id for e in b.entries] == ["d1"]
    assert b.deadend_count == 1


def test_brief_never_lists_resolution_record() -> None:
    entries = [_deadend("cache", id_="d1"), _resolution("d1")]
    b = build_brief(entries, include_resolved=True)
    assert all(e.type != EntryType.RESOLVE for e in b.entries)


def test_guard_ignores_resolved_deadend() -> None:
    entries = [_deadend("cache", ["cache.py"], id_="d1"), _resolution("d1")]
    blocks = guard.blocking_deadends(entries, {"cache.py"})
    assert blocks == []


def test_guard_include_resolved_still_blocks() -> None:
    entries = [_deadend("cache", ["cache.py"], id_="d1"), _resolution("d1")]
    blocks = guard.blocking_deadends(entries, {"cache.py"}, include_resolved=True)
    assert [b.entry.id for b in blocks] == ["d1"]


# -- CLI: end-to-end ----------------------------------------------------------


def _add_deadend(repo: Path, summary: str, files: str = "cache.py") -> str:
    runner.invoke(app, ["init"])
    runner.invoke(app, ["add", "deadend", summary, "--why", "no invalidation", "--files", files])
    res = runner.invoke(app, ["ls", "--type", "deadend", "--json"])
    return json.loads(res.stdout)[0]["id"]


def test_resolve_appends_linked_record(repo: Path) -> None:
    did = _add_deadend(repo, "global cache breaks writes")
    res = runner.invoke(app, ["resolve", did, "--why", "added invalidation"])
    assert res.exit_code == 0
    # A resolve record now points back at the dead-end.
    show = runner.invoke(app, ["show", did, "--json"])
    payload = json.loads(show.stdout)
    assert payload["resolution"] is not None
    assert payload["resolution"]["how"] == "added invalidation"
    # Original dead-end line is never mutated.
    assert payload["type"] == "deadend"
    assert payload["summary"] == "global cache breaks writes"


def test_resolve_suppresses_in_brief_and_guard(repo: Path) -> None:
    did = _add_deadend(repo, "cache breaks writes")
    runner.invoke(app, ["resolve", did, "--why", "fixed"])

    brief = runner.invoke(app, ["brief", "--json"])
    assert json.loads(brief.stdout)["deadends"] == 0

    brief_inc = runner.invoke(app, ["brief", "--json", "--include-resolved"])
    assert json.loads(brief_inc.stdout)["deadends"] == 1

    (repo / "cache.py").write_text("x\n")
    _git("add", "cache.py", cwd=repo)
    g = runner.invoke(app, ["guard", "--json"])
    assert json.loads(g.stdout)["count"] == 0


def test_ls_unresolved_filters_resolved(repo: Path) -> None:
    did = _add_deadend(repo, "cache breaks writes")
    runner.invoke(app, ["resolve", did, "--why", "fixed"])
    res = runner.invoke(app, ["ls", "--unresolved", "--type", "deadend", "--json"])
    assert json.loads(res.stdout) == []


def test_ask_unresolved_excludes_resolved(repo: Path) -> None:
    did = _add_deadend(repo, "redis cache breaks writes")
    runner.invoke(app, ["resolve", did, "--why", "fixed"])
    res = runner.invoke(app, ["ask", "cache", "--unresolved", "--json"])
    hits = json.loads(res.stdout)["hits"]
    assert all(h["id"] != did for h in hits)


def test_resolve_rejects_non_deadend(repo: Path) -> None:
    runner.invoke(app, ["init"])
    runner.invoke(app, ["add", "note", "just a note"])
    nid = json.loads(runner.invoke(app, ["ls", "--type", "note", "--json"]).stdout)[0]["id"]
    res = runner.invoke(app, ["resolve", nid, "--why", "x"])
    assert res.exit_code == 1
    assert "not a dead-end" in res.stderr


def test_resolve_rejects_missing_id(repo: Path) -> None:
    runner.invoke(app, ["init"])
    res = runner.invoke(app, ["resolve", "ZZZZZZ", "--why", "x"])
    assert res.exit_code == 1
    assert "no entry with id" in res.stderr


def test_resolve_rejects_double_resolve(repo: Path) -> None:
    did = _add_deadend(repo, "cache breaks writes")
    runner.invoke(app, ["resolve", did, "--why", "fixed"])
    res = runner.invoke(app, ["resolve", did, "--why", "again"])
    assert res.exit_code == 1
    assert "already resolved" in res.stderr


def test_resolve_survives_verify(repo: Path) -> None:
    did = _add_deadend(repo, "cache breaks writes")
    runner.invoke(app, ["resolve", did, "--why", "fixed"])
    res = runner.invoke(app, ["verify"])
    assert res.exit_code == 0
