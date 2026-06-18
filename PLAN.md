# ship-log 🧭⚓

> A git-native captain's log for the multi-agent era.

## 1. Pitch

When three AI coding agents and a sleep-deprived human all hack on the same repo,
they keep re-litigating the same decisions: "let's switch to X" → it breaks Y → revert →
next agent suggests X again three hours later. **ship-log** is a tiny, append-only,
plain-text ledger that lives *inside your repo* (`.shiplog/`) where every attempt, decision,
and dead-end gets logged with a one-line "why." The next agent (or you, tomorrow) reads the
log **before** touching the code and skips the graveyard of already-tried ideas.

It's not blame (who/when) and not a diff reviewer (is-this-good). It's **forward-looking
institutional memory** — the captain's log of your repo's voyage.

## 2. Trend inspiration

Everything I scanned this week pointed at one thing: AI agents are now writing a huge
share of the code, and the bottleneck has shifted from *typing* to *coordination & memory*.

- **"Agent Infrastructure Boom"** — Product Hunt Weekly (2026-05-07): over half the top-20
  products were AI-agent infra: VMs, observability, and notably **"shared context boards."**
  <https://www.shareuhack.com/en/posts/product-hunt-weekly-2026-05-07>
- **PR volume up 29% YoY from AI-assisted coding**, and "manual review can't keep pace…
  teams need agents that understand *system impact*, not just diffs." — Qodo, Feb 2026.
  <https://www.qodo.ai/blog/github-ai-code-review/>
- **GitHub Copilot coding agent** now ships self-review, custom agents, and CLI handoff —
  i.e. *multiple* autonomous agents operating on one repo is now the default, not exotic.
  <https://github.blog/ai-and-ml/github-copilot/whats-new-with-github-copilot-coding-agent/>
- **"git blame is missing the most important information"** (nat.io) — the field agrees the
  missing layer is *rationale*, not authorship. We attack the rationale layer, but at
  **decision-time going forward** instead of archaeology after the fact.
  <https://nat.io/blog/git-blame-is-missing-the-most-important-information>
- **Terminal Renaissance** — fast local TUIs/CLIs are where the energy is in 2026.
  <https://1337skills.com/blog/2026-03-09-terminal-renaissance-modern-tui-tools-reshaping-developer-workflows/>

## 3. Why it's different

| Tool / approach | What it answers | What it misses |
|---|---|---|
| `git blame` / Phy Blame Archaeologist | *Who/when* (and lately *why*) a line got there | Backward-looking; nothing about what was **tried and rejected** |
| diff2ai / difit / CodeRabbit | "Is this diff good?" | Per-PR; no cross-session memory of dead-ends |
| Agent observability platforms | Traces/metrics for prod agent fleets | Heavyweight, cloud, ops-focused; not a per-repo dev artifact |
| yak-tracker (our own) | Reconstructs *your* day after the fact | Personal, retrospective, single-author |
| **ship-log** | "What have we already tried here, what did we decide, and what's a known dead-end?" | — |

The fresh angle: a **decision is a first-class, append-only artifact committed with the code**,
written *at the moment of choosing* by whoever (or whatever) is choosing — designed so an
**agent can both write and read it** via a stable CLI + JSON. Think "ADRs, but lightweight,
line-anchored, agent-native, and impossible to forget because the agent's workflow reads it first."

## 4. MVP scope (v0.1)

The smallest useful thing:

- `shiplog init` — create `.shiplog/log.jsonl` + a `.shiplog/config.toml`.
- `shiplog add` — append an entry. Fields: `type` (decision|attempt|deadend|note),
  `summary` (one line), optional `--why`, `--files`, `--tags`, `--ref` (issue/PR).
  Auto-captures author, timestamp, current branch, and short HEAD sha.
- `shiplog ls` — pretty, skimmable table of recent entries (newest first), filterable by
  `--type`, `--tag`, `--file`, `--since`.
- `shiplog show <id>` — full detail for one entry.
- `shiplog brief` — print a compact, token-efficient digest (markdown) of decisions &
  dead-ends for the current repo/branch — the thing an agent pastes into context **before** working.
- `--json` on every read command, so agents parse instead of scrape.
- Entries are plain JSONL (one entry per line) → trivially diffable, merge-friendly-ish,
  and greppable without the tool.

## 5. Tech stack

Boring, fast, single-binary-friendly.

- **Language:** Python 3.11 + [Typer](https://typer.tiangolo.com/) for the CLI (fast to write,
  great help output, easy subcommands) and **Rich** for the pretty `ls`/`show`. Pipx-installable.
  - *Why not Rust/Go?* v0.1 in hours > shaving 20ms. Storage is plain JSONL so a future Go/Rust
    rewrite can read the exact same files. Personality + speed-of-iteration win here.
- **Storage:** plain **JSONL** (`.shiplog/log.jsonl`), append-only. No DB, no daemon.
- **Config:** TOML (`tomllib`, stdlib in 3.11).
- **IDs:** short, sortable — `ULID`-style or `<yymmdd>-<6char>`.
- **Packaging:** `pyproject.toml`, console_scripts entrypoint `shiplog`.
- **Tests:** `pytest`. **Lint:** `ruff`.

## 6. Architecture

```
shiplog/
  __init__.py
  cli.py          # Typer app: init/add/ls/show/brief wiring
  store.py        # read/append JSONL, locking, id generation
  models.py       # Entry dataclass + (de)serialization, schema version
  gitctx.py       # branch, short sha, repo root, author from git config
  render.py       # Rich tables (ls/show) + markdown digest (brief)
  config.py       # load/merge .shiplog/config.toml + defaults
```

Key ideas:
- **Append-only** writes (open in `a` mode, one JSON object per line) keep merges sane and
  history honest — you don't edit the past, you log a correction.
- **`brief` is the headline feature**: it ranks/filters entries (dead-ends + recent decisions
  for touched files) into a <40-line digest tuned to drop straight into an agent prompt.
- Everything readable is also `--json` for programmatic/agent use.

## 7. Milestones

1. **M1 — Scaffold + hello-world.** `pyproject.toml`, package skeleton, `shiplog --version`
   and `shiplog hello` working via pipx/editable install. CI runs `ruff` + `pytest` (1 dummy test).
2. **M2 — Store + models.** `Entry` dataclass, JSONL append/read in `store.py`, id generation,
   schema version field. Unit tests for round-trip + concurrent append.
3. **M3 — `init` + `add`.** Create `.shiplog/`, write config, append real entries with
   git-captured author/branch/sha. Validation + friendly errors.
4. **M4 — `ls` + `show`.** Rich table, filters (`--type/--tag/--file/--since`), `show <id>`
   detail, and `--json` on both.
5. **M5 — `brief`.** Token-efficient markdown digest: dead-ends first, then recent decisions
   for files in the working tree / a `--files` set. `--json` variant too.
6. **M6 — Polish + agent ergonomics.** README quickstart, `AGENT.md` snippet teaching agents
   to `shiplog brief` before work and `shiplog add` after, demo gif/asciinema, publishable to PyPI (TestPyPI first).

## 8. Backlog / future features (v0.2+)

1. **`shiplog blame <file>:<line>`** — nearest logged decision/dead-end anchored to a line range.
2. **Git hook installer** — `prepare-commit-msg` nudge: "log a decision for this change? (y/N)".
3. **MCP server mode** — expose `add`/`ls`/`brief` as MCP tools so agents call ship-log natively.
4. **`shiplog link`** — attach a commit sha / PR URL to an existing entry after the fact.
5. **Conflict-free merges** — dedupe + stable sort so two branches' logs union cleanly.
6. **`shiplog tui`** — full-screen browser (Textual) for scrolling the log with live filter.
7. **AI summarizer** — `shiplog brief --smart` clusters entries via a local LLM (Ollama) into themes.
8. **Per-file watch** — warn (pre-commit) when you edit a file with an open dead-end touching it.
9. **`shiplog export`** — render the log to a CHANGELOG/ADR markdown set for release notes.
10. **Web viewer** — static HTML export of the log for a repo's GitHub Pages.
11. **Stats** — `shiplog stats`: decisions/week, top churned files, dead-end ratio.
12. **Templates** — `shiplog add --template adr|incident|spike` for structured entry types.

## 9. Out of scope

- No server, no SaaS, no account, no telemetry. The log lives in the repo. Full stop.
- Not a code reviewer, linter, or test runner. We log *intent*, not quality.
- Not `git blame` and not trying to replace it — complementary.
- No fleet/prod observability dashboards. This is a per-repo dev artifact, not an ops platform.
- No automatic decision *inference* in v0.1 (you log on purpose; smart inference is a v0.2 stretch).
