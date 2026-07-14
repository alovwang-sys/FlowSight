"""One bounded proof that an exact published sidecar startup is healthy."""

from __future__ import annotations

import http.client
import json
import math
import select
import socket
import sys
import time
from typing import Any, Final, NoReturn, cast

from .state import SidecarState

MAX_HEALTH_RESPONSE_BYTES: Final = 4096
MAX_HEALTH_PROBE_TIMEOUT_SECONDS: Final = 30.0

_HEALTH_TARGET: Final = "/internal/v1/health"
_MAX_RESPONSE_HEADER_BYTES: Final = 8192
_MAX_RESPONSE_HEADERS: Final = 64
_HEADER_TERMINATOR: Final = b"\r\n\r\n"
_TOKEN_CHARACTERS: Final = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
_HEADER_NAME_CHARACTERS: Final = frozenset(
    b"!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)
_EXPECTED_FIELDS: Final = frozenset(
    {
        "status",
        "protocol_version",
        "state_schema_version",
        "project_id",
        "startup_id",
        "sidecar_pid",
        "host",
        "port",
    }
)


class _InvalidHealthResponse(ValueError):
    pass


def _validate_inputs(state: object, timeout: object) -> tuple[SidecarState, float]:
    if type(state) is not SidecarState:
        raise TypeError("state must be an exact SidecarState")
    trusted_state = SidecarState.from_wire(state.to_wire())
    if type(timeout) not in {int, float}:
        raise TypeError("timeout must be a built-in int or float")
    if type(timeout) is int:
        integer_timeout = timeout
        if integer_timeout <= 0 or integer_timeout > MAX_HEALTH_PROBE_TIMEOUT_SECONDS:
            raise ValueError("timeout must be finite, positive, and at most 30 seconds")
        normalized_timeout = float(integer_timeout)
    else:
        normalized_timeout = cast(float, timeout)
        if (
            not math.isfinite(normalized_timeout)
            or normalized_timeout <= 0.0
            or normalized_timeout > MAX_HEALTH_PROBE_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout must be finite, positive, and at most 30 seconds")
    if not trusted_state.token.isascii() or any(
        character not in _TOKEN_CHARACTERS for character in trusted_state.token
    ):
        raise ValueError("state token is not HTTP bearer-safe")
    return trusted_state, normalized_timeout


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidHealthResponse
        result[key] = value
    return result


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0.0:
        raise TimeoutError
    return remaining


def _invalid_response() -> NoReturn:
    raise _InvalidHealthResponse


def _valid_field_text(value: bytes) -> bool:
    return all(character == 9 or 32 <= character <= 126 or character >= 128 for character in value)


def _parse_response_head(encoded: bytes) -> tuple[int, int]:
    if len(encoded) > _MAX_RESPONSE_HEADER_BYTES:
        _invalid_response()
    lines = encoded.split(b"\r\n")
    if not lines or len(lines) - 1 > _MAX_RESPONSE_HEADERS:
        _invalid_response()
    status_parts = lines[0].split(b" ", 2)
    if (
        len(status_parts) != 3
        or status_parts[0] not in {b"HTTP/1.0", b"HTTP/1.1"}
        or len(status_parts[1]) != 3
        or not status_parts[1].isdigit()
        or not _valid_field_text(status_parts[2])
    ):
        _invalid_response()
    status = int(status_parts[1])
    if not 100 <= status <= 599:
        _invalid_response()

    headers: dict[bytes, list[bytes]] = {}
    for line in lines[1:]:
        if not line or line[:1] in {b" ", b"\t"} or b":" not in line:
            _invalid_response()
        name, value = line.split(b":", 1)
        if (
            not name
            or any(character not in _HEADER_NAME_CHARACTERS for character in name)
            or not _valid_field_text(value)
        ):
            _invalid_response()
        headers.setdefault(name.lower(), []).append(value.strip(b" \t"))

    if headers.get(b"transfer-encoding"):
        _invalid_response()
    content_lengths = headers.get(b"content-length", [])
    if len(content_lengths) != 1:
        _invalid_response()
    encoded_length = content_lengths[0]
    if (
        not encoded_length
        or not encoded_length.isdigit()
        or len(encoded_length) > len(str(MAX_HEALTH_RESPONSE_BYTES))
    ):
        _invalid_response()
    content_length = int(encoded_length)
    if content_length > MAX_HEALTH_RESPONSE_BYTES:
        _invalid_response()
    content_types = headers.get(b"content-type", [])
    if len(content_types) != 1 or content_types[0].lower() != b"application/json":
        _invalid_response()
    return status, content_length


def _read_response(connection_socket: socket.socket, deadline: float) -> tuple[int, bytes]:
    encoded = bytearray()
    body_offset: int | None = None
    expected_total: int | None = None
    status = 0
    connection_socket.setblocking(False)
    while True:
        if expected_total is not None:
            if len(encoded) >= expected_total:
                return status, bytes(encoded[cast(int, body_offset) : expected_total])

        readable, _, _ = select.select(
            [connection_socket],
            [],
            [],
            _remaining(deadline),
        )
        if not readable:
            raise TimeoutError
        maximum_total = (
            _MAX_RESPONSE_HEADER_BYTES + len(_HEADER_TERMINATOR) + MAX_HEALTH_RESPONSE_BYTES
        )
        remaining_capacity = maximum_total + 1 - len(encoded)
        if expected_total is not None:
            remaining_capacity = min(remaining_capacity, expected_total - len(encoded))
        chunk = connection_socket.recv(min(4096, remaining_capacity))
        if not chunk:
            _invalid_response()
        encoded.extend(chunk)

        if body_offset is None:
            header_end = encoded.find(_HEADER_TERMINATOR)
            if header_end < 0:
                if len(encoded) > (_MAX_RESPONSE_HEADER_BYTES + len(_HEADER_TERMINATOR) - 1):
                    _invalid_response()
                continue
            if header_end > _MAX_RESPONSE_HEADER_BYTES:
                _invalid_response()
            status, content_length = _parse_response_head(bytes(encoded[:header_end]))
            body_offset = header_end + len(_HEADER_TERMINATOR)
            expected_total = body_offset + content_length


def _decode_response(encoded: bytes) -> object:
    decoded_text = encoded.decode("utf-8")
    return json.loads(decoded_text, object_pairs_hook=_strict_object)


def _matches_exact_startup(value: object, state: SidecarState) -> bool:
    if type(value) is not dict or set(value) != _EXPECTED_FIELDS:
        return False
    response: dict[str, object] = value
    expected: dict[str, object] = {
        "status": "ok",
        "protocol_version": state.protocol_version,
        "state_schema_version": state.state_schema_version,
        "project_id": state.project_id,
        "startup_id": state.startup_id,
        "sidecar_pid": state.pid,
        "host": state.host,
        "port": state.port,
    }
    return all(
        type(response[name]) is type(expected_value) and response[name] == expected_value
        for name, expected_value in expected.items()
    )


def _finish_connection(
    active_error: BaseException | None,
    connection: http.client.HTTPConnection | None,
) -> bool:
    if connection is None:
        return False
    try:
        connection.close()
    except BaseException as error:
        if active_error is not None and not isinstance(active_error, Exception):
            active_error.add_note("sidecar health probe cleanup failed")
            return True
        if not isinstance(error, Exception):
            raise
        return True
    return False


def probe_sidecar_health(state: SidecarState, timeout: float = 0.5) -> bool:
    """Return whether one authenticated request proves this exact startup."""

    trusted_state, normalized_timeout = _validate_inputs(state, timeout)
    deadline = time.monotonic() + normalized_timeout
    connection: http.client.HTTPConnection | None = None
    healthy = False
    cleanup_failed = False
    try:
        connection = http.client.HTTPConnection(
            trusted_state.host,
            trusted_state.port,
            timeout=_remaining(deadline),
        )
        connection.set_debuglevel(0)
        connection.connect()
        connection_socket = connection.sock
        if connection_socket is None:
            raise OSError
        connection_socket.settimeout(_remaining(deadline))
        connection.putrequest(
            "GET",
            _HEALTH_TARGET,
            skip_host=True,
            skip_accept_encoding=True,
        )
        connection.putheader("Host", trusted_state.authority)
        connection.putheader("Authorization", f"Bearer {trusted_state.token}")
        connection.putheader("Content-Length", "0")
        connection.endheaders()
        status, encoded = _read_response(connection_socket, deadline)
        decoded = _decode_response(encoded)
        healthy = (
            type(status) is int and status == 200 and _matches_exact_startup(decoded, trusted_state)
        )
        _remaining(deadline)
    except Exception:
        healthy = False
    finally:
        connection_to_close = connection
        connection = None
        cleanup_failed = _finish_connection(sys.exception(), connection_to_close)
    return healthy and not cleanup_failed
