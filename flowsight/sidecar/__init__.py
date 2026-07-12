"""Production sidecar primitives."""

from .app import MAX_PRIVATE_BODY_BYTES, create_sidecar_app
from .child_adoption import adopt_sidecar_child_descriptors
from .child_bootstrap import (
    CHILD_BOOTSTRAP_SCHEMA_VERSION,
    SidecarChildBootstrap,
    decode_sidecar_child_bootstrap,
    encode_sidecar_child_bootstrap,
)
from .child_preparation import prepare_sidecar_child
from .health import probe_sidecar_health
from .incumbent_port import admit_configured_incumbent_port
from .listener import (
    DEFAULT_SIDECAR_PORT,
    ListenerBindError,
    ListenerErrorCode,
    bind_loopback_listener,
)
from .owner_lock import OwnerLock, OwnerLockError, OwnerLockErrorCode
from .runtime_config import SidecarRuntimeConfig, prepare_sidecar_runtime_config
from .startup_admission import (
    StartupAdmissionError,
    StartupAdmissionErrorCode,
    receive_startup_outcome,
)
from .startup_channel import (
    MAX_STARTUP_SIGNAL_BYTES,
    STARTUP_CHANNEL_SCHEMA_VERSION,
    StartupChannelError,
    StartupChannelErrorCode,
    StartupFailure,
    StartupFailureCode,
    StartupReader,
    StartupReady,
    StartupWriter,
    open_startup_channel,
)
from .startup_discovery import discover_existing_startup
from .startup_election import (
    OwnerElectionError,
    OwnerElectionErrorCode,
    resolve_owner_election,
)
from .startup_state import (
    StartupStateError,
    StartupStateErrorCode,
    create_startup_state,
)
from .startup_verification import verify_ready_startup
from .startup_wait import wait_for_owner_election
from .state import (
    LOOPBACK_HOST,
    MAX_STATE_BYTES,
    PROTOCOL_VERSION,
    STATE_SCHEMA_VERSION,
    InvalidStateError,
    SidecarState,
    StateBusyError,
    StateStorageError,
    StateStore,
)

__all__ = [
    "LOOPBACK_HOST",
    "MAX_PRIVATE_BODY_BYTES",
    "MAX_STARTUP_SIGNAL_BYTES",
    "MAX_STATE_BYTES",
    "CHILD_BOOTSTRAP_SCHEMA_VERSION",
    "DEFAULT_SIDECAR_PORT",
    "ListenerBindError",
    "ListenerErrorCode",
    "OwnerLock",
    "OwnerLockError",
    "OwnerLockErrorCode",
    "SidecarChildBootstrap",
    "SidecarRuntimeConfig",
    "OwnerElectionError",
    "OwnerElectionErrorCode",
    "PROTOCOL_VERSION",
    "STATE_SCHEMA_VERSION",
    "STARTUP_CHANNEL_SCHEMA_VERSION",
    "InvalidStateError",
    "SidecarState",
    "StateBusyError",
    "StateStorageError",
    "StateStore",
    "StartupChannelError",
    "StartupChannelErrorCode",
    "StartupAdmissionError",
    "StartupAdmissionErrorCode",
    "StartupFailure",
    "StartupFailureCode",
    "StartupReader",
    "StartupReady",
    "StartupStateError",
    "StartupStateErrorCode",
    "StartupWriter",
    "admit_configured_incumbent_port",
    "adopt_sidecar_child_descriptors",
    "bind_loopback_listener",
    "create_sidecar_app",
    "create_startup_state",
    "decode_sidecar_child_bootstrap",
    "discover_existing_startup",
    "encode_sidecar_child_bootstrap",
    "open_startup_channel",
    "probe_sidecar_health",
    "prepare_sidecar_child",
    "prepare_sidecar_runtime_config",
    "receive_startup_outcome",
    "resolve_owner_election",
    "verify_ready_startup",
    "wait_for_owner_election",
]
