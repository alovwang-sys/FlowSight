"""Isolated process and OpenTelemetry lifecycle spike.

Nothing in this package is part of FlowSight's public runtime API.  The spike
exists to make the Phase 0 process contract executable before that contract is
promoted into ``flowsight``.
"""

from .lifecycle import FlowSightLifecycle, LifecycleHealth, LifecycleInitialization
from .runtime import (
    AuthenticationError,
    LeaseRejectedError,
    RuntimeConfig,
    SidecarError,
    SidecarHandle,
    SidecarStartupError,
    SidecarUnavailableError,
    ensure_sidecar,
    flush,
    goodbye,
    hello,
    probe_health,
    renew,
    request_json,
    stop_sidecar,
)
from .state import PROTOCOL_VERSION, SidecarState, StateStore

__all__ = [
    "PROTOCOL_VERSION",
    "AuthenticationError",
    "FlowSightLifecycle",
    "LifecycleHealth",
    "LifecycleInitialization",
    "LeaseRejectedError",
    "RuntimeConfig",
    "SidecarError",
    "SidecarHandle",
    "SidecarStartupError",
    "SidecarState",
    "SidecarUnavailableError",
    "StateStore",
    "ensure_sidecar",
    "flush",
    "goodbye",
    "hello",
    "probe_health",
    "renew",
    "request_json",
    "stop_sidecar",
]
