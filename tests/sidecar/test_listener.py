from __future__ import annotations

import errno
import socket
from collections.abc import Callable
from enum import IntEnum
from typing import NoReturn

import pytest

from flowsight.sidecar import (
    DEFAULT_SIDECAR_PORT,
    LOOPBACK_HOST,
    ListenerBindError,
    ListenerErrorCode,
    bind_loopback_listener,
)
from flowsight.sidecar import listener as listener_module


def _real_listener(port: int = 0) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((LOOPBACK_HOST, port))
    listener.listen(8)
    return listener


def _bound_port(listener: socket.socket) -> int:
    host, port = listener.getsockname()
    assert host == LOOPBACK_HOST
    assert type(port) is int
    return port


class _FakeSocket:
    def __init__(
        self,
        *,
        failure_stage: str | None = None,
        failure: BaseException | None = None,
        close_failure: BaseException | None = None,
    ) -> None:
        self.failure_stage = failure_stage
        self.failure = failure or OSError(errno.EIO, "raw setup detail")
        self.close_failure = close_failure
        self.calls: list[tuple[object, ...]] = []
        self.close_count = 0

    def _fail(self, stage: str) -> None:
        if self.failure_stage == stage:
            raise self.failure

    def set_inheritable(self, inheritable: bool) -> None:
        self.calls.append(("set_inheritable", inheritable))
        self._fail("set_inheritable")

    def setsockopt(self, level: int, option: int, value: int) -> None:
        self.calls.append(("setsockopt", level, option, value))
        self._fail("setsockopt")

    def bind(self, address: tuple[str, int]) -> None:
        self.calls.append(("bind", address))
        self._fail("bind")

    def listen(self, backlog: int) -> None:
        self.calls.append(("listen", backlog))
        self._fail("listen")

    def close(self) -> None:
        self.close_count += 1
        if self.close_failure is not None:
            raise self.close_failure


class _IntSubclass(int):
    pass


class _IntPort(IntEnum):
    VALUE = 4040


def _install_fake_sockets(
    monkeypatch: pytest.MonkeyPatch,
    sockets: list[_FakeSocket],
) -> list[tuple[int, int]]:
    allocations: list[tuple[int, int]] = []

    def create_socket(family: int, kind: int) -> _FakeSocket:
        allocations.append((family, kind))
        if not sockets:
            raise AssertionError("unexpected extra socket allocation")
        return sockets.pop(0)

    monkeypatch.setattr(listener_module.socket, "socket", create_socket)
    return allocations


def _install_socket_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[_FakeSocket | BaseException],
) -> list[tuple[int, int]]:
    allocations: list[tuple[int, int]] = []

    def create_socket(family: int, kind: int) -> _FakeSocket:
        allocations.append((family, kind))
        if not outcomes:
            raise AssertionError("unexpected extra socket allocation")
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(listener_module.socket, "socket", create_socket)
    return allocations


def _assert_public_error_is_private(
    error: ListenerBindError,
    *forbidden_values: str,
) -> None:
    expected_message = f"sidecar listener failed ({error.code})"
    exposed = "\n".join(
        (
            str(error),
            repr(error),
            repr(error.args),
            repr(getattr(error, "__notes__", None)),
            repr(error.__cause__),
            repr(error.__context__),
        )
    )
    for value in forbidden_values:
        assert value not in exposed
    assert str(error) == expected_message
    assert error.args == (expected_message,)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert getattr(error, "__notes__", None) is None


def test_default_constant_is_the_v1_port() -> None:
    assert DEFAULT_SIDECAR_PORT == 4040


def test_requested_zero_returns_exact_listening_loopback_socket() -> None:
    listener = bind_loopback_listener(0)
    try:
        assert listener.family == socket.AF_INET
        assert listener.type & socket.SOCK_STREAM == socket.SOCK_STREAM
        assert listener.get_inheritable() is False
        assert listener.fileno() >= 0
        port = _bound_port(listener)
        assert port > 0
        listener.settimeout(1.0)
        client = socket.create_connection((LOOPBACK_HOST, port), timeout=1.0)
        try:
            accepted, _address = listener.accept()
            accepted.close()
        finally:
            client.close()
    finally:
        listener.close()


def test_available_default_port_is_retained_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSocket()
    allocations = _install_fake_sockets(monkeypatch, [fake])

    listener = bind_loopback_listener(default_port=4321)

    assert listener is fake
    assert allocations == [(socket.AF_INET, socket.SOCK_STREAM)]
    assert ("bind", (LOOPBACK_HOST, 4321)) in fake.calls
    assert fake.close_count == 0


def test_default_zero_requests_one_real_os_selected_port() -> None:
    listener = bind_loopback_listener(default_port=0)
    try:
        assert _bound_port(listener) > 0
    finally:
        listener.close()


def test_default_conflict_falls_back_to_os_selected_port() -> None:
    blocker = _real_listener()
    blocked_port = _bound_port(blocker)
    try:
        listener = bind_loopback_listener(default_port=blocked_port)
        try:
            assert _bound_port(listener) > 0
            assert _bound_port(listener) != blocked_port
        finally:
            listener.close()
    finally:
        blocker.close()


def test_explicit_conflict_never_falls_back() -> None:
    blocker = _real_listener()
    blocked_port = _bound_port(blocker)
    calls: list[int] = []
    real_open = listener_module._open_listener

    def recording_open(port: int) -> socket.socket:
        calls.append(port)
        return real_open(port)

    try:
        with pytest.MonkeyPatch.context() as context:
            context.setattr(listener_module, "_open_listener", recording_open)
            with pytest.raises(ListenerBindError) as captured:
                bind_loopback_listener(blocked_port)
    finally:
        blocker.close()

    assert captured.value.code is ListenerErrorCode.EXPLICIT_PORT_CONFLICT
    _assert_public_error_is_private(captured.value, str(blocked_port))
    assert calls == [blocked_port]


def test_returned_listener_exclusively_holds_port_until_close() -> None:
    listener = bind_loopback_listener(0)
    port = _bound_port(listener)
    try:
        reuse_options: list[int | None] = [None]
        if hasattr(socket, "SO_REUSEPORT"):
            reuse_options.append(socket.SO_REUSEPORT)
        for extra_option in reuse_options:
            competitor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            competitor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if extra_option is not None:
                competitor.setsockopt(socket.SOL_SOCKET, extra_option, 1)
            try:
                with pytest.raises(OSError) as captured:
                    competitor.bind((LOOPBACK_HOST, port))
                assert captured.value.errno == errno.EADDRINUSE
            finally:
                competitor.close()
    finally:
        listener.close()

    replacement = _real_listener(port)
    replacement.close()


@pytest.mark.parametrize(
    "invalid_port",
    [
        True,
        False,
        -1,
        65_536,
        1.0,
        "4040",
        b"4040",
        object(),
        _IntSubclass(4040),
        _IntPort.VALUE,
    ],
)
@pytest.mark.parametrize("argument", ["requested", "default"])
def test_invalid_ports_fail_before_socket_allocation(
    monkeypatch: pytest.MonkeyPatch,
    invalid_port: object,
    argument: str,
) -> None:
    def unexpected_allocation(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("invalid input allocated a socket")

    monkeypatch.setattr(listener_module.socket, "socket", unexpected_allocation)

    with pytest.raises(ValueError, match="exact built-in int") as captured:
        if argument == "requested":
            bind_loopback_listener(invalid_port)  # type: ignore[arg-type]
        else:
            bind_loopback_listener(default_port=invalid_port)  # type: ignore[arg-type]

    assert repr(invalid_port) not in str(captured.value)


@pytest.mark.parametrize("port", [0, 65_535])
@pytest.mark.parametrize("argument", ["requested", "default"])
def test_exact_port_boundaries_are_accepted(
    monkeypatch: pytest.MonkeyPatch,
    port: int,
    argument: str,
) -> None:
    fake = _FakeSocket()
    allocations = _install_fake_sockets(monkeypatch, [fake])

    if argument == "requested":
        result = bind_loopback_listener(port)
    else:
        result = bind_loopback_listener(default_port=port)

    assert result is fake
    assert allocations == [(socket.AF_INET, socket.SOCK_STREAM)]
    assert ("bind", (LOOPBACK_HOST, port)) in fake.calls


def test_success_uses_exact_socket_and_fixed_safe_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSocket()
    allocations = _install_fake_sockets(monkeypatch, [fake])

    result = bind_loopback_listener(1234)

    assert result is fake
    assert allocations == [(socket.AF_INET, socket.SOCK_STREAM)]
    assert fake.calls == [
        ("set_inheritable", False),
        ("setsockopt", socket.SOL_SOCKET, socket.SO_REUSEADDR, 1),
        ("bind", (LOOPBACK_HOST, 1234)),
        ("listen", 128),
    ]
    assert fake.close_count == 0


def test_default_conflict_uses_one_fresh_fallback_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _FakeSocket(
        failure_stage="bind",
        failure=OSError(errno.EADDRINUSE, "raw occupied port detail"),
    )
    fallback = _FakeSocket()
    allocations = _install_fake_sockets(monkeypatch, [first, fallback])

    result = bind_loopback_listener(default_port=4321)

    assert result is fallback
    assert allocations == [
        (socket.AF_INET, socket.SOCK_STREAM),
        (socket.AF_INET, socket.SOCK_STREAM),
    ]
    assert first.close_count == 1
    assert ("bind", (LOOPBACK_HOST, 4321)) in first.calls
    assert ("bind", (LOOPBACK_HOST, 0)) in fallback.calls
    assert fallback.close_count == 0


@pytest.mark.parametrize(
    ("explicit", "expected_code"),
    [
        (False, ListenerErrorCode.DEFAULT_PORT_UNAVAILABLE),
        (True, ListenerErrorCode.EXPLICIT_PORT_UNAVAILABLE),
    ],
)
def test_non_conflict_bind_failure_does_not_fall_back_and_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
    expected_code: ListenerErrorCode,
) -> None:
    raw_detail = "secret path /tmp/private and port 4567"
    fake = _FakeSocket(
        failure_stage="bind",
        failure=OSError(errno.EACCES, raw_detail),
    )
    remaining = [fake]
    allocations = _install_fake_sockets(monkeypatch, remaining)

    with pytest.raises(ListenerBindError) as captured:
        if explicit:
            bind_loopback_listener(4567)
        else:
            bind_loopback_listener(default_port=4567)

    assert captured.value.code is expected_code
    _assert_public_error_is_private(captured.value, raw_detail, "4567")
    assert allocations == [(socket.AF_INET, socket.SOCK_STREAM)]
    assert not remaining
    assert fake.close_count == 1


def test_failed_fallback_closes_both_sockets_and_returns_safe_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_detail = "fallback secret errno detail"
    first = _FakeSocket(
        failure_stage="bind",
        failure=OSError(errno.EADDRINUSE, "default conflict"),
    )
    fallback = _FakeSocket(
        failure_stage="bind",
        failure=OSError(errno.EACCES, raw_detail),
    )
    _install_fake_sockets(monkeypatch, [first, fallback])

    with pytest.raises(ListenerBindError) as captured:
        bind_loopback_listener(default_port=4321)

    assert captured.value.code is ListenerErrorCode.DEFAULT_PORT_UNAVAILABLE
    _assert_public_error_is_private(captured.value, "default conflict", raw_detail, "4321")
    assert first.close_count == 1
    assert fallback.close_count == 1


@pytest.mark.parametrize("stage", ["allocation", "set_inheritable", "setsockopt", "listen"])
def test_fallback_setup_failures_are_bounded_closed_and_private(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    raw_detail = f"raw fallback {stage} secret"
    first = _FakeSocket(
        failure_stage="bind",
        failure=OSError(errno.EADDRINUSE, "raw default conflict"),
    )
    fallback: _FakeSocket | None = None
    outcomes: list[_FakeSocket | BaseException] = [first]
    if stage == "allocation":
        outcomes.append(OSError(errno.EMFILE, raw_detail))
    else:
        fallback = _FakeSocket(
            failure_stage=stage,
            failure=OSError(errno.EIO, raw_detail),
        )
        outcomes.append(fallback)
    allocations = _install_socket_outcomes(monkeypatch, outcomes)

    with pytest.raises(ListenerBindError) as captured:
        bind_loopback_listener(default_port=4321)

    assert captured.value.code is ListenerErrorCode.LISTENER_SETUP_FAILED
    _assert_public_error_is_private(
        captured.value,
        "raw default conflict",
        raw_detail,
        "4321",
    )
    assert allocations == [
        (socket.AF_INET, socket.SOCK_STREAM),
        (socket.AF_INET, socket.SOCK_STREAM),
    ]
    assert not outcomes
    assert first.close_count == 1
    if fallback is not None:
        assert fallback.close_count == 1


@pytest.mark.parametrize(
    "stage",
    ["allocation", "set_inheritable", "setsockopt", "bind", "listen"],
)
@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_process_control_during_fallback_closes_once_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    error_factory: Callable[[], BaseException],
) -> None:
    first = _FakeSocket(
        failure_stage="bind",
        failure=OSError(errno.EADDRINUSE, "raw default conflict"),
    )
    fallback: _FakeSocket | None = None
    outcomes: list[_FakeSocket | BaseException] = [first]
    if stage == "allocation":
        outcomes.append(error_factory())
    else:
        fallback = _FakeSocket(failure_stage=stage, failure=error_factory())
        outcomes.append(fallback)
    _install_socket_outcomes(monkeypatch, outcomes)

    with pytest.raises(error_factory):
        bind_loopback_listener(default_port=4321)

    assert not outcomes
    assert first.close_count == 1
    if fallback is not None:
        assert fallback.close_count == 1


def test_listen_eaddrinuse_is_setup_failure_not_default_bind_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSocket(
        failure_stage="listen",
        failure=OSError(errno.EADDRINUSE, "raw listen conflict detail"),
    )
    remaining = [fake]
    allocations = _install_fake_sockets(monkeypatch, remaining)

    with pytest.raises(ListenerBindError) as captured:
        bind_loopback_listener(default_port=4321)

    assert captured.value.code is ListenerErrorCode.LISTENER_SETUP_FAILED
    _assert_public_error_is_private(captured.value, "raw listen conflict detail", "4321")
    assert allocations == [(socket.AF_INET, socket.SOCK_STREAM)]
    assert not remaining
    assert fake.close_count == 1


@pytest.mark.parametrize("stage", ["set_inheritable", "setsockopt", "listen"])
def test_setup_failures_close_once_without_raw_detail(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    raw_detail = "raw setup failure with secret"
    fake = _FakeSocket(failure_stage=stage, failure=OSError(errno.EIO, raw_detail))
    allocations = _install_fake_sockets(monkeypatch, [fake])

    with pytest.raises(ListenerBindError) as captured:
        bind_loopback_listener(0)

    assert captured.value.code is ListenerErrorCode.LISTENER_SETUP_FAILED
    _assert_public_error_is_private(captured.value, raw_detail)
    assert allocations == [(socket.AF_INET, socket.SOCK_STREAM)]
    assert fake.close_count == 1


def test_socket_allocation_failure_is_fixed_and_non_sensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_detail = "too many files near /private/project"
    allocations: list[tuple[int, int]] = []

    def fail_allocation(family: int, kind: int) -> NoReturn:
        allocations.append((family, kind))
        raise OSError(errno.EMFILE, raw_detail)

    monkeypatch.setattr(listener_module.socket, "socket", fail_allocation)

    with pytest.raises(ListenerBindError) as captured:
        bind_loopback_listener(0)

    assert captured.value.code is ListenerErrorCode.LISTENER_SETUP_FAILED
    _assert_public_error_is_private(captured.value, raw_detail)
    assert allocations == [(socket.AF_INET, socket.SOCK_STREAM)]


@pytest.mark.parametrize("stage", ["set_inheritable", "setsockopt", "bind", "listen"])
@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_process_control_exceptions_close_once_and_propagate(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    error_factory: Callable[[], BaseException],
) -> None:
    fake = _FakeSocket(failure_stage=stage, failure=error_factory())
    _install_fake_sockets(monkeypatch, [fake])

    with pytest.raises(error_factory):
        bind_loopback_listener(0)

    assert fake.close_count == 1


@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_process_control_during_allocation_propagates(
    monkeypatch: pytest.MonkeyPatch,
    error_factory: Callable[[], BaseException],
) -> None:
    def interrupt_allocation(_family: int, _kind: int) -> NoReturn:
        raise error_factory()

    monkeypatch.setattr(listener_module.socket, "socket", interrupt_allocation)

    with pytest.raises(error_factory):
        bind_loopback_listener(0)


def test_close_failure_does_not_replace_process_control_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = KeyboardInterrupt()
    fake = _FakeSocket(
        failure_stage="listen",
        failure=error,
        close_failure=OSError(errno.EIO, "raw close detail"),
    )
    allocations = _install_fake_sockets(monkeypatch, [fake])

    with pytest.raises(KeyboardInterrupt) as captured:
        bind_loopback_listener(0)

    assert captured.value is error
    assert allocations == [(socket.AF_INET, socket.SOCK_STREAM)]
    assert fake.close_count == 1
    assert getattr(captured.value, "__notes__", []) == ["loopback listener cleanup failed"]


@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_process_control_raised_by_close_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
    error_factory: Callable[[], BaseException],
) -> None:
    cleanup_error = error_factory()
    fake = _FakeSocket(
        failure_stage="listen",
        failure=OSError(errno.EIO, "raw active detail"),
        close_failure=cleanup_error,
    )
    allocations = _install_fake_sockets(monkeypatch, [fake])

    with pytest.raises(error_factory) as captured:
        bind_loopback_listener(0)

    assert captured.value is cleanup_error
    assert allocations == [(socket.AF_INET, socket.SOCK_STREAM)]
    assert fake.close_count == 1


@pytest.mark.parametrize("stage", ["set_inheritable", "setsockopt", "bind", "listen"])
def test_close_failure_on_regular_error_becomes_safe_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    active_errno = errno.EADDRINUSE if stage == "bind" else errno.EIO
    fake = _FakeSocket(
        failure_stage=stage,
        failure=OSError(active_errno, "raw active detail"),
        close_failure=OSError(errno.EIO, "raw close detail"),
    )
    allocations = _install_fake_sockets(monkeypatch, [fake])

    with pytest.raises(ListenerBindError) as captured:
        bind_loopback_listener(default_port=4321)

    assert captured.value.code is ListenerErrorCode.LISTENER_SETUP_FAILED
    _assert_public_error_is_private(
        captured.value,
        "raw active detail",
        "raw close detail",
        "4321",
    )
    assert allocations == [(socket.AF_INET, socket.SOCK_STREAM)]
    assert fake.close_count == 1
