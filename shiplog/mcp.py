"""Model Context Protocol (MCP) server mode for ship-log.

``shiplog mcp`` starts a stdio MCP server so agents call ship-log **natively** as
tools instead of shelling out to the CLI and scraping text. The same store,
models, ranking, and filters back these tools as the CLI commands — there is no
logic fork (see :mod:`shiplog.store`, :mod:`shiplog.brief`, :mod:`shiplog.filters`).

Why a tiny hand-rolled server (no SDK dependency)
-------------------------------------------------
ship-log's whole ethos is "boring, fast, no daemon, no heavyweight deps" (PLAN.md).
The MCP stdio transport is just **newline-delimited JSON-RPC 2.0 on stdin/stdout**:
each message is a single JSON object on one line, with no embedded newlines, and
the server never writes anything but protocol messages to stdout. That is small
enough to implement directly and keeps install + CI hermetic (no extra wheels).

Design
------
- :func:`dispatch` is a **pure function**: ``request dict -> response dict | None``.
  It contains all protocol + tool logic and is fully unit-testable without touching
  stdio. A JSON-RPC *notification* (no ``id``) returns ``None`` (no reply is sent).
- :func:`serve` is the thin transport loop: read a line, parse, dispatch, write the
  response line. All human-facing chatter goes to **stderr** so stdout stays a clean
  protocol channel.
- Tools resolve the repo from the server's working directory at call time via
  :class:`~shiplog.gitctx.GitContext`, exactly like the CLI does — so an agent points
  the server at a repo by launching it with that repo as ``cwd``.

Exposed tools
-------------
- ``shiplog_add``   — append a decision/attempt/deadend/note (git-stamped).
- ``shiplog_brief`` — token-efficient digest (dead-ends first) for context.
- ``shiplog_ls``    — list entries newest-first with optional filters.

Each returns MCP ``content`` (a human-readable text block) **and** ``structuredContent``
(the same stable JSON an agent gets from the CLI's ``--json``), so clients that
understand structured tool output parse it directly.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import IO, Any

from . import __version__
from .brief import DEFAULT_BUDGET, brief_to_dict, build_brief
from .config import Config
from .filters import filter_entries, parse_since, sort_newest_first
from .gitctx import GitContext, working_tree_files
from .links import split_links
from .models import Entry, EntryType
from .store import SHIPLOG_DIR, Store

# The MCP revision we implement against. Clients send their own in ``initialize``;
# we echo a version we support. This is a stable, dated protocol string.
PROTOCOL_VERSION = "2024-11-05"

SERVER_NAME = "ship-log"

JSONRPC_VERSION = "2.0"

# JSON-RPC 2.0 standard error codes (subset we use).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class McpError(Exception):
    """A JSON-RPC error to return to the client.

    Args:
        code: One of the JSON-RPC error codes above.
        message: Human-readable, short error message.
    """

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# Shared helpers (CSV parsing mirrors the CLI's _split_csv exactly)
# --------------------------------------------------------------------------- #


def _as_list(value: Any) -> list[str]:
    """Coerce a tool argument into a clean ``list[str]`` (order-stable, de-duped).

    Accepts either a JSON array of strings or a single comma-separated string, so
    the tools are forgiving to clients that send ``"a,b"`` instead of ``["a","b"]``
    (and vice versa). Blanks and duplicates are dropped, preserving first-seen order
    — identical to the CLI's comma-option handling.
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            parts.extend(str(item).split(","))
    else:  # numbers/bools etc. — stringify defensively
        parts = [str(value)]
    seen: list[str] = []
    for part in parts:
        item = part.strip()
        if item and item not in seen:
            seen.append(item)
    return seen


def _require_repo_store(*, must_exist: bool) -> tuple[Store, GitContext]:
    """Resolve the current repo's store + git context, or raise :class:`McpError`.

    Mirrors the CLI's "are we in a repo / has it been init'd" guard so MCP tools
    fail with the same friendly guidance an interactive user would see.
    """
    ctx = GitContext.capture()
    if ctx.repo_root is None:
        raise McpError(
            INVALID_PARAMS,
            "not inside a git repository. Launch the MCP server with your repo as "
            "the working directory (or run `git init` there first).",
        )
    store = Store.for_repo(ctx.repo_root)
    if must_exist and not store.exists():
        raise McpError(
            INVALID_PARAMS,
            "no ship-log here yet. Run `shiplog init` in the repo first.",
        )
    return store, ctx


# --------------------------------------------------------------------------- #
# Tool implementations — each returns (text_summary, structured_dict)
# --------------------------------------------------------------------------- #


def _tool_add(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """``shiplog_add``: append one entry, git-stamped, like ``shiplog add``."""
    type_raw = args.get("type")
    if not type_raw:
        raise McpError(INVALID_PARAMS, "missing required argument 'type'.")
    try:
        entry_type = EntryType.coerce(type_raw)
    except ValueError as exc:
        raise McpError(INVALID_PARAMS, str(exc)) from exc

    summary = str(args.get("summary", "")).strip()
    if not summary:
        raise McpError(INVALID_PARAMS, "summary must not be empty.")

    store, ctx = _require_repo_store(must_exist=True)

    # Config may override the author; otherwise use the captured git author —
    # identical precedence to the CLI's `add`.
    config = Config.load(ctx.repo_root)
    author = config.author or ctx.author

    entry = Entry(
        summary=summary,
        type=entry_type,
        author=author,
        branch=ctx.branch,
        sha=ctx.sha,
        why=str(args.get("why", "")).strip(),
        files=_as_list(args.get("files")),
        tags=_as_list(args.get("tags")),
        ref=str(args.get("ref", "")).strip(),
    )
    store.append(entry)

    meta = entry.branch or "(no branch)"
    if entry.sha:
        meta += f" @ {entry.sha}"
    text = f"⚓ logged {entry.type.value} {entry.id}: {entry.summary} [{meta}]"
    return text, entry.to_dict()


def _tool_brief(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """``shiplog_brief``: ranked, budgeted digest — same logic as ``shiplog brief``."""
    store, ctx = _require_repo_store(must_exist=True)

    files = _as_list(args.get("files"))
    if files:
        focus = files
    else:
        # Default focus is the working tree (minus the .shiplog/ storage dir),
        # exactly like the CLI so the digest auto-scopes to what's being touched.
        focus = [
            f
            for f in working_tree_files(ctx.repo_root)
            if not f.rstrip("/").startswith(SHIPLOG_DIR)
        ]

    limit = _coerce_int(args.get("limit"), default=DEFAULT_BUDGET, name="limit")
    digest = build_brief(store.read_all(), focus=focus, budget=limit)
    structured = brief_to_dict(digest)

    # A compact text summary for clients that only render content blocks.
    text = (
        f"{structured['shown']} of {structured['total']} entries "
        f"({structured['deadends']} dead-end(s) up top)."
    )
    if structured["truncated"]:
        text += f" +{structured['truncated']} more not shown (raise limit)."
    return text, structured


def _tool_ls(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """``shiplog_ls``: filtered, newest-first listing — same logic as ``shiplog ls``."""
    type_q = str(args.get("type", "") or "").strip()
    if type_q:
        try:
            type_q = EntryType.coerce(type_q).value
        except ValueError as exc:
            raise McpError(INVALID_PARAMS, str(exc)) from exc

    since_dt = None
    since_raw = str(args.get("since", "") or "").strip()
    if since_raw:
        try:
            since_dt = parse_since(since_raw)
        except ValueError as exc:
            raise McpError(INVALID_PARAMS, str(exc)) from exc

    store, _ = _require_repo_store(must_exist=True)

    all_entries = store.read_all()
    # Mirror the CLI `ls`: link records annotate other entries, so they don't
    # appear as standalone rows unless explicitly requested via type='link'.
    if type_q == EntryType.LINK.value:
        source = all_entries
    else:
        source, _links = split_links(all_entries)

    entries = filter_entries(
        source,
        type_=type_q,
        tag=str(args.get("tag", "") or "").strip(),
        file=str(args.get("file", "") or "").strip(),
        since=since_dt,
    )
    entries = sort_newest_first(entries)
    limit = _coerce_int(args.get("limit"), default=0, name="limit")
    if limit and limit > 0:
        entries = entries[:limit]

    payload = [e.to_dict() for e in entries]
    structured = {"entries": payload, "count": len(payload)}
    text = f"{len(payload)} entr{'y' if len(payload) == 1 else 'ies'}."
    return text, structured


def _coerce_int(value: Any, *, default: int, name: str) -> int:
    """Coerce an optional numeric tool arg to ``int`` (``None`` -> default)."""
    if value is None:
        return default
    if isinstance(value, bool):  # bool is a subclass of int — reject explicitly
        raise McpError(INVALID_PARAMS, f"'{name}' must be an integer, not a boolean.")
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise McpError(INVALID_PARAMS, f"'{name}' must be an integer.") from exc


# --------------------------------------------------------------------------- #
# Tool registry + JSON schemas (advertised by tools/list)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Tool:
    """An MCP tool: its name, description, input schema, and handler."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], tuple[str, dict[str, Any]]]


_ENTRY_TYPES = [t.value for t in EntryType]

TOOLS: list[Tool] = [
    Tool(
        name="shiplog_add",
        description=(
            "Append an entry to this repo's ship-log (append-only decision/dead-end "
            "ledger). Use AFTER you make a choice or hit a dead-end so the next agent "
            "skips it. Author, branch, short SHA, and timestamp are auto-stamped."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": _ENTRY_TYPES,
                    "description": (
                        "Entry kind. 'deadend' (tried & rejected) is the highest-value "
                        "signal; 'decision' a choice made; 'attempt' in-progress; 'note' context."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": "One-line, skimmable summary of the decision/dead-end/etc.",
                },
                "why": {
                    "type": "string",
                    "description": "The rationale — the whole point of the log.",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Paths this entry is about (a path may pin a line: 'file.py:40-80'). "
                        "Used by brief/blame to decide relevance."
                    ),
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Free-form labels for filtering.",
                },
                "ref": {
                    "type": "string",
                    "description": "Linked issue/PR reference (e.g. '#42' or a URL).",
                },
            },
            "required": ["type", "summary"],
            "additionalProperties": False,
        },
        handler=_tool_add,
    ),
    Tool(
        name="shiplog_brief",
        description=(
            "Token-efficient digest of this repo's ship-log to drop into context "
            "BEFORE editing: dead-ends first (what NOT to redo), then decisions, "
            "prioritizing entries touching the given files (default: the working tree)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Paths to focus on. Omit to use the repo's working tree.",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"Max entries in the digest (default {DEFAULT_BUDGET}; 0 = no cap)."
                    ),
                },
            },
            "additionalProperties": False,
        },
        handler=_tool_brief,
    ),
    Tool(
        name="shiplog_ls",
        description=(
            "List this repo's ship-log entries newest-first, with optional filters "
            "(AND-combined). Returns structured JSON entries for programmatic use."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": _ENTRY_TYPES,
                    "description": "Only entries of this type.",
                },
                "tag": {"type": "string", "description": "Only entries carrying this tag."},
                "file": {
                    "type": "string",
                    "description": (
                        "Only entries referencing this path (suffix match, e.g. 'cli.py')."
                    ),
                },
                "since": {
                    "type": "string",
                    "description": "Only entries at/after a time: relative (7d, 24h) or ISO date.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Show at most N entries (0 = no limit).",
                },
            },
            "additionalProperties": False,
        },
        handler=_tool_ls,
    ),
]

_TOOLS_BY_NAME: dict[str, Tool] = {t.name: t for t in TOOLS}


def tool_descriptors() -> list[dict[str, Any]]:
    """Return the ``tools/list`` payload: each tool's name/description/inputSchema."""
    return [
        {
            "name": t.name,
            "description": t.description,
            "inputSchema": t.input_schema,
        }
        for t in TOOLS
    ]


# --------------------------------------------------------------------------- #
# JSON-RPC dispatch (pure; unit-testable)
# --------------------------------------------------------------------------- #


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON-RPC success envelope."""
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    """Build a JSON-RPC error envelope."""
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _handle_initialize(_params: dict[str, Any]) -> dict[str, Any]:
    """Reply to ``initialize`` with our capabilities + server info."""
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": __version__},
        "instructions": (
            "ship-log MCP: call shiplog_brief BEFORE editing (read dead-ends/decisions), "
            "and shiplog_add AFTER deciding or hitting a dead-end. shiplog_ls lists entries. "
            "The server operates on the repo it was launched in."
        ),
    }


def _handle_tools_call(params: dict[str, Any]) -> dict[str, Any]:
    """Run a tool by name and shape its MCP ``tools/call`` result.

    On a tool-level failure we return an MCP result with ``isError: true`` (per the
    spec, tool errors are reported in-band so the model can see them) rather than a
    transport-level JSON-RPC error.
    """
    name = params.get("name")
    if not name or name not in _TOOLS_BY_NAME:
        known = ", ".join(_TOOLS_BY_NAME)
        raise McpError(INVALID_PARAMS, f"unknown tool {name!r}; available: {known}")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise McpError(INVALID_PARAMS, "'arguments' must be an object.")

    tool = _TOOLS_BY_NAME[name]
    try:
        text, structured = tool.handler(arguments)
    except McpError as exc:
        # Surface tool errors in-band so the agent sees the guidance.
        return {
            "content": [{"type": "text", "text": f"error: {exc.message}"}],
            "isError": True,
        }
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured,
        "isError": False,
    }


def dispatch(request: Any) -> dict[str, Any] | None:
    """Handle one decoded JSON-RPC message; return a response dict or ``None``.

    ``None`` is returned for notifications (messages without an ``id``), which must
    not receive a reply. Malformed requests and unknown methods produce a proper
    JSON-RPC error envelope. This function is pure (no I/O), so the whole protocol
    surface is unit-testable.
    """
    if not isinstance(request, dict):
        return _error(None, INVALID_REQUEST, "request must be a JSON object")

    request_id = request.get("id")
    is_notification = "id" not in request
    method = request.get("method")

    # Notifications (e.g. notifications/initialized, notifications/cancelled) get
    # no response at all.
    if is_notification:
        return None

    if not isinstance(method, str) or not method:
        return _error(request_id, INVALID_REQUEST, "missing or invalid 'method'")

    params = request.get("params") or {}
    if not isinstance(params, dict):
        return _error(request_id, INVALID_PARAMS, "'params' must be an object")

    try:
        if method == "initialize":
            return _result(request_id, _handle_initialize(params))
        if method == "ping":
            return _result(request_id, {})
        if method == "tools/list":
            return _result(request_id, {"tools": tool_descriptors()})
        if method == "tools/call":
            return _result(request_id, _handle_tools_call(params))
        return _error(request_id, METHOD_NOT_FOUND, f"method not found: {method}")
    except McpError as exc:
        return _error(request_id, exc.code, exc.message)
    except Exception as exc:  # pragma: no cover - defensive catch-all
        return _error(request_id, INTERNAL_ERROR, f"internal error: {exc}")


# --------------------------------------------------------------------------- #
# stdio transport loop
# --------------------------------------------------------------------------- #


def serve(
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Run the stdio MCP server until stdin closes (EOF). Returns an exit code.

    Transport: newline-delimited JSON-RPC. Each inbound line is one request; each
    response is written as one compact JSON line + ``\\n`` and flushed immediately so
    the client isn't left waiting on a buffer. Blank lines are ignored. Anything that
    fails to parse as JSON yields a JSON-RPC parse error (id ``null``). Stdout carries
    *only* protocol messages; status/errors go to stderr.
    """
    fin = stdin or sys.stdin
    fout = stdout or sys.stdout
    ferr = stderr or sys.stderr

    print(
        f"ship-log MCP server v{__version__} ready on stdio "
        f"(protocol {PROTOCOL_VERSION}). Reading JSON-RPC from stdin…",
        file=ferr,
        flush=True,
    )

    for raw in fin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            _write(fout, _error(None, PARSE_ERROR, f"invalid JSON: {exc}"))
            continue
        response = dispatch(message)
        if response is not None:
            _write(fout, response)

    return 0


def _write(fout: IO[str], message: dict[str, Any]) -> None:
    """Write one JSON-RPC message as a single newline-terminated line, then flush.

    ``ensure_ascii=False`` keeps unicode in summaries intact; ``separators`` keeps the
    line compact. The MCP stdio framing forbids embedded newlines, and ``json.dumps``
    never emits them for our data, so one ``write`` per message is correct.
    """
    fout.write(
        json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    fout.flush()
