# ship-log 🧭⚓

**A git-native captain's log for the multi-agent era.**

When several AI coding agents (and you) churn the same repo, everyone keeps re-trying the
same already-failed ideas. `ship-log` is a tiny, append-only, plain-text ledger that lives
*inside your repo* (`.shiplog/`). Every **decision**, **attempt**, and **dead-end** gets one
line and a "why." The next agent reads the log before touching code — and skips the graveyard.

> Not `git blame` (who/when). Not a diff reviewer (is-this-good).
> It's the *forward-looking memory* of what's already been tried here.

## Demo

![ship-log demo: init, log a decision + a dead-end, then brief reads it back](./demo/shiplog.gif)

*`brief`-in / `add`-out in ~8 seconds: log a decision and a dead-end, then watch `brief`
lead with the dead-end so the next agent skips the graveyard.* Regenerate it (or grab the
asciinema [`shiplog.cast`](./demo/shiplog.cast)) from [`demo/`](./demo).

## Why

- AI-assisted PR volume is up ~29% YoY — the bottleneck is now **coordination & memory**, not typing.
- `git blame` tells you *who* changed a line, never *what was tried and rejected*.
- Agents have no shared, durable, per-repo memory. Now they do — and it's just a file.

## Install

```bash
pipx install ship-log          # isolated, on your PATH (recommended)
pipx install 'ship-log[tui]'   # …with the optional full-screen browser (shiplog tui)
shiplog --version              # -> shiplog 0.1.0
python -m shiplog --version    # same thing, no PATH shim (handy in CI/venvs)
```

> Publishing to PyPI is wired up via OIDC trusted publishing (see
> `.github/workflows/release.yml`); until the first tag lands, install from a clone:

```bash
git clone https://github.com/rwrife/ship-log
cd ship-log
pipx install --editable .      # or, for development: uv pip install -e ".[dev]"
shiplog hello                  # friendly banner; proof the install works
```

## Quickstart

`init`, `add`, `ls`, `show`, and `brief` all work today (through M5).

```bash
shiplog init                   # creates .shiplog/log.jsonl + .shiplog/config.toml (idempotent)
shiplog add decision "Use JSONL not SQLite for the store" \
  --why "merge-friendly + greppable" --files shiplog/store.py --tags storage
shiplog add deadend "Tried threading for append; lock contention" --files shiplog/store.py
```

Every `add` auto-stamps the entry with your git **author**, **branch**, **short sha**, and a
UTC **timestamp** — you only type the `type` + one-line summary (plus optional
`--why/--files/--tags/--ref`). Entries are plain JSONL in `.shiplog/log.jsonl`, so they're
diffable and greppable without the tool.

### Read it back (M4)

```bash
shiplog ls                     # skimmable Rich table, newest first
shiplog ls --type deadend      # skim what NOT to redo
shiplog ls --tag storage       # filter by tag
shiplog ls --file store.py     # entries touching a path (suffix match)
shiplog ls --since 7d          # last 7 days (also: 24h, 2w, or an ISO date)
shiplog ls --json              # stable JSON array for agents/pipes

shiplog show 260621-K3F9Q2     # full detail for one entry (id or unique prefix)
shiplog show 260621-K3 --json  # same, machine-readable object
```

Filters are AND-combined and case-insensitive; `--json` on `ls`/`show` emits clean,
ANSI-free output (array for `ls`, object for `show`) so agents parse instead of scrape.

### Link it — attach a commit / PR / ref after the fact

You log a decision **at decision-time**, before the commit exists. Later it lands
in `abc1234` or PR #42. `shiplog link` closes that gap **without breaking
append-only**: it appends a tiny `link` record pointing back at the original entry
(the original line is never mutated), and `shiplog show` then surfaces the
accumulated links.

```bash
# log the decision now (before the code exists)
shiplog add decision "Switch the store to JSONL" --why "diffable, mergeable"

# ...you write the code and ship it, then tie the record to what landed:
shiplog link 260621-K3F9Q2 --commit abc1234
shiplog link 260621-K3F9Q2 --pr "#42" --note "first cut"
shiplog link 260621-K3F9Q2 --ref "https://tracker/PROJ-7"

shiplog show 260621-K3F9Q2   # now renders a Links section (newest-first)
```

Exactly one of `--commit` / `--pr` / `--ref` is required. Link records don't
clutter `ls`/`brief` as standalone rows (see them with `shiplog ls --type link`);
`shiplog show <id> --json` includes a `links` array for agents.

### Brief it (M5) — the headline feature

`shiplog brief` prints a compact, token-efficient digest to drop straight into an
agent's context **before** it edits — leading with dead-ends (what NOT to redo),
then decisions, prioritizing entries that touch files in your **working tree**.

```bash
shiplog brief                  # markdown digest, dead-ends first, scoped to the working tree
shiplog brief --files cli.py   # focus on specific paths instead of the working tree
shiplog brief --limit 20       # tune the size budget (default 12; 0 = no cap)
shiplog brief --json           # machine-readable: {entries[], focus, total, deadends, ...}
```

Example output:

```markdown
# ship-log brief
_focus: shiplog/store.py · 2 dead-ends · 5 of 5 entries_

## Dead-ends (do NOT redo)
- `260622-534CC7` Tried threading for append; lock contention — GIL + fsync made it slower _(shiplog/store.py)_

## Decisions
- `260622-4RXE2Y` Use JSONL not SQLite — merge-friendly + greppable _(shiplog/store.py)_
```

The digest is ranked *before* the size budget is applied, so truncation always drops
the least-relevant tail — never a dead-end in favor of an old note. Output is plain
markdown (verbatim, no ANSI) so it's clean when piped into a prompt.

### Browse it (TUI) — the cozy full-screen view

Prefer scrolling to grepping? `shiplog tui` opens a full-screen, keyboard-first
browser: a newest-first Rich table on the left, a detail pane on the right, and a
live filter box. It reuses the **exact same** store/filters/rendering as
`ls`/`show` (no logic fork) — it's just a cozier lens on the same log.

```bash
pipx install 'ship-log[tui]'   # the TUI needs the optional `textual` dependency
shiplog tui                    # full-screen browser of this repo's log
```

Keyboard-first navigation:

- **`/`** — jump to the search box; type to filter live across
  summary / why / tags / files / id (space-separated terms are AND-combined).
- **`t`** / **`T`** — cycle the type filter forward / back
  (all → dead-end → decision → attempt → note → all).
- **`↑` / `↓` + `Enter`** — move the selection; the detail pane follows.
- **`Esc`** — clear the search (then hand focus back to the list).
- **`q`** — quit.

If the `textual` extra isn't installed, `shiplog tui` prints a one-line install
hint instead of a traceback — every other command works without it.

### Blame a line — the "why" `git blame` lacks

`shiplog blame <file>:<line>` surfaces the nearest logged decision/dead-end anchored to
a line. `git blame` tells you *who* last touched a line; `shiplog blame` tells you *what
was decided or ruled out there, and why*.

```bash
shiplog blame shiplog/store.py:50   # nearest rationale for line 50, + alternates
shiplog blame store.py:50           # basename works too (path-suffix match)
shiplog blame shiplog/store.py      # no line → all entries touching the file, recent first
shiplog blame shiplog/store.py:50 --json   # stable object: {target, best, alternates, count}
```

Anchor an entry to a line range when you log it, so `blame` can pinpoint it:

```bash
shiplog add deadend "threading the append loop thrashes the lock" \
  --files shiplog/store.py:40-80 --why "GIL + fsync made it slower"
```

A file reference is `path`, `path:line`, or `path:start-end`. Ranking is
**containment → proximity → tighter range → recency**: an entry whose range *covers*
the line wins, then nearer ranges, then whole-file references, with newest breaking ties.
Plain (range-less) entries still match — they just rank below a line-pinned one. If nothing
touches the file you get a friendly hint, not an error.

### Stats — the whole-log health read

`shiplog stats` zooms all the way out: a compact dashboard of the *entire* log. Where
`brief` is per-context ("what should I know before touching these files"), `stats` answers
*"are we deciding or thrashing?"* — led by the **dead-end ratio** (`deadends / (decisions +
attempts)`), plus recent activity, decision hotspots, and who's logging.

```bash
shiplog stats                  # totals by type + dead-end ratio, activity, top files/tags/authors
shiplog stats --since 30d      # window it (relative 7d/24h/2w or an ISO date — same parser as ls/brief)
shiplog stats --top 10         # more rows in the top-files/tags/authors lists (0 = all)
shiplog stats --json           # stable object for agents (keys below)
```

The `--json` object is stable for agents: `total`, `by_type` (`{type: count}`),
`deadend_ratio` (float in `[0,1]`, or `null` when nothing's been tried yet — no
divide-by-zero), `recent` (`{"7": n, "30": n}`), `per_week` (`[{week, count}, …]`, oldest
first), `top_files` / `top_tags` / `top_authors` (`[{name, count}, …]`, highest first), and
the `first_ts`/`last_ts` span. An empty log prints a friendly "no entries yet" line and
exits 0.

### Verify it — a CI gate against log rot

Agents append autonomously, and an append-only file can rot: a malformed JSON line, a
missing field, an unknown `type`, a duplicate `id`, a dangling `link`/`ack` target, or a
`schema_version` an older CLI can't read. `shiplog verify` is a fast, **read-only**
validator that returns a clean exit code so a bad append can't silently ship. It
complements the [merge driver](#conflict-free-merges-union-merge-driver) (which
unions/dedupes) by *catching* corruption instead of masking it.

```bash
shiplog verify            # exit 0 if clean, 1 on any error
shiplog verify --strict   # also fail on warnings (non-monotonic timestamps)
shiplog verify --json     # structured findings (line, id, code) for agents/CI
```

Drop it into CI as a 3-line gate:

```yaml
- name: Verify ship-log
  run: pipx run ship-log verify --strict
```

The `--json` object is stable: `ok` (bool), `checked` (int), `strict` (bool), `errors` /
`warnings` counts, and `findings` (`[{line, id, code, severity, message}, …]`). Codes are
greppable: `bad-json`, `not-object`, `missing-field`, `unknown-type`, `duplicate-id`,
`schema-too-new`, `bad-schema-version`, `dangling-ref`, `non-monotonic-ts`.

### Export it — durable ADR / CHANGELOG markdown for humans

`brief` is *ephemeral* (token-tuned, for agents). `shiplog export` is *persistent*: it
renders the log into human-facing markdown you commit and ship at milestones. Log
continuously → export for humans when you cut a release.

```bash
shiplog export adr --out docs/adr/            # one classic NNNN-slug.md per DECISION entry
shiplog export changelog                      # grouped digest (decisions + dead-ends) to stdout
shiplog export changelog --out CHANGELOG.shiplog.md   # …or write it to a file
shiplog export changelog --since 30d --tag release    # reuse the ls filters (--since/--type/--tag)
shiplog export html                           # single self-contained shiplog.html viewer (for humans)
shiplog export html --out - > public/log.html # …or stream it wherever you publish
```

- **`adr`** → an [Architecture Decision Record](https://adr.github.io/) set: one
  `NNNN-slug.md` per `decision`, numbered from log order (stable — a decision keeps its
  number as earlier ones don't change), with front-matter, rationale, affected files, and
  the source entry id for traceability. `--out <dir>` is required.
- **`changelog`** → a single markdown digest grouping decisions + dead-ends by date
  (newest first); prints to stdout, or `--out <file>` to write it.
- **`html`** → a single **self-contained** `shiplog.html` (CSS + JS inlined — no CDN, no
  build step, no framework; opens straight from `file://`). Renders every entry
  newest-first with type badges, rationale, files, tags, refs, and branch/short-sha;
  `link` records surface on their target entry, and **dead-ends are visually distinct**.
  Ships an inlined **client-side filter** (text / type / tag / file) mirroring the
  `ls`/TUI filters. Writes `shiplog.html` by default (`--out <file>`, or `--out -` for
  stdout); `--title` sets the page heading. Perfect for publishing the log to GitHub
  Pages so a PM or teammate can browse "why we abandoned approach X" as a URL — **no
  network calls, no telemetry**.

Output is **deterministic**: re-running with no new entries rewrites nothing
(byte-identical, so committing the results is a clean no-op diff). An empty/filtered-to-
nothing selection prints a friendly note and exits 0 without writing any partial files.

#### Publish the HTML viewer to GitHub Pages (optional)

Export on every push to `main` and publish the single file to Pages — the log becomes a
shareable, linkable URL (enable Pages → "GitHub Actions" in repo settings first):

```yaml
# .github/workflows/shiplog-pages.yml
name: Publish ship-log
on:
  push:
    branches: [main]
permissions:
  pages: write
  id-token: write
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pipx install ship-log && shiplog export html --out public/index.html
      - uses: actions/upload-pages-artifact@v3
        with:
          path: public
  deploy:
    needs: build
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deploy.outputs.page_url }}
    steps:
      - id: deploy
        uses: actions/deploy-pages@v4
```

### Commit-time nudge (git hook)

The log only helps if it stays fresh. `shiplog hook install` adds an opt-in
`prepare-commit-msg` git hook that spots *interesting* commits and reminds you to log a
decision — **without ever blocking the commit**.

```bash
shiplog hook install      # add the prepare-commit-msg nudge to this repo
shiplog hook status       # is it installed?
shiplog hook uninstall    # remove it (surgical + reversible)
```

A commit is “interesting” when it touches several files (default ≥ 3, tune via
`hook_file_threshold` in `.shiplog/config.toml`) **or** its subject smells like a real
decision (`refactor`, `rewrite`, `switch to`, `drop`, `revert`, `breaking`, …). When it
is, the hook injects a few **commented** lines into your commit-message template:

```text
# ⚓ ship-log: this looks like a notable change. Consider logging a decision
#    so the next agent (or you, tomorrow) knows the *why*:
#      shiplog add decision "<your subject>" --why "..."
```

Git strips comment lines before committing, so the nudge shows up where you're already
typing, then **vanishes from the actual commit** — zero pollution, and it's impossible to
“fail.” It stays out of the way for non-interactive commits (`-m`, merges, squashes,
rewords). Installing never clobbers a pre-existing hook (use `--force` to overwrite), and
uninstall removes only ship-log's block if you've added your own content alongside it.

### Dead-end guard (enforcing pre-commit tripwire)

The nudge only *reminds* — and agents ignore reminders. `shiplog guard` is the opt-in
**seatbelt**: a `pre-commit` hook that scans your staged diff, finds any open `deadend`
entry whose `--files` overlap the files you're about to commit, and **fails the commit**.
Institutional memory goes from passive warning to a real gate.

```bash
shiplog guard install      # add the enforcing pre-commit hook to this repo
shiplog guard status       # is it installed?
shiplog guard uninstall    # remove it (surgical + reversible)
shiplog guard --json       # report what would block your currently staged files
```

When a staged file overlaps an open dead-end, the commit is blocked with an actionable
message:

```text
⚓ guard: 1 open dead-end blocks this commit:
  260710-A1B2C3 — switched to asyncpg, deadlocked under load
      why: pool starvation under concurrent writes
      files: db.py
  Acknowledge with shiplog guard --ack <id>, or override once with
  SHIPLOG_GUARD=off (or git commit --no-verify).
```

Two escape hatches:

- **Acknowledge** a specific dead-end once you've made peace with it:
  `shiplog guard --ack <id>` (optionally `--note "..."`). This appends an append-only
  `ack` entry pointing back at the dead-end — the original line is never mutated — and
  that dead-end stops blocking future commits.
- **Override** the whole gate for a single commit: `SHIPLOG_GUARD=off git commit`
  (any of `off`/`0`/`false`/`no`/`skip`), or git's own `--no-verify`.

Dead-ends with **no recorded files** never block (too blunt to gate on). Installing never
clobbers a pre-existing `pre-commit` hook (use `--force` to append the guard block after
yours); uninstall strips only ship-log's block. A missing `shiplog` on `PATH` or any
internal error degrades to *allow* — the guard can never wedge a repo.

### Conflict-free merges (union merge driver)

ship-log's whole premise is *many branches* (one per agent) each appending to
`.shiplog/log.jsonl`. Left to git, two branches that both append lines hit a classic
append-region **merge conflict**, and even a hand resolution can leave the log with
duplicate or out-of-order entries. `shiplog install-merge-driver` makes merges
**conflict-free by construction**:

```bash
shiplog install-merge-driver            # register the union merge driver in this clone
shiplog install-merge-driver --status   # is it installed?
shiplog install-merge-driver --uninstall # remove it (surgical + reversible)
```

It writes two things: a committed `.gitattributes` rule
(`.shiplog/log.jsonl merge=shiplog`) so collaborators inherit the routing, and a
per-clone `.git/config` entry defining the driver command. On a merge, git hands both
sides to the driver, which takes their **union**, **dedupes by entry `id`**, and emits a
**stable sort** (by timestamp, then id) — so both branches converge on **byte-identical**
output regardless of merge order, with no `<<<<<<<` markers ever. It's idempotent and
never clobbers a foreign `.gitattributes` (only its own fenced block). Commit
`.gitattributes`; each collaborator runs `install-merge-driver` once per clone (the
`.git/config` half isn't committed).

**Already have a mangled log?** `shiplog fix` runs the same dedupe + stable-sort over the
current log (for logs corrupted *before* the driver was installed):

```bash
shiplog fix --check   # exit 1 if the log has dupes / is out of order (CI-friendly); 0 if clean
shiplog fix --write   # rewrite it in canonical form (idempotent; content never changed)
shiplog fix           # dry run: report what --write would change, touch nothing
```

`fix` only ever changes *ordering* and removes exact `id` duplicates — an entry's content
is never touched, `link` records are preserved, and any unparseable line is kept (pinned
to the end) rather than dropped. Wire `shiplog fix --check` into CI to catch a bad merge
before it lands.

## For agents

The whole point: an agent runs `shiplog brief` **before** editing and `shiplog add`
**after** making a decision. Drop [`AGENT.md`](./AGENT.md) (or paste its protocol block)
into your agent's instructions — it's copy-paste ready.

```bash
shiplog brief                  # read this BEFORE you edit — skip known dead-ends
# …make a call…
shiplog add deadend "Tried X; it broke Y" --why "..." --files path  # log it AFTER
```

See [`demo/`](./demo) for a runnable walkthrough (`./demo/demo.sh`) plus a recorded
asciinema cast ([`shiplog.cast`](./demo/shiplog.cast)) and the [`shiplog.gif`](./demo/shiplog.gif)
shown above.

## MCP server mode

Prefer that your agent call ship-log as **native tools** instead of shelling out? Run
the built-in [Model Context Protocol](https://modelcontextprotocol.io) server:

```bash
shiplog mcp        # stdio MCP server, operates on the repo it's launched in
```

It speaks newline-delimited JSON-RPC on stdin/stdout and exposes three tools — backed
by the **exact same** store/ranking/filters as the CLI (no logic fork):

- **`shiplog_brief`** — token-efficient digest (dead-ends first) to read *before* editing.
- **`shiplog_add`** — append a `decision`/`attempt`/`deadend`/`note` (git-stamped) *after* deciding.
- **`shiplog_ls`** — list entries newest-first with optional `type`/`tag`/`file`/`since`/`limit` filters.

Each tool returns both a human-readable text block and `structuredContent` (the same
stable JSON as the CLI's `--json`).

**Point a client at a repo** by launching the server with that repo as its working
directory. For a Claude Desktop–style client, add to its MCP config:

```json
{
  "mcpServers": {
    "ship-log": {
      "command": "shiplog",
      "args": ["mcp"],
      "cwd": "/absolute/path/to/your/repo"
    }
  }
}
```

(For a clone instead of an install, use `"command": "uv"` /
`"args": ["run", "shiplog", "mcp"]`, or point at the venv's `shiplog`.) The repo must
have been `shiplog init`'d. Quick manual smoke test:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | shiplog mcp
```

## Status

🚧 v0.1 in progress. See [`PLAN.md`](./PLAN.md) for scope, architecture, and milestones (M1–M6).

- **M1** — package scaffold, `shiplog --version` / `shiplog hello`. ✅
- **M2** — append-only JSONL store backbone (`shiplog/models.py` + `shiplog/store.py`): `Entry`
  model, JSONL (de)serialization, sortable ids, file-locked concurrent append + read. ✅
- **M3** — `shiplog init` (creates `.shiplog/` + `config.toml`, idempotent) and `shiplog add`
  (git-stamped append with validation + friendly errors), via `shiplog/gitctx.py` +
  `shiplog/config.py`. ✅
- **M4** — `shiplog ls` (Rich table, newest-first, `--type/--tag/--file/--since/--limit`) and
  `shiplog show <id>` (full detail, id or unique prefix), both with `--json`, via
  `shiplog/filters.py` + `shiplog/render.py`. ✅
- **M5** — `shiplog brief` (token-efficient markdown digest: dead-ends first, then decisions,
  prioritizing working-tree / `--files`; `--limit` size budget; `--json` variant), via
  `shiplog/brief.py`. ✅
- **M6** — polish + agent ergonomics: copy-paste [`AGENT.md`](./AGENT.md) protocol (brief-in /
  add-out), README quickstart, runnable [`demo/`](./demo) walkthrough with a recorded
  [cast + gif](./demo/shiplog.gif), a [`CHANGELOG`](./CHANGELOG.md), a warning-free import,
  a lean published sdist, and an OIDC trusted-publishing release workflow
  (`.github/workflows/release.yml`) for TestPyPI → PyPI. ✅ 🚧 *(owner action: configure the
  trusted publisher, then push the `v0.1.0` tag to publish)*

### Backlog (v0.2+)

- **`shiplog blame <file>:<line>`** — nearest logged decision/dead-end for a line
  (containment → proximity → recency), with `--json`. ✅ Anchor entries via
  `--files path:start-end`.
- **Git hook installer** — `shiplog hook install` adds a non-blocking
  `prepare-commit-msg` nudge to log a decision on interesting commits. ✅
- **MCP server mode** — `shiplog mcp` exposes `shiplog_add`/`shiplog_brief`/`shiplog_ls`
  as Model Context Protocol tools over stdio (same store/ranking/filters as the CLI), so
  agents call ship-log natively. ✅ See [MCP server mode](#mcp-server-mode).
- **`shiplog tui`** — full-screen Textual browser: newest-first table, detail pane,
  live free-text + type filtering, keyboard-first (`/` search, `t` cycle type, `q`
  quit). ✅ Optional extra: `pipx install 'ship-log[tui]'`.
- **`shiplog stats`** — whole-log analytics: totals by type, dead-end ratio,
  recent activity (7d/30d + per-week sparkline), and top files/tags/authors, with
  `--since`/`--top`/`--json`. ✅ See [Stats](#stats--the-whole-log-health-read).
- **`shiplog export`** — durable, human-facing artifacts: an `adr` set (one
  `NNNN-slug.md` per decision), a grouped `changelog` digest, or a single
  self-contained `html` viewer (inlined CSS/JS, client-side filter, dead-ends
  distinct — publish it to GitHub Pages), reusing the `ls` filters and
  deterministic (idempotent, safe to commit). ✅ See
  [Export](#export-it--durable-adr--changelog-markdown-for-humans).
- **`shiplog link <id>`** — attach a commit / PR / ref to an existing entry after
  the fact by appending a `link` record (append-only; never mutates the original),
  surfaced as a Links section in `shiplog show` (+ `--json`). ✅ See
  [Link it](#link-it--attach-a-commit--pr--ref-after-the-fact).
- **Conflict-free merges** — `shiplog install-merge-driver` registers a git union
  merge driver (`.gitattributes` + `.git/config`) that dedupes by id and stable-sorts
  so two branches' logs merge with no conflict and byte-identical output; `shiplog fix
  --check/--write` repairs logs mangled before it was installed. ✅ See
  [Conflict-free merges](#conflict-free-merges-union-merge-driver).

## License

MIT (see `LICENSE`, added in M1).

---

Part of the `auto-tool-lab` experiment. Built small on purpose.
