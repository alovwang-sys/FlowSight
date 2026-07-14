"""Real Uvicorn ``--reload`` fixture for the lifecycle spike.

Run it as an application factory so every replacement worker gets fresh SDK
threads and a fresh producer identity while reconnecting to the same sidecar::

    python -m uvicorn \
      --factory spikes.sidecar_otel.reload_fixture:create_reload_app \
      --reload --reload-dir <fixture-dir> --host 127.0.0.1 --port <port>

Tests can bind an AF_UNIX datagram socket and put its path in
``FLOWSIGHT_SPIKE_NOTIFY_SOCKET``.  Worker startup/shutdown then becomes an
observable, bounded socket wait rather than a timing sleep or log scrape.
"""

from __future__ import annotations

import contextlib
import os
import socket
import threading
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI

from .runtime import (
    RuntimeConfig,
    SidecarHandle,
    flush,
    goodbye,
    hello,
    renew,
)


@dataclass(frozen=True, slots=True)
class ReloadSettings:
    runtime_dir: Path
    project_id: str
    requested_port: int | None
    default_port: int
    startup_timeout: float
    idle_timeout: float
    lease_ttl: float
    writer_capacity: int
    notify_socket: str | None

    @classmethod
    def from_environment(cls) -> ReloadSettings:
        runtime_dir = os.environ.get("FLOWSIGHT_SPIKE_RUNTIME_DIR")
        project_id = os.environ.get("FLOWSIGHT_SPIKE_PROJECT_ID")
        if not runtime_dir or not project_id:
            raise RuntimeError(
                "FLOWSIGHT_SPIKE_RUNTIME_DIR and FLOWSIGHT_SPIKE_PROJECT_ID are required"
            )
        requested = os.environ.get("FLOWSIGHT_SPIKE_SIDECAR_PORT")
        return cls(
            runtime_dir=Path(runtime_dir),
            project_id=project_id,
            requested_port=None if requested is None else int(requested),
            default_port=int(os.environ.get("FLOWSIGHT_SPIKE_DEFAULT_PORT", "4040")),
            startup_timeout=float(os.environ.get("FLOWSIGHT_SPIKE_STARTUP_TIMEOUT", "5")),
            idle_timeout=float(os.environ.get("FLOWSIGHT_SPIKE_IDLE_TIMEOUT", "5")),
            lease_ttl=float(os.environ.get("FLOWSIGHT_SPIKE_LEASE_TTL", "5")),
            writer_capacity=int(os.environ.get("FLOWSIGHT_SPIKE_WRITER_CAPACITY", "64")),
            notify_socket=os.environ.get("FLOWSIGHT_SPIKE_NOTIFY_SOCKET"),
        )

    def runtime_config(self) -> RuntimeConfig:
        return RuntimeConfig(
            runtime_dir=self.runtime_dir,
            project_id=self.project_id,
            requested_port=self.requested_port,
            default_port=self.default_port,
            startup_timeout=self.startup_timeout,
            idle_timeout=self.idle_timeout,
            lease_ttl=self.lease_ttl,
            writer_capacity=self.writer_capacity,
        )


def _notify(settings: ReloadSettings, event: str, **fields: object) -> None:
    if settings.notify_socket is None:
        return
    import json

    payload = json.dumps({"event": event, **fields}, separators=(",", ":"), sort_keys=True).encode()
    notifier = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        notifier.sendto(payload, settings.notify_socket)
    finally:
        notifier.close()


class _LeaseHeartbeat:
    def __init__(
        self,
        settings: ReloadSettings,
        handle: SidecarHandle,
        producer_id: str,
        lease_id: str,
    ) -> None:
        self._settings = settings
        self._handle = handle
        self._producer_id = producer_id
        self._lease_id = lease_id
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="flowsight-spike-reload-heartbeat",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self._settings.lease_ttl))

    def _run(self) -> None:
        interval = max(0.05, self._settings.lease_ttl / 3)
        renewal_timeout = min(1.0, max(0.05, interval / 2))
        while not self._stop.is_set():
            try:
                renew(
                    self._handle.state,
                    self._producer_id,
                    self._lease_id,
                    timeout=renewal_timeout,
                )
            except Exception as error:
                _notify(
                    self._settings,
                    "heartbeat_retry",
                    worker_pid=os.getpid(),
                    error_type=type(error).__name__,
                )
                if self._stop.wait(min(0.01, interval / 10)):
                    return
                continue
            if self._stop.wait(interval):
                return


def create_reload_app() -> FastAPI:
    """Create one worker generation that attaches, leases, and says goodbye."""

    settings = ReloadSettings.from_environment()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Import here so test collection can inspect this fixture without
        # starting a sidecar.  Every reload generation executes this afresh.
        from .runtime import ensure_sidecar

        producer_id = f"worker-{os.getpid()}-{uuid.uuid4().hex}"
        handle: SidecarHandle | None = None
        lease_id: str | None = None
        heartbeat: _LeaseHeartbeat | None = None
        try:
            handle = ensure_sidecar(settings.runtime_config())
            wait_ms = min(5000, max(0, int(settings.startup_timeout * 1000)))
            lease_id = hello(
                handle.state,
                producer_id,
                wait_timeout_ms=wait_ms,
                timeout=settings.startup_timeout + 1,
            )
            heartbeat = _LeaseHeartbeat(settings, handle, producer_id, lease_id)
            heartbeat.start()
            app.state.sidecar_handle = handle
            app.state.producer_id = producer_id
            app.state.lease_id = lease_id
            _notify(
                settings,
                "worker_started",
                worker_pid=os.getpid(),
                producer_id=producer_id,
                lease_id=lease_id,
                sidecar_pid=handle.state.pid,
                sidecar_port=handle.state.port,
                startup_id=handle.state.startup_id,
            )
            yield
        except BaseException as error:
            _notify(
                settings,
                "worker_start_failed",
                worker_pid=os.getpid(),
                error_type=type(error).__name__,
            )
            raise
        finally:
            if heartbeat is not None:
                heartbeat.stop()
            if handle is not None and lease_id is not None:
                try:
                    flush(handle.state, producer_id, lease_id, timeout=settings.startup_timeout)
                    goodbye(handle.state, producer_id, lease_id, timeout=settings.startup_timeout)
                except Exception as error:
                    _notify(
                        settings,
                        "worker_shutdown_failed",
                        worker_pid=os.getpid(),
                        error_type=type(error).__name__,
                    )
            _notify(
                settings,
                "worker_stopped",
                worker_pid=os.getpid(),
                producer_id=producer_id,
                sidecar_pid=None if handle is None else handle.state.pid,
            )

    app.router.lifespan_context = lifespan

    @app.get("/probe")
    def probe() -> dict[str, object]:
        handle: SidecarHandle = app.state.sidecar_handle
        return {
            "worker_pid": os.getpid(),
            "producer_id": app.state.producer_id,
            "lease_id": app.state.lease_id,
            "sidecar_pid": handle.state.pid,
            "sidecar_port": handle.state.port,
            "startup_id": handle.state.startup_id,
        }

    return app
