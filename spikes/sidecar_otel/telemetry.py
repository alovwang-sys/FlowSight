"""Isolated OpenTelemetry lifecycle and request-association spike.

This module intentionally uses only OpenTelemetry's public provider, processor,
tracer, and FastAPI instrumentation APIs.  It is a trial harness, not the
production Phase 1 collector.
"""

from __future__ import annotations

import hashlib
import math
import threading
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from time import monotonic
from typing import Protocol, cast
from weakref import WeakKeyDictionary

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import (
    ReadableSpan,
    SpanProcessor,
)
from opentelemetry.sdk.trace import (
    Span as SDKSpan,
)
from opentelemetry.sdk.trace import (
    TracerProvider as SDKTracerProvider,
)
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer

from flowsight.security import safe_summary

Clock = Callable[[], float]
SpanKey = tuple[int, int]


class Enqueue(Protocol):
    """Non-blocking handoff used by processor callbacks.

    Returning exactly ``False`` reports bounded-queue rejection.  ``None`` is
    accepted as a convenience for ``list.append`` and similarly small fakes.
    """

    def __call__(self, event: TelemetryEvent, /) -> bool | None: ...


Flush = Callable[[float], bool | None]


class UnsupportedTracerProviderError(RuntimeError):
    """The configured provider cannot accept an SDK ``SpanProcessor``."""


@dataclass(frozen=True, slots=True)
class TelemetryEvent:
    """Already-safe immutable event handed to the bounded sender queue."""

    event_type: str
    request_trace_id: str
    otel_trace_id: str
    span_id: str
    parent_span_id: str | None
    payload_json: str


@dataclass(frozen=True, slots=True)
class TelemetryHealth:
    """Immutable diagnostic snapshot; it retains no SDK span objects."""

    active: bool
    draining: bool
    association_count: int
    orphan_count: int
    queue_drop_count: int
    delivery_failure_count: int
    incomplete_request_overflow_count: int
    provisional_root_count: int
    provisional_root_overflow_count: int
    provisional_root_drop_count: int
    provisional_orphan_count: int
    provisional_orphan_overflow_count: int
    provisional_span_count: int
    provisional_span_overflow_count: int
    admission_uncertain: bool
    ignored_span_count: int
    ignored_span_overflow_count: int
    association_eviction_count: int
    orphan_capacity_drop_count: int
    orphan_loss_global: bool
    completion_uncertain: bool
    unscoped_span_count: int
    callback_error_count: int
    non_recording_request_count: int
    emitted_event_count: int
    incomplete_request_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProviderBindingSnapshot:
    """Public, immutable evidence for provider ownership and processor reuse."""

    provider_identity: int
    processor_identity: int
    owns_provider: bool
    registration_count: int
    sampler_description: str
    warning: str | None
    active: bool


class FastAPIAppBinding:
    """One reusable app binding with a fresh identity for each active session."""

    __slots__ = ("_activation", "_lock")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._activation: object | None = None

    def activate(self) -> object:
        """Activate idempotently; reactivation after shutdown gets a new identity."""

        with self._lock:
            if self._activation is None:
                self._activation = object()
            return self._activation

    def deactivate(self) -> object | None:
        """Invalidate this app and every request that entered its old session."""

        with self._lock:
            activation = self._activation
            self._activation = None
            return activation

    def capture(self) -> object | None:
        """Return the current opaque activation identity for one request entry."""

        with self._lock:
            return self._activation

    def accepts(self, activation: object | None) -> bool:
        """Return whether an entry identity still belongs to the active session."""

        if activation is None:
            return False
        with self._lock:
            return self._activation is activation


@dataclass(frozen=True, slots=True)
class FastAPIInstrumentationResult:
    """Result of one idempotent FlowSight app binding request.

    ``newly_registered`` describes the FlowSight context-middleware registry.
    OpenTelemetry's public ``instrument_app`` API does not report whether it
    added a wrapper or found a wrapper installed earlier by the user.
    """

    newly_registered: bool
    provider_identity: int
    processor_identity: int
    app_binding: FastAPIAppBinding


@dataclass(frozen=True, slots=True)
class _Association:
    request_trace_id: str
    root_span_id: int
    app_capture: _AppCapture | None = None


@dataclass(frozen=True, slots=True)
class _RequestContext:
    processor_marker: object
    request_trace_id: str
    otel_trace_id: int
    root_span_id: int
    app_capture: _AppCapture | None = None


@dataclass(frozen=True, slots=True)
class _AppCapture:
    processor_marker: object
    app_binding: FastAPIAppBinding
    activation: object | None

    def is_active(self) -> bool:
        return self.app_binding.accepts(self.activation)


@dataclass(frozen=True, slots=True)
class _PendingSpan:
    key: SpanKey
    parent_key: SpanKey | None
    event: TelemetryEvent
    expires_at: float
    app_capture: _AppCapture | None = None
    provisional_root_key: SpanKey | None = None
    provisional_boundary_key: SpanKey | None = None
    is_provisional_server: bool = False


@dataclass(frozen=True, slots=True)
class _ResolvedEvent:
    event: TelemetryEvent
    app_capture: _AppCapture | None


@dataclass(slots=True)
class _ProvisionalRoot:
    app_capture: _AppCapture
    loss_boundaries: set[SpanKey | None] = field(default_factory=set)
    loss_overflow: bool = False


@dataclass(frozen=True, slots=True)
class _ProvisionalSpan:
    root_key: SpanKey
    boundary_key: SpanKey | None
    is_server: bool


_CURRENT_REQUEST: ContextVar[_RequestContext | None] = ContextVar(
    "flowsight_spike_current_request", default=None
)
_CURRENT_APP_CAPTURE: ContextVar[_AppCapture | None] = ContextVar(
    "flowsight_spike_current_app_capture", default=None
)


def derive_request_trace_id(project_id: str, trace_id: int, root_span_id: int) -> str:
    """Derive one local request identity from the nearest server span."""

    material = f"{project_id}:{trace_id:032x}:{root_span_id:016x}".encode()
    return hashlib.sha256(material).hexdigest()[:32]


class FlowSightSpanProcessor(SpanProcessor):
    """Reusable, disableable processor with bounded exact-parent association."""

    def __init__(
        self,
        enqueue: Enqueue | None = None,
        *,
        flush: Flush | None = None,
        project_id: str = "flowsight-spike",
        clock: Clock = monotonic,
        association_capacity: int = 4096,
        orphan_capacity: int = 1024,
        orphan_ttl: float = 1.0,
        event_snapshot_capacity: int = 1024,
    ) -> None:
        if association_capacity < 1:
            raise ValueError("association_capacity must be positive")
        if orphan_capacity < 1:
            raise ValueError("orphan_capacity must be positive")
        if orphan_ttl <= 0:
            raise ValueError("orphan_ttl must be positive")
        if event_snapshot_capacity < 1:
            raise ValueError("event_snapshot_capacity must be positive")

        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._marker = object()
        self._project_id = project_id
        self._clock = clock
        self._association_capacity = association_capacity
        self._orphan_capacity = orphan_capacity
        self._orphan_ttl = orphan_ttl
        self._associations: OrderedDict[SpanKey, _Association] = OrderedDict()
        self._provisional_roots: OrderedDict[SpanKey, _ProvisionalRoot] = OrderedDict()
        self._provisional_orphans: OrderedDict[SpanKey, _PendingSpan] = OrderedDict()
        self._provisional_orphans_by_parent: dict[SpanKey, set[SpanKey]] = {}
        self._provisional_spans: OrderedDict[SpanKey, _ProvisionalSpan] = OrderedDict()
        self._tombstones: OrderedDict[SpanKey, None] = OrderedDict()
        self._orphans: OrderedDict[SpanKey, _PendingSpan] = OrderedDict()
        self._orphans_by_parent: dict[SpanKey, set[SpanKey]] = {}
        self._orphan_loss_parents: OrderedDict[SpanKey, None] = OrderedDict()
        self._orphan_loss_global = False
        self._admission_uncertain = False
        self._resolved_events: deque[_ResolvedEvent] = deque(maxlen=orphan_capacity)
        self._incomplete: OrderedDict[str, None] = OrderedDict()
        self._event_snapshots: deque[TelemetryEvent] = deque(maxlen=event_snapshot_capacity)
        self._enqueue: Enqueue | None = None
        self._flush: Flush | None = None
        self._active = False
        self._accepting = False
        self._callbacks_in_flight = 0
        self._draining = False
        self._last_deactivate_result = True
        self._terminal = False
        self._app_gate_transition = False
        self._queue_drop_count = 0
        self._delivery_failure_count = 0
        self._incomplete_request_overflow_count = 0
        self._provisional_root_overflow_count = 0
        self._provisional_root_drop_count = 0
        self._provisional_orphan_overflow_count = 0
        self._provisional_span_overflow_count = 0
        self._ignored_span_overflow_count = 0
        self._association_eviction_count = 0
        self._orphan_capacity_drop_count = 0
        self._unscoped_span_count = 0
        self._callback_error_count = 0
        self._non_recording_request_count = 0
        self._emitted_event_count = 0
        if enqueue is not None:
            self.activate(enqueue, flush=flush)

        self._app_gating_enabled = False
        self._active_app_capture: _AppCapture | None = None

    def activate(self, enqueue: Enqueue, *, flush: Flush | None = None) -> None:
        """Enable or re-enable the permanently registered processor."""

        with self._condition:
            if self._terminal:
                raise RuntimeError("provider has shut down this processor")
            if self._draining:
                raise RuntimeError("processor deactivation is still draining")
            if self._active:
                if self._accepting:
                    return
                raise RuntimeError("processor deactivation must complete before reactivation")
            if not self._last_deactivate_result and self._flush is not None:
                raise RuntimeError("processor flush must succeed before reactivation")
            self._enqueue = enqueue
            self._flush = flush
            self._active = True
            self._accepting = True
            self._last_deactivate_result = True

    def enable_app_gating(self, timeout: float = 2.0) -> None:
        """Require positive FastAPI middleware admission for future server roots."""

        if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be a finite non-negative built-in number")
        deadline = monotonic() + timeout
        with self._condition:
            while self._app_gate_transition:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("app-gating transition timed out")
                self._condition.wait(remaining)
            if self._app_gating_enabled:
                return
            previous_accepting = self._accepting
            self._app_gate_transition = True
            self._accepting = False
            try:
                while self._callbacks_in_flight:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise TimeoutError("app-gating transition timed out")
                    self._condition.wait(remaining)
                try:
                    self._finish_app_gating_transition_locked()
                except BaseException:
                    # The transition is fail-closed. A single asynchronous
                    # interruption cannot leave an ungated association behind.
                    self._finish_app_gating_transition_locked()
                    raise
            finally:
                if self._active and not self._draining:
                    self._accepting = previous_accepting
                self._app_gate_transition = False
                self._condition.notify_all()

    def _finish_app_gating_transition_locked(self) -> None:
        self._app_gating_enabled = True
        for key, association in tuple(self._associations.items()):
            if association.app_capture is None:
                self._mark_incomplete_locked(association.request_trace_id)
                self._put_tombstone_locked(key)
                self._discard_orphan_descendants_locked(key)
                self._associations.pop(key, None)
        retained_resolved: deque[_ResolvedEvent] = deque(maxlen=self._orphan_capacity)
        for resolved in self._resolved_events:
            if resolved.app_capture is None:
                self._mark_incomplete_locked(resolved.event.request_trace_id)
            else:
                retained_resolved.append(resolved)
        self._resolved_events = retained_resolved

    def activate_app_binding(self, app_binding: FastAPIAppBinding) -> None:
        """Activate an app only while this processor can accept its callbacks."""

        with self._condition:
            if self._terminal or not self._active or not self._accepting or self._draining:
                raise RuntimeError("telemetry processor cannot activate a FastAPI app")
            current = self._active_app_capture
            if (
                current is not None
                and current.is_active()
                and current.app_binding is not app_binding
            ):
                raise RuntimeError("telemetry processor already has an active FastAPI app")
            activation = app_binding.activate()
            self._active_app_capture = _AppCapture(
                processor_marker=self._marker,
                app_binding=app_binding,
                activation=activation,
            )

    def deactivate_app_binding(self, app_binding: FastAPIAppBinding) -> None:
        """Invalidate one activation and retain bounded exact ignore tombstones."""

        activation = app_binding.deactivate()
        if activation is None:
            return
        with self._lock:
            if self._capture_matches(self._active_app_capture, app_binding, activation):
                self._active_app_capture = None
            retired_keys: list[SpanKey] = []
            for key, provisional in tuple(self._provisional_roots.items()):
                if self._capture_matches(provisional.app_capture, app_binding, activation):
                    self._provisional_roots.pop(key, None)
                    self._put_tombstone_locked(key)
                    self._discard_provisional_descendants_locked(key)
                    self._provisional_root_drop_count += 1
            for key, association in tuple(self._associations.items()):
                association_capture = association.app_capture
                if (
                    association_capture is not None
                    and association_capture.app_binding is app_binding
                    and association_capture.activation is activation
                ):
                    self._associations.pop(key, None)
                    self._mark_incomplete_locked(association.request_trace_id)
                    self._put_tombstone_locked(key)
                    retired_keys.append(key)
            for key, pending in tuple(self._orphans.items()):
                pending_capture = pending.app_capture
                if (
                    pending_capture is not None
                    and pending_capture.app_binding is app_binding
                    and pending_capture.activation is activation
                ):
                    self._remove_orphan_locked(key)
                    self._put_tombstone_locked(key)
            retained_resolved: deque[_ResolvedEvent] = deque(maxlen=self._orphan_capacity)
            for resolved in self._resolved_events:
                if self._capture_matches(resolved.app_capture, app_binding, activation):
                    self._mark_incomplete_locked(resolved.event.request_trace_id)
                else:
                    retained_resolved.append(resolved)
            self._resolved_events = retained_resolved
            for key in retired_keys:
                self._discard_orphan_descendants_locked(key)

    def deactivate(self, timeout: float = 2.0) -> bool:
        """Stop accepting events, clear request state, then flush our sender only."""

        if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be a finite non-negative built-in number")
        deadline = monotonic() + timeout
        with self._condition:
            if self._draining:
                while self._draining:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        return False
                    self._condition.wait(remaining)
                return self._last_deactivate_result
            self._draining = True
            if self._active:
                self._accepting = False
                while self._callbacks_in_flight:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        self._draining = False
                        self._last_deactivate_result = False
                        self._condition.notify_all()
                        return False
                    self._condition.wait(remaining)
                self._active = False
                self._enqueue = None
                self._active_app_capture = None
                self._provisional_root_drop_count += len(self._provisional_roots)
                for key in self._provisional_roots:
                    self._put_tombstone_locked(key)
                self._provisional_roots.clear()
                for key in self._provisional_orphans:
                    self._put_tombstone_locked(key)
                self._provisional_orphans.clear()
                self._provisional_orphans_by_parent.clear()
                self._provisional_spans.clear()
                for key, association in self._associations.items():
                    self._mark_incomplete_locked(association.request_trace_id)
                    self._put_tombstone_locked(key)
                self._associations.clear()
                for event in self._resolved_events:
                    self._mark_incomplete_locked(event.event.request_trace_id)
                if self._orphans or self._orphan_loss_parents:
                    self._orphan_loss_global = True
                self._orphans.clear()
                self._orphans_by_parent.clear()
                self._orphan_loss_parents.clear()
                self._resolved_events.clear()
            elif self._last_deactivate_result or self._flush is None:
                self._draining = False
                return self._last_deactivate_result
            flush = self._flush
        result = True
        fatal_error: BaseException | None = None
        if flush is not None:
            try:
                result = flush(max(0.0, deadline - monotonic())) is not False
            except Exception:
                with self._lock:
                    self._callback_error_count += 1
                result = False
            except BaseException as error:
                with self._lock:
                    self._callback_error_count += 1
                result = False
                fatal_error = error
        with self._condition:
            self._last_deactivate_result = result
            if result:
                self._flush = None
            self._draining = False
            self._condition.notify_all()
        if fatal_error is not None:
            raise fatal_error
        return result

    def health_snapshot(self) -> TelemetryHealth:
        """Return stable counters without exposing mutable processor state."""

        with self._lock:
            return TelemetryHealth(
                active=self._active,
                draining=self._draining,
                association_count=len(self._associations),
                orphan_count=len(self._orphans),
                queue_drop_count=self._queue_drop_count,
                delivery_failure_count=self._delivery_failure_count,
                incomplete_request_overflow_count=self._incomplete_request_overflow_count,
                provisional_root_count=len(self._provisional_roots),
                provisional_root_overflow_count=self._provisional_root_overflow_count,
                provisional_root_drop_count=self._provisional_root_drop_count,
                provisional_orphan_count=len(self._provisional_orphans),
                provisional_orphan_overflow_count=self._provisional_orphan_overflow_count,
                provisional_span_count=len(self._provisional_spans),
                provisional_span_overflow_count=self._provisional_span_overflow_count,
                admission_uncertain=self._admission_uncertain,
                ignored_span_count=len(self._tombstones),
                ignored_span_overflow_count=self._ignored_span_overflow_count,
                association_eviction_count=self._association_eviction_count,
                orphan_capacity_drop_count=self._orphan_capacity_drop_count,
                orphan_loss_global=self._orphan_loss_global,
                completion_uncertain=(self._orphan_loss_global or self._admission_uncertain),
                unscoped_span_count=self._unscoped_span_count,
                callback_error_count=self._callback_error_count,
                non_recording_request_count=self._non_recording_request_count,
                emitted_event_count=self._emitted_event_count,
                incomplete_request_ids=tuple(self._incomplete),
            )

    snapshot_health = health_snapshot

    def event_snapshot(self) -> tuple[TelemetryEvent, ...]:
        """Return the bounded immutable history of accepted callback events."""

        with self._lock:
            return tuple(self._event_snapshots)

    snapshot_events = event_snapshot

    def on_start(self, span: SDKSpan, parent_context: Context | None = None) -> None:
        """Associate IDs only; never serialize values, block, or access network."""

        if not self._begin_callback():
            return
        try:
            self._on_start(span, parent_context)
        except Exception:
            with self._lock:
                self._callback_error_count += 1
        finally:
            self._end_callback()

    def _on_start(self, span: SDKSpan, parent_context: Context | None) -> None:
        span_context = span.get_span_context()
        if not span_context.is_valid:
            return
        key = (span_context.trace_id, span_context.span_id)
        parent_key = self._parent_key(span, parent_context)
        app_capture = self._current_app_capture()
        with self._lock:
            if not self._active:
                return
            if key in self._tombstones:
                self._tombstones.move_to_end(key)
                if span.kind is SpanKind.SERVER:
                    if self._provisional_roots.pop(key, None) is not None:
                        self._provisional_root_drop_count += 1
                    self._discard_orphan_descendants_locked(key)
                    self._discard_provisional_descendants_locked(key)
                return
            if parent_key is not None and parent_key in self._tombstones:
                self._put_tombstone_locked(key)
                return
            if app_capture is not None and not app_capture.is_active():
                self._associations.pop(key, None)
                self._put_tombstone_locked(key)
                return
            self._sweep_expired_locked(self._clock())
            provisional_span: _ProvisionalSpan | None = None
            if self._app_gating_enabled and parent_key is not None:
                root_key: SpanKey | None
                parent_boundary: SpanKey | None
                if parent_key in self._provisional_roots:
                    root_key = parent_key
                    parent_boundary = None
                else:
                    provisional_parent = self._provisional_spans.get(parent_key)
                    root_key = None if provisional_parent is None else provisional_parent.root_key
                    parent_boundary = (
                        None
                        if provisional_parent is None
                        else (
                            parent_key
                            if provisional_parent.is_server
                            else provisional_parent.boundary_key
                        )
                    )
                if root_key is not None:
                    provisional_span = _ProvisionalSpan(
                        root_key=root_key,
                        boundary_key=(key if span.kind is SpanKind.SERVER else parent_boundary),
                        is_server=span.kind is SpanKind.SERVER,
                    )
                    self._put_provisional_span_locked(key, provisional_span)
            association: _Association | None
            if span.kind is SpanKind.SERVER:
                if self._app_gating_enabled:
                    if app_capture is not None and app_capture.is_active():
                        association = self._server_association(key, app_capture=app_capture)
                        self._put_association_locked(key, association)
                        return
                    parent_association = (
                        None if parent_key is None else self._associations.get(parent_key)
                    )
                    if parent_association is not None and self._association_is_active(
                        parent_association
                    ):
                        association = self._server_association(
                            key,
                            app_capture=parent_association.app_capture,
                        )
                        self._put_association_locked(key, association)
                        return
                    if provisional_span is not None:
                        return
                    active_capture = self._active_app_capture
                    if active_capture is None or not active_capture.is_active():
                        self._put_tombstone_locked(key)
                        self._provisional_root_drop_count += 1
                        return
                    self._put_provisional_root_locked(key, active_capture)
                    return
                association = self._server_association(key, app_capture=app_capture)
            else:
                association = self._association_for_parent_locked(span_context.trace_id, parent_key)
            if association is None:
                return
            if not self._association_is_active(association):
                self._associations.pop(key, None)
                self._put_tombstone_locked(key)
                return
            self._put_association_locked(key, association)
            for event in self._resolve_orphans_locked(key, association):
                if len(self._resolved_events) == self._resolved_events.maxlen:
                    dropped = self._resolved_events.popleft().event
                    self._orphan_capacity_drop_count += 1
                    self._mark_incomplete_locked(dropped.request_trace_id)
                self._resolved_events.append(_ResolvedEvent(event, association.app_capture))

    def on_end(self, span: ReadableSpan) -> None:
        """Convert to a safe immutable event and perform non-blocking handoff."""

        if not self._begin_callback():
            return
        try:
            self._on_end(span)
        except Exception:
            with self._lock:
                self._callback_error_count += 1
        finally:
            self._end_callback()

    def _on_end(self, span: ReadableSpan) -> None:
        span_context = span.context
        if span_context is None or not span_context.is_valid:
            return
        key = (span_context.trace_id, span_context.span_id)
        parent_key = self._readable_parent_key(span)
        app_capture = self._current_app_capture()
        to_emit: list[TelemetryEvent] = []
        with self._lock:
            if not self._active:
                return
            if key in self._tombstones:
                self._tombstones.move_to_end(key)
                if span.kind is SpanKind.SERVER:
                    if self._provisional_roots.pop(key, None) is not None:
                        self._provisional_root_drop_count += 1
                    self._discard_orphan_descendants_locked(key)
                    self._discard_provisional_descendants_locked(key)
                return
            if app_capture is not None and not app_capture.is_active():
                self._associations.pop(key, None)
                self._put_tombstone_locked(key)
                return
            association = self._associations.get(key)
            if association is not None and not self._association_is_active(association):
                self._associations.pop(key, None)
                self._put_tombstone_locked(key)
                return
            provisional_nested = self._provisional_spans.get(key)
            if (
                association is None
                and span.kind is SpanKind.SERVER
                and self._app_gating_enabled
                and (provisional_nested is None or not provisional_nested.is_server)
            ):
                if self._provisional_roots.pop(key, None) is not None:
                    self._provisional_root_drop_count += 1
                self._put_tombstone_locked(key)
                self._discard_orphan_descendants_locked(key)
                self._discard_provisional_descendants_locked(key)
                return
        event = self._safe_span_event(span, request_trace_id="")
        with self._lock:
            if not self._active:
                return
            now = self._clock()
            self._sweep_expired_locked(now)
            while self._resolved_events:
                resolved = self._resolved_events.popleft()
                if resolved.app_capture is None or resolved.app_capture.is_active():
                    to_emit.append(resolved.event)
                else:
                    self._mark_incomplete_locked(resolved.event.request_trace_id)
            association = self._associations.get(key)
            if association is not None and not self._association_is_active(association):
                self._associations.pop(key, None)
                self._put_tombstone_locked(key)
                return
            if association is None and span.kind is SpanKind.SERVER:
                if not self._app_gating_enabled:
                    association = self._server_association(key, app_capture=app_capture)
            if association is None:
                association = self._association_for_parent_locked(span_context.trace_id, parent_key)
            if association is not None and not self._association_is_active(association):
                self._associations.pop(key, None)
                self._put_tombstone_locked(key)
                return
            if association is None:
                provisional_span = self._provisional_spans.pop(key, None)
                pending = _PendingSpan(
                    key=key,
                    parent_key=parent_key,
                    event=event,
                    expires_at=now + self._orphan_ttl,
                    app_capture=app_capture,
                    provisional_root_key=(
                        None if provisional_span is None else provisional_span.root_key
                    ),
                    provisional_boundary_key=(
                        None if provisional_span is None else provisional_span.boundary_key
                    ),
                    is_provisional_server=(
                        provisional_span is not None and provisional_span.is_server
                    ),
                )
                if provisional_span is not None:
                    self._buffer_provisional_orphan_locked(pending)
                elif self._app_gating_enabled and parent_key is not None:
                    self._put_tombstone_locked(key)
                else:
                    self._buffer_orphan_locked(pending)
            else:
                self._apply_orphan_loss_locked(key, association)
                to_emit.append(self._with_request(event, association.request_trace_id))
                to_emit.extend(self._resolve_orphans_locked(key, association))
                to_emit.extend(self._resolve_provisional_orphans_locked(key, association))
                self._associations.pop(key, None)
        self._emit_many(to_emit)

    @contextmanager
    def request_context(
        self,
        span: Span,
        *,
        app_capture: _AppCapture | None = None,
    ) -> Iterator[str | None]:
        """Bind the recording FastAPI server span for sync/async/thread handoff."""

        span_context = span.get_span_context()
        if not span.is_recording() or not span_context.is_valid:
            with self._lock:
                if self._accepting:
                    self._non_recording_request_count += 1
            yield None
            return

        key = (span_context.trace_id, span_context.span_id)
        association = self._server_association(key, app_capture=app_capture)
        accepting = False
        if self._begin_callback():
            try:
                with self._lock:
                    accepting = self._accepting
                    provisional_root = self._provisional_roots.pop(key, None)
                    approved_capture = (
                        app_capture is not None
                        and app_capture.is_active()
                        and (
                            not self._app_gating_enabled
                            or (
                                provisional_root is not None
                                and self._same_capture(provisional_root.app_capture, app_capture)
                            )
                        )
                    )
                    if key in self._tombstones or (
                        self._app_gating_enabled and not approved_capture
                    ):
                        if provisional_root is not None:
                            self._provisional_root_drop_count += 1
                        self._put_tombstone_locked(key)
                        self._discard_orphan_descendants_locked(key)
                        self._discard_provisional_descendants_locked(key)
                        accepting = False
                    elif app_capture is not None and not app_capture.is_active():
                        self._put_tombstone_locked(key)
                        self._discard_orphan_descendants_locked(key)
                        self._discard_provisional_descendants_locked(key)
                        accepting = False
                    elif accepting and (app_capture is not None or not self._app_gating_enabled):
                        self._put_association_locked(key, association)
                        if provisional_root is not None:
                            for loss_boundary in provisional_root.loss_boundaries:
                                self._mark_incomplete_locked(
                                    association.request_trace_id
                                    if loss_boundary is None
                                    else derive_request_trace_id(
                                        self._project_id,
                                        loss_boundary[0],
                                        loss_boundary[1],
                                    )
                                )
                            if provisional_root.loss_overflow:
                                self._orphan_loss_global = True
                        promoted_boundaries: dict[SpanKey, _Association] = {}
                        for provisional_span_key, provisional_span in tuple(
                            self._provisional_spans.items()
                        ):
                            if provisional_span.root_key != key:
                                continue
                            self._provisional_spans.pop(provisional_span_key, None)
                            if provisional_span.is_server:
                                promoted_association = self._server_association(
                                    provisional_span_key,
                                    app_capture=association.app_capture,
                                )
                                promoted_boundaries[provisional_span_key] = promoted_association
                            elif provisional_span.boundary_key is not None:
                                boundary_association = self._associations.get(
                                    provisional_span.boundary_key
                                ) or promoted_boundaries.get(provisional_span.boundary_key)
                                if boundary_association is None:
                                    boundary_association = self._server_association(
                                        provisional_span.boundary_key,
                                        app_capture=association.app_capture,
                                    )
                                    promoted_boundaries[provisional_span.boundary_key] = (
                                        boundary_association
                                    )
                                promoted_association = boundary_association
                            else:
                                promoted_association = association
                            self._put_association_locked(provisional_span_key, promoted_association)
                            for event in self._resolve_provisional_orphans_locked(
                                provisional_span_key, promoted_association
                            ):
                                if len(self._resolved_events) == self._resolved_events.maxlen:
                                    dropped = self._resolved_events.popleft().event
                                    self._orphan_capacity_drop_count += 1
                                    self._mark_incomplete_locked(dropped.request_trace_id)
                                self._resolved_events.append(
                                    _ResolvedEvent(event, promoted_association.app_capture)
                                )
                        for event in self._resolve_orphans_locked(key, association):
                            if len(self._resolved_events) == self._resolved_events.maxlen:
                                dropped = self._resolved_events.popleft().event
                                self._orphan_capacity_drop_count += 1
                                self._mark_incomplete_locked(dropped.request_trace_id)
                            self._resolved_events.append(
                                _ResolvedEvent(event, association.app_capture)
                            )
                        for event in self._resolve_provisional_orphans_locked(key, association):
                            if len(self._resolved_events) == self._resolved_events.maxlen:
                                dropped = self._resolved_events.popleft().event
                                self._orphan_capacity_drop_count += 1
                                self._mark_incomplete_locked(dropped.request_trace_id)
                            self._resolved_events.append(
                                _ResolvedEvent(event, association.app_capture)
                            )
                    else:
                        accepting = False
            finally:
                self._end_callback()
        if not accepting:
            yield None
            return
        request_context = _RequestContext(
            processor_marker=self._marker,
            request_trace_id=association.request_trace_id,
            otel_trace_id=key[0],
            root_span_id=key[1],
            app_capture=app_capture,
        )
        token: Token[_RequestContext | None] = _CURRENT_REQUEST.set(request_context)
        try:
            yield association.request_trace_id
        finally:
            _CURRENT_REQUEST.reset(token)

    @contextmanager
    def app_context(self, app_binding: FastAPIAppBinding) -> Iterator[_AppCapture]:
        """Gate all OTel callbacks created by one FastAPI request entry."""

        activation = app_binding.capture()
        app_capture = _AppCapture(
            processor_marker=self._marker,
            app_binding=app_binding,
            activation=activation,
        )
        token: Token[_AppCapture | None] = _CURRENT_APP_CAPTURE.set(app_capture)
        try:
            yield app_capture
        finally:
            _CURRENT_APP_CAPTURE.reset(token)

    def sweep_expired(self) -> None:
        """Expire orphan spans deterministically, primarily for the spike tests."""

        with self._lock:
            self._sweep_expired_locked(self._clock())

    def mark_delivery_failure(self, request_trace_ids: Iterable[str]) -> None:
        """Make asynchronous sender/storage failures visible on every request."""

        unique_ids: dict[str, None] = {}
        for request_trace_id in request_trace_ids:
            if type(request_trace_id) is not str or not request_trace_id:
                raise ValueError("request_trace_ids must contain non-empty built-in strings")
            unique_ids[request_trace_id] = None
        with self._lock:
            self._delivery_failure_count += len(unique_ids)
            for request_trace_id in unique_ids:
                self._mark_incomplete_locked(request_trace_id)

    def emit_enrichment(self, span: Span, payload: dict[str, object]) -> bool:
        """Send private safe enrichment keyed by span identity, never OTel attrs."""

        if not self._begin_callback():
            return False
        try:
            return self._emit_enrichment(span, payload)
        finally:
            self._end_callback()

    def _emit_enrichment(self, span: Span, payload: dict[str, object]) -> bool:
        span_context = span.get_span_context()
        if not span_context.is_valid:
            return False
        key = (span_context.trace_id, span_context.span_id)
        with self._lock:
            if not self._active:
                return False
            association = self._associations.get(key)
        if association is None or not self._association_is_active(association):
            return False
        summary = safe_summary(payload)
        event = TelemetryEvent(
            event_type="span.enrichment",
            request_trace_id=association.request_trace_id,
            otel_trace_id=f"{key[0]:032x}",
            span_id=f"{key[1]:016x}",
            parent_span_id=None,
            payload_json=summary.payload_json,
        )
        return self._emit(event)

    def shutdown(self) -> None:
        """Terminal hook owned by the provider, not by ``FlowSight.shutdown``."""

        with self._condition:
            self._terminal = True
        if self.deactivate(timeout=2.0):
            return
        with self._condition:
            self._active = False
            self._accepting = False
            self._enqueue = None
            self._flush = None
            self._active_app_capture = None
            self._provisional_root_drop_count += len(self._provisional_roots)
            for key in self._provisional_roots:
                self._put_tombstone_locked(key)
            self._provisional_roots.clear()
            for key in self._provisional_orphans:
                self._put_tombstone_locked(key)
            self._provisional_orphans.clear()
            self._provisional_orphans_by_parent.clear()
            self._provisional_spans.clear()
            for key, association in self._associations.items():
                self._mark_incomplete_locked(association.request_trace_id)
                self._put_tombstone_locked(key)
            self._associations.clear()
            for event in self._resolved_events:
                self._mark_incomplete_locked(event.event.request_trace_id)
            if self._orphans or self._orphan_loss_parents:
                self._orphan_loss_global = True
            self._orphans.clear()
            self._orphans_by_parent.clear()
            self._orphan_loss_parents.clear()
            self._resolved_events.clear()
            self._draining = False
            self._last_deactivate_result = False
            self._condition.notify_all()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        with self._lock:
            flush = self._flush if self._active else None
        if flush is None:
            return True
        try:
            return flush(max(0, timeout_millis) / 1000.0) is not False
        except Exception:
            with self._lock:
                self._callback_error_count += 1
            return False

    def abandon_failed_flush(self) -> bool:
        """Acknowledge externally proven delivery loss so a new session can start."""

        with self._condition:
            if self._active or self._draining or self._callbacks_in_flight:
                return False
            if self._last_deactivate_result:
                return True
            if self._flush is None:
                if not self._terminal:
                    return False
                self._last_deactivate_result = True
                self._delivery_failure_count += 1
                return True
            self._flush = None
            self._last_deactivate_result = True
            self._delivery_failure_count += 1
            return True

    def _begin_callback(self) -> bool:
        with self._condition:
            if not self._accepting:
                return False
            self._callbacks_in_flight += 1
            return True

    def _end_callback(self) -> None:
        with self._condition:
            self._callbacks_in_flight -= 1
            self._condition.notify_all()

    def _server_association(
        self, key: SpanKey, *, app_capture: _AppCapture | None = None
    ) -> _Association:
        return _Association(
            request_trace_id=derive_request_trace_id(self._project_id, key[0], key[1]),
            root_span_id=key[1],
            app_capture=app_capture,
        )

    def _current_app_capture(self) -> _AppCapture | None:
        capture = _CURRENT_APP_CAPTURE.get()
        if capture is None or capture.processor_marker is not self._marker:
            return None
        return capture

    @staticmethod
    def _association_is_active(association: _Association) -> bool:
        capture = association.app_capture
        return capture is None or capture.is_active()

    def _association_for_parent_locked(
        self, trace_id: int, parent_key: SpanKey | None
    ) -> _Association | None:
        if parent_key is not None:
            association = self._associations.get(parent_key)
            if association is not None:
                self._associations.move_to_end(parent_key)
                return association
        current = _CURRENT_REQUEST.get()
        if (
            current is not None
            and current.processor_marker is self._marker
            and current.otel_trace_id == trace_id
            and parent_key == (current.otel_trace_id, current.root_span_id)
        ):
            return _Association(
                request_trace_id=current.request_trace_id,
                root_span_id=current.root_span_id,
                app_capture=current.app_capture,
            )
        return None

    @staticmethod
    def _parent_key(span: SDKSpan, parent_context: Context | None) -> SpanKey | None:
        parent = span.parent
        if parent is None and parent_context is not None:
            parent = trace.get_current_span(parent_context).get_span_context()
        if parent is None or not parent.is_valid:
            return None
        return (parent.trace_id, parent.span_id)

    @staticmethod
    def _readable_parent_key(span: ReadableSpan) -> SpanKey | None:
        parent = span.parent
        if parent is None or not parent.is_valid:
            return None
        return (parent.trace_id, parent.span_id)

    def _put_association_locked(self, key: SpanKey, association: _Association) -> None:
        self._apply_orphan_loss_locked(key, association)
        if key in self._associations:
            self._associations[key] = association
            self._associations.move_to_end(key)
            return
        self._associations[key] = association
        if len(self._associations) > self._association_capacity:
            _, evicted = self._associations.popitem(last=False)
            self._association_eviction_count += 1
            self._mark_incomplete_locked(evicted.request_trace_id)

    def _put_tombstone_locked(self, key: SpanKey) -> None:
        self._tombstones[key] = None
        self._tombstones.move_to_end(key)
        while len(self._tombstones) > self._association_capacity:
            self._tombstones.popitem(last=False)
            self._ignored_span_overflow_count += 1

    def _put_provisional_root_locked(self, key: SpanKey, capture: _AppCapture) -> None:
        self._provisional_roots[key] = _ProvisionalRoot(capture)
        self._provisional_roots.move_to_end(key)
        while len(self._provisional_roots) > self._association_capacity:
            evicted_key, _ = self._provisional_roots.popitem(last=False)
            self._put_tombstone_locked(evicted_key)
            self._discard_provisional_descendants_locked(evicted_key)
            self._provisional_root_overflow_count += 1
            self._provisional_root_drop_count += 1
            self._admission_uncertain = True

    def _put_provisional_span_locked(
        self, span_key: SpanKey, provisional_span: _ProvisionalSpan
    ) -> None:
        self._provisional_spans[span_key] = provisional_span
        self._provisional_spans.move_to_end(span_key)
        while len(self._provisional_spans) > self._orphan_capacity:
            evicted_key, evicted_span = self._provisional_spans.popitem(last=False)
            self._put_tombstone_locked(evicted_key)
            self._mark_provisional_child_loss_locked(
                evicted_span.root_key, evicted_span.boundary_key
            )
            self._provisional_span_overflow_count += 1

    def _buffer_provisional_orphan_locked(self, pending: _PendingSpan) -> None:
        if pending.key in self._provisional_orphans:
            self._remove_provisional_orphan_locked(pending.key)
        if len(self._provisional_orphans) >= self._orphan_capacity:
            oldest_key = next(iter(self._provisional_orphans))
            dropped = self._remove_provisional_orphan_locked(oldest_key)
            if dropped is not None:
                self._put_tombstone_locked(dropped.key)
                self._mark_provisional_child_loss_locked(
                    dropped.provisional_root_key,
                    dropped.provisional_boundary_key,
                )
            self._provisional_orphan_overflow_count += 1
        self._provisional_orphans[pending.key] = pending
        if pending.parent_key is not None:
            self._provisional_orphans_by_parent.setdefault(pending.parent_key, set()).add(
                pending.key
            )

    def _resolve_provisional_orphans_locked(
        self, parent_key: SpanKey, association: _Association
    ) -> list[TelemetryEvent]:
        resolved: list[TelemetryEvent] = []
        pending_to_resolve = [
            (pending_key, association)
            for pending_key in reversed(
                tuple(self._provisional_orphans_by_parent.get(parent_key, ()))
            )
        ]
        while pending_to_resolve:
            pending_key, parent_association = pending_to_resolve.pop()
            pending = self._remove_provisional_orphan_locked(pending_key)
            if pending is None:
                continue
            pending_association = (
                self._server_association(
                    pending.key,
                    app_capture=parent_association.app_capture,
                )
                if pending.is_provisional_server
                else parent_association
            )
            resolved.append(self._with_request(pending.event, pending_association.request_trace_id))
            pending_to_resolve.extend(
                (child_key, pending_association)
                for child_key in reversed(
                    tuple(self._provisional_orphans_by_parent.get(pending.key, ()))
                )
            )
        return resolved

    def _discard_provisional_descendants_locked(self, parent_key: SpanKey) -> None:
        for span_key, provisional_span in tuple(self._provisional_spans.items()):
            if provisional_span.root_key == parent_key:
                self._provisional_spans.pop(span_key, None)
                self._put_tombstone_locked(span_key)
        for pending_key, pending in tuple(self._provisional_orphans.items()):
            if pending.provisional_root_key == parent_key:
                self._remove_provisional_orphan_locked(pending_key)
                self._put_tombstone_locked(pending.key)

    def _mark_provisional_child_loss_locked(
        self, root_key: SpanKey | None, boundary_key: SpanKey | None
    ) -> None:
        if root_key is None:
            return
        root = self._provisional_roots.get(root_key)
        if root is not None:
            if len(root.loss_boundaries) >= self._orphan_capacity:
                root.loss_overflow = True
            else:
                root.loss_boundaries.add(boundary_key)

    def _remove_provisional_orphan_locked(self, key: SpanKey) -> _PendingSpan | None:
        pending = self._provisional_orphans.pop(key, None)
        if pending is None or pending.parent_key is None:
            return pending
        children = self._provisional_orphans_by_parent.get(pending.parent_key)
        if children is not None:
            children.discard(key)
            if not children:
                self._provisional_orphans_by_parent.pop(pending.parent_key, None)
        return pending

    @staticmethod
    def _same_capture(left: _AppCapture | None, right: _AppCapture | None) -> bool:
        return (
            left is not None
            and right is not None
            and left.app_binding is right.app_binding
            and left.activation is right.activation
        )

    @staticmethod
    def _capture_matches(
        capture: _AppCapture | None,
        app_binding: FastAPIAppBinding,
        activation: object,
    ) -> bool:
        return (
            capture is not None
            and capture.app_binding is app_binding
            and capture.activation is activation
        )

    def _discard_orphan_descendants_locked(self, parent_key: SpanKey) -> None:
        parents_to_discard = [parent_key]
        while parents_to_discard:
            current_parent = parents_to_discard.pop()
            for pending_key in tuple(self._orphans_by_parent.get(current_parent, ())):
                pending = self._remove_orphan_locked(pending_key)
                if pending is None:
                    continue
                self._put_tombstone_locked(pending.key)
                parents_to_discard.append(pending.key)

    def _apply_orphan_loss_locked(self, key: SpanKey, association: _Association) -> None:
        """Attribute a retained loss marker without consuming active-map capacity."""

        if key in self._orphan_loss_parents:
            self._orphan_loss_parents.pop(key, None)
            self._mark_incomplete_locked(association.request_trace_id)

    def _buffer_orphan_locked(self, pending: _PendingSpan) -> None:
        if pending.key in self._orphans:
            self._remove_orphan_locked(pending.key)
        if len(self._orphans) >= self._orphan_capacity:
            oldest_key = next(iter(self._orphans))
            dropped = self._remove_orphan_locked(oldest_key)
            self._orphan_capacity_drop_count += 1
            if dropped is None or dropped.parent_key is None:
                self._unscoped_span_count += 1
            else:
                if len(self._orphan_loss_parents) >= self._association_capacity:
                    self._orphan_loss_parents.popitem(last=False)
                    self._orphan_loss_global = True
                    self._unscoped_span_count += 1
                self._orphan_loss_parents[dropped.parent_key] = None
        self._orphans[pending.key] = pending
        if pending.parent_key is not None:
            self._orphans_by_parent.setdefault(pending.parent_key, set()).add(pending.key)

    def _resolve_orphans_locked(
        self, parent_key: SpanKey, association: _Association
    ) -> list[TelemetryEvent]:
        resolved: list[TelemetryEvent] = []
        parents_to_resolve = [parent_key]
        while parents_to_resolve:
            current_parent = parents_to_resolve.pop()
            pending_keys = list(self._orphans_by_parent.get(current_parent, ()))
            for pending_key in pending_keys:
                pending = self._remove_orphan_locked(pending_key)
                if pending is None:
                    continue
                self._apply_orphan_loss_locked(pending.key, association)
                resolved.append(self._with_request(pending.event, association.request_trace_id))
                parents_to_resolve.append(pending.key)
        return resolved

    def _remove_orphan_locked(self, key: SpanKey) -> _PendingSpan | None:
        pending = self._orphans.pop(key, None)
        if pending is None or pending.parent_key is None:
            return pending
        children = self._orphans_by_parent.get(pending.parent_key)
        if children is not None:
            children.discard(key)
            if not children:
                self._orphans_by_parent.pop(pending.parent_key, None)
        return pending

    def _sweep_expired_locked(self, now: float) -> None:
        expired = [key for key, pending in self._orphans.items() if pending.expires_at <= now]
        for key in expired:
            self._remove_orphan_locked(key)
            self._unscoped_span_count += 1
        expired_provisional = [
            key for key, pending in self._provisional_orphans.items() if pending.expires_at <= now
        ]
        for key in expired_provisional:
            expired_pending = self._remove_provisional_orphan_locked(key)
            if expired_pending is not None:
                self._mark_provisional_child_loss_locked(
                    expired_pending.provisional_root_key,
                    expired_pending.provisional_boundary_key,
                )
            self._put_tombstone_locked(key)

    def _mark_incomplete_locked(self, request_trace_id: str) -> None:
        self._incomplete[request_trace_id] = None
        self._incomplete.move_to_end(request_trace_id)
        while len(self._incomplete) > self._association_capacity:
            self._incomplete.popitem(last=False)
            self._incomplete_request_overflow_count += 1
            self._orphan_loss_global = True

    def _emit_many(self, events: list[TelemetryEvent]) -> None:
        for event in events:
            self._emit(event)

    def _emit(self, event: TelemetryEvent) -> bool:
        with self._lock:
            enqueue = self._enqueue if self._active else None
        if enqueue is None:
            return False
        try:
            accepted = enqueue(event) is not False
        except Exception:
            with self._lock:
                self._callback_error_count += 1
                self._queue_drop_count += 1
                self._mark_incomplete_locked(event.request_trace_id)
            return False
        with self._lock:
            if accepted:
                self._emitted_event_count += 1
                self._event_snapshots.append(event)
            else:
                self._queue_drop_count += 1
                self._mark_incomplete_locked(event.request_trace_id)
        return accepted

    @staticmethod
    def _with_request(event: TelemetryEvent, request_trace_id: str) -> TelemetryEvent:
        return TelemetryEvent(
            event_type=event.event_type,
            request_trace_id=request_trace_id,
            otel_trace_id=event.otel_trace_id,
            span_id=event.span_id,
            parent_span_id=event.parent_span_id,
            payload_json=event.payload_json,
        )

    @staticmethod
    def _safe_span_event(span: ReadableSpan, *, request_trace_id: str) -> TelemetryEvent:
        context = cast(trace.SpanContext, span.context)
        parent = span.parent
        status = span.status
        summary = safe_summary(
            {
                "name": span.name,
                "kind": span.kind.name,
                "start_ns": span.start_time,
                "end_ns": span.end_time,
                # Status descriptions may contain raw exception text.  The
                # private path intentionally persists only the stable code;
                # explicit safe exception enrichment is a separate event.
                "status": {"code": status.status_code.name},
            }
        )
        return TelemetryEvent(
            event_type="span.ended",
            request_trace_id=request_trace_id,
            otel_trace_id=f"{context.trace_id:032x}",
            span_id=f"{context.span_id:016x}",
            parent_span_id=f"{parent.span_id:016x}" if parent is not None else None,
            payload_json=summary.payload_json,
        )


@dataclass(slots=True, weakref_slot=True)
class TelemetryBinding:
    """Live handle for one provider's single permanently registered processor."""

    provider: SDKTracerProvider
    processor: FlowSightSpanProcessor
    project_id: str
    owns_provider: bool
    registration_count: int
    sampler_description: str
    warning: str | None

    def snapshot(self) -> ProviderBindingSnapshot:
        return ProviderBindingSnapshot(
            provider_identity=id(self.provider),
            processor_identity=id(self.processor),
            owns_provider=self.owns_provider,
            registration_count=self.registration_count,
            sampler_description=self.sampler_description,
            warning=self.warning,
            active=self.processor.health_snapshot().active,
        )

    def deactivate(self, timeout: float = 2.0) -> bool:
        return self.processor.deactivate(timeout)

    def abandon_failed_flush(self) -> bool:
        return self.processor.abandon_failed_flush()


@dataclass(frozen=True, slots=True)
class _ProviderRegistration:
    """Provider cache value that deliberately does not retain its weak key."""

    processor: FlowSightSpanProcessor
    project_id: str
    owns_provider: bool
    registration_count: int
    sampler_description: str
    warning: str | None

    def binding(self, provider: SDKTracerProvider) -> TelemetryBinding:
        return TelemetryBinding(
            provider=provider,
            processor=self.processor,
            project_id=self.project_id,
            owns_provider=self.owns_provider,
            registration_count=self.registration_count,
            sampler_description=self.sampler_description,
            warning=self.warning,
        )


class ProviderRegistry:
    """Process-local single-provider registry used to prove add-once semantics."""

    _provider_lock = threading.Lock()
    _provider_registrations: WeakKeyDictionary[SDKTracerProvider, _ProviderRegistration] = (
        WeakKeyDictionary()
    )
    _provider_failures: WeakKeyDictionary[SDKTracerProvider, str] = WeakKeyDictionary()

    def __init__(self) -> None:
        self._binding: TelemetryBinding | None = None

    def bind(
        self,
        provider: SDKTracerProvider,
        enqueue: Enqueue,
        *,
        owns_provider: bool = False,
        flush: Flush | None = None,
        project_id: str = "flowsight-spike",
        clock: Clock = monotonic,
        association_capacity: int = 4096,
        orphan_capacity: int = 1024,
        orphan_ttl: float = 1.0,
        event_snapshot_capacity: int = 1024,
        app_gating: bool = False,
    ) -> TelemetryBinding:
        if not isinstance(provider, SDKTracerProvider):
            raise UnsupportedTracerProviderError(
                "FlowSight requires opentelemetry.sdk.trace.TracerProvider"
            )
        with self._provider_lock:
            if provider in self._provider_failures:
                raise UnsupportedTracerProviderError(
                    "prior span-processor registration outcome is unsafe to retry"
                )
            binding = self._binding
            if binding is not None:
                if binding.provider is not provider:
                    raise UnsupportedTracerProviderError(
                        "the process tracer provider changed after FlowSight initialization"
                    )
                if binding.project_id != project_id:
                    raise UnsupportedTracerProviderError(
                        "one tracer provider cannot serve multiple FlowSight projects"
                    )
                if app_gating:
                    binding.processor.enable_app_gating()
                binding.processor.activate(enqueue, flush=flush)
                return binding

            registration = self._provider_registrations.get(provider)
            if registration is not None:
                if registration.project_id != project_id:
                    raise UnsupportedTracerProviderError(
                        "one tracer provider cannot serve multiple FlowSight projects"
                    )
                if app_gating:
                    registration.processor.enable_app_gating()
                registration.processor.activate(enqueue, flush=flush)
                binding = registration.binding(provider)
                self._binding = binding
                return binding

            processor = FlowSightSpanProcessor(
                project_id=project_id,
                clock=clock,
                association_capacity=association_capacity,
                orphan_capacity=orphan_capacity,
                orphan_ttl=orphan_ttl,
                event_snapshot_capacity=event_snapshot_capacity,
            )
            if app_gating:
                processor.enable_app_gating()
            try:
                provider.add_span_processor(processor)
                sampler_description, warning = _sampler_status(provider, owns_provider)
                registration = _ProviderRegistration(
                    processor=processor,
                    project_id=project_id,
                    owns_provider=owns_provider,
                    registration_count=1,
                    sampler_description=sampler_description,
                    warning=warning,
                )
                self._provider_registrations[provider] = registration
                processor.activate(enqueue, flush=flush)
                binding = registration.binding(provider)
                self._binding = binding
                return binding
            except BaseException:
                self._binding = None
                self._provider_registrations.pop(provider, None)
                self._provider_failures[provider] = "SPAN_PROCESSOR_REGISTRATION_UNCERTAIN"
                try:
                    processor.shutdown()
                except BaseException:
                    pass
                raise

    def snapshot(self) -> ProviderBindingSnapshot | None:
        with self._provider_lock:
            binding = self._binding
        return None if binding is None else binding.snapshot()

    def release_binding(self, provider: SDKTracerProvider) -> None:
        """Drop this registry's strong handle after its shared session closes."""

        with self._provider_lock:
            if self._binding is not None and self._binding.provider is provider:
                self._binding = None


def _sampler_status(provider: SDKTracerProvider, owns_provider: bool) -> tuple[str, str | None]:
    sampler = provider.sampler
    if owns_provider or sampler is ALWAYS_ON:
        return "AlwaysOnSampler", None
    if sampler is ALWAYS_OFF:
        return (
            "AlwaysOffSampler",
            "the existing AlwaysOff sampler makes request spans non-recording",
        )
    return (
        type(sampler).__name__,
        "FlowSight respects the existing sampler; non-recording requests are invisible",
    )


_DEFAULT_PROVIDER_REGISTRY = ProviderRegistry()


def install_global_telemetry(
    enqueue: Enqueue,
    *,
    flush: Flush | None = None,
    registry: ProviderRegistry = _DEFAULT_PROVIDER_REGISTRY,
    project_id: str = "flowsight-spike",
    clock: Clock = monotonic,
    association_capacity: int = 4096,
    orphan_capacity: int = 1024,
    orphan_ttl: float = 1.0,
    event_snapshot_capacity: int = 1024,
) -> TelemetryBinding:
    """Attach to an existing SDK provider or atomically install an owned one."""

    current = trace.get_tracer_provider()
    owns_provider = False
    if isinstance(current, SDKTracerProvider):
        provider = current
    else:
        candidate = SDKTracerProvider(sampler=ALWAYS_ON)
        trace.set_tracer_provider(candidate)
        resolved = trace.get_tracer_provider()
        if resolved is candidate:
            provider = candidate
            owns_provider = True
        else:
            candidate.shutdown()
            if not isinstance(resolved, SDKTracerProvider):
                raise UnsupportedTracerProviderError(
                    "the configured tracer provider cannot accept span processors"
                )
            provider = resolved

    return registry.bind(
        provider,
        enqueue,
        owns_provider=owns_provider,
        flush=flush,
        project_id=project_id,
        clock=clock,
        association_capacity=association_capacity,
        orphan_capacity=orphan_capacity,
        orphan_ttl=orphan_ttl,
        event_snapshot_capacity=event_snapshot_capacity,
    )


class _RequestContextMiddleware:
    def __init__(
        self,
        app: object,
        processor: FlowSightSpanProcessor,
        app_binding: FastAPIAppBinding,
    ) -> None:
        self.app = cast(Callable[..., object], app)
        self.processor = processor
        self.app_binding = app_binding

    async def __call__(
        self,
        scope: dict[str, object],
        receive: Callable[..., object],
        send: Callable[..., object],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)  # type: ignore[misc]
            return
        # The pinned OTel middleware wraps this user-middleware layer, so the
        # SERVER span starts before this context. request_context positively
        # admits that provisional root only after this app/session is known.
        with self.processor.app_context(self.app_binding) as app_capture:
            current_span = trace.get_current_span()
            with self.processor.request_context(current_span, app_capture=app_capture):
                await self.app(scope, receive, send)  # type: ignore[misc]


_APP_INSTRUMENTATION_LOCK = threading.Lock()
_APP_INSTRUMENTATION: WeakKeyDictionary[
    FastAPI, tuple[object, FlowSightSpanProcessor, FastAPIAppBinding]
] = WeakKeyDictionary()


def instrument_fastapi_app(
    app: FastAPI,
    *,
    tracer_provider: trace.TracerProvider,
    processor: FlowSightSpanProcessor,
    activate: bool = True,
) -> FastAPIInstrumentationResult:
    """Instrument one app once while allowing an explicitly supplied provider."""

    with _APP_INSTRUMENTATION_LOCK:
        processor.enable_app_gating()
        existing = _APP_INSTRUMENTATION.get(app)
        if existing is not None:
            existing_provider, existing_processor, app_binding = existing
            if existing_provider is not tracer_provider or existing_processor is not processor:
                raise RuntimeError("FastAPI app was already bound to a different telemetry owner")
            if activate:
                processor.activate_app_binding(app_binding)
            return FastAPIInstrumentationResult(
                newly_registered=False,
                provider_identity=id(tracer_provider),
                processor_identity=id(processor),
                app_binding=app_binding,
            )

        app_binding = FastAPIAppBinding()
        try:
            FastAPIInstrumentor.instrument_app(
                app,
                tracer_provider=tracer_provider,
                exclude_spans=["receive", "send"],
            )
            app.add_middleware(
                _RequestContextMiddleware,
                processor=processor,
                app_binding=app_binding,
            )
        except BaseException:
            processor.deactivate_app_binding(app_binding)
            raise
        _APP_INSTRUMENTATION[app] = (tracer_provider, processor, app_binding)
        if activate:
            processor.activate_app_binding(app_binding)
        return FastAPIInstrumentationResult(
            newly_registered=True,
            provider_identity=id(tracer_provider),
            processor_identity=id(processor),
            app_binding=app_binding,
        )


class FunctionSpanScope:
    """Private enrichment state for one explicitly traced function call."""

    __slots__ = ("_return_summary_json", "span")

    def __init__(self, span: Span) -> None:
        self.span = span
        self._return_summary_json: str | None = None

    def set_return(self, value: object) -> None:
        """Convert the return value immediately; retain no raw object."""

        self._return_summary_json = safe_summary({"return": value}).payload_json


@contextmanager
def private_function_span(
    tracer: Tracer,
    processor: FlowSightSpanProcessor,
    name: str,
    *,
    args: dict[str, object] | None = None,
) -> Iterator[FunctionSpanScope]:
    """Create a minimal public child span and private safe enrichment event."""

    args_summary_json = safe_summary({"args": args if args is not None else {}}).payload_json
    with tracer.start_as_current_span(
        name,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        scope = FunctionSpanScope(span)
        try:
            yield scope
        except BaseException as error:
            span.set_status(Status(StatusCode.ERROR))
            exception_summary_json = safe_summary({"exception": error}).payload_json
            processor.emit_enrichment(
                span,
                {
                    "args_summary": args_summary_json,
                    "exception_summary": exception_summary_json,
                },
            )
            raise
        else:
            processor.emit_enrichment(
                span,
                {
                    "args_summary": args_summary_json,
                    "return_summary": scope._return_summary_json,
                },
            )
