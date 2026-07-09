"""Tests for ``shiplog export html``: the self-contained HTML viewer.

Two layers, mirroring :mod:`tests.test_export`:

* **Pure** (:mod:`shiplog.export`) — deterministic rendering of the HTML body
  (a golden/snapshot on the entry cards), the fully-offline property (no external
  requests), HTML-escaping, newest-first ordering, dead-end distinction, and link
  records surfacing on their target entry, all over hand-built
  :class:`~shiplog.models.Entry` lists (no git/CLI/disk).
* **End-to-end** (Typer) — the ``export html`` command against a throwaway git
  repo: default filename, ``--out <file>`` / ``--out -`` (stdout), idempotent
  re-runs, filter passthrough (incl. links surviving a ``--type`` filter), and
  friendly empty handling.

Required by #32: a self-contained (offline) HTML file, all entry fields rendered,
dead-ends visually distinguished, links surfaced on their target, an inlined
client-side filter, deterministic (golden) output, and no network/telemetry.
"""

from __future__ import annotations

import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shiplog.cli import app
from shiplog.export import render_html
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
    branch: str = "",
    sha: str = "",
    link_target: str = "",
    link_kind: str = "",
) -> Entry:
    """Build an Entry with explicit fields so HTML output is deterministic."""
    kwargs = dict(
        summary=summary,
        type=EntryType.coerce(type_),
        ts=ts,
        why=why,
        files=files or [],
        tags=tags or [],
        ref=ref,
        author=author,
        branch=branch,
        sha=sha,
        link_target=link_target,
        link_kind=link_kind,
    )
    if id is not None:
        kwargs["id"] = id
    return Entry(**kwargs)


# Patterns that would indicate an *external* request (the file must be fully
# self-contained: no CDN, no remote fonts/scripts/styles, no telemetry beacons).
_EXTERNAL_PATTERNS = (
    "http://",
    "https://",
    "//cdn",
    "@import",
    "url(",
    "src=",
    "integrity=",
    "crossorigin",
)


# -- offline / self-contained ---------------------------------------------


def test_html_is_fully_self_contained_no_external_requests() -> None:
    doc = render_html(
        [
            _entry("a decision", "decision", why="because"),
            _entry("a dead end", "deadend", why="nope"),
        ]
    )
    for pattern in _EXTERNAL_PATTERNS:
        assert pattern not in doc, f"HTML made an external reference: {pattern!r}"
    # CSS + JS are inlined (not linked).
    assert "<style>" in doc and "</style>" in doc
    assert "<script>" in doc and "</script>" in doc
    assert "<link" not in doc


def test_html_has_no_generation_timestamp() -> None:
    # Determinism guard: nothing time-of-render leaks in (only entry data).
    doc = render_html([_entry("x", "decision", ts="2026-06-01T00:00:00Z")])
    # The only ts present should be the entry's own; no "generated at"/now stamp.
    assert "generated at" not in doc.lower()
    assert "generated on" not in doc.lower()


# -- determinism (the headline property) ----------------------------------


def test_html_is_byte_identical_across_runs() -> None:
    entries = [
        _entry("first", "decision", why="because", files=["a.py"], tags=["x"]),
        _entry("second", "deadend", why="reasons", tags=["y"]),
        _entry("note", "note"),
    ]
    assert render_html(entries) == render_html(entries)


def test_html_ends_with_single_trailing_newline() -> None:
    doc = render_html([_entry("x", "decision")])
    assert doc.endswith("</html>\n")
    assert not doc.endswith("\n\n")


# -- golden / snapshot on the entry body ----------------------------------


def test_html_entry_card_golden() -> None:
    """Snapshot the rendered entry card for a fixed entry (acceptance: golden).

    Locks the structure/fields of one entry article so accidental markup changes
    are caught. Uses a fully-specified entry (every field populated) and asserts
    the exact ``<article>…</article>`` block.
    """
    e = _entry(
        "use jsonl for the log",
        "decision",
        ts="2026-06-19T12:00:00Z",
        why="diffable and greppable",
        files=["shiplog/store.py"],
        tags=["storage", "core"],
        ref="#12",
        id="260619-ABC123",
        author="Cap",
        branch="main",
        sha="abc1234def5678",
    )
    doc = render_html([e], title="ship-log")
    article = doc[doc.index("<article") : doc.index("</article>") + len("</article>")]

    expected = (
        '<article class="entry decision" '
        'data-type="decision" '
        'data-tags="storage core" data-files="shiplog/store.py" '
        'data-search="use jsonl for the log diffable and greppable #12 main '
        'abc1234def5678 storage core shiplog/store.py">\n'
        '<header class="entry-head">\n'
        '<span class="badge badge-decision">Decision</span>\n'
        '<h2 class="summary">use jsonl for the log</h2>\n'
        '<code class="eid">260619-ABC123</code>\n'
        "</header>\n"
        '<div class="meta">'
        '<span class="meta-ts" title="2026-06-19T12:00:00Z">2026-06-19 12:00</span>'
        '<span class="meta-author">Cap</span>'
        '<span class="meta-branch">main</span>'
        '<span class="meta-sha"><code>abc1234</code></span>'
        '<span class="meta-ref">#12</span>'
        "</div>\n"
        '<p class="why">diffable and greppable</p>\n'
        '<div class="tags"><span class="tag">storage</span>'
        '<span class="tag">core</span></div>\n'
        '<ul class="files"><li><code>shiplog/store.py</code></li></ul>\n'
        "</article>"
    )
    assert article == expected


def test_html_all_entry_fields_render() -> None:
    e = _entry(
        "summary here",
        "decision",
        why="the rationale",
        files=["pkg/mod.py"],
        tags=["alpha", "beta"],
        ref="#77",
        id="260601-ZZZ999",
        branch="feat/x",
        sha="deadbeef1234",
    )
    doc = render_html([e])
    assert "summary here" in doc
    assert "the rationale" in doc
    assert "pkg/mod.py" in doc
    assert ">alpha<" in doc and ">beta<" in doc
    assert "#77" in doc
    assert "260601-ZZZ999" in doc
    assert "feat/x" in doc
    assert "deadbee" in doc  # short sha (7 chars: 'deadbeef1234' -> 'deadbee')
    # The visible meta cell shows the 7-char short sha, not the full one.
    assert "<code>deadbee</code>" in doc
    # The full sha still lives in the searchable data-search haystack (for filter).
    assert "deadbeef1234" in doc


# -- HTML escaping (no injection) -----------------------------------------


def test_html_escapes_user_content() -> None:
    e = _entry(
        'break <b>out</b> & "quote"',
        "decision",
        why="<script>alert(1)</script>",
        files=['weird<>.py'],
        tags=['<tag>'],
        ref='<ref>',
    )
    doc = render_html([e])
    # No raw injected tags survive (the alert script must be escaped, not live).
    assert "<script>alert(1)</script>" not in doc
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in doc
    assert "break &lt;b&gt;out&lt;/b&gt; &amp; &quot;quote&quot;" in doc
    # Exactly one real <script> block (our behaviour JS), never the injected one.
    assert doc.count("<script>") == 1


# -- ordering: newest first -----------------------------------------------


def test_html_orders_entries_newest_first() -> None:
    entries = [
        _entry("oldest", "decision", ts="2026-06-01T09:00:00Z"),
        _entry("middle", "deadend", ts="2026-06-03T09:00:00Z"),
        _entry("newest", "note", ts="2026-06-05T09:00:00Z"),
    ]
    doc = render_html(entries)
    assert doc.index("newest") < doc.index("middle") < doc.index("oldest")


def test_html_equal_ts_keeps_append_order() -> None:
    ts = "2026-06-01T09:00:00Z"
    entries = [
        _entry("first appended", "decision", ts=ts),
        _entry("second appended", "decision", ts=ts),
    ]
    doc = render_html(entries)
    assert doc.index("first appended") < doc.index("second appended")


# -- dead-ends visually distinct ------------------------------------------


def test_html_deadend_is_visually_distinct() -> None:
    doc = render_html([_entry("bad path", "deadend", why="dont")])
    # Dead-ends carry the type class the CSS styles distinctly, plus a Dead-end badge.
    assert 'class="entry deadend"' in doc
    assert "badge-deadend" in doc
    assert ">Dead-end<" in doc
    # CSS actually differentiates dead-ends (strike-through + red accent present).
    assert ".entry.deadend" in doc
    assert "line-through" in doc


# -- links surface on their target ----------------------------------------


def test_html_link_surfaces_on_target_entry() -> None:
    dec = _entry("shipped decision", "decision", id="260601-TARGET")
    link = _entry(
        "links commit abc1234",
        "link",
        id="260602-LINKAA",
        link_target="260601-TARGET",
        link_kind="commit",
        ref="abc1234",
        why="landed here",
        ts="2026-06-02T00:00:00Z",
    )
    doc = render_html([dec, link])
    # The link is rendered as a Links section on the decision, not as its own card.
    assert 'data-type="link"' not in doc  # no standalone link card
    assert "<span class=\"links-label\">Links</span>" in doc
    assert "commit abc1234 \u2014 landed here" in doc


def test_html_orphan_link_without_target_is_dropped() -> None:
    # A link pointing at an entry not in the set simply doesn't render.
    link = _entry(
        "links pr 5",
        "link",
        id="260602-ORPHAN",
        link_target="260601-MISSING",
        link_kind="pr",
        ref="5",
    )
    doc = render_html([link])
    assert "links pr 5" not in doc
    assert "No entries logged yet" in doc  # empty of primary entries


# -- filter controls present + tag options --------------------------------


def test_html_has_client_side_filter_controls() -> None:
    doc = render_html([_entry("x", "decision", tags=["storage"])])
    # Four filter inputs (text/type/tag/file) + count/empty hooks the JS wires to.
    for hook in ('id="q"', 'id="ty"', 'id="tg"', 'id="fl"', 'id="count"', 'id="empty"'):
        assert hook in doc, f"missing filter hook {hook}"
    # Filtering runs in-page (no framework/CDN) — our behaviour JS is inlined.
    assert "getElementById('q')" in doc
    assert "classList.toggle('hidden'" in doc


def test_html_tag_dropdown_lists_sorted_unique_tags() -> None:
    entries = [
        _entry("a", "decision", tags=["zebra", "alpha"]),
        _entry("b", "deadend", tags=["alpha", "mango"]),
    ]
    doc = render_html(entries)
    # Options appear sorted + de-duplicated (alpha once, then mango, then zebra).
    select = doc[doc.index('id="tg"') : doc.index("</select>", doc.index('id="tg"'))]
    opts = re.findall(r'<option value="([^"]*)">', select)
    # First is the "All tags" empty option, then sorted unique tags.
    assert opts == ["", "alpha", "mango", "zebra"]


# -- empty log ------------------------------------------------------------


def test_html_empty_log_is_valid_not_error() -> None:
    doc = render_html([])
    assert doc.startswith("<!DOCTYPE html>")
    assert "No entries logged yet" in doc
    assert doc.endswith("</html>\n")


# -- well-formedness (defensive) ------------------------------------------


class _WellFormed(HTMLParser):
    """Minimal nesting checker: flags mismatched/extra close tags."""

    _VOID = {"meta", "input", "br", "hr", "img", "link", "source", "area", "base"}
    _IMPLICIT = {"p", "li", "option"}

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag not in self._VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack:
            self.errors.append(f"extra </{tag}>")
            return
        if self.stack[-1] == tag:
            self.stack.pop()
        elif tag in self.stack:
            while self.stack and self.stack[-1] != tag:
                if self.stack[-1] not in self._IMPLICIT:
                    self.errors.append(f"improperly nested <{self.stack[-1]}>")
                self.stack.pop()
            if self.stack:
                self.stack.pop()
        else:
            self.errors.append(f"mismatched </{tag}> (top={self.stack[-1]})")


def test_html_document_is_well_formed() -> None:
    doc = render_html(
        [
            _entry("d", "decision", why="w", files=["a.py"], tags=["t"], sha="abc1234"),
            _entry("de", "deadend", why="nope"),
        ]
    )
    checker = _WellFormed()
    checker.feed(doc)
    assert checker.errors == [], checker.errors
    # Only <html>/<body> may legitimately remain if not closed; assert fully closed.
    assert checker.stack == []


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
    """init + a spread of types/tags so the viewer has real content."""
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


def test_export_html_default_filename(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["export", "html"])
    assert result.exit_code == 0, result.output
    out = repo / "shiplog.html"
    assert out.exists()
    doc = out.read_text(encoding="utf-8")
    assert doc.startswith("<!DOCTYPE html>")
    assert "use jsonl" in doc and "sqlite backend" in doc
    # Notes DO show in the viewer (unlike changelog); it's the full log for humans.
    assert "tidy readme" in doc


def test_export_html_reports_entry_count(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["export", "html"])
    # 4 seeded entries (2 decisions, 1 deadend, 1 note); links excluded from count.
    assert "4 entries" in " ".join(result.output.split())


def test_export_html_to_named_file(repo: Path) -> None:
    _seed(repo)
    out = repo / "docs" / "log.html"
    result = runner.invoke(app, ["export", "html", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "<!DOCTYPE html>" in out.read_text()


def test_export_html_to_stdout_dash(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["export", "html", "--out", "-"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("<!DOCTYPE html>")
    assert "use jsonl" in result.output
    # Streaming to stdout writes no file.
    assert not (repo / "shiplog.html").exists()


def test_export_html_is_idempotent_on_disk(repo: Path) -> None:
    _seed(repo)
    runner.invoke(app, ["export", "html"])
    first = (repo / "shiplog.html").read_bytes()
    result = runner.invoke(app, ["export", "html"])
    assert result.exit_code == 0
    assert "unchanged" in " ".join(result.output.split())
    assert (repo / "shiplog.html").read_bytes() == first


def test_export_html_title_option(repo: Path) -> None:
    _seed(repo)
    result = runner.invoke(app, ["export", "html", "--out", "-", "--title", "My Repo Log"])
    assert result.exit_code == 0
    assert "<title>My Repo Log</title>" in result.output
    assert "<h1>My Repo Log</h1>" in result.output


def test_export_html_filter_passthrough_type(repo: Path) -> None:
    _seed(repo)
    out = runner.invoke(app, ["export", "html", "--out", "-", "--type", "deadend"]).output
    assert "sqlite backend" in out
    assert "use jsonl" not in out
    assert "typer cli" not in out


def test_export_html_filter_passthrough_tag(repo: Path) -> None:
    _seed(repo)
    out = runner.invoke(app, ["export", "html", "--out", "-", "--tag", "storage"]).output
    assert "use jsonl" in out
    assert "sqlite backend" not in out


def test_export_html_link_survives_type_filter(repo: Path) -> None:
    _seed(repo)
    # Grab the first decision id and link a commit to it.
    ls = runner.invoke(app, ["ls", "--type", "decision", "--json"])
    import json as _json

    dec_id = _json.loads(ls.output)[0]["id"]
    linked = runner.invoke(app, ["link", dec_id, "--commit", "abc1234", "--note", "shipped"])
    assert linked.exit_code == 0, linked.output
    # Even filtered to just decisions, the link surfaces on the target decision.
    out = runner.invoke(app, ["export", "html", "--out", "-", "--type", "decision"]).output
    assert "commit abc1234" in out


def test_export_html_empty_selection_still_writes_valid_file(repo: Path) -> None:
    runner.invoke(app, ["init"])
    # A far-future --since filters everything out → still a valid (empty) page.
    result = runner.invoke(app, ["export", "html", "--since", "2099-01-01"])
    assert result.exit_code == 0
    out = repo / "shiplog.html"
    assert out.exists()
    assert "No entries logged yet" in out.read_text()


def test_export_html_before_init_fails(repo: Path) -> None:
    result = runner.invoke(app, ["export", "html"])
    assert result.exit_code == 1
    assert "shiplog init" in result.output
