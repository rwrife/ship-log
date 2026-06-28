"""Tests for MCP server mode (``shiplog mcp``).

These exercise the pure :func:`~shiplog.mcp.dispatch` protocol surface and the tool
handlers end-to-end against a real throwaway git repo (so git-stamping and the
shared store/brief/filters are covered for real, not mocked), plus a round-trip of
the stdio :func:`~shiplog.mcp.serve` loop over in-memory buffers.

The key invariant we guard: MCP tools reuse the same store/models/ranking/filters
as the CLI — no logic fork — so a tool call writes/reads exactly what ``shiplog
add``/``ls``/``brief`` would.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shiplog import mcp
from shiplog.cli import app
from shiplog.models import EntryType
from shiplog.store import Store

runner = CliRunner()


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh git repo (one commit, ship-log initialized) with cwd set to it."""
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
    runner.invoke(app, ["init"])
    return tmp_path


def _call(name: str, arguments: dict | None = None, *, rid: int = 1) -> dict:
    """Dispatch a tools/call request and return the JSON-RPC response."""
    return mcp.dispatch(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
    )


# -- protocol handshake ---------------------------------------------------


def test_initialize_returns_capabilities_and_server_info() -> None:
    resp = mcp.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert resp["id"] == 1
    result = resp["result"]
    assert result["protocolVersion"] == mcp.PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == mcp.SERVER_NAME
    assert "tools" in result["capabilities"]
    assert "instructions" in result


def test_ping_returns_empty_result() -> None:
    resp = mcp.dispatch({"jsonrpc": "2.0", "id": 7, "method": "ping"})
    assert resp == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_notification_gets_no_response() -> None:
    # No "id" => notification => no reply at all.
    assert mcp.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method_is_method_not_found() -> None:
    resp = mcp.dispatch({"jsonrpc": "2.0", "id": 2, "method": "nope/zzz"})
    assert resp["error"]["code"] == mcp.METHOD_NOT_FOUND


def test_non_object_request_is_invalid() -> None:
    resp = mcp.dispatch(["not", "an", "object"])
    assert resp["error"]["code"] == mcp.INVALID_REQUEST


# -- tools/list -----------------------------------------------------------


def test_tools_list_advertises_three_tools_with_schemas() -> None:
    resp = mcp.dispatch({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    tools = resp["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"shiplog_add", "shiplog_brief", "shiplog_ls"}
    for t in tools:
        assert t["description"]
        schema = t["inputSchema"]
        assert schema["type"] == "object"
        assert "properties" in schema
    # add requires type+summary and constrains type to the known set.
    add = next(t for t in tools if t["name"] == "shiplog_add")
    assert set(add["inputSchema"]["required"]) == {"type", "summary"}
    assert set(add["inputSchema"]["properties"]["type"]["enum"]) == {
        t.value for t in EntryType
    }


# -- shiplog_add ----------------------------------------------------------


def test_add_tool_writes_git_stamped_entry(repo: Path) -> None:
    resp = _call(
        "shiplog_add",
        {
            "type": "decision",
            "summary": "use JSONL for storage",
            "why": "diffable + merge-friendly",
            "files": ["shiplog/store.py", "shiplog/models.py"],
            "tags": ["storage", "arch"],
            "ref": "#2",
        },
    )
    result = resp["result"]
    assert result["isError"] is False
    structured = result["structuredContent"]
    assert structured["type"] == "decision"
    assert structured["summary"] == "use JSONL for storage"

    # Same store the CLI writes — verify on disk.
    entries = Store.for_repo(repo).read_all()
    assert len(entries) == 1
    e = entries[0]
    assert e.type == EntryType.DECISION
    assert e.why == "diffable + merge-friendly"
    assert e.files == ["shiplog/store.py", "shiplog/models.py"]
    assert e.tags == ["storage", "arch"]
    assert e.ref == "#2"
    assert e.author == "Test Captain <cap@ship.log>"
    assert e.branch == "main"
    assert e.sha  # short HEAD sha present


def test_add_tool_accepts_comma_string_for_list_fields(repo: Path) -> None:
    # Forgiving: a client may send "a,b" instead of ["a","b"].
    resp = _call(
        "shiplog_add",
        {"type": "note", "summary": "csv coercion", "files": "x.py, y.py", "tags": "t1,t2"},
    )
    assert resp["result"]["isError"] is False
    e = Store.for_repo(repo).read_all()[0]
    assert e.files == ["x.py", "y.py"]
    assert e.tags == ["t1", "t2"]


def test_add_tool_unknown_type_is_in_band_error(repo: Path) -> None:
    resp = _call("shiplog_add", {"type": "wibble", "summary": "x"})
    result = resp["result"]
    assert result["isError"] is True
    assert "unknown entry type" in result["content"][0]["text"]
    assert Store.for_repo(repo).count() == 0  # nothing written


def test_add_tool_empty_summary_is_in_band_error(repo: Path) -> None:
    resp = _call("shiplog_add", {"type": "note", "summary": "   "})
    assert resp["result"]["isError"] is True
    assert "summary must not be empty" in resp["result"]["content"][0]["text"]


def test_add_tool_before_init_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _git("init", cwd=tmp_path)
    _git("config", "user.email", "a@b.c", cwd=tmp_path)
    _git("config", "user.name", "A", cwd=tmp_path)
    monkeypatch.chdir(tmp_path)
    from shiplog import gitctx

    gitctx.find_repo_root.cache_clear()
    resp = _call("shiplog_add", {"type": "note", "summary": "x"})
    assert resp["result"]["isError"] is True
    assert "shiplog init" in resp["result"]["content"][0]["text"]


# -- shiplog_ls -----------------------------------------------------------


def test_ls_tool_lists_newest_first_with_filters(repo: Path) -> None:
    _call("shiplog_add", {"type": "decision", "summary": "first", "tags": ["keep"]})
    _call("shiplog_add", {"type": "deadend", "summary": "second", "tags": ["keep"]})
    _call("shiplog_add", {"type": "note", "summary": "third"})

    # No filter. The three are written in the same second, so their timestamps
    # tie and newest-first (a stable sort) preserves append order — see
    # sort_newest_first / test_ls_show.py. Cross-second ordering is covered there.
    resp = _call("shiplog_ls", {})
    entries = resp["result"]["structuredContent"]["entries"]
    assert [e["summary"] for e in entries] == ["first", "second", "third"]
    assert resp["result"]["structuredContent"]["count"] == 3

    # Type filter.
    resp = _call("shiplog_ls", {"type": "deadend"})
    only = resp["result"]["structuredContent"]["entries"]
    assert [e["summary"] for e in only] == ["second"]

    # Tag filter + limit (both tagged 'keep'; limit caps to the first listed).
    resp = _call("shiplog_ls", {"tag": "keep", "limit": 1})
    limited = resp["result"]["structuredContent"]["entries"]
    assert [e["summary"] for e in limited] == ["first"]


def test_ls_tool_bad_since_is_in_band_error(repo: Path) -> None:
    resp = _call("shiplog_ls", {"since": "not-a-date"})
    assert resp["result"]["isError"] is True


# -- shiplog_brief --------------------------------------------------------


def test_brief_tool_ranks_deadends_first(repo: Path) -> None:
    _call("shiplog_add", {"type": "note", "summary": "a note"})
    _call("shiplog_add", {"type": "decision", "summary": "a decision"})
    _call("shiplog_add", {"type": "deadend", "summary": "a dead-end"})

    resp = _call("shiplog_brief", {"limit": 0})
    structured = resp["result"]["structuredContent"]
    assert structured["deadends"] == 1
    # Dead-end leads the ranked digest.
    assert structured["entries"][0]["summary"] == "a dead-end"
    assert structured["shown"] == 3
    assert structured["total"] == 3


def test_brief_tool_focus_files_argument(repo: Path) -> None:
    _call(
        "shiplog_add",
        {"type": "decision", "summary": "touches store", "files": ["shiplog/store.py"]},
    )
    _call("shiplog_add", {"type": "decision", "summary": "unrelated"})
    resp = _call("shiplog_brief", {"files": ["shiplog/store.py"]})
    structured = resp["result"]["structuredContent"]
    assert structured["focus"] == ["shiplog/store.py"]
    # Focused entry ranks ahead of the unrelated one (same type).
    assert structured["entries"][0]["summary"] == "touches store"


def test_brief_tool_invalid_limit_is_in_band_error(repo: Path) -> None:
    resp = _call("shiplog_brief", {"limit": "lots"})
    assert resp["result"]["isError"] is True


# -- unknown tool ---------------------------------------------------------


def test_call_unknown_tool_is_invalid_params(repo: Path) -> None:
    resp = _call("shiplog_nope", {})
    assert resp["error"]["code"] == mcp.INVALID_PARAMS


# -- stdio serve loop round-trip -----------------------------------------


def test_serve_loop_round_trips_over_buffers(repo: Path) -> None:
    """Drive serve() with line-delimited JSON-RPC and assert clean responses."""
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},  # no reply
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "shiplog_add", "arguments": {"type": "note", "summary": "hi"}},
        },
    ]
    stdin = io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n")
    stdout = io.StringIO()
    stderr = io.StringIO()

    code = mcp.serve(stdin=stdin, stdout=stdout, stderr=stderr)
    assert code == 0

    lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
    # 3 requests with ids -> 3 responses; the notification produced none.
    assert len(lines) == 3
    responses = [json.loads(ln) for ln in lines]
    assert [r["id"] for r in responses] == [1, 2, 3]
    assert responses[0]["result"]["serverInfo"]["name"] == mcp.SERVER_NAME
    assert {t["name"] for t in responses[1]["result"]["tools"]} == {
        "shiplog_add",
        "shiplog_brief",
        "shiplog_ls",
    }
    assert responses[2]["result"]["isError"] is False
    # The add actually landed in the shared store.
    assert Store.for_repo(repo).read_all()[0].summary == "hi"


def test_serve_loop_reports_parse_error_for_bad_json(repo: Path) -> None:
    stdin = io.StringIO("not json at all\n")
    stdout = io.StringIO()
    stderr = io.StringIO()
    mcp.serve(stdin=stdin, stdout=stdout, stderr=stderr)
    resp = json.loads(stdout.getvalue().strip())
    assert resp["error"]["code"] == mcp.PARSE_ERROR
    assert resp["id"] is None
