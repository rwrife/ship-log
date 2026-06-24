#!/usr/bin/env bash
# demo/demo.sh — a self-contained ship-log walkthrough in a throwaway repo.
#
# Run it directly to see the flow, or record it as an asciinema cast:
#
#   asciinema rec demo/shiplog.cast -c demo/demo.sh --overwrite
#   # then publish/convert:  agg demo/shiplog.cast demo/shiplog.gif
#
# Requires `shiplog` on PATH (pipx install ship-log  — or  pipx install --editable .).
set -euo pipefail

# Slow things down a touch so a recording is readable.
pause() { sleep "${DEMO_PAUSE:-1.1}"; }
run() { printf '\033[1;36m$ %s\033[0m\n' "$*"; eval "$*"; pause; }
# `clear` only when we have a usable terminal; no-op when piped/recorded headless.
cls() { if [ -t 1 ] && [ -n "${TERM:-}" ]; then clear; else echo; fi; }

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT
cd "$workdir"

git init -q
git config user.name "Demo Captain"
git config user.email "captain@example.com"
mkdir -p src
printf 'def store(): ...\n' > src/store.py
git add -A && git commit -qm "init demo repo"

cls
echo "# ship-log — a git-native captain's log for the multi-agent era"
echo "# (running in a throwaway repo: $workdir)"
pause

run "shiplog init"
run "shiplog add decision 'Use JSONL not SQLite for the store' --why 'merge-friendly + greppable' --files src/store.py --tags storage"
run "shiplog add deadend 'Threaded the append path; lock contention made it slower' --why 'GIL + fsync per write' --files src/store.py"
run "shiplog add note 'Config lives in .shiplog/config.toml'"

echo
echo "# --- the next agent shows up. it reads the log BEFORE editing: ---"
pause
run "shiplog ls"
run "shiplog brief --files src/store.py"

echo
echo "# dead-ends lead the digest, so the next agent skips the graveyard. ⚓"
pause
