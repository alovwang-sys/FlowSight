"""Bounded SDK sender used only by the TRIAL-004 lifecycle spike."""

from __future__ import annotations

import http.client
import json
import math
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from opentelemetry.instrumentation.utils import (
    suppress_http_instrumentation,
    suppress_instrumentation,
)

from .state import PROTOCOL_VERSION, SidecarState
from .telemetry import TelemetryEvent

_MAX_EVENT_BYTES: Final = 16 * 1024
_MAX_BATCH_BYTES: Final = 1024 * 1024
_MAX_EVENTS_PER_BATCH: Final = 128
_BATCH_FIXED_BUDGET: Final = 16 * 1024
_EVENT_WIRE_OVERHEAD_BUDGET: Final = 2 * 1024
_MAX_RESPONSE_BYTES: Final = 256 * 1024
_IMPACTED_TRACE_LIMIT: Final = 64

type Transport = Callable[[dict[str, object], float], dict[str, object]]
type FailureObserver = Callable[["DeliveryFailure"], None]

_LOWER_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_LOWER_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")
_EVENT_TYPES = {
    "route_catalog.replaced",
    "span.ended",
    "span.enrichment",
    "snapshot.captured",
    "trace.drop_notice",
}


class SenderError(RuntimeError):
    """Base class for safe private-delivery failures."""


class SenderTimeoutError(SenderError):
    """A bounded flush or close operation exceeded its timeout."""


class DeliveryError(SenderError):
    """One or more accepted events could not obtain a commit ACK."""


class TransportError(SenderError):
    """The authenticated sidecar transport rejected a batch."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"private delivery failed ({code})")


class SenderState(StrEnum):
    """Lifecycle state for one private sender thread."""

    RUNNING = "running"
    CLOSING = "closing"
    CLOSED = "closed"


class EnqueueOutcome(StrEnum):
    """Non-blocking result returned at the SDK acceptance boundary."""

    ACCEPTED = "accepted"
    FULL = "full"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class DeliveryFailure:
    """Safe terminal-failure notice for completion-state accounting."""

    code: str
    first_producer_seq: int
    last_producer_seq: int
    events: tuple[WireEvent, ...]


@dataclass(frozen=True, slots=True)
class WireEvent:
    """An already-sanitized event that retains no runtime object references."""

    event_id: str
    event_type: str
    schema_version: int
    timestamp_ns: int
    payload_json: str
    request_trace_id: str | None = None
    otel_trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("event_id", self.event_id, 256),
            ("event_type", self.event_type, 128),
        ):
            if type(value) is not str or not value or len(value) > maximum or not value.isascii():
                raise ValueError(f"{name} must be a bounded non-empty ASCII built-in str")
        if self.event_type not in _EVENT_TYPES:
            raise ValueError("event_type is unsupported by the private v1 protocol")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("schema_version must be the private v1 schema")
        if type(self.timestamp_ns) is not int or not 0 < self.timestamp_ns < 2**63:
            raise ValueError("timestamp_ns must be a positive signed-64 built-in int")
        if type(self.payload_json) is not str:
            raise TypeError("payload_json must be a built-in str")
        if len(self.payload_json.encode("utf-8")) > _MAX_EVENT_BYTES:
            raise ValueError("payload_json exceeds the private event limit")
        try:
            decoded_payload = json.loads(self.payload_json)
        except json.JSONDecodeError as error:
            raise ValueError("payload_json must be valid JSON") from error
        if type(decoded_payload) is not dict or set(decoded_payload) != {
            "data",
            "redaction",
            "truncated",
            "truncation",
        }:
            raise ValueError("payload_json must be a safe-summary envelope")
        if type(decoded_payload.get("data")) is not dict:
            raise ValueError("payload_json data must be an object")
        for optional_name, optional_value in (
            ("request_trace_id", self.request_trace_id),
            ("otel_trace_id", self.otel_trace_id),
            ("span_id", self.span_id),
            ("parent_span_id", self.parent_span_id),
        ):
            if optional_value is not None and (
                type(optional_value) is not str
                or not optional_value
                or len(optional_value) > 256
                or not optional_value.isascii()
            ):
                raise ValueError(
                    f"{optional_name} must be None or a bounded non-empty ASCII built-in str"
                )
        if self.otel_trace_id is not None and _LOWER_TRACE_ID.fullmatch(self.otel_trace_id) is None:
            raise ValueError("otel_trace_id must be 32 lowercase hexadecimal characters")
        for span_name, span_value in (
            ("span_id", self.span_id),
            ("parent_span_id", self.parent_span_id),
        ):
            if span_value is not None and _LOWER_SPAN_ID.fullmatch(span_value) is None:
                raise ValueError(f"{span_name} must be 16 lowercase hexadecimal characters")
        if self.event_type in {"span.ended", "span.enrichment", "snapshot.captured"} and (
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

    def to_wire(self, producer_seq: int) -> dict[str, object]:
        value: dict[str, object] = {
            "event_id": self.event_id,
            "producer_seq": producer_seq,
            "type": self.event_type,
            "schema_version": self.schema_version,
            "timestamp_ns": self.timestamp_ns,
            "payload_json": self.payload_json,
        }
        if self.request_trace_id is not None:
            value["request_trace_id"] = self.request_trace_id
        if self.otel_trace_id is not None:
            value["otel_trace_id"] = self.otel_trace_id
        if self.span_id is not None:
            value["span_id"] = self.span_id
        if self.parent_span_id is not None:
            value["parent_span_id"] = self.parent_span_id
        return value


@dataclass(frozen=True, slots=True)
class SenderHealth:
    """Immutable, thread-safe health snapshot with no capability token."""

    state: SenderState
    capacity: int
    queue_depth: int
    in_flight_count: int
    accepted_count: int
    acked_count: int
    dropped_count: int
    failed_count: int
    error_count: int
    last_error_code: str | None
    impacted_request_trace_ids: tuple[str, ...]
    impacted_request_overflow_count: int
    first_failed_producer_seq: int | None
    last_failed_producer_seq: int | None
    worker_thread_id: int | None


@dataclass(frozen=True, slots=True)
class _QueuedEvent:
    event: WireEvent
    producer_seq: int


class BoundedSender:
    """Batch safe events on one independent, bounded transport thread.

    ``enqueue`` never performs transport I/O and never waits for the queue.  A
    successful flush means every event accepted before the call received an
    explicit ACK proving a sidecar commit.  Failed batches remain visible and
    make the corresponding flush fail rather than silently pretending success.
    """

    def __init__(
        self,
        *,
        project_id: str,
        producer_id: str,
        lease_id: str,
        transport: Transport,
        capacity: int = 2048,
        batch_size: int = 128,
        request_timeout: float = 0.5,
        max_attempts: int = 2,
        failure_observer: FailureObserver | None = None,
    ) -> None:
        for name, value in (
            ("project_id", project_id),
            ("producer_id", producer_id),
            ("lease_id", lease_id),
        ):
            if type(value) is not str or not value or len(value) > 256:
                raise ValueError(f"{name} must be a bounded non-empty built-in str")
        if not callable(transport):
            raise TypeError("transport must be callable")
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("capacity must be a positive built-in int")
        if (
            type(batch_size) is not int
            or not 0 < batch_size <= capacity
            or batch_size > _MAX_EVENTS_PER_BATCH
        ):
            raise ValueError("batch_size must be in 1..min(capacity, 128)")
        if (
            type(request_timeout) not in {int, float}
            or not math.isfinite(request_timeout)
            or request_timeout <= 0
        ):
            raise ValueError("request_timeout must be a finite positive built-in number")
        if type(max_attempts) is not int or not 0 < max_attempts <= 3:
            raise ValueError("max_attempts must be a built-in int in 1..3")
        if failure_observer is not None and not callable(failure_observer):
            raise TypeError("failure_observer must be None or callable")

        self._project_id = project_id
        self._producer_id = producer_id
        self._lease_id = lease_id
        self._transport = transport
        self._capacity = capacity
        self._batch_size = batch_size
        self._request_timeout = float(request_timeout)
        self._max_attempts = max_attempts
        self._failure_observer = failure_observer
        self._condition = threading.Condition()
        self._pending: deque[_QueuedEvent] = deque()
        self._outstanding_event_ids: set[str] = set()
        self._state = SenderState.RUNNING
        self._thread_finished = False
        self._in_flight_count = 0
        self._accepted_count = 0
        self._processed_count = 0
        self._acked_count = 0
        self._dropped_count = 0
        self._failed_count = 0
        self._error_count = 0
        self._last_error_code: str | None = None
        self._first_failed_sequence = 0
        self._last_failed_sequence = 0
        self._impacted_request_trace_ids: dict[str, None] = {}
        self._impacted_request_overflow_count = 0
        self._worker_thread_id: int | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="flowsight-private-sender-spike",
            daemon=True,
        )
        self._thread.start()

    @property
    def health(self) -> SenderHealth:
        with self._condition:
            return SenderHealth(
                state=self._state,
                capacity=self._capacity,
                queue_depth=len(self._pending),
                in_flight_count=self._in_flight_count,
                accepted_count=self._accepted_count,
                acked_count=self._acked_count,
                dropped_count=self._dropped_count,
                failed_count=self._failed_count,
                error_count=self._error_count,
                last_error_code=self._last_error_code,
                impacted_request_trace_ids=tuple(self._impacted_request_trace_ids),
                impacted_request_overflow_count=self._impacted_request_overflow_count,
                first_failed_producer_seq=(
                    None if self._first_failed_sequence == 0 else self._first_failed_sequence
                ),
                last_failed_producer_seq=(
                    None if self._last_failed_sequence == 0 else self._last_failed_sequence
                ),
                worker_thread_id=self._worker_thread_id,
            )

    def enqueue(self, event: WireEvent) -> EnqueueOutcome:
        if type(event) is not WireEvent:
            raise TypeError("event must be an exact WireEvent")
        with self._condition:
            if self._state is not SenderState.RUNNING:
                self._record_impact_locked(event)
                self._dropped_count += 1
                return EnqueueOutcome.CLOSED
            if event.event_id in self._outstanding_event_ids:
                raise ValueError("event_id is already outstanding in this sender")
            if self._accepted_count - self._processed_count >= self._capacity:
                self._record_impact_locked(event)
                self._dropped_count += 1
                return EnqueueOutcome.FULL
            self._accepted_count += 1
            self._outstanding_event_ids.add(event.event_id)
            self._pending.append(_QueuedEvent(event, self._accepted_count))
            self._condition.notify()
            return EnqueueOutcome.ACCEPTED

    def flush(self, timeout: float = 2.0) -> None:
        deadline = self._deadline(timeout)
        with self._condition:
            target = self._accepted_count
            while self._processed_count < target:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SenderTimeoutError("private sender flush timed out")
                self._condition.wait(remaining)
            if 0 < self._first_failed_sequence <= target:
                raise DeliveryError("accepted private events failed before commit ACK")

    def close(self, timeout: float = 2.0) -> None:
        deadline = self._deadline(timeout)
        with self._condition:
            if self._state is SenderState.RUNNING:
                self._state = SenderState.CLOSING
                self._condition.notify_all()
            while not self._thread_finished:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SenderTimeoutError("private sender close timed out")
                self._condition.wait(remaining)
            failed = self._failed_count
        if failed:
            raise DeliveryError("private sender closed with uncommitted accepted events")

    @staticmethod
    def _deadline(timeout: float) -> float:
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be a finite non-negative built-in number")
        return time.monotonic() + timeout

    def _run(self) -> None:
        try:
            self._run_batches()
        except BaseException:
            with self._condition:
                outstanding = self._accepted_count - self._processed_count
                if outstanding:
                    if self._first_failed_sequence == 0:
                        self._first_failed_sequence = self._processed_count + 1
                    self._last_failed_sequence = self._accepted_count
                    self._failed_count += outstanding
                    self._processed_count = self._accepted_count
                self._pending.clear()
                self._outstanding_event_ids.clear()
                self._in_flight_count = 0
                self._error_count += 1
                self._last_error_code = "SENDER_INTERNAL_FAILED"
                self._state = SenderState.CLOSED
                self._thread_finished = True
                self._condition.notify_all()

    def _run_batches(self) -> None:
        with self._condition:
            self._worker_thread_id = threading.get_ident()
            self._condition.notify_all()

        while True:
            with self._condition:
                while not self._pending and self._state is SenderState.RUNNING:
                    self._condition.wait()
                if not self._pending:
                    if self._state is SenderState.CLOSING:
                        self._state = SenderState.CLOSED
                        self._thread_finished = True
                        self._condition.notify_all()
                        return
                    continue
                batch = self._take_batch_locked()
                self._in_flight_count = len(batch)

            envelope = self._envelope(batch)
            error_code: str | None = None
            for _attempt in range(self._max_attempts):
                try:
                    with suppress_instrumentation(), suppress_http_instrumentation():
                        response = self._transport(envelope, self._request_timeout)
                    self._validate_ack(envelope, response)
                except TransportError as error:
                    error_code = error.code
                except (OSError, TimeoutError, http.client.HTTPException):
                    error_code = "TRANSPORT_ERROR"
                except BaseException:
                    error_code = "INTERNAL_TRANSPORT_ERROR"
                else:
                    error_code = None
                    break

            failure_notice = (
                None
                if error_code is None
                else DeliveryFailure(
                    code=error_code,
                    first_producer_seq=batch[0].producer_seq,
                    last_producer_seq=batch[-1].producer_seq,
                    events=tuple(item.event for item in batch),
                )
            )
            observer_failed = False
            if failure_notice is not None and self._failure_observer is not None:
                try:
                    self._failure_observer(failure_notice)
                except BaseException:
                    observer_failed = True

            with self._condition:
                self._processed_count += len(batch)
                self._in_flight_count = 0
                for item in batch:
                    self._outstanding_event_ids.discard(item.event.event_id)
                if error_code is None:
                    self._acked_count += len(batch)
                else:
                    self._failed_count += len(batch)
                    self._error_count += 1
                    self._last_error_code = error_code
                    if self._first_failed_sequence == 0:
                        self._first_failed_sequence = batch[0].producer_seq
                    self._last_failed_sequence = batch[-1].producer_seq
                    for item in batch:
                        self._record_impact_locked(item.event)
                if observer_failed:
                    self._error_count += 1
                    self._last_error_code = "FAILURE_OBSERVER_FAILED"
                self._condition.notify_all()

    def _take_batch_locked(self) -> tuple[_QueuedEvent, ...]:
        selected: list[_QueuedEvent] = []
        remaining_budget = _MAX_BATCH_BYTES - _BATCH_FIXED_BUDGET
        while self._pending and len(selected) < self._batch_size:
            candidate = self._pending[0]
            candidate_budget = self._event_wire_budget(candidate.event)
            if selected and candidate_budget > remaining_budget:
                break
            selected.append(self._pending.popleft())
            remaining_budget -= candidate_budget
        return tuple(selected)

    @staticmethod
    def _event_wire_budget(event: WireEvent) -> int:
        text_values = (
            event.event_id,
            event.event_type,
            event.payload_json,
            event.request_trace_id or "",
            event.otel_trace_id or "",
            event.span_id or "",
            event.parent_span_id or "",
        )
        return _EVENT_WIRE_OVERHEAD_BUDGET + sum(
            max(len(value.encode("utf-8")), len(value) * 12) for value in text_values
        )

    def _envelope(self, batch: tuple[_QueuedEvent, ...]) -> dict[str, object]:
        first = batch[0].producer_seq
        last = batch[-1].producer_seq
        return {
            "protocol_version": PROTOCOL_VERSION,
            "project_id": self._project_id,
            "producer_id": self._producer_id,
            "lease_id": self._lease_id,
            "batch_id": f"{first}-{last}",
            "events": [item.event.to_wire(item.producer_seq) for item in batch],
        }

    @staticmethod
    def _validate_ack(envelope: dict[str, object], response: dict[str, object]) -> None:
        if type(response) is not dict or response.get("batch_id") != envelope["batch_id"]:
            raise TransportError("INVALID_ACK")
        raw_events = envelope["events"]
        if type(raw_events) is not list:
            raise TransportError("INVALID_BATCH")
        expected = {
            event["event_id"]
            for event in raw_events
            if type(event) is dict and type(event.get("event_id")) is str
        }
        if len(expected) != len(raw_events):
            raise TransportError("INVALID_BATCH")
        committed = response.get("committed_event_ids")
        duplicate = response.get("duplicate_event_ids")
        if type(committed) is not list or type(duplicate) is not list:
            raise TransportError("INVALID_ACK")
        if any(type(value) is not str for value in (*committed, *duplicate)):
            raise TransportError("INVALID_ACK")
        acknowledged_values = [*committed, *duplicate]
        acknowledged = set(acknowledged_values)
        if (
            len(acknowledged_values) != len(expected)
            or len(acknowledged) != len(acknowledged_values)
            or acknowledged != expected
        ):
            raise TransportError("INCOMPLETE_ACK")

    def _record_impact_locked(self, event: WireEvent) -> None:
        request_trace_id = event.request_trace_id
        if request_trace_id is None or request_trace_id in self._impacted_request_trace_ids:
            return
        if len(self._impacted_request_trace_ids) >= _IMPACTED_TRACE_LIMIT:
            self._impacted_request_overflow_count += 1
            return
        self._impacted_request_trace_ids[request_trace_id] = None


@dataclass(frozen=True, slots=True)
class HTTPBatchTransport:
    """Authenticated loopback transport invoked only by ``BoundedSender``."""

    state: SidecarState = field(repr=False)

    def __call__(self, envelope: dict[str, object], timeout: float) -> dict[str, object]:
        encoded = json.dumps(envelope, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        if len(encoded) > _MAX_BATCH_BYTES:
            raise TransportError("BATCH_TOO_LARGE")
        connection = http.client.HTTPConnection(self.state.host, self.state.port, timeout=timeout)
        connection.set_debuglevel(0)
        try:
            connection.putrequest(
                "POST",
                "/internal/v1/events",
                skip_host=True,
                skip_accept_encoding=True,
            )
            connection.putheader("Host", self.state.authority)
            connection.putheader("Authorization", f"Bearer {self.state.token}")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(len(encoded)))
            connection.endheaders(encoded)
            response = connection.getresponse()
            response_body = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(response_body) > _MAX_RESPONSE_BYTES:
                raise TransportError("RESPONSE_TOO_LARGE")
            if response.status != 200:
                raise TransportError(f"HTTP_{response.status}")
            try:
                decoded: Any = json.loads(response_body)
            except json.JSONDecodeError as error:
                raise TransportError("INVALID_JSON") from error
            if type(decoded) is not dict:
                raise TransportError("INVALID_ACK")
            return decoded
        finally:
            connection.close()


class TelemetrySenderAdapter:
    """Convert safe processor events and preserve exact queue rejection semantics."""

    def __init__(
        self,
        sender: BoundedSender,
        *,
        event_id_prefix: str | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if type(sender) is not BoundedSender:
            raise TypeError("sender must be an exact BoundedSender")
        prefix = secrets.token_hex(16) if event_id_prefix is None else event_id_prefix
        if type(prefix) is not str or not prefix or len(prefix) > 128 or not prefix.isascii():
            raise ValueError("event_id_prefix must be a bounded non-empty ASCII built-in str")
        if not callable(clock_ns):
            raise TypeError("clock_ns must be callable")
        self._sender = sender
        self._event_id_prefix = prefix
        self._clock_ns = clock_ns
        self._lock = threading.Lock()
        self._event_counter = 0

    def enqueue(self, event: TelemetryEvent) -> bool:
        """Return ``False`` for FULL/CLOSED instead of leaking truthy enums."""

        if type(event) is not TelemetryEvent:
            raise TypeError("event must be an exact TelemetryEvent")
        with self._lock:
            self._event_counter += 1
            event_id = f"{self._event_id_prefix}:{self._event_counter}"
        wire_event = WireEvent(
            event_id=event_id,
            event_type=event.event_type,
            schema_version=1,
            timestamp_ns=self._clock_ns(),
            payload_json=event.payload_json,
            request_trace_id=event.request_trace_id,
            otel_trace_id=event.otel_trace_id,
            span_id=event.span_id,
            parent_span_id=event.parent_span_id,
        )
        return self._sender.enqueue(wire_event) is EnqueueOutcome.ACCEPTED
