"""Configured incumbent-port admission without lifecycle work."""

from __future__ import annotations

from collections.abc import Callable
from typing import Final, NoReturn, cast

from .runtime_config import SidecarRuntimeConfig
from .state import SidecarState

type _BoundSlotGetter = Callable[[object, type[object]], object]

_CONFIG_TYPE: Final = SidecarRuntimeConfig
_STATE_TYPE: Final = SidecarState

_CONFIG_PORT_GETTER: Final[_BoundSlotGetter] = cast(
    _BoundSlotGetter,
    _CONFIG_TYPE.__dict__["requested_port"].__get__,
)
_STATE_PORT_GETTER: Final[_BoundSlotGetter] = cast(
    _BoundSlotGetter,
    _STATE_TYPE.__dict__["port"].__get__,
)


class _PortAdmissionFailure(Exception):
    pass


def _read_requested_port(config: SidecarRuntimeConfig) -> object:
    value: object = None
    try:
        value = _CONFIG_PORT_GETTER(config, _CONFIG_TYPE)
    except BaseException:
        del config, value
        raise
    else:
        return value


def _read_incumbent_port(incumbent: SidecarState) -> object:
    value: object = None
    try:
        value = _STATE_PORT_GETTER(incumbent, _STATE_TYPE)
    except BaseException:
        del incumbent, value
        raise
    else:
        return value


def _admit_port_stage(
    config: SidecarRuntimeConfig,
    incumbent: SidecarState,
) -> SidecarState:
    requested_port: object = None
    incumbent_port: object = None
    try:
        try:
            requested_port = _read_requested_port(config)
        except Exception:
            pass
        else:
            if requested_port is None or (
                type(requested_port) is int and 0 <= requested_port <= 65_535
            ):
                try:
                    incumbent_port = _read_incumbent_port(incumbent)
                except Exception:
                    pass
                else:
                    if (
                        type(incumbent_port) is int
                        and 1 <= incumbent_port <= 65_535
                        and (
                            requested_port is None
                            or requested_port == 0
                            or requested_port == incumbent_port
                        )
                    ):
                        return incumbent
    except BaseException:
        del config, incumbent, requested_port, incumbent_port
        raise
    del config, incumbent, requested_port, incumbent_port
    raise _PortAdmissionFailure from None


def _raise_config_type() -> NoReturn:
    raise TypeError("config must be an exact SidecarRuntimeConfig") from None


def _raise_incumbent_type() -> NoReturn:
    raise TypeError("incumbent must be an exact SidecarState") from None


def _raise_incompatible() -> NoReturn:
    raise RuntimeError("configured incumbent port is incompatible") from None


def admit_configured_incumbent_port(
    config: SidecarRuntimeConfig,
    incumbent: SidecarState,
) -> SidecarState:
    """Return the exact incumbent when its port matches configured policy."""

    if type(config) is not _CONFIG_TYPE:
        del config, incumbent
        _raise_config_type()
    if type(incumbent) is not _STATE_TYPE:
        del config, incumbent
        _raise_incumbent_type()
    try:
        return _admit_port_stage(config, incumbent)
    except _PortAdmissionFailure:
        pass
    except BaseException:
        del config, incumbent
        raise
    del config, incumbent
    _raise_incompatible()
