# Changelog

All notable changes to **ship-log** are documented here.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`shiplog guard`** — an opt-in **enforcing** `pre-commit` tripwire that turns
  dead-ends from passive warnings into a real gate. `shiplog guard install`
  wires a `pre-commit` hook that scans the staged diff and **fails the commit**
  when any staged file overlaps an open (un-acknowledged) `deadend` entry's
  `--files`, printing an actionable report (id, summary, why, overlapping files).
  Clear a specific one with `shiplog guard --ack <id>` (appends an append-only
  `ack` record pointing back at the dead-end; the original line is never mutated),
  or override a single commit with `SHIPLOG_GUARD=off` (`off`/`0`/`false`/`no`/`skip`)
  or git's `--no-verify`. `shiplog guard --json` reports what would block the
  currently staged files for agent parsing; `status`/`uninstall` mirror the nudge
  hook (surgical, reversible, never clobbers a foreign hook without `--force`).
  File-less dead-ends never block, and a missing `shiplog`/internal error degrades
  to *allow* so the guard can never wedge a repo.
- **`shiplog export html`** — render the log to a single **self-contained**
  `shiplog.html` viewer (CSS + JS inlined, no CDN, no build step, no framework) so
  a human teammate can browse a repo's decision history without installing the CLI
  — e.g. published to GitHub Pages. Renders every entry **newest-first** with type
  badges, rationale, files, tags, refs, branch/short-sha, and surfaces `link`
  records on their target entry; **dead-ends are visually distinct** (the headline
  value: "already tried, don't repeat"). Includes an inlined **client-side filter**
  (text / type / tag / file, vanilla JS) mirroring the `ls`/TUI filters, and works
  offline from `file://`. Reuses the `ls` filters (`--since`/`--type`/`--tag`);
  `--title` sets the page heading; `--out -` streams to stdout. Output is
  **deterministic** (no generation-time stamps) so re-exports are byte-identical.
  No network calls, no telemetry. README documents an optional GitHub Actions
  snippet to publish it to Pages on push to `main`.
- **`shiplog link <id>`** — attach a commit / PR / ref to an existing entry *after
  the fact*. Appends a tiny `link` record pointing back at the target (append-only;
  the original entry line is never mutated), so a decision logged before the code
  existed can be tied to the commit/PR that shipped it. `shiplog show <id>` renders
  a **Links** section (newest-first) and `--json` includes a `links` array. Exactly
  one of `--commit` / `--pr` / `--ref` is required; `--note` adds a human label.
  Link records stay out of the default `ls` table and `brief` digest (reveal them
  with `shiplog ls --type link`). Reuses the existing flat schema — no
  `SCHEMA_VERSION` bump.
- **Conflict-free merges** — `shiplog install-merge-driver` registers a git *union*
  merge driver (a committed `.gitattributes` rule + a per-clone `.git/config` entry)
  so concurrent branches appending to `.shiplog/log.jsonl` merge with **no conflict**:
  git hands both sides to the driver, which takes their union, **dedupes by entry
  `id`**, and **stable-sorts** (by `ts`, then `id`) → byte-identical output regardless
  of merge order, no `<<<<<<<` markers. Idempotent; never clobbers a foreign
  `.gitattributes`; `--status` / `--uninstall` supported. Plus **`shiplog fix`** to
  repair logs mangled *before* the driver was installed: `--check` exits non-zero on
  duplicates / out-of-order entries (CI guard), `--write` normalizes in place
  (idempotent). `fix` only reorders and de-dupes — entry content is never changed,
  `link` records are preserved, and unparseable lines are kept (pinned to the end).

## [0.1.0] — 2026-06-30

First public release. A git-native, append-only captain's log that lives inside your
repo (`.shiplog/`) so agents (and humans) can read what's already been tried before
touching the code.

### Added

- **Core workflow** — `shiplog init`, `add`, `ls`, `show`, and `brief`:
  - `init` scaffolds `.shiplog/log.jsonl` + `.shiplog/config.toml` (idempotent).
  - `add` appends a `decision` / `attempt` / `deadend` / `note`, auto-stamping git
    author, branch, short SHA, and a UTC timestamp; supports `--why/--files/--tags/--ref`.
  - `ls` renders a skimmable, newest-first table with `--type/--tag/--file/--since` filters.
  - `show <id>` prints full detail for one entry (accepts a unique id prefix).
  - `brief` emits a token-efficient markdown digest — **dead-ends first**, then recent
    decisions, prioritizing files in the working tree — built to drop into an agent prompt.
- **`--json`** on every read command (`ls`, `show`, `brief`, `blame`) for agent parsing.
- **Storage** — plain append-only JSONL (`.shiplog/log.jsonl`): diffable, greppable, and
  merge-friendly without the tool. Short sortable ids (`<yymmdd>-<6char>`) and a schema
  version field.
- **`shiplog blame <file>:<line>`** — the nearest logged decision/dead-end anchored to a
  line range; the "why" `git blame` lacks.
- **`shiplog hook`** — install/manage a `prepare-commit-msg` nudge to log a decision.
- **`shiplog mcp`** — a stdio [Model Context Protocol](https://modelcontextprotocol.io)
  server exposing `add` / `ls` / `brief` as native agent tools.
- **`shiplog tui`** — an optional full-screen, filterable browser (via the `tui` extra).
- **`AGENT.md`** — a copy-paste protocol teaching agents to `shiplog brief` before editing
  and `shiplog add` after deciding (with an MCP variant).
- **Packaging** — `pipx install ship-log`, console entrypoint `shiplog`, and OIDC trusted
  publishing to TestPyPI → PyPI via `.github/workflows/release.yml`.
- **CI** — `ruff` + `pytest` on Python 3.11 and 3.12.

[0.1.0]: https://github.com/rwrife/ship-log/releases/tag/v0.1.0
