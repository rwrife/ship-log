"""Tests for ``shiplog verify`` — the read-only integrity & schema linter (#39).

Two layers:
- Unit tests against :func:`shiplog.verify.verify`, driving a ``Store`` whose
  ``log.jsonl`` we write by hand so we can inject each failure class (malformed
  JSON, non-object, missing field, unknown type, dup id, schema-too-new, dangling
  ref, non-monotonic ts) plus the happy path.
- CLI tests end-to-end through Typer against a real throwaway git repo, asserting
  exit codes (0 clean / 1 error / strict-warning) and ``--json`` shape.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shiplog.cli import app
from shiplog.models import SCHEMA_VERSION, EntryType
from shiplog.store import Store
from shiplog.verify import Code, verify

runner = CliRunner()


# --------------------------------------------------------------------------
# unit: verify() against hand-written log files
# --------------------------------------------------------------------------


def _store_with_lines(tmp_path: Path, lines: list[str]) -> Store:
    """Write ``lines`` (each a full JSONL record or raw text) and return a Store."""
    path = tmp_path / "log.jsonl"
    path.write_text("".join(ln + "\n" for ln in lines), encoding="utf-8")
    return Store(path)


def _line(**overrides) -> str:
    """A valid entry JSON line with optional field overrides."""
    base = {
        "summary": "s",
        "id": overrides.pop("id", "260101-AAAAAA"),
        "type": "note",
        "ts": "2026-01-01T00:00:00Z",
        "schema_version": SCHEMA_VERSION,
    }
    base.update(overrides)
    return json.dumps(base, sort_keys=True)


def test_missing_file_is_clean(tmp_path: Path):
    store = Store(tmp_path / "nope.jsonl")
    report = verify(store)
    assert report.ok and report.checked == 0 and not report.findings


def test_happy_path_multiple_entries(tmp_path: Path):
    store = _store_with_lines(
        tmp_path,
        [
            _line(id="260101-AAAAAA", ts="2026-01-01T00:00:00Z"),
            _line(id="260102-BBBBBB", ts="2026-01-02T00:00:00Z"),
            "",  # blank padding line is legal, not counted
            _line(id="260103-CCCCCC", ts="2026-01-03T00:00:00Z"),
        ],
    )
    report = verify(store, strict=True)
    assert report.ok
    assert report.checked == 3
    assert not report.findings


def test_bad_json(tmp_path: Path):
    store = _store_with_lines(tmp_path, ["{not valid json", _line()])
    report = verify(store)
    assert not report.ok
    codes = [f.code for f in report.findings]
    assert Code.BAD_JSON in codes
    assert report.findings[0].line == 1


def test_not_object(tmp_path: Path):
    store = _store_with_lines(tmp_path, ["[1, 2, 3]"])
    report = verify(store)
    assert not report.ok
    assert report.findings[0].code == Code.NOT_OBJECT


def test_missing_required_field(tmp_path: Path):
    # Drop `summary`.
    line = json.dumps(
        {"id": "260101-AAAAAA", "type": "note", "ts": "2026-01-01T00:00:00Z"},
        sort_keys=True,
    )
    store = _store_with_lines(tmp_path, [line])
    report = verify(store)
    assert not report.ok
    finding = next(f for f in report.findings if f.code == Code.MISSING_FIELD)
    assert "summary" in finding.message
    assert finding.id == "260101-AAAAAA"


def test_unknown_type(tmp_path: Path):
    store = _store_with_lines(tmp_path, [_line(type="banana")])
    report = verify(store)
    assert not report.ok
    assert any(f.code == Code.UNKNOWN_TYPE for f in report.findings)


def test_all_known_types_pass(tmp_path: Path):
    lines = [
        _line(id=f"260101-{i:06d}", type=t.value, ts=f"2026-01-0{i+1}T00:00:00Z")
        for i, t in enumerate(EntryType)
    ]
    store = _store_with_lines(tmp_path, lines)
    report = verify(store)
    assert report.ok, [f.message for f in report.findings]


def test_duplicate_id(tmp_path: Path):
    store = _store_with_lines(
        tmp_path,
        [
            _line(id="260101-DUPDUP", ts="2026-01-01T00:00:00Z"),
            _line(id="260101-DUPDUP", ts="2026-01-02T00:00:00Z"),
        ],
    )
    report = verify(store)
    assert not report.ok
    dup = next(f for f in report.findings if f.code == Code.DUPLICATE_ID)
    assert dup.line == 2 and dup.id == "260101-DUPDUP"


def test_schema_too_new(tmp_path: Path):
    store = _store_with_lines(tmp_path, [_line(schema_version=SCHEMA_VERSION + 1)])
    report = verify(store)
    assert not report.ok
    assert any(f.code == Code.SCHEMA_TOO_NEW for f in report.findings)


def test_bad_schema_version_type(tmp_path: Path):
    store = _store_with_lines(tmp_path, [_line(schema_version="one")])
    report = verify(store)
    assert not report.ok
    assert any(f.code == Code.BAD_SCHEMA for f in report.findings)


def test_dangling_reference(tmp_path: Path):
    store = _store_with_lines(
        tmp_path,
        [
            _line(id="260101-AAAAAA", ts="2026-01-01T00:00:00Z"),
            _line(
                id="260102-BBBBBB",
                type="link",
                ts="2026-01-02T00:00:00Z",
                link_target="260101-MISSING",
            ),
        ],
    )
    report = verify(store)
    assert not report.ok
    dangling = next(f for f in report.findings if f.code == Code.DANGLING_REF)
    assert "260101-MISSING" in dangling.message
    assert dangling.line == 2


def test_valid_reference_passes(tmp_path: Path):
    store = _store_with_lines(
        tmp_path,
        [
            _line(id="260101-AAAAAA", ts="2026-01-01T00:00:00Z"),
            _line(
                id="260102-BBBBBB",
                type="link",
                ts="2026-01-02T00:00:00Z",
                link_target="260101-AAAAAA",
            ),
        ],
    )
    report = verify(store, strict=True)
    assert report.ok


def test_forward_reference_resolves(tmp_path: Path):
    # A link that points at an id appearing on a LATER line still resolves,
    # because dangling-ref checking is a deferred cross-line pass.
    store = _store_with_lines(
        tmp_path,
        [
            _line(
                id="260101-AAAAAA",
                type="link",
                ts="2026-01-01T00:00:00Z",
                link_target="260102-BBBBBB",
            ),
            _line(id="260102-BBBBBB", ts="2026-01-02T00:00:00Z"),
        ],
    )
    report = verify(store)
    assert report.ok


def test_non_monotonic_ts_is_warning(tmp_path: Path):
    store = _store_with_lines(
        tmp_path,
        [
            _line(id="260102-AAAAAA", ts="2026-01-02T00:00:00Z"),
            _line(id="260101-BBBBBB", ts="2026-01-01T00:00:00Z"),
        ],
    )
    # Non-strict: warning present but report still OK.
    report = verify(store)
    assert report.ok
    assert report.warnings and report.warnings[0].code == Code.NON_MONOTONIC_TS
    # Strict: same warning now fails.
    strict_report = verify(store, strict=True)
    assert not strict_report.ok


def test_to_dict_shape(tmp_path: Path):
    store = _store_with_lines(tmp_path, [_line(type="banana")])
    d = verify(store).to_dict()
    assert set(d) == {"ok", "checked", "strict", "errors", "warnings", "findings"}
    assert d["ok"] is False and d["errors"] == 1
    f0 = d["findings"][0]
    assert set(f0) == {"line", "code", "severity", "message", "id"}


# --------------------------------------------------------------------------
# CLI end-to-end
# --------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _git("init", cwd=tmp_path)
    _git("config", "user.name", "Test Captain", cwd=tmp_path)
    _git("config", "user.email", "cap@ship.log", cwd=tmp_path)
    _git("checkout", "-b", "main", cwd=tmp_path)
    (tmp_path / "a.txt").write_text("hi\n")
    _git("add", "a.txt", cwd=tmp_path)
    _git("commit", "-m", "init", cwd=tmp_path)
    monkeypatch.chdir(tmp_path)
    from shiplog import gitctx

    gitctx.find_repo_root.cache_clear()
    return tmp_path


def _write_log(repo: Path, lines: list[str]) -> None:
    d = repo / ".shiplog"
    d.mkdir(exist_ok=True)
    (d / "log.jsonl").write_text("".join(ln + "\n" for ln in lines), encoding="utf-8")


def test_cli_no_init_fails_friendly(repo: Path):
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 1
    assert "shiplog init" in result.output


def test_cli_clean_exit_zero(repo: Path):
    _write_log(repo, [_line(id="260101-AAAAAA")])
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0
    assert "clean" in result.output


def test_cli_error_exit_one(repo: Path):
    _write_log(repo, ["{broken"])
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 1
    assert "bad-json" in result.output


def test_cli_json_shape_and_exit(repo: Path):
    _write_log(repo, [_line(type="banana")])
    result = runner.invoke(app, ["verify", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["findings"][0]["code"] == "unknown-type"


def test_cli_strict_flag(repo: Path):
    _write_log(
        repo,
        [
            _line(id="260102-AAAAAA", ts="2026-01-02T00:00:00Z"),
            _line(id="260101-BBBBBB", ts="2026-01-01T00:00:00Z"),
        ],
    )
    # Non-strict passes.
    assert runner.invoke(app, ["verify"]).exit_code == 0
    # Strict fails on the ts warning.
    assert runner.invoke(app, ["verify", "--strict"]).exit_code == 1
