from __future__ import annotations

import sqlite3
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

import pytest

from flowsight.store import (
    SCHEMA_VERSION,
    EnqueueResult,
    FakeEvent,
    SQLiteWALWriter,
    StorageError,
    StorageErrorCode,
    UnsupportedSchemaError,
    WriterState,
    WriterTimeoutError,
)


def _event_ids(database_path: Path) -> list[str]:
    connection = sqlite3.connect(database_path)
    try:
        rows = connection.execute("SELECT event_id FROM fake_events ORDER BY event_id").fetchall()
    finally:
        connection.close()
    return [row[0] for row in rows]


def _blocking_connection_factory(
    write_started: threading.Event,
    release_write: threading.Event,
):
    class BlockingConnection(sqlite3.Connection):
        blocked = False

        def execute(self, sql, parameters=(), /):
            if sql.lstrip().upper().startswith("INSERT INTO FAKE_EVENTS") and not self.blocked:
                self.blocked = True
                write_started.set()
                if not release_write.wait(2.0):
                    raise sqlite3.OperationalError("fixture write release timed out")
            return super().execute(sql, parameters)

    def connect(database_path: Path) -> sqlite3.Connection:
        return sqlite3.connect(database_path, factory=BlockingConnection)

    return connect


def test_enables_wal_versions_schema_and_reopens_without_data_loss(tmp_path: Path) -> None:
    database_path = tmp_path / "events.sqlite3"
    first_writer = SQLiteWALWriter(database_path, capacity=4)
    assert first_writer.enqueue(FakeEvent("event-1", '{"value":1}')) is EnqueueResult.ACCEPTED
    first_writer.close(timeout=2.0)

    connection = sqlite3.connect(database_path)
    try:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()

    assert journal_mode == "wal"
    assert schema_version == SCHEMA_VERSION

    second_writer = SQLiteWALWriter(database_path, capacity=4)
    assert second_writer.enqueue(FakeEvent("event-2", '{"value":2}')) is EnqueueResult.ACCEPTED
    second_writer.flush(timeout=2.0)
    second_writer.close(timeout=2.0)

    assert _event_ids(database_path) == ["event-1", "event-2"]


def test_rejects_a_newer_schema_without_downgrading_it(tmp_path: Path) -> None:
    database_path = tmp_path / "newer.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("CREATE TABLE newer_marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO newer_marker VALUES ('preserve-me')")
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(UnsupportedSchemaError, match="newer"):
        SQLiteWALWriter(database_path)

    connection = sqlite3.connect(database_path)
    try:
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        marker = connection.execute("SELECT value FROM newer_marker").fetchone()[0]
        fake_events_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='fake_events'"
        ).fetchone()
    finally:
        connection.close()

    assert schema_version == SCHEMA_VERSION + 1
    assert marker == "preserve-me"
    assert fake_events_table is None


@pytest.mark.parametrize("existing_version", [0, SCHEMA_VERSION])
def test_rejects_an_incompatible_existing_table_without_promoting_the_schema(
    tmp_path: Path,
    existing_version: int,
) -> None:
    database_path = tmp_path / f"malformed-{existing_version}.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("CREATE TABLE fake_events (wrong_column TEXT)")
        connection.execute(f"PRAGMA user_version={existing_version}")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(StorageError):
        SQLiteWALWriter(database_path)

    connection = sqlite3.connect(database_path)
    try:
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        columns = connection.execute("PRAGMA table_info(fake_events)").fetchall()
    finally:
        connection.close()

    assert schema_version == existing_version
    assert [column[1] for column in columns] == ["wrong_column"]


def test_initialization_failure_is_reported_synchronously_without_raw_error_text(
    tmp_path: Path,
) -> None:
    factory_called = threading.Event()

    def fail_connection(_path: Path) -> sqlite3.Connection:
        factory_called.set()
        raise sqlite3.OperationalError("raw injected startup detail")

    with pytest.raises(StorageError) as startup_error:
        SQLiteWALWriter(tmp_path / "startup-failure.sqlite3", _connection_factory=fail_connection)

    assert factory_called.is_set()
    assert "raw injected" not in str(startup_error.value)


def test_concurrent_producers_use_one_writer_connection_and_lose_no_events(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "concurrent.sqlite3"
    identity_lock = threading.Lock()
    connection_thread_ids: set[int] = set()
    sql_thread_ids: set[int] = set()
    producer_thread_ids: set[int] = set()
    connection_count = 0

    class TrackingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            if sql.lstrip().upper().startswith("INSERT INTO FAKE_EVENTS"):
                with identity_lock:
                    sql_thread_ids.add(threading.get_ident())
            return super().execute(sql, parameters)

        def commit(self) -> None:
            with identity_lock:
                sql_thread_ids.add(threading.get_ident())
            return super().commit()

    def connect(path: Path) -> sqlite3.Connection:
        nonlocal connection_count
        with identity_lock:
            connection_count += 1
            connection_thread_ids.add(threading.get_ident())
        return sqlite3.connect(path, factory=TrackingConnection)

    writer = SQLiteWALWriter(database_path, capacity=100, _connection_factory=connect)

    def produce(producer_index: int) -> list[EnqueueResult]:
        with identity_lock:
            producer_thread_ids.add(threading.get_ident())
        return [
            writer.enqueue(
                FakeEvent(
                    f"event-{producer_index:02d}-{event_index:02d}",
                    f'{{"producer":{producer_index},"event":{event_index}}}',
                )
            )
            for event_index in range(10)
        ]

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(produce, producer_index) for producer_index in range(10)]
        results = [result for future in futures for result in future.result(timeout=3.0)]

    writer.flush(timeout=3.0)
    health = writer.health
    writer.close(timeout=3.0)

    assert results == [EnqueueResult.ACCEPTED] * 100
    assert health.accepted_count == 100
    assert health.committed_count == 100
    assert health.uncommitted_count == 0
    assert health.dropped_count == 0
    assert connection_count == 1
    assert connection_thread_ids == {health.writer_thread_id}
    assert sql_thread_ids == {health.writer_thread_id}
    assert producer_thread_ids.isdisjoint(sql_thread_ids)
    assert len(_event_ids(database_path)) == 100
    assert len(set(_event_ids(database_path))) == 100


def test_bounded_queue_reports_full_without_blocking_or_persisting_rejected_event(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "bounded.sqlite3"
    write_started = threading.Event()
    release_write = threading.Event()
    writer = SQLiteWALWriter(
        database_path,
        capacity=2,
        _connection_factory=_blocking_connection_factory(write_started, release_write),
    )

    assert writer.enqueue(FakeEvent("in-flight", "{}")) is EnqueueResult.ACCEPTED
    assert write_started.wait(2.0)
    assert writer.enqueue(FakeEvent("queued", "{}")) is EnqueueResult.ACCEPTED
    executor = ThreadPoolExecutor(max_workers=1)
    full_result = executor.submit(writer.enqueue, FakeEvent("rejected", "{}"))
    try:
        assert full_result.result(timeout=0.5) is EnqueueResult.FULL
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    blocked_health = writer.health
    assert blocked_health.queue_depth == 1
    assert blocked_health.in_flight_count == 1
    assert blocked_health.uncommitted_count == blocked_health.capacity == 2
    assert blocked_health.dropped_count == 1

    release_write.set()
    writer.close(timeout=2.0)

    assert _event_ids(database_path) == ["in-flight", "queued"]


def test_close_timeout_stops_acceptance_and_a_later_close_finishes_the_flush(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "close-timeout.sqlite3"
    write_started = threading.Event()
    release_write = threading.Event()
    writer = SQLiteWALWriter(
        database_path,
        capacity=2,
        _connection_factory=_blocking_connection_factory(write_started, release_write),
    )

    assert writer.enqueue(FakeEvent("accepted-before-close", "{}")) is EnqueueResult.ACCEPTED
    assert write_started.wait(2.0)

    with pytest.raises(WriterTimeoutError, match="close timed out"):
        writer.close(timeout=0.0)

    assert writer.health.state is WriterState.CLOSING
    assert writer.enqueue(FakeEvent("after-close", "{}")) is EnqueueResult.CLOSED

    release_write.set()
    writer.close(timeout=2.0)
    writer.close(timeout=0.0)

    assert writer.health.state is WriterState.CLOSED
    assert _event_ids(database_path) == ["accepted-before-close"]


def test_concurrent_close_and_enqueue_persist_exactly_the_accepted_set(tmp_path: Path) -> None:
    database_path = tmp_path / "close-race.sqlite3"
    writer = SQLiteWALWriter(database_path, capacity=10)
    barrier = threading.Barrier(11)

    def race_enqueue(index: int) -> tuple[str, EnqueueResult]:
        barrier.wait(timeout=2.0)
        event_id = f"event-{index:02d}"
        return event_id, writer.enqueue(FakeEvent(event_id, "{}"))

    def race_close() -> None:
        barrier.wait(timeout=2.0)
        writer.close(timeout=2.0)

    with ThreadPoolExecutor(max_workers=11) as executor:
        enqueue_futures = [executor.submit(race_enqueue, index) for index in range(10)]
        close_future = executor.submit(race_close)
        results = [future.result(timeout=3.0) for future in enqueue_futures]
        close_future.result(timeout=3.0)

    accepted_ids = sorted(
        event_id for event_id, result in results if result is EnqueueResult.ACCEPTED
    )
    assert all(result in {EnqueueResult.ACCEPTED, EnqueueResult.CLOSED} for _, result in results)
    assert _event_ids(database_path) == accepted_ids


def test_close_cannot_cross_an_enqueue_inside_the_acceptance_critical_section(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "deterministic-close-race.sqlite3"
    writer = SQLiteWALWriter(database_path, capacity=2)
    append_entered = threading.Event()
    release_append = threading.Event()
    close_called = threading.Event()

    class BlockingDeque(deque[FakeEvent]):
        def append(self, event: FakeEvent) -> None:
            append_entered.set()
            if not release_append.wait(2.0):
                raise AssertionError("fixture append release timed out")
            super().append(event)

    with writer._condition:
        writer._pending = BlockingDeque()

    def close_writer() -> None:
        close_called.set()
        writer.close(timeout=2.0)

    with ThreadPoolExecutor(max_workers=2) as executor:
        enqueue_future = executor.submit(writer.enqueue, FakeEvent("linearized", "{}"))
        assert append_entered.wait(1.0)
        close_future = executor.submit(close_writer)
        assert close_called.wait(1.0)
        try:
            with pytest.raises(FutureTimeoutError):
                close_future.result(timeout=0.05)
        finally:
            release_append.set()
        assert enqueue_future.result(timeout=1.0) is EnqueueResult.ACCEPTED
        close_future.result(timeout=2.0)

    assert writer.enqueue(FakeEvent("too-late", "{}")) is EnqueueResult.CLOSED
    assert _event_ids(database_path) == ["linearized"]


@pytest.mark.parametrize("failure_mode", ["execute", "commit"])
def test_injected_sqlite_failures_are_visible_to_callers_and_health(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    database_path = tmp_path / f"failure-{failure_mode}.sqlite3"

    class FailingConnection(sqlite3.Connection):
        event_inserted = False

        def execute(self, sql, parameters=(), /):
            if sql.lstrip().upper().startswith("INSERT INTO FAKE_EVENTS"):
                if failure_mode == "execute":
                    raise sqlite3.OperationalError("raw injected execute detail")
                cursor = super().execute(sql, parameters)
                self.event_inserted = True
                return cursor
            return super().execute(sql, parameters)

        def commit(self) -> None:
            if failure_mode == "commit" and self.event_inserted:
                raise sqlite3.OperationalError("raw injected commit detail")
            return super().commit()

    def connect(path: Path) -> sqlite3.Connection:
        return sqlite3.connect(path, factory=FailingConnection)

    writer = SQLiteWALWriter(database_path, capacity=2, _connection_factory=connect)
    assert writer.enqueue(FakeEvent("will-fail", '{"secret":"already-fake"}')) is (
        EnqueueResult.ACCEPTED
    )

    with pytest.raises(StorageError) as flush_error:
        writer.flush(timeout=2.0)

    health = writer.health
    assert health.state is WriterState.FAILED
    assert health.accepted_count == 1
    assert health.committed_count == 0
    assert health.uncommitted_count == 1
    assert health.error_count == 1
    assert health.last_error_code is StorageErrorCode.WRITE_FAILED
    assert writer.enqueue(FakeEvent("after-failure", "{}")) is EnqueueResult.FAILED
    assert "raw injected" not in str(flush_error.value)

    with pytest.raises(StorageError):
        writer.close(timeout=2.0)

    assert _event_ids(database_path) == []


def test_rollback_failure_is_counted_and_exposed(tmp_path: Path) -> None:
    database_path = tmp_path / "rollback-failure.sqlite3"

    class RollbackFailingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            if sql.lstrip().upper().startswith("INSERT INTO FAKE_EVENTS"):
                raise sqlite3.OperationalError("raw injected write detail")
            return super().execute(sql, parameters)

        def rollback(self) -> None:
            raise sqlite3.OperationalError("raw injected rollback detail")

    def connect(path: Path) -> sqlite3.Connection:
        return sqlite3.connect(path, factory=RollbackFailingConnection)

    writer = SQLiteWALWriter(database_path, _connection_factory=connect)
    assert writer.enqueue(FakeEvent("will-fail", "{}")) is EnqueueResult.ACCEPTED

    with pytest.raises(StorageError) as flush_error:
        writer.flush(timeout=2.0)

    assert writer.health.error_count == 2
    assert writer.health.last_error_code is StorageErrorCode.ROLLBACK_FAILED
    assert "raw injected" not in str(flush_error.value)
    with pytest.raises(StorageError):
        writer.close(timeout=2.0)


def test_connection_close_failure_is_visible_to_caller_and_health(tmp_path: Path) -> None:
    database_path = tmp_path / "close-failure.sqlite3"

    class CloseFailingConnection(sqlite3.Connection):
        def close(self) -> None:
            super().close()
            raise sqlite3.OperationalError("raw injected close detail")

    def connect(path: Path) -> sqlite3.Connection:
        return sqlite3.connect(path, factory=CloseFailingConnection)

    writer = SQLiteWALWriter(database_path, _connection_factory=connect)
    with pytest.raises(StorageError) as close_error:
        writer.close(timeout=2.0)

    assert writer.health.state is WriterState.FAILED
    assert writer.health.error_count == 1
    assert writer.health.last_error_code is StorageErrorCode.CLOSE_FAILED
    assert writer.enqueue(FakeEvent("after-close-error", "{}")) is EnqueueResult.FAILED
    assert "raw injected" not in str(close_error.value)


def test_fake_events_reject_raw_objects_and_string_subclasses() -> None:
    class UserString(str):
        pass

    with pytest.raises(TypeError, match="event_id"):
        FakeEvent(UserString("event"), "{}")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="payload_json"):
        FakeEvent("event", object())  # type: ignore[arg-type]
