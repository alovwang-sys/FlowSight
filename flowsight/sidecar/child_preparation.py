"""Exact composition of decoded sidecar child resources."""

from __future__ import annotations

from typing import Final, NoReturn

from .child_adoption import adopt_sidecar_child_descriptors
from .child_bootstrap import SidecarChildBootstrap, decode_sidecar_child_bootstrap
from .owner_lock import OwnerLock
from .runtime_config import SidecarRuntimeConfig
from .startup_channel import StartupWriter

_BOOTSTRAP_TYPE: Final = SidecarChildBootstrap
_CONFIG_TYPE: Final = SidecarRuntimeConfig

_DECODE_BOOTSTRAP: Final = decode_sidecar_child_bootstrap
_ADOPT_DESCRIPTORS: Final = adopt_sidecar_child_descriptors

type _PreparedChild = tuple[SidecarRuntimeConfig, OwnerLock, StartupWriter]


class _DecodeStageFailure(Exception):
    pass


class _AdoptionStageFailure(Exception):
    pass


def _decode_bootstrap(arguments: tuple[str, ...]) -> object:
    return _DECODE_BOOTSTRAP(arguments)


def _decode_stage(
    arguments: tuple[str, ...],
) -> tuple[SidecarChildBootstrap, SidecarRuntimeConfig]:
    candidate: object | None = None
    bootstrap: SidecarChildBootstrap | None = None
    config: object | None = None
    trusted_config: SidecarRuntimeConfig | None = None
    try:
        candidate = _decode_bootstrap(arguments)
        if type(candidate) is not _BOOTSTRAP_TYPE:
            raise ValueError
        bootstrap = candidate
        config = object.__getattribute__(bootstrap, "config")
        if type(config) is not _CONFIG_TYPE:
            raise ValueError
        trusted_config = config
        if trusted_config is not object.__getattribute__(bootstrap, "config"):
            raise ValueError
        return bootstrap, trusted_config
    except Exception:
        pass
    del arguments, candidate, bootstrap, config, trusted_config
    raise _DecodeStageFailure from None


def _prepare_child(arguments: tuple[str, ...]) -> _PreparedChild:
    bootstrap, config = _decode_stage(arguments)
    del arguments
    try:
        pair = _ADOPT_DESCRIPTORS(bootstrap)
    except Exception:
        pass
    else:
        return (config, pair[0], pair[1])
    del bootstrap, config
    raise _AdoptionStageFailure from None


def _raise_decode_failure() -> NoReturn:
    raise ValueError("sidecar child bootstrap is invalid") from None


def _raise_adoption_failure() -> NoReturn:
    raise RuntimeError("sidecar child descriptor adoption failed") from None


def prepare_sidecar_child(
    arguments: tuple[str, ...],
) -> tuple[SidecarRuntimeConfig, OwnerLock, StartupWriter]:
    """Re-derive one child configuration and consume its descriptor pair."""

    failure = 0
    try:
        return _prepare_child(arguments)
    except _DecodeStageFailure:
        failure = 1
    except _AdoptionStageFailure:
        failure = 2
    del arguments
    if failure == 1:
        del failure
        _raise_decode_failure()
    del failure
    _raise_adoption_failure()
