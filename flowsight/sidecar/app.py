"""Authenticated, bounded ASGI boundary for the local FlowSight sidecar."""

from __future__ import annotations

import json
import mimetypes
import secrets
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Final, cast

from fastapi import FastAPI, Request, Response
from starlette.types import Message, Receive, Scope, Send

from flowsight.sidecar.state import SidecarState

MAX_PRIVATE_BODY_BYTES: Final = 1024 * 1024

_MAX_PRIVATE_BODY_MESSAGES: Final = 1024
_INTERNAL_PREFIX: Final = "/internal/v1"
_BROWSER_PREFIX: Final = "/api/v1"
_SAFE_BROWSER_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
_MAX_CONTENT_LENGTH_DIGITS: Final = 20
_TOKEN_CHARACTERS: Final = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
_STATIC_CACHE_CONTROL: Final = "no-store"


@dataclass(frozen=True, slots=True)
class _BundledFile:
    name: str
    body: bytes
    media_type: str


@dataclass(frozen=True, slots=True)
class _BundledUI:
    index: _BundledFile
    assets: tuple[_BundledFile, ...]

    def find_asset(self, name: str) -> _BundledFile | None:
        for asset in self.assets:
            if asset.name == name:
                return asset
        return None


def _regular_bundle_bytes(static_root: Path, path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("bundled UI contains a non-regular file")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise RuntimeError("bundled UI path is invalid") from None
    if not resolved.is_relative_to(static_root):
        raise RuntimeError("bundled UI path escaped its package root")
    try:
        return path.read_bytes()
    except OSError:
        raise RuntimeError("bundled UI could not be read") from None


def _bundle_media_type(path: Path) -> str:
    media_type, _encoding = mimetypes.guess_type(path.name)
    if media_type is None:
        return "application/octet-stream"
    return media_type


def _load_bundled_ui() -> _BundledUI:
    package_static = Path(str(files("flowsight").joinpath("static")))
    if package_static.is_symlink() or not package_static.is_dir():
        raise RuntimeError("bundled UI package directory is invalid")
    try:
        static_root = package_static.resolve(strict=True)
    except (OSError, RuntimeError):
        raise RuntimeError("bundled UI package directory is invalid") from None

    index_path = package_static / "index.html"
    assets_path = package_static / "assets"
    if assets_path.is_symlink() or not assets_path.is_dir():
        raise RuntimeError("bundled UI asset directory is invalid")

    try:
        asset_paths = sorted(assets_path.iterdir(), key=lambda item: item.name)
    except OSError:
        raise RuntimeError("bundled UI asset directory could not be read") from None
    if not asset_paths:
        raise RuntimeError("bundled UI asset directory is empty")

    assets: list[_BundledFile] = []
    for asset_path in asset_paths:
        if asset_path.name in {".", ".."} or "/" in asset_path.name or "\\" in asset_path.name:
            raise RuntimeError("bundled UI asset name is invalid")
        assets.append(
            _BundledFile(
                name=asset_path.name,
                body=_regular_bundle_bytes(static_root, asset_path),
                media_type=_bundle_media_type(asset_path),
            )
        )

    return _BundledUI(
        index=_BundledFile(
            name="index.html",
            body=_regular_bundle_bytes(static_root, index_path),
            media_type="text/html",
        ),
        assets=tuple(assets),
    )


def _bundled_response(file: _BundledFile, method: str) -> Response:
    body = b"" if method == "HEAD" else file.body
    return Response(
        content=body,
        media_type=file.media_type,
        headers={
            "cache-control": _STATIC_CACHE_CONTROL,
            "content-length": str(len(file.body)),
            "x-content-type-options": "nosniff",
        },
    )


def _bundled_not_found() -> Response:
    return Response(
        status_code=404,
        headers={
            "cache-control": _STATIC_CACHE_CONTROL,
            "content-length": "0",
            "x-content-type-options": "nosniff",
        },
    )


def _private_namespace(path: str) -> str | None:
    if path == _INTERNAL_PREFIX or path.startswith(f"{_INTERNAL_PREFIX}/"):
        return "internal"
    if path == _BROWSER_PREFIX or path.startswith(f"{_BROWSER_PREFIX}/"):
        return "browser"
    return None


def _parse_headers(scope: Scope) -> dict[bytes, tuple[bytes, ...]] | None:
    raw_headers = scope.get("headers")
    if type(raw_headers) not in {list, tuple}:
        return None
    header_items = cast(list[object] | tuple[object, ...], raw_headers)

    grouped: dict[bytes, list[bytes]] = {}
    for raw_header in header_items:
        if type(raw_header) not in {list, tuple}:
            return None
        header_pair = cast(list[object] | tuple[object, ...], raw_header)
        if len(header_pair) != 2:
            return None
        raw_name, raw_value = header_pair
        if type(raw_name) is not bytes or type(raw_value) is not bytes or not raw_name:
            return None
        name = raw_name
        value = raw_value
        grouped.setdefault(name.lower(), []).append(value)
    return {name: tuple(values) for name, values in grouped.items()}


def _single_exact_header(
    headers: dict[bytes, tuple[bytes, ...]],
    name: bytes,
    expected: bytes,
) -> bool:
    values = headers.get(name, ())
    return len(values) == 1 and secrets.compare_digest(values[0], expected)


def _without_cors_headers(value: object) -> list[tuple[bytes, bytes]] | None:
    if type(value) not in {list, tuple}:
        return None
    header_items = cast(list[object] | tuple[object, ...], value)
    result: list[tuple[bytes, bytes]] = []
    for raw_header in header_items:
        if type(raw_header) not in {list, tuple}:
            return None
        header_pair = cast(list[object] | tuple[object, ...], raw_header)
        if len(header_pair) != 2:
            return None
        raw_name, raw_value = header_pair
        if type(raw_name) is not bytes or type(raw_value) is not bytes or not raw_name:
            return None
        if raw_name.lower().startswith(b"access-control-"):
            continue
        result.append((raw_name, raw_value))
    return result


def _content_length(
    headers: dict[bytes, tuple[bytes, ...]],
) -> tuple[int | None, tuple[int, str] | None]:
    if headers.get(b"transfer-encoding"):
        return None, (400, "REQUEST_FRAMING_INVALID")
    values = headers.get(b"content-length", ())
    if len(values) > 1:
        return None, (400, "CONTENT_LENGTH_INVALID")
    if not values:
        return None, None

    encoded = values[0]
    if not encoded or len(encoded) > _MAX_CONTENT_LENGTH_DIGITS or not encoded.isdigit():
        return None, (400, "CONTENT_LENGTH_INVALID")
    normalized = encoded.lstrip(b"0") or b"0"
    maximum = str(MAX_PRIVATE_BODY_BYTES).encode("ascii")
    if len(normalized) > len(maximum) or (len(normalized) == len(maximum) and normalized > maximum):
        return None, (413, "BODY_TOO_LARGE")
    return int(normalized), None


class _PrivateSidecarFastAPI(FastAPI):
    """Enforce the private boundary before FastAPI parses a request body."""

    def __init__(self, state: SidecarState) -> None:
        super().__init__(docs_url=None, redoc_url=None, openapi_url=None)
        self.router.redirect_slashes = False
        if not state.token.isascii() or any(
            character not in _TOKEN_CHARACTERS for character in state.token
        ):
            raise ValueError("state token is not HTTP bearer-safe")
        self._expected_host = state.authority.encode("ascii")
        self._expected_authorization = f"Bearer {state.token}".encode()
        self._expected_origin = state.origin.encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await super().__call__(scope, receive, send)
            return

        path = scope.get("path")
        method = scope.get("method")
        if type(path) is not str or type(method) is not str:
            await self._send_fault(send, 400, "REQUEST_INVALID")
            return
        headers = _parse_headers(scope)
        if headers is None:
            await self._send_fault(send, 400, "HEADERS_INVALID")
            return
        if not _single_exact_header(headers, b"host", self._expected_host):
            await self._send_fault(send, 403, "HOST_REJECTED")
            return

        response_rejected = False

        async def send_without_cors(message: Message) -> None:
            nonlocal response_rejected
            if response_rejected:
                return
            if message.get("type") == "http.response.start":
                filtered_headers = _without_cors_headers(message.get("headers", []))
                if filtered_headers is None:
                    response_rejected = True
                    await self._send_fault(send, 500, "RESPONSE_HEADERS_INVALID")
                    return
                message = {**message, "headers": filtered_headers}
            await send(message)

        namespace = _private_namespace(path)
        if namespace is None:
            await super().__call__(scope, receive, send_without_cors)
            return
        if not _single_exact_header(
            headers,
            b"authorization",
            self._expected_authorization,
        ):
            await self._send_fault(send, 401, "AUTH_REJECTED")
            return
        if namespace == "browser" and method.upper() not in _SAFE_BROWSER_METHODS:
            if not _single_exact_header(headers, b"origin", self._expected_origin):
                await self._send_fault(send, 403, "ORIGIN_REJECTED")
                return

        declared_length, length_fault = _content_length(headers)
        if length_fault is not None:
            await self._send_fault(send, *length_fault)
            return

        body = bytearray()
        for _message_index in range(_MAX_PRIVATE_BODY_MESSAGES):
            message = await receive()
            if type(message) is not dict:
                await self._send_fault(send, 400, "BODY_INVALID")
                return
            message_type = message.get("type")
            if message_type == "http.disconnect":
                await self._send_fault(send, 400, "BODY_INCOMPLETE")
                return
            if message_type != "http.request":
                await self._send_fault(send, 400, "BODY_INVALID")
                return
            chunk = message.get("body", b"")
            more_body = message.get("more_body", False)
            if type(chunk) is not bytes or type(more_body) is not bool:
                await self._send_fault(send, 400, "BODY_INVALID")
                return
            if len(body) + len(chunk) > MAX_PRIVATE_BODY_BYTES:
                await self._send_fault(send, 413, "BODY_TOO_LARGE")
                return
            body.extend(chunk)
            if not more_body:
                break
        else:
            await self._send_fault(send, 400, "BODY_MESSAGE_LIMIT")
            return

        if declared_length is not None and len(body) != declared_length:
            await self._send_fault(send, 400, "BODY_LENGTH_MISMATCH")
            return

        delivered = False

        async def replay_body() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {
                "type": "http.request",
                "body": bytes(body),
                "more_body": False,
            }

        await super().__call__(scope, replay_body, send_without_cors)

    @staticmethod
    async def _send_fault(send: Send, status: int, code: str) -> None:
        encoded = json.dumps(
            {"detail": {"code": code}},
            separators=(",", ":"),
        ).encode("ascii")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(encoded)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": encoded})


def create_sidecar_app(state: SidecarState) -> FastAPI:
    """Build the private ASGI boundary and read-only bundled UI."""

    if type(state) is not SidecarState:
        raise TypeError("state must be an exact SidecarState")

    bundled_ui = _load_bundled_ui()
    app = _PrivateSidecarFastAPI(state)
    health_payload: dict[str, object] = {
        "status": "ok",
        "protocol_version": state.protocol_version,
        "state_schema_version": state.state_schema_version,
        "project_id": state.project_id,
        "startup_id": state.startup_id,
        "sidecar_pid": state.pid,
        "host": state.host,
        "port": state.port,
    }

    @app.get("/internal/v1/health", include_in_schema=False)
    async def health() -> dict[str, object]:
        return dict(health_payload)

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def ui_index(request: Request) -> Response:
        return _bundled_response(bundled_ui.index, request.method)

    @app.api_route(
        "/assets/{asset_path:path}",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    async def ui_asset(asset_path: str, request: Request) -> Response:
        if not asset_path or asset_path in {".", ".."} or "/" in asset_path or "\\" in asset_path:
            return _bundled_not_found()
        asset = bundled_ui.find_asset(asset_path)
        if asset is None:
            return _bundled_not_found()
        return _bundled_response(asset, request.method)

    return app
