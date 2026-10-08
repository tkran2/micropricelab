"""Append-only, committed-before-publication SQLite journal with a hash chain."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

FORMAT = "mdpipe-journal-v1"
ZERO_HASH = "0" * 64

class JournalError(RuntimeError):
    pass

def canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)

def row_hash(previous: str, payload: str) -> str:
    return hashlib.sha256(bytes.fromhex(previous) + payload.encode("utf-8")).hexdigest()

class Journal:
    def __init__(self, path: str | Path, product: str, max_bytes: int = 512 * 1024 * 1024):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError as exc:
            raise JournalError(f"journal already exists: {self.path}; choose a new output path") from exc
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA wal_autocheckpoint=256")
        self.connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, payload TEXT NOT NULL, previous_hash TEXT NOT NULL, hash TEXT NOT NULL)")
        self.connection.commit()
        self.last_hash = ZERO_HASH
        self.count = 0
        self.max_bytes = max_bytes
        self.append({"kind": "metadata", "format": FORMAT, "product": product,
                     "scale": 100000000, "created_ns": time.time_ns()})

    def append(self, event: dict) -> int:
        payload = canonical(event)
        size = sum(p.stat().st_size for p in (
            self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")
        ) if p.exists())
        if size + len(payload.encode()) * 2 + 8192 > self.max_bytes:
            raise JournalError("journal storage budget exhausted")
        checksum = row_hash(self.last_hash, payload)
        try:
            with self.connection:
                self.connection.execute("INSERT INTO events VALUES (?, ?, ?, ?)",
                                        (self.count + 1, payload, self.last_hash, checksum))
        except (sqlite3.Error, OSError) as exc:
            raise JournalError("journal commit failed") from exc
        self.count += 1
        self.last_hash = checksum
        return self.count

    def close(self):
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.connection.close()

class Recorder:
    def __init__(self, journal: Journal):
        self.journal = journal
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="journal")
        self.failed = False

    async def append(self, event: dict):
        if self.failed:
            raise JournalError("recorder unavailable after a failed commit")
        future = asyncio.get_running_loop().run_in_executor(self.worker, self.journal.append, event)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # Finish a started commit before releasing its ordering position.
            try:
                await future
            except Exception:
                self.failed = True
                raise
            raise
        except Exception:
            self.failed = True
            raise

    async def close(self):
        await asyncio.get_running_loop().run_in_executor(self.worker, self.journal.close)
        self.worker.shutdown(wait=True)

def records(path: str | Path):
    """Stream and verify committed rows; never silently skip a damaged record."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    previous = ZERO_HASH
    expected_id = 1
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise JournalError("SQLite integrity check failed")
        for row_id, payload, previous_hash, checksum in connection.execute(
            "SELECT id, payload, previous_hash, hash FROM events ORDER BY id"
        ):
            if row_id != expected_id or previous_hash != previous or row_hash(previous, payload) != checksum:
                raise JournalError(f"journal hash/order mismatch at row {row_id}")
            try:
                event = json.loads(payload)
            except ValueError as exc:
                raise JournalError("invalid journal JSON") from exc
            if not isinstance(event, dict):
                raise JournalError("invalid journal record")
            if expected_id == 1 and (event.get("kind") != "metadata" or event.get("format") != FORMAT):
                raise JournalError("unsupported journal format")
            yield row_id, event
            expected_id += 1
            previous = checksum
        if expected_id == 1:
            raise JournalError("empty journal")
    finally:
        connection.close()
