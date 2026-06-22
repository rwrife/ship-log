"""Git context capture for ship-log.

``add`` stamps every entry with *who/where* it was written: the repo root (so the
log lands in the right ``.shiplog/``), the current branch, the short HEAD sha, and
the author from ``git config``. All of it is best-effort — shiplog still works in a
fresh repo with no commits, or even outside git entirely, by degrading to sensible
empty/fallback values instead of raising.

We shell out to ``git`` rather than depend on a library: git is already required to
have a meaningful repo context, and the porcelain we use (``rev-parse``,
``config``) is stable and fast.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _run_git(*args: str, cwd: str | os.PathLike[str] | None = None) -> str | None:
    """Run ``git <args>`` and return stripped stdout, or ``None`` on failure.

    Returns ``None`` (never raises) when git is missing, the command exits
    non-zero (e.g. no commits yet for ``rev-parse HEAD``), or output is empty.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=os.fspath(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):  # git not installed / bad cwd
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out or None


@lru_cache(maxsize=128)
def find_repo_root(start: str | os.PathLike[str] | None = None) -> Path | None:
    """Return the repository root for ``start`` (default: cwd), or ``None``.

    Uses ``git rev-parse --show-toplevel`` so worktrees and subdirectories all
    resolve to the right root. ``None`` means "not inside a git repo".
    """
    start = Path(start) if start is not None else Path.cwd()
    top = _run_git("rev-parse", "--show-toplevel", cwd=start)
    return Path(top) if top else None


def current_branch(cwd: str | os.PathLike[str] | None = None) -> str:
    """Return the current branch name, or ``""`` if undeterminable.

    On a detached HEAD ``git`` yields ``HEAD``; we normalize that to empty so the
    field reads cleanly rather than implying a branch named "HEAD".
    """
    branch = _run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)
    if not branch or branch == "HEAD":
        return ""
    return branch


def short_sha(cwd: str | os.PathLike[str] | None = None) -> str:
    """Return the short HEAD sha, or ``""`` for a repo with no commits yet."""
    return _run_git("rev-parse", "--short", "HEAD", cwd=cwd) or ""


def working_tree_files(cwd: str | os.PathLike[str] | None = None) -> list[str]:
    """Return repo-relative paths present in the working tree (best effort).

    Used by ``brief`` to prioritize log entries touching what you're working on.
    Combines tracked files (``git ls-files``) with anything added/modified/untracked
    per ``git status --porcelain`` so a brand-new file you're editing still counts.
    Paths are repo-relative with forward slashes (git's native form), de-duplicated
    and order-stable. Returns ``[]`` outside a repo or on any git failure.

    Note: ``git status --porcelain`` may quote/rename paths (``R  old -> new``);
    we keep this intentionally simple and skip rename arrows, which is fine since
    the result only *ranks* entries — it never gates correctness.
    """
    seen: list[str] = []

    def _add(path: str) -> None:
        p = path.strip().strip('"')
        if p and p not in seen:
            seen.append(p)

    tracked = _run_git("ls-files", cwd=cwd)
    if tracked:
        for line in tracked.splitlines():
            _add(line)

    status = _run_git("status", "--porcelain", cwd=cwd)
    if status:
        for line in status.splitlines():
            # Format: "XY <path>" (cols 0-1 = status, path from col 3). Renames
            # show "old -> new"; take the right-hand (current) side.
            entry = line[3:] if len(line) > 3 else line
            if " -> " in entry:
                entry = entry.split(" -> ", 1)[1]
            _add(entry)

    return seen


def git_author(cwd: str | os.PathLike[str] | None = None) -> str:
    """Return the configured author as ``"Name <email>"`` (best effort).

    Falls back gracefully: name+email → just name → just email → the ``$USER``
    environment variable → ``""``. This keeps entries attributable without ever
    blocking a write on incomplete git config.
    """
    name = _run_git("config", "user.name", cwd=cwd)
    email = _run_git("config", "user.email", cwd=cwd)
    if name and email:
        return f"{name} <{email}>"
    if name:
        return name
    if email:
        return email
    return os.environ.get("USER") or os.environ.get("USERNAME") or ""


@dataclass(slots=True)
class GitContext:
    """Captured git facts for stamping onto an :class:`~shiplog.models.Entry`.

    Attributes:
        repo_root: Path to the repository root, or ``None`` outside a repo.
        branch: Current branch (empty on detached HEAD / no repo).
        sha: Short HEAD sha (empty when there are no commits).
        author: ``"Name <email>"`` best-effort author string.
    """

    repo_root: Path | None
    branch: str
    sha: str
    author: str

    @classmethod
    def capture(cls, cwd: str | os.PathLike[str] | None = None) -> GitContext:
        """Capture all git context for ``cwd`` (default: current directory)."""
        root = find_repo_root(cwd)
        # Anchor branch/sha/author lookups at the repo root when we have one so a
        # subdirectory cwd still reports the repo's branch/sha.
        anchor = root if root is not None else cwd
        return cls(
            repo_root=root,
            branch=current_branch(anchor),
            sha=short_sha(anchor),
            author=git_author(anchor),
        )
