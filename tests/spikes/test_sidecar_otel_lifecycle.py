"""Executable evidence for the isolated TRIAL-004 lifecycle architecture."""

from __future__ import annotations

import asyncio
import fcntl
import gc
import http.client
import inspect
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from weakref import ref

import pytest
from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.utils import (
    is_http_instrumentation_enabled,
    is_instrumentation_enabled,
)
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON, TraceIdRatioBased
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    SpanKind,
    Status,
    StatusCode,
    TraceFlags,
    TraceState,
    get_current_span,
    set_span_in_context,
)
from starlette.concurrency import run_in_threadpool

from flowsight.security import safe_summary
from spikes.sidecar_otel.lifecycle import FlowSightLifecycle, LifecycleInitialization
from spikes.sidecar_otel.runtime import (
    LeaseRejectedError,
    RuntimeConfig,
    SidecarHandle,
    SidecarStartupError,
    ensure_sidecar,
    goodbye,
    hello,
    probe_health,
    renew,
    stop_sidecar,
)
from spikes.sidecar_otel.sender import (
    BoundedSender,
    DeliveryError,
    DeliveryFailure,
    EnqueueOutcome,
    HTTPBatchTransport,
    SenderTimeoutError,
    TelemetrySenderAdapter,
    TransportError,
    WireEvent,
)
from spikes.sidecar_otel.sidecar import (
    EventWriter,
    SidecarService,
    WriterError,
    create_sidecar_app,
)
from spikes.sidecar_otel.sidecar import (
    WireEvent as SidecarWireEvent,
)
from spikes.sidecar_otel.state import SidecarState, StateStore
from spikes.sidecar_otel.telemetry import (
    FastAPIAppBinding,
    FlowSightSpanProcessor,
    ProviderRegistry,
    TelemetryEvent,
    UnsupportedTracerProviderError,
    derive_request_trace_id,
    instrument_fastapi_app,
    private_function_span,
)


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _safe_envelope_value(value: str, *, ensure_ascii: bool = True) -> str:
    envelope = {
        "data": {"value": value},
        "redaction": {"count": 0, "paths": []},
        "truncated": False,
        "truncation": {"count": 0, "reasons": []},
    }
    return json.dumps(envelope, separators=(",", ":"), ensure_ascii=ensure_ascii)


def _safe_envelope_bytes(size: int) -> str:
    empty = _safe_envelope_value("")
    filler_size = size - len(empty.encode())
    if filler_size < 0:
        raise ValueError("requested envelope size is too small")
    encoded = _safe_envelope_value("x" * filler_size)
    assert len(encoded.encode()) == size
    return encoded


def _python_probe(source: str, *, mode: str) -> dict[str, object]:
    root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root)
    environment["TRIAL004_PROBE_MODE"] = mode
    completed = subprocess.run(
        [sys.executable, "-c", source],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=15.0,
        check=True,
    )
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert type(result) is dict
    return result


def _wire_event(event_id: str, request_trace_id: str) -> WireEvent:
    return WireEvent(
        event_id=event_id,
        event_type="span.ended",
        schema_version=1,
        timestamp_ns=time.time_ns(),
        payload_json=safe_summary({"safe": True}).payload_json,
        request_trace_id=request_trace_id,
        otel_trace_id="0" * 32,
        span_id="1" * 16,
    )


def _ack(envelope: dict[str, object]) -> dict[str, object]:
    events = envelope["events"]
    assert type(events) is list
    return {
        "batch_id": envelope["batch_id"],
        "committed_event_ids": [event["event_id"] for event in events],
        "duplicate_event_ids": [],
    }


class _TelemetryCollector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[TelemetryEvent] = []
        self.reject_request_trace_id: str | None = None
        self.callback_thread_ids: set[int] = set()

    def enqueue(self, event: TelemetryEvent) -> bool:
        with self._lock:
            self.callback_thread_ids.add(threading.get_ident())
            if event.request_trace_id == self.reject_request_trace_id:
                return False
            self._events.append(event)
            return True

    def snapshot(self) -> tuple[TelemetryEvent, ...]:
        with self._lock:
            return tuple(self._events)


def _asgi_get(app: FastAPI, path: str) -> None:
    async def request() -> None:
        sent: list[dict[str, object]] = []
        delivered = False

        async def receive() -> dict[str, object]:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "headers": [(b"host", b"127.0.0.1")],
                "client": ("127.0.0.1", 1234),
                "server": ("127.0.0.1", 4040),
                "root_path": "",
            },
            receive,
            send,
        )
        response_start = next(
            message for message in sent if message["type"] == "http.response.start"
        )
        assert response_start["status"] == 200

    asyncio.run(request())


def _asgi_json(
    app: FastAPI,
    state: SidecarState,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
    *,
    host: str | None = None,
    token: str | None = None,
    origin: str | None = None,
) -> tuple[int, list[tuple[bytes, bytes]], dict[str, object]]:
    async def request() -> tuple[int, list[tuple[bytes, bytes]], dict[str, object]]:
        encoded = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        headers = [
            (b"host", (state.authority if host is None else host).encode()),
            (
                b"authorization",
                f"Bearer {state.token if token is None else token}".encode(),
            ),
            (b"content-length", str(len(encoded)).encode()),
        ]
        if encoded:
            headers.append((b"content-type", b"application/json"))
        if origin is not None:
            headers.append((b"origin", origin.encode()))
        sent: list[dict[str, object]] = []
        delivered = False

        async def receive() -> dict[str, object]:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": encoded, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": method,
                "scheme": "http",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "headers": headers,
                "client": ("127.0.0.1", 1234),
                "server": (state.host, state.port),
                "root_path": "",
            },
            receive,
            send,
        )
        response_start = next(
            message for message in sent if message["type"] == "http.response.start"
        )
        response_body = b"".join(
            message.get("body", b"") for message in sent if message["type"] == "http.response.body"
        )
        decoded = json.loads(response_body or b"{}")
        assert type(decoded) is dict
        return response_start["status"], response_start["headers"], decoded

    return asyncio.run(request())


def _wait_until(predicate, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    waiter = threading.Event()
    while not predicate():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        waiter.wait(min(0.025, remaining))
    return True


def test_state_discovery_is_atomic_private_and_strict(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "runtime")
    state = SidecarState(
        project_id="project-trial-004",
        startup_id="startup-1",
        pid=os.getpid(),
        port=4040,
        token="t" * 43,
        database_path=str(store.database_path),
        started_at_ns=time.time_ns(),
    )

    store.publish(state)

    assert store.load() == state
    assert _mode(store.runtime_dir) == 0o700
    assert _mode(store.state_path) == 0o600
    assert "t" * 43 not in repr(state)

    store.state_path.write_text('{"truncated":', encoding="utf-8")
    os.chmod(store.state_path, 0o600)
    assert store.load() is None

    store.publish(state)
    os.chmod(store.state_path, 0o644)
    assert store.load() is None


def test_sidecar_auth_origin_and_ack_only_after_sqlite_commit(tmp_path: Path) -> None:
    commit_entered = threading.Event()
    release_commit = threading.Event()

    def before_commit() -> None:
        commit_entered.set()
        if not release_commit.wait(2.0):
            raise TimeoutError

    database_path = tmp_path / "events.sqlite3"
    state = SidecarState(
        project_id="project",
        startup_id="startup",
        pid=os.getpid(),
        port=4040,
        token="a" * 43,
        database_path=str(database_path),
        started_at_ns=time.time_ns(),
    )
    writer = EventWriter(
        database_path,
        project_id=state.project_id,
        startup_id=state.startup_id,
        before_commit=before_commit,
    )
    service = SidecarService(state, writer, lease_ttl=5.0, idle_timeout=5.0)
    app = create_sidecar_app(service)

    assert _asgi_json(app, state, "GET", "/internal/v1/health")[0] == 200
    assert _asgi_json(app, state, "GET", "/internal/v1/health", token="wrong-token")[0] == 401
    assert _asgi_json(app, state, "GET", "/internal/v1/health", host="localhost:4040")[0] == 403
    assert _asgi_json(app, state, "POST", "/api/v1/write-probe")[0] == 403
    assert (
        _asgi_json(
            app,
            state,
            "POST",
            "/api/v1/write-probe",
            origin="http://attacker.invalid",
        )[0]
        == 403
    )
    write_status, write_headers, _ = _asgi_json(
        app,
        state,
        "POST",
        "/api/v1/write-probe",
        origin=state.origin,
    )
    assert write_status == 200
    assert all(name.lower() != b"access-control-allow-origin" for name, _ in write_headers)

    hello_status, _, hello_body = _asgi_json(
        app,
        state,
        "POST",
        "/internal/v1/hello",
        {
            "protocol_version": 1,
            "project_id": state.project_id,
            "producer_id": "producer",
            "wait_timeout_ms": 0,
        },
    )
    assert hello_status == 200
    lease_id = hello_body["lease_id"]
    assert type(lease_id) is str
    safe_payload = _safe_envelope_bytes(16 * 1024)
    WireEvent(
        event_id="payload-boundary",
        event_type="span.ended",
        schema_version=1,
        timestamp_ns=1,
        payload_json=safe_payload,
        request_trace_id="boundary-request",
        otel_trace_id="a" * 32,
        span_id="b" * 16,
    )
    with pytest.raises(ValueError, match="event limit"):
        WireEvent(
            event_id="payload-over-limit",
            event_type="span.ended",
            schema_version=1,
            timestamp_ns=1,
            payload_json=_safe_envelope_bytes(16 * 1024 + 1),
            request_trace_id="boundary-request",
            otel_trace_id="a" * 32,
            span_id="b" * 16,
        )
    event = {
        "event_id": "event-1",
        "producer_seq": 1,
        "type": "span.enrichment",
        "schema_version": 1,
        "timestamp_ns": 1,
        "request_trace_id": "r" * 32,
        "otel_trace_id": "a" * 32,
        "span_id": "b" * 16,
        "parent_span_id": "c" * 16,
        "payload_json": safe_payload,
    }

    def event_request(batch_id: str, wire_event: dict[str, object]):
        return _asgi_json(
            app,
            state,
            "POST",
            "/internal/v1/events",
            {
                "protocol_version": 1,
                "project_id": state.project_id,
                "producer_id": "producer",
                "lease_id": lease_id,
                "batch_id": batch_id,
                "events": [wire_event],
            },
        )

    invalid_events: list[dict[str, object]] = []
    oversized_sequence = dict(event)
    oversized_sequence["event_id"] = "oversized-sequence"
    oversized_sequence["producer_seq"] = 2**100
    invalid_events.append(oversized_sequence)
    unknown_type = dict(event)
    unknown_type["event_id"] = "unknown-type"
    unknown_type["producer_seq"] = 2
    unknown_type["type"] = "arbitrary.event"
    invalid_events.append(unknown_type)
    unsafe_shape = dict(event)
    unsafe_shape["event_id"] = "unsafe-shape"
    unsafe_shape["producer_seq"] = 3
    unsafe_shape["payload_json"] = "[]"
    invalid_events.append(unsafe_shape)
    missing_identity = dict(event)
    missing_identity["event_id"] = "missing-identity"
    missing_identity["producer_seq"] = 4
    missing_identity.pop("span_id")
    invalid_events.append(missing_identity)
    oversized_payload = dict(event)
    oversized_payload["event_id"] = "oversized-payload"
    oversized_payload["producer_seq"] = 5
    oversized_payload["payload_json"] = _safe_envelope_bytes(16 * 1024 + 1)
    invalid_events.append(oversized_payload)
    for index, invalid_event in enumerate(invalid_events):
        invalid_status, _, _ = event_request(f"invalid-{index}", invalid_event)
        assert invalid_status == 422
    pre_commit_health = _asgi_json(app, state, "GET", "/internal/v1/health")[2]
    assert pre_commit_health["storage_error_code"] is None

    with ThreadPoolExecutor(max_workers=1) as executor:
        response_future = executor.submit(event_request, "batch-1", event)
        assert commit_entered.wait(1.0)
        assert not response_future.done()
        connection = sqlite3.connect(database_path)
        try:
            assert connection.execute("SELECT COUNT(*) FROM event_receipts").fetchone()[0] == 0
        finally:
            connection.close()
        release_commit.set()
        status, _, response = response_future.result(timeout=2.0)

    assert status == 200
    assert response["committed_event_ids"] == ["event-1"]
    connection = sqlite3.connect(database_path)
    try:
        receipt = connection.execute(
            "SELECT request_trace_id, otel_trace_id, span_id, parent_span_id, payload_json "
            "FROM event_receipts WHERE event_id='event-1'"
        ).fetchone()
    finally:
        connection.close()
    assert receipt == ("r" * 32, "a" * 32, "b" * 16, "c" * 16, safe_payload)

    duplicate_status, _, duplicate = event_request("batch-retry", event)
    assert duplicate_status == 200
    assert duplicate["committed_event_ids"] == []
    assert duplicate["duplicate_event_ids"] == ["event-1"]
    conflicting_event = dict(event)
    conflicting_event["event_id"] = "event-conflict"
    conflict_status, _, conflict = event_request("batch-conflict", conflicting_event)
    assert conflict_status == 409
    assert conflict["detail"] == {"code": "PRODUCER_SEQ_CONFLICT"}

    service.close()


def test_sidecar_writer_queue_storage_failure_and_close_retry_are_visible(
    tmp_path: Path,
) -> None:
    commit_entered = threading.Event()
    release_commit = threading.Event()

    def before_commit() -> None:
        commit_entered.set()
        if not release_commit.wait(2.0):
            raise TimeoutError

    writer = EventWriter(
        tmp_path / "bounded.sqlite3",
        project_id="project",
        startup_id="startup",
        capacity=1,
        before_commit=before_commit,
    )
    first_event = SidecarWireEvent("event-1", 1, "span.ended", 1, 1, "{}")
    second_event = SidecarWireEvent("event-2", 2, "span.ended", 1, 2, "{}")
    third_event = SidecarWireEvent("event-3", 3, "span.ended", 1, 3, "{}")

    def submit(event: SidecarWireEvent):
        return writer.submit(
            project_id="project",
            producer_id="producer",
            batch_id=f"batch-{event.producer_seq}",
            events=(event,),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(submit, first_event)
        assert commit_entered.wait(1.0)
        second_future = executor.submit(submit, second_event)
        assert _wait_until(lambda: writer.health["writer_queue_depth"] == 1, timeout=1.0)
        with pytest.raises(WriterError, match="WRITER_QUEUE_FULL") as queue_error:
            submit(third_event)
        assert queue_error.value.code == "WRITER_QUEUE_FULL"
        with pytest.raises(WriterError, match="WRITER_CLOSE_TIMEOUT"):
            writer.close(timeout=0.01)
        release_commit.set()
        assert first_future.result(timeout=2.0).committed_event_ids == ("event-1",)
        assert second_future.result(timeout=2.0).committed_event_ids == ("event-2",)

    writer.close(timeout=2.0)
    writer.close(timeout=0.0)
    writer_health = writer.health
    assert writer_health["writer_rejected_count"] >= 2
    assert writer_health["writer_timeout_count"] >= 1
    assert writer_health["last_writer_queue_error_code"] == "WRITER_CLOSE_TIMEOUT"

    class FailingInsertConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            if sql.lstrip().upper().startswith("INSERT INTO EVENT_RECEIPTS"):
                raise sqlite3.OperationalError("injected raw storage detail")
            return super().execute(sql, parameters)

    def connect(path: Path) -> sqlite3.Connection:
        return sqlite3.connect(path, factory=FailingInsertConnection)

    failed_writer = EventWriter(
        tmp_path / "failed.sqlite3",
        project_id="project",
        startup_id="startup-failed",
        connection_factory=connect,
    )
    with pytest.raises(WriterError, match="STORAGE_FAILED") as storage_error:
        failed_writer.submit(
            project_id="project",
            producer_id="producer",
            batch_id="batch-failed",
            events=(first_event,),
        )
    assert storage_error.value.code == "STORAGE_FAILED"
    assert "injected raw" not in str(storage_error.value)
    assert failed_writer.health["storage_error_code"] == "STORAGE_FAILED"
    assert failed_writer.health["storage_error_count"] >= 1
    failed_state = SidecarState(
        project_id="project",
        startup_id="startup-failed",
        pid=os.getpid(),
        port=4041,
        token="f" * 43,
        database_path=str(tmp_path / "failed.sqlite3"),
        started_at_ns=time.time_ns(),
    )
    failed_service = SidecarService(
        failed_state,
        failed_writer,
        lease_ttl=5.0,
        idle_timeout=5.0,
    )
    failed_app = create_sidecar_app(failed_service)
    failed_hello = _asgi_json(
        failed_app,
        failed_state,
        "POST",
        "/internal/v1/hello",
        {
            "protocol_version": 1,
            "project_id": "project",
            "producer_id": "failed-producer",
            "wait_timeout_ms": 0,
        },
    )[2]
    failed_lease = failed_hello["lease_id"]
    assert type(failed_lease) is str
    storage_processor = FlowSightSpanProcessor(
        lambda _event: True,
        project_id="project",
    )

    def observe_storage_failure(failure: DeliveryFailure) -> None:
        storage_processor.mark_delivery_failure(
            event.request_trace_id for event in failure.events if event.request_trace_id is not None
        )

    def failed_asgi_transport(envelope: dict[str, object], _timeout: float) -> dict[str, object]:
        status, _, body = _asgi_json(
            failed_app,
            failed_state,
            "POST",
            "/internal/v1/events",
            envelope,
        )
        if status != 200:
            detail = body.get("detail")
            code = detail.get("code") if type(detail) is dict else None
            raise TransportError(code if type(code) is str else f"HTTP_{status}")
        return body

    failed_sender = BoundedSender(
        project_id="project",
        producer_id="failed-producer",
        lease_id=failed_lease,
        transport=failed_asgi_transport,
        max_attempts=1,
        failure_observer=observe_storage_failure,
    )
    assert (
        failed_sender.enqueue(
            WireEvent(
                event_id="storage-failed-event",
                event_type="span.ended",
                schema_version=1,
                timestamp_ns=1,
                payload_json=safe_summary({"safe": True}).payload_json,
                request_trace_id="storage-request",
                otel_trace_id="a" * 32,
                span_id="b" * 16,
            )
        )
        is EnqueueOutcome.ACCEPTED
    )
    with pytest.raises(DeliveryError):
        failed_sender.flush(timeout=2.0)
    assert storage_processor.health_snapshot().incomplete_request_ids == ("storage-request",)
    with pytest.raises(DeliveryError):
        failed_sender.close(timeout=2.0)

    stop_status, _, stop_failure = _asgi_json(
        failed_app,
        failed_state,
        "POST",
        "/internal/v1/stop",
        {"protocol_version": 1, "project_id": "project"},
    )
    assert stop_status == 503
    assert stop_failure["detail"] == {"code": "STORAGE_FAILED"}
    assert failed_service.stop_event.is_set()
    with pytest.raises(WriterError, match="STORAGE_FAILED"):
        failed_writer.close(timeout=2.0)


def test_real_sidecar_stale_recovery_lease_delivery_stop_and_idle_shutdown(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_dir = tmp_path / "runtime"
    store = StateStore(runtime_dir)
    stale_state = SidecarState(
        project_id="project",
        startup_id="stale-startup",
        pid=os.getpid(),
        port=65534,
        token="s" * 43,
        database_path=str(store.database_path),
        started_at_ns=time.time_ns(),
    )
    store.publish(stale_state)
    config = RuntimeConfig(
        runtime_dir=runtime_dir,
        project_id="project",
        default_port=0,
        startup_timeout=10.0,
        idle_timeout=5.0,
        lease_ttl=2.0,
    )
    handle = ensure_sidecar(config)
    state = handle.state
    try:
        duplicate = ensure_sidecar(config)
        assert handle.started_by_caller is True
        assert duplicate.started_by_caller is False
        assert duplicate.state == state
        assert state.pid != os.getpid()
        assert state.startup_id != stale_state.startup_id
        assert state.host == "127.0.0.1"
        assert _mode(store.runtime_dir) == 0o700
        assert _mode(store.state_path) == 0o600
        assert _mode(store.database_path) == 0o600

        old_debuglevel = http.client.HTTPConnection.debuglevel
        http.client.HTTPConnection.debuglevel = 1
        try:
            health = probe_health(state, timeout=1.0)
        finally:
            http.client.HTTPConnection.debuglevel = old_debuglevel
        captured = capsys.readouterr()
        assert health is not None
        assert state.token not in captured.out
        assert state.token not in captured.err
        assert health["sqlite_owner_id"] == state.startup_id

        def header_only_status(token: str, content_length: int) -> int:
            connection = http.client.HTTPConnection(state.host, state.port, timeout=1.0)
            connection.set_debuglevel(0)
            try:
                connection.putrequest(
                    "POST",
                    "/internal/v1/events",
                    skip_host=True,
                    skip_accept_encoding=True,
                )
                connection.putheader("Host", state.authority)
                connection.putheader("Authorization", f"Bearer {token}")
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", str(content_length))
                connection.endheaders()
                response = connection.getresponse()
                response.read()
                return response.status
            finally:
                connection.close()

        assert header_only_status("wrong-token", 10 * 1024 * 1024) == 401
        assert header_only_status(state.token, 10 * 1024 * 1024) == 413

        lease_id = hello(state, "producer", timeout=2.0)
        assert hello(state, "producer", timeout=2.0) == lease_id
        with pytest.raises(LeaseRejectedError) as second_producer:
            hello(state, "other-producer", wait_timeout_ms=0, timeout=2.0)
        assert second_producer.value.code == "MULTI_WORKER_UNSUPPORTED"

        import spikes.sidecar_otel.runtime as runtime_module

        real_request_json = runtime_module.request_json
        renew_calls = 0

        def committed_renew_then_lose_response(*args, **kwargs):
            nonlocal renew_calls
            status, body = real_request_json(*args, **kwargs)
            if args[2] == "/internal/v1/renew":
                renew_calls += 1
                if renew_calls == 1:
                    raise TimeoutError("response lost after committed renew")
            return status, body

        monkeypatch.setattr(runtime_module, "request_json", committed_renew_then_lose_response)
        with pytest.raises(TimeoutError, match="committed renew"):
            renew(state, "producer", lease_id, timeout=2.0)
        committed_renew_health = probe_health(state, timeout=1.0)
        assert committed_renew_health is not None
        assert committed_renew_health["active_producer_id"] == "producer"
        assert committed_renew_health["active_lease_id"] == lease_id
        renew(state, "producer", lease_id, timeout=2.0)
        assert renew_calls == 2
        monkeypatch.setattr(runtime_module, "request_json", real_request_json)

        real_transport = HTTPBatchTransport(state)
        transport_calls = 0

        def lose_first_response(envelope: dict[str, object], timeout: float) -> dict[str, object]:
            nonlocal transport_calls
            transport_calls += 1
            response = real_transport(envelope, timeout)
            if transport_calls == 1:
                raise TimeoutError
            return response

        sender = BoundedSender(
            project_id=state.project_id,
            producer_id="producer",
            lease_id=lease_id,
            transport=lose_first_response,
            max_attempts=2,
        )
        adapter = TelemetrySenderAdapter(sender, event_id_prefix="real-request")
        processor = FlowSightSpanProcessor(
            adapter.enqueue,
            flush=lambda timeout: sender.flush(timeout),
            project_id=state.project_id,
        )
        provider = TracerProvider(sampler=ALWAYS_ON)
        provider.add_span_processor(processor)
        tracer = provider.get_tracer("trial-004-real-private-path")
        visibility_started = time.monotonic()
        with tracer.start_as_current_span("finished-request", kind=SpanKind.SERVER) as request_span:
            request_context = request_span.get_span_context()
        expected_request_id = derive_request_trace_id(
            state.project_id,
            request_context.trace_id,
            request_context.span_id,
        )
        assert processor.deactivate(timeout=2.0) is True
        sender.close(timeout=2.0)
        provider.shutdown()
        connection = sqlite3.connect(store.database_path)
        try:
            receipt = connection.execute(
                "SELECT event_id, event_type, request_trace_id, otel_trace_id, span_id, "
                "parent_span_id "
                "FROM event_receipts"
            ).fetchall()
        finally:
            connection.close()
        assert time.monotonic() - visibility_started < 1.0
        assert receipt == [
            (
                "real-request:1",
                "span.ended",
                expected_request_id,
                f"{request_context.trace_id:032x}",
                f"{request_context.span_id:016x}",
                None,
            )
        ]
        assert transport_calls == 2
        refreshed_health = probe_health(state, timeout=1.0)
        assert refreshed_health is not None
        assert refreshed_health["committed_event_count"] == 1
        assert refreshed_health["duplicate_event_count"] == 1

        goodbye(state, "producer", lease_id, timeout=2.0)
        stop_sidecar(state, timeout=2.0)
        assert _wait_until(lambda: store.load() is None, timeout=5.0)
        assert _wait_until(lambda: probe_health(state, timeout=0.1) is None, timeout=5.0)
    finally:
        if probe_health(state, timeout=0.1) is not None:
            stop_sidecar(state, timeout=2.0)

    lock_fd = store.open_lock()
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)

    idle_store = StateStore(tmp_path / "idle-runtime")
    idle_handle = ensure_sidecar(
        RuntimeConfig(
            runtime_dir=idle_store.runtime_dir,
            project_id="idle-project",
            default_port=0,
            startup_timeout=10.0,
            idle_timeout=0.2,
            lease_ttl=1.0,
        )
    )
    idle_lease = hello(idle_handle.state, "idle-producer", timeout=2.0)
    goodbye(idle_handle.state, "idle-producer", idle_lease, timeout=2.0)
    assert _wait_until(lambda: idle_store.load() is None, timeout=5.0)
    assert _wait_until(
        lambda: probe_health(idle_handle.state, timeout=0.1) is None,
        timeout=5.0,
    )


def test_default_port_conflict_falls_back_and_explicit_conflict_cleans_up(
    tmp_path: Path,
) -> None:
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    blocked_port = int(blocker.getsockname()[1])
    fallback_store = StateStore(tmp_path / "fallback")
    fallback = ensure_sidecar(
        RuntimeConfig(
            runtime_dir=fallback_store.runtime_dir,
            project_id="fallback-project",
            default_port=blocked_port,
            startup_timeout=10.0,
            idle_timeout=5.0,
        )
    )
    try:
        assert fallback.state.host == "127.0.0.1"
        assert fallback.state.port != blocked_port
    finally:
        stop_sidecar(fallback.state, timeout=2.0)
        assert _wait_until(lambda: fallback_store.load() is None, timeout=5.0)

    explicit_store = StateStore(tmp_path / "explicit")
    try:
        with pytest.raises(SidecarStartupError) as conflict:
            ensure_sidecar(
                RuntimeConfig(
                    runtime_dir=explicit_store.runtime_dir,
                    project_id="explicit-project",
                    requested_port=blocked_port,
                    startup_timeout=10.0,
                    idle_timeout=5.0,
                )
            )
        assert conflict.value.code == "EXPLICIT_PORT_CONFLICT"
        assert explicit_store.load() is None
        lock_fd = explicit_store.open_lock()
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
    finally:
        blocker.close()


def test_real_storage_startup_failure_is_structured_and_releases_owner_lock(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "storage-failure")
    store.ensure_private_directory()
    store.database_path.mkdir()

    with pytest.raises(SidecarStartupError) as startup_error:
        ensure_sidecar(
            RuntimeConfig(
                runtime_dir=store.runtime_dir,
                project_id="storage-failure-project",
                default_port=0,
                startup_timeout=5.0,
            )
        )

    assert startup_error.value.code == "STORAGE_STARTUP_FAILED"
    assert store.load() is None
    lock_fd = store.open_lock()
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


def test_explicit_stop_is_bounded_even_with_authenticated_stalled_request(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "stalled-request")
    handle = ensure_sidecar(
        RuntimeConfig(
            runtime_dir=store.runtime_dir,
            project_id="stalled-project",
            default_port=0,
            startup_timeout=10.0,
            idle_timeout=10.0,
        )
    )
    state = handle.state
    stalled = socket.create_connection((state.host, state.port), timeout=2.0)
    stalled.sendall(
        (
            "POST /internal/v1/events HTTP/1.1\r\n"
            f"Host: {state.authority}\r\n"
            f"Authorization: Bearer {state.token}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 100000\r\n"
            "Connection: keep-alive\r\n\r\n"
            "{"
        ).encode()
    )
    try:
        stop_started = time.monotonic()
        stop_sidecar(state, timeout=2.0)
        assert _wait_until(lambda: store.load() is None, timeout=5.0)
        assert time.monotonic() - stop_started < 5.0
        lock_fd = store.open_lock()
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
    finally:
        stalled.close()
        if probe_health(state, timeout=0.1) is not None:
            stop_sidecar(state, timeout=2.0)


def test_reload_heartbeat_retries_transient_renewal_without_changing_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import spikes.sidecar_otel.reload_fixture as reload_module

    state = SidecarState(
        project_id="heartbeat-project",
        startup_id="heartbeat-startup",
        pid=os.getpid(),
        port=4040,
        token="h" * 43,
        database_path=str(tmp_path / "heartbeat.sqlite3"),
        started_at_ns=time.time_ns(),
    )
    settings = reload_module.ReloadSettings(
        runtime_dir=tmp_path,
        project_id=state.project_id,
        requested_port=None,
        default_port=0,
        startup_timeout=2.0,
        idle_timeout=5.0,
        lease_ttl=1.0,
        writer_capacity=8,
        notify_socket=None,
    )
    recovered = threading.Event()
    calls = 0

    def transient_renew(
        _state: SidecarState,
        _producer_id: str,
        lease_id: str,
        *,
        timeout: float,
    ) -> None:
        nonlocal calls
        assert lease_id == "stable-lease"
        assert timeout <= settings.lease_ttl
        calls += 1
        if calls == 1:
            raise ConnectionError("transient test transport failure")
        recovered.set()

    monkeypatch.setattr(reload_module, "renew", transient_renew)
    heartbeat = reload_module._LeaseHeartbeat(
        settings,
        SidecarHandle(state=state, started_by_caller=False),
        "producer",
        "stable-lease",
    )
    heartbeat.start()
    try:
        assert recovered.wait(1.5)
    finally:
        heartbeat.stop()
    assert calls >= 2

    with pytest.raises(ValueError, match="at least 1 second"):
        RuntimeConfig(
            runtime_dir=tmp_path,
            project_id="too-short-lease",
            lease_ttl=0.1,
        )


def test_concurrent_process_launchers_elect_exactly_one_sidecar(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    runtime_dir = tmp_path / "concurrent-runtime"
    source = r"""
import json
import os
import sys
from pathlib import Path
from spikes.sidecar_otel.runtime import RuntimeConfig, ensure_sidecar

sys.stdin.buffer.read(1)
handle = ensure_sidecar(RuntimeConfig(
    runtime_dir=Path(os.environ["TRIAL004_RUNTIME_DIR"]),
    project_id="concurrent-project",
    default_port=0,
    startup_timeout=10.0,
    idle_timeout=10.0,
))
print(json.dumps({
    "started": handle.started_by_caller,
    "pid": handle.state.pid,
    "port": handle.state.port,
    "startup_id": handle.state.startup_id,
}), flush=True)
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root)
    environment["TRIAL004_RUNTIME_DIR"] = str(runtime_dir)
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", source],
            cwd=root,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        for _ in range(2)
    ]
    for process in processes:
        assert process.stdin is not None
        process.stdin.write(b"x")
        process.stdin.close()
    results: list[dict[str, object]] = []
    for process in processes:
        process.wait(timeout=15.0)
        assert process.stdout is not None
        assert process.stderr is not None
        output = process.stdout.read().decode()
        errors = process.stderr.read().decode()
        assert process.returncode == 0, errors
        result = json.loads(output.strip().splitlines()[-1])
        assert type(result) is dict
        results.append(result)

    assert sum(result["started"] is True for result in results) == 1
    assert len({result["pid"] for result in results}) == 1
    assert len({result["port"] for result in results}) == 1
    assert len({result["startup_id"] for result in results}) == 1

    store = StateStore(runtime_dir)
    state = store.load()
    assert state is not None
    stop_sidecar(state, timeout=2.0)
    assert _wait_until(lambda: store.load() is None, timeout=5.0)


def test_waiting_launcher_re_elects_when_lock_owner_exits_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = tmp_path / "abandoned-election-runtime"
    store = StateStore(runtime_dir)
    held_lock = store.open_lock()
    fcntl.flock(held_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    original_open_lock = StateStore.open_lock
    retry_opened = threading.Event()
    open_count = 0
    count_lock = threading.Lock()

    def observed_open_lock(self: StateStore) -> int:
        nonlocal open_count
        descriptor = original_open_lock(self)
        with count_lock:
            open_count += 1
            if open_count >= 2:
                retry_opened.set()
        return descriptor

    monkeypatch.setattr(StateStore, "open_lock", observed_open_lock)
    config = RuntimeConfig(
        runtime_dir=runtime_dir,
        project_id="abandoned-election-project",
        default_port=0,
        startup_timeout=3.0,
        idle_timeout=10.0,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(ensure_sidecar, config)
        assert retry_opened.wait(1.0)
        fcntl.flock(held_lock, fcntl.LOCK_UN)
        os.close(held_lock)
        held_lock = -1
        handle = future.result(timeout=8.0)

    try:
        assert handle.started_by_caller is True
        assert probe_health(handle.state, timeout=0.5) is not None
    finally:
        if held_lock >= 0:
            fcntl.flock(held_lock, fcntl.LOCK_UN)
            os.close(held_lock)
        stop_sidecar(handle.state, timeout=2.0)
        assert _wait_until(lambda: store.load() is None, timeout=5.0)


def test_ordinary_uvicorn_worker_attaches_to_one_sidecar_and_sqlite_owner(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    runtime_dir = tmp_path / "ordinary-runtime"
    notify_path = Path(f"/tmp/flowsight-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock")
    notifier = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    notifier.bind(str(notify_path))
    notifier.settimeout(20.0)
    app_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    app_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    app_socket.bind(("127.0.0.1", 0))
    app_socket.listen(128)
    app_port = int(app_socket.getsockname()[1])
    app_socket.set_inheritable(True)
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": str(root),
            "FLOWSIGHT_SPIKE_RUNTIME_DIR": str(runtime_dir),
            "FLOWSIGHT_SPIKE_PROJECT_ID": "ordinary-project",
            "FLOWSIGHT_SPIKE_NOTIFY_SOCKET": str(notify_path),
            "FLOWSIGHT_SPIKE_DEFAULT_PORT": "0",
            "FLOWSIGHT_SPIKE_STARTUP_TIMEOUT": "15",
            "FLOWSIGHT_SPIKE_IDLE_TIMEOUT": "10",
            "FLOWSIGHT_SPIKE_LEASE_TTL": "2",
        }
    )
    log_path = tmp_path / "uvicorn-ordinary.log"
    log = log_path.open("w+", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "--factory",
            "spikes.sidecar_otel.reload_fixture:create_reload_app",
            "--fd",
            str(app_socket.fileno()),
            "--log-level",
            "warning",
            "--no-access-log",
        ],
        cwd=root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        pass_fds=(app_socket.fileno(),),
        start_new_session=True,
    )
    app_socket.close()
    state: SidecarState | None = None
    try:
        deadline = time.monotonic() + 20.0
        started: dict[str, object] | None = None
        while started is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.flush()
                raise AssertionError(
                    f"ordinary startup notification timed out: {log_path.read_text()}"
                )
            notifier.settimeout(remaining)
            event = json.loads(notifier.recv(64 * 1024))
            assert type(event) is dict
            if event.get("event") == "worker_start_failed":
                log.flush()
                raise AssertionError(f"ordinary worker failed: {event}; {log_path.read_text()}")
            if event.get("event") == "worker_started":
                started = event

        worker_pid = started["worker_pid"]
        assert type(worker_pid) is int
        probe: dict[str, object] | None = None

        def worker_is_queryable() -> bool:
            nonlocal probe
            connection = http.client.HTTPConnection("127.0.0.1", app_port, timeout=0.25)
            connection.set_debuglevel(0)
            try:
                connection.request("GET", "/probe")
                response = connection.getresponse()
                body = json.loads(response.read())
                if response.status == 200 and body.get("worker_pid") == worker_pid:
                    probe = body
                    return True
                return False
            except (OSError, TimeoutError, http.client.HTTPException, json.JSONDecodeError):
                return False
            finally:
                connection.close()

        assert _wait_until(worker_is_queryable, timeout=5.0)
        state = StateStore(runtime_dir).load()
        assert state is not None
        assert probe is not None
        for identity_field in ("sidecar_pid", "sidecar_port", "startup_id"):
            assert started[identity_field] == probe[identity_field]
        assert probe["sidecar_pid"] == state.pid
        assert probe["sidecar_port"] == state.port
        assert probe["startup_id"] == state.startup_id
        connection = sqlite3.connect(state.database_path)
        try:
            owner = connection.execute(
                "SELECT startup_id, sidecar_pid FROM runtime_owners"
            ).fetchone()
        finally:
            connection.close()
        assert owner == (state.startup_id, state.pid)
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=15.0)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5.0)
        if state is None:
            state = StateStore(runtime_dir).load()
        if state is not None and probe_health(state, timeout=0.2) is not None:
            stop_sidecar(state, timeout=2.0)
        log.close()
        notifier.close()
        notify_path.unlink(missing_ok=True)

    assert process.returncode == 0
    assert _wait_until(lambda: StateStore(runtime_dir).load() is None, timeout=5.0)


def test_real_uvicorn_reload_reconnects_to_same_sidecar_and_sqlite_owner(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    runtime_dir = tmp_path / "reload-runtime"
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    marker = watch_dir / "generation.py"
    marker.write_text("GENERATION = 1\n", encoding="utf-8")
    notify_path = Path(f"/tmp/flowsight-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock")
    notifier = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    notifier.bind(str(notify_path))
    notifier.settimeout(20.0)
    app_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    app_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    app_socket.bind(("127.0.0.1", 0))
    app_socket.listen(128)
    app_port = int(app_socket.getsockname()[1])
    app_socket.set_inheritable(True)
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": str(root),
            "FLOWSIGHT_SPIKE_RUNTIME_DIR": str(runtime_dir),
            "FLOWSIGHT_SPIKE_PROJECT_ID": "reload-project",
            "FLOWSIGHT_SPIKE_NOTIFY_SOCKET": str(notify_path),
            "FLOWSIGHT_SPIKE_DEFAULT_PORT": "0",
            "FLOWSIGHT_SPIKE_STARTUP_TIMEOUT": "15",
            "FLOWSIGHT_SPIKE_IDLE_TIMEOUT": "10",
            "FLOWSIGHT_SPIKE_LEASE_TTL": "2",
        }
    )
    log_path = tmp_path / "uvicorn-reload.log"
    log = log_path.open("w+", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "--factory",
            "spikes.sidecar_otel.reload_fixture:create_reload_app",
            "--reload",
            "--reload-dir",
            str(watch_dir),
            "--fd",
            str(app_socket.fileno()),
            "--log-level",
            "warning",
            "--no-access-log",
        ],
        cwd=root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        pass_fds=(app_socket.fileno(),),
        start_new_session=True,
    )
    app_socket.close()
    stopped_workers: set[int] = set()

    def receive_event(deadline: float) -> dict[str, object]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.flush()
            raise AssertionError(f"reload notification timed out: {log_path.read_text()}")
        notifier.settimeout(remaining)
        event = json.loads(notifier.recv(64 * 1024))
        assert type(event) is dict
        if event.get("event") in {
            "worker_start_failed",
            "worker_shutdown_failed",
            "heartbeat_failed",
        }:
            log.flush()
            raise AssertionError(f"reload worker failed: {event}; {log_path.read_text()}")
        if event.get("event") == "worker_stopped" and type(event.get("worker_pid")) is int:
            stopped_workers.add(event["worker_pid"])
        return event

    def receive_started(*, different_from: int | None = None) -> dict[str, object]:
        deadline = time.monotonic() + 20.0
        while True:
            event = receive_event(deadline)
            if event.get("event") == "worker_started" and (
                different_from is None or event.get("worker_pid") != different_from
            ):
                return event

    def receive_stopped(worker_pid: int) -> None:
        deadline = time.monotonic() + 10.0
        while worker_pid not in stopped_workers:
            receive_event(deadline)

    def query_probe(expected_worker: int) -> dict[str, object] | None:
        connection = http.client.HTTPConnection("127.0.0.1", app_port, timeout=0.25)
        connection.set_debuglevel(0)
        try:
            connection.request("GET", "/probe")
            response = connection.getresponse()
            body = json.loads(response.read())
            if response.status == 200 and body.get("worker_pid") == expected_worker:
                return body
            return None
        except (OSError, TimeoutError, http.client.HTTPException, json.JSONDecodeError):
            return None
        finally:
            connection.close()

    state: SidecarState | None = None
    try:
        first = receive_started()
        first_worker = first["worker_pid"]
        assert type(first_worker) is int
        first_probe: dict[str, object] | None = None

        def first_is_queryable() -> bool:
            nonlocal first_probe
            first_probe = query_probe(first_worker)
            return first_probe is not None

        assert _wait_until(first_is_queryable, timeout=5.0)
        old_mtime = marker.stat().st_mtime_ns
        marker.write_text("GENERATION = 2\n", encoding="utf-8")
        changed_mtime = max(time.time_ns(), old_mtime + 2_000_000_000)
        os.utime(marker, ns=(changed_mtime, changed_mtime))
        second = receive_started(different_from=first_worker)
        receive_stopped(first_worker)
        second_worker = second["worker_pid"]
        assert type(second_worker) is int
        second_probe: dict[str, object] | None = None

        def second_is_queryable() -> bool:
            nonlocal second_probe
            second_probe = query_probe(second_worker)
            return second_probe is not None

        assert _wait_until(second_is_queryable, timeout=5.0)
        assert first_worker != second_worker
        for identity_field in ("sidecar_pid", "sidecar_port", "startup_id"):
            assert first[identity_field] == second[identity_field]
            assert first_probe is not None
            assert second_probe is not None
            assert first_probe[identity_field] == second_probe[identity_field]

        state = StateStore(runtime_dir).load()
        assert state is not None
        connection = sqlite3.connect(state.database_path)
        try:
            owner = connection.execute(
                "SELECT startup_id, sidecar_pid FROM runtime_owners"
            ).fetchone()
        finally:
            connection.close()
        assert owner == (state.startup_id, state.pid)
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=15.0)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5.0)
        if state is None:
            state = StateStore(runtime_dir).load()
        if state is not None and probe_health(state, timeout=0.2) is not None:
            stop_sidecar(state, timeout=2.0)
        log.close()
        notifier.close()
        notify_path.unlink(missing_ok=True)

    assert process.returncode == 0
    assert _wait_until(lambda: StateStore(runtime_dir).load() is None, timeout=5.0)


def test_coordinated_init_shutdown_and_reinit_reuse_one_processor(
    tmp_path: Path,
) -> None:
    class CountingProvider(TracerProvider):
        def __init__(self) -> None:
            self.flowsight_add_count = 0
            super().__init__(sampler=ALWAYS_ON)

        def add_span_processor(self, span_processor) -> None:
            if isinstance(span_processor, FlowSightSpanProcessor):
                self.flowsight_add_count += 1
            super().add_span_processor(span_processor)

    provider = CountingProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    config = RuntimeConfig(
        runtime_dir=tmp_path / "coordinated-runtime",
        project_id="coordinated-project",
        default_port=0,
        startup_timeout=5.0,
        idle_timeout=0.25,
        lease_ttl=1.0,
    )
    lifecycle = FlowSightLifecycle(config, provider)
    store = StateStore(config.runtime_dir)
    app = FastAPI()

    @app.get("/probe")
    def probe() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/retired-error")
    def retired_error() -> None:
        raise RuntimeError("retired app failure")

    replacement_app = FastAPI()

    @replacement_app.get("/replacement")
    def replacement_probe() -> dict[str, bool]:
        return {"replacement": True}

    first_collector = _TelemetryCollector()
    ignored_duplicate_collector = _TelemetryCollector()
    replacement_collector = _TelemetryCollector()
    try:
        first = lifecycle.init_app(app, first_collector.enqueue)
        duplicate = lifecycle.init_app(app, ignored_duplicate_collector.enqueue)
        assert first.sidecar.started_by_caller is True
        assert duplicate.sidecar is first.sidecar
        assert duplicate.telemetry is first.telemetry
        assert duplicate.lease_id == first.lease_id
        assert first.instrumentation.newly_registered is True
        assert duplicate.instrumentation.newly_registered is False
        assert provider.flowsight_add_count == 1
        assert _wait_until(
            lambda: lifecycle.health_snapshot().heartbeat_renewal_count >= 2,
            timeout=2.0,
        )
        live_health = probe_health(first.sidecar.state, timeout=0.5)
        assert live_health is not None
        assert live_health["active_producer_id"] == first.producer_id

        competing = FlowSightLifecycle(config, provider)
        shared = competing.init_app(app, ignored_duplicate_collector.enqueue)
        assert shared.sidecar is first.sidecar
        assert shared.telemetry.processor is first.telemetry.processor
        assert shared.producer_id == first.producer_id
        assert shared.lease_id == first.lease_id
        assert lifecycle.shutdown(timeout=0.0) is True
        assert competing.health_snapshot().active is True

        _asgi_get(app, "/probe")
        first_event_count = len(first_collector.snapshot())
        assert first_event_count == 1
        assert ignored_duplicate_collector.snapshot() == ()
        assert competing.shutdown(timeout=3.0) is True
        released_health = probe_health(first.sidecar.state, timeout=0.5)
        assert released_health is not None
        assert released_health["active_producer_id"] is None

        with provider.get_tracer("trial-004-user-owned").start_as_current_span(
            "user-span-after-flowsight-shutdown"
        ):
            pass
        assert len(first_collector.snapshot()) == first_event_count
        assert any(
            span.name == "user-span-after-flowsight-shutdown"
            for span in exporter.get_finished_spans()
        )

        reinitialized = lifecycle.init_app(replacement_app, replacement_collector.enqueue)
        assert reinitialized.telemetry.processor is first.telemetry.processor
        assert reinitialized.sidecar.state.startup_id == first.sidecar.state.startup_id
        assert reinitialized.producer_id != first.producer_id
        assert reinitialized.lease_id != first.lease_id
        assert reinitialized.instrumentation.newly_registered is True
        assert provider.flowsight_add_count == 1
        # The retired app remains OTel-instrumented, but its invalidated session
        # identity must never leak spans into the replacement delivery target.
        _asgi_get(app, "/probe")
        with pytest.raises(RuntimeError, match="retired app failure"):
            _asgi_get(app, "/retired-error")
        with provider.get_tracer("trial-004-unbound-server").start_as_current_span(
            "unbound-server",
            kind=SpanKind.SERVER,
        ):
            pass
        _asgi_get(replacement_app, "/replacement")
        assert len(first_collector.snapshot()) == first_event_count
        [replacement_event] = replacement_collector.snapshot()
        replacement_payload = json.loads(replacement_event.payload_json)["data"]
        assert replacement_payload["name"] == "GET /replacement"
        exported_names = {span.name for span in exporter.get_finished_spans()}
        assert {"GET /retired-error", "unbound-server", "GET /replacement"} <= exported_names
        assert lifecycle.shutdown(timeout=3.0) is True
        assert lifecycle.shutdown(timeout=0.0) is True
    finally:
        lifecycle.shutdown(timeout=3.0)
        state = store.load()
        if state is not None and probe_health(state, timeout=0.2) is not None:
            stop_sidecar(state, timeout=2.0)
            assert _wait_until(lambda: store.load() is None, timeout=5.0)
        provider.shutdown()


def test_inflight_request_token_cannot_leak_across_different_or_same_app_reinit(
    tmp_path: Path,
) -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    config = RuntimeConfig(
        runtime_dir=tmp_path / "inflight-app-generation-runtime",
        project_id="inflight-app-generation-project",
        default_port=0,
        startup_timeout=5.0,
        idle_timeout=10.0,
        lease_ttl=1.0,
    )
    lifecycle = FlowSightLifecycle(config, provider)
    store = StateStore(config.runtime_dir)
    first_app = FastAPI()
    replacement_app = FastAPI()
    request_entered = threading.Event()
    release_request = threading.Event()
    held_contexts: list[SpanContext] = []
    tracer = provider.get_tracer("trial-004-inflight-generation")

    @first_app.get("/held")
    def held_request() -> None:
        held_contexts.append(get_current_span().get_span_context())
        request_entered.set()
        if not release_request.wait(4.0):
            raise TimeoutError("held request was not released")
        with tracer.start_as_current_span("old-generation-child"):
            pass
        raise RuntimeError("old generation finished late")

    @first_app.get("/new")
    def new_first_app_request() -> dict[str, bool]:
        return {"new": True}

    @replacement_app.get("/replacement")
    def replacement_request() -> dict[str, bool]:
        return {"replacement": True}

    first_collector = _TelemetryCollector()
    replacement_collector = _TelemetryCollector()
    reactivated_collector = _TelemetryCollector()
    first = lifecycle.init_app(first_app, first_collector.enqueue)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            held_future = executor.submit(_asgi_get, first_app, "/held")
            assert request_entered.wait(1.0)
            [held_context] = held_contexts
            held_request_id = derive_request_trace_id(
                config.project_id,
                held_context.trace_id,
                held_context.span_id,
            )

            assert lifecycle.shutdown(timeout=3.0) is True
            assert (
                held_request_id
                in first.telemetry.processor.health_snapshot().incomplete_request_ids
            )
            lifecycle.init_app(replacement_app, replacement_collector.enqueue)
            release_request.set()
            with pytest.raises(RuntimeError, match="old generation finished late"):
                held_future.result(timeout=2.0)

        assert first_collector.snapshot() == ()
        assert replacement_collector.snapshot() == ()
        _asgi_get(replacement_app, "/replacement")
        [replacement_event] = replacement_collector.snapshot()
        assert json.loads(replacement_event.payload_json)["data"]["name"] == "GET /replacement"
        assert lifecycle.shutdown(timeout=3.0) is True

        lifecycle.init_app(first_app, reactivated_collector.enqueue)
        _asgi_get(first_app, "/new")
        [reactivated_event] = reactivated_collector.snapshot()
        assert json.loads(reactivated_event.payload_json)["data"]["name"] == "GET /new"
        assert lifecycle.shutdown(timeout=3.0) is True
        exported_names = {span.name for span in exporter.get_finished_spans()}
        assert {
            "GET /held",
            "old-generation-child",
            "GET /replacement",
            "GET /new",
        } <= exported_names
    finally:
        release_request.set()
        lifecycle.shutdown(timeout=3.0)
        state = store.load()
        if state is not None and probe_health(state, timeout=0.2) is not None:
            stop_sidecar(state, timeout=2.0)
            assert _wait_until(lambda: store.load() is None, timeout=5.0)
        provider.shutdown()


def test_lifecycle_publication_join_and_detach_are_baseexception_atomic(
    tmp_path: Path,
) -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    config = RuntimeConfig(
        runtime_dir=tmp_path / "baseexception-atomic-runtime",
        project_id="baseexception-atomic-project",
        default_port=0,
        startup_timeout=5.0,
        idle_timeout=10.0,
        lease_ttl=1.0,
    )
    owner = FlowSightLifecycle(config, provider)
    joiner = FlowSightLifecycle(config, provider)
    store = StateStore(config.runtime_dir)
    app = FastAPI()
    source_lines, source_start = inspect.getsourcelines(FlowSightLifecycle.init_app)

    def line_after(start_fragment: str, target_fragment: str) -> int:
        start_index = next(
            index for index, line in enumerate(source_lines) if start_fragment in line
        )
        target_index = next(
            index
            for index in range(start_index + 1, len(source_lines))
            if target_fragment in source_lines[index]
        )
        return source_start + target_index

    def interrupt_at(function, target_line: int, message: str):
        fired = False

        def trace_lines(frame, event, _argument):
            nonlocal fired
            if (
                not fired
                and event == "line"
                and frame.f_code is function.__code__
                and frame.f_lineno == target_line
            ):
                fired = True
                raise KeyboardInterrupt(message)
            return trace_lines

        return trace_lines, lambda: fired

    publish_line = line_after(
        "self._shared.active = _ActiveLifecycle(",
        "self._shared.reference_count = 1",
    )
    publish_trace, publish_fired = interrupt_at(
        FlowSightLifecycle.init_app,
        publish_line,
        "after active publication",
    )
    try:
        sys.settrace(publish_trace)
        with pytest.raises(KeyboardInterrupt, match="after active publication"):
            owner.init_app(app, _TelemetryCollector().enqueue)
    finally:
        sys.settrace(None)
    assert publish_fired()
    assert owner.health_snapshot().active is False
    state = store.load()
    assert state is not None
    failed_health = probe_health(state, timeout=0.5)
    assert failed_health is not None
    assert failed_health["active_producer_id"] is None

    owner.init_app(app, _TelemetryCollector().enqueue)
    increment_line = line_after(
        "self._shared.reference_count += 1",
        "self._generation = self._shared.active.generation",
    )
    increment_trace, increment_fired = interrupt_at(
        FlowSightLifecycle.init_app,
        increment_line,
        "after duplicate refcount increment",
    )
    try:
        sys.settrace(increment_trace)
        with pytest.raises(KeyboardInterrupt, match="after duplicate refcount increment"):
            joiner.init_app(app, _TelemetryCollector().enqueue)
    finally:
        sys.settrace(None)
    assert increment_fired()

    joiner.init_app(app, _TelemetryCollector().enqueue)
    shutdown_lines, shutdown_start = inspect.getsourcelines(FlowSightLifecycle.shutdown)
    decrement_index = next(
        index
        for index, line in enumerate(shutdown_lines)
        if "self._shared.reference_count -= 1" in line
    )
    clear_index = next(
        index
        for index in range(decrement_index + 1, len(shutdown_lines))
        if "self._generation = None" in shutdown_lines[index]
    )
    detach_trace, detach_fired = interrupt_at(
        FlowSightLifecycle.shutdown,
        shutdown_start + clear_index,
        "after duplicate refcount decrement",
    )
    try:
        sys.settrace(detach_trace)
        with pytest.raises(KeyboardInterrupt, match="after duplicate refcount decrement"):
            owner.shutdown(timeout=3.0)
    finally:
        sys.settrace(None)
    assert detach_fired()
    assert owner.shutdown(timeout=3.0) is True
    assert joiner.health_snapshot().active is True
    assert joiner.shutdown(timeout=3.0) is True

    state = store.load()
    if state is not None and probe_health(state, timeout=0.2) is not None:
        stop_sidecar(state, timeout=2.0)
        assert _wait_until(lambda: store.load() is None, timeout=5.0)
    provider.shutdown()


def test_coordinated_shutdown_timeout_retries_before_release_and_reinit(
    tmp_path: Path,
) -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    config = RuntimeConfig(
        runtime_dir=tmp_path / "retryable-shutdown-runtime",
        project_id="retryable-shutdown-project",
        default_port=0,
        startup_timeout=5.0,
        idle_timeout=10.0,
        lease_ttl=1.0,
    )
    lifecycle = FlowSightLifecycle(config, provider)
    store = StateStore(config.runtime_dir)
    enqueue_started = threading.Event()
    release_enqueue = threading.Event()
    accepted_events: list[TelemetryEvent] = []

    def blocked_enqueue(event: TelemetryEvent) -> bool:
        enqueue_started.set()
        if not release_enqueue.wait(3.0):
            raise TimeoutError
        accepted_events.append(event)
        return True

    app = FastAPI()
    initialization = lifecycle.init_app(app, blocked_enqueue)

    @app.get("/blocked")
    def blocked_route() -> dict[str, bool]:
        return {"ok": True}

    def finish_request() -> None:
        _asgi_get(app, "/blocked")

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(finish_request)
            assert enqueue_started.wait(1.0)
            assert lifecycle.shutdown(timeout=0.01) is False
            failed_health = lifecycle.health_snapshot()
            assert failed_health.active is True
            assert failed_health.shutdown_failure_count == 1
            assert failed_health.shutdown_last_error_code == "TELEMETRY_DRAIN_TIMEOUT"
            sidecar_health = probe_health(initialization.sidecar.state, timeout=0.5)
            assert sidecar_health is not None
            assert sidecar_health["active_producer_id"] == initialization.producer_id
            release_enqueue.set()
            future.result(timeout=2.0)

        assert len(accepted_events) == 1
        assert lifecycle.shutdown(timeout=3.0) is True
        assert lifecycle.health_snapshot().active is False

        replacement_collector = _TelemetryCollector()
        reinitialized = lifecycle.init_app(app, replacement_collector.enqueue)
        assert reinitialized.telemetry.processor is initialization.telemetry.processor
        _asgi_get(app, "/blocked")
        assert len(replacement_collector.snapshot()) == 1
        assert lifecycle.shutdown(timeout=3.0) is True
    finally:
        release_enqueue.set()
        lifecycle.shutdown(timeout=3.0)
        state = store.load()
        if state is not None and probe_health(state, timeout=0.2) is not None:
            stop_sidecar(state, timeout=2.0)
            assert _wait_until(lambda: store.load() is None, timeout=5.0)
        provider.shutdown()


def test_concurrent_shutdown_lock_wait_is_bounded_and_visible(tmp_path: Path) -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    config = RuntimeConfig(
        runtime_dir=tmp_path / "bounded-coordinator-lock-runtime",
        project_id="bounded-coordinator-lock-project",
        default_port=0,
        startup_timeout=5.0,
        idle_timeout=10.0,
        lease_ttl=1.0,
    )
    lifecycle = FlowSightLifecycle(config, provider)
    observer = FlowSightLifecycle(config, provider)
    store = StateStore(config.runtime_dir)
    flush_started = threading.Event()
    release_flush = threading.Event()

    def blocked_flush(_timeout: float) -> bool:
        flush_started.set()
        return release_flush.wait(2.0)

    lifecycle.init_app(
        FastAPI(),
        _TelemetryCollector().enqueue,
        flush=blocked_flush,
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            primary_shutdown = executor.submit(lifecycle.shutdown, 2.0)
            assert flush_started.wait(1.0)
            timeout_started = time.monotonic()
            assert observer.shutdown(timeout=0.01) is False
            timeout_elapsed = time.monotonic() - timeout_started
            assert timeout_elapsed < 0.25
            release_flush.set()
            assert primary_shutdown.result(timeout=2.0) is True

        health = observer.health_snapshot()
        assert health.active is False
        assert health.shutdown_failure_count == 1
        assert health.shutdown_last_error_code == "COORDINATOR_LOCK_TIMEOUT"
    finally:
        release_flush.set()
        lifecycle.shutdown(timeout=3.0)
        state = store.load()
        if state is not None and probe_health(state, timeout=0.2) is not None:
            stop_sidecar(state, timeout=2.0)
            assert _wait_until(lambda: store.load() is None, timeout=5.0)
        provider.shutdown()


def test_baseexception_flush_resets_draining_and_shutdown_retry_releases_lease(
    tmp_path: Path,
) -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    config = RuntimeConfig(
        runtime_dir=tmp_path / "baseexception-flush-runtime",
        project_id="baseexception-flush-project",
        default_port=0,
        startup_timeout=5.0,
        idle_timeout=10.0,
        lease_ttl=1.0,
    )
    lifecycle = FlowSightLifecycle(config, provider)
    store = StateStore(config.runtime_dir)
    flush_calls = 0

    def interrupt_once(_timeout: float) -> bool:
        nonlocal flush_calls
        flush_calls += 1
        if flush_calls == 1:
            raise KeyboardInterrupt("flush interrupted")
        return True

    initialization = lifecycle.init_app(
        FastAPI(),
        _TelemetryCollector().enqueue,
        flush=interrupt_once,
    )
    try:
        with pytest.raises(KeyboardInterrupt, match="flush interrupted"):
            lifecycle.shutdown(timeout=1.0)
        processor_health = initialization.telemetry.processor.health_snapshot()
        assert processor_health.active is False
        assert processor_health.draining is False
        assert lifecycle.health_snapshot().active is True

        assert lifecycle.shutdown(timeout=3.0) is True
        assert flush_calls == 2
        sidecar_health = probe_health(initialization.sidecar.state, timeout=0.5)
        assert sidecar_health is not None
        assert sidecar_health["active_producer_id"] is None
    finally:
        lifecycle.shutdown(timeout=3.0)
        state = store.load()
        if state is not None and probe_health(state, timeout=0.2) is not None:
            stop_sidecar(state, timeout=2.0)
            assert _wait_until(lambda: store.load() is None, timeout=5.0)
        provider.shutdown()


def test_final_shutdown_registry_release_baseexception_keeps_retry_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    config = RuntimeConfig(
        runtime_dir=tmp_path / "release-interrupt-runtime",
        project_id="release-interrupt-project",
        default_port=0,
        startup_timeout=5.0,
        idle_timeout=10.0,
        lease_ttl=1.0,
    )
    lifecycle = FlowSightLifecycle(config, provider)
    store = StateStore(config.runtime_dir)
    lifecycle.init_app(FastAPI(), _TelemetryCollector().enqueue)
    registry = lifecycle._shared.registry
    real_release = registry.release_binding
    release_calls = 0

    def interrupt_once(release_provider: TracerProvider) -> None:
        nonlocal release_calls
        release_calls += 1
        if release_calls == 1:
            raise KeyboardInterrupt("registry release interrupted")
        real_release(release_provider)

    monkeypatch.setattr(registry, "release_binding", interrupt_once)
    try:
        with pytest.raises(KeyboardInterrupt, match="registry release interrupted"):
            lifecycle.shutdown(timeout=3.0)
        assert lifecycle.health_snapshot().active is True
        assert registry.snapshot() is not None

        assert lifecycle.shutdown(timeout=3.0) is True
        assert release_calls == 2
        assert lifecycle.health_snapshot().active is False
        assert registry.snapshot() is None
    finally:
        monkeypatch.setattr(registry, "release_binding", real_release)
        lifecycle.shutdown(timeout=3.0)
        state = store.load()
        if state is not None and probe_health(state, timeout=0.2) is not None:
            stop_sidecar(state, timeout=2.0)
            assert _wait_until(lambda: store.load() is None, timeout=5.0)
        provider.shutdown()


def test_pending_cleanup_registry_release_baseexception_keeps_retry_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import spikes.sidecar_otel.lifecycle as lifecycle_module

    provider = TracerProvider(sampler=ALWAYS_ON)
    config = RuntimeConfig(
        runtime_dir=tmp_path / "pending-release-interrupt-runtime",
        project_id="pending-release-interrupt-project",
        default_port=0,
        startup_timeout=5.0,
        idle_timeout=10.0,
        lease_ttl=1.0,
    )
    lifecycle = FlowSightLifecycle(config, provider)
    store = StateStore(config.runtime_dir)
    registry = lifecycle._shared.registry
    real_release = registry.release_binding
    real_instrument = lifecycle_module.instrument_fastapi_app
    release_calls = 0

    def fail_instrumentation(*_args, **_kwargs):
        raise RuntimeError("instrumentation failed")

    def interrupt_once(release_provider: TracerProvider) -> None:
        nonlocal release_calls
        release_calls += 1
        if release_calls == 1:
            raise KeyboardInterrupt("pending registry release interrupted")
        real_release(release_provider)

    monkeypatch.setattr(lifecycle_module, "instrument_fastapi_app", fail_instrumentation)
    monkeypatch.setattr(registry, "release_binding", interrupt_once)
    try:
        with pytest.raises(KeyboardInterrupt, match="pending registry release interrupted"):
            lifecycle.init_app(FastAPI(), _TelemetryCollector().enqueue)
        assert lifecycle.health_snapshot().active is True
        assert registry.snapshot() is not None

        assert lifecycle.shutdown(timeout=3.0) is True
        assert release_calls == 2
        assert lifecycle.health_snapshot().active is False
        assert registry.snapshot() is None
    finally:
        monkeypatch.setattr(lifecycle_module, "instrument_fastapi_app", real_instrument)
        monkeypatch.setattr(registry, "release_binding", real_release)
        lifecycle.shutdown(timeout=3.0)
        state = store.load()
        if state is not None and probe_health(state, timeout=0.2) is not None:
            stop_sidecar(state, timeout=2.0)
            assert _wait_until(lambda: store.load() is None, timeout=5.0)
        provider.shutdown()


def test_permanent_flush_failure_abandons_with_visible_loss_and_no_lease_leak(
    tmp_path: Path,
) -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    config = RuntimeConfig(
        runtime_dir=tmp_path / "permanent-flush-runtime",
        project_id="permanent-flush-project",
        default_port=0,
        startup_timeout=5.0,
        idle_timeout=10.0,
        lease_ttl=1.0,
    )
    lifecycle = FlowSightLifecycle(config, provider)
    store = StateStore(config.runtime_dir)
    app = FastAPI()
    flush_calls = 0

    def failed_flush(_timeout: float) -> bool:
        nonlocal flush_calls
        flush_calls += 1
        return False

    initialization = lifecycle.init_app(
        app,
        _TelemetryCollector().enqueue,
        flush=failed_flush,
    )
    try:
        assert lifecycle.shutdown(timeout=1.0) is False
        assert lifecycle.health_snapshot().active is True
        assert lifecycle.shutdown(timeout=1.0) is False
        failed_health = lifecycle.health_snapshot()
        assert failed_health.active is False
        assert failed_health.shutdown_last_error_code == "TELEMETRY_FLUSH_ABANDONED"
        assert flush_calls == 2
        sidecar_health = probe_health(initialization.sidecar.state, timeout=0.5)
        assert sidecar_health is not None
        assert sidecar_health["active_producer_id"] is None
        assert lifecycle.shutdown(timeout=0.0) is True

        reinitialized = lifecycle.init_app(app, _TelemetryCollector().enqueue)
        assert reinitialized.producer_id != initialization.producer_id
        assert lifecycle.shutdown(timeout=3.0) is True
    finally:
        lifecycle.shutdown(timeout=3.0)
        state = store.load()
        if state is not None and probe_health(state, timeout=0.2) is not None:
            stop_sidecar(state, timeout=2.0)
            assert _wait_until(lambda: store.load() is None, timeout=5.0)
        provider.shutdown()


def test_provider_terminal_shutdown_does_not_wedge_lifecycle_lease_cleanup(
    tmp_path: Path,
) -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    config = RuntimeConfig(
        runtime_dir=tmp_path / "terminal-provider-runtime",
        project_id="terminal-provider-project",
        default_port=0,
        startup_timeout=5.0,
        idle_timeout=10.0,
        lease_ttl=1.0,
    )
    lifecycle = FlowSightLifecycle(config, provider)
    store = StateStore(config.runtime_dir)
    initialization = lifecycle.init_app(
        FastAPI(),
        _TelemetryCollector().enqueue,
        flush=lambda _timeout: False,
    )
    provider.shutdown()
    try:
        assert lifecycle.shutdown(timeout=1.0) is False
        assert lifecycle.shutdown(timeout=1.0) is False
        health = lifecycle.health_snapshot()
        assert health.active is False
        assert health.shutdown_last_error_code == "TELEMETRY_FLUSH_ABANDONED"
        sidecar_health = probe_health(initialization.sidecar.state, timeout=0.5)
        assert sidecar_health is not None
        assert sidecar_health["active_producer_id"] is None
    finally:
        lifecycle.shutdown(timeout=3.0)
        state = store.load()
        if state is not None and probe_health(state, timeout=0.2) is not None:
            stop_sidecar(state, timeout=2.0)
            assert _wait_until(lambda: store.load() is None, timeout=5.0)


def test_coordinated_shutdown_retries_late_failure_and_observes_lost_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import spikes.sidecar_otel.lifecycle as lifecycle_module

    provider = TracerProvider(sampler=ALWAYS_ON)
    config = RuntimeConfig(
        runtime_dir=tmp_path / "ambiguous-goodbye-runtime",
        project_id="ambiguous-goodbye-project",
        default_port=0,
        startup_timeout=5.0,
        idle_timeout=10.0,
        lease_ttl=1.0,
    )
    lifecycle = FlowSightLifecycle(config, provider)
    store = StateStore(config.runtime_dir)
    app = FastAPI()
    initialization = lifecycle.init_app(app, _TelemetryCollector().enqueue)
    real_goodbye = lifecycle_module.goodbye
    goodbye_calls = 0

    def fail_then_commit_and_lose(*args, **kwargs) -> None:
        nonlocal goodbye_calls
        goodbye_calls += 1
        if goodbye_calls == 1:
            raise TimeoutError("request failed before goodbye commit")
        real_goodbye(*args, **kwargs)
        raise TimeoutError("response lost after committed goodbye")

    monkeypatch.setattr(lifecycle_module, "goodbye", fail_then_commit_and_lose)
    try:
        assert lifecycle.shutdown(timeout=3.0) is False
        assert lifecycle.health_snapshot().shutdown_last_error_code == "LEASE_RELEASE_FAILED"
        with pytest.raises(RuntimeError, match="shutdown must complete"):
            lifecycle.init_app(app, _TelemetryCollector().enqueue)
        assert lifecycle.shutdown(timeout=3.0) is True
        assert goodbye_calls == 2
        assert lifecycle.health_snapshot().active is False
        health = probe_health(initialization.sidecar.state, timeout=0.5)
        assert health is not None
        assert health["active_producer_id"] is None

        monkeypatch.setattr(lifecycle_module, "goodbye", real_goodbye)
        reinitialized = lifecycle.init_app(app, _TelemetryCollector().enqueue)
        assert reinitialized.sidecar.state.startup_id == initialization.sidecar.state.startup_id
        assert lifecycle.shutdown(timeout=3.0) is True
    finally:
        monkeypatch.setattr(lifecycle_module, "goodbye", real_goodbye)
        lifecycle.shutdown(timeout=3.0)
        state = store.load()
        if state is not None and probe_health(state, timeout=0.2) is not None:
            stop_sidecar(state, timeout=2.0)
            assert _wait_until(lambda: store.load() is None, timeout=5.0)
        provider.shutdown()


def test_coordinated_init_observes_committed_hello_after_lost_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import spikes.sidecar_otel.lifecycle as lifecycle_module

    provider = TracerProvider(sampler=ALWAYS_ON)
    config = RuntimeConfig(
        runtime_dir=tmp_path / "ambiguous-hello-runtime",
        project_id="ambiguous-hello-project",
        default_port=0,
        startup_timeout=5.0,
        idle_timeout=10.0,
        lease_ttl=1.0,
    )
    lifecycle = FlowSightLifecycle(config, provider)
    store = StateStore(config.runtime_dir)
    app = FastAPI()
    real_hello = lifecycle_module.hello
    hello_calls = 0

    def committed_then_lost(*args, **kwargs) -> str:
        nonlocal hello_calls
        hello_calls += 1
        real_hello(*args, **kwargs)
        raise TimeoutError("response lost after committed hello")

    monkeypatch.setattr(lifecycle_module, "hello", committed_then_lost)
    try:
        initialization = lifecycle.init_app(app, _TelemetryCollector().enqueue)
        assert hello_calls == 1
        health = probe_health(initialization.sidecar.state, timeout=0.5)
        assert health is not None
        assert health["active_producer_id"] == initialization.producer_id
        assert health["active_lease_id"] == initialization.lease_id
        monkeypatch.setattr(lifecycle_module, "hello", real_hello)
        assert lifecycle.shutdown(timeout=3.0) is True
    finally:
        monkeypatch.setattr(lifecycle_module, "hello", real_hello)
        lifecycle.shutdown(timeout=3.0)
        state = store.load()
        if state is not None and probe_health(state, timeout=0.2) is not None:
            stop_sidecar(state, timeout=2.0)
            assert _wait_until(lambda: store.load() is None, timeout=5.0)
        provider.shutdown()


def test_coordinated_init_observes_exact_lease_after_lost_renew_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import spikes.sidecar_otel.lifecycle as lifecycle_module

    provider = TracerProvider(sampler=ALWAYS_ON)
    config = RuntimeConfig(
        runtime_dir=tmp_path / "ambiguous-renew-runtime",
        project_id="ambiguous-renew-project",
        default_port=0,
        startup_timeout=5.0,
        idle_timeout=10.0,
        lease_ttl=1.0,
    )
    lifecycle = FlowSightLifecycle(config, provider)
    store = StateStore(config.runtime_dir)
    real_renew = lifecycle_module.renew
    committed_renewals = 0

    def committed_then_lost(*args, **kwargs) -> None:
        nonlocal committed_renewals
        real_renew(*args, **kwargs)
        committed_renewals += 1
        raise TimeoutError("response lost after committed renew")

    monkeypatch.setattr(lifecycle_module, "renew", committed_then_lost)
    try:
        initialization = lifecycle.init_app(FastAPI(), _TelemetryCollector().enqueue)
        assert committed_renewals >= 1
        health = probe_health(initialization.sidecar.state, timeout=0.5)
        assert health is not None
        assert health["active_producer_id"] == initialization.producer_id
        assert health["active_lease_id"] == initialization.lease_id
        monkeypatch.setattr(lifecycle_module, "renew", real_renew)
        assert lifecycle.shutdown(timeout=3.0) is True
    finally:
        monkeypatch.setattr(lifecycle_module, "renew", real_renew)
        lifecycle.shutdown(timeout=3.0)
        state = store.load()
        if state is not None and probe_health(state, timeout=0.2) is not None:
            stop_sidecar(state, timeout=2.0)
            assert _wait_until(lambda: store.load() is None, timeout=5.0)
        provider.shutdown()


def test_coordinated_reinit_rotates_producer_for_fresh_sender_sequence(
    tmp_path: Path,
) -> None:
    class DeferredDelivery:
        def __init__(self) -> None:
            self.adapter: TelemetrySenderAdapter | None = None
            self.sender: BoundedSender | None = None

        def enqueue(self, event: TelemetryEvent) -> bool:
            if self.adapter is None:
                raise RuntimeError("delivery adapter is not ready")
            return self.adapter.enqueue(event)

        def flush(self, timeout: float) -> bool:
            if self.sender is None:
                raise RuntimeError("sender is not ready")
            self.sender.flush(timeout)
            return True

    provider = TracerProvider(sampler=ALWAYS_ON)
    config = RuntimeConfig(
        runtime_dir=tmp_path / "coordinated-reinit-runtime",
        project_id="coordinated-reinit-project",
        default_port=0,
        startup_timeout=5.0,
        idle_timeout=10.0,
        lease_ttl=1.0,
    )
    lifecycle = FlowSightLifecycle(config, provider)
    store = StateStore(config.runtime_dir)
    app = FastAPI()
    sessions: list[LifecycleInitialization] = []

    @app.get("/request")
    def request_route() -> dict[str, bool]:
        return {"ok": True}

    def run_session(prefix: str) -> None:
        delivery = DeferredDelivery()
        initialization = lifecycle.init_app(app, delivery.enqueue, flush=delivery.flush)
        sender = BoundedSender(
            project_id=initialization.sidecar.state.project_id,
            producer_id=initialization.producer_id,
            lease_id=initialization.lease_id,
            transport=HTTPBatchTransport(initialization.sidecar.state),
        )
        delivery.sender = sender
        delivery.adapter = TelemetrySenderAdapter(sender, event_id_prefix=prefix)
        _asgi_get(app, "/request")
        assert lifecycle.shutdown(timeout=3.0) is True
        sender.close(timeout=2.0)
        sessions.append(initialization)

    try:
        run_session("session-one")
        run_session("session-two")
        assert sessions[0].sidecar.state.startup_id == sessions[1].sidecar.state.startup_id
        assert sessions[0].producer_id != sessions[1].producer_id
        connection = sqlite3.connect(store.database_path)
        try:
            receipts = connection.execute(
                "SELECT producer_id, producer_seq, event_id FROM event_receipts "
                "ORDER BY committed_at_ns"
            ).fetchall()
        finally:
            connection.close()
        assert receipts == [
            (sessions[0].producer_id, 1, "session-one:1"),
            (sessions[1].producer_id, 1, "session-two:1"),
        ]
    finally:
        lifecycle.shutdown(timeout=3.0)
        state = store.load()
        if state is not None and probe_health(state, timeout=0.2) is not None:
            stop_sidecar(state, timeout=2.0)
            assert _wait_until(lambda: store.load() is None, timeout=5.0)
        provider.shutdown()


def test_failed_init_cleanup_is_adoptable_and_stale_handle_cannot_stop_new_generation(
    tmp_path: Path,
) -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    config = RuntimeConfig(
        runtime_dir=tmp_path / "failed-init-runtime",
        project_id="failed-init-project",
        default_port=0,
        startup_timeout=5.0,
        idle_timeout=10.0,
        lease_ttl=1.0,
    )
    failed_app = FastAPI()
    conflicting_processor = FlowSightSpanProcessor(lambda _event: True)
    instrument_fastapi_app(
        failed_app,
        tracer_provider=provider,
        processor=conflicting_processor,
    )
    failed_lifecycle = FlowSightLifecycle(config, provider)
    store = StateStore(config.runtime_dir)
    flush_calls = 0

    def permanently_failed_flush(_timeout: float) -> bool:
        nonlocal flush_calls
        flush_calls += 1
        return False

    with pytest.raises(
        BaseExceptionGroup, match="FlowSight spike initialization failed"
    ) as failure:
        failed_lifecycle.init_app(
            failed_app,
            lambda _event: True,
            flush=permanently_failed_flush,
        )
    assert any("cleanup remains pending" in str(error) for error in failure.value.exceptions)
    assert failed_lifecycle.health_snapshot().active is True

    adopter = FlowSightLifecycle(config, provider)
    assert adopter.shutdown(timeout=3.0) is False
    abandoned_health = adopter.health_snapshot()
    assert abandoned_health.active is False
    assert abandoned_health.shutdown_last_error_code == "TELEMETRY_FLUSH_ABANDONED"
    assert flush_calls == 2

    clean_app = FastAPI()
    collector = _TelemetryCollector()

    @clean_app.get("/clean")
    def clean_probe() -> dict[str, bool]:
        return {"clean": True}

    initialization = adopter.init_app(clean_app, collector.enqueue)
    assert failed_lifecycle.shutdown(timeout=0.0) is True
    assert adopter.health_snapshot().active is True
    _asgi_get(clean_app, "/clean")
    assert len(collector.snapshot()) == 1
    assert adopter.shutdown(timeout=3.0) is True

    state = store.load()
    if state is not None and probe_health(state, timeout=0.2) is not None:
        stop_sidecar(state, timeout=2.0)
        assert _wait_until(lambda: store.load() is None, timeout=5.0)
    assert initialization.telemetry.processor is not conflicting_processor
    provider.shutdown()


def test_heartbeat_start_failure_rolls_back_and_allows_clean_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import spikes.sidecar_otel.lifecycle as lifecycle_module

    provider = TracerProvider(sampler=ALWAYS_ON)
    config = RuntimeConfig(
        runtime_dir=tmp_path / "heartbeat-start-runtime",
        project_id="heartbeat-start-project",
        default_port=0,
        startup_timeout=5.0,
        idle_timeout=10.0,
        lease_ttl=1.0,
    )
    lifecycle = FlowSightLifecycle(config, provider)
    store = StateStore(config.runtime_dir)
    app = FastAPI()
    real_start = lifecycle_module._LeaseHeartbeat.start

    def failed_start(_heartbeat) -> None:
        raise RuntimeError("heartbeat thread unavailable")

    monkeypatch.setattr(lifecycle_module._LeaseHeartbeat, "start", failed_start)
    with pytest.raises(RuntimeError, match="heartbeat thread unavailable"):
        lifecycle.init_app(app, _TelemetryCollector().enqueue)
    assert lifecycle.health_snapshot().active is False

    monkeypatch.setattr(lifecycle_module._LeaseHeartbeat, "start", real_start)
    initialization = lifecycle.init_app(app, _TelemetryCollector().enqueue)
    assert lifecycle.shutdown(timeout=3.0) is True
    state = store.load()
    if state is not None and probe_health(state, timeout=0.2) is not None:
        stop_sidecar(state, timeout=2.0)
        assert _wait_until(lambda: store.load() is None, timeout=5.0)
    assert initialization.sidecar.state.project_id == config.project_id
    provider.shutdown()


def test_heartbeat_renews_exact_lease_during_slow_instrumentation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import spikes.sidecar_otel.lifecycle as lifecycle_module

    provider = TracerProvider(sampler=ALWAYS_ON)
    config = RuntimeConfig(
        runtime_dir=tmp_path / "slow-instrument-runtime",
        project_id="slow-instrument-project",
        default_port=0,
        startup_timeout=5.0,
        idle_timeout=5.0,
        lease_ttl=1.0,
    )
    lifecycle = FlowSightLifecycle(config, provider)
    store = StateStore(config.runtime_dir)
    app = FastAPI()
    real_renew = lifecycle_module.renew
    real_instrument = lifecycle_module.instrument_fastapi_app
    fourth_renewal = threading.Event()
    renewal_count = 0

    def observed_renew(*args, **kwargs) -> None:
        nonlocal renewal_count
        real_renew(*args, **kwargs)
        renewal_count += 1
        if renewal_count >= 4:
            fourth_renewal.set()

    def slow_instrument(*args, **kwargs):
        assert fourth_renewal.wait(2.5)
        return real_instrument(*args, **kwargs)

    monkeypatch.setattr(lifecycle_module, "renew", observed_renew)
    monkeypatch.setattr(lifecycle_module, "instrument_fastapi_app", slow_instrument)
    initialization = lifecycle.init_app(app, _TelemetryCollector().enqueue)
    try:
        assert renewal_count >= 4
        health = probe_health(initialization.sidecar.state, timeout=0.5)
        assert health is not None
        assert health["active_producer_id"] == initialization.producer_id
        assert health["active_lease_id"] == initialization.lease_id
        assert lifecycle.shutdown(timeout=3.0) is True
    finally:
        lifecycle.shutdown(timeout=3.0)
        state = store.load()
        if state is not None and probe_health(state, timeout=0.2) is not None:
            stop_sidecar(state, timeout=2.0)
            assert _wait_until(lambda: store.load() is None, timeout=5.0)
        provider.shutdown()


def test_lease_loss_during_init_never_publishes_session_and_allows_new_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import spikes.sidecar_otel.lifecycle as lifecycle_module

    provider = TracerProvider(sampler=ALWAYS_ON)
    config = RuntimeConfig(
        runtime_dir=tmp_path / "lease-loss-during-init-runtime",
        project_id="lease-loss-during-init-project",
        default_port=0,
        startup_timeout=5.0,
        idle_timeout=5.0,
        lease_ttl=1.0,
    )
    lifecycle = FlowSightLifecycle(config, provider)
    store = StateStore(config.runtime_dir)
    failed_app = FastAPI()
    replacement_app = FastAPI()

    @failed_app.get("/failed")
    def failed_probe() -> dict[str, bool]:
        return {"failed": True}

    @replacement_app.get("/replacement")
    def replacement_probe() -> dict[str, bool]:
        return {"replacement": True}

    real_renew = lifecycle_module.renew
    real_instrument = lifecycle_module.instrument_fastapi_app

    def unavailable_renew(*_args, **_kwargs) -> None:
        raise ConnectionError("lease renewal unavailable during initialization")

    def instrument_after_expiry(*args, **kwargs):
        def lease_is_expired() -> bool:
            state = store.load()
            if state is None:
                return False
            health = probe_health(state, timeout=0.2)
            return health is not None and health["active_producer_id"] is None

        assert _wait_until(lease_is_expired, timeout=3.0)
        return real_instrument(*args, **kwargs)

    monkeypatch.setattr(lifecycle_module, "renew", unavailable_renew)
    monkeypatch.setattr(lifecycle_module, "instrument_fastapi_app", instrument_after_expiry)
    try:
        with pytest.raises(BaseExceptionGroup) as failed_initialization:
            lifecycle.init_app(failed_app, _TelemetryCollector().enqueue)
        assert any(
            isinstance(error, ConnectionError) for error in failed_initialization.value.exceptions
        )
        failed_health = lifecycle.health_snapshot()
        assert failed_health.active is False
        assert failed_health.shutdown_last_error_code == "SIDECAR_FLUSH_ABANDONED"

        monkeypatch.setattr(lifecycle_module, "renew", real_renew)
        monkeypatch.setattr(lifecycle_module, "instrument_fastapi_app", real_instrument)
        replacement_collector = _TelemetryCollector()
        lifecycle.init_app(replacement_app, replacement_collector.enqueue)
        _asgi_get(failed_app, "/failed")
        _asgi_get(replacement_app, "/replacement")
        [replacement_event] = replacement_collector.snapshot()
        assert json.loads(replacement_event.payload_json)["data"]["name"] == "GET /replacement"
        assert lifecycle.shutdown(timeout=3.0) is True
    finally:
        monkeypatch.setattr(lifecycle_module, "renew", real_renew)
        monkeypatch.setattr(lifecycle_module, "instrument_fastapi_app", real_instrument)
        lifecycle.shutdown(timeout=3.0)
        state = store.load()
        if state is not None and probe_health(state, timeout=0.2) is not None:
            stop_sidecar(state, timeout=2.0)
            assert _wait_until(lambda: store.load() is None, timeout=5.0)
        provider.shutdown()


def test_provider_terminal_during_instrumentation_rolls_back_without_lease_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import spikes.sidecar_otel.lifecycle as lifecycle_module

    provider = TracerProvider(sampler=ALWAYS_ON)
    config = RuntimeConfig(
        runtime_dir=tmp_path / "provider-terminal-during-init-runtime",
        project_id="provider-terminal-during-init-project",
        default_port=0,
        startup_timeout=5.0,
        idle_timeout=5.0,
        lease_ttl=1.0,
    )
    lifecycle = FlowSightLifecycle(config, provider)
    store = StateStore(config.runtime_dir)
    real_instrument = lifecycle_module.instrument_fastapi_app

    def terminate_after_instrumentation(*args, **kwargs):
        result = real_instrument(*args, **kwargs)
        provider.shutdown()
        return result

    monkeypatch.setattr(
        lifecycle_module,
        "instrument_fastapi_app",
        terminate_after_instrumentation,
    )
    with pytest.raises(RuntimeError, match="provider became unavailable"):
        lifecycle.init_app(FastAPI(), _TelemetryCollector().enqueue)

    health = lifecycle.health_snapshot()
    assert health.active is False
    state = store.load()
    assert state is not None
    sidecar_health = probe_health(state, timeout=0.5)
    assert sidecar_health is not None
    assert sidecar_health["active_producer_id"] is None
    if probe_health(state, timeout=0.2) is not None:
        stop_sidecar(state, timeout=2.0)
        assert _wait_until(lambda: store.load() is None, timeout=5.0)


def test_expired_lease_finishes_local_cleanup_with_visible_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import spikes.sidecar_otel.lifecycle as lifecycle_module

    provider = TracerProvider(sampler=ALWAYS_ON)
    config = RuntimeConfig(
        runtime_dir=tmp_path / "expired-lease-runtime",
        project_id="expired-lease-project",
        default_port=0,
        startup_timeout=5.0,
        idle_timeout=5.0,
        lease_ttl=1.0,
    )
    lifecycle = FlowSightLifecycle(config, provider)
    store = StateStore(config.runtime_dir)
    real_renew = lifecycle_module.renew

    def failed_renew(*_args, **_kwargs) -> None:
        raise ConnectionError("lease renewal unavailable")

    app = FastAPI()
    initialization = lifecycle.init_app(app, _TelemetryCollector().enqueue)
    monkeypatch.setattr(lifecycle_module, "renew", failed_renew)

    def lease_expired() -> bool:
        health = probe_health(initialization.sidecar.state, timeout=0.2)
        return health is not None and health["active_producer_id"] is None

    assert _wait_until(lease_expired, timeout=3.0)
    assert lifecycle.shutdown(timeout=3.0) is False
    failed_health = lifecycle.health_snapshot()
    assert failed_health.active is False
    assert failed_health.shutdown_last_error_code == "SIDECAR_FLUSH_ABANDONED"
    assert failed_health.heartbeat_error_count >= 1

    monkeypatch.setattr(lifecycle_module, "renew", real_renew)
    # The local resources are closed even though this session lost its lease;
    # reinitialization is no longer wedged.
    reinitialized = lifecycle.init_app(app, _TelemetryCollector().enqueue)
    assert reinitialized.producer_id != initialization.producer_id
    assert lifecycle.shutdown(timeout=3.0) is True
    state = store.load()
    if state is not None and probe_health(state, timeout=0.2) is not None:
        stop_sidecar(state, timeout=2.0)
        assert _wait_until(lambda: store.load() is None, timeout=5.0)
    provider.shutdown()


def test_sender_queue_is_nonblocking_bounded_and_transport_stays_on_worker_thread() -> None:
    transport_started = threading.Event()
    release_transport = threading.Event()
    transport_thread_ids: set[int] = set()
    envelopes: list[dict[str, object]] = []

    def blocked_transport(envelope: dict[str, object], _timeout: float) -> dict[str, object]:
        transport_thread_ids.add(threading.get_ident())
        envelopes.append(envelope)
        transport_started.set()
        if not release_transport.wait(2.0):
            raise TimeoutError
        return _ack(envelope)

    sender = BoundedSender(
        project_id="project",
        producer_id="producer",
        lease_id="lease",
        transport=blocked_transport,
        capacity=2,
        batch_size=1,
    )
    request_thread_id = threading.get_ident()

    assert sender.enqueue(_wire_event("event-1", "trace-1")) is EnqueueOutcome.ACCEPTED
    assert transport_started.wait(1.0)
    with pytest.raises(ValueError, match="already outstanding"):
        sender.enqueue(_wire_event("event-1", "trace-duplicate"))
    assert sender.enqueue(_wire_event("event-2", "trace-2")) is EnqueueOutcome.ACCEPTED
    assert sender.enqueue(_wire_event("event-3", "trace-3")) is EnqueueOutcome.FULL

    blocked_health = sender.health
    assert blocked_health.in_flight_count == 1
    assert blocked_health.queue_depth == 1
    assert blocked_health.dropped_count == 1
    assert blocked_health.impacted_request_trace_ids == ("trace-3",)
    with pytest.raises(SenderTimeoutError):
        sender.flush(timeout=0.01)

    release_transport.set()
    sender.flush(timeout=2.0)
    finished_health = sender.health
    sender.close(timeout=2.0)

    assert finished_health.acked_count == 2
    assert finished_health.failed_count == 0
    assert transport_thread_ids == {finished_health.worker_thread_id}
    assert request_thread_id not in transport_thread_ids
    assert [envelope["batch_id"] for envelope in envelopes] == ["1-1", "2-2"]


def test_sender_retries_same_batch_and_reports_missing_commit_ack_as_incomplete() -> None:
    envelopes: list[dict[str, object]] = []

    def invalid_ack_transport(envelope: dict[str, object], _timeout: float) -> dict[str, object]:
        envelopes.append(envelope)
        return {
            "batch_id": envelope["batch_id"],
            "committed_event_ids": [],
            "duplicate_event_ids": [],
        }

    sender = BoundedSender(
        project_id="project",
        producer_id="producer",
        lease_id="lease",
        transport=invalid_ack_transport,
        max_attempts=2,
    )
    assert (
        sender.enqueue(_wire_event("event-failed", "trace-incomplete")) is EnqueueOutcome.ACCEPTED
    )

    with pytest.raises(DeliveryError):
        sender.flush(timeout=2.0)
    health = sender.health
    with pytest.raises(DeliveryError):
        sender.close(timeout=2.0)

    assert len(envelopes) == 2
    assert envelopes[0] == envelopes[1]
    assert health.failed_count == 1
    assert health.error_count == 1
    assert health.last_error_code == "INCOMPLETE_ACK"
    assert health.impacted_request_trace_ids == ("trace-incomplete",)


def test_sender_rejects_duplicate_ack_entries_and_caps_retry_policy() -> None:
    def duplicate_ack(envelope: dict[str, object], _timeout: float) -> dict[str, object]:
        return {
            "batch_id": envelope["batch_id"],
            "committed_event_ids": ["event", "event"],
            "duplicate_event_ids": [],
        }

    sender = BoundedSender(
        project_id="project",
        producer_id="producer",
        lease_id="lease",
        transport=duplicate_ack,
        max_attempts=1,
    )
    assert sender.enqueue(_wire_event("event", "trace")) is EnqueueOutcome.ACCEPTED
    with pytest.raises(DeliveryError):
        sender.flush(timeout=1.0)
    assert sender.health.last_error_code == "INCOMPLETE_ACK"
    with pytest.raises(DeliveryError):
        sender.close(timeout=1.0)

    with pytest.raises(TransportError, match="INVALID_BATCH"):
        BoundedSender._validate_ack(
            {
                "batch_id": "duplicate-outbound",
                "events": [{"event_id": "same"}, {"event_id": "same"}],
            },
            {
                "batch_id": "duplicate-outbound",
                "committed_event_ids": ["same"],
                "duplicate_event_ids": [],
            },
        )

    with pytest.raises(ValueError, match="1..3"):
        BoundedSender(
            project_id="project",
            producer_id="producer",
            lease_id="lease",
            transport=duplicate_ack,
            max_attempts=4,
        )


def test_sender_close_timeout_is_bounded_then_retry_closes_after_transport_release() -> None:
    transport_started = threading.Event()
    release_transport = threading.Event()

    def noncooperative_test_transport(
        envelope: dict[str, object], _timeout: float
    ) -> dict[str, object]:
        transport_started.set()
        if not release_transport.wait(2.0):
            raise TimeoutError
        return _ack(envelope)

    sender = BoundedSender(
        project_id="project",
        producer_id="producer",
        lease_id="lease",
        transport=noncooperative_test_transport,
    )
    assert sender.enqueue(_wire_event("event", "trace")) is EnqueueOutcome.ACCEPTED
    assert transport_started.wait(1.0)
    with pytest.raises(SenderTimeoutError):
        sender.close(timeout=0.01)
    assert sender.health.state.value == "closing"

    release_transport.set()
    sender.close(timeout=2.0)
    sender.close(timeout=0.0)
    assert sender.health.state.value == "closed"


def test_terminal_delivery_failures_mark_more_than_health_id_cap_incomplete() -> None:
    transport_started = threading.Event()
    release_transport = threading.Event()
    processor = FlowSightSpanProcessor(
        lambda _event: True,
        project_id="project",
        association_capacity=128,
    )

    def observe_failure(failure: DeliveryFailure) -> None:
        processor.mark_delivery_failure(
            event.request_trace_id for event in failure.events if event.request_trace_id is not None
        )

    def failed_transport(_envelope: dict[str, object], _timeout: float) -> dict[str, object]:
        transport_started.set()
        if not release_transport.wait(2.0):
            raise TimeoutError
        raise TransportError("HTTP_503")

    event_count = 70
    sender = BoundedSender(
        project_id="project",
        producer_id="producer",
        lease_id="lease",
        transport=failed_transport,
        capacity=event_count,
        batch_size=event_count,
        max_attempts=1,
        failure_observer=observe_failure,
    )

    def enqueue(index: int) -> None:
        assert (
            sender.enqueue(
                WireEvent(
                    event_id=f"failure-{index}",
                    event_type="span.ended",
                    schema_version=1,
                    timestamp_ns=index + 1,
                    payload_json=_safe_envelope_value(f"failure-{index}"),
                    request_trace_id=f"request-{index}",
                    otel_trace_id="a" * 32,
                    span_id=f"{index + 1:016x}",
                )
            )
            is EnqueueOutcome.ACCEPTED
        )

    enqueue(0)
    assert transport_started.wait(1.0)
    for index in range(1, event_count):
        enqueue(index)
    release_transport.set()

    with pytest.raises(DeliveryError):
        sender.flush(timeout=2.0)
    sender_health = sender.health
    processor_health = processor.health_snapshot()
    with pytest.raises(DeliveryError):
        sender.close(timeout=2.0)

    assert len(sender_health.impacted_request_trace_ids) == 64
    assert sender_health.impacted_request_overflow_count == 6
    assert sender_health.first_failed_producer_seq == 1
    assert sender_health.last_failed_producer_seq == event_count
    assert processor_health.delivery_failure_count == event_count
    assert set(processor_health.incomplete_request_ids) == {
        f"request-{index}" for index in range(event_count)
    }


@pytest.mark.parametrize(
    ("large_payload", "event_count"),
    [
        (_safe_envelope_bytes(15_000), 80),
        (_safe_envelope_value("😀" * 4_000, ensure_ascii=False), 30),
    ],
)
def test_sender_splits_large_valid_events_below_protocol_batch_budget(
    large_payload: str, event_count: int
) -> None:
    envelope_sizes: list[int] = []
    envelope_event_ids: list[list[str]] = []
    first_transport_started = threading.Event()
    release_first_transport = threading.Event()

    def record_transport(envelope: dict[str, object], _timeout: float) -> dict[str, object]:
        envelope_sizes.append(
            len(json.dumps(envelope, separators=(",", ":"), ensure_ascii=True).encode())
        )
        events = envelope["events"]
        assert type(events) is list
        envelope_event_ids.append([event["event_id"] for event in events])
        if len(envelope_sizes) == 1:
            first_transport_started.set()
            if not release_first_transport.wait(2.0):
                raise TimeoutError
        return _ack(envelope)

    sender = BoundedSender(
        project_id="project",
        producer_id="producer",
        lease_id="lease",
        transport=record_transport,
        capacity=event_count,
        batch_size=event_count,
    )

    def enqueue(index: int) -> None:
        outcome = sender.enqueue(
            WireEvent(
                event_id=f"large-{index}",
                event_type="span.ended",
                schema_version=1,
                timestamp_ns=index + 1,
                payload_json=large_payload,
                request_trace_id=f"trace-{index}",
                otel_trace_id="a" * 32,
                span_id="b" * 16,
            )
        )
        assert outcome is EnqueueOutcome.ACCEPTED

    enqueue(0)
    assert first_transport_started.wait(1.0)
    for index in range(1, event_count):
        enqueue(index)
    release_first_transport.set()

    sender.flush(timeout=2.0)
    health = sender.health
    sender.close(timeout=2.0)

    assert len(envelope_sizes) > 1
    assert any(len(event_ids) > 1 for event_ids in envelope_event_ids[1:])
    assert {event_id for batch in envelope_event_ids for event_id in batch} == {
        f"large-{index}" for index in range(event_count)
    }
    assert max(envelope_sizes) <= 1024 * 1024
    assert health.acked_count == event_count
    assert health.failed_count == 0


def test_sender_rejects_non_utf8_identifier_before_it_can_kill_worker() -> None:
    with pytest.raises(ValueError, match="ASCII"):
        WireEvent(
            event_id="bad-\ud800",
            event_type="span.ended",
            schema_version=1,
            timestamp_ns=1,
            payload_json="{}",
        )


def test_processor_adapter_propagates_sender_full_and_suppresses_internal_transport() -> None:
    transport_started = threading.Event()
    release_transport = threading.Event()
    suppression_observations: list[tuple[bool, bool]] = []

    def blocked_transport(envelope: dict[str, object], _timeout: float) -> dict[str, object]:
        suppression_observations.append(
            (is_instrumentation_enabled(), is_http_instrumentation_enabled())
        )
        transport_started.set()
        if not release_transport.wait(2.0):
            raise TimeoutError
        return _ack(envelope)

    sender = BoundedSender(
        project_id="project",
        producer_id="producer",
        lease_id="lease",
        transport=blocked_transport,
        capacity=1,
        batch_size=1,
    )
    adapter = TelemetrySenderAdapter(
        sender,
        event_id_prefix="adapter",
        clock_ns=lambda: 1,
    )
    provider = TracerProvider(sampler=ALWAYS_ON)
    binding = ProviderRegistry().bind(
        provider,
        adapter.enqueue,
        flush=lambda timeout: sender.flush(timeout),
        project_id="project",
    )
    tracer = provider.get_tracer("trial-004-adapter")

    with tracer.start_as_current_span("request-1", kind=SpanKind.SERVER):
        pass
    assert transport_started.wait(1.0)
    with tracer.start_as_current_span("request-2", kind=SpanKind.SERVER) as second:
        second_context = second.get_span_context()
        second_request_id = derive_request_trace_id(
            "project", second_context.trace_id, second_context.span_id
        )

    processor_health = binding.processor.health_snapshot()
    sender_health = sender.health
    assert processor_health.queue_drop_count == 1
    assert processor_health.incomplete_request_ids == (second_request_id,)
    assert sender_health.dropped_count == 1
    assert sender_health.impacted_request_trace_ids == (second_request_id,)

    release_transport.set()
    assert binding.deactivate(timeout=2.0) is True
    sender.close(timeout=2.0)
    assert suppression_observations == [(False, False)]

    provider.shutdown()


def test_existing_provider_is_preserved_with_one_reusable_processor() -> None:
    class CountingProvider(TracerProvider):
        def __init__(self) -> None:
            self.added_processors: list[object] = []
            super().__init__(sampler=ALWAYS_ON)

        def add_span_processor(self, span_processor) -> None:
            self.added_processors.append(span_processor)
            super().add_span_processor(span_processor)

    provider = CountingProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    collector = _TelemetryCollector()
    flush_calls: list[float] = []
    registry = ProviderRegistry()

    binding = registry.bind(
        provider,
        collector.enqueue,
        flush=lambda timeout: flush_calls.append(timeout),
        project_id="project",
    )
    rebound = registry.bind(provider, collector.enqueue, project_id="project")
    tracer = provider.get_tracer("trial-004-existing-provider")
    with tracer.start_as_current_span("request-1", kind=SpanKind.SERVER):
        with tracer.start_as_current_span("child-1"):
            pass

    snapshot = binding.snapshot()
    initial_event_count = len(collector.snapshot())
    assert rebound is binding
    assert provider.added_processors.count(binding.processor) == 1
    assert snapshot.registration_count == 1
    assert snapshot.owns_provider is False
    assert initial_event_count == 2

    assert binding.deactivate(timeout=0.25) is True
    with tracer.start_as_current_span("user-span-after-flowsight-shutdown"):
        pass
    assert len(collector.snapshot()) == initial_event_count
    assert any(
        span.name == "user-span-after-flowsight-shutdown" for span in exporter.get_finished_spans()
    )

    replacement_collector = _TelemetryCollector()
    reactivated = registry.bind(provider, replacement_collector.enqueue, project_id="project")
    with tracer.start_as_current_span("request-2", kind=SpanKind.SERVER):
        pass
    assert reactivated.processor is binding.processor
    assert provider.added_processors.count(binding.processor) == 1
    assert len(collector.snapshot()) == initial_event_count
    assert len(replacement_collector.snapshot()) == 1
    assert len(flush_calls) == 1
    assert 0 <= flush_calls[0] <= 0.25

    provider.shutdown()


def test_registry_instances_share_exactly_one_processor_per_provider() -> None:
    class CountingProvider(TracerProvider):
        def __init__(self) -> None:
            self.flowsight_add_count = 0
            super().__init__(sampler=ALWAYS_ON)

        def add_span_processor(self, span_processor) -> None:
            if isinstance(span_processor, FlowSightSpanProcessor):
                self.flowsight_add_count += 1
            super().add_span_processor(span_processor)

    provider = CountingProvider()
    first_collector = _TelemetryCollector()
    second_collector = _TelemetryCollector()
    first = ProviderRegistry().bind(provider, first_collector.enqueue, project_id="project")
    second = ProviderRegistry().bind(provider, second_collector.enqueue, project_id="project")

    with provider.get_tracer("trial-004-global-registry").start_as_current_span(
        "request", kind=SpanKind.SERVER
    ):
        pass

    assert second is not first
    assert second.processor is first.processor
    assert provider.flowsight_add_count == 1
    assert len(first_collector.snapshot()) == 1
    assert second_collector.snapshot() == ()

    provider.shutdown()


def test_registry_cache_survives_binding_gc_without_duplicate_processor() -> None:
    class CountingProvider(TracerProvider):
        def __init__(self) -> None:
            self.flowsight_add_count = 0
            super().__init__(sampler=ALWAYS_ON)

        def add_span_processor(self, span_processor) -> None:
            if isinstance(span_processor, FlowSightSpanProcessor):
                self.flowsight_add_count += 1
            super().add_span_processor(span_processor)

    provider = CountingProvider()
    first_registry = ProviderRegistry()
    first_binding = first_registry.bind(provider, lambda _event: True)
    processor = first_binding.processor
    binding_reference = ref(first_binding)

    del first_binding
    del first_registry
    gc.collect()
    assert binding_reference() is None

    second_binding = ProviderRegistry().bind(provider, lambda _event: True)
    assert second_binding.processor is processor
    assert provider.flowsight_add_count == 1

    provider.shutdown()


def test_provider_registration_rejects_a_second_project() -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    first = ProviderRegistry().bind(provider, lambda _event: True, project_id="project-a")
    with pytest.raises(UnsupportedTracerProviderError, match="multiple FlowSight projects"):
        ProviderRegistry().bind(provider, lambda _event: True, project_id="project-b")
    assert first.deactivate(timeout=0.25) is True
    provider.shutdown()


def test_ambiguous_provider_add_fails_closed_without_duplicate_processor() -> None:
    class CommitThenFailProvider(TracerProvider):
        def __init__(self) -> None:
            self.flowsight_add_count = 0
            super().__init__(sampler=ALWAYS_ON)

        def add_span_processor(self, span_processor) -> None:
            super().add_span_processor(span_processor)
            if isinstance(span_processor, FlowSightSpanProcessor):
                self.flowsight_add_count += 1
                raise RuntimeError("response lost after processor registration")

    provider = CommitThenFailProvider()
    collector = _TelemetryCollector()
    with pytest.raises(RuntimeError, match="response lost"):
        ProviderRegistry().bind(provider, collector.enqueue, project_id="project")
    with pytest.raises(UnsupportedTracerProviderError, match="unsafe to retry"):
        ProviderRegistry().bind(provider, collector.enqueue, project_id="project")

    with provider.get_tracer("trial-004-ambiguous-provider-add").start_as_current_span(
        "request-after-ambiguous-add",
        kind=SpanKind.SERVER,
    ):
        pass
    assert provider.flowsight_add_count == 1
    assert collector.snapshot() == ()
    provider.shutdown()


@pytest.mark.parametrize("mode", ["owned", "existing", "unsupported"])
def test_global_provider_installation_and_ownership_contract_in_fresh_process(
    mode: str,
) -> None:
    source = r"""
import json
import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from spikes.sidecar_otel.telemetry import (
    ProviderRegistry,
    UnsupportedTracerProviderError,
    install_global_telemetry,
)

mode = os.environ["TRIAL004_PROBE_MODE"]
events = []
registry = ProviderRegistry()
if mode == "unsupported":
    trace.set_tracer_provider(trace.NoOpTracerProvider())
    try:
        install_global_telemetry(events.append, registry=registry)
    except UnsupportedTracerProviderError:
        print(json.dumps({"unsupported": True, "event_count": len(events)}))
    else:
        raise AssertionError("non-SDK provider was silently accepted")
else:
    exporter = InMemorySpanExporter()
    existing = None
    if mode == "existing":
        existing = TracerProvider()
        existing.add_span_processor(SimpleSpanProcessor(exporter))
        trace.set_tracer_provider(existing)
    binding = install_global_telemetry(events.append, registry=registry, project_id="project")
    provider = trace.get_tracer_provider()
    tracer = provider.get_tracer("trial-004-global-probe")
    with tracer.start_as_current_span("request", kind=SpanKind.SERVER):
        pass
    snapshot = binding.snapshot()
    event_count = len(events)
    binding.deactivate(timeout=0.5)
    with tracer.start_as_current_span("user-span-after-deactivate"):
        pass
    replacement_rejected = False
    try:
        registry.bind(TracerProvider(), events.append)
    except UnsupportedTracerProviderError:
        replacement_rejected = True
    print(json.dumps({
        "owns_provider": snapshot.owns_provider,
        "sampler": snapshot.sampler_description,
        "registration_count": snapshot.registration_count,
        "provider_preserved": existing is None or provider is existing,
        "event_count": event_count,
        "post_deactivate_event_count": len(events),
        "user_exported": (
            mode == "owned"
            or any(
                span.name == "user-span-after-deactivate"
                for span in exporter.get_finished_spans()
            )
        ),
        "replacement_rejected": replacement_rejected,
    }))
"""
    result = _python_probe(source, mode=mode)

    if mode == "unsupported":
        assert result == {"unsupported": True, "event_count": 0}
        return
    assert result["owns_provider"] is (mode == "owned")
    assert result["sampler"] == ("AlwaysOnSampler" if mode == "owned" else "ParentBased")
    assert result["registration_count"] == 1
    assert result["provider_preserved"] is True
    assert result["event_count"] == 1
    assert result["post_deactivate_event_count"] == 1
    assert result["user_exported"] is True
    assert result["replacement_rejected"] is True


def test_deactivated_processor_does_no_conversion_for_later_user_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import spikes.sidecar_otel.telemetry as telemetry_module

    provider = TracerProvider(sampler=ALWAYS_ON)
    collector = _TelemetryCollector()
    binding = ProviderRegistry().bind(provider, collector.enqueue, project_id="project")
    assert binding.deactivate(timeout=0.25) is True
    conversion_calls = 0
    real_safe_summary = telemetry_module.safe_summary

    def counted_safe_summary(payload):
        nonlocal conversion_calls
        conversion_calls += 1
        return real_safe_summary(payload)

    monkeypatch.setattr(telemetry_module, "safe_summary", counted_safe_summary)
    with provider.get_tracer("trial-004-noop").start_as_current_span(
        "post-shutdown-user-span", kind=SpanKind.SERVER
    ):
        pass

    assert conversion_calls == 0
    assert collector.snapshot() == ()

    provider.shutdown()


def test_deactivate_waits_for_inflight_callback_before_bounded_flush() -> None:
    enqueue_started = threading.Event()
    release_enqueue = threading.Event()
    flush_started = threading.Event()
    release_flush = threading.Event()
    accepted_events: list[TelemetryEvent] = []
    flush_observations: list[tuple[float, int]] = []

    def blocked_enqueue(event: TelemetryEvent) -> bool:
        enqueue_started.set()
        if not release_enqueue.wait(2.0):
            raise TimeoutError
        accepted_events.append(event)
        return True

    def flush(timeout: float) -> bool:
        flush_observations.append((timeout, len(accepted_events)))
        flush_started.set()
        if not release_flush.wait(2.0):
            raise TimeoutError
        return True

    provider = TracerProvider(sampler=ALWAYS_ON)
    binding = ProviderRegistry().bind(
        provider,
        blocked_enqueue,
        flush=flush,
        project_id="project",
    )
    tracer = provider.get_tracer("trial-004-deactivate-race")

    def end_server_span() -> None:
        with tracer.start_as_current_span("request", kind=SpanKind.SERVER):
            pass

    with ThreadPoolExecutor(max_workers=3) as executor:
        span_future = executor.submit(end_server_span)
        assert enqueue_started.wait(1.0)
        first_deactivate = executor.submit(binding.deactivate, 1.5)
        assert not first_deactivate.done()
        release_enqueue.set()
        span_future.result(timeout=1.0)
        assert flush_started.wait(1.0)
        second_deactivate = executor.submit(binding.deactivate, 1.0)
        assert not second_deactivate.done()
        with pytest.raises(RuntimeError, match="still draining"):
            binding.processor.activate(lambda _event: True)
        release_flush.set()
        assert first_deactivate.result(timeout=1.0) is True
        assert second_deactivate.result(timeout=1.0) is True

    assert len(accepted_events) == 1
    assert len(flush_observations) == 1
    assert flush_observations[0][1] == 1
    assert 0 <= flush_observations[0][0] <= 1.5

    provider.shutdown()


def test_deactivate_retries_a_failed_flush_before_reactivation() -> None:
    flush_calls: list[float] = []

    def transient_flush(timeout: float) -> bool:
        flush_calls.append(timeout)
        return len(flush_calls) > 1

    first_events: list[TelemetryEvent] = []
    replacement_events: list[TelemetryEvent] = []
    processor = FlowSightSpanProcessor(first_events.append, flush=transient_flush)
    assert processor.deactivate(timeout=0.25) is False
    with pytest.raises(RuntimeError, match="flush must succeed"):
        processor.activate(replacement_events.append)
    assert processor.deactivate(timeout=0.25) is True
    processor.activate(replacement_events.append)

    context = SpanContext(
        trace_id=1,
        span_id=2,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )
    processor.on_end(
        ReadableSpan(
            "request-after-flush-retry",
            context=context,
            kind=SpanKind.SERVER,
            start_time=1,
            end_time=2,
        )
    )
    assert first_events == []
    assert len(replacement_events) == 1
    assert len(flush_calls) == 2


def test_terminal_processor_shutdown_forces_noop_after_callback_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import spikes.sidecar_otel.telemetry as telemetry_module

    conversion_started = threading.Event()
    release_conversion = threading.Event()
    collector = _TelemetryCollector()
    processor = FlowSightSpanProcessor(collector.enqueue, project_id="project")
    real_safe_summary = telemetry_module.safe_summary

    def blocked_safe_summary(payload):
        conversion_started.set()
        if not release_conversion.wait(4.0):
            raise TimeoutError
        return real_safe_summary(payload)

    monkeypatch.setattr(telemetry_module, "safe_summary", blocked_safe_summary)
    context = SpanContext(
        trace_id=1,
        span_id=2,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )
    span = ReadableSpan(
        "blocked-server",
        context=context,
        kind=SpanKind.SERVER,
        start_time=1,
        end_time=2,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        callback = executor.submit(processor.on_end, span)
        assert conversion_started.wait(1.0)
        shutdown_started = time.monotonic()
        processor.shutdown()
        shutdown_elapsed = time.monotonic() - shutdown_started
        assert 1.8 <= shutdown_elapsed < 2.5
        assert processor.health_snapshot().active is False
        release_conversion.set()
        callback.result(timeout=1.0)

    assert collector.snapshot() == ()
    with pytest.raises(RuntimeError, match="shut down"):
        processor.activate(collector.enqueue)


def test_completed_requests_retire_associations_without_false_incomplete_evictions() -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    collector = _TelemetryCollector()
    processor = (
        ProviderRegistry()
        .bind(
            provider,
            collector.enqueue,
            project_id="project",
            association_capacity=2,
        )
        .processor
    )
    tracer = provider.get_tracer("trial-004-association-retirement")

    for index in range(5):
        with tracer.start_as_current_span(f"request-{index}", kind=SpanKind.SERVER):
            pass

    health = processor.health_snapshot()
    assert len(collector.snapshot()) == 5
    assert health.association_count == 0
    assert health.association_eviction_count == 0
    assert health.incomplete_request_ids == ()

    provider.shutdown()


def test_active_association_capacity_evicts_only_the_oldest_request() -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    collector = _TelemetryCollector()
    processor = (
        ProviderRegistry()
        .bind(
            provider,
            collector.enqueue,
            project_id="project",
            association_capacity=2,
        )
        .processor
    )
    tracer = provider.get_tracer("trial-004-association-capacity")
    active_spans = [
        tracer.start_span(f"active-request-{index}", kind=SpanKind.SERVER) for index in range(3)
    ]
    request_ids = [
        derive_request_trace_id(
            "project",
            span.get_span_context().trace_id,
            span.get_span_context().span_id,
        )
        for span in active_spans
    ]

    health = processor.health_snapshot()
    assert health.association_count == 2
    assert health.association_eviction_count == 1
    assert health.incomplete_request_ids == (request_ids[0],)
    assert request_ids[1] not in health.incomplete_request_ids
    assert request_ids[2] not in health.incomplete_request_ids

    for span in active_spans:
        span.end()
    final_health = processor.health_snapshot()
    assert final_health.association_count == 0
    assert final_health.association_eviction_count == 1
    assert final_health.incomplete_request_ids == (request_ids[0],)
    assert {event.request_trace_id for event in collector.snapshot()} == set(request_ids)
    assert processor.deactivate(timeout=0.25) is True
    provider.shutdown()


def test_incomplete_request_id_overflow_sets_sticky_completion_uncertainty() -> None:
    processor = FlowSightSpanProcessor(lambda _event: True, association_capacity=1)
    processor.mark_delivery_failure(["request-one"])
    processor.mark_delivery_failure(["request-two"])
    health = processor.health_snapshot()
    assert health.incomplete_request_ids == ("request-two",)
    assert health.incomplete_request_overflow_count == 1
    assert health.completion_uncertain is True
    assert processor.deactivate(timeout=0.25) is True
    assert processor.health_snapshot().completion_uncertain is True


def test_deactivate_marks_active_child_association_incomplete_after_root_end() -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    collector = _TelemetryCollector()
    binding = ProviderRegistry().bind(provider, collector.enqueue, project_id="project")
    tracer = provider.get_tracer("trial-004-active-child-shutdown")
    server = tracer.start_span("server", kind=SpanKind.SERVER)
    server_context = server.get_span_context()
    child = tracer.start_span("still-active-child", context=set_span_in_context(server))
    server.end()
    request_id = derive_request_trace_id("project", server_context.trace_id, server_context.span_id)
    assert len(collector.snapshot()) == 1
    assert binding.processor.health_snapshot().association_count == 1

    assert binding.deactivate(timeout=0.25) is True
    child.end()
    health = binding.processor.health_snapshot()
    assert health.association_count == 0
    assert health.incomplete_request_ids == (request_id,)
    assert len(collector.snapshot()) == 1
    provider.shutdown()


def test_raw_otel_status_description_never_enters_private_span_event() -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    collector = _TelemetryCollector()
    ProviderRegistry().bind(provider, collector.enqueue, project_id="project")
    tracer = provider.get_tracer("trial-004-status-privacy")
    sentinel = "ValueError: raw-server-exception-sentinel"

    with tracer.start_as_current_span("request", kind=SpanKind.SERVER) as span:
        span.set_status(Status(StatusCode.ERROR, sentinel))

    [event] = collector.snapshot()
    assert sentinel not in event.payload_json
    assert "description" not in event.payload_json
    assert '"code":"ERROR"' in event.payload_json

    provider.shutdown()


@pytest.mark.parametrize(
    ("sampler", "expected_events", "expects_warning"),
    [
        (ALWAYS_ON, 1, False),
        (TraceIdRatioBased(1.0), 1, True),
        (ALWAYS_OFF, 0, True),
    ],
)
def test_sampler_visibility_and_warning_contract(
    sampler, expected_events: int, expects_warning: bool
) -> None:
    provider = TracerProvider(sampler=sampler)
    collector = _TelemetryCollector()
    binding = ProviderRegistry().bind(provider, collector.enqueue, project_id="project")
    tracer = provider.get_tracer("trial-004-samplers")

    with tracer.start_as_current_span("request", kind=SpanKind.SERVER) as server_span:
        with binding.processor.request_context(server_span):
            assert server_span.is_recording() is (sampler is not ALWAYS_OFF)

    health = binding.processor.health_snapshot()
    assert len(collector.snapshot()) == expected_events
    assert (binding.warning is not None) is expects_warning
    assert health.non_recording_request_count == (1 if sampler is ALWAYS_OFF else 0)

    provider.shutdown()


def test_fastapi_instrumentation_is_idempotent_and_preserves_context_across_threads() -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    collector = _TelemetryCollector()
    binding = ProviderRegistry().bind(provider, collector.enqueue, project_id="project")
    tracer = provider.get_tracer("trial-004-fastapi")
    app = FastAPI()
    observed_request_context: list[tuple[str, bool]] = []

    @app.get("/sync")
    def sync_route() -> dict[str, bool]:
        with tracer.start_as_current_span("sync-child") as child:
            observed_request_context.append(("sync", get_current_span() is child))
        return {"ok": True}

    @app.get("/async")
    async def async_route() -> dict[str, bool]:
        with tracer.start_as_current_span("async-child") as child:
            observed_request_context.append(("async", get_current_span() is child))

        def in_threadpool() -> None:
            with tracer.start_as_current_span("threadpool-child") as child:
                observed_request_context.append(("threadpool", get_current_span() is child))

        await run_in_threadpool(in_threadpool)
        return {"ok": True}

    first = instrument_fastapi_app(app, tracer_provider=provider, processor=binding.processor)
    second = instrument_fastapi_app(app, tracer_provider=provider, processor=binding.processor)
    _asgi_get(app, "/sync")
    _asgi_get(app, "/async")

    finished = exporter.get_finished_spans()
    server_spans = [span for span in finished if span.kind is SpanKind.SERVER]
    events = collector.snapshot()
    request_by_span = {event.span_id: event.request_trace_id for event in events}

    assert first.newly_registered is True
    assert second.newly_registered is False
    assert len(server_spans) == 2
    assert {name for name, has_current_child in observed_request_context if has_current_child} == {
        "sync",
        "async",
        "threadpool",
    }
    for child_name in ("sync-child", "async-child", "threadpool-child"):
        child = next(span for span in finished if span.name == child_name)
        assert child.parent is not None
        assert (
            request_by_span[f"{child.context.span_id:016x}"]
            == request_by_span[f"{child.parent.span_id:016x}"]
        )

    provider.shutdown()


def test_preinstrumented_fastapi_app_still_exports_exactly_one_server_root() -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    collector = _TelemetryCollector()
    binding = ProviderRegistry().bind(provider, collector.enqueue, project_id="project")
    app = FastAPI()

    @app.get("/probe")
    def probe() -> dict[str, bool]:
        return {"ok": True}

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        exclude_spans=["receive", "send"],
    )
    instrument_fastapi_app(app, tracer_provider=provider, processor=binding.processor)
    _asgi_get(app, "/probe")

    server_spans = [span for span in exporter.get_finished_spans() if span.kind is SpanKind.SERVER]
    assert len(server_spans) == 1
    assert len(collector.snapshot()) == 1

    FastAPIInstrumentor.uninstrument_app(app)
    provider.shutdown()


def test_enabling_app_gating_retires_an_unbound_inflight_server() -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    collector = _TelemetryCollector()
    binding = ProviderRegistry().bind(provider, collector.enqueue, project_id="gating-project")
    tracer = provider.get_tracer("trial-004-gating-transition")
    unbound = tracer.start_span("unbound-started-before-gating", kind=SpanKind.SERVER)
    unbound_context = unbound.get_span_context()
    expected_incomplete = derive_request_trace_id(
        "gating-project",
        unbound_context.trace_id,
        unbound_context.span_id,
    )
    app = FastAPI()

    @app.get("/bound")
    def bound() -> dict[str, bool]:
        return {"bound": True}

    instrumentation = instrument_fastapi_app(
        app,
        tracer_provider=provider,
        processor=binding.processor,
    )
    try:
        unbound.end()
        assert collector.snapshot() == ()
        assert expected_incomplete in binding.processor.health_snapshot().incomplete_request_ids
        _asgi_get(app, "/bound")
        [bound_event] = collector.snapshot()
        assert json.loads(bound_event.payload_json)["data"]["name"] == "GET /bound"
        assert "unbound-started-before-gating" in {
            span.name for span in exporter.get_finished_spans()
        }
    finally:
        binding.processor.deactivate_app_binding(instrumentation.app_binding)
        binding.deactivate(timeout=1.0)
        FastAPIInstrumentor.uninstrument_app(app)
        provider.shutdown()


def test_app_gating_transition_waits_for_materialized_pre_gate_emission() -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    enqueue_entered = threading.Event()
    release_enqueue = threading.Event()
    transition_completed = threading.Event()
    accepted: list[TelemetryEvent] = []

    def blocked_enqueue(event: TelemetryEvent) -> bool:
        name = json.loads(event.payload_json)["data"]["name"]
        if name == "unbound-racing-end":
            enqueue_entered.set()
            if not release_enqueue.wait(3.0):
                raise TimeoutError("pre-gate enqueue was not released")
        accepted.append(event)
        return True

    binding = ProviderRegistry().bind(
        provider,
        blocked_enqueue,
        project_id="gating-race-project",
    )
    tracer = provider.get_tracer("trial-004-gating-race")
    unbound = tracer.start_span("unbound-racing-end", kind=SpanKind.SERVER)
    app = FastAPI()

    @app.get("/bound")
    def bound() -> dict[str, bool]:
        return {"bound": True}

    instrumentation = None

    def transition():
        try:
            return instrument_fastapi_app(
                app,
                tracer_provider=provider,
                processor=binding.processor,
            )
        finally:
            transition_completed.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            ending = executor.submit(unbound.end)
            assert enqueue_entered.wait(1.0)
            transition_future = executor.submit(transition)
            assert not transition_completed.wait(0.1)
            release_enqueue.set()
            ending.result(timeout=2.0)
            instrumentation = transition_future.result(timeout=2.0)

        assert transition_completed.is_set()
        assert [json.loads(event.payload_json)["data"]["name"] for event in accepted] == [
            "unbound-racing-end"
        ]
        with tracer.start_as_current_span("unbound-after-gating", kind=SpanKind.SERVER):
            pass
        assert len(accepted) == 1
        _asgi_get(app, "/bound")
        assert [json.loads(event.payload_json)["data"]["name"] for event in accepted] == [
            "unbound-racing-end",
            "GET /bound",
        ]
    finally:
        release_enqueue.set()
        if instrumentation is not None:
            binding.processor.deactivate_app_binding(instrumentation.app_binding)
        binding.deactivate(timeout=1.0)
        FastAPIInstrumentor.uninstrument_app(app)
        provider.shutdown()


def test_app_gating_transition_timeout_restores_callback_admission() -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    enqueue_entered = threading.Event()
    release_enqueue = threading.Event()
    collector = _TelemetryCollector()

    def blocked_enqueue(event: TelemetryEvent) -> bool:
        enqueue_entered.set()
        if not release_enqueue.wait(3.0):
            raise TimeoutError("enqueue was not released")
        return collector.enqueue(event)

    processor = FlowSightSpanProcessor(blocked_enqueue, project_id="gate-timeout-project")
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("trial-004-gate-timeout")
    first = tracer.start_span("first-unbound", kind=SpanKind.SERVER)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            ending = executor.submit(first.end)
            assert enqueue_entered.wait(1.0)
            with pytest.raises(TimeoutError, match="app-gating transition timed out"):
                processor.enable_app_gating(timeout=0.01)
            release_enqueue.set()
            ending.result(timeout=2.0)

        with tracer.start_as_current_span("second-unbound", kind=SpanKind.SERVER):
            pass
        assert {
            json.loads(event.payload_json)["data"]["name"] for event in collector.snapshot()
        } == {"first-unbound", "second-unbound"}

        processor.enable_app_gating(timeout=1.0)
        with tracer.start_as_current_span("ignored-after-gating", kind=SpanKind.SERVER):
            pass
        assert len(collector.snapshot()) == 2
    finally:
        release_enqueue.set()
        processor.deactivate(timeout=1.0)
        provider.shutdown()


def test_app_gating_transition_baseexception_finishes_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    collector = _TelemetryCollector()
    processor = FlowSightSpanProcessor(
        collector.enqueue,
        project_id="gate-baseexception-project",
    )
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("trial-004-gate-baseexception")
    unbound = tracer.start_span("unbound-before-interrupt", kind=SpanKind.SERVER)
    unbound_context = unbound.get_span_context()
    request_id = derive_request_trace_id(
        "gate-baseexception-project",
        unbound_context.trace_id,
        unbound_context.span_id,
    )
    real_put_tombstone = processor._put_tombstone_locked
    tombstone_calls = 0

    def interrupt_once(key) -> None:
        nonlocal tombstone_calls
        tombstone_calls += 1
        if tombstone_calls == 1:
            raise KeyboardInterrupt("gating mutation interrupted")
        real_put_tombstone(key)

    monkeypatch.setattr(processor, "_put_tombstone_locked", interrupt_once)
    try:
        with pytest.raises(KeyboardInterrupt, match="gating mutation interrupted"):
            processor.enable_app_gating()
        monkeypatch.setattr(processor, "_put_tombstone_locked", real_put_tombstone)
        unbound.end()
        with tracer.start_as_current_span("ignored-after-interrupt", kind=SpanKind.SERVER):
            pass

        health = processor.health_snapshot()
        assert collector.snapshot() == ()
        assert request_id in health.incomplete_request_ids
        assert health.active is True
    finally:
        monkeypatch.setattr(processor, "_put_tombstone_locked", real_put_tombstone)
        processor.deactivate(timeout=1.0)
        provider.shutdown()


def test_gated_nested_server_uses_nearest_local_server_boundary() -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    collector = _TelemetryCollector()
    binding = ProviderRegistry().bind(provider, collector.enqueue, project_id="nested-project")
    tracer = provider.get_tracer("trial-004-gated-nested-server")
    app = FastAPI()

    @app.get("/nested")
    def nested() -> dict[str, bool]:
        with tracer.start_as_current_span("inner-server", kind=SpanKind.SERVER):
            with tracer.start_as_current_span("inner-child"):
                pass
        with tracer.start_as_current_span("outer-child"):
            pass
        return {"nested": True}

    instrumentation = instrument_fastapi_app(
        app,
        tracer_provider=provider,
        processor=binding.processor,
    )
    try:
        _asgi_get(app, "/nested")
        events_by_name = {
            json.loads(event.payload_json)["data"]["name"]: event for event in collector.snapshot()
        }
        assert set(events_by_name) == {
            "GET /nested",
            "inner-server",
            "inner-child",
            "outer-child",
        }
        outer_request_id = events_by_name["GET /nested"].request_trace_id
        inner_request_id = events_by_name["inner-server"].request_trace_id
        assert inner_request_id != outer_request_id
        assert events_by_name["inner-child"].request_trace_id == inner_request_id
        assert events_by_name["outer-child"].request_trace_id == outer_request_id
    finally:
        binding.processor.deactivate_app_binding(instrumentation.app_binding)
        binding.deactivate(timeout=1.0)
        FastAPIInstrumentor.uninstrument_app(app)
        provider.shutdown()


def test_preinstrumented_hook_nested_server_is_promoted_without_stale_state() -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    collector = _TelemetryCollector()
    binding = ProviderRegistry().bind(
        provider,
        collector.enqueue,
        project_id="hook-nested-project",
    )
    tracer = provider.get_tracer("trial-004-hook-nested-server")
    app = FastAPI()

    def nested_server_hook(_span, _scope) -> None:
        with tracer.start_as_current_span("hook-inner-server", kind=SpanKind.SERVER):
            with tracer.start_as_current_span("hook-inner-child"):
                pass

    @app.get("/x")
    def route() -> dict[str, bool]:
        return {"ok": True}

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        server_request_hook=nested_server_hook,
        exclude_spans=["receive", "send"],
    )
    instrumentation = instrument_fastapi_app(
        app,
        tracer_provider=provider,
        processor=binding.processor,
    )
    try:
        _asgi_get(app, "/x")
        events_by_name = {
            json.loads(event.payload_json)["data"]["name"]: event for event in collector.snapshot()
        }
        assert set(events_by_name) == {
            "GET /x",
            "hook-inner-server",
            "hook-inner-child",
        }
        outer_id = events_by_name["GET /x"].request_trace_id
        inner_id = events_by_name["hook-inner-server"].request_trace_id
        assert inner_id != outer_id
        assert events_by_name["hook-inner-child"].request_trace_id == inner_id
        health = binding.processor.health_snapshot()
        assert health.association_count == 0
        assert health.provisional_span_count == 0
        assert health.provisional_orphan_count == 0
        assert health.incomplete_request_ids == ()
    finally:
        binding.processor.deactivate_app_binding(instrumentation.app_binding)
        binding.deactivate(timeout=1.0)
        FastAPIInstrumentor.uninstrument_app(app)
        provider.shutdown()


def test_preinstrumented_hook_live_child_keeps_nested_server_boundary() -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    collector = _TelemetryCollector()
    binding = ProviderRegistry().bind(
        provider,
        collector.enqueue,
        project_id="hook-live-child-project",
    )
    tracer = provider.get_tracer("trial-004-hook-live-child")
    app = FastAPI()
    live_children = []

    def live_child_hook(_span, _scope) -> None:
        inner_server = tracer.start_span("hook-live-inner-server", kind=SpanKind.SERVER)
        live_children.append(
            tracer.start_span(
                "hook-live-child",
                context=set_span_in_context(inner_server),
            )
        )
        inner_server.end()

    @app.get("/x")
    def route() -> dict[str, bool]:
        live_children[0].end()
        return {"ok": True}

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        server_request_hook=live_child_hook,
        exclude_spans=["receive", "send"],
    )
    instrumentation = instrument_fastapi_app(
        app,
        tracer_provider=provider,
        processor=binding.processor,
    )
    try:
        _asgi_get(app, "/x")
        events_by_name = {
            json.loads(event.payload_json)["data"]["name"]: event for event in collector.snapshot()
        }
        assert set(events_by_name) == {
            "GET /x",
            "hook-live-inner-server",
            "hook-live-child",
        }
        inner_id = events_by_name["hook-live-inner-server"].request_trace_id
        assert events_by_name["hook-live-child"].request_trace_id == inner_id
        assert events_by_name["GET /x"].request_trace_id != inner_id
        health = binding.processor.health_snapshot()
        assert health.association_count == 0
        assert health.incomplete_request_ids == ()
    finally:
        binding.processor.deactivate_app_binding(instrumentation.app_binding)
        binding.deactivate(timeout=1.0)
        FastAPIInstrumentor.uninstrument_app(app)
        provider.shutdown()


def test_preinstrumented_nested_loss_marks_inner_not_outer_request_incomplete() -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    collector = _TelemetryCollector()
    binding = ProviderRegistry().bind(
        provider,
        collector.enqueue,
        project_id="hook-nested-loss-project",
        orphan_capacity=2,
    )
    tracer = provider.get_tracer("trial-004-hook-nested-loss")
    app = FastAPI()

    def nested_loss_hook(_span, _scope) -> None:
        with tracer.start_as_current_span("hook-loss-inner-server", kind=SpanKind.SERVER):
            with tracer.start_as_current_span("hook-loss-child-1"):
                pass
            with tracer.start_as_current_span("hook-loss-child-2"):
                pass

    @app.get("/x")
    def route() -> dict[str, bool]:
        return {"ok": True}

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        server_request_hook=nested_loss_hook,
        exclude_spans=["receive", "send"],
    )
    instrumentation = instrument_fastapi_app(
        app,
        tracer_provider=provider,
        processor=binding.processor,
    )
    try:
        _asgi_get(app, "/x")
        events_by_name = {
            json.loads(event.payload_json)["data"]["name"]: event for event in collector.snapshot()
        }
        assert set(events_by_name) == {
            "GET /x",
            "hook-loss-inner-server",
            "hook-loss-child-2",
        }
        outer_id = events_by_name["GET /x"].request_trace_id
        inner_id = events_by_name["hook-loss-inner-server"].request_trace_id
        health = binding.processor.health_snapshot()
        assert inner_id in health.incomplete_request_ids
        assert outer_id not in health.incomplete_request_ids
        assert health.provisional_orphan_overflow_count == 1
    finally:
        binding.processor.deactivate_app_binding(instrumentation.app_binding)
        binding.deactivate(timeout=1.0)
        FastAPIInstrumentor.uninstrument_app(app)
        provider.shutdown()


def test_exception_handler_nested_server_inherits_exact_admitted_root() -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    collector = _TelemetryCollector()
    binding = ProviderRegistry().bind(
        provider,
        collector.enqueue,
        project_id="exception-handler-project",
    )
    tracer = provider.get_tracer("trial-004-exception-handler")
    app = FastAPI()

    @app.exception_handler(Exception)
    async def exception_handler(_request, error: Exception):
        with tracer.start_as_current_span("error-handler-server", kind=SpanKind.SERVER):
            with tracer.start_as_current_span("error-handler-child"):
                pass
        raise error

    @app.get("/error")
    def error_route() -> None:
        raise RuntimeError("route failure")

    instrumentation = instrument_fastapi_app(
        app,
        tracer_provider=provider,
        processor=binding.processor,
    )
    try:
        with pytest.raises(RuntimeError, match="route failure"):
            _asgi_get(app, "/error")
        events_by_name = {
            json.loads(event.payload_json)["data"]["name"]: event for event in collector.snapshot()
        }
        assert set(events_by_name) == {
            "GET /error",
            "error-handler-server",
            "error-handler-child",
        }
        handler_id = events_by_name["error-handler-server"].request_trace_id
        assert events_by_name["error-handler-child"].request_trace_id == handler_id
        assert events_by_name["GET /error"].request_trace_id != handler_id
        assert binding.processor.health_snapshot().incomplete_request_ids == ()
    finally:
        binding.processor.deactivate_app_binding(instrumentation.app_binding)
        binding.deactivate(timeout=1.0)
        FastAPIInstrumentor.uninstrument_app(app)
        provider.shutdown()


@pytest.mark.parametrize("loss_mode", ["overflow", "ttl"])
def test_provisional_child_loss_marks_promoted_request_incomplete(loss_mode: str) -> None:
    now = [0.0]
    provider = TracerProvider(sampler=ALWAYS_ON)
    collector = _TelemetryCollector()
    binding = ProviderRegistry().bind(
        provider,
        collector.enqueue,
        project_id=f"provisional-{loss_mode}-project",
        clock=lambda: now[0],
        orphan_capacity=1,
        orphan_ttl=1.0,
    )
    tracer = provider.get_tracer(f"trial-004-provisional-{loss_mode}")
    app = FastAPI()

    def loss_hook(_span, _scope) -> None:
        with tracer.start_as_current_span("lost-provisional-child"):
            pass
        if loss_mode == "overflow":
            with tracer.start_as_current_span("retained-provisional-child"):
                pass
        else:
            now[0] = 2.0
            binding.processor.sweep_expired()

    @app.get("/probe")
    def probe() -> dict[str, bool]:
        return {"ok": True}

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        server_request_hook=loss_hook,
        exclude_spans=["receive", "send"],
    )
    instrumentation = instrument_fastapi_app(
        app,
        tracer_provider=provider,
        processor=binding.processor,
    )
    try:
        _asgi_get(app, "/probe")
        root_event = next(
            event
            for event in collector.snapshot()
            if json.loads(event.payload_json)["data"]["name"] == "GET /probe"
        )
        health = binding.processor.health_snapshot()
        assert root_event.request_trace_id in health.incomplete_request_ids
        assert health.completion_uncertain is False
        if loss_mode == "overflow":
            assert health.provisional_orphan_overflow_count == 1
        else:
            assert health.provisional_orphan_count == 0
    finally:
        binding.processor.deactivate_app_binding(instrumentation.app_binding)
        binding.deactivate(timeout=1.0)
        FastAPIInstrumentor.uninstrument_app(app)
        provider.shutdown()


def test_provisional_loss_is_exact_for_two_local_roots_sharing_upstream_trace() -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    collector = _TelemetryCollector()
    processor = FlowSightSpanProcessor(
        collector.enqueue,
        project_id="shared-provisional-project",
        association_capacity=4,
        orphan_capacity=1,
    )
    provider.add_span_processor(processor)
    processor.enable_app_gating()
    app_binding = FastAPIAppBinding()
    processor.activate_app_binding(app_binding)
    upstream = SpanContext(
        trace_id=0x123456,
        span_id=0x999,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )
    upstream_context = set_span_in_context(NonRecordingSpan(upstream))
    tracer = provider.get_tracer("trial-004-shared-provisional")
    root_a = tracer.start_span("request-a", context=upstream_context, kind=SpanKind.SERVER)
    root_b = tracer.start_span("request-b", context=upstream_context, kind=SpanKind.SERVER)
    root_b_context = set_span_in_context(root_b)
    tracer.start_span("b-child-1", context=root_b_context).end()
    tracer.start_span("b-child-2", context=root_b_context).end()
    a_context = root_a.get_span_context()
    b_context = root_b.get_span_context()
    a_id = derive_request_trace_id(
        "shared-provisional-project", a_context.trace_id, a_context.span_id
    )
    b_id = derive_request_trace_id(
        "shared-provisional-project", b_context.trace_id, b_context.span_id
    )

    try:
        with processor.app_context(app_binding) as capture:
            with processor.request_context(root_a, app_capture=capture):
                root_a.end()
        with processor.app_context(app_binding) as capture:
            with processor.request_context(root_b, app_capture=capture):
                root_b.end()

        health = processor.health_snapshot()
        assert a_id not in health.incomplete_request_ids
        assert b_id in health.incomplete_request_ids
        assert health.provisional_orphan_overflow_count == 1
        events_by_name = {
            json.loads(event.payload_json)["data"]["name"]: event for event in collector.snapshot()
        }
        assert events_by_name["request-a"].request_trace_id == a_id
        assert events_by_name["request-b"].request_trace_id == b_id
        assert events_by_name["b-child-2"].request_trace_id == b_id
        assert "b-child-1" not in events_by_name
    finally:
        processor.deactivate_app_binding(app_binding)
        processor.deactivate(timeout=1.0)
        provider.shutdown()


def test_inactive_preinstrumented_app_pressure_cannot_evict_active_request() -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    collector = _TelemetryCollector()
    binding = ProviderRegistry().bind(
        provider,
        collector.enqueue,
        project_id="app-isolation-project",
        association_capacity=1,
        orphan_capacity=1,
    )
    tracer = provider.get_tracer("trial-004-preinstrumented-isolation")
    inactive_app = FastAPI()
    active_app = FastAPI()
    active_entered = threading.Event()
    release_active = threading.Event()
    active_contexts: list[SpanContext] = []

    def preinstrumented_hook(_span, _scope) -> None:
        with tracer.start_as_current_span("preinstrumented-hook-child"):
            pass

    @inactive_app.get("/inactive")
    def inactive_request() -> dict[str, bool]:
        return {"inactive": True}

    @inactive_app.get("/inactive-error")
    def inactive_error() -> None:
        raise RuntimeError("inactive app error")

    @active_app.get("/held")
    def active_held_request() -> dict[str, bool]:
        active_contexts.append(get_current_span().get_span_context())
        active_entered.set()
        if not release_active.wait(3.0):
            raise TimeoutError("active request was not released")
        return {"active": True}

    FastAPIInstrumentor.instrument_app(
        inactive_app,
        tracer_provider=provider,
        server_request_hook=preinstrumented_hook,
        exclude_spans=["receive", "send"],
    )
    inactive_instrumentation = instrument_fastapi_app(
        inactive_app,
        tracer_provider=provider,
        processor=binding.processor,
    )
    binding.processor.deactivate_app_binding(inactive_instrumentation.app_binding)
    active_instrumentation = instrument_fastapi_app(
        active_app,
        tracer_provider=provider,
        processor=binding.processor,
    )

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            active_future = executor.submit(_asgi_get, active_app, "/held")
            assert active_entered.wait(1.0)
            [active_context] = active_contexts
            active_request_id = derive_request_trace_id(
                "app-isolation-project",
                active_context.trace_id,
                active_context.span_id,
            )

            for index in range(8):
                if index % 2:
                    with pytest.raises(RuntimeError, match="inactive app error"):
                        _asgi_get(inactive_app, "/inactive-error")
                else:
                    _asgi_get(inactive_app, "/inactive")

            pressure_health = binding.processor.health_snapshot()
            assert pressure_health.association_count == 1
            assert pressure_health.orphan_count == 0
            assert pressure_health.ignored_span_count <= 1
            assert pressure_health.ignored_span_overflow_count > 0
            assert pressure_health.provisional_root_drop_count > 0
            assert active_request_id not in pressure_health.incomplete_request_ids
            assert pressure_health.completion_uncertain is False
            assert collector.snapshot() == ()

            release_active.set()
            active_future.result(timeout=2.0)

        [active_event] = collector.snapshot()
        assert active_event.request_trace_id == active_request_id
        assert json.loads(active_event.payload_json)["data"]["name"] == "GET /held"
        exported_names = {span.name for span in exporter.get_finished_spans()}
        assert {
            "GET /inactive",
            "GET /inactive-error",
            "preinstrumented-hook-child",
            "GET /held",
        } <= exported_names
    finally:
        release_active.set()
        binding.processor.deactivate_app_binding(active_instrumentation.app_binding)
        binding.deactivate(timeout=1.0)
        FastAPIInstrumentor.uninstrument_app(inactive_app)
        FastAPIInstrumentor.uninstrument_app(active_app)
        provider.shutdown()


def test_root_started_before_same_app_reactivation_cannot_gain_new_session_token() -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    collector = _TelemetryCollector()
    binding = ProviderRegistry().bind(
        provider,
        collector.enqueue,
        project_id="root-promotion-race-project",
    )
    app = FastAPI()
    hook_entered = threading.Event()
    release_hook = threading.Event()
    hook_calls = 0

    def blocking_server_hook(_span, _scope) -> None:
        nonlocal hook_calls
        hook_calls += 1
        if hook_calls == 1:
            hook_entered.set()
            if not release_hook.wait(3.0):
                raise TimeoutError("server hook was not released")

    @app.get("/probe")
    def probe() -> dict[str, bool]:
        return {"ok": True}

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        server_request_hook=blocking_server_hook,
        exclude_spans=["receive", "send"],
    )
    instrumentation = instrument_fastapi_app(
        app,
        tracer_provider=provider,
        processor=binding.processor,
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            old_request = executor.submit(_asgi_get, app, "/probe")
            assert hook_entered.wait(1.0)
            assert binding.processor.health_snapshot().provisional_root_count == 1

            binding.processor.deactivate_app_binding(instrumentation.app_binding)
            assert binding.processor.health_snapshot().provisional_root_drop_count >= 1
            binding.processor.activate_app_binding(instrumentation.app_binding)
            release_hook.set()
            old_request.result(timeout=2.0)

        assert collector.snapshot() == ()
        _asgi_get(app, "/probe")
        [current_event] = collector.snapshot()
        assert json.loads(current_event.payload_json)["data"]["name"] == "GET /probe"
        server_spans = [
            span for span in exporter.get_finished_spans() if span.kind is SpanKind.SERVER
        ]
        assert len(server_spans) == 2
    finally:
        release_hook.set()
        binding.processor.deactivate_app_binding(instrumentation.app_binding)
        binding.deactivate(timeout=1.0)
        FastAPIInstrumentor.uninstrument_app(app)
        provider.shutdown()


def test_provisional_root_pressure_defaults_to_session_uncertainty() -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    collector = _TelemetryCollector()
    binding = ProviderRegistry().bind(
        provider,
        collector.enqueue,
        project_id="provisional-root-pressure-project",
        association_capacity=1,
    )
    tracer = provider.get_tracer("trial-004-provisional-root-pressure")
    app = FastAPI()
    hook_entered = threading.Event()
    release_hook = threading.Event()

    def blocking_hook(_span, _scope) -> None:
        hook_entered.set()
        if not release_hook.wait(3.0):
            raise TimeoutError("pressure hook was not released")

    @app.get("/probe")
    def probe() -> dict[str, bool]:
        return {"ok": True}

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        server_request_hook=blocking_hook,
        exclude_spans=["receive", "send"],
    )
    instrumentation = instrument_fastapi_app(
        app,
        tracer_provider=provider,
        processor=binding.processor,
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            admitted_request = executor.submit(_asgi_get, app, "/probe")
            assert hook_entered.wait(1.0)
            with tracer.start_as_current_span(
                "unbound-pressure-root",
                kind=SpanKind.SERVER,
            ):
                pass
            release_hook.set()
            admitted_request.result(timeout=2.0)

        health = binding.processor.health_snapshot()
        assert collector.snapshot() == ()
        assert health.provisional_root_overflow_count == 1
        assert health.provisional_root_drop_count >= 2
        assert health.admission_uncertain is True
        assert health.completion_uncertain is True
    finally:
        release_hook.set()
        binding.processor.deactivate_app_binding(instrumentation.app_binding)
        binding.deactivate(timeout=1.0)
        FastAPIInstrumentor.uninstrument_app(app)
        provider.shutdown()


def test_raw_thread_child_ending_after_reactivation_stays_in_old_request() -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    registry = ProviderRegistry()
    first_collector = _TelemetryCollector()
    replacement_collector = _TelemetryCollector()
    binding = registry.bind(
        provider,
        first_collector.enqueue,
        project_id="raw-thread-generation-project",
    )
    tracer = provider.get_tracer("trial-004-raw-thread-generation")
    app = FastAPI()
    child_spans = []
    request_contexts: list[SpanContext] = []

    @app.get("/start-child")
    def start_child() -> dict[str, bool]:
        request_contexts.append(get_current_span().get_span_context())
        child_spans.append(tracer.start_span("raw-thread-child"))
        return {"started": True}

    @app.get("/new")
    def new_request() -> dict[str, bool]:
        return {"new": True}

    instrumentation = instrument_fastapi_app(
        app,
        tracer_provider=provider,
        processor=binding.processor,
    )
    _asgi_get(app, "/start-child")
    [request_context] = request_contexts
    request_id = derive_request_trace_id(
        "raw-thread-generation-project",
        request_context.trace_id,
        request_context.span_id,
    )
    [first_root] = first_collector.snapshot()
    assert first_root.request_trace_id == request_id

    binding.processor.deactivate_app_binding(instrumentation.app_binding)
    assert binding.deactivate(timeout=1.0) is True
    replacement = registry.bind(
        provider,
        replacement_collector.enqueue,
        project_id="raw-thread-generation-project",
    )
    replacement.processor.activate_app_binding(instrumentation.app_binding)
    try:
        child_ended = threading.Event()

        def end_child_without_context() -> None:
            child_spans[0].end()
            child_ended.set()

        thread = threading.Thread(target=end_child_without_context)
        thread.start()
        assert child_ended.wait(1.0)
        thread.join(timeout=1.0)
        assert not thread.is_alive()
        assert replacement_collector.snapshot() == ()
        assert request_id in replacement.processor.health_snapshot().incomplete_request_ids

        _asgi_get(app, "/new")
        [replacement_root] = replacement_collector.snapshot()
        assert json.loads(replacement_root.payload_json)["data"]["name"] == "GET /new"
        exported_names = {span.name for span in exporter.get_finished_spans()}
        assert {"GET /start-child", "raw-thread-child", "GET /new"} <= exported_names
    finally:
        replacement.processor.deactivate_app_binding(instrumentation.app_binding)
        replacement.deactivate(timeout=1.0)
        FastAPIInstrumentor.uninstrument_app(app)
        provider.shutdown()


def test_function_child_exports_minimal_error_and_keeps_values_private() -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    collector = _TelemetryCollector()
    processor = ProviderRegistry().bind(provider, collector.enqueue, project_id="project").processor
    tracer = provider.get_tracer("trial-004-private-function")
    raw_password = "dont-export-this-password"
    raw_exception = "dont-export-this-exception"
    raw_return_token = "dont-export-this-return-token"

    with tracer.start_as_current_span("request", kind=SpanKind.SERVER):
        with pytest.raises(ValueError, match=raw_exception):
            with private_function_span(
                tracer,
                processor,
                "package.module.function",
                args={"password": raw_password, "safe": 7},
            ):
                raise ValueError(raw_exception)
        with private_function_span(
            tracer,
            processor,
            "package.module.success",
            args={"safe": 8},
        ) as success_scope:
            success_scope.set_return({"api_key": raw_return_token, "result": 9})

    function_span = next(
        span for span in exporter.get_finished_spans() if span.name == "package.module.function"
    )
    success_span = next(
        span for span in exporter.get_finished_spans() if span.name == "package.module.success"
    )
    enrichments = [event for event in collector.snapshot() if event.event_type == "span.enrichment"]
    enrichment_by_span = {event.span_id: event for event in enrichments}

    assert function_span.status.status_code is StatusCode.ERROR
    assert function_span.status.description is None
    assert function_span.events == ()
    assert dict(function_span.attributes or {}) == {}
    assert success_span.events == ()
    assert dict(success_span.attributes or {}) == {}
    assert len(enrichments) == 2
    error_enrichment = enrichment_by_span[f"{function_span.context.span_id:016x}"]
    return_enrichment = enrichment_by_span[f"{success_span.context.span_id:016x}"]
    assert raw_password not in error_enrichment.payload_json
    assert raw_exception not in error_enrichment.payload_json
    assert raw_return_token not in return_enrichment.payload_json
    assert "REDACTED" in error_enrichment.payload_json
    assert "REDACTED" in return_enrichment.payload_json

    error_payload = json.loads(error_enrichment.payload_json)["data"]
    exception_summary = json.loads(error_payload["exception_summary"])
    assert exception_summary["data"]["exception"]["$type"] == "builtins.ValueError"
    return_payload = json.loads(return_enrichment.payload_json)["data"]
    return_summary = json.loads(return_payload["return_summary"])
    assert return_summary["data"]["return"] == {
        "api_key": "<REDACTED>",
        "result": 9,
    }

    provider.shutdown()


def test_shared_upstream_trace_keeps_local_requests_and_drop_state_separate() -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    collector = _TelemetryCollector()
    binding = ProviderRegistry().bind(
        provider, collector.enqueue, project_id="project", association_capacity=16
    )
    tracer = provider.get_tracer("trial-004-shared-upstream")
    upstream = SpanContext(
        trace_id=0x1234567890ABCDEF1234567890ABCDEF,
        span_id=0x1234567890ABCDEF,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )
    upstream_context = set_span_in_context(NonRecordingSpan(upstream))
    requests_started = threading.Barrier(2)

    def run_request(index: int, reject: bool) -> tuple[SpanContext, str]:
        with tracer.start_as_current_span(
            f"local-request-{index}", context=upstream_context, kind=SpanKind.SERVER
        ) as server:
            server_context = server.get_span_context()
            request_id = derive_request_trace_id(
                "project", server_context.trace_id, server_context.span_id
            )
            if reject:
                collector.reject_request_trace_id = request_id
            requests_started.wait(timeout=1.0)
            with tracer.start_as_current_span(f"child-{index}"):
                pass
        return server_context, request_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(run_request, 1, True)
        second_future = executor.submit(run_request, 2, False)
        first_context, first_request_id = first_future.result(timeout=2.0)
        second_context, second_request_id = second_future.result(timeout=2.0)

    events = collector.snapshot()
    health = binding.processor.health_snapshot()
    assert first_context.trace_id == second_context.trace_id == upstream.trace_id
    assert first_context.span_id != second_context.span_id
    assert first_request_id != second_request_id
    assert {event.request_trace_id for event in events} == {second_request_id}
    assert health.queue_drop_count == 2
    assert health.incomplete_request_ids == (first_request_id,)

    provider.shutdown()


def test_missing_explicit_parent_never_falls_back_to_another_local_request_context() -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    collector = _TelemetryCollector()
    processor = (
        ProviderRegistry()
        .bind(
            provider,
            collector.enqueue,
            project_id="exact-parent-project",
            association_capacity=1,
            orphan_capacity=4,
        )
        .processor
    )
    tracer = provider.get_tracer("trial-004-exact-parent")
    upstream = SpanContext(
        trace_id=0xABCDEFABCDEFABCDEFABCDEFABCDEFAB,
        span_id=0x1111111111111111,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )
    upstream_context = set_span_in_context(NonRecordingSpan(upstream))
    server_a = tracer.start_span("server-a", context=upstream_context, kind=SpanKind.SERVER)
    server_b = tracer.start_span("server-b", context=upstream_context, kind=SpanKind.SERVER)
    context_a = server_a.get_span_context()
    context_b = server_b.get_span_context()
    request_a = derive_request_trace_id(
        "exact-parent-project", context_a.trace_id, context_a.span_id
    )
    request_b = derive_request_trace_id(
        "exact-parent-project", context_b.trace_id, context_b.span_id
    )

    with processor.request_context(server_b):
        explicit_a_child = tracer.start_span(
            "explicit-a-child",
            context=set_span_in_context(server_a),
        )
        explicit_a_child.end()
    assert collector.snapshot() == ()
    assert processor.health_snapshot().orphan_count == 1

    server_a.end()
    server_b.end()
    request_by_name: dict[str, str] = {}
    for event in collector.snapshot():
        for name in ("server-a", "server-b", "explicit-a-child"):
            if name in event.payload_json:
                request_by_name[name] = event.request_trace_id
    assert request_by_name == {
        "server-a": request_a,
        "server-b": request_b,
        "explicit-a-child": request_a,
    }
    assert processor.health_snapshot().incomplete_request_ids == (request_a,)
    provider.shutdown()


def test_nested_server_span_uses_nearest_boundary_without_merging_outer_request() -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    collector = _TelemetryCollector()
    processor = ProviderRegistry().bind(provider, collector.enqueue, project_id="project").processor
    tracer = provider.get_tracer("trial-004-nested-server")

    with tracer.start_as_current_span("outer-server", kind=SpanKind.SERVER) as outer:
        outer_context = outer.get_span_context()
        outer_request_id = derive_request_trace_id(
            "project", outer_context.trace_id, outer_context.span_id
        )
        with tracer.start_as_current_span("inner-server", kind=SpanKind.SERVER) as inner:
            inner_context = inner.get_span_context()
            inner_request_id = derive_request_trace_id(
                "project", inner_context.trace_id, inner_context.span_id
            )
            with tracer.start_as_current_span("inner-child"):
                pass
        with tracer.start_as_current_span("outer-child"):
            pass

    request_by_name: dict[str, str] = {}
    for event in collector.snapshot():
        payload = event.payload_json
        for name in ("outer-server", "inner-server", "inner-child", "outer-child"):
            if name in payload:
                request_by_name[name] = event.request_trace_id

    assert inner_request_id != outer_request_id
    assert request_by_name == {
        "outer-server": outer_request_id,
        "inner-server": inner_request_id,
        "inner-child": inner_request_id,
        "outer-child": outer_request_id,
    }
    assert processor.health_snapshot().incomplete_request_ids == ()

    provider.shutdown()


def test_orphans_expire_bounded_as_unscoped_without_creating_or_damaging_trace() -> None:
    now = [10.0]
    provider = TracerProvider(sampler=ALWAYS_ON)
    collector = _TelemetryCollector()
    processor = (
        ProviderRegistry()
        .bind(
            provider,
            collector.enqueue,
            project_id="project",
            clock=lambda: now[0],
            association_capacity=8,
            orphan_capacity=2,
            orphan_ttl=1.0,
        )
        .processor
    )
    tracer = provider.get_tracer("trial-004-orphans")

    for name in ("startup-root", "manual-root", "background-root"):
        with tracer.start_as_current_span(name):
            pass

    capacity_health = processor.health_snapshot()
    assert capacity_health.orphan_count == 2
    assert capacity_health.orphan_capacity_drop_count == 1
    assert capacity_health.unscoped_span_count == 1
    assert collector.snapshot() == ()

    now[0] = 11.1
    processor.sweep_expired()
    expired_health = processor.health_snapshot()
    assert expired_health.orphan_count == 0
    assert expired_health.unscoped_span_count == 3
    assert expired_health.incomplete_request_ids == ()
    assert collector.snapshot() == ()

    with tracer.start_as_current_span("valid-server", kind=SpanKind.SERVER):
        pass
    assert len(collector.snapshot()) == 1
    assert processor.health_snapshot().incomplete_request_ids == ()

    provider.shutdown()


def test_child_ending_before_local_server_resolves_by_exact_parent_chain() -> None:
    collector = _TelemetryCollector()
    processor = FlowSightSpanProcessor(collector.enqueue, project_id="project")
    trace_id = 0xABCDEF1234567890ABCDEF1234567890
    server_context = SpanContext(
        trace_id=trace_id,
        span_id=0x1111111111111111,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )
    child_context = SpanContext(
        trace_id=trace_id,
        span_id=0x2222222222222222,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )
    upstream_context = SpanContext(
        trace_id=trace_id,
        span_id=0x3333333333333333,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )
    processor.on_end(
        ReadableSpan(
            "early-child",
            context=child_context,
            parent=server_context,
            kind=SpanKind.INTERNAL,
            start_time=1,
            end_time=2,
        )
    )
    assert processor.health_snapshot().orphan_count == 1
    assert collector.snapshot() == ()

    processor.on_end(
        ReadableSpan(
            "late-server",
            context=server_context,
            parent=upstream_context,
            kind=SpanKind.SERVER,
            start_time=0,
            end_time=3,
        )
    )
    events = collector.snapshot()
    expected_request_id = derive_request_trace_id("project", trace_id, server_context.span_id)
    assert len(events) == 2
    assert {event.request_trace_id for event in events} == {expected_request_id}
    assert {event.span_id for event in events} == {
        f"{server_context.span_id:016x}",
        f"{child_context.span_id:016x}",
    }
    health = processor.health_snapshot()
    assert health.orphan_count == 0
    assert health.association_count == 0
    assert health.unscoped_span_count == 0
    assert health.incomplete_request_ids == ()


def test_orphan_capacity_loss_marks_later_exact_parent_request_incomplete() -> None:
    collector = _TelemetryCollector()
    processor = FlowSightSpanProcessor(
        collector.enqueue,
        project_id="project",
        association_capacity=4,
        orphan_capacity=1,
    )
    trace_id = 0x1234

    def context(span_id: int) -> SpanContext:
        return SpanContext(
            trace_id=trace_id,
            span_id=span_id,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
            trace_state=TraceState(),
        )

    first_server = context(1)
    second_server = context(2)
    processor.on_end(
        ReadableSpan(
            "first-child",
            context=context(3),
            parent=first_server,
            start_time=1,
            end_time=2,
        )
    )
    processor.on_end(
        ReadableSpan(
            "pressure-child",
            context=context(4),
            parent=second_server,
            start_time=1,
            end_time=2,
        )
    )
    processor.on_end(
        ReadableSpan(
            "first-server",
            context=first_server,
            kind=SpanKind.SERVER,
            start_time=0,
            end_time=3,
        )
    )

    first_request_id = derive_request_trace_id("project", trace_id, first_server.span_id)
    health = processor.health_snapshot()
    assert health.orphan_capacity_drop_count == 1
    assert health.incomplete_request_ids == (first_request_id,)
    assert {event.request_trace_id for event in collector.snapshot()} == {first_request_id}


def test_unattributed_orphan_pressure_does_not_damage_an_unrelated_request() -> None:
    collector = _TelemetryCollector()
    processor = FlowSightSpanProcessor(
        collector.enqueue,
        project_id="project",
        association_capacity=1,
        orphan_capacity=1,
    )

    def context(trace_id: int, span_id: int) -> SpanContext:
        return SpanContext(
            trace_id=trace_id,
            span_id=span_id,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
            trace_state=TraceState(),
        )

    for trace_id in range(1, 4):
        processor.on_end(
            ReadableSpan(
                f"unscoped-{trace_id}",
                context=context(trace_id, 10),
                parent=context(trace_id, 9),
                start_time=1,
                end_time=2,
            )
        )

    pressure_health = processor.health_snapshot()
    assert pressure_health.orphan_capacity_drop_count == 2
    assert pressure_health.orphan_loss_global is True
    assert pressure_health.unscoped_span_count == 1

    server_context = context(99, 1)
    processor.on_end(
        ReadableSpan(
            "valid-server",
            context=server_context,
            kind=SpanKind.SERVER,
            start_time=0,
            end_time=3,
        )
    )
    request_id = derive_request_trace_id("project", 99, 1)
    final_health = processor.health_snapshot()
    assert final_health.incomplete_request_ids == ()
    assert {event.request_trace_id for event in collector.snapshot()} == {request_id}

    affected_server = context(1, 9)
    processor.on_end(
        ReadableSpan(
            "affected-server",
            context=affected_server,
            kind=SpanKind.SERVER,
            start_time=0,
            end_time=4,
        )
    )
    affected_request_id = derive_request_trace_id("project", 1, 9)
    overflow_health = processor.health_snapshot()
    assert overflow_health.incomplete_request_ids == ()
    assert overflow_health.completion_uncertain is True
    assert {event.request_trace_id for event in collector.snapshot()} == {
        request_id,
        affected_request_id,
    }
    assert processor.deactivate(timeout=0.25) is True
    assert processor.health_snapshot().completion_uncertain is True


def test_deep_orphan_parent_chain_resolves_iteratively_within_bounded_capacity() -> None:
    collector = _TelemetryCollector()
    chain_length = 1000
    processor = FlowSightSpanProcessor(
        collector.enqueue,
        project_id="project",
        association_capacity=chain_length + 1,
        orphan_capacity=chain_length,
        clock=lambda: 0.0,
        event_snapshot_capacity=chain_length + 1,
    )
    trace_id = 0x5678

    def context(span_id: int) -> SpanContext:
        return SpanContext(
            trace_id=trace_id,
            span_id=span_id,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
            trace_state=TraceState(),
        )

    for span_id in range(chain_length, 1, -1):
        processor.on_end(
            ReadableSpan(
                f"child-{span_id}",
                context=context(span_id),
                parent=context(span_id - 1),
                start_time=span_id,
                end_time=span_id + 1,
            )
        )
    processor.on_end(
        ReadableSpan(
            "server-root",
            context=context(1),
            kind=SpanKind.SERVER,
            start_time=0,
            end_time=chain_length + 2,
        )
    )

    health = processor.health_snapshot()
    assert len(collector.snapshot()) == chain_length
    assert health.orphan_count == 0
    assert health.association_count == 0
    assert health.callback_error_count == 0
    assert health.incomplete_request_ids == ()


def test_deep_provisional_chain_resolves_iteratively_with_nested_server_boundary() -> None:
    collector = _TelemetryCollector()
    chain_length = 1050
    nested_server_index = 525
    provider = TracerProvider(sampler=ALWAYS_ON)
    processor = FlowSightSpanProcessor(
        collector.enqueue,
        project_id="deep-provisional-project",
        association_capacity=chain_length + 1,
        orphan_capacity=chain_length + 1,
        clock=lambda: 0.0,
        event_snapshot_capacity=chain_length + 1,
    )
    provider.add_span_processor(processor)
    processor.enable_app_gating()
    app_binding = FastAPIAppBinding()
    processor.activate_app_binding(app_binding)
    tracer = provider.get_tracer("trial-004-deep-provisional")
    root = tracer.start_span("root", kind=SpanKind.SERVER)
    parent = root
    descendants = []
    for index in range(1, chain_length + 1):
        descendant = tracer.start_span(
            f"descendant-{index}",
            context=set_span_in_context(parent),
            kind=(SpanKind.SERVER if index == nested_server_index else SpanKind.INTERNAL),
        )
        descendants.append(descendant)
        parent = descendant

    for descendant in reversed(descendants):
        descendant.end()

    root_context = root.get_span_context()
    nested_context = descendants[nested_server_index - 1].get_span_context()
    outer_request_id = derive_request_trace_id(
        "deep-provisional-project",
        root_context.trace_id,
        root_context.span_id,
    )
    nested_request_id = derive_request_trace_id(
        "deep-provisional-project",
        nested_context.trace_id,
        nested_context.span_id,
    )
    try:
        with processor.app_context(app_binding) as capture:
            with processor.request_context(root, app_capture=capture):
                root.end()

        events_by_name = {
            json.loads(event.payload_json)["data"]["name"]: event for event in collector.snapshot()
        }
        assert len(events_by_name) == chain_length + 1
        assert events_by_name["descendant-1"].request_trace_id == outer_request_id
        assert (
            events_by_name[f"descendant-{nested_server_index}"].request_trace_id
            == nested_request_id
        )
        assert events_by_name[f"descendant-{chain_length}"].request_trace_id == nested_request_id
        assert events_by_name["root"].request_trace_id == outer_request_id
        health = processor.health_snapshot()
        assert health.callback_error_count == 0
        assert health.provisional_orphan_count == 0
        assert health.provisional_span_count == 0
        assert health.incomplete_request_ids == ()
    finally:
        processor.deactivate_app_binding(app_binding)
        processor.deactivate(timeout=1.0)
        provider.shutdown()
