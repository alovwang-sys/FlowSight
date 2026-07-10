"""Bounded single-writer SQLite WAL queue used by the Phase 0 trial."""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

SCHEMA_VERSION = 1

type ConnectionFactory = Callable[[Path], sqlite3.Connection]


class WriterState(StrEnum):
    """Lifecycle state for one writer instance."""

    STARTING = "starting"
    RUNNING = "running"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


class EnqueueResult(StrEnum):
    """Non-blocking result returned by :meth:`SQLiteWALWriter.enqueue`."""

    ACCEPTED = "accepted"
    FULL = "full"
    CLOSED = "closed"
    FAILED = "failed"


class StorageErrorCode(StrEnum):
    """Stable, non-sensitive health code for storage failures."""

    STARTUP_FAILED = "startup_failed"
    SCHEMA_TOO_NEW = "schema_too_new"
    WRITE_FAILED = "write_failed"
    ROLLBACK_FAILED = "rollback_failed"
    CLOSE_FAILED = "close_failed"
    INTERNAL_FAILED = "internal_failed"


class StorageError(RuntimeError):
    """A SQLite writer operation could not satisfy its durability contract."""


class UnsupportedSchemaError(StorageError):
    """The database was created by a newer FlowSight schema."""


class WriterTimeoutError(StorageError):
    """A bounded writer lifecycle operation exceeded its timeout."""


@dataclass(frozen=True, slots=True)
class FakeEvent:
    """A deliberately tiny, already-serialized event for the storage trial."""

    event_id: str
    payload_json: str

    def __post_init__(self) -> None:
        if type(self.event_id) is not str or not self.event_id:
            raise TypeError("event_id must be a non-empty built-in str")
        if type(self.payload_json) is not str:
            raise TypeError("payload_json must be a built-in str")


@dataclass(frozen=True, slots=True)
class WriterHealth:
    """Thread-safe immutable snapshot of writer health."""

    state: WriterState
    capacity: int
    queue_depth: int
    in_flight_count: int
    accepted_count: int
    committed_count: int
    uncommitted_count: int
    dropped_count: int
    rejected_closed_count: int
    rejected_failed_count: int
    error_count: int
    last_error_code: StorageErrorCode | None
    writer_thread_id: int | None


def _default_connection_factory(database_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(database_path, timeout=5.0)


class SQLiteWALWriter:
    """Serialize fake events through one bounded queue and SQLite connection.

    ``enqueue`` is non-blocking. An ``ACCEPTED`` result means the event crossed
    the writer's acceptance boundary; it becomes durable after ``flush`` or a
    successful ``close``. Capacity includes the event currently being written.
    Construction returns only after initialization succeeds or fails, so it
    never reports a startup timeout while hidden migration work continues.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        capacity: int = 4096,
        _connection_factory: ConnectionFactory | None = None,
    ) -> None:
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("capacity must be a positive built-in int")

        self._database_path = Path(database_path)
        self._capacity = capacity
        self._connection_factory = _connection_factory or _default_connection_factory
        self._condition = threading.Condition()
        self._pending: deque[FakeEvent] = deque()
        self._state = WriterState.STARTING
        self._thread_finished = False
        self._in_flight_count = 0
        self._accepted_count = 0
        self._committed_count = 0
        self._processed_count = 0
        self._dropped_count = 0
        self._rejected_closed_count = 0
        self._rejected_failed_count = 0
        self._error_count = 0
        self._last_error_code: StorageErrorCode | None = None
        self._writer_thread_id: int | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="flowsight-sqlite-writer",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()

        with self._condition:
            state = self._state
            error_code = self._last_error_code
        if state is WriterState.FAILED:
            self._raise_storage_error(error_code)

    @staticmethod
    def _validate_timeout(timeout: float) -> None:
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be a finite non-negative built-in number")

    @staticmethod
    def _deadline(timeout: float) -> float:
        SQLiteWALWriter._validate_timeout(timeout)
        return time.monotonic() + timeout

    @property
    def health(self) -> WriterHealth:
        """Return a consistent health snapshot without exposing SQLite objects."""

        with self._condition:
            return WriterHealth(
                state=self._state,
                capacity=self._capacity,
                queue_depth=len(self._pending),
                in_flight_count=self._in_flight_count,
                accepted_count=self._accepted_count,
                committed_count=self._committed_count,
                uncommitted_count=self._accepted_count - self._committed_count,
                dropped_count=self._dropped_count,
                rejected_closed_count=self._rejected_closed_count,
                rejected_failed_count=self._rejected_failed_count,
                error_count=self._error_count,
                last_error_code=self._last_error_code,
                writer_thread_id=self._writer_thread_id,
            )

    def enqueue(self, event: FakeEvent) -> EnqueueResult:
        """Try to accept one fake event without blocking the caller."""

        if type(event) is not FakeEvent:
            raise TypeError("event must be an exact FakeEvent")

        with self._condition:
            if self._state is WriterState.FAILED:
                self._rejected_failed_count += 1
                return EnqueueResult.FAILED
            if self._state is not WriterState.RUNNING:
                self._rejected_closed_count += 1
                return EnqueueResult.CLOSED
            if self._accepted_count - self._processed_count >= self._capacity:
                self._dropped_count += 1
                return EnqueueResult.FULL

            self._pending.append(event)
            self._accepted_count += 1
            self._condition.notify()
            return EnqueueResult.ACCEPTED

    def flush(self, timeout: float = 2.0) -> None:
        """Wait until every event accepted before this call is committed."""

        deadline = self._deadline(timeout)
        with self._condition:
            target = self._accepted_count
            while self._committed_count < target and self._last_error_code is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WriterTimeoutError("SQLite writer flush timed out")
                self._condition.wait(remaining)
            error_code = self._last_error_code

        if error_code is not None:
            self._raise_storage_error(error_code)

    def close(self, timeout: float = 2.0) -> None:
        """Stop accepting events, drain accepted work, and close SQLite."""

        deadline = self._deadline(timeout)
        with self._condition:
            if self._state in {WriterState.STARTING, WriterState.RUNNING}:
                self._state = WriterState.CLOSING
                self._condition.notify_all()

            while not self._thread_finished:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WriterTimeoutError("SQLite writer close timed out")
                self._condition.wait(remaining)
            error_code = self._last_error_code

        if error_code is not None:
            self._raise_storage_error(error_code)

    def _run(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            with self._condition:
                self._writer_thread_id = threading.get_ident()

            connection = self._connection_factory(self._database_path)
            self._prepare_connection(connection)
            with self._condition:
                if self._state is WriterState.STARTING:
                    self._state = WriterState.RUNNING
                self._ready.set()
                self._condition.notify_all()

            self._write_loop(connection)
        except UnsupportedSchemaError:
            with self._condition:
                self._record_failure_locked(StorageErrorCode.SCHEMA_TOO_NEW)
                self._condition.notify_all()
        except sqlite3.Error:
            with self._condition:
                self._record_failure_locked(StorageErrorCode.STARTUP_FAILED)
                self._condition.notify_all()
        except BaseException:
            with self._condition:
                self._record_failure_locked(StorageErrorCode.INTERNAL_FAILED)
                self._condition.notify_all()
        finally:
            if connection is not None:
                try:
                    connection.close()
                except BaseException:
                    with self._condition:
                        self._record_failure_locked(StorageErrorCode.CLOSE_FAILED)
                        self._condition.notify_all()

            with self._condition:
                if self._last_error_code is None:
                    self._state = WriterState.CLOSED
                else:
                    self._state = WriterState.FAILED
                self._thread_finished = True
                self._ready.set()
                self._condition.notify_all()

    def _write_loop(self, connection: sqlite3.Connection) -> None:
        while True:
            with self._condition:
                while not self._pending and self._state is WriterState.RUNNING:
                    self._condition.wait()
                if self._state is WriterState.FAILED:
                    return
                if not self._pending:
                    if self._state is WriterState.CLOSING:
                        return
                    continue
                event = self._pending.popleft()
                self._in_flight_count = 1

            try:
                connection.execute(
                    "INSERT INTO fake_events (event_id, payload_json) VALUES (?, ?)",
                    (event.event_id, event.payload_json),
                )
                connection.commit()
            except sqlite3.Error:
                rollback_failed = False
                try:
                    connection.rollback()
                except BaseException:
                    rollback_failed = True
                with self._condition:
                    self._processed_count += 1
                    self._in_flight_count = 0
                    self._record_failure_locked(StorageErrorCode.WRITE_FAILED)
                    if rollback_failed:
                        self._record_failure_locked(StorageErrorCode.ROLLBACK_FAILED)
                    self._condition.notify_all()
                return
            else:
                with self._condition:
                    self._processed_count += 1
                    self._committed_count += 1
                    self._in_flight_count = 0
                    self._condition.notify_all()

    @staticmethod
    def _prepare_connection(connection: sqlite3.Connection) -> None:
        version_row = connection.execute("PRAGMA user_version").fetchone()
        if version_row is None or type(version_row[0]) is not int:
            raise sqlite3.DatabaseError("SQLite did not return a schema version")
        version = version_row[0]
        if version > SCHEMA_VERSION:
            raise UnsupportedSchemaError("SQLite schema is newer than this FlowSight writer")

        journal_row = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        if (
            journal_row is None
            or type(journal_row[0]) is not str
            or journal_row[0].lower() != "wal"
        ):
            raise sqlite3.DatabaseError("SQLite WAL mode is unavailable")
        connection.execute("PRAGMA busy_timeout=5000")

        if version == 0:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS fake_events (
                        event_id TEXT PRIMARY KEY NOT NULL,
                        payload_json TEXT NOT NULL
                    )
                    """
                )
                SQLiteWALWriter._validate_fake_events_table(connection)
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                connection.commit()
            except sqlite3.Error:
                connection.rollback()
                raise
            return

        SQLiteWALWriter._validate_fake_events_table(connection)

    @staticmethod
    def _validate_fake_events_table(connection: sqlite3.Connection) -> None:
        table_row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fake_events'"
        ).fetchone()
        if table_row is None:
            raise sqlite3.DatabaseError("SQLite schema version exists without fake_events")
        columns = connection.execute("PRAGMA table_info(fake_events)").fetchall()
        expected_columns = [
            (0, "event_id", "TEXT", 1, None, 1),
            (1, "payload_json", "TEXT", 1, None, 0),
        ]
        if columns != expected_columns:
            raise sqlite3.DatabaseError("SQLite fake_events schema is incompatible")

    def _record_failure_locked(self, error_code: StorageErrorCode) -> None:
        self._state = WriterState.FAILED
        self._error_count += 1
        self._last_error_code = error_code

    @staticmethod
    def _raise_storage_error(error_code: StorageErrorCode | None) -> NoReturn:
        if error_code is StorageErrorCode.SCHEMA_TOO_NEW:
            raise UnsupportedSchemaError("SQLite schema is newer than this FlowSight writer")
        raise StorageError(f"SQLite writer failed ({error_code or 'unknown'})")
