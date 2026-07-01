# Changelog

All notable changes to **ship-log** are documented here.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
