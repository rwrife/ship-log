"""Config load/merge for ship-log.

Each repo gets a tiny ``.shiplog/config.toml`` written by ``shiplog init``. It is
intentionally minimal: a couple of defaults an agent or human might want to tweak
(default entry type, default author override). Reading uses stdlib :mod:`tomllib`
(Python 3.11+); writing uses a small hand-rolled serializer since the stdlib has no
TOML *writer*. We only ever emit flat ``key = value`` pairs, so a full TOML library
would be overkill.

Unknown keys in an existing config are preserved on read (merged over defaults) so a
newer shiplog reading an older/newer file never silently drops settings.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .store import SHIPLOG_DIR

CONFIG_FILENAME = "config.toml"

# Built-in defaults. Anything the config file omits falls back to these.
DEFAULTS: dict[str, Any] = {
    "default_type": "note",
    # Empty author => fall back to git config at write time (see gitctx).
    "author": "",
    "schema_version": 1,
}


def config_path_for_repo(repo_root: str | os.PathLike[str]) -> Path:
    """Return ``<repo_root>/.shiplog/config.toml``."""
    return Path(repo_root) / SHIPLOG_DIR / CONFIG_FILENAME


def _toml_escape(value: str) -> str:
    """Escape a string for a double-quoted TOML basic string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _toml_value(value: Any) -> str:
    """Render a scalar as TOML (only the small set of types we emit)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return f'"{_toml_escape(value)}"'
    # Fallback: stringify anything unexpected so we never crash on write.
    return f'"{_toml_escape(str(value))}"'


def dumps(data: dict[str, Any]) -> str:
    """Serialize a flat mapping to TOML text (one ``key = value`` per line)."""
    lines = [f"{key} = {_toml_value(val)}" for key, val in data.items()]
    return "\n".join(lines) + "\n"


@dataclass(slots=True)
class Config:
    """Resolved ship-log configuration for a repo.

    Attributes:
        default_type: Entry type used when ``add`` is given none.
        author: Author override; empty means "use git config".
        schema_version: Config format version stamp.
        extra: Any unrecognized keys from the file, preserved verbatim.
    """

    default_type: str = DEFAULTS["default_type"]
    author: str = DEFAULTS["author"]
    schema_version: int = DEFAULTS["schema_version"]
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        """Build a :class:`Config`, merging ``data`` over :data:`DEFAULTS`.

        Recognized keys populate fields; everything else is kept in ``extra`` so
        round-tripping the file never loses user settings.
        """
        merged = {**DEFAULTS, **data}
        known = {"default_type", "author", "schema_version"}
        extra = {k: v for k, v in data.items() if k not in known}
        return cls(
            default_type=str(merged["default_type"]),
            author=str(merged["author"]),
            schema_version=int(merged["schema_version"]),
            extra=extra,
        )

    @classmethod
    def load(cls, repo_root: str | os.PathLike[str]) -> Config:
        """Load config for ``repo_root``; return defaults if no file exists."""
        path = config_path_for_repo(repo_root)
        if not path.exists():
            return cls()
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        return cls.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        """Return a flat dict (known fields first, then preserved extras)."""
        data: dict[str, Any] = {
            "default_type": self.default_type,
            "author": self.author,
            "schema_version": self.schema_version,
        }
        data.update(self.extra)
        return data

    def dumps(self) -> str:
        """Serialize this config to TOML text."""
        return dumps(self.to_dict())


def default_config_text() -> str:
    """Return the TOML text for a fresh, default config file (with comments).

    ``init`` writes this on first run. Comments document the knobs without
    requiring a comment-preserving TOML library for the common case.
    """
    return (
        "# ship-log config — see https://github.com/rwrife/ship-log\n"
        "\n"
        "# Default entry type when `shiplog add` is given none of\n"
        "# {decision, attempt, deadend, note}.\n"
        f'default_type = "{DEFAULTS["default_type"]}"\n'
        "\n"
        "# Override the author string. Leave empty to use your git config\n"
        '# (user.name <user.email>).\n'
        f'author = "{DEFAULTS["author"]}"\n'
        "\n"
        "# Config format version. Don't edit by hand.\n"
        f"schema_version = {DEFAULTS['schema_version']}\n"
    )
