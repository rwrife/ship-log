"""Tests for the M2 store + models backbone.

Covers the issue #2 acceptance checklist:
- round-trip serialize (Entry <-> JSON line)
- append + read N entries
- two near-simultaneous appends don't corrupt the JSONL
"""

from __future__ import annotations

import json
import multiprocessing as mp
from pathlib import Path

import pytest

from shiplog.models import (
    SCHEMA_VERSION,
    Entry,
    EntryType,
    generate_id,
)
from shiplog.store import Store

# --------------------------------------------------------------------------
# models: id generation
# --------------------------------------------------------------------------


def test_generate_id_shape():
    eid = generate_id()
    date_part, _, suffix = eid.partition("-")
    assert len(date_part) == 6 and date_part.isdigit()
    assert len(suffix) == 6
    # Crockford base32 alphabet: no I, L, O, U.
    assert all(c in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" for c in suffix)


def test_generate_id_unique_enough():
    ids = {generate_id() for _ in range(2000)}
    # Collisions in 2000 draws of 32**6 space should be effectively nil.
    assert len(ids) == 2000


# --------------------------------------------------------------------------
# models: entry type coercion
# --------------------------------------------------------------------------


def test_entry_type_coerce_from_string():
    assert EntryType.coerce("decision") is EntryType.DECISION
    assert EntryType.coerce("DEADEND") is EntryType.DEADEND
    assert EntryType.coerce(EntryType.NOTE) is EntryType.NOTE


def test_entry_type_coerce_rejects_unknown():
    with pytest.raises(ValueError):
        EntryType.coerce("nope")


def test_entry_accepts_plain_string_type():
    e = Entry(summary="x", type="attempt")
    assert e.type is EntryType.ATTEMPT


# --------------------------------------------------------------------------
# models: round-trip serialization
# --------------------------------------------------------------------------


def test_round_trip_full_entry():
    original = Entry(
        summary="Switched JSON lib to orjson",
        type=EntryType.DEADEND,
        author="rwrife",
        branch="main",
        sha="deadbee",
        why="orjson is faster but breaks on our datetimes; reverted",
        files=["shiplog/store.py", "shiplog/models.py"],
        tags=["perf", "json"],
        ref="#42",
    )
    line = original.to_json()

    # Single line, valid JSON, no embedded newline.
    assert "\n" not in line
    decoded = json.loads(line)
    assert decoded["type"] == "deadend"
    assert decoded["schema_version"] == SCHEMA_VERSION

    restored = Entry.from_json(line)
    assert restored == original


def test_round_trip_minimal_entry():
    e = Entry(summary="just a note")
    restored = Entry.from_json(e.to_json())
    assert restored == e
    assert restored.type is EntryType.NOTE
    assert restored.files == [] and restored.tags == []


def test_from_dict_ignores_unknown_keys():
    payload = Entry(summary="hi").to_dict()
    payload["future_field"] = "ignored"
    e = Entry.from_dict(payload)
    assert e.summary == "hi"


def test_from_json_requires_summary():
    with pytest.raises(ValueError):
        Entry.from_json(json.dumps({"type": "note"}))


def test_from_json_rejects_non_object():
    with pytest.raises(ValueError):
        Entry.from_json(json.dumps([1, 2, 3]))


def test_unicode_survives_round_trip():
    e = Entry(summary="Ahoy ⚓ café — naïve", why="日本語 ok")
    assert Entry.from_json(e.to_json()) == e


# --------------------------------------------------------------------------
# store: append + read N
# --------------------------------------------------------------------------


def test_read_missing_log_is_empty(tmp_path: Path):
    store = Store(tmp_path / ".shiplog" / "log.jsonl")
    assert store.exists() is False
    assert store.read_all() == []
    assert store.count() == 0


def test_append_and_read_n(tmp_path: Path):
    store = Store.for_repo(tmp_path)
    made = [Entry(summary=f"entry {i}", tags=[f"t{i}"]) for i in range(25)]
    for e in made:
        store.append(e)

    assert store.exists()
    read = store.read_all()
    assert len(read) == 25
    assert store.count() == 25
    # Order preserved (oldest first) and content intact.
    assert [e.summary for e in read] == [e.summary for e in made]
    assert read == made


def test_append_many_matches_append(tmp_path: Path):
    store = Store(tmp_path / "log.jsonl")
    batch = [Entry(summary=f"b{i}") for i in range(10)]
    n = store.append_many(batch)
    assert n == 10
    assert store.read_all() == batch


def test_iter_entries_streams(tmp_path: Path):
    store = Store(tmp_path / "log.jsonl")
    store.append_many([Entry(summary=f"s{i}") for i in range(5)])
    summaries = [e.summary for e in store.iter_entries()]
    assert summaries == [f"s{i}" for i in range(5)]


def test_blank_lines_skipped(tmp_path: Path):
    p = tmp_path / "log.jsonl"
    store = Store(p)
    store.append(Entry(summary="one"))
    # Inject stray blank lines like a sloppy merge might.
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("\n   \n")
    store.append(Entry(summary="two"))
    read = store.read_all()
    assert [e.summary for e in read] == ["one", "two"]


def test_every_line_is_valid_json(tmp_path: Path):
    store = Store(tmp_path / "log.jsonl")
    store.append_many([Entry(summary=f"x{i}") for i in range(8)])
    raw = (tmp_path / "log.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(raw) == 8
    for line in raw:
        json.loads(line)  # must not raise


# --------------------------------------------------------------------------
# store: concurrent appends don't corrupt
# --------------------------------------------------------------------------


def _writer(path_str: str, worker: int, count: int) -> None:
    """Child-process body: hammer the same log with `count` appends."""
    store = Store(path_str)
    for i in range(count):
        store.append(
            Entry(summary=f"w{worker}-{i}", author=f"worker-{worker}", tags=["concurrent"])
        )


def test_concurrent_appends_do_not_corrupt(tmp_path: Path):
    """Two+ near-simultaneous writers must not interleave/corrupt lines."""
    path = tmp_path / ".shiplog" / "log.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path_str = str(path)

    workers = 4
    per_worker = 60
    ctx = mp.get_context()  # default start method for the platform
    procs = [
        ctx.Process(target=_writer, args=(path_str, w, per_worker))
        for w in range(workers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0

    # Every line must parse, and we must see exactly workers*per_worker entries.
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == workers * per_worker
    parsed = [json.loads(line) for line in raw_lines]  # no corruption => no raise

    store = Store(path_str)
    entries = store.read_all()
    assert len(entries) == workers * per_worker

    # Every (worker, i) pair shows up exactly once — nothing lost or duplicated.
    expected = {f"w{w}-{i}" for w in range(workers) for i in range(per_worker)}
    got = {e.summary for e in entries}
    assert got == expected
    assert len(parsed) == len(expected)
