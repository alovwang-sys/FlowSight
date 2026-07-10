"""Independent loopback sidecar used only by the lifecycle spike."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import queue
import secrets
import socket
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, Self

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.types import Message, Receive, Scope, Send

from .state import LOOPBACK_HOST, PROTOCOL_VERSION, SidecarState, StateStore

MAX_EVENTS_PER_BATCH: Final = 128
MAX_PAYLOAD_JSON_BYTES: Final = 16 * 1024
MAX_BATCH_BYTES: Final = 1024 * 1024
WRITER_WAIT_SECONDS: Final = 2.0

ConnectionFactory = Callable[[Path], sqlite3.Connection]
CommitGate = Callable[[], None]


class WriterError(RuntimeError):
    """A bounded writer operation failed with a safe error code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"spike writer failed ({code})")


class LeaseError(RuntimeError):
    """A producer does not own the active lease."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"producer lease failed ({code})")


class PortBindError(RuntimeError):
    """A requested loopback port could not be bound atomically."""


@dataclass(frozen=True, slots=True)
class WireEvent:
    event_id: str
    producer_seq: int
    event_type: str
    schema_version: int
    timestamp_ns: int
    payload_json: str
    request_trace_id: str | None = None
    otel_trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None


@dataclass(frozen=True, slots=True)
class BatchResult:
    committed_event_ids: tuple[str, ...]
    duplicate_event_ids: tuple[str, ...]


@dataclass(slots=True)
class _BatchCommand:
    project_id: str
    producer_id: str
    batch_id: str
    events: tuple[WireEvent, ...]
    done: threading.Event = field(default_factory=threading.Event)
    result: BatchResult | None = None
    error_code: str | None = None


@dataclass(slots=True)
class _FlushCommand:
    done: threading.Event = field(default_factory=threading.Event)
    error_code: str | None = None


@dataclass(slots=True)
class _StopCommand:
    done: threading.Event = field(default_factory=threading.Event)


WriterCommand = _BatchCommand | _FlushCommand | _StopCommand


def _default_connection_factory(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path, timeout=2.0)


class EventWriter:
    """One bounded command queue and one SQLite-owning writer thread.

    ``before_commit`` is deliberately injectable.  A test can hold that gate,
    prove the HTTP future has not returned, release it, and then observe the ACK.
    The result event is set only after ``connection.commit()`` succeeds.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        project_id: str,
        startup_id: str,
        capacity: int = 64,
        before_commit: CommitGate | None = None,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("capacity must be a positive built-in int")
        self.database_path = Path(database_path)
        self.project_id = project_id
        self.startup_id = startup_id
        self.capacity = capacity
        self._before_commit = before_commit
        self._connection_factory = connection_factory or _default_connection_factory
        self._commands: queue.Queue[WriterCommand] = queue.Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._thread_finished = threading.Event()
        self._closed = False
        self._stop_command: _StopCommand | None = None
        self._failed_code: str | None = None
        self._writer_thread_id: int | None = None
        self._committed_count = 0
        self._duplicate_count = 0
        self._error_count = 0
        self._rejected_count = 0
        self._timeout_count = 0
        self._last_queue_error_code: str | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="flowsight-spike-sidecar-writer",
            daemon=True,
        )
        self._thread.start()
        # The enclosing sidecar subprocess is the bounded startup boundary.  Do
        # not return an error while a migration thread is still hidden and live;
        # the SDK kills the entire child if its startup deadline expires.
        self._ready.wait()
        with self._lock:
            failure = self._failed_code
        if failure is not None:
            self._thread_finished.wait()
            raise WriterError(failure)

    @property
    def health(self) -> dict[str, object]:
        with self._lock:
            return {
                "writer_thread_id": self._writer_thread_id,
                "writer_queue_capacity": self.capacity,
                "writer_queue_depth": self._commands.qsize(),
                "committed_event_count": self._committed_count,
                "duplicate_event_count": self._duplicate_count,
                "storage_error_count": self._error_count,
                "storage_error_code": self._failed_code,
                "writer_rejected_count": self._rejected_count,
                "writer_timeout_count": self._timeout_count,
                "last_writer_queue_error_code": self._last_queue_error_code,
            }

    def submit(
        self,
        *,
        project_id: str,
        producer_id: str,
        batch_id: str,
        events: tuple[WireEvent, ...],
        timeout: float = WRITER_WAIT_SECONDS,
    ) -> BatchResult:
        command = _BatchCommand(project_id, producer_id, batch_id, events)
        self._put_nowait(command)
        if not command.done.wait(timeout):
            self._record_queue_error("WRITER_TIMEOUT", timeout=True)
            raise WriterError("WRITER_TIMEOUT")
        if command.error_code is not None:
            raise WriterError(command.error_code)
        if command.result is None:
            raise WriterError("WRITER_INTERNAL_ERROR")
        return command.result

    def flush(self, timeout: float = WRITER_WAIT_SECONDS) -> None:
        command = _FlushCommand()
        self._put_nowait(command)
        if not command.done.wait(timeout):
            self._record_queue_error("WRITER_TIMEOUT", timeout=True)
            raise WriterError("WRITER_TIMEOUT")
        if command.error_code is not None:
            raise WriterError(command.error_code)

    def close(self, timeout: float = WRITER_WAIT_SECONDS) -> None:
        deadline = time.monotonic() + timeout
        enqueue_stop = False
        with self._lock:
            if self._thread_finished.is_set():
                if self._failed_code is not None:
                    raise WriterError(self._failed_code)
                return
            self._closed = True
            command = self._stop_command
            if command is None:
                command = _StopCommand()
                self._stop_command = command
                enqueue_stop = True
        if enqueue_stop:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with self._lock:
                    if self._stop_command is command:
                        self._stop_command = None
                self._record_queue_error("WRITER_CLOSE_TIMEOUT", timeout=True)
                raise WriterError("WRITER_CLOSE_TIMEOUT")
            try:
                self._commands.put(command, timeout=remaining)
            except queue.Full as error:
                # A later close retries instead of mistaking "not accepting"
                # for "the writer thread has finished".
                with self._lock:
                    if self._stop_command is command:
                        self._stop_command = None
                self._record_queue_error("WRITER_CLOSE_TIMEOUT", timeout=True)
                raise WriterError("WRITER_CLOSE_TIMEOUT") from error
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._thread_finished.wait(remaining):
            self._record_queue_error("WRITER_CLOSE_TIMEOUT", timeout=True)
            raise WriterError("WRITER_CLOSE_TIMEOUT")
        with self._lock:
            if self._failed_code is not None:
                raise WriterError(self._failed_code)

    def _put_nowait(self, command: WriterCommand) -> None:
        with self._lock:
            if self._closed:
                self._rejected_count += 1
                self._last_queue_error_code = "WRITER_CLOSED"
                raise WriterError("WRITER_CLOSED")
            if self._failed_code is not None:
                self._rejected_count += 1
                self._last_queue_error_code = self._failed_code
                raise WriterError(self._failed_code)
        try:
            self._commands.put_nowait(command)
        except queue.Full as error:
            self._record_queue_error("WRITER_QUEUE_FULL")
            raise WriterError("WRITER_QUEUE_FULL") from error

    def _record_queue_error(self, code: str, *, timeout: bool = False) -> None:
        with self._lock:
            self._rejected_count += 1
            if timeout:
                self._timeout_count += 1
            self._last_queue_error_code = code

    def _run(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            with self._lock:
                self._writer_thread_id = threading.get_ident()
            connection = self._connection_factory(self.database_path)
            self._prepare(connection)
            self._ready.set()
            while True:
                command = self._commands.get()
                if isinstance(command, _StopCommand):
                    command.done.set()
                    return
                if isinstance(command, _FlushCommand):
                    command.done.set()
                    continue
                if not self._commit_batch(connection, command):
                    return
        except BaseException:
            self._mark_failed("STORAGE_STARTUP_FAILED")
        finally:
            self._ready.set()
            if connection is not None:
                try:
                    connection.close()
                except BaseException:
                    self._mark_failed("STORAGE_CLOSE_FAILED")
            self._fail_pending()
            self._thread_finished.set()

    def _prepare(self, connection: sqlite3.Connection) -> None:
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        if mode is None or str(mode[0]).lower() != "wal":
            raise sqlite3.OperationalError("WAL unavailable")
        connection.execute("PRAGMA user_version=1")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS event_receipts (
                event_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                producer_id TEXT NOT NULL,
                producer_seq INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                timestamp_ns INTEGER NOT NULL,
                request_trace_id TEXT,
                otel_trace_id TEXT,
                span_id TEXT,
                parent_span_id TEXT,
                payload_json TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                committed_at_ns INTEGER NOT NULL,
                UNIQUE(project_id, producer_id, producer_seq)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_owners (
                startup_id TEXT PRIMARY KEY,
                sidecar_pid INTEGER NOT NULL,
                writer_thread_id INTEGER NOT NULL,
                started_at_ns INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT OR REPLACE INTO runtime_owners VALUES (?, ?, ?, ?)",
            (self.startup_id, os.getpid(), threading.get_ident(), time.time_ns()),
        )
        connection.commit()

    def _commit_batch(self, connection: sqlite3.Connection, command: _BatchCommand) -> bool:
        committed: list[str] = []
        duplicates: list[str] = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            for event in command.events:
                existing = connection.execute(
                    "SELECT project_id, producer_id, producer_seq, event_type, schema_version, "
                    "timestamp_ns, request_trace_id, otel_trace_id, span_id, parent_span_id, "
                    "payload_json "
                    "FROM event_receipts WHERE event_id = ?",
                    (event.event_id,),
                ).fetchone()
                identity = (
                    command.project_id,
                    command.producer_id,
                    event.producer_seq,
                    event.event_type,
                    event.schema_version,
                    event.timestamp_ns,
                    event.request_trace_id,
                    event.otel_trace_id,
                    event.span_id,
                    event.parent_span_id,
                    event.payload_json,
                )
                if existing is not None:
                    if tuple(existing) != identity:
                        raise WriterError("EVENT_ID_CONFLICT")
                    duplicates.append(event.event_id)
                    continue
                sequence_owner = connection.execute(
                    "SELECT event_id FROM event_receipts "
                    "WHERE project_id = ? AND producer_id = ? AND producer_seq = ?",
                    (command.project_id, command.producer_id, event.producer_seq),
                ).fetchone()
                if sequence_owner is not None:
                    raise WriterError("PRODUCER_SEQ_CONFLICT")
                connection.execute(
                    "INSERT INTO event_receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.event_id,
                        command.project_id,
                        command.producer_id,
                        event.producer_seq,
                        event.event_type,
                        event.schema_version,
                        event.timestamp_ns,
                        event.request_trace_id,
                        event.otel_trace_id,
                        event.span_id,
                        event.parent_span_id,
                        event.payload_json,
                        command.batch_id,
                        time.time_ns(),
                    ),
                )
                committed.append(event.event_id)
            if self._before_commit is not None:
                self._before_commit()
            connection.commit()
        except WriterError as error:
            connection.rollback()
            command.error_code = error.code
            command.done.set()
            return True
        except BaseException:
            try:
                connection.rollback()
            except BaseException:
                pass
            command.error_code = "STORAGE_FAILED"
            command.done.set()
            self._mark_failed("STORAGE_FAILED")
            return False

        with self._lock:
            self._committed_count += len(committed)
            self._duplicate_count += len(duplicates)
        command.result = BatchResult(tuple(committed), tuple(duplicates))
        command.done.set()
        return True

    def _mark_failed(self, code: str) -> None:
        with self._lock:
            if self._failed_code is None:
                self._failed_code = code
            self._error_count += 1

    def _fail_pending(self) -> None:
        with self._lock:
            code = self._failed_code or "WRITER_CLOSED"
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            if isinstance(command, (_BatchCommand, _FlushCommand)):
                command.error_code = code
            command.done.set()


class LeaseRegistry:
    """One renewable producer lease with bounded reload handoff waiting."""

    def __init__(self, ttl: float) -> None:
        self._ttl = ttl
        self._condition = threading.Condition()
        self._producer_id: str | None = None
        self._lease_id: str | None = None
        self._expires_at = 0.0

    def hello(self, producer_id: str, wait_timeout_ms: int) -> str:
        deadline = time.monotonic() + wait_timeout_ms / 1000
        with self._condition:
            while True:
                self._expire_locked()
                if self._producer_id is None:
                    self._producer_id = producer_id
                    self._lease_id = uuid.uuid4().hex
                    self._expires_at = time.monotonic() + self._ttl
                    self._condition.notify_all()
                    return self._lease_id
                if self._producer_id == producer_id:
                    self._expires_at = time.monotonic() + self._ttl
                    assert self._lease_id is not None
                    return self._lease_id
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LeaseError("MULTI_WORKER_UNSUPPORTED")
                until_expiry = max(0.0, self._expires_at - time.monotonic())
                self._condition.wait(min(remaining, until_expiry or remaining))

    def validate(self, producer_id: str, lease_id: str, *, touch: bool) -> None:
        with self._condition:
            self._expire_locked()
            if self._producer_id != producer_id or self._lease_id != lease_id:
                raise LeaseError("INVALID_LEASE")
            if touch:
                self._expires_at = time.monotonic() + self._ttl

    def release(self, producer_id: str, lease_id: str) -> str:
        with self._condition:
            self._expire_locked()
            if self._producer_id is None:
                return "already_released"
            if self._producer_id != producer_id or self._lease_id != lease_id:
                raise LeaseError("INVALID_LEASE")
            self._producer_id = None
            self._lease_id = None
            self._expires_at = 0.0
            self._condition.notify_all()
            return "released"

    def snapshot(self) -> tuple[str | None, str | None, float | None]:
        with self._condition:
            self._expire_locked()
            expires_in = (
                None if self._producer_id is None else max(0.0, self._expires_at - time.monotonic())
            )
            return self._producer_id, self._lease_id, expires_in

    def _expire_locked(self) -> None:
        if self._producer_id is not None and time.monotonic() >= self._expires_at:
            self._producer_id = None
            self._lease_id = None
            self._expires_at = 0.0
            self._condition.notify_all()


class SidecarService:
    """Injectable service backing both subprocess and in-process tests."""

    def __init__(
        self,
        state: SidecarState,
        writer: EventWriter,
        *,
        lease_ttl: float,
        idle_timeout: float,
    ) -> None:
        self.state = state
        self.writer = writer
        self.leases = LeaseRegistry(lease_ttl)
        self.idle_timeout = idle_timeout
        self.stop_event = threading.Event()
        self._activity = threading.Event()
        self._stop_callback: Callable[[], None] | None = None
        self._watchdog: threading.Thread | None = None

    def set_stop_callback(self, callback: Callable[[], None]) -> None:
        self._stop_callback = callback

    def note_activity(self) -> None:
        self._activity.set()

    def request_stop(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        self._activity.set()
        if self._stop_callback is not None:
            self._stop_callback()

    def start_idle_watchdog(self) -> None:
        if self._watchdog is not None:
            return
        self._watchdog = threading.Thread(
            target=self._watch_idle,
            name="flowsight-spike-idle-watchdog",
            daemon=True,
        )
        self._watchdog.start()

    def close(self) -> None:
        self.stop_event.set()
        self._activity.set()
        self.writer.close(timeout=WRITER_WAIT_SECONDS)
        if self._watchdog is not None:
            self._watchdog.join(WRITER_WAIT_SECONDS)

    def _watch_idle(self) -> None:
        idle_since: float | None = None
        while not self.stop_event.is_set():
            producer_id, _, expires_in = self.leases.snapshot()
            now = time.monotonic()
            if producer_id is not None:
                idle_since = None
                wait_for = expires_in if expires_in is not None else self.idle_timeout
            else:
                if idle_since is None:
                    idle_since = now
                wait_for = self.idle_timeout - (now - idle_since)
                if wait_for <= 0:
                    self.request_stop()
                    return
            self._activity.wait(wait_for)
            self._activity.clear()


class _StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", populate_by_name=False)


class HelloBody(_StrictModel):
    protocol_version: Literal[1]
    project_id: str = Field(min_length=1, max_length=256)
    producer_id: str = Field(min_length=1, max_length=256)
    wait_timeout_ms: int = Field(default=0, ge=0, le=5000)


class LeaseBody(_StrictModel):
    protocol_version: Literal[1]
    project_id: str = Field(min_length=1, max_length=256)
    producer_id: str = Field(min_length=1, max_length=256)
    lease_id: str = Field(min_length=1, max_length=256)


class StopBody(_StrictModel):
    protocol_version: Literal[1]
    project_id: str = Field(min_length=1, max_length=256)


class EventBody(_StrictModel):
    event_id: str = Field(min_length=1, max_length=256)
    producer_seq: int = Field(ge=0, le=2**63 - 1)
    event_type: Literal[
        "route_catalog.replaced",
        "span.ended",
        "span.enrichment",
        "snapshot.captured",
        "trace.drop_notice",
    ] = Field(alias="type")
    schema_version: Literal[1]
    timestamp_ns: int = Field(ge=0, le=2**63 - 1)
    request_trace_id: str | None = Field(default=None, min_length=1, max_length=256)
    otel_trace_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    span_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    parent_span_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    payload_json: str = Field(max_length=MAX_PAYLOAD_JSON_BYTES)

    @field_validator("payload_json")
    @classmethod
    def validate_payload_json(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_PAYLOAD_JSON_BYTES:
            raise ValueError("payload_json is too large")
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("payload_json must be valid JSON") from error
        if type(decoded) is not dict or set(decoded) != {
            "data",
            "redaction",
            "truncated",
            "truncation",
        }:
            raise ValueError("payload_json must be a safe-summary envelope")
        if type(decoded.get("data")) is not dict:
            raise ValueError("payload_json data must be an object")
        return value

    @model_validator(mode="after")
    def validate_scope_identity(self) -> Self:
        request_scoped = {
            "span.ended",
            "span.enrichment",
            "snapshot.captured",
        }
        if self.event_type in request_scoped and (
            self.request_trace_id is None or self.otel_trace_id is None or self.span_id is None
        ):
            raise ValueError("request-scoped events require request/trace/span identity")
        if self.event_type == "route_catalog.replaced" and any(
            value is not None
            for value in (
                self.request_trace_id,
                self.otel_trace_id,
                self.span_id,
                self.parent_span_id,
            )
        ):
            raise ValueError("project-scoped events cannot carry request/span identity")
        if self.event_type == "trace.drop_notice" and any(
            value is not None for value in (self.otel_trace_id, self.span_id, self.parent_span_id)
        ):
            raise ValueError("drop notices cannot claim one OTel span identity")
        return self


class EventsBody(LeaseBody):
    batch_id: str = Field(min_length=1, max_length=256)
    events: list[EventBody] = Field(min_length=1, max_length=MAX_EVENTS_PER_BATCH)


def _fault(code: str, status_code: int = 409) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code})


class SidecarAuth:
    def __init__(self, state: SidecarState) -> None:
        self._state = state

    def require_internal(self, request: Request) -> None:
        host_values = [value for name, value in request.scope["headers"] if name == b"host"]
        auth_values = [
            value for name, value in request.scope["headers"] if name == b"authorization"
        ]
        expected_auth = f"Bearer {self._state.token}".encode()
        if len(host_values) != 1 or not secrets.compare_digest(
            host_values[0], self._state.authority.encode()
        ):
            raise _fault("HOST_REJECTED", 403)
        if len(auth_values) != 1 or not secrets.compare_digest(auth_values[0], expected_auth):
            raise _fault("AUTH_REJECTED", 401)

    def require_browser_write(self, request: Request) -> None:
        self.require_internal(request)
        origin_values = [value for name, value in request.scope["headers"] if name == b"origin"]
        if len(origin_values) != 1 or not secrets.compare_digest(
            origin_values[0], self._state.origin.encode()
        ):
            raise _fault("ORIGIN_REJECTED", 403)


class _BoundedPrivateFastAPI(FastAPI):
    """Authenticate and bound private HTTP bodies before FastAPI parses them."""

    def __init__(self, state: SidecarState) -> None:
        super().__init__(docs_url=None, redoc_url=None, openapi_url=None)
        self._boundary_state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await super().__call__(scope, receive, send)
            return
        path = scope.get("path", "")
        if not (path.startswith("/internal/v1/") or path == "/api/v1/write-probe"):
            await super().__call__(scope, receive, send)
            return

        headers = list(scope.get("headers", ()))
        host_values = [value for name, value in headers if name == b"host"]
        auth_values = [value for name, value in headers if name == b"authorization"]
        expected_auth = f"Bearer {self._boundary_state.token}".encode()
        if len(host_values) != 1 or not secrets.compare_digest(
            host_values[0], self._boundary_state.authority.encode()
        ):
            await self._send_fault(send, 403, "HOST_REJECTED")
            return
        if len(auth_values) != 1 or not secrets.compare_digest(auth_values[0], expected_auth):
            await self._send_fault(send, 401, "AUTH_REJECTED")
            return
        if path == "/api/v1/write-probe":
            origin_values = [value for name, value in headers if name == b"origin"]
            if len(origin_values) != 1 or not secrets.compare_digest(
                origin_values[0], self._boundary_state.origin.encode()
            ):
                await self._send_fault(send, 403, "ORIGIN_REJECTED")
                return

        content_lengths = [value for name, value in headers if name == b"content-length"]
        if len(content_lengths) > 1:
            await self._send_fault(send, 400, "CONTENT_LENGTH_INVALID")
            return
        if content_lengths:
            try:
                content_length = int(content_lengths[0].decode("ascii"))
            except (UnicodeError, ValueError):
                await self._send_fault(send, 400, "CONTENT_LENGTH_INVALID")
                return
            if content_length < 0:
                await self._send_fault(send, 400, "CONTENT_LENGTH_INVALID")
                return
            if content_length > MAX_BATCH_BYTES:
                await self._send_fault(send, 413, "BODY_TOO_LARGE")
                return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                await self._send_fault(send, 400, "BODY_INCOMPLETE")
                return
            if message["type"] != "http.request":
                await self._send_fault(send, 400, "BODY_INVALID")
                return
            chunk = message.get("body", b"")
            if type(chunk) is not bytes:
                await self._send_fault(send, 400, "BODY_INVALID")
                return
            body.extend(chunk)
            if len(body) > MAX_BATCH_BYTES:
                await self._send_fault(send, 413, "BODY_TOO_LARGE")
                return
            if not message.get("more_body", False):
                break

        delivered = False

        async def replay_body() -> Message:
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await super().__call__(scope, replay_body, send)

    @staticmethod
    async def _send_fault(send: Send, status: int, code: str) -> None:
        encoded = json.dumps({"detail": {"code": code}}, separators=(",", ":")).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(encoded)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": encoded})


def create_sidecar_app(service: SidecarService) -> FastAPI:
    """Build the injectable ASGI app used by subprocess and strict ACK tests."""

    app = _BoundedPrivateFastAPI(service.state)
    auth = SidecarAuth(service.state)

    def validate_project(project_id: str) -> None:
        if project_id != service.state.project_id:
            raise _fault("PROJECT_MISMATCH", 400)

    @app.get("/internal/v1/health", dependencies=[Depends(auth.require_internal)])
    def health() -> dict[str, object]:
        producer_id, lease_id, expires_in = service.leases.snapshot()
        return {
            "status": "ok",
            "protocol_version": PROTOCOL_VERSION,
            "project_id": service.state.project_id,
            "startup_id": service.state.startup_id,
            "sidecar_pid": service.state.pid,
            "port": service.state.port,
            "sqlite_owner_id": service.state.startup_id,
            "active_producer_id": producer_id,
            "active_lease_id": lease_id,
            "lease_expires_in_ms": (None if expires_in is None else max(0, int(expires_in * 1000))),
            **service.writer.health,
        }

    @app.post("/internal/v1/hello", dependencies=[Depends(auth.require_internal)])
    def hello_endpoint(body: HelloBody) -> dict[str, object]:
        validate_project(body.project_id)
        try:
            lease_id = service.leases.hello(body.producer_id, body.wait_timeout_ms)
        except LeaseError as error:
            raise _fault(error.code) from error
        service.note_activity()
        return {"status": "accepted", "producer_id": body.producer_id, "lease_id": lease_id}

    @app.post("/internal/v1/renew", dependencies=[Depends(auth.require_internal)])
    def renew_endpoint(body: LeaseBody) -> dict[str, object]:
        validate_project(body.project_id)
        try:
            service.leases.validate(body.producer_id, body.lease_id, touch=True)
        except LeaseError as error:
            raise _fault(error.code) from error
        service.note_activity()
        return {"status": "renewed"}

    @app.post("/internal/v1/events", dependencies=[Depends(auth.require_internal)])
    def events_endpoint(body: EventsBody) -> dict[str, object]:
        validate_project(body.project_id)
        if (
            len(body.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8"))
            > MAX_BATCH_BYTES
        ):
            raise _fault("BATCH_TOO_LARGE", 413)
        try:
            service.leases.validate(body.producer_id, body.lease_id, touch=True)
            result = service.writer.submit(
                project_id=body.project_id,
                producer_id=body.producer_id,
                batch_id=body.batch_id,
                events=tuple(
                    WireEvent(
                        event_id=event.event_id,
                        producer_seq=event.producer_seq,
                        event_type=event.event_type,
                        schema_version=event.schema_version,
                        timestamp_ns=event.timestamp_ns,
                        payload_json=event.payload_json,
                        request_trace_id=event.request_trace_id,
                        otel_trace_id=event.otel_trace_id,
                        span_id=event.span_id,
                        parent_span_id=event.parent_span_id,
                    )
                    for event in body.events
                ),
            )
        except LeaseError as error:
            raise _fault(error.code) from error
        except WriterError as error:
            status = 409 if error.code in {"EVENT_ID_CONFLICT", "PRODUCER_SEQ_CONFLICT"} else 503
            raise _fault(error.code, status) from error
        service.note_activity()
        return {
            "status": "committed",
            "batch_id": body.batch_id,
            "committed_event_ids": list(result.committed_event_ids),
            "duplicate_event_ids": list(result.duplicate_event_ids),
            "committed_count": len(result.committed_event_ids),
        }

    @app.post("/internal/v1/flush", dependencies=[Depends(auth.require_internal)])
    def flush_endpoint(body: LeaseBody) -> dict[str, object]:
        validate_project(body.project_id)
        try:
            service.leases.validate(body.producer_id, body.lease_id, touch=True)
            service.writer.flush()
        except LeaseError as error:
            raise _fault(error.code) from error
        except WriterError as error:
            raise _fault(error.code, 503) from error
        service.note_activity()
        return {"status": "flushed"}

    @app.post("/internal/v1/goodbye", dependencies=[Depends(auth.require_internal)])
    def goodbye_endpoint(body: LeaseBody) -> dict[str, object]:
        validate_project(body.project_id)
        try:
            service.writer.flush()
            status = service.leases.release(body.producer_id, body.lease_id)
        except LeaseError as error:
            raise _fault(error.code) from error
        except WriterError as error:
            raise _fault(error.code, 503) from error
        service.note_activity()
        return {"status": status}

    @app.post("/internal/v1/stop", dependencies=[Depends(auth.require_internal)])
    def stop_endpoint(body: StopBody) -> dict[str, object]:
        validate_project(body.project_id)
        try:
            service.writer.flush()
        except WriterError as error:
            service.request_stop()
            raise _fault(error.code, 503) from error
        service.request_stop()
        return {"status": "stopping"}

    @app.post("/api/v1/write-probe", dependencies=[Depends(auth.require_browser_write)])
    def browser_write_probe() -> dict[str, object]:
        return {"status": "accepted"}

    return app


def bind_loopback_socket(requested_port: int | None, default_port: int) -> socket.socket:
    """Bind once and retain the exact socket handed to Uvicorn."""

    preferred = default_port if requested_port is None else requested_port
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((LOOPBACK_HOST, preferred))
        return listener
    except OSError as error:
        listener.close()
        if requested_port is not None:
            raise PortBindError("EXPLICIT_PORT_CONFLICT") from error

    fallback = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    fallback.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        fallback.bind((LOOPBACK_HOST, 0))
        return fallback
    except OSError as fallback_error:
        fallback.close()
        raise PortBindError("DEFAULT_PORT_UNAVAILABLE") from fallback_error


def _write_ready(fd: int, payload: dict[str, object]) -> None:
    encoded = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
    view = memoryview(encoded)
    while view:
        written = os.write(fd, view)
        view = view[written:]
    os.close(fd)


class _ReadyServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, on_ready: Callable[[], None]) -> None:
        super().__init__(config)
        self._on_ready = on_ready

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        if not self.should_exit:
            self._on_ready()


def _chmod_sqlite_files(database_path: Path) -> None:
    for path in (
        database_path,
        Path(f"{database_path}-wal"),
        Path(f"{database_path}-shm"),
    ):
        try:
            os.chmod(path, 0o600)
        except FileNotFoundError:
            pass


def run_sidecar(
    *,
    runtime_dir: str | Path,
    project_id: str,
    lock_fd: int,
    ready_fd: int,
    requested_port: int | None,
    default_port: int,
    idle_timeout: float,
    lease_ttl: float,
    writer_capacity: int,
    before_commit: CommitGate | None = None,
    connection_factory: ConnectionFactory | None = None,
) -> int:
    """Run one independent process; return a safe process exit code."""

    store = StateStore(runtime_dir)
    listener: socket.socket | None = None
    writer: EventWriter | None = None
    service: SidecarService | None = None
    startup_id = uuid.uuid4().hex
    state: SidecarState | None = None
    ready_sent = False
    exit_code = 4
    previous_umask = os.umask(0o077)
    try:
        store.ensure_private_directory()
        # This either confirms the inherited lock or acquires it for a direct
        # in-process invocation.  The descriptor remains open for the lifetime.
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.set_inheritable(lock_fd, False)
        listener = bind_loopback_socket(requested_port, default_port)
        port = int(listener.getsockname()[1])
        state = SidecarState(
            project_id=project_id,
            startup_id=startup_id,
            pid=os.getpid(),
            port=port,
            token=secrets.token_urlsafe(32),
            database_path=str(store.database_path),
            started_at_ns=time.time_ns(),
        )
        writer = EventWriter(
            store.database_path,
            project_id=project_id,
            startup_id=startup_id,
            capacity=writer_capacity,
            before_commit=before_commit,
            connection_factory=connection_factory,
        )
        _chmod_sqlite_files(store.database_path)
        service = SidecarService(
            state,
            writer,
            lease_ttl=lease_ttl,
            idle_timeout=idle_timeout,
        )
        app = create_sidecar_app(service)
        store.publish(state)
        server_config = uvicorn.Config(
            app,
            host=LOOPBACK_HOST,
            port=port,
            access_log=False,
            log_level="warning",
            proxy_headers=False,
            lifespan="on",
            timeout_graceful_shutdown=int(WRITER_WAIT_SECONDS),
        )

        def ready() -> None:
            nonlocal ready_sent
            assert service is not None
            service.start_idle_watchdog()
            _write_ready(
                ready_fd,
                {
                    "status": "ready",
                    "startup_id": startup_id,
                    "pid": os.getpid(),
                    "port": port,
                },
            )
            ready_sent = True

        server = _ReadyServer(server_config, ready)
        service.set_stop_callback(lambda: setattr(server, "should_exit", True))
        server.run(sockets=[listener])
        exit_code = 0
    except PortBindError as error:
        if not ready_sent:
            _write_ready(ready_fd, {"status": "error", "code": str(error)})
            ready_sent = True
        exit_code = 2
    except BlockingIOError:
        if not ready_sent:
            _write_ready(ready_fd, {"status": "error", "code": "OWNER_LOCK_HELD"})
            ready_sent = True
        exit_code = 3
    except WriterError as error:
        if not ready_sent:
            _write_ready(ready_fd, {"status": "error", "code": error.code})
            ready_sent = True
        exit_code = 4
    except BaseException:
        if not ready_sent:
            try:
                _write_ready(ready_fd, {"status": "error", "code": "SIDECAR_STARTUP_FAILED"})
                ready_sent = True
            except OSError:
                pass
        exit_code = 4
    finally:
        if service is not None:
            try:
                service.close()
            except WriterError:
                if exit_code == 0:
                    exit_code = 5
        elif writer is not None:
            try:
                writer.close()
            except WriterError:
                if exit_code == 0:
                    exit_code = 5
        if writer is not None:
            _chmod_sqlite_files(store.database_path)
        if state is not None:
            try:
                store.remove_if_owned(state.startup_id)
            except OSError:
                if exit_code == 0:
                    exit_code = 5
        if listener is not None:
            listener.close()
        if not ready_sent:
            try:
                os.close(ready_fd)
            except OSError:
                pass
        try:
            os.close(lock_fd)
        except OSError:
            pass
        os.umask(previous_umask)
    return exit_code


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--lock-fd", required=True, type=int)
    parser.add_argument("--ready-fd", required=True, type=int)
    parser.add_argument("--requested-port", type=int)
    parser.add_argument("--default-port", required=True, type=int)
    parser.add_argument("--idle-timeout", required=True, type=float)
    parser.add_argument("--lease-ttl", required=True, type=float)
    parser.add_argument("--writer-capacity", required=True, type=int)
    return parser.parse_args()


def main() -> int:
    arguments = _parse_args()
    return run_sidecar(
        runtime_dir=arguments.runtime_dir,
        project_id=arguments.project_id,
        lock_fd=arguments.lock_fd,
        ready_fd=arguments.ready_fd,
        requested_port=arguments.requested_port,
        default_port=arguments.default_port,
        idle_timeout=arguments.idle_timeout,
        lease_ttl=arguments.lease_ttl,
        writer_capacity=arguments.writer_capacity,
    )


if __name__ == "__main__":
    raise SystemExit(main())
