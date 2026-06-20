"""Tests for config load/merge/serialize (``shiplog.config``)."""

from __future__ import annotations

from pathlib import Path

from shiplog.config import (
    DEFAULTS,
    Config,
    config_path_for_repo,
    default_config_text,
    dumps,
)
from shiplog.store import SHIPLOG_DIR


def test_defaults_when_no_file(tmp_path: Path) -> None:
    cfg = Config.load(tmp_path)
    assert cfg.default_type == DEFAULTS["default_type"]
    assert cfg.author == ""
    assert cfg.schema_version == 1


def test_from_dict_merges_over_defaults() -> None:
    cfg = Config.from_dict({"author": "Ada"})
    assert cfg.author == "Ada"
    assert cfg.default_type == DEFAULTS["default_type"]  # filled from defaults


def test_unknown_keys_preserved_in_extra() -> None:
    cfg = Config.from_dict({"author": "Ada", "future_knob": 7})
    assert cfg.extra == {"future_knob": 7}
    # And they survive a round-trip back to a dict.
    assert cfg.to_dict()["future_knob"] == 7


def test_load_reads_written_file(tmp_path: Path) -> None:
    path = config_path_for_repo(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(default_config_text(), encoding="utf-8")
    cfg = Config.load(tmp_path)
    assert cfg.default_type == "note"
    assert cfg.schema_version == 1


def test_dumps_emits_valid_toml_roundtrip() -> None:
    text = dumps({"author": 'has "quotes"', "n": 3, "flag": True})
    # tomllib must be able to parse what we emit.
    import tomllib

    parsed = tomllib.loads(text)
    assert parsed == {"author": 'has "quotes"', "n": 3, "flag": True}


def test_config_path_location(tmp_path: Path) -> None:
    p = config_path_for_repo(tmp_path)
    assert p.parent.name == SHIPLOG_DIR
    assert p.name == "config.toml"
