"""Git ``prepare-commit-msg`` hook installer for ship-log.

The point: keep the log fresh *without discipline*. A ``prepare-commit-msg`` hook
spots "interesting" commits (touching several files, or whose message smells like a
real decision) and nudges you to log one — but it never, ever blocks the commit.

Why ``prepare-commit-msg`` and why a *commented* nudge rather than a ``y/N`` prompt?
During ``git commit`` the hook's stdin is **not** the terminal (git wires it for its
own use), so a classic ``read -p "log a decision? (y/N)"`` can't reliably talk to the
user and risks hanging a commit. So instead of prompting, we *inject a few commented
lines* into the commit-message template. Git strips comment lines (those starting with
``core.commentChar``, default ``#``) before committing, so the nudge:

- shows up in the editor right where you're already typing the message,
- disappears from the actual commit (zero pollution),
- and is impossible to "fail" — worst case it's invisible.

For non-interactive commits (``-m``, merges, squashes, amends, templates) we stay out
of the way entirely: there's no editor to nudge into, and we never want to touch a
message the user already finalized.

Design:
- The installed hook is a tiny POSIX-sh stub that shells out to ``shiplog hook _nudge``
  so all the real logic lives here in Python (testable), not in shell.
- Install is idempotent and *safe*: we never clobber a pre-existing, foreign
  ``prepare-commit-msg`` hook unless ``--force`` is given. Our own hook is fenced with
  marker comments so uninstall removes exactly what we wrote.
- The stub always ``exit 0`` — a broken or missing ``shiplog`` can never block a commit.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .gitctx import _run_git

HOOK_NAME = "prepare-commit-msg"

# Fence markers so we can detect/remove exactly our block and recognize our hook
# even if a user has wrapped it. The version lets a future install refresh an old
# stub in place.
MARKER_BEGIN = "# >>> shiplog prepare-commit-msg >>>"
MARKER_END = "# <<< shiplog prepare-commit-msg <<<"
HOOK_VERSION = "1"

# Default: a commit touching at least this many files is "interesting" enough to
# nudge. Overridable per-repo via config (``hook_file_threshold``).
DEFAULT_FILE_THRESHOLD = 3

# Commit-message smells that suggest a real decision worth logging, regardless of
# file count. Matched case-insensitively against the subject line.
DECISION_PATTERNS: tuple[str, ...] = (
    r"\brefactor",
    r"\brewrite",
    r"\bredesign",
    r"\bmigrat",  # migrate/migration
    r"\bswitch(ed|ing)?\s+to\b",
    r"\bdrop(ped|ping)?\b",
    r"\bremove(d)?\b",
    r"\bdeprecat",
    r"\bbreaking\b",
    r"\brevert",
    r"\bworkaround\b",
    r"\bhack\b",
    r"\barchitect",
)

_COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in DECISION_PATTERNS]

# Commit "sources" git passes as the 2nd arg to prepare-commit-msg. When the
# message is already supplied (``-m``/``-F``), a merge/squash is in progress, or a
# commit is being reworded from an existing one, there's no editor to nudge into —
# so we skip these outright.
_NONINTERACTIVE_SOURCES = frozenset({"message", "merge", "squash", "commit"})


@dataclass(slots=True)
class HookPaths:
    """Resolved locations for hook installation in a repo.

    Attributes:
        hooks_dir: The repo's git hooks directory (honors ``core.hooksPath`` and
            worktrees via ``git rev-parse --git-path hooks``).
        hook_file: Full path to the ``prepare-commit-msg`` file within it.
    """

    hooks_dir: Path
    hook_file: Path

    @classmethod
    def resolve(cls, repo_root: str | os.PathLike[str]) -> HookPaths | None:
        """Resolve hook paths for ``repo_root``; ``None`` outside a git repo.

        Uses ``git rev-parse --git-path hooks`` so the right directory is found
        even with worktrees, submodules, or a custom ``core.hooksPath``.
        """
        rel = _run_git("rev-parse", "--git-path", "hooks", cwd=repo_root)
        if rel is None:
            return None
        hooks_dir = Path(rel)
        if not hooks_dir.is_absolute():
            hooks_dir = Path(repo_root) / hooks_dir
        return cls(hooks_dir=hooks_dir, hook_file=hooks_dir / HOOK_NAME)


def hook_stub() -> str:
    """Return the POSIX-sh hook script ship-log installs.

    Fenced with markers, version-stamped, and defensive: it forwards the
    commit-message file + source to ``shiplog hook _nudge`` and *always* exits 0 so
    nothing it does can ever block a commit. If ``shiplog`` isn't on PATH the hook
    silently no-ops.
    """
    return (
        f"{MARKER_BEGIN}\n"
        f"# version: {HOOK_VERSION}\n"
        "#\n"
        "# ship-log nudge: reminds you to log a decision on interesting commits.\n"
        "# It NEVER blocks a commit and only injects *commented* lines into the\n"
        "# message template (git strips them). Remove with: shiplog hook uninstall\n"
        "#\n"
        '# Args from git: $1 = path to commit message file, $2 = source, $3 = sha.\n'
        "if command -v shiplog >/dev/null 2>&1; then\n"
        '  shiplog hook _nudge "$1" "$2" || true\n'
        "fi\n"
        "exit 0\n"
        f"{MARKER_END}\n"
    )


def _shebang_wrapped_stub() -> str:
    """The stub as a standalone hook file (shebang + our fenced block)."""
    return "#!/bin/sh\n" + hook_stub()


def is_ours(text: str) -> bool:
    """True if ``text`` contains ship-log's hook block (by marker)."""
    return MARKER_BEGIN in text and MARKER_END in text


def _make_executable(path: Path) -> None:
    """Add the user/group/other execute bits git needs to run a hook."""
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@dataclass(slots=True)
class InstallResult:
    """Outcome of :func:`install`.

    Attributes:
        action: ``"created"``, ``"updated"``, or ``"unchanged"``.
        hook_file: Path to the hook that was written.
    """

    action: str
    hook_file: Path


def install(repo_root: str | os.PathLike[str], *, force: bool = False) -> InstallResult:
    """Install (or refresh) the ``prepare-commit-msg`` nudge hook.

    Idempotent and safe:

    - No hook present → write our fenced stub (with shebang) and ``chmod +x``.
    - Our hook already present → rewrite only if the content changed
      (``"updated"``), else ``"unchanged"``.
    - A *foreign* hook present → refuse with :class:`FileExistsError` unless
      ``force=True``, in which case it's overwritten.

    Raises:
        RuntimeError: if ``repo_root`` is not inside a git repository.
        FileExistsError: if a non-ship-log hook exists and ``force`` is False.
    """
    paths = HookPaths.resolve(repo_root)
    if paths is None:
        raise RuntimeError("not inside a git repository")

    paths.hooks_dir.mkdir(parents=True, exist_ok=True)
    desired = _shebang_wrapped_stub()

    if paths.hook_file.exists():
        current = paths.hook_file.read_text(encoding="utf-8")
        if is_ours(current):
            if current == desired:
                # Still make sure it's executable (e.g. perms got stripped).
                _make_executable(paths.hook_file)
                return InstallResult(action="unchanged", hook_file=paths.hook_file)
            paths.hook_file.write_text(desired, encoding="utf-8")
            _make_executable(paths.hook_file)
            return InstallResult(action="updated", hook_file=paths.hook_file)
        if not force:
            raise FileExistsError(
                f"a non-ship-log {HOOK_NAME} hook already exists at "
                f"{paths.hook_file}. Re-run with --force to overwrite it "
                "(back it up first if you need it)."
            )
        # Forced overwrite of a foreign hook.
        paths.hook_file.write_text(desired, encoding="utf-8")
        _make_executable(paths.hook_file)
        return InstallResult(action="updated", hook_file=paths.hook_file)

    paths.hook_file.write_text(desired, encoding="utf-8")
    _make_executable(paths.hook_file)
    return InstallResult(action="created", hook_file=paths.hook_file)


@dataclass(slots=True)
class UninstallResult:
    """Outcome of :func:`uninstall`.

    Attributes:
        action: ``"removed"`` (file deleted), ``"stripped"`` (our block removed
            from a larger file that had other content), or ``"absent"`` (nothing
            of ours was there).
        hook_file: Path to the hook file considered.
    """

    action: str
    hook_file: Path


def _strip_block(text: str) -> str:
    """Remove ship-log's fenced block (inclusive) from ``text``.

    Leaves any surrounding user content intact. Tolerates a missing end marker by
    cutting to end-of-file from the begin marker.
    """
    begin = text.find(MARKER_BEGIN)
    if begin == -1:
        return text
    end = text.find(MARKER_END, begin)
    if end == -1:
        # Malformed (no end marker) — drop from begin to EOF.
        return text[:begin].rstrip("\n") + "\n"
    end += len(MARKER_END)
    # Swallow a trailing newline after the end marker so we don't leave a gap.
    if end < len(text) and text[end] == "\n":
        end += 1
    stripped = text[:begin] + text[end:]
    return stripped


def uninstall(repo_root: str | os.PathLike[str]) -> UninstallResult:
    """Remove the ship-log nudge hook (reversible, surgical).

    - File is purely ours (just shebang + our block) → delete the file.
    - File mixes our block with other content → strip only our block.
    - No ship-log block present → report ``"absent"`` and touch nothing.

    Raises:
        RuntimeError: if ``repo_root`` is not inside a git repository.
    """
    paths = HookPaths.resolve(repo_root)
    if paths is None:
        raise RuntimeError("not inside a git repository")

    if not paths.hook_file.exists():
        return UninstallResult(action="absent", hook_file=paths.hook_file)

    text = paths.hook_file.read_text(encoding="utf-8")
    if not is_ours(text):
        return UninstallResult(action="absent", hook_file=paths.hook_file)

    stripped = _strip_block(text)
    # If what remains is just a shebang (and/or whitespace), the file was wholly
    # ours — remove it entirely rather than leave a bare ``#!/bin/sh``.
    residue = "\n".join(
        ln for ln in stripped.splitlines() if ln.strip() and not ln.startswith("#!")
    ).strip()
    if not residue:
        paths.hook_file.unlink()
        return UninstallResult(action="removed", hook_file=paths.hook_file)

    paths.hook_file.write_text(stripped, encoding="utf-8")
    return UninstallResult(action="stripped", hook_file=paths.hook_file)


def status(repo_root: str | os.PathLike[str]) -> bool:
    """Return True iff a ship-log nudge hook is currently installed."""
    paths = HookPaths.resolve(repo_root)
    if paths is None or not paths.hook_file.exists():
        return False
    return is_ours(paths.hook_file.read_text(encoding="utf-8"))


# -- nudge decision logic (called by the installed hook) ----------------------


def _subject_line(message: str) -> str:
    """Return the first non-empty, non-comment line of a commit message."""
    for raw in message.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            continue
        return line
    return ""


def matches_decision_pattern(subject: str) -> bool:
    """True if ``subject`` smells like a decision worth logging."""
    return any(p.search(subject) for p in _COMPILED_PATTERNS)


def staged_file_count(repo_root: str | os.PathLike[str]) -> int:
    """Number of staged files for the pending commit (best effort, 0 on failure).

    Uses ``git diff --cached --name-only`` which reflects exactly what's about to
    be committed.
    """
    out = _run_git("diff", "--cached", "--name-only", cwd=repo_root)
    if not out:
        return 0
    return sum(1 for line in out.splitlines() if line.strip())


def is_interesting(
    repo_root: str | os.PathLike[str],
    subject: str,
    *,
    file_threshold: int = DEFAULT_FILE_THRESHOLD,
) -> bool:
    """Decide whether this pending commit deserves a nudge.

    Interesting when **either** the staged file count meets ``file_threshold``
    **or** the subject matches a decision pattern. A pattern match alone is enough
    even for a one-file change (a deliberate "switch to X" is worth a note).
    """
    if matches_decision_pattern(subject):
        return True
    return staged_file_count(repo_root) >= max(1, file_threshold)


def nudge_text(subject: str) -> str:
    """The commented nudge block injected into the commit-message template.

    Every line is a comment (``#``) so git strips it from the final message. Kept
    short and copy-pasteable, with a ready-to-run ``shiplog add`` line.
    """
    safe = subject.replace('"', "'").strip() or "<one-line decision>"
    return (
        "\n"
        "# ⚓ ship-log: this looks like a notable change. Consider logging a decision\n"
        "#    so the next agent (or you, tomorrow) knows the *why*:\n"
        f'#      shiplog add decision "{safe}" --why "..."\n'
        "#    (or: deadend / attempt / note). This reminder is stripped from the commit.\n"
        "#    Silence these: shiplog hook uninstall\n"
    )


def already_nudged(message: str) -> bool:
    """True if our nudge marker is already present in ``message`` (avoid dupes)."""
    return "⚓ ship-log:" in message


def run_nudge(
    repo_root: str | os.PathLike[str],
    msg_file: str | os.PathLike[str],
    source: str,
    *,
    file_threshold: int = DEFAULT_FILE_THRESHOLD,
) -> bool:
    """Append a nudge to ``msg_file`` when appropriate. Returns True if appended.

    No-ops (returns False) when:
    - the commit source is non-interactive (``-m``/merge/squash/reword),
    - the message file is missing/unreadable,
    - the change isn't "interesting",
    - or a nudge is already present.

    This is what the installed hook stub calls. It is intentionally
    failure-tolerant: any unexpected error degrades to "do nothing" so a commit is
    never disrupted.
    """
    if source in _NONINTERACTIVE_SOURCES:
        return False

    path = Path(msg_file)
    try:
        message = path.read_text(encoding="utf-8")
    except OSError:
        return False

    if already_nudged(message):
        return False

    subject = _subject_line(message)
    if not is_interesting(repo_root, subject, file_threshold=file_threshold):
        return False

    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(nudge_text(subject))
    except OSError:
        return False
    return True
