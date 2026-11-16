"""Enforcing ``pre-commit`` dead-end tripwire for ship-log.

Where :mod:`shiplog.hooks` only *nudges* (a commented reminder that never blocks),
``guard`` is the opt-in **seatbelt**: a ``pre-commit`` hook that scans the staged
diff, finds any open ``deadend`` entry whose ``--files`` overlap the files you're
about to commit, and **exits non-zero** — stopping the commit before you re-drive
into a known wall. Warnings get ignored; a failed commit does not.

An agent (or you, tomorrow) can clear a specific block two ways:

- **Acknowledge** it: ``shiplog guard --ack <id>`` appends an ``ack`` entry that
  points back at the dead-end (append-only — the dead-end line is never mutated).
  A dead-end with a matching ``ack`` no longer blocks.
- **Override** the whole gate for one commit: ``SHIPLOG_GUARD=off git commit`` (or
  git's own ``--no-verify``, which skips all pre-commit hooks).

Design mirrors :mod:`shiplog.hooks`:

- Reuse :class:`shiplog.hooks.HookPaths` for worktree-/``core.hooksPath``-aware
  installation; install our fenced stub under ``pre-commit``.
- The stub shells out to ``shiplog guard _check`` so all real logic lives here
  (testable) rather than in shell.
- Install is idempotent and safe: never clobber a foreign ``pre-commit`` hook
  unless ``--force``; uninstall strips exactly our fenced block.
- Unlike the nudge stub, this stub *propagates* the checker's exit code so a hit
  actually blocks the commit — but ``SHIPLOG_GUARD=off`` short-circuits to 0, and
  a missing ``shiplog`` on PATH degrades to "allow" (never wedge a repo).
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .gitctx import _run_git
from .hooks import HookPaths as _NudgeHookPaths
from .models import Entry, EntryType
from .resolutions import resolved_ids

HOOK_NAME = "pre-commit"

# Fence markers (distinct from the nudge hook's) so install/uninstall touch only
# our block even if the user maintains their own pre-commit content alongside it.
MARKER_BEGIN = "# >>> shiplog guard pre-commit >>>"
MARKER_END = "# <<< shiplog guard pre-commit <<<"
HOOK_VERSION = "1"

# Env escape hatch: any of these (case-insensitive) fully disables the gate for a
# single commit without uninstalling. ``--no-verify`` (git's own) also skips it.
_OFF_VALUES = frozenset({"off", "0", "false", "no", "skip"})
ENV_VAR = "SHIPLOG_GUARD"


def guard_disabled_via_env(environ: dict[str, str] | None = None) -> bool:
    """True if ``SHIPLOG_GUARD`` is set to an "off" value in ``environ``."""
    env = environ if environ is not None else os.environ
    val = env.get(ENV_VAR, "").strip().lower()
    return val in _OFF_VALUES


# -- staged files + file matching ---------------------------------------------


def staged_files(repo_root: str | os.PathLike[str]) -> list[str]:
    """Return repo-relative paths staged for the pending commit (best effort).

    Uses ``git diff --cached --name-only`` — exactly what's about to be committed.
    Returns ``[]`` on any git failure so a broken lookup degrades to "nothing to
    block" rather than raising inside a hook.
    """
    out = _run_git("diff", "--cached", "--name-only", cwd=repo_root)
    if not out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _path_part(spec: str) -> str:
    """Strip a trailing ``:line`` / ``:start-end`` anchor from a --files entry.

    Dead-ends may pin a line range (``db.py:40-80``); overlap is decided at the
    *path* granularity, so we compare only the path portion. A bare Windows-style
    drive letter is never in play here (paths are git-relative, forward-slashed).
    """
    p = spec.strip()
    if not p:
        return ""
    # Only treat a trailing ``:<digits...>`` as a line anchor; keep other colons.
    head, sep, tail = p.rpartition(":")
    if sep and head and tail and tail[0].isdigit():
        return head
    return p


def paths_overlap(deadend_files: list[str], touched: set[str]) -> list[str]:
    """Return the touched paths that overlap a dead-end's ``files`` list.

    Matching is exact on the git-relative path (after stripping any ``:line``
    anchor). A dead-end with no ``files`` recorded is repo-wide by nature and does
    **not** block (too blunt to be useful as a gate); such entries are filtered by
    the caller before reaching here.
    """
    wanted = {_path_part(f) for f in deadend_files if _path_part(f)}
    if not wanted:
        return []
    return sorted(touched & wanted)


# -- dead-end / ack resolution ------------------------------------------------


@dataclass(slots=True)
class Block:
    """One dead-end blocking the commit.

    Attributes:
        entry: The offending ``deadend`` entry.
        files: The staged paths that overlap it (sorted, non-empty).
    """

    entry: Entry
    files: list[str]

    def to_dict(self) -> dict[str, object]:
        """JSON-ready view for ``--json`` agent parsing."""
        return {
            "id": self.entry.id,
            "summary": self.entry.summary,
            "why": self.entry.why,
            "files": list(self.files),
            "ts": self.entry.ts,
        }


def acknowledged_ids(entries: list[Entry]) -> set[str]:
    """Return the set of dead-end ids that carry an ``ack`` entry.

    An ``ack`` is an append-only :class:`Entry` of type ``ack`` whose
    ``link_target`` names the dead-end it clears. Once present, that dead-end no
    longer blocks future commits.
    """
    return {
        e.link_target
        for e in entries
        if e.type == EntryType.ACK and e.link_target
    }


def blocking_deadends(
    entries: list[Entry],
    touched: set[str],
    *,
    include_resolved: bool = False,
) -> list[Block]:
    """Compute the dead-ends that should block a commit touching ``touched``.

    A dead-end blocks when it (a) has at least one recorded file, (b) overlaps a
    staged path, (c) has **not** been acknowledged, and (d) has **not** been
    resolved (unless ``include_resolved`` re-surfaces resolved ones). Results are
    newest-first (most recent dead-end shown first) for a stable ordering.
    """
    acked = acknowledged_ids(entries)
    resolved = set() if include_resolved else resolved_ids(entries)
    blocks: list[Block] = []
    for e in entries:
        if e.type != EntryType.DEADEND:
            continue
        if e.id in acked:
            continue
        if e.id in resolved:
            continue
        overlap = paths_overlap(e.files, touched)
        if overlap:
            blocks.append(Block(entry=e, files=overlap))
    # Newest-first by timestamp (ids are day-granular; ts breaks ties precisely).
    blocks.sort(key=lambda b: b.entry.ts, reverse=True)
    return blocks


# -- pre-commit hook install / uninstall / status -----------------------------


def _resolve_paths(repo_root: str | os.PathLike[str]) -> _NudgeHookPaths | None:
    """Resolve the ``pre-commit`` hook path (worktree/hooksPath aware).

    Reuses :class:`shiplog.hooks.HookPaths` resolution logic but retargets the
    filename to ``pre-commit`` (its ``HOOK_NAME`` is ``prepare-commit-msg``).
    """
    base = _NudgeHookPaths.resolve(repo_root)
    if base is None:
        return None
    return _NudgeHookPaths(hooks_dir=base.hooks_dir, hook_file=base.hooks_dir / HOOK_NAME)


def hook_stub() -> str:
    """Return the fenced POSIX-sh guard block ship-log installs.

    Unlike the nudge stub, this **propagates** the checker's exit code so a hit
    blocks the commit. ``SHIPLOG_GUARD=off`` short-circuits to allow, and a
    missing ``shiplog`` on PATH degrades to allow (never wedge a repo).
    """
    return (
        f"{MARKER_BEGIN}\n"
        f"# version: {HOOK_VERSION}\n"
        "#\n"
        "# ship-log guard: blocks a commit that re-touches a known open dead-end.\n"
        "# Override once with SHIPLOG_GUARD=off (or git commit --no-verify).\n"
        "# Ack a specific one with: shiplog guard --ack <id>\n"
        "# Remove entirely with: shiplog guard uninstall\n"
        "#\n"
        'case "$(printf %s \"${SHIPLOG_GUARD:-}\" | tr \"[:upper:]\" \"[:lower:]\")" in\n'
        "  off|0|false|no|skip) exit 0 ;;\n"
        "esac\n"
        "if command -v shiplog >/dev/null 2>&1; then\n"
        "  shiplog guard _check || exit $?\n"
        "fi\n"
        f"{MARKER_END}\n"
    )


def _shebang_wrapped_stub() -> str:
    """The stub as a standalone hook file (shebang + our fenced block)."""
    return "#!/bin/sh\n" + hook_stub()


def is_ours(text: str) -> bool:
    """True if ``text`` contains ship-log's guard block (by marker)."""
    return MARKER_BEGIN in text and MARKER_END in text


def _make_executable(path: Path) -> None:
    """Add the execute bits git needs to run a hook."""
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@dataclass(slots=True)
class InstallResult:
    """Outcome of :func:`install` (``created`` / ``updated`` / ``unchanged``)."""

    action: str
    hook_file: Path


def install(repo_root: str | os.PathLike[str], *, force: bool = False) -> InstallResult:
    """Install (or refresh) the ``pre-commit`` guard hook.

    Idempotent and safe, mirroring :func:`shiplog.hooks.install`:

    - No hook present → write our fenced stub (with shebang) and ``chmod +x``.
    - Our block already present in the file → append/rewrite only if content
      changed, else ``"unchanged"``. If a *foreign* ``pre-commit`` also exists we
      keep it and merge our block after it.
    - A foreign hook with no ship-log block → refuse with
      :class:`FileExistsError` unless ``force=True`` (then our block is appended,
      preserving their content).

    Raises:
        RuntimeError: if ``repo_root`` is not inside a git repository.
        FileExistsError: if a non-ship-log hook exists and ``force`` is False.
    """
    paths = _resolve_paths(repo_root)
    if paths is None:
        raise RuntimeError("not inside a git repository")

    paths.hooks_dir.mkdir(parents=True, exist_ok=True)
    block = hook_stub()

    if not paths.hook_file.exists():
        paths.hook_file.write_text(_shebang_wrapped_stub(), encoding="utf-8")
        _make_executable(paths.hook_file)
        return InstallResult(action="created", hook_file=paths.hook_file)

    current = paths.hook_file.read_text(encoding="utf-8")

    if is_ours(current):
        desired = _replace_block(current, block)
        if desired == current:
            _make_executable(paths.hook_file)
            return InstallResult(action="unchanged", hook_file=paths.hook_file)
        paths.hook_file.write_text(desired, encoding="utf-8")
        _make_executable(paths.hook_file)
        return InstallResult(action="updated", hook_file=paths.hook_file)

    # Foreign pre-commit hook with no ship-log block.
    if not force:
        raise FileExistsError(
            f"a non-ship-log {HOOK_NAME} hook already exists at "
            f"{paths.hook_file}. Re-run with --force to append the guard block "
            "(your existing hook is kept and runs first)."
        )
    merged = current.rstrip("\n") + "\n\n" + block
    paths.hook_file.write_text(merged, encoding="utf-8")
    _make_executable(paths.hook_file)
    return InstallResult(action="updated", hook_file=paths.hook_file)


def _replace_block(text: str, block: str) -> str:
    """Return ``text`` with our fenced block replaced by ``block`` (in place)."""
    begin = text.find(MARKER_BEGIN)
    end = text.find(MARKER_END, begin)
    if begin == -1 or end == -1:
        return text
    end += len(MARKER_END)
    if end < len(text) and text[end] == "\n":
        end += 1
    block_text = block if block.endswith("\n") else block + "\n"
    return text[:begin] + block_text + text[end:]


@dataclass(slots=True)
class UninstallResult:
    """Outcome of :func:`uninstall` (``removed`` / ``stripped`` / ``absent``)."""

    action: str
    hook_file: Path


def _strip_block(text: str) -> str:
    """Remove ship-log's guard block (inclusive) from ``text``."""
    begin = text.find(MARKER_BEGIN)
    if begin == -1:
        return text
    end = text.find(MARKER_END, begin)
    if end == -1:
        return text[:begin].rstrip("\n") + "\n"
    end += len(MARKER_END)
    if end < len(text) and text[end] == "\n":
        end += 1
    return text[:begin] + text[end:]


def uninstall(repo_root: str | os.PathLike[str]) -> UninstallResult:
    """Remove the guard block (surgical + reversible).

    - File is purely ours (shebang + our block) → delete the file.
    - File mixes our block with other content → strip only our block.
    - No ship-log block present → ``"absent"``; touch nothing.

    Raises:
        RuntimeError: if ``repo_root`` is not inside a git repository.
    """
    paths = _resolve_paths(repo_root)
    if paths is None:
        raise RuntimeError("not inside a git repository")

    if not paths.hook_file.exists():
        return UninstallResult(action="absent", hook_file=paths.hook_file)

    text = paths.hook_file.read_text(encoding="utf-8")
    if not is_ours(text):
        return UninstallResult(action="absent", hook_file=paths.hook_file)

    stripped = _strip_block(text)
    residue = "\n".join(
        ln for ln in stripped.splitlines() if ln.strip() and not ln.startswith("#!")
    ).strip()
    if not residue:
        paths.hook_file.unlink()
        return UninstallResult(action="removed", hook_file=paths.hook_file)

    paths.hook_file.write_text(stripped, encoding="utf-8")
    return UninstallResult(action="stripped", hook_file=paths.hook_file)


def status(repo_root: str | os.PathLike[str]) -> bool:
    """Return True iff the guard hook is currently installed in this repo."""
    paths = _resolve_paths(repo_root)
    if paths is None or not paths.hook_file.exists():
        return False
    return is_ours(paths.hook_file.read_text(encoding="utf-8"))
