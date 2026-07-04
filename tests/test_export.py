"""Tests for ``shiplog export``: ADR set + CHANGELOG digest.

Two layers, mirroring the ``brief`` test split:

* **Pure** (:mod:`shiplog.export`) — deterministic slug/filename generation,
  changelog grouping, ADR numbering, and the byte-identical idempotency property,
  all over hand-built :class:`~shiplog.models.Entry` lists (no git/CLI/disk).
* **End-to-end** (Typer) — the ``export`` command against a throwaway git repo,
  asserting file layout on disk, stdout output, filter passthrough, idempotent
  re-runs, and friendly empty handling.

Required by #26: ADR filename/slug generation, changelog grouping, idempotency
(same input → same bytes), and filter passthrough.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shiplog.cli import app
from shiplog.export import (
    adr_filename,
    build_adr_set,
    render_adr,
    render_changelog,
    slugify,
)
from shiplog.models import Entry, EntryType

runner = CliRunner()


def _entry(
    summary: str,
    type_: str,
    *,
    ts: str = "2026-06-01T00:00:00Z",
    why: str = "",
    files: list[str] | None = None,
    tags: list[str] | None = None,
    ref: str = "",
    id: str | None = None,
    author: str = "Test Captain",
) -> Entry:
    """Build an Entry with explicit fields so export output is deterministic."""
    kwargs = dict(
        summary=summary,
        type=EntryType.coerce(type_),
        ts=ts,
        why=why,
        files=files or [],
        tags=tags or [],
        ref=ref,
        author=author,
    )
    if id is not None:
        kwargs["id"] = id
    return Entry(**kwargs)


# -- slugify --------------------------------------------------------------


def test_slugify_basic_lowercases_and_hyphenates() -> None:
    assert slugify("Use JSONL for the log") == "use-jsonl-for-the-log"


def test_slugify_collapses_punctuation_and_strips_edges() -> None:
    assert slugify("  Why not SQLite?!  ") == "why-not-sqlite"
    assert slugify("a---b__c") == "a-b-c"


def test_slugify_empty_or_symbols_only_is_untitled() -> None:
    assert slugify("") == "untitled"
    assert slugify("!!!") == "untitled"
    assert slugify("   ") == "untitled"


def test_slugify_truncates_on_word_boundary() -> None:
    long = "the quick brown fox jumps over the lazy dog and then keeps on running forever"
    slug = slugify(long)
    assert len(slug) <= 60
    # Truncation happens on a hyphen boundary — never a dangling partial word tail.
    assert not slug.endswith("-")
    assert slug.startswith("the-quick-brown-fox")


# -- ADR filename + numbering ---------------------------------------------


def test_adr_filename_zero_pads_number() -> None:
    assert adr_filename(1, "Use JSONL") == "0001-use-jsonl.md"
    assert adr_filename(42, "Pick Typer") == "0042-pick-typer.md"


def test_build_adr_set_only_decisions_numbered_in_order() -> None:
    entries = [
        _entry("first decision", "decision", ts="2026-06-01T00:00:00Z"),
        _entry("a dead end", "deadend", ts="2026-06-02T00:00:00Z"),
        _entry("a note", "note", ts="2026-06-03T00:00:00Z"),
        _entry("second decision", "decision", ts="2026-06-04T00:00:00Z"),
    ]
    files = build_adr_set(entries)
    names = list(files.keys())
    # Only the two decisions become ADRs, numbered in append order.
    assert names == ["0001-first-decision.md", "0002-second-decision.md"]


def test_build_adr_set_numbering_is_append_order_not_recency() -> None:
    # Even if a later-numbered decision has an *earlier* ts, numbering follows
    # append (input) order — the property that keeps numbers stable over time.
    entries = [
        _entry("older ts but logged first", "decision", ts="2026-06-10T00:00:00Z"),
        _entry("newer ts logged second", "decision", ts="2026-06-01T00:00:00Z"),
    ]
    names = list(build_adr_set(entries).keys())
    assert names[0].startswith("0001-older-ts")
    assert names[1].startswith("0002-newer-ts")


def test_build_adr_set_duplicate_summaries_stay_unique_via_number() -> None:
    entries = [
        _entry("same title", "decision"),
        _entry("same title", "decision"),
    ]
    names = list(build_adr_set(entries).keys())
    assert names == ["0001-same-title.md", "0002-same-title.md"]
    assert len(set(names)) == 2


def test_build_adr_set_empty_when_no_decisions() -> None:
    entries = [_entry("note", "note"), _entry("deadend", "deadend")]
    assert build_adr_set(entries) == {}


# -- ADR rendering content ------------------------------------------------


def test_render_adr_contains_all_fields() -> None:
    e = _entry(
        "use jsonl for the log",
        "decision",
        ts="2026-06-19T12:00:00Z",
        why="diffable and greppable",
        files=["shiplog/store.py"],
        tags=["storage", "core"],
        ref="#12",
        id="260619-ABC123",
        author="Cap <cap@ship.log>",
    )
    md = render_adr(e, 1)
    # Front-matter + traceability to the source entry id.
    assert md.startswith("---\n")
    assert "source_entry: \"260619-ABC123\"" in md
    assert "id: 0001" in md
    assert "# 0001. use jsonl for the log" in md
    assert "diffable and greppable" in md  # rationale
    assert "`shiplog/store.py`" in md  # affected file
    assert "#12" in md  # reference
    assert md.endswith("\n") and not md.endswith("\n\n")  # single trailing newline


def test_render_adr_missing_why_has_placeholder() -> None:
    e = _entry("no rationale here", "decision")
    md = render_adr(e, 3)
    assert "_No rationale recorded._" in md
    # No "Affected files" section when there are no files.
    assert "Affected files" not in md


def test_render_adr_quotes_special_chars_in_frontmatter() -> None:
    # A colon/quote in the summary must not break the YAML front-matter.
    e = _entry('use "X": because reasons', "decision")
    md = render_adr(e, 1)
    fm = md.split("---", 2)[1]
    assert 'title: "use \\"X\\": because reasons"' in fm


# -- changelog rendering --------------------------------------------------


def test_render_changelog_groups_by_date_newest_first() -> None:
    entries = [
        _entry("older decision", "decision", ts="2026-06-01T09:00:00Z"),
        _entry("newer decision", "decision", ts="2026-06-05T09:00:00Z"),
    ]
    md = render_changelog(entries)
    assert "## 2026-06-05" in md and "## 2026-06-01" in md
    # Newest date section precedes the older one.
    assert md.index("## 2026-06-05") < md.index("## 2026-06-01")


def test_render_changelog_decisions_before_deadends_within_a_date() -> None:
    entries = [
        _entry("a dead end", "deadend", ts="2026-06-05T09:00:00Z"),
        _entry("a decision", "decision", ts="2026-06-05T10:00:00Z"),
    ]
    md = render_changelog(entries)
    assert "### Decisions" in md and "### Dead-ends" in md
    assert md.index("### Decisions") < md.index("### Dead-ends")


def test_render_changelog_excludes_attempts_and_notes() -> None:
    entries = [
        _entry("a decision", "decision"),
        _entry("an attempt", "attempt"),
        _entry("a note", "note"),
    ]
    md = render_changelog(entries)
    assert "a decision" in md
    assert "an attempt" not in md
    assert "a note" not in md


def test_render_changelog_bullet_has_why_ref_and_id() -> None:
    e = _entry(
        "use jsonl",
        "decision",
        why="diffable",
        ref="#7",
        id="260619-XYZ999",
    )
    md = render_changelog([e])
    assert "- use jsonl \u2014 diffable (#7) [`260619-XYZ999`]" in md


def test_render_changelog_empty_is_friendly_not_error() -> None:
    md = render_changelog([_entry("note only", "note")])
    assert "No decisions or dead-ends" in md
    assert md.endswith("\n")


# -- idempotency (the headline property) ----------------------------------


def test_adr_set_is_byte_identical_across_runs() -> None:
    entries = [
        _entry("first", "decision", why="because", files=["a.py"]),
        _entry("second", "decision", why="reasons", tags=["x"]),
    ]
    first = build_adr_set(entries)
    second = build_adr_set(entries)
    assert first == second  # same names AND same bytes per file


def test_changelog_is_byte_identical_across_runs() -> None:
    entries = [
        _entry("d1", "decision", ts="2026-06-01T00:00:00Z"),
        _entry("de1", "deadend", ts="2026-06-02T00:00:00Z"),
    ]
    assert render_changelog(entries) == render_changelog(entries)


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
    """init + a spread of types/tags so export has decisions AND dead-ends."""
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(
        app,
        ["add", "decision", "use jsonl", "--why", "diffable",
         "--files", "shiplog/store.py", "--tags", "storage"],
    ).exit_code == 0
    assert runner.invoke(
        app,
        ["add", "deadend", "sqlite backend", "--why", "heavy dep", "--tags", "perf"],
    ).exit_code == 0
    assert runner.invoke(
        app,
        ["add", "decision", "typer cli", "--why", "declarative"],
    ).exit_code == 0
    assert runner.invoke(app, ["add", "note", "tidy readme"]).exit_code == 0


def test_export_adr_writes_files(repo: Path) -> None:
    _seed(repo)
    out_dir = repo / "docs" / "adr"
    result = runner.invoke(app, ["export", "adr", "--out", str(out_dir)])
    assert result.exit_code == 0, result.output
    files = sorted(p.name for p in out_dir.glob("*.md"))
    # Two decisions → two ADRs, numbered in log order.
    assert files == ["0001-use-jsonl.md", "0002-typer-cli.md"]
    assert "diffable" in (out_dir / "0001-use-jsonl.md").read_text()


def test_export_adr_is_idempotent_on_disk(repo: Path) -> None:
    _seed(repo)
    out_dir = repo / "docs" / "adr"
    runner.invoke(app, ["export", "adr", "--out", str(out_dir)])
    first = (out_dir / "0001-use-jsonl.md").read_bytes()
    # Re-run: byte-identical files, and the summary reports 0 written.
    result = runner.invoke(app, ["export", "adr", "--out", str(out_dir)])
    assert result.exit_code == 0
    # Collapse whitespace: Rich may wrap the summary line in a narrow test tty.
    assert "0 written" in " ".join(result.output.split())
    assert (out_dir / "0001-use-jsonl.md").read_bytes() == first


def test_export_adr_requires_out(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["export", "adr"])
    assert result.exit_code == 1
    assert "output directory" in " ".join(result.output.split())


def test_export_adr_no_decisions_is_friendly_no_files(repo: Path) -> None:
    runner.invoke(app, ["init"])
    runner.invoke(app, ["add", "note", "just a note"])
    out_dir = repo / "docs" / "adr"
    result = runner.invoke(app, ["export", "adr", "--out", str(out_dir)])
    assert result.exit_code == 0
    assert "no decision entries" in " ".join(result.output.split())
    # No partial/garbage directory or files written.
    assert not out_dir.exists()


def test_export_changelog_to_stdout(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["export", "changelog"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "# Changelog" in out
    assert "### Decisions" in out and "### Dead-ends" in out
    assert "use jsonl" in out and "sqlite backend" in out
    # Notes are excluded from the digest.
    assert "tidy readme" not in out


def test_export_changelog_to_file(repo: Path) -> None:
    _seed(repo)
    out_file = repo / "CHANGELOG.shiplog.md"
    result = runner.invoke(app, ["export", "changelog", "--out", str(out_file)])
    assert result.exit_code == 0, result.output
    assert out_file.exists()
    assert "# Changelog" in out_file.read_text()


def test_export_changelog_file_is_idempotent(repo: Path) -> None:
    _seed(repo)
    out_file = repo / "CHANGELOG.shiplog.md"
    runner.invoke(app, ["export", "changelog", "--out", str(out_file)])
    first = out_file.read_bytes()
    result = runner.invoke(app, ["export", "changelog", "--out", str(out_file)])
    assert result.exit_code == 0
    assert "unchanged" in result.output
    assert out_file.read_bytes() == first


def test_export_filter_passthrough_type(repo: Path) -> None:
    _seed(repo)
    # --type deadend should drop decisions from the changelog.
    out = runner.invoke(app, ["export", "changelog", "--type", "deadend"]).output
    assert "sqlite backend" in out
    assert "use jsonl" not in out


def test_export_filter_passthrough_tag(repo: Path) -> None:
    _seed(repo)
    out_dir = repo / "adr"
    # Only the 'storage'-tagged decision survives the tag filter.
    runner.invoke(app, ["export", "adr", "--out", str(out_dir), "--tag", "storage"])
    files = sorted(p.name for p in out_dir.glob("*.md"))
    assert files == ["0001-use-jsonl.md"]


def test_export_filter_passthrough_since(repo: Path) -> None:
    _seed(repo)
    # A far-future --since filters everything out → friendly empty, no files.
    out_dir = repo / "adr"
    result = runner.invoke(
        app, ["export", "adr", "--out", str(out_dir), "--since", "2099-01-01"]
    )
    assert result.exit_code == 0
    assert "no decision entries" in " ".join(result.output.split())
    assert not out_dir.exists()


def test_export_bad_format_is_friendly_error(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["export", "wat"])
    assert result.exit_code == 1
    assert "unknown export format" in " ".join(result.output.split())


def test_export_bad_since_is_friendly_error(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["export", "changelog", "--since", "yesterday"])
    assert result.exit_code == 1
    assert "--since" in result.output


def test_export_bad_type_is_friendly_error(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["export", "changelog", "--type", "wat"])
    assert result.exit_code == 1
    assert "unknown entry type" in result.output


def test_export_before_init_fails(repo: Path) -> None:
    result = runner.invoke(app, ["export", "changelog"])
    assert result.exit_code == 1
    assert "shiplog init" in result.output
