"""One bounded startup signal between a future sidecar child and launcher."""

from __future__ import annotations

import errno
import fcntl
import json
import math
import os
import select
import stat
import sys
import time
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Any, Final, Literal, NoReturn, cast

STARTUP_CHANNEL_SCHEMA_VERSION: Final = 1
MAX_STARTUP_SIGNAL_BYTES: Final = 512
MAX_STARTUP_CHANNEL_TIMEOUT_SECONDS: Final = 30.0

_MIN_CHANNEL_DESCRIPTOR: Final = 3
_STARTUP_ID_CHARACTERS: Final = frozenset("0123456789abcdef")
_READY_FIELDS: Final = frozenset(
    {
        "startup_channel_schema_version",
        "status",
        "startup_id",
        "sidecar_pid",
        "port",
    }
)
_FAILURE_FIELDS: Final = frozenset({"startup_channel_schema_version", "status", "code"})


class StartupFailureCode(StrEnum):
    """Fixed child-reported failure codes safe to cross the startup pipe."""

    SIDECAR_STARTUP_FAILED = "SIDECAR_STARTUP_FAILED"


class StartupChannelErrorCode(StrEnum):
    """Fixed, non-sensitive local startup-channel failure codes."""

    STARTUP_CHANNEL_SETUP_FAILED = "STARTUP_CHANNEL_SETUP_FAILED"
    STARTUP_CHANNEL_DESCRIPTOR_INVALID = "STARTUP_CHANNEL_DESCRIPTOR_INVALID"
    STARTUP_CHANNEL_WRITE_FAILED = "STARTUP_CHANNEL_WRITE_FAILED"
    STARTUP_CHANNEL_READ_FAILED = "STARTUP_CHANNEL_READ_FAILED"
    STARTUP_CHANNEL_TIMEOUT = "STARTUP_CHANNEL_TIMEOUT"
    STARTUP_CHANNEL_MESSAGE_INVALID = "STARTUP_CHANNEL_MESSAGE_INVALID"
    STARTUP_CHANNEL_MESSAGE_TOO_LARGE = "STARTUP_CHANNEL_MESSAGE_TOO_LARGE"
    STARTUP_CHANNEL_CLEANUP_FAILED = "STARTUP_CHANNEL_CLEANUP_FAILED"
    STARTUP_CHANNEL_CLOSED = "STARTUP_CHANNEL_CLOSED"


class StartupChannelError(RuntimeError):
    """The one-shot startup channel could not satisfy its fixed contract."""

    def __init__(self, code: StartupChannelErrorCode) -> None:
        if type(code) is not StartupChannelErrorCode:
            raise TypeError("code must be an exact StartupChannelErrorCode")
        self.code = code
        super().__init__(f"sidecar startup channel failed ({code})")


class _ChannelFailure(Exception):
    def __init__(self, code: StartupChannelErrorCode) -> None:
        self.code = code
        super().__init__()


def _exact_int(value: object, name: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an exact built-in int")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} is outside its supported range")
    return value


def _startup_id(value: object) -> str:
    if type(value) is not str:
        raise TypeError("startup_id must be an exact built-in str")
    if len(value) != 32 or any(character not in _STARTUP_ID_CHARACTERS for character in value):
        raise ValueError("startup_id must be an exact lowercase hex identifier")
    return value


@dataclass(frozen=True, slots=True)
class StartupReady:
    """Exact identity scalars emitted only after child startup completes."""

    startup_id: str
    sidecar_pid: int
    port: int

    def __post_init__(self) -> None:
        _startup_id(self.startup_id)
        _exact_int(self.sidecar_pid, "sidecar_pid", minimum=1, maximum=2**31 - 1)
        _exact_int(self.port, "port", minimum=1, maximum=65_535)


@dataclass(frozen=True, slots=True)
class StartupFailure:
    """One safe generic failure emitted instead of raw startup details."""

    code: StartupFailureCode

    def __post_init__(self) -> None:
        if type(self.code) is not StartupFailureCode:
            raise TypeError("code must be an exact StartupFailureCode")


type StartupMessage = StartupReady | StartupFailure


def _public_failure(code: StartupChannelErrorCode) -> StartupChannelError:
    return StartupChannelError(code)


def _raise_public(code: StartupChannelErrorCode) -> NoReturn:
    raise _public_failure(code) from None


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_MESSAGE_INVALID)
        result[key] = value
    return result


def _reject_constant(_value: str) -> NoReturn:
    raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_MESSAGE_INVALID)


def _trusted_message(message: object) -> StartupMessage:
    if type(message) is StartupReady:
        return StartupReady(
            startup_id=message.startup_id,
            sidecar_pid=message.sidecar_pid,
            port=message.port,
        )
    if type(message) is StartupFailure:
        return StartupFailure(code=message.code)
    raise TypeError("message must be an exact startup message")


def _encode_message(message: StartupMessage) -> bytes:
    if type(message) is StartupReady:
        value: dict[str, object] = {
            "startup_channel_schema_version": STARTUP_CHANNEL_SCHEMA_VERSION,
            "status": "ready",
            "startup_id": message.startup_id,
            "sidecar_pid": message.sidecar_pid,
            "port": message.port,
        }
    else:
        failure = cast(StartupFailure, message)
        value = {
            "startup_channel_schema_version": STARTUP_CHANNEL_SCHEMA_VERSION,
            "status": "error",
            "code": str(failure.code),
        }
    encoded = (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )
    if len(encoded) > MAX_STARTUP_SIGNAL_BYTES:
        raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_MESSAGE_TOO_LARGE)
    return encoded


def _decode_message(frame: bytes) -> StartupMessage:
    if len(frame) > MAX_STARTUP_SIGNAL_BYTES:
        raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_MESSAGE_TOO_LARGE)
    if not frame.endswith(b"\n") or frame.count(b"\n") != 1:
        raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_MESSAGE_INVALID)
    try:
        text = frame[:-1].decode("utf-8")
        decoded = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except _ChannelFailure:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_MESSAGE_INVALID) from None
    if type(decoded) is not dict:
        raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_MESSAGE_INVALID)
    value: dict[str, object] = decoded
    version = value.get("startup_channel_schema_version")
    status = value.get("status")
    try:
        if type(version) is not int or version != STARTUP_CHANNEL_SCHEMA_VERSION:
            raise ValueError
        if type(status) is not str:
            raise ValueError
        if status == "ready" and set(value) == _READY_FIELDS:
            raw_startup_id = value["startup_id"]
            raw_sidecar_pid = value["sidecar_pid"]
            raw_port = value["port"]
            if (
                type(raw_startup_id) is not str
                or type(raw_sidecar_pid) is not int
                or type(raw_port) is not int
            ):
                raise ValueError
            message: StartupMessage = StartupReady(
                startup_id=raw_startup_id,
                sidecar_pid=raw_sidecar_pid,
                port=raw_port,
            )
        elif status == "error" and set(value) == _FAILURE_FIELDS:
            raw_code = value["code"]
            if type(raw_code) is not str:
                raise ValueError
            message = StartupFailure(code=StartupFailureCode(raw_code))
        else:
            raise ValueError
    except (TypeError, ValueError):
        raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_MESSAGE_INVALID) from None
    if _encode_message(message) != frame:
        raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_MESSAGE_INVALID)
    return message


def _validate_timeout(timeout: object) -> float:
    if type(timeout) not in {int, float}:
        raise TypeError("timeout must be a built-in int or float")
    if type(timeout) is int:
        integer_timeout = timeout
        if integer_timeout <= 0 or integer_timeout > MAX_STARTUP_CHANNEL_TIMEOUT_SECONDS:
            raise ValueError("timeout must be finite, positive, and at most 30 seconds")
        return float(integer_timeout)
    normalized = cast(float, timeout)
    if (
        not math.isfinite(normalized)
        or normalized <= 0.0
        or normalized > MAX_STARTUP_CHANNEL_TIMEOUT_SECONDS
    ):
        raise ValueError("timeout must be finite, positive, and at most 30 seconds")
    return normalized


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0.0:
        raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_TIMEOUT)
    return remaining


def _finish_descriptors(
    active_error: BaseException | None,
    *descriptors: int,
) -> None:
    cleanup_failed = False
    cleanup_control: BaseException | None = None
    cleanup_control_followup_failed = False
    seen: set[int] = set()
    for descriptor in descriptors:
        if descriptor < 0 or descriptor in seen:
            continue
        seen.add(descriptor)
        try:
            os.close(descriptor)
        except BaseException as error:
            if isinstance(error, Exception):
                cleanup_failed = True
            elif cleanup_control is None:
                cleanup_control = error
            else:
                cleanup_control_followup_failed = True
    if active_error is not None and not isinstance(active_error, Exception):
        if cleanup_failed or cleanup_control is not None:
            active_error.add_note("sidecar startup channel cleanup failed")
        return
    if cleanup_control is not None:
        if cleanup_failed or cleanup_control_followup_failed:
            cleanup_control.add_note("sidecar startup channel cleanup failed")
        raise cleanup_control
    if cleanup_failed:
        raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_CLEANUP_FAILED)


def _descriptor_is_endpoint(descriptor: int, access_mode: int) -> bool:
    metadata = os.fstat(descriptor)
    flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
    pipe_buffer = os.fpathconf(descriptor, "PC_PIPE_BUF")
    return (
        stat.S_ISFIFO(metadata.st_mode)
        and flags & os.O_ACCMODE == access_mode
        and type(pipe_buffer) is int
        and pipe_buffer >= MAX_STARTUP_SIGNAL_BYTES
    )


def _configure_endpoint(descriptor: int, access_mode: int) -> None:
    os.set_inheritable(descriptor, False)
    os.set_blocking(descriptor, False)
    if os.get_inheritable(descriptor) or os.get_blocking(descriptor):
        raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_DESCRIPTOR_INVALID)
    if not _descriptor_is_endpoint(descriptor, access_mode):
        raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_DESCRIPTOR_INVALID)


def _prepare_endpoint(descriptor: int, access_mode: int) -> None:
    if not _descriptor_is_endpoint(descriptor, access_mode):
        raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_DESCRIPTOR_INVALID)
    _configure_endpoint(descriptor, access_mode)


def _promote_owned_descriptor(descriptors: list[int], index: int) -> None:
    descriptor = descriptors[index]
    if descriptor >= _MIN_CHANNEL_DESCRIPTOR:
        return
    promoted = fcntl.fcntl(
        descriptor,
        fcntl.F_DUPFD_CLOEXEC,
        _MIN_CHANNEL_DESCRIPTOR,
    )
    if type(promoted) is not int or promoted < _MIN_CHANNEL_DESCRIPTOR:
        raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_SETUP_FAILED)
    descriptors[index] = promoted
    source = descriptor
    _finish_descriptors(None, source)


def _open_channel() -> tuple[StartupReader, StartupWriter]:
    owned_descriptors = [-1, -1]
    try:
        opened: object = os.pipe()
        if type(opened) is tuple or type(opened) is list:
            owned_descriptors[:] = [item for item in opened if type(item) is int and item >= 0]
        if (
            type(opened) is not tuple
            or len(opened) != 2
            or type(opened[0]) is not int
            or type(opened[1]) is not int
            or opened[0] < 0
            or opened[1] < 0
        ):
            raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_SETUP_FAILED)
        owned_descriptors[:] = opened
        if owned_descriptors[0] == owned_descriptors[1]:
            owned_descriptors[1] = -1
            raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_SETUP_FAILED)
        _promote_owned_descriptor(owned_descriptors, 0)
        _promote_owned_descriptor(owned_descriptors, 1)
        if owned_descriptors[0] == owned_descriptors[1]:
            owned_descriptors[1] = -1
            raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_SETUP_FAILED)
        _prepare_endpoint(owned_descriptors[0], os.O_RDONLY)
        _prepare_endpoint(owned_descriptors[1], os.O_WRONLY)
        reader = StartupReader._from_descriptor(owned_descriptors[0])
        writer = StartupWriter._from_descriptor(owned_descriptors[1])
        owned_descriptors[:] = [-1, -1]
        return reader, writer
    except _ChannelFailure:
        raise
    except Exception:
        raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_SETUP_FAILED) from None
    finally:
        _finish_descriptors(sys.exception(), *owned_descriptors)


def _adopt_writer(file_descriptor: int) -> StartupWriter:
    owned_descriptor = file_descriptor
    try:
        try:
            os.fstat(owned_descriptor)
        except OSError as error:
            if error.errno == errno.EBADF:
                owned_descriptor = -1
            raise
        if not _descriptor_is_endpoint(owned_descriptor, os.O_WRONLY):
            raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_DESCRIPTOR_INVALID)
        _configure_endpoint(owned_descriptor, os.O_WRONLY)
        writer = StartupWriter._from_descriptor(owned_descriptor)
        owned_descriptor = -1
        return writer
    except _ChannelFailure:
        raise
    except Exception:
        raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_DESCRIPTOR_INVALID) from None
    finally:
        _finish_descriptors(sys.exception(), owned_descriptor)


def _read_message(file_descriptor: int, deadline: float) -> StartupMessage:
    data = bytearray()
    while True:
        if not _poll_readable(file_descriptor, _remaining(deadline)):
            raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_TIMEOUT)
        remaining_capacity = MAX_STARTUP_SIGNAL_BYTES + 1 - len(data)
        chunk = os.read(file_descriptor, remaining_capacity)
        if type(chunk) is not bytes:
            raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_READ_FAILED)
        if not chunk:
            if not data:
                raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_CLOSED)
            message = _decode_message(bytes(data))
            _remaining(deadline)
            return message
        data.extend(chunk)
        if len(data) > MAX_STARTUP_SIGNAL_BYTES:
            raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_MESSAGE_TOO_LARGE)


def _poll_readable(file_descriptor: int, timeout: float) -> bool:
    poller = select.poll()
    poller.register(file_descriptor, select.POLLIN | select.POLLHUP | select.POLLERR)
    events = poller.poll(math.ceil(timeout * 1000.0))
    if type(events) is not list:
        raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_READ_FAILED)
    if not events:
        return False
    if len(events) != 1:
        raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_READ_FAILED)
    event = events[0]
    if (
        type(event) is not tuple
        or len(event) != 2
        or type(event[0]) is not int
        or event[0] != file_descriptor
        or type(event[1]) is not int
        or event[1] == 0
    ):
        raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_READ_FAILED)
    return True


def _close_handle(file_descriptor: int) -> None:
    failure_code: StartupChannelErrorCode | None = None
    try:
        _finish_descriptors(None, file_descriptor)
    except _ChannelFailure as failure:
        failure_code = failure.code
    if failure_code is not None:
        _raise_public(failure_code)


def _finish_context_descriptor(
    active_error: BaseException | None,
    file_descriptor: int,
) -> StartupChannelErrorCode | None:
    try:
        os.close(file_descriptor)
    except BaseException as cleanup_error:
        if active_error is not None and not isinstance(active_error, Exception):
            active_error.add_note("sidecar startup channel cleanup failed")
            return None
        if not isinstance(cleanup_error, Exception):
            raise
        if active_error is not None:
            active_error.add_note("sidecar startup channel cleanup failed")
            return None
        return StartupChannelErrorCode.STARTUP_CHANNEL_CLEANUP_FAILED
    return None


class StartupReader:
    """Move-only owner of one startup-channel read endpoint."""

    __slots__ = ("_descriptor",)
    _descriptor: int

    def __init__(self) -> None:
        raise TypeError("StartupReader must be opened by open_startup_channel")

    @classmethod
    def _from_descriptor(cls, file_descriptor: int) -> StartupReader:
        result = object.__new__(cls)
        result._descriptor = file_descriptor
        return result

    def __repr__(self) -> str:
        state = "active" if self._descriptor >= 0 else "closed"
        return f"<StartupReader {state}>"

    def __copy__(self) -> NoReturn:
        raise TypeError("StartupReader is move-only")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("StartupReader is move-only")

    def __reduce__(self) -> NoReturn:
        raise TypeError("StartupReader is move-only")

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        del protocol
        raise TypeError("StartupReader is move-only")

    def fileno(self) -> int:
        if self._descriptor < 0:
            _raise_public(StartupChannelErrorCode.STARTUP_CHANNEL_CLOSED)
        return self._descriptor

    def receive(self, timeout: float = 0.5) -> StartupMessage:
        normalized_timeout = _validate_timeout(timeout)
        file_descriptor = self.fileno()
        self._descriptor = -1
        failure_code: StartupChannelErrorCode | None = None
        result: StartupMessage | None = None
        try:
            deadline = time.monotonic() + normalized_timeout
            _prepare_endpoint(file_descriptor, os.O_RDONLY)
            result = _read_message(file_descriptor, deadline)
        except _ChannelFailure as failure:
            failure_code = failure.code
        except Exception:
            failure_code = StartupChannelErrorCode.STARTUP_CHANNEL_READ_FAILED
        finally:
            try:
                _finish_descriptors(sys.exception(), file_descriptor)
            except _ChannelFailure as failure:
                failure_code = failure.code
        if failure_code is not None:
            _raise_public(failure_code)
        assert result is not None
        return result

    def close(self) -> None:
        file_descriptor = self._descriptor
        if file_descriptor < 0:
            return
        self._descriptor = -1
        _close_handle(file_descriptor)

    def __enter__(self) -> StartupReader:
        self.fileno()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del exception_type, traceback
        file_descriptor = self._descriptor
        if file_descriptor < 0:
            return False
        self._descriptor = -1
        failure_code = _finish_context_descriptor(exception, file_descriptor)
        if failure_code is not None:
            _raise_public(failure_code)
        return False


class StartupWriter:
    """Move-only owner of one startup-channel write endpoint."""

    __slots__ = ("_descriptor",)
    _descriptor: int

    def __init__(self) -> None:
        raise TypeError("StartupWriter must be opened or adopted")

    @classmethod
    def _from_descriptor(cls, file_descriptor: int) -> StartupWriter:
        result = object.__new__(cls)
        result._descriptor = file_descriptor
        return result

    @classmethod
    def adopt_inherited(cls, file_descriptor: int) -> StartupWriter:
        if type(file_descriptor) is not int:
            raise TypeError("file_descriptor must be an exact built-in int")
        if file_descriptor < _MIN_CHANNEL_DESCRIPTOR:
            raise ValueError("file_descriptor must be at least 3")
        failure_code: StartupChannelErrorCode | None = None
        try:
            return _adopt_writer(file_descriptor)
        except _ChannelFailure as failure:
            failure_code = failure.code
        _raise_public(failure_code)

    def __repr__(self) -> str:
        state = "active" if self._descriptor >= 0 else "closed"
        return f"<StartupWriter {state}>"

    def __copy__(self) -> NoReturn:
        raise TypeError("StartupWriter is move-only")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("StartupWriter is move-only")

    def __reduce__(self) -> NoReturn:
        raise TypeError("StartupWriter is move-only")

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        del protocol
        raise TypeError("StartupWriter is move-only")

    def fileno(self) -> int:
        if self._descriptor < 0:
            _raise_public(StartupChannelErrorCode.STARTUP_CHANNEL_CLOSED)
        return self._descriptor

    def send(self, message: StartupMessage) -> None:
        trusted_message = _trusted_message(message)
        encoding_failure: StartupChannelErrorCode | None = None
        try:
            encoded = _encode_message(trusted_message)
        except _ChannelFailure as failure:
            encoding_failure = failure.code
        except Exception:
            encoding_failure = StartupChannelErrorCode.STARTUP_CHANNEL_WRITE_FAILED
        if encoding_failure is not None:
            _raise_public(encoding_failure)
        file_descriptor = self.fileno()
        self._descriptor = -1
        failure_code: StartupChannelErrorCode | None = None
        try:
            _prepare_endpoint(file_descriptor, os.O_WRONLY)
            written = os.write(file_descriptor, encoded)
            if type(written) is not int or written != len(encoded):
                raise _ChannelFailure(StartupChannelErrorCode.STARTUP_CHANNEL_WRITE_FAILED)
        except _ChannelFailure as failure:
            failure_code = failure.code
        except Exception:
            failure_code = StartupChannelErrorCode.STARTUP_CHANNEL_WRITE_FAILED
        finally:
            try:
                _finish_descriptors(sys.exception(), file_descriptor)
            except _ChannelFailure as failure:
                failure_code = failure.code
        if failure_code is not None:
            _raise_public(failure_code)

    def close(self) -> None:
        file_descriptor = self._descriptor
        if file_descriptor < 0:
            return
        self._descriptor = -1
        _close_handle(file_descriptor)

    def __enter__(self) -> StartupWriter:
        self.fileno()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del exception_type, traceback
        file_descriptor = self._descriptor
        if file_descriptor < 0:
            return False
        self._descriptor = -1
        failure_code = _finish_context_descriptor(exception, file_descriptor)
        if failure_code is not None:
            _raise_public(failure_code)
        return False


def open_startup_channel() -> tuple[StartupReader, StartupWriter]:
    """Open one bounded, one-shot, nonblocking startup signal pipe."""

    failure_code: StartupChannelErrorCode | None = None
    try:
        return _open_channel()
    except _ChannelFailure as failure:
        failure_code = failure.code
    _raise_public(failure_code)
