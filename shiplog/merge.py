"""Conflict-free log merges for ship-log (issue #31).

ship-log's whole premise is *many agents + a human on one repo*, which means many
branches each appending to ``.shiplog/log.jsonl``. Plain JSONL is "merge-friendly-
ish" — but two branches that both append lines hit a classic append-region git
conflict, and even after a hand resolution the log can end up with duplicate or
out-of-order entries. For a tool whose whole job is durable cross-session memory,
a log-corrupting merge is an existential papercut.

This module makes the log **conflict-free by construction** in two ways:

* A git *merge driver* (``shiplog install-merge-driver``) so git delegates
  ``.shiplog/log.jsonl`` conflicts to us. On a merge we take the **union** of both
  sides' entry lines, **dedupe by entry ``id``**, and emit a **stable sort** (by
  ``ts`` then ``id``) so both branches converge on byte-identical output regardless
  of merge order. No ``<<<<<<<`` markers, ever.
* A manual repair (``shiplog fix``) that runs the same dedupe + stable-sort over an
  existing log — for logs that got mangled *before* the driver was installed.
  ``--check`` is CI-friendly (non-zero when the log is out of order / has dupes);
  ``--write`` normalizes it. Neither ever mutates an entry's *content* — only its
  ordering and duplicate lines.

The normalization is the shared heart of both paths (:func:`normalize_lines`), so
the driver and ``fix`` can never disagree about what "clean" means. ``link`` records
are ordinary entries with their own unique ``id``, so they survive dedupe intact and
sort into place alongside the entries they annotate.

Design mirrors :mod:`shiplog.hooks`: idempotent install, marker-fenced
``.gitattributes`` block we can strip surgically, and a tiny stub that shells back to
``shiplog`` so all real logic lives here in testable Python.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .gitctx import _run_git
from .models import Entry
from .store import LOG_FILENAME, SHIPLOG_DIR

# The merge driver's registered name. Referenced from both ``.git/config``
# (``merge.shiplog.*``) and ``.gitattributes`` (``merge=shiplog``).
DRIVER_NAME = "shiplog"

# The pathspec we claim in .gitattributes. Anchored to the repo's .shiplog/ log.
LOG_ATTR_PATH = f"{SHIPLOG_DIR}/{LOG_FILENAME}"
ATTR_LINE = f"{LOG_ATTR_PATH} merge={DRIVER_NAME}"

# Marker fences so we can detect/strip exactly our .gitattributes block without
# touching a user's own attribute rules.
ATTR_BEGIN = "# >>> shiplog merge driver >>>"
ATTR_END = "# <<< shiplog merge driver <<<"

# The driver invocation git runs. %O/%A/%B are the ancestor/current/other blobs;
# git replaces them with temp paths. We forward them to a hidden shiplog command so
# the union logic is Python, not shell. A non-zero exit tells git the merge failed;
# our command exits 0 on a clean union (there is never a real conflict to report).
DRIVER_COMMAND = 'shiplog _merge-driver %O %A %B %P'


@dataclass(slots=True)
class NormalizeResult:
    """Outcome of normalizing a set of JSONL lines.

    Attributes:
        text: The normalized log body (deduped, stably sorted, newline-terminated;
            empty string for an empty log).
        line_count: Number of entry lines in ``text``.
        duplicates: How many duplicate-``id`` lines were collapsed away.
        reordered: True if the input order differed from the normalized order
            (i.e. the log was not already sorted).
        malformed: Count of non-blank input lines that could not be parsed as an
            entry (preserved verbatim, sorted to the end — never dropped).
    """

    text: str
    line_count: int
    duplicates: int
    reordered: bool
    malformed: int

    @property
    def is_clean(self) -> bool:
        """True if the input was already normalized (no dupes, no reordering).

        ``fix --check`` keys off this: a clean log exits 0, a dirty one exits 1.
        Malformed lines never count as "clean" — they're a corruption signal.
        """
        return self.duplicates == 0 and not self.reordered and self.malformed == 0


def _sort_key(entry: Entry) -> tuple[str, str]:
    """Stable ordering key: ``(ts, id)``.

    ``ts`` is an ISO-8601 UTC string (lexical sort == chronological); ``id`` breaks
    ties for entries written in the same second, and is itself unique, so the total
    order is fully deterministic regardless of input/merge order.
    """
    return (entry.ts, entry.id)


def _iter_nonblank(lines: list[str]) -> list[str]:
    """Return the stripped, non-blank lines from ``lines`` (drops phantom blanks)."""
    return [s for s in (ln.strip() for ln in lines) if s]


def normalize_lines(lines: list[str]) -> NormalizeResult:
    """Dedupe by id + stable-sort a list of JSONL lines into canonical form.

    This is the shared core of the merge driver and ``shiplog fix``. Given raw
    lines (from one log, or the concatenation of two sides of a merge):

    1. Parse each non-blank line into an :class:`~shiplog.models.Entry`. Lines that
       don't parse are *preserved verbatim* (never dropped — losing data would be
       worse than a messy line) and sorted to the very end.
    2. Dedupe parsed entries by ``id`` — the first occurrence of an id wins, so a
       union of two sides collapses shared history to one line each.
    3. Stable-sort the survivors by ``(ts, id)`` and re-serialize each via
       :meth:`Entry.to_json` (canonical, sorted-key output → byte-stable).

    The result is deterministic: the same set of entries always yields identical
    bytes, no matter what order they arrived in — which is exactly what makes two
    branches converge after a merge.
    """
    raw = _iter_nonblank(lines)

    seen_ids: set[str] = set()
    parsed: list[Entry] = []
    malformed: list[str] = []
    duplicates = 0

    for line in raw:
        try:
            entry = Entry.from_json(line)
        except (ValueError, json.JSONDecodeError):
            malformed.append(line)
            continue
        if entry.id in seen_ids:
            duplicates += 1
            continue
        seen_ids.add(entry.id)
        parsed.append(entry)

    ordered = sorted(parsed, key=_sort_key)
    # Canonical re-serialization (sorted keys, compact) so output is byte-stable.
    out_lines = [e.to_json() for e in ordered]
    # Malformed lines can't be ordered meaningfully; keep them, pinned to the end,
    # in their original relative order so nothing is silently lost.
    out_lines.extend(malformed)

    # "reordered" compares the *entry* lines only (malformed already trail): did the
    # canonical entry order differ from the input's entry order? Compare the parsed
    # entries' canonical forms against their input arrival order.
    input_entry_forms = [e.to_json() for e in parsed]
    canonical_entry_forms = [e.to_json() for e in ordered]
    reordered = input_entry_forms != canonical_entry_forms

    text = "".join(line + "\n" for line in out_lines)
    return NormalizeResult(
        text=text,
        line_count=len(out_lines),
        duplicates=duplicates,
        reordered=reordered,
        malformed=len(malformed),
    )


def normalize_text(text: str) -> NormalizeResult:
    """:func:`normalize_lines` convenience wrapper for a whole-file string."""
    return normalize_lines(text.splitlines())


def _read_lines(path: str | os.PathLike[str]) -> list[str]:
    """Read a file's lines, tolerating a missing file (→ empty)."""
    p = Path(path)
    if not p.exists():
        return []
    return p.read_text(encoding="utf-8").splitlines()


def run_merge_driver(
    current_path: str | os.PathLike[str],
    other_path: str | os.PathLike[str],
    ancestor_path: str | os.PathLike[str] | None = None,
) -> int:
    """Perform a union merge of two log versions, writing the result to ``current``.

    Called by the installed git merge driver with git's ``%A`` (current/ours),
    ``%B`` (other/theirs), and ``%O`` (ancestor) temp files. We take the **union**
    of ours + theirs (the ancestor is implicitly included via both sides and needs
    no special handling for an append-only log), normalize it (dedupe by id, stable
    sort), and overwrite ``%A`` with the canonical bytes git will keep.

    Returns 0 always: an append-only union has no true conflict to report, so the
    merge is always resolvable. (The ancestor is read for completeness/robustness
    but doesn't change the union result — appends only ever *add* lines.)
    """
    lines = _read_lines(current_path) + _read_lines(other_path)
    if ancestor_path is not None:
        # Included for completeness; union+dedupe makes this a no-op for pure
        # appends, but it keeps us correct if a side ever rewrote history.
        lines = _read_lines(ancestor_path) + lines
    result = normalize_lines(lines)
    Path(current_path).write_text(result.text, encoding="utf-8")
    return 0


# -- .gitattributes + git config installer ------------------------------------


@dataclass(slots=True)
class GitAttributesPaths:
    """Resolved ``.gitattributes`` location for a repo.

    Attributes:
        repo_root: The repository root.
        attributes_file: ``<repo_root>/.gitattributes`` (repo-level, committed so
            collaborators inherit the rule; the driver itself is configured
            per-clone in ``.git/config``).
    """

    repo_root: Path
    attributes_file: Path

    @classmethod
    def resolve(cls, repo_root: str | os.PathLike[str]) -> GitAttributesPaths:
        """Resolve the repo-root ``.gitattributes`` path for ``repo_root``."""
        root = Path(repo_root)
        return cls(repo_root=root, attributes_file=root / ".gitattributes")


def _attr_block() -> str:
    """The fenced ``.gitattributes`` block ship-log manages (with markers)."""
    return (
        f"{ATTR_BEGIN}\n"
        "# Route .shiplog/log.jsonl merges through the shiplog union driver so\n"
        "# concurrent appends never conflict. Configure the driver per-clone with:\n"
        "#   shiplog install-merge-driver\n"
        f"{ATTR_LINE}\n"
        f"{ATTR_END}\n"
    )


def attr_is_ours(text: str) -> bool:
    """True if ``text`` already contains ship-log's ``.gitattributes`` block."""
    return ATTR_BEGIN in text and ATTR_END in text


def _strip_attr_block(text: str) -> str:
    """Remove ship-log's fenced ``.gitattributes`` block (inclusive) from ``text``.

    Mirrors :func:`shiplog.hooks._strip_block`: leaves surrounding user rules
    intact and tolerates a missing end marker by cutting to EOF from the begin
    marker.
    """
    begin = text.find(ATTR_BEGIN)
    if begin == -1:
        return text
    end = text.find(ATTR_END, begin)
    if end == -1:
        return text[:begin].rstrip("\n") + "\n"
    end += len(ATTR_END)
    if end < len(text) and text[end] == "\n":
        end += 1
    return text[:begin] + text[end:]


def _git_config_driver_set(repo_root: str | os.PathLike[str]) -> bool:
    """Return True iff our merge driver is configured in this clone's git config."""
    val = _run_git("config", "--local", f"merge.{DRIVER_NAME}.driver", cwd=repo_root)
    return bool(val)


def _configure_git_driver(repo_root: str | os.PathLike[str]) -> None:
    """Register (or refresh) the merge driver in this clone's ``.git/config``.

    Sets a human ``name`` and the ``driver`` command git runs on conflict. Local
    scope: the driver command lives per-clone (never committed), while the
    ``.gitattributes`` rule that *references* it is committed for everyone.

    Raises:
        RuntimeError: if git rejects the config writes (e.g. not a repo).
    """
    name_ok = _run_git(
        "config", "--local", f"merge.{DRIVER_NAME}.name",
        "shiplog union merge driver (dedupe by id + stable sort)",
        cwd=repo_root,
    )
    driver_ok = _run_git(
        "config", "--local", f"merge.{DRIVER_NAME}.driver", DRIVER_COMMAND, cwd=repo_root
    )
    # ``git config`` (a set) prints nothing on success, so ``_run_git`` returns
    # None even when it worked. Verify by reading the value back.
    if not _git_config_driver_set(repo_root):
        raise RuntimeError(
            "failed to configure the shiplog merge driver in .git/config "
            "(is this a git repository?)."
        )
    _ = (name_ok, driver_ok)  # values unused; presence checked via read-back


def _unconfigure_git_driver(repo_root: str | os.PathLike[str]) -> None:
    """Remove the merge driver from this clone's ``.git/config`` (best effort)."""
    # ``--remove-section`` cleans up both name+driver in one shot; ignore failure
    # (section may already be absent).
    _run_git("config", "--local", "--remove-section", f"merge.{DRIVER_NAME}", cwd=repo_root)


@dataclass(slots=True)
class MergeInstallResult:
    """Outcome of :func:`install`.

    Attributes:
        attr_action: ``"created"`` / ``"updated"`` / ``"unchanged"`` for the
            ``.gitattributes`` block.
        config_action: ``"configured"`` (driver written) or ``"unchanged"`` (was
            already present) for ``.git/config``.
        attributes_file: Path to the ``.gitattributes`` file touched.
    """

    attr_action: str
    config_action: str
    attributes_file: Path


def install(repo_root: str | os.PathLike[str]) -> MergeInstallResult:
    """Install the union merge driver: ``.gitattributes`` rule + ``.git/config``.

    Idempotent and surgical, mirroring ``shiplog hook install``:

    * ``.gitattributes`` — append our fenced block if absent, refresh it if ours is
      stale, else leave it (``"unchanged"``). A foreign ``.gitattributes`` is never
      clobbered: we only ever add/replace *our* fenced block.
    * ``.git/config`` — register ``merge.shiplog.{name,driver}`` if not already set.

    Raises:
        RuntimeError: if ``repo_root`` is not inside a git repository.
    """
    if _run_git("rev-parse", "--git-dir", cwd=repo_root) is None:
        raise RuntimeError("not inside a git repository")

    paths = GitAttributesPaths.resolve(repo_root)
    desired_block = _attr_block()

    if paths.attributes_file.exists():
        current = paths.attributes_file.read_text(encoding="utf-8")
        if attr_is_ours(current):
            stripped = _strip_attr_block(current)
            rebuilt = _ensure_trailing_block(stripped, desired_block)
            if rebuilt == current:
                attr_action = "unchanged"
            else:
                paths.attributes_file.write_text(rebuilt, encoding="utf-8")
                attr_action = "updated"
        else:
            paths.attributes_file.write_text(
                _ensure_trailing_block(current, desired_block), encoding="utf-8"
            )
            attr_action = "updated"
    else:
        paths.attributes_file.write_text(desired_block, encoding="utf-8")
        attr_action = "created"

    already_configured = _git_config_driver_set(repo_root)
    _configure_git_driver(repo_root)
    config_action = "unchanged" if already_configured else "configured"

    return MergeInstallResult(
        attr_action=attr_action,
        config_action=config_action,
        attributes_file=paths.attributes_file,
    )


def _ensure_trailing_block(existing: str, block: str) -> str:
    """Append ``block`` to ``existing`` content with exactly one separating newline.

    Keeps a user's existing ``.gitattributes`` rules intact and tacks our fenced
    block on the end, normalizing whitespace so we don't accumulate blank lines on
    repeated installs.
    """
    base = existing.rstrip("\n")
    if not base:
        return block
    return base + "\n\n" + block


@dataclass(slots=True)
class MergeUninstallResult:
    """Outcome of :func:`uninstall`.

    Attributes:
        attr_action: ``"removed"`` (file deleted — it was purely ours),
            ``"stripped"`` (our block removed from a file with other rules), or
            ``"absent"`` (no ship-log block was present).
        config_action: ``"removed"`` or ``"absent"`` for the ``.git/config`` driver.
        attributes_file: Path to the ``.gitattributes`` file considered.
    """

    attr_action: str
    config_action: str
    attributes_file: Path


def uninstall(repo_root: str | os.PathLike[str]) -> MergeUninstallResult:
    """Remove the merge driver: strip the ``.gitattributes`` block + git config.

    Reversible and surgical:

    * ``.gitattributes`` purely ours → delete it; mixed with other rules → strip
      only our fenced block; no block → ``"absent"`` (touch nothing).
    * ``.git/config`` → remove the ``merge.shiplog`` section if present.

    Raises:
        RuntimeError: if ``repo_root`` is not inside a git repository.
    """
    if _run_git("rev-parse", "--git-dir", cwd=repo_root) is None:
        raise RuntimeError("not inside a git repository")

    paths = GitAttributesPaths.resolve(repo_root)

    config_present = _git_config_driver_set(repo_root)
    if config_present:
        _unconfigure_git_driver(repo_root)
    config_action = "removed" if config_present else "absent"

    if not paths.attributes_file.exists():
        return MergeUninstallResult(
            attr_action="absent",
            config_action=config_action,
            attributes_file=paths.attributes_file,
        )

    text = paths.attributes_file.read_text(encoding="utf-8")
    if not attr_is_ours(text):
        return MergeUninstallResult(
            attr_action="absent",
            config_action=config_action,
            attributes_file=paths.attributes_file,
        )

    stripped = _strip_attr_block(text)
    residue = "\n".join(
        ln for ln in stripped.splitlines() if ln.strip() and not ln.startswith("#")
    ).strip()
    if not residue:
        paths.attributes_file.unlink()
        attr_action = "removed"
    else:
        # Tidy any leftover blank runs from the strip before writing back.
        paths.attributes_file.write_text(stripped.rstrip("\n") + "\n", encoding="utf-8")
        attr_action = "stripped"

    return MergeUninstallResult(
        attr_action=attr_action,
        config_action=config_action,
        attributes_file=paths.attributes_file,
    )


@dataclass(slots=True)
class MergeStatus:
    """Reported install state of the merge driver.

    Attributes:
        attr_installed: True if ``.gitattributes`` carries our fenced block.
        driver_configured: True if ``.git/config`` has the ``merge.shiplog`` driver.
    """

    attr_installed: bool
    driver_configured: bool

    @property
    def fully_installed(self) -> bool:
        """True only when *both* the attributes rule and the driver are present.

        Both halves are required for merges to actually route through us — a rule
        with no configured driver silently falls back to git's default merge.
        """
        return self.attr_installed and self.driver_configured


def status(repo_root: str | os.PathLike[str]) -> MergeStatus:
    """Report whether the merge driver is installed (attributes + config).

    Raises:
        RuntimeError: if ``repo_root`` is not inside a git repository.
    """
    if _run_git("rev-parse", "--git-dir", cwd=repo_root) is None:
        raise RuntimeError("not inside a git repository")
    paths = GitAttributesPaths.resolve(repo_root)
    attr_installed = (
        paths.attributes_file.exists()
        and attr_is_ours(paths.attributes_file.read_text(encoding="utf-8"))
    )
    return MergeStatus(
        attr_installed=attr_installed,
        driver_configured=_git_config_driver_set(repo_root),
    )
