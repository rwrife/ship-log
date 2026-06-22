"""Tests for M5 ``brief``: ranking (dead-ends first), the size budget, focus-file
prioritization, and ``--json`` shape.

Split in two layers:

* **Pure** (`shiplog.brief`) -- deterministic ordering/budget over hand-built
  :class:`~shiplog.models.Entry` lists, no git/CLI needed.
* **End-to-end** (Typer) -- the ``brief`` command against a throwaway git repo,
  mirroring the M4 setup, asserting the rendered markdown + JSON contract.

Required by #5: ordering (deadends first) + size budget, plus the ``--json``
variant and working-tree/`--files` prioritization.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shiplog.brief import DEFAULT_BUDGET, build_brief, rank_entries
from shiplog.cli import app
from shiplog.models import Entry, EntryType

runner = CliRunner()


# -- pure ranking / budget ------------------------------------------------


def _entry(
    summary: str,
    type_: str,
    *,
    ts: str,
    files: list[str] | None = None,
) -> Entry:
    """Build an Entry with an explicit ts (so ordering is deterministic)."""
    return Entry(
        summary=summary,
        type=EntryType.coerce(type_),
        ts=ts,
        files=files or [],
    )


def test_rank_puts_deadends_first_then_decisions_then_notes() -> None:
    entries = [
        _entry("a note", "note", ts="2026-06-01T00:00:00Z"),
        _entry("a decision", "decision", ts="2026-06-01T00:00:00Z"),
        _entry("an attempt", "attempt", ts="2026-06-01T00:00:00Z"),
        _entry("a deadend", "deadend", ts="2026-06-01T00:00:00Z"),
    ]
    ranked = rank_entries(entries, focus=[])
    assert [e.type.value for e in ranked] == [
        "deadend",
        "decision",
        "attempt",
        "note",
    ]


def test_rank_newest_first_within_a_type() -> None:
    entries = [
        _entry("old", "decision", ts="2026-06-01T00:00:00Z"),
        _entry("new", "decision", ts="2026-06-10T00:00:00Z"),
        _entry("mid", "decision", ts="2026-06-05T00:00:00Z"),
    ]
    ranked = rank_entries(entries, focus=[])
    assert [e.summary for e in ranked] == ["new", "mid", "old"]


def test_rank_prioritizes_focus_files_within_a_type() -> None:
    entries = [
        _entry("off-focus newer", "decision", ts="2026-06-10T00:00:00Z", files=["other.py"]),
        _entry("in-focus older", "decision", ts="2026-06-01T00:00:00Z", files=["shiplog/cli.py"]),
    ]
    # Even though the off-focus entry is newer, the in-focus one leads its type.
    ranked = rank_entries(entries, focus=["cli.py"])
    assert [e.summary for e in ranked] == ["in-focus older", "off-focus newer"]


def test_rank_focus_does_not_override_type_order() -> None:
    # An in-focus *note* must still sort below an off-focus *deadend*: type wins.
    entries = [
        _entry("off-focus deadend", "deadend", ts="2026-06-01T00:00:00Z", files=["other.py"]),
        _entry("in-focus note", "note", ts="2026-06-10T00:00:00Z", files=["shiplog/cli.py"]),
    ]
    ranked = rank_entries(entries, focus=["cli.py"])
    assert [e.type.value for e in ranked] == ["deadend", "note"]


def test_build_brief_budget_truncates_lowest_priority_tail() -> None:
    entries = [
        _entry("deadend", "deadend", ts="2026-06-01T00:00:00Z"),
        _entry("decision", "decision", ts="2026-06-01T00:00:00Z"),
        _entry("note", "note", ts="2026-06-01T00:00:00Z"),
    ]
    brief = build_brief(entries, focus=[], budget=2)
    # The note (lowest priority) is dropped; the deadend + decision survive.
    assert [e.type.value for e in brief.entries] == ["deadend", "decision"]
    assert brief.total == 3
    assert brief.truncated == 1
    assert brief.deadend_count == 1


def test_build_brief_budget_zero_means_no_cap() -> None:
    entries = [_entry(f"e{i}", "note", ts="2026-06-01T00:00:00Z") for i in range(20)]
    brief = build_brief(entries, focus=[], budget=0)
    assert len(brief.entries) == 20
    assert brief.truncated == 0


def test_build_brief_empty_log() -> None:
    brief = build_brief([], focus=["cli.py"], budget=DEFAULT_BUDGET)
    assert brief.entries == []
    assert brief.total == 0
    assert brief.deadend_count == 0
    assert brief.truncated == 0


# -- end-to-end CLI -------------------------------------------------------


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
    """init + a spread of types/files so ranking has something to chew on."""
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(
        app,
        ["add", "note", "tidy the readme", "--tags", "docs"],
    ).exit_code == 0
    assert runner.invoke(
        app,
        ["add", "decision", "use jsonl", "--why", "diffable",
         "--files", "shiplog/store.py"],
    ).exit_code == 0
    assert runner.invoke(
        app,
        ["add", "deadend", "threading append lock contention",
         "--files", "shiplog/store.py"],
    ).exit_code == 0


def test_brief_markdown_leads_with_deadends(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["brief"])
    assert result.exit_code == 0, result.output
    out = result.output
    # The dead-ends section header appears before the decisions header, which
    # appears before the recent (notes) header.
    assert "Dead-ends" in out and "Decisions" in out
    assert out.index("Dead-ends") < out.index("Decisions")
    assert out.index("Decisions") < out.index("Recent")
    # And the actual deadend summary precedes the note summary in the body.
    assert out.index("threading append lock contention") < out.index("tidy the readme")


def test_brief_stays_within_size_budget(repo: Path) -> None:
    # Seed many entries; the default budget must keep the digest compact.
    runner.invoke(app, ["init"])
    for i in range(40):
        runner.invoke(app, ["add", "note", f"entry number {i}"])
    out = runner.invoke(app, ["brief"]).output
    body_lines = [ln for ln in out.splitlines() if ln.strip()]
    # Default budget is small; even with headers the digest is well under ~40 lines.
    assert len(body_lines) <= DEFAULT_BUDGET + 6
    # Bullet lines never exceed the default budget count.
    bullets = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert len(bullets) == DEFAULT_BUDGET


def test_brief_limit_option_caps_entries(repo: Path) -> None:
    runner.invoke(app, ["init"])
    for i in range(10):
        runner.invoke(app, ["add", "note", f"n{i}"])
    out = runner.invoke(app, ["brief", "--limit", "3"]).output
    bullets = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert len(bullets) == 3
    assert "more in `shiplog ls`" in out  # truncation footer


def test_brief_json_shape(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["brief", "--json"])
    assert result.exit_code == 0, result.output
    obj = json.loads(result.output)
    assert isinstance(obj, dict)
    for key in ("entries", "focus", "total", "shown", "truncated", "deadends"):
        assert key in obj
    assert isinstance(obj["entries"], list)
    # Ranked: first entry is the deadend.
    assert obj["entries"][0]["type"] == "deadend"
    assert obj["deadends"] == 1
    assert obj["shown"] == len(obj["entries"])


def test_brief_files_option_prioritizes_focus(repo: Path) -> None:
    runner.invoke(app, ["init"])
    # Two decisions touching different files; --files should float one up.
    runner.invoke(app, ["add", "decision", "store choice", "--files", "shiplog/store.py"])
    runner.invoke(app, ["add", "decision", "cli choice", "--files", "shiplog/cli.py"])
    obj = json.loads(runner.invoke(app, ["brief", "--files", "cli.py", "--json"]).output)
    assert obj["focus"] == ["cli.py"]
    # The cli-touching decision ranks ahead of the store one.
    summaries = [e["summary"] for e in obj["entries"]]
    assert summaries.index("cli choice") < summaries.index("store choice")


def test_brief_before_init_fails(repo: Path) -> None:
    result = runner.invoke(app, ["brief"])
    assert result.exit_code == 1
    assert "shiplog init" in result.output


def test_brief_empty_log_is_friendly(repo: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["brief"])
    assert result.exit_code == 0
    assert "log is empty" in result.output
