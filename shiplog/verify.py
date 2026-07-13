"""Read-only integrity & schema linter for a ship-log JSONL file.

As agents append to ``.shiplog/log.jsonl`` autonomously, the file can rot:
malformed JSON lines, missing required fields, an unknown ``type``, duplicate
``id``s, non-monotonic ``ts``, dangling ``link``/``ack``/``fix`` targets, or a
``schema_version`` a given CLI is too old to read. :func:`verify` walks every
line and reports these as structured :class:`Finding`s so CI can fail a bad
append instead of shipping it.

Design:
- We read *raw lines* (not :meth:`Store.read_all`, which raises on the first bad
  line) so one corrupt record never hides the rest of the report.
- Findings carry a stable machine ``code`` (see :class:`Code`) plus a line
  number and, when known, the offending entry ``id`` — agent/CI parsable via
  ``--json``.
- Severity is either ``error`` (fails ``verify``) or ``warning`` (only fails
  under ``--strict``). Non-monotonic ``ts`` is the sole warning by default.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .models import SCHEMA_VERSION, EntryType
from .store import Store

# Reference-bearing fields whose non-empty value must name an existing entry id.
# ``link_target`` backs ``link`` entries; ``ack``/``fix`` are forward-looking
# reference kinds the guard/link machinery may write. We validate any that are
# present so dangling pointers are caught regardless of who wrote them.
_REFERENCE_FIELDS = ("link_target", "ack_target", "fix_target")

_KNOWN_TYPES = frozenset(t.value for t in EntryType)


class Severity(StrEnum):
    """Whether a finding fails ``verify`` outright or only under ``--strict``."""

    ERROR = "error"
    WARNING = "warning"


class Code(StrEnum):
    """Stable machine codes for each failure class (safe to grep / branch on)."""

    BAD_JSON = "bad-json"           # line is not parseable JSON
    NOT_OBJECT = "not-object"       # parsed, but not a JSON object
    MISSING_FIELD = "missing-field" # required field absent
    UNKNOWN_TYPE = "unknown-type"   # `type` not in EntryType
    DUPLICATE_ID = "duplicate-id"   # `id` seen on an earlier line
    SCHEMA_TOO_NEW = "schema-too-new"  # schema_version > SCHEMA_VERSION
    BAD_SCHEMA = "bad-schema-version"  # schema_version not an int
    DANGLING_REF = "dangling-ref"   # link/ack/fix points at a missing id
    NON_MONOTONIC_TS = "non-monotonic-ts"  # ts earlier than a previous line


# Fields every entry must carry to be meaningful/round-trippable.
_REQUIRED_FIELDS = ("summary", "id", "type", "ts")


@dataclass(slots=True)
class Finding:
    """One problem found in the log.

    Attributes:
        line: 1-based line number in the file.
        code: Stable :class:`Code` for programmatic handling.
        severity: ``error`` or ``warning``.
        message: Human-readable description.
        id: The entry's ``id`` when known (empty if unparseable/absent).
    """

    line: int
    code: Code
    severity: Severity
    message: str
    id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict (enums as their string values)."""
        return {
            "line": self.line,
            "code": self.code.value,
            "severity": self.severity.value,
            "message": self.message,
            "id": self.id,
        }


@dataclass(slots=True)
class Report:
    """The outcome of a :func:`verify` run.

    ``ok`` folds ``strict`` in: with ``strict=True`` any warning also makes the
    report not-ok, so callers can map ``ok`` straight to an exit code.
    """

    findings: list[Finding] = field(default_factory=list)
    checked: int = 0
    strict: bool = False

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == Severity.WARNING]

    @property
    def ok(self) -> bool:
        """True when the log passes (no errors; no warnings when ``strict``)."""
        if self.errors:
            return False
        if self.strict and self.warnings:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        """Structured summary for ``--json`` (agent/CI parsable)."""
        return {
            "ok": self.ok,
            "checked": self.checked,
            "strict": self.strict,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "findings": [f.to_dict() for f in self.findings],
        }


def _raw_lines(path: Path) -> list[str]:
    """Return raw (un-stripped) lines; missing file is an empty log."""
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as fh:
        return fh.readlines()


def verify(store: Store, *, strict: bool = False) -> Report:
    """Validate every line of ``store``'s log and return a :class:`Report`.

    Read-only. Two passes conceptually, one loop in practice:

    1. Per-line: JSON parse, object shape, required fields, known ``type``,
       duplicate ``id``, ``schema_version`` bounds, and ``ts`` monotonicity.
    2. Cross-line: collect every reference (``link``/``ack``/``fix`` target) and,
       after all ids are known, flag any that point at a missing id.

    Args:
        store: The log to check (only its ``path`` is read).
        strict: When True, warnings (non-monotonic ``ts``) also fail ``ok``.
    """
    report = Report(strict=strict)
    seen_ids: set[str] = set()
    prev_ts: str | None = None
    # (line, id, field, target) captured for a deferred dangling-ref pass.
    pending_refs: list[tuple[int, str, str, str]] = []

    for lineno, raw in enumerate(_raw_lines(store.path), start=1):
        line = raw.strip()
        if not line:
            continue  # blank lines are legal padding, not entries
        report.checked += 1

        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            report.findings.append(
                Finding(lineno, Code.BAD_JSON, Severity.ERROR, f"invalid JSON: {exc.msg}")
            )
            continue

        if not isinstance(obj, dict):
            report.findings.append(
                Finding(
                    lineno,
                    Code.NOT_OBJECT,
                    Severity.ERROR,
                    f"expected a JSON object, got {type(obj).__name__}",
                )
            )
            continue

        entry_id = str(obj.get("id", "") or "")

        # Required fields.
        for req in _REQUIRED_FIELDS:
            if req not in obj or obj[req] in (None, ""):
                report.findings.append(
                    Finding(
                        lineno,
                        Code.MISSING_FIELD,
                        Severity.ERROR,
                        f"missing required field {req!r}",
                        entry_id,
                    )
                )

        # Known type.
        etype = obj.get("type")
        if etype is not None and str(etype) not in _KNOWN_TYPES:
            valid = ", ".join(sorted(_KNOWN_TYPES))
            report.findings.append(
                Finding(
                    lineno,
                    Code.UNKNOWN_TYPE,
                    Severity.ERROR,
                    f"unknown type {etype!r}; expected one of: {valid}",
                    entry_id,
                )
            )

        # Duplicate id.
        if entry_id:
            if entry_id in seen_ids:
                report.findings.append(
                    Finding(
                        lineno,
                        Code.DUPLICATE_ID,
                        Severity.ERROR,
                        f"duplicate id {entry_id!r} (already seen earlier)",
                        entry_id,
                    )
                )
            else:
                seen_ids.add(entry_id)

        # Schema version bounds.
        sv = obj.get("schema_version", SCHEMA_VERSION)
        if not isinstance(sv, int) or isinstance(sv, bool):
            report.findings.append(
                Finding(
                    lineno,
                    Code.BAD_SCHEMA,
                    Severity.ERROR,
                    f"schema_version must be an integer, got {sv!r}",
                    entry_id,
                )
            )
        elif sv > SCHEMA_VERSION:
            report.findings.append(
                Finding(
                    lineno,
                    Code.SCHEMA_TOO_NEW,
                    Severity.ERROR,
                    f"schema_version {sv} is newer than supported ({SCHEMA_VERSION}); "
                    "upgrade shiplog to read this log",
                    entry_id,
                )
            )

        # Timestamp monotonicity (warning; strict-only failure). We compare ISO
        # strings lexically — valid Z-suffixed UTC ISO-8601 sorts chronologically.
        ts = obj.get("ts")
        if isinstance(ts, str) and ts:
            if prev_ts is not None and ts < prev_ts:
                report.findings.append(
                    Finding(
                        lineno,
                        Code.NON_MONOTONIC_TS,
                        Severity.WARNING,
                        f"ts {ts!r} is earlier than a previous entry ({prev_ts!r})",
                        entry_id,
                    )
                )
            prev_ts = ts if prev_ts is None or ts >= prev_ts else prev_ts

        # Defer reference checks until every id is known.
        for ref_field in _REFERENCE_FIELDS:
            target = obj.get(ref_field)
            if isinstance(target, str) and target:
                pending_refs.append((lineno, entry_id, ref_field, target))

    # Cross-line dangling-reference pass.
    for lineno, entry_id, ref_field, target in pending_refs:
        if target not in seen_ids:
            report.findings.append(
                Finding(
                    lineno,
                    Code.DANGLING_REF,
                    Severity.ERROR,
                    f"{ref_field} points at unknown id {target!r}",
                    entry_id,
                )
            )

    # Stable ordering: by line, then by code, for deterministic output/tests.
    report.findings.sort(key=lambda f: (f.line, f.code.value))
    return report
