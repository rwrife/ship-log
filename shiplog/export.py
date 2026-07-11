"""Human-facing markdown export for ship-log (``shiplog export``).

Where :mod:`shiplog.brief` is *ephemeral* and *agent-facing* (token-tuned digest
you paste into a prompt), ``export`` is *persistent* and *human-facing*: it turns
the append-only JSONL log into durable markdown artifacts you commit and ship in
release notes or a docs site. Two sub-formats:

* **ADR set** — one ``NNNN-slug.md`` per ``decision`` entry in the classic
  Architecture Decision Record shape, so the log becomes a browsable decision
  archive under e.g. ``docs/adr/``.
* **CHANGELOG digest** — a single markdown file grouping entries (decisions +
  dead-ends) by date, suitable for release notes.
* **HTML viewer** — a single self-contained ``.html`` file (CSS + JS inlined, no
  CDN, no build step) that renders every entry newest-first with type badges and
  a client-side filter box, so a human teammate can browse the log — e.g. served
  from GitHub Pages — without installing the CLI.

The rendering logic lives here as **pure functions** (entries → markdown string /
``{filename: content}`` map) so it's unit-testable without touching disk, and so
the CLI owns the actual file writes. Filtering is *not* re-implemented here: the
CLI narrows entries via the shared :mod:`shiplog.filters` helpers and hands the
already-filtered list to these functions.

Determinism is a hard requirement: the same input entries must produce
byte-identical output every run, so committing the results yields a clean no-op
diff when nothing changed. That means:

* ADR numbering is derived from **append order** (chronological), not wall-clock
  or hash, so a given decision keeps its number as long as earlier decisions
  don't change.
* No "generated at <now>" stamps anywhere — only data drawn from the entries.
* Slugs are a pure function of the summary (+ the stable number for uniqueness).
"""

from __future__ import annotations

import html
import re
from collections import OrderedDict

from .links import LinkView, links_for, split_links
from .models import Entry, EntryType

# Supported export formats (kept as a constant so the CLI can validate + list
# them in one place).
ADR = "adr"
CHANGELOG = "changelog"
HTML = "html"
FORMATS = (ADR, CHANGELOG, HTML)

# Width of the zero-padded ADR sequence number (0001, 0002, …). Four digits is
# the ADR convention and comfortably covers any realistic decision count.
_ADR_NUM_WIDTH = 4

# Keep slugs readable and filesystem-safe; long summaries get truncated on a word
# boundary so filenames stay tidy.
_SLUG_MAX_LEN = 60


def slugify(text: str) -> str:
    """Return a filesystem-safe, lowercase, hyphenated slug for ``text``.

    Non-alphanumeric runs collapse to a single hyphen; leading/trailing hyphens
    are stripped. Purely deterministic (no randomness), so the same summary always
    yields the same slug. Empty/blank input yields ``"untitled"`` so a filename is
    never degenerate.
    """
    flat = (text or "").strip().lower()
    # Replace any run of non [a-z0-9] with a single hyphen.
    slug = re.sub(r"[^a-z0-9]+", "-", flat).strip("-")
    if len(slug) > _SLUG_MAX_LEN:
        # Trim to the last full hyphen-delimited word within the cap so we don't
        # cut a word in half; fall back to a hard cut if the first word is huge.
        cut = slug[:_SLUG_MAX_LEN]
        if "-" in cut:
            cut = cut.rsplit("-", 1)[0]
        slug = cut.strip("-") or slug[:_SLUG_MAX_LEN].strip("-")
    return slug or "untitled"


def _date_of(entry: Entry) -> str:
    """Return the ``YYYY-MM-DD`` date portion of an entry's timestamp.

    Timestamps are ISO-8601 UTC (``2026-06-19T12:00:00Z``); we take the leading
    date. A missing/short ``ts`` yields ``"unknown"`` so grouping still has a
    stable, non-crashing bucket.
    """
    ts = (entry.ts or "").strip()
    if len(ts) >= 10 and ts[4] == "-" and ts[7] == "-":
        return ts[:10]
    return "unknown"


def _clean(text: str) -> str:
    """Collapse internal whitespace/newlines to single spaces and strip.

    Summaries/why are single-line in practice, but be defensive so an entry that
    smuggled a newline can't break markdown structure (headings, front-matter).
    """
    return " ".join((text or "").split())


# -- ADR export -----------------------------------------------------------


def adr_filename(number: int, summary: str) -> str:
    """Build the stable ADR filename ``NNNN-slug.md`` for a decision.

    ``number`` is 1-based (first decision → ``0001``). The slug is derived purely
    from the summary, so re-exporting an unchanged log reproduces the same name.
    """
    return f"{number:0{_ADR_NUM_WIDTH}d}-{slugify(summary)}.md"


def render_adr(entry: Entry, number: int) -> str:
    """Render a single ``decision`` entry as one ADR markdown document.

    Layout: YAML front-matter (stable, machine-readable metadata including the
    source entry id so you can trace it back to the log) followed by a human-
    readable body — title, context/decision (``why``), affected files, and
    references. Deterministic: every field comes from the entry, with no
    generation-time data, so output is byte-stable across runs.
    """
    title = _clean(entry.summary) or "(no summary)"
    date = _date_of(entry)
    author = _clean(entry.author) or "unknown"

    # Front-matter: quote string values so colons/special chars in a summary can't
    # break the YAML. Lists are rendered as flow sequences for compactness.
    def _q(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines: list[str] = ["---"]
    lines.append(f"id: {number:0{_ADR_NUM_WIDTH}d}")
    lines.append(f"title: {_q(title)}")
    lines.append(f"date: {_q(date)}")
    lines.append("status: accepted")
    lines.append(f"author: {_q(author)}")
    lines.append(f"source_entry: {_q(entry.id)}")
    if entry.tags:
        tags_flow = ", ".join(_q(_clean(t)) for t in entry.tags)
        lines.append(f"tags: [{tags_flow}]")
    lines.append("---")
    lines.append("")

    # Body.
    lines.append(f"# {number:0{_ADR_NUM_WIDTH}d}. {title}")
    lines.append("")
    lines.append(f"- **Date:** {date}")
    lines.append("- **Status:** Accepted")
    lines.append(f"- **Author:** {author}")
    lines.append(f"- **Source entry:** `{entry.id}`")
    if entry.ref:
        lines.append(f"- **Reference:** {_clean(entry.ref)}")
    lines.append("")

    lines.append("## Decision")
    lines.append("")
    lines.append(title)
    lines.append("")

    lines.append("## Rationale")
    lines.append("")
    lines.append(_clean(entry.why) if entry.why else "_No rationale recorded._")
    lines.append("")

    if entry.files:
        lines.append("## Affected files")
        lines.append("")
        for f in entry.files:
            lines.append(f"- `{_clean(f)}`")
        lines.append("")

    # Exactly one trailing newline for a clean, POSIX-y file (no diff churn).
    return "\n".join(lines).rstrip("\n") + "\n"


def build_adr_set(entries: list[Entry]) -> OrderedDict[str, str]:
    """Render all ``decision`` entries to an ordered ``{filename: content}`` map.

    Only ``decision`` entries become ADRs (attempts/dead-ends/notes are not
    architecture decisions). Numbering is 1-based in **append order** (the input
    list is the log's chronological order), so a decision keeps its number as long
    as no earlier decision is added/removed — the property that makes committing
    the output safe.

    If two decisions slugify identically (same summary), the numeric prefix still
    makes the filenames unique, so no collision handling is needed.

    Returns an :class:`~collections.OrderedDict` in ascending ADR-number order so
    callers can write files (and tests can assert order) deterministically.
    """
    out: OrderedDict[str, str] = OrderedDict()
    number = 0
    for entry in entries:
        if entry.type != EntryType.DECISION:
            continue
        number += 1
        out[adr_filename(number, entry.summary)] = render_adr(entry, number)
    return out


# -- CHANGELOG export -----------------------------------------------------

# Types that belong in a human changelog and the label used for each. Attempts
# and notes are intentionally excluded: a release digest wants the durable
# "what we decided / what we ruled out" signal, not every scratch note.
_CHANGELOG_TYPES: OrderedDict[str, str] = OrderedDict(
    [
        (EntryType.DECISION.value, "Decisions"),
        (EntryType.DEADEND.value, "Dead-ends"),
    ]
)


def _changelog_bullet(entry: Entry) -> str:
    """Render one entry as a single changelog bullet line.

    Shape: ``- summary — why (`id`)``. The ``why`` and a code-spanned id are
    appended when present so a reader can trace back to the source entry, while
    keeping the line to one row for skimmability.
    """
    parts = [f"- {_clean(entry.summary)}"]
    if entry.why:
        parts.append(f" \u2014 {_clean(entry.why)}")
    if entry.ref:
        parts.append(f" ({_clean(entry.ref)})")
    parts.append(f" [`{entry.id}`]")
    return "".join(parts)


def render_changelog(entries: list[Entry], *, title: str = "Changelog") -> str:
    """Render entries to a single CHANGELOG-style markdown digest.

    Entries are grouped by **date** (newest date first), and within each date by
    type (decisions, then dead-ends), each as a one-line bullet. Only decisions
    and dead-ends are included (see :data:`_CHANGELOG_TYPES`). Deterministic: dates
    sort descending, entries within a (date, type) bucket keep their append order,
    and there are no generation-time stamps — so re-running with the same log is a
    byte-identical no-op.

    An empty (or filtered-to-nothing) input yields a minimal document with a
    friendly placeholder rather than an error, so callers can still write a file
    if they choose (the CLI opts to warn + skip instead).
    """
    lines: list[str] = [f"# {title}", ""]

    relevant = [e for e in entries if e.type.value in _CHANGELOG_TYPES]
    if not relevant:
        lines.append("_No decisions or dead-ends logged yet._")
        return "\n".join(lines).rstrip("\n") + "\n"

    # Bucket by date, preserving append order within each date.
    by_date: OrderedDict[str, list[Entry]] = OrderedDict()
    for e in relevant:
        by_date.setdefault(_date_of(e), []).append(e)

    # Newest date first; "unknown" (undated) sorts last so real dates lead.
    # Real dates descending, then an "unknown" bucket appended at the end.
    real_dates = sorted((d for d in by_date if d != "unknown"), reverse=True)
    ordered_dates = real_dates + (["unknown"] if "unknown" in by_date else [])

    for date in ordered_dates:
        day_entries = by_date[date]
        heading = date if date != "unknown" else "Undated"
        lines.append(f"## {heading}")
        lines.append("")
        for type_value, label in _CHANGELOG_TYPES.items():
            bucket = [e for e in day_entries if e.type.value == type_value]
            if not bucket:
                continue
            lines.append(f"### {label}")
            lines.append("")
            lines.extend(_changelog_bullet(e) for e in bucket)
            lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


# -- HTML export ----------------------------------------------------------
#
# A single self-contained ``.html`` file: CSS + JS inlined, no external CDN, no
# build step, no framework. It renders every non-link entry newest-first (the
# human reading order, matching ``ls``) with a glanceable type badge, and inlines
# a vanilla-JS filter box (text / type / tag / file) that mirrors the ``ls``/TUI
# filters. Crucially the entries are rendered **server-side** (real DOM rows), so
# the page is fully readable with JavaScript disabled; the filter is progressive
# enhancement that just toggles ``hidden`` on the pre-rendered rows.
#
# Determinism is the same hard requirement as the markdown exports: no
# generation-time stamps anywhere, entries in a stable (ts-desc, append-order tie)
# sort, tags/files in given order -- so re-exporting an unchanged log yields a
# byte-identical file and a clean no-op diff.

# Human labels for the type badges (title-case; ``deadend`` reads as "Dead-end").
# The per-type accent colors live in the inlined CSS (badge/border classes) so the
# stylesheet stays the single source of truth and the file is fully self-contained.
_HTML_TYPE_LABEL: dict[str, str] = {
    EntryType.DECISION.value: "Decision",
    EntryType.ATTEMPT.value: "Attempt",
    EntryType.DEADEND.value: "Dead-end",
    EntryType.NOTE.value: "Note",
    EntryType.LINK.value: "Link",
}

_HTML_LINK_KIND_LABEL = {"commit": "commit", "pr": "PR", "ref": "ref"}


def _esc(text: str) -> str:
    """HTML-escape ``text`` (incl. quotes) for safe inlining in body/attributes.

    Everything user-authored (summaries, why, tags, file paths, refs) flows
    through here before it touches the document, so a summary containing ``<`` or
    ``"`` can never break out of its element or attribute.
    """
    return html.escape(text or "", quote=True)


def _short_sha(sha: str) -> str:
    """Trim a sha to the conventional 7-char short form (empty stays empty)."""
    return (sha or "").strip()[:7]


def _short_ts_html(ts: str) -> str:
    """Trim an ISO timestamp to ``YYYY-MM-DD HH:MM`` for compact display.

    The full ``ts`` is preserved verbatim in the row's ``title`` tooltip; this is
    just the glanceable label.
    """
    t = (ts or "").strip()
    if not t:
        return ""
    t = t.replace("T", " ")
    if t.endswith("Z"):
        t = t[:-1]
    return t[:16]


def _link_line(link: LinkView) -> str:
    """One human line for a link record: ``commit abc1234 - note``."""
    kind = _HTML_LINK_KIND_LABEL.get(link.kind, link.kind or "ref")
    base = f"{kind} {link.value or ''}".strip()
    note = (link.note or "").strip()
    return f"{base} \u2014 {note}" if note else base


def _entry_filter_blob(entry: Entry, link_lines: list[str]) -> str:
    """Lowercased haystack of an entry's text for the JS free-text filter.

    Concatenates summary/why/tags/files/ref/branch/sha (+ any link lines) so the
    filter box matches on any of them. Stored in a ``data-search`` attribute so
    the client never has to re-derive it from the DOM.
    """
    parts = [
        entry.summary, entry.why, entry.ref, entry.branch, entry.sha,
        *entry.tags, *entry.files, *link_lines,
    ]
    return " ".join(p for p in parts if p).lower()


def _render_entry_article(entry: Entry, links: list[LinkView]) -> str:
    """Render one entry as a self-contained ``<article>`` card (server-side).

    Includes ``data-*`` attributes (type / tags / files / search haystack) that the
    inlined JS uses to filter without reparsing the DOM. All user text is escaped.
    Dead-ends carry the ``deadend`` type class, which the CSS styles distinctly.
    """
    type_value = entry.type.value
    label = _HTML_TYPE_LABEL.get(type_value, type_value)
    link_lines = [_link_line(lv) for lv in links]

    data_tags = _esc(" ".join(t.strip().lower() for t in entry.tags))
    data_files = _esc(" ".join(f.strip().lower() for f in entry.files))
    data_search = _esc(_entry_filter_blob(entry, link_lines))

    # Dead-ends already carry the ``deadend`` type class (from ``type_value``),
    # which the CSS targets to make them visually distinct; no extra marker needed.
    out: list[str] = []
    out.append(
        f'<article class="entry {_esc(type_value)}" '
        f'data-type="{_esc(type_value)}" '
        f'data-tags="{data_tags}" data-files="{data_files}" '
        f'data-search="{data_search}">'
    )

    out.append('<header class="entry-head">')
    out.append(f'<span class="badge badge-{_esc(type_value)}">{_esc(label)}</span>')
    out.append(f'<h2 class="summary">{_esc(entry.summary) or "(no summary)"}</h2>')
    out.append(f'<code class="eid">{_esc(entry.id)}</code>')
    out.append("</header>")

    meta: list[str] = []
    if entry.ts:
        meta.append(
            f'<span class="meta-ts" title="{_esc(entry.ts)}">'
            f'{_esc(_short_ts_html(entry.ts))}</span>'
        )
    if entry.author:
        meta.append(f'<span class="meta-author">{_esc(entry.author)}</span>')
    if entry.branch:
        meta.append(f'<span class="meta-branch">{_esc(entry.branch)}</span>')
    if entry.sha:
        meta.append(f'<span class="meta-sha"><code>{_esc(_short_sha(entry.sha))}</code></span>')
    if entry.ref:
        meta.append(f'<span class="meta-ref">{_esc(entry.ref)}</span>')
    if meta:
        out.append('<div class="meta">' + "".join(meta) + "</div>")

    if entry.why:
        out.append(f'<p class="why">{_esc(entry.why)}</p>')

    if entry.tags:
        chips = "".join(f'<span class="tag">{_esc(t)}</span>' for t in entry.tags)
        out.append('<div class="tags">' + chips + "</div>")

    if entry.files:
        items = "".join(f"<li><code>{_esc(f)}</code></li>" for f in entry.files)
        out.append('<ul class="files">' + items + "</ul>")

    if links:
        rows = "".join(f"<li>{_esc(line)}</li>" for line in link_lines)
        out.append(
            '<div class="links"><span class="links-label">Links</span>'
            f'<ul class="link-list">{rows}</ul></div>'
        )

    out.append("</article>")
    return "\n".join(out)


# Inlined stylesheet -- dependency-free, respects light/dark, prints cleanly. No
# @import / no url() so the file makes zero network requests from file:// or Pages.
_HTML_CSS = """\
:root{color-scheme:light dark;--bg:#0d1117;--fg:#e6edf3;--muted:#8b949e;
--card:#161b22;--border:#30363d;--accent:#58a6ff;--dead:#cf222e}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
.wrap{max-width:920px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 2px}
.sub{color:var(--muted);margin:0 0 20px;font-size:13px}
.controls{position:sticky;top:0;background:var(--bg);padding:12px 0;
border-bottom:1px solid var(--border);margin-bottom:16px;z-index:5}
.controls input,.controls select{background:var(--card);color:var(--fg);
border:1px solid var(--border);border-radius:6px;padding:7px 9px;font-size:14px}
.controls input[type=search]{width:100%;margin-bottom:8px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.row select,.row input{flex:1 1 140px;min-width:120px}
.count{color:var(--muted);font-size:12px;margin-left:auto;flex:0 0 auto}
.entry{background:var(--card);border:1px solid var(--border);
border-left:4px solid var(--border);border-radius:8px;padding:14px 16px;margin:0 0 12px}
.entry.decision{border-left-color:#1a7f37}
.entry.attempt{border-left-color:#9a6700}
.entry.note{border-left-color:#0969da}
.entry.deadend{border-left-color:var(--dead);
background:linear-gradient(0deg,rgba(207,34,46,.06),rgba(207,34,46,.06)),var(--card)}
.entry-head{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.summary{font-size:16px;margin:0;flex:1 1 auto;font-weight:600}
.entry.deadend .summary{text-decoration:line-through;
text-decoration-color:rgba(207,34,46,.55)}
.badge{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;
padding:2px 7px;border-radius:999px;color:#fff;white-space:nowrap}
.badge-decision{background:#1a7f37}.badge-attempt{background:#9a6700}
.badge-deadend{background:#cf222e}.badge-note{background:#0969da}
.badge-link{background:#8250df}
.eid{color:var(--muted);font-size:12px}
.meta{color:var(--muted);font-size:12px;margin:8px 0 0;display:flex;gap:14px;flex-wrap:wrap}
.meta code{color:var(--muted)}
.why{margin:10px 0 0;white-space:pre-wrap}
.tags{margin:10px 0 0;display:flex;gap:6px;flex-wrap:wrap}
.tag{font-size:12px;background:rgba(88,166,255,.12);color:var(--accent);
border:1px solid rgba(88,166,255,.3);border-radius:999px;padding:1px 8px}
.files{margin:10px 0 0;padding-left:18px}
.files code{font-size:12px}
.links{margin:12px 0 0;border-top:1px dashed var(--border);padding-top:10px}
.links-label{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.link-list{margin:4px 0 0;padding-left:18px}
.link-list li{font-size:13px}
.empty{color:var(--muted);text-align:center;padding:48px 0;display:none}
.hidden{display:none}
@media(prefers-color-scheme:light){:root{--bg:#fff;--fg:#1f2328;--muted:#636c76;
--card:#f6f8fa;--border:#d0d7de}}
"""

# Inlined behaviour -- pure vanilla JS, no framework. Reads the pre-rendered rows
# and toggles ``.hidden`` based on the four filters. Everything degrades to a fully
# readable static page when JS is off (rows start visible; this only ever hides).
_HTML_JS = r"""
(function(){
  var q=document.getElementById('q'),ty=document.getElementById('ty'),
      tg=document.getElementById('tg'),fl=document.getElementById('fl'),
      count=document.getElementById('count'),empty=document.getElementById('empty'),
      rows=Array.prototype.slice.call(document.querySelectorAll('.entry'));
  function norm(s){return (s||'').trim().toLowerCase();}
  function apply(){
    var text=norm(q.value),type=norm(ty.value),tag=norm(tg.value),file=norm(fl.value),shown=0;
    for(var i=0;i<rows.length;i++){
      var r=rows[i],ok=true;
      if(type&&r.getAttribute('data-type')!==type)ok=false;
      if(ok&&tag){var tags=(r.getAttribute('data-tags')||'').split(/\s+/);
        if(tags.indexOf(tag)===-1)ok=false;}
      if(ok&&file){var files=(r.getAttribute('data-files')||'').split(/\s+/),hit=false;
        for(var j=0;j<files.length;j++){var f=files[j];
          if(f===file||f.slice(-(file.length+1))==='/'+file){hit=true;break;}}
        if(!hit)ok=false;}
      if(ok&&text&&(r.getAttribute('data-search')||'').indexOf(text)===-1)ok=false;
      r.classList.toggle('hidden',!ok);
      if(ok)shown++;
    }
    count.textContent=shown+' / '+rows.length;
    empty.style.display=shown?'none':'block';
  }
  [q,ty,tg,fl].forEach(function(el){el.addEventListener('input',apply);
    el.addEventListener('change',apply);});
  apply();
})();
"""


def _tag_options(entries: list[Entry]) -> list[str]:
    """Sorted unique tag list (lowercased) for the tag ``<select>`` dropdown."""
    seen: set[str] = set()
    for e in entries:
        for t in e.tags:
            tt = t.strip().lower()
            if tt:
                seen.add(tt)
    return sorted(seen)


def render_html(entries: list[Entry], *, title: str = "ship-log") -> str:
    """Render the log to one self-contained HTML document (string).

    ``entries`` is the full (already ``--type``/``--tag``/``--since``-filtered) log
    in **append order**; link records are split out and surfaced on their target
    entries, and the remaining primary entries are shown **newest-first** (the
    human reading order). CSS + JS are inlined -- the file makes zero network
    requests and works from ``file://`` or GitHub Pages.

    Deterministic: no generation-time stamps, stable sort (ts desc, append-order
    ties), tags/files kept in given order -- so the same log yields byte-identical
    output and committing the result is a clean no-op diff.

    An empty (or filtered-to-nothing) log still produces a valid page with an
    empty-state message rather than erroring, so callers can always write a file.
    """
    primary, link_entries = split_links(entries)

    # Newest-first for display; stable so equal-ts entries keep append order.
    ordered = sorted(primary, key=lambda e: e.ts, reverse=True)

    safe_title = _esc(title) or "ship-log"
    tag_opts = _tag_options(primary)

    out: list[str] = []
    out.append("<!DOCTYPE html>")
    out.append('<html lang="en">')
    out.append("<head>")
    out.append('<meta charset="utf-8">')
    out.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    out.append('<meta name="generator" content="shiplog">')
    out.append(f"<title>{safe_title}</title>")
    out.append("<style>")
    out.append(_HTML_CSS.rstrip("\n"))
    out.append("</style>")
    out.append("</head>")
    out.append("<body>")
    out.append('<div class="wrap">')
    out.append(f"<h1>{safe_title}</h1>")
    out.append('<p class="sub">Decision &amp; dead-end log \u2014 newest first. '
               "Filter below; dead-ends are highlighted.</p>")

    out.append('<div class="controls">')
    out.append('<input type="search" id="q" placeholder="Filter by text\u2026" '
               'autocomplete="off">')
    out.append('<div class="row">')
    out.append('<select id="ty" aria-label="Filter by type">'
               '<option value="">All types</option>'
               '<option value="decision">Decisions</option>'
               '<option value="attempt">Attempts</option>'
               '<option value="deadend">Dead-ends</option>'
               '<option value="note">Notes</option>'
               "</select>")
    tag_opt_html = "".join(
        f'<option value="{_esc(t)}">{_esc(t)}</option>' for t in tag_opts
    )
    out.append('<select id="tg" aria-label="Filter by tag">'
               '<option value="">All tags</option>' + tag_opt_html + "</select>")
    out.append('<input type="search" id="fl" placeholder="File\u2026" '
               'aria-label="Filter by file" autocomplete="off">')
    out.append(f'<span class="count" id="count">{len(ordered)} / {len(ordered)}</span>')
    out.append("</div>")
    out.append("</div>")

    out.append('<main id="entries">')
    if not ordered:
        out.append('<p class="sub">No entries logged yet.</p>')
    else:
        for entry in ordered:
            entry_links = links_for(entry.id, link_entries)
            out.append(_render_entry_article(entry, entry_links))
    out.append("</main>")

    out.append('<p class="empty" id="empty">No entries match the current filter.</p>')
    out.append("</div>")
    out.append("<script>")
    out.append(_HTML_JS.rstrip("\n"))
    out.append("</script>")
    out.append("</body>")
    out.append("</html>")

    return "\n".join(out).rstrip("\n") + "\n"
