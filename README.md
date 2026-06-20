# ship-log 🧭⚓

**A git-native captain's log for the multi-agent era.**

When several AI coding agents (and you) churn the same repo, everyone keeps re-trying the
same already-failed ideas. `ship-log` is a tiny, append-only, plain-text ledger that lives
*inside your repo* (`.shiplog/`). Every **decision**, **attempt**, and **dead-end** gets one
line and a "why." The next agent reads the log before touching code — and skips the graveyard.

> Not `git blame` (who/when). Not a diff reviewer (is-this-good).
> It's the *forward-looking memory* of what's already been tried here.

## Why

- AI-assisted PR volume is up ~29% YoY — the bottleneck is now **coordination & memory**, not typing.
- `git blame` tells you *who* changed a line, never *what was tried and rejected*.
- Agents have no shared, durable, per-repo memory. Now they do — and it's just a file.

## Install (dev / M1)

Not on PyPI yet. Run it from a clone:

```bash
git clone https://github.com/rwrife/ship-log
cd ship-log

# pipx (recommended): isolated, on your PATH
pipx install --editable .

# …or a plain venv
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

shiplog --version              # -> shiplog 0.1.0
shiplog hello                  # friendly banner; proof the install works
```

## Quickstart

`init` + `add` work today (M3). `ls`/`show`/`brief` land in M4–M5.

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

```bash
# coming next (M4–M5):
shiplog ls --type deadend      # skim what NOT to redo
shiplog brief                  # token-efficient digest to paste into an agent's context
shiplog brief --json           # same, machine-readable
```

## Status

🚧 v0.1 in progress. See [`PLAN.md`](./PLAN.md) for scope, architecture, and milestones (M1–M6).

- **M1** — package scaffold, `shiplog --version` / `shiplog hello`. ✅
- **M2** — append-only JSONL store backbone (`shiplog/models.py` + `shiplog/store.py`): `Entry`
  model, JSONL (de)serialization, sortable ids, file-locked concurrent append + read. ✅
- **M3** — `shiplog init` (creates `.shiplog/` + `config.toml`, idempotent) and `shiplog add`
  (git-stamped append with validation + friendly errors), via `shiplog/gitctx.py` +
  `shiplog/config.py`. ✅
  *(`ls`/`show`/`brief` that read it back land in M4–M5.)*

## For agents

The whole point: an agent should run `shiplog brief` **before** editing and `shiplog add`
**after** making a decision. A drop-in `AGENT.md` snippet lands in M6.

## License

MIT (see `LICENSE`, added in M1).

---

Part of the `auto-tool-lab` experiment. Built small on purpose.
