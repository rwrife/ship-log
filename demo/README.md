# Demo

A self-contained walkthrough of the `brief`-in / `add`-out workflow.

## Run it

```bash
pipx install ship-log        # or: pipx install --editable .  (from a clone)
./demo/demo.sh               # spins up a throwaway git repo, no cleanup needed
```

`demo.sh` creates a temporary repo, logs a decision + a dead-end + a note, then shows
`shiplog ls` and `shiplog brief` — i.e. exactly what the next agent sees before editing.
It removes its scratch directory on exit and never touches this repo.

## Record a cast / gif

```bash
asciinema rec demo/shiplog.cast -c demo/demo.sh --overwrite
agg demo/shiplog.cast demo/shiplog.gif        # optional: cast -> gif (agg)
```

Tune the pacing with `DEMO_PAUSE` (seconds between steps, default `1.1`):

```bash
DEMO_PAUSE=0.6 asciinema rec demo/shiplog.cast -c demo/demo.sh --overwrite
```

> The committed `shiplog.cast` / `shiplog.gif` (if present) are generated artifacts —
> regenerate them with the commands above rather than editing by hand.
