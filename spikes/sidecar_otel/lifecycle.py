"""Coordinated public-lifecycle shape for the TRIAL-004 spike only."""

from __future__ import annotations

import math
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from weakref import WeakKeyDictionary

from fastapi import FastAPI
from opentelemetry.sdk.trace import TracerProvider

from .runtime import (
    RuntimeConfig,
    SidecarHandle,
    goodbye,
    hello,
    probe_health,
    renew,
)
from .runtime import (
    flush as flush_sidecar,
)
from .state import StateStore
from .telemetry import (
    Enqueue,
    FastAPIInstrumentationResult,
    Flush,
    ProviderRegistry,
    TelemetryBinding,
    instrument_fastapi_app,
)


@dataclass(frozen=True, slots=True)
class LifecycleInitialization:
    """One immutable observation returned by the spike ``init_app`` path."""

    sidecar: SidecarHandle
    telemetry: TelemetryBinding
    instrumentation: FastAPIInstrumentationResult
    producer_id: str
    lease_id: str


@dataclass(frozen=True, slots=True)
class LifecycleHealth:
    """Safe lifecycle health without provider objects or capability tokens."""

    active: bool
    producer_id: str | None
    heartbeat_renewal_count: int
    heartbeat_error_count: int
    heartbeat_last_error_code: str | None
    shutdown_failure_count: int
    shutdown_last_error_code: str | None


@dataclass(frozen=True, slots=True)
class _HeartbeatHealth:
    renewal_count: int
    error_count: int
    last_error_code: str | None


class _AcquirableLock(Protocol):
    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool: ...

    def release(self) -> None: ...


class _BoundedLock:
    def __init__(self, lock: _AcquirableLock, timeout: float) -> None:
        self._lock = lock
        self._timeout = timeout
        self._acquired = False

    def __enter__(self) -> bool:
        self._acquired = self._lock.acquire(timeout=self._timeout)
        return self._acquired

    def __exit__(self, *_error: object) -> None:
        if self._acquired:
            self._lock.release()


class _LeaseHeartbeat:
    """Renew one producer lease without retaining arbitrary error objects."""

    def __init__(
        self,
        sidecar: SidecarHandle,
        producer_id: str,
        lease_id: str,
        *,
        lease_ttl: float,
        request_timeout: float,
    ) -> None:
        self._sidecar = sidecar
        self._producer_id = producer_id
        self._lease_id = lease_id
        self._lease_ttl = lease_ttl
        self._request_timeout = request_timeout
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._renewal_count = 0
        self._error_count = 0
        self._last_error_code: str | None = None
        self._started = False
        self._thread = threading.Thread(
            target=self._run,
            name="flowsight-spike-lifecycle-heartbeat",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()
        with self._lock:
            self._started = True

    def stop(self, timeout: float) -> bool:
        self._stop.set()
        with self._lock:
            started = self._started
        if not started:
            return True
        self._thread.join(timeout=max(0.0, timeout))
        return not self._thread.is_alive()

    def snapshot(self) -> _HeartbeatHealth:
        with self._lock:
            return _HeartbeatHealth(
                renewal_count=self._renewal_count,
                error_count=self._error_count,
                last_error_code=self._last_error_code,
            )

    def _run(self) -> None:
        interval = max(0.05, self._lease_ttl / 3)
        renewal_timeout = min(self._request_timeout, max(0.05, interval / 2))
        while not self._stop.is_set():
            try:
                renew(
                    self._sidecar.state,
                    self._producer_id,
                    self._lease_id,
                    timeout=renewal_timeout,
                )
            except Exception as error:
                self._record_error(type(error).__name__)
                if self._stop.wait(min(0.01, interval / 10)):
                    return
                continue
            with self._lock:
                self._renewal_count += 1
                self._last_error_code = None
            if self._stop.wait(interval):
                return

    def _record_error(self, code: str) -> None:
        with self._lock:
            self._error_count += 1
            self._last_error_code = code


@dataclass(slots=True)
class _ActiveLifecycle:
    generation: int
    initialization: LifecycleInitialization
    heartbeat: _LeaseHeartbeat
    app: FastAPI
    telemetry_drained: bool = False
    sidecar_flushed: bool = False
    heartbeat_stopped: bool = False
    lease_released: bool = False
    shutdown_started: bool = False
    telemetry_failure_count: int = 0
    sidecar_flush_failure_count: int = 0
    lease_release_failure_count: int = 0
    loss_code: str | None = None
    heartbeat_archived: bool = False
    registry_binding_released: bool = False
    loss_reported: bool = False


@dataclass(slots=True)
class _PendingCleanup:
    generation: int
    sidecar: SidecarHandle
    telemetry: TelemetryBinding | None
    producer_id: str
    lease_id: str
    heartbeat: _LeaseHeartbeat
    telemetry_drained: bool = False
    sidecar_flushed: bool = False
    heartbeat_stopped: bool = False
    lease_released: bool = False
    telemetry_failure_count: int = 0
    sidecar_flush_failure_count: int = 0
    lease_release_failure_count: int = 0
    loss_code: str | None = None
    heartbeat_archived: bool = False
    registry_binding_released: bool = False
    loss_reported: bool = False


class _SharedLifecycle:
    """One process/provider/project coordinator shared by SDK instances."""

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.registry = ProviderRegistry()
        self.lock = threading.RLock()
        self.active: _ActiveLifecycle | None = None
        self.pending_cleanup: _PendingCleanup | None = None
        self.reference_count = 0
        self.delivery_enqueue: Enqueue | None = None
        self.delivery_flush: Flush | None = None
        self.shutdown_failure_count = 0
        self.shutdown_last_error_code: str | None = None
        self.heartbeat_renewal_count = 0
        self.heartbeat_error_count = 0
        self.heartbeat_last_error_code: str | None = None
        self.next_generation = 1

    def allocate_generation(self) -> int:
        generation = self.next_generation
        self.next_generation += 1
        return generation


_SHARED_LOCK = threading.Lock()
_SHARED_BY_PROVIDER: WeakKeyDictionary[TracerProvider, dict[tuple[Path, str], _SharedLifecycle]] = (
    WeakKeyDictionary()
)


def _shared_lifecycle(config: RuntimeConfig, provider: TracerProvider) -> _SharedLifecycle:
    key = (Path(config.runtime_dir).absolute(), config.project_id)
    with _SHARED_LOCK:
        by_project = _SHARED_BY_PROVIDER.setdefault(provider, {})
        shared = by_project.get(key)
        if shared is None:
            if by_project:
                raise ValueError("one OTel provider can serve only one FlowSight project")
            shared = _SharedLifecycle(config)
            by_project[key] = shared
        elif shared.config != config:
            raise ValueError("one project/provider cannot use conflicting runtime configuration")
        return shared


class FlowSightLifecycle:
    """Coordinate the process and OTel primitives without becoming product code.

    The class exists only to prove that the independently tested primitives can
    support the public ``init_app`` / ``shutdown`` lifecycle. Production Phase
    0/1 work must promote the contract rather than importing this spike module.
    Instances for one process/provider/project share the first coordinator-owned
    delivery target until the last joined instance shuts down.
    """

    def __init__(self, config: RuntimeConfig, provider: TracerProvider) -> None:
        if type(config) is not RuntimeConfig:
            raise TypeError("config must be an exact RuntimeConfig")
        if not isinstance(provider, TracerProvider):
            raise TypeError("provider must be an SDK TracerProvider")
        self._provider = provider
        self._shared = _shared_lifecycle(config, provider)
        self._generation: int | None = None

    def init_app(
        self,
        app: FastAPI,
        enqueue: Enqueue,
        *,
        flush: Flush | None = None,
    ) -> LifecycleInitialization:
        """Idempotently attach one app to the sidecar and provider primitives."""

        if not isinstance(app, FastAPI):
            raise TypeError("FlowSightLifecycle.init_app() requires FastAPI")
        with self._shared.lock:
            if self._shared.pending_cleanup is not None:
                raise RuntimeError("failed initialization cleanup must complete before init_app")
            if self._shared.active is not None:
                if self._shared.active.shutdown_started:
                    raise RuntimeError("lifecycle shutdown must complete before init_app")
                if self._shared.active.app is not app:
                    raise RuntimeError("one shared lifecycle can bind only one FastAPI app")
                shared_enqueue = self._shared.delivery_enqueue
                if shared_enqueue is None:
                    raise RuntimeError("active lifecycle has no shared delivery target")
                active_binding = self._shared.registry.bind(
                    self._provider,
                    shared_enqueue,
                    flush=self._shared.delivery_flush,
                    project_id=self._shared.config.project_id,
                    app_gating=True,
                )
                instrumentation = instrument_fastapi_app(
                    app,
                    tracer_provider=self._provider,
                    processor=active_binding.processor,
                    activate=False,
                )
                current = self._shared.active.initialization
                duplicate = LifecycleInitialization(
                    sidecar=current.sidecar,
                    telemetry=active_binding,
                    instrumentation=instrumentation,
                    producer_id=current.producer_id,
                    lease_id=current.lease_id,
                )
                previous_reference_count = self._shared.reference_count
                previous_generation = self._generation
                try:
                    self._shared.active.initialization = duplicate
                    if self._generation != self._shared.active.generation:
                        self._shared.reference_count += 1
                        self._generation = self._shared.active.generation
                    return duplicate
                except BaseException:
                    self._shared.active.initialization = current
                    self._shared.reference_count = previous_reference_count
                    self._generation = previous_generation
                    raise

            sidecar = self._ensure_sidecar()
            producer_id = f"lifecycle-{os.getpid()}-{uuid.uuid4().hex}"
            lease_id: str | None = None
            binding: TelemetryBinding | None = None
            heartbeat: _LeaseHeartbeat | None = None
            new_instrumentation: FastAPIInstrumentationResult | None = None
            try:
                try:
                    lease_id = hello(
                        sidecar.state,
                        producer_id,
                        wait_timeout_ms=0,
                        timeout=self._shared.config.startup_timeout,
                    )
                except Exception:
                    lease_id = self._observe_owned_lease(sidecar, producer_id)
                    if lease_id is None:
                        raise
                heartbeat = _LeaseHeartbeat(
                    sidecar,
                    producer_id,
                    lease_id,
                    lease_ttl=self._shared.config.lease_ttl,
                    request_timeout=self._shared.config.request_timeout,
                )
                heartbeat.start()
                binding = self._shared.registry.bind(
                    self._provider,
                    enqueue,
                    flush=flush,
                    project_id=self._shared.config.project_id,
                    app_gating=True,
                )
                new_instrumentation = instrument_fastapi_app(
                    app,
                    tracer_provider=self._provider,
                    processor=binding.processor,
                    activate=False,
                )
                if not binding.processor.health_snapshot().active:
                    raise RuntimeError("telemetry provider became unavailable during init_app")
                try:
                    renew(
                        sidecar.state,
                        producer_id,
                        lease_id,
                        timeout=self._shared.config.request_timeout,
                    )
                except Exception:
                    if self._observe_owned_lease(sidecar, producer_id) != lease_id:
                        raise
                if not binding.processor.health_snapshot().active:
                    raise RuntimeError("telemetry provider became unavailable during init_app")
                binding.processor.activate_app_binding(new_instrumentation.app_binding)
                initialization = LifecycleInitialization(
                    sidecar=sidecar,
                    telemetry=binding,
                    instrumentation=new_instrumentation,
                    producer_id=producer_id,
                    lease_id=lease_id,
                )
                assert heartbeat is not None
                generation = self._shared.allocate_generation()
                self._shared.active = _ActiveLifecycle(generation, initialization, heartbeat, app)
                self._shared.reference_count = 1
                self._shared.delivery_enqueue = enqueue
                self._shared.delivery_flush = flush
                self._generation = generation
                return initialization
            except BaseException as initialization_error:
                failures: list[BaseException] = [initialization_error]
                self._shared.active = None
                self._shared.reference_count = 0
                self._shared.delivery_enqueue = None
                self._shared.delivery_flush = None
                self._generation = None
                if new_instrumentation is not None:
                    assert binding is not None
                    binding.processor.deactivate_app_binding(new_instrumentation.app_binding)
                if lease_id is not None:
                    if heartbeat is None:
                        heartbeat = _LeaseHeartbeat(
                            sidecar,
                            producer_id,
                            lease_id,
                            lease_ttl=self._shared.config.lease_ttl,
                            request_timeout=self._shared.config.request_timeout,
                        )
                    pending = _PendingCleanup(
                        generation=self._shared.allocate_generation(),
                        sidecar=sidecar,
                        telemetry=binding,
                        producer_id=producer_id,
                        lease_id=lease_id,
                        heartbeat=heartbeat,
                        telemetry_drained=binding is None,
                    )
                    self._shared.pending_cleanup = pending
                    self._shared.reference_count = 1
                    self._shared.delivery_enqueue = enqueue
                    self._shared.delivery_flush = flush
                    self._generation = pending.generation
                    if not self._cleanup_pending_locked(
                        pending,
                        time.monotonic() + min(1.0, self._shared.config.startup_timeout),
                    ):
                        failures.append(RuntimeError("initialization cleanup remains pending"))
                if len(failures) > 1:
                    raise BaseExceptionGroup(
                        "FlowSight spike initialization failed", failures
                    ) from None
                raise

    def shutdown(self, timeout: float = 2.0) -> bool:
        """Drain and release this producer; never stop the shared sidecar/provider."""

        if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be a finite non-negative built-in number")
        deadline = time.monotonic() + timeout
        with _BoundedLock(self._shared.lock, self._remaining(deadline)) as acquired:
            if not acquired:
                self._record_shutdown_failure("COORDINATOR_LOCK_TIMEOUT")
                return False
            pending = self._shared.pending_cleanup
            if pending is not None:
                if self._generation not in {None, pending.generation}:
                    self._generation = None
                    return True
                self._generation = pending.generation
                return self._cleanup_pending_locked(pending, deadline)
            active = self._shared.active
            if active is None:
                self._generation = None
                return True
            if self._generation != active.generation:
                if self._generation is not None or not active.shutdown_started:
                    self._generation = None
                    return True
                self._generation = active.generation
            if self._shared.reference_count > 1:
                previous_reference_count = self._shared.reference_count
                previous_generation = self._generation
                try:
                    self._shared.reference_count -= 1
                    self._generation = None
                    return True
                except BaseException:
                    self._shared.reference_count = previous_reference_count
                    self._generation = previous_generation
                    raise
            active.shutdown_started = True
            initialization = active.initialization
            initialization.telemetry.processor.deactivate_app_binding(
                initialization.instrumentation.app_binding
            )
            if not active.telemetry_drained:
                if not initialization.telemetry.deactivate(self._remaining(deadline)):
                    active.telemetry_failure_count += 1
                    if (
                        active.telemetry_failure_count < 2
                        or not initialization.telemetry.abandon_failed_flush()
                    ):
                        self._record_shutdown_failure("TELEMETRY_DRAIN_TIMEOUT")
                        return False
                    active.loss_code = "TELEMETRY_FLUSH_ABANDONED"
                active.telemetry_drained = True
            if not active.sidecar_flushed:
                try:
                    flush_sidecar(
                        initialization.sidecar.state,
                        initialization.producer_id,
                        initialization.lease_id,
                        timeout=self._remaining(deadline),
                    )
                except Exception:
                    active.sidecar_flush_failure_count += 1
                    release_observed = self._release_is_observable(
                        initialization.sidecar,
                        initialization.producer_id,
                        deadline,
                    )
                    if not release_observed and active.sidecar_flush_failure_count < 2:
                        self._record_shutdown_failure("SIDECAR_FLUSH_FAILED")
                        return False
                    active.loss_code = active.loss_code or "SIDECAR_FLUSH_ABANDONED"
                    if release_observed:
                        active.lease_released = True
                active.sidecar_flushed = True
            if not active.heartbeat_stopped:
                if not active.heartbeat.stop(self._remaining(deadline)):
                    self._record_shutdown_failure("HEARTBEAT_STOP_TIMEOUT")
                    return False
                active.heartbeat_stopped = True
            if not active.lease_released:
                try:
                    goodbye(
                        initialization.sidecar.state,
                        initialization.producer_id,
                        initialization.lease_id,
                        timeout=self._remaining(deadline),
                    )
                except Exception:
                    if not self._release_is_observable(
                        initialization.sidecar,
                        initialization.producer_id,
                        deadline,
                    ):
                        active.lease_release_failure_count += 1
                        if active.lease_release_failure_count < 2:
                            self._record_shutdown_failure("LEASE_RELEASE_FAILED")
                            return False
                        active.loss_code = active.loss_code or "LEASE_RELEASE_ABANDONED"
                active.lease_released = True
            loss_code = active.loss_code
            self._finalize_resources_locked(active)
            previous_active = self._shared.active
            previous_reference_count = self._shared.reference_count
            previous_enqueue = self._shared.delivery_enqueue
            previous_flush = self._shared.delivery_flush
            previous_generation = self._generation
            try:
                self._shared.active = None
                self._shared.reference_count = 0
                self._shared.delivery_enqueue = None
                self._shared.delivery_flush = None
                self._generation = None
            except BaseException:
                self._shared.active = previous_active
                self._shared.reference_count = previous_reference_count
                self._shared.delivery_enqueue = previous_enqueue
                self._shared.delivery_flush = previous_flush
                self._generation = previous_generation
                raise
            if loss_code is not None:
                return False
            return True

    def health_snapshot(self) -> LifecycleHealth:
        with self._shared.lock:
            active = self._shared.active
            pending = self._shared.pending_cleanup
            heartbeat = (
                active.heartbeat.snapshot()
                if active is not None
                else (
                    pending.heartbeat.snapshot()
                    if pending is not None
                    else _HeartbeatHealth(0, 0, None)
                )
            )
            return LifecycleHealth(
                active=active is not None or pending is not None,
                producer_id=(
                    active.initialization.producer_id
                    if active is not None
                    else (None if pending is None else pending.producer_id)
                ),
                heartbeat_renewal_count=self._shared.heartbeat_renewal_count
                + heartbeat.renewal_count,
                heartbeat_error_count=self._shared.heartbeat_error_count + heartbeat.error_count,
                heartbeat_last_error_code=(
                    heartbeat.last_error_code or self._shared.heartbeat_last_error_code
                ),
                shutdown_failure_count=self._shared.shutdown_failure_count,
                shutdown_last_error_code=self._shared.shutdown_last_error_code,
            )

    def _record_shutdown_failure(self, code: str) -> None:
        # v1 is standard GIL-enabled CPython. These two scalar updates must not
        # wait on another coordinator lock after the caller's deadline expires.
        self._shared.shutdown_failure_count += 1
        self._shared.shutdown_last_error_code = code

    def _cleanup_pending_locked(self, pending: _PendingCleanup, deadline: float) -> bool:
        if not pending.telemetry_drained:
            if pending.telemetry is None or not pending.telemetry.deactivate(
                self._remaining(deadline)
            ):
                pending.telemetry_failure_count += 1
                if (
                    pending.telemetry is None
                    or pending.telemetry_failure_count < 2
                    or not pending.telemetry.abandon_failed_flush()
                ):
                    self._record_shutdown_failure("TELEMETRY_DRAIN_TIMEOUT")
                    return False
                pending.loss_code = "TELEMETRY_FLUSH_ABANDONED"
            pending.telemetry_drained = True
        if not pending.sidecar_flushed:
            try:
                flush_sidecar(
                    pending.sidecar.state,
                    pending.producer_id,
                    pending.lease_id,
                    timeout=self._remaining(deadline),
                )
            except Exception:
                pending.sidecar_flush_failure_count += 1
                release_observed = self._release_is_observable(
                    pending.sidecar, pending.producer_id, deadline
                )
                if not release_observed and pending.sidecar_flush_failure_count < 2:
                    self._record_shutdown_failure("SIDECAR_FLUSH_FAILED")
                    return False
                pending.loss_code = pending.loss_code or "SIDECAR_FLUSH_ABANDONED"
                if release_observed:
                    pending.lease_released = True
            pending.sidecar_flushed = True
        if not pending.heartbeat_stopped:
            if not pending.heartbeat.stop(self._remaining(deadline)):
                self._record_shutdown_failure("HEARTBEAT_STOP_TIMEOUT")
                return False
            pending.heartbeat_stopped = True
        if not pending.lease_released:
            try:
                goodbye(
                    pending.sidecar.state,
                    pending.producer_id,
                    pending.lease_id,
                    timeout=self._remaining(deadline),
                )
            except Exception:
                if not self._release_is_observable(pending.sidecar, pending.producer_id, deadline):
                    pending.lease_release_failure_count += 1
                    if pending.lease_release_failure_count < 2:
                        self._record_shutdown_failure("LEASE_RELEASE_FAILED")
                        return False
                    pending.loss_code = pending.loss_code or "LEASE_RELEASE_ABANDONED"
            pending.lease_released = True
        loss_code = pending.loss_code
        self._finalize_resources_locked(pending)
        previous_pending = self._shared.pending_cleanup
        previous_reference_count = self._shared.reference_count
        previous_enqueue = self._shared.delivery_enqueue
        previous_flush = self._shared.delivery_flush
        previous_generation = self._generation
        try:
            self._shared.pending_cleanup = None
            self._shared.reference_count = 0
            self._shared.delivery_enqueue = None
            self._shared.delivery_flush = None
            self._generation = None
        except BaseException:
            self._shared.pending_cleanup = previous_pending
            self._shared.reference_count = previous_reference_count
            self._shared.delivery_enqueue = previous_enqueue
            self._shared.delivery_flush = previous_flush
            self._generation = previous_generation
            raise
        if loss_code is not None:
            return False
        return True

    def _finalize_resources_locked(self, cleanup: _ActiveLifecycle | _PendingCleanup) -> None:
        if not cleanup.heartbeat_archived:
            snapshot = cleanup.heartbeat.snapshot()
            previous_renewals = self._shared.heartbeat_renewal_count
            previous_errors = self._shared.heartbeat_error_count
            previous_code = self._shared.heartbeat_last_error_code
            try:
                self._shared.heartbeat_renewal_count += snapshot.renewal_count
                self._shared.heartbeat_error_count += snapshot.error_count
                if snapshot.last_error_code is not None:
                    self._shared.heartbeat_last_error_code = snapshot.last_error_code
                cleanup.heartbeat_archived = True
            except BaseException:
                self._shared.heartbeat_renewal_count = previous_renewals
                self._shared.heartbeat_error_count = previous_errors
                self._shared.heartbeat_last_error_code = previous_code
                cleanup.heartbeat_archived = False
                raise
        if not cleanup.registry_binding_released:
            self._shared.registry.release_binding(self._provider)
            cleanup.registry_binding_released = True
        if cleanup.loss_code is not None and not cleanup.loss_reported:
            previous_failure_count = self._shared.shutdown_failure_count
            previous_failure_code = self._shared.shutdown_last_error_code
            try:
                self._record_shutdown_failure(cleanup.loss_code)
                cleanup.loss_reported = True
            except BaseException:
                self._shared.shutdown_failure_count = previous_failure_count
                self._shared.shutdown_last_error_code = previous_failure_code
                cleanup.loss_reported = False
                raise

    def _release_is_observable(
        self, sidecar: SidecarHandle, producer_id: str, deadline: float
    ) -> bool:
        health = probe_health(
            sidecar.state,
            timeout=min(self._shared.config.request_timeout, self._remaining(deadline)),
        )
        if health is not None:
            return health.get("active_producer_id") != producer_id
        state = StateStore(self._shared.config.runtime_dir).load()
        return state is None or state.startup_id != sidecar.state.startup_id

    def _observe_owned_lease(self, sidecar: SidecarHandle, producer_id: str) -> str | None:
        health = probe_health(sidecar.state, timeout=self._shared.config.request_timeout)
        if health is None or health.get("active_producer_id") != producer_id:
            return None
        lease_id = health.get("active_lease_id")
        return lease_id if type(lease_id) is str and lease_id else None

    def _ensure_sidecar(self) -> SidecarHandle:
        # Local import keeps this trial facade from becoming a production import
        # path and lets process tests replace the election primitive explicitly.
        from .runtime import ensure_sidecar

        return ensure_sidecar(self._shared.config)

    @staticmethod
    def _remaining(deadline: float) -> float:
        return min(threading.TIMEOUT_MAX, max(0.0, deadline - time.monotonic()))
