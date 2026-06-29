# Using ship-log (for AI coding agents)

This repo carries a **ship-log**: an append-only ledger of decisions, attempts, and
dead-ends living in `.shiplog/`. It exists so you don't waste a session re-trying an
idea a previous agent already proved doesn't work here.

**Copy the block below into your system prompt / agent instructions.**

---

## ⚓ ship-log protocol

This repo uses [`ship-log`](https://github.com/rwrife/ship-log). Treat it as shared,
durable memory for what has already been tried in this codebase.

**1. Before you edit — read the log.**

```bash
shiplog brief                      # compact digest: dead-ends first, then decisions
shiplog brief --files path/to/f    # focus on the files you're about to touch
shiplog brief --json               # machine-readable, if you parse instead of read
```

`brief` leads with **dead-ends (what NOT to redo)**, then decisions, prioritizing entries
that touch files in the working tree. If a dead-end already covers your plan, pick a
different approach — don't re-litigate it.

**2. After you decide something — log it.**

```bash
# You chose an approach:
shiplog add decision "Use JSONL not SQLite for the store" \
  --why "merge-friendly + greppable" --files shiplog/store.py --tags storage

# You tried something that did NOT work — save the next agent the detour:
shiplog add deadend "Threading the append path; lock contention made it slower" \
  --why "GIL + fsync per write" --files shiplog/store.py

# Lighter-weight signals:
shiplog add attempt "Spiking a Rich-based TUI for ls" --files shiplog/render.py
shiplog add note "Config lives in .shiplog/config.toml, not pyproject"
```

`add` auto-stamps author, branch, short SHA, and a UTC timestamp — you only supply the
`type`, a one-line `summary`, and optional `--why/--files/--tags/--ref`.

**Entry types:** `decision` (a choice made) · `deadend` (tried, rejected — highest value to
future agents) · `attempt` (in-progress exploration) · `note` (context worth keeping).

**3. Rules of thumb**

- **Log dead-ends religiously.** A logged dead-end is the single highest-leverage thing
  you can leave behind — it deletes wasted work from every future session.
- **One line, then `--why`.** The summary is skim-bait; the rationale goes in `--why`.
- **Attach `--files`.** It's how `brief` decides what's relevant to the next task.
- **Don't edit the past.** The log is append-only. To correct an entry, add a new one.
- **Reference issues/PRs** with `--ref` (e.g. `--ref '#42'`) when a decision ties to one.

That's it: **`brief` in, `add` out.**

---

## Prefer native tools? (MCP)

If your client speaks the [Model Context Protocol](https://modelcontextprotocol.io),
run `shiplog mcp` (a stdio MCP server) so you call ship-log as **tools** instead of
shelling out: `shiplog_brief` before editing, `shiplog_add` after deciding, and
`shiplog_ls` to list. Same behavior as the CLI commands above. Launch the server with
the target repo as its working directory; see the README's *MCP server mode* section
for a client config snippet.

---

## Why this matters

AI-assisted PR volume keeps climbing, and the bottleneck has moved from *typing* to
*coordination & memory*. `git blame` tells you who changed a line; it never tells you what
was tried and thrown away. ship-log is that missing layer — written at decision-time, read
before the next edit, committed alongside the code so it's impossible to forget.

## If `shiplog` isn't installed

```bash
pipx install ship-log            # once it's published
# or, from a clone:
pipx install --editable .
shiplog init                     # creates .shiplog/ in the current repo
```

See [`README.md`](./README.md) for the full command reference.
