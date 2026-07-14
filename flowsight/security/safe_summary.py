"""Side-effect-free, bounded summaries for future runtime event payloads."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Protocol, cast

REDACTED = "<REDACTED>"
_TRUNCATED = "<TRUNCATED>"
_MAX_PATH_BYTES = 256
_MAX_KEY_BYTES = 128
_MAX_TYPE_BYTES = 128
_MAX_SECRET_NAME_CHARS = 256
_SIGNED_64_MIN = -(2**63)
_SIGNED_64_MAX = 2**63 - 1

_SECRET_NAME_PARTS = (
    "password",
    "passwd",
    "pwd",
    "token",
    "secret",
    "key",
    "auth",
    "credential",
    "cookie",
    "session",
    "api_key",
    "access_token",
    "refresh_token",
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?:password|passwd|pwd|token|secret|key|auth|credential|cookie|session)"
    r"[A-Za-z0-9_.-]*\s*[:=]",
    re.IGNORECASE,
)
_CONTENT_REDACTORS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(
        r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{5,}\."
        r"[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]*(?![A-Za-z0-9_-])"
    ),
    re.compile(
        r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{5,}\."
        r"[A-Za-z0-9_-]*\.[A-Za-z0-9_-]{2,}\."
        r"[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}(?![A-Za-z0-9_-])"
    ),
    re.compile(
        r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\."
        r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
    ),
    re.compile(
        r"(?<![A-Za-z0-9])(?:sk(?:_live)?[-_][A-Za-z0-9_-]{12,}|"
        r"ghp_[A-Za-z0-9]{12,}|github_pat_[A-Za-z0-9_]{12,}|"
        r"AKIA[0-9A-Z]{16}|AIza[A-Za-z0-9_-]{12,})(?![A-Za-z0-9])"
    ),
    re.compile(r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
    re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"),
    re.compile(r"(?<![\w])(?:\+?\d[\d ().-]{6,}\d)(?![\w])"),
)
_PARTIAL_SENSITIVE_RE = re.compile(
    r"(?:@|Bearer\s|sk[-_]|sk_live-|ghp_|github_pat_|AKIA|AIza|"
    r"[A-Za-z0-9_-]{8,}\.|(?:\d[ -]?){6,})",
    re.IGNORECASE,
)
_SAFE_TYPE_PART_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class _TypeStringDescriptor(Protocol):
    def __get__(self, instance: object, owner: object | None = None, /) -> object: ...


_TYPE_MODULE_DESCRIPTOR = cast(
    _TypeStringDescriptor,
    type.__dict__["__module__"],
)
_TYPE_QUALNAME_DESCRIPTOR = cast(
    _TypeStringDescriptor,
    type.__dict__["__qualname__"],
)


@dataclass(frozen=True, slots=True)
class SummaryLimits:
    """Deterministic limits applied before a summary can enter an SDK queue."""

    max_depth: int = 3
    max_items_per_container: int = 50
    max_total_items: int = 200
    max_value_bytes: int = 1024
    max_summary_bytes: int = 4096
    max_total_bytes: int = 16 * 1024
    max_report_items: int = 32

    def __post_init__(self) -> None:
        integer_fields = (
            ("max_depth", self.max_depth, 0),
            ("max_items_per_container", self.max_items_per_container, 1),
            ("max_total_items", self.max_total_items, 1),
            ("max_value_bytes", self.max_value_bytes, 32),
            ("max_summary_bytes", self.max_summary_bytes, 96),
            ("max_total_bytes", self.max_total_bytes, 256),
            ("max_report_items", self.max_report_items, 1),
        )
        for name, value, minimum in integer_fields:
            if type(value) is not int or value < minimum:
                raise ValueError(
                    f"{name} must be a built-in int greater than or equal to {minimum}"
                )
        if not self.max_value_bytes <= self.max_summary_bytes <= self.max_total_bytes:
            raise ValueError(
                "byte limits must satisfy max_value_bytes <= max_summary_bytes <= max_total_bytes"
            )
        if self.max_total_items < self.max_items_per_container:
            raise ValueError(
                "max_total_items must be greater than or equal to max_items_per_container"
            )


DEFAULT_LIMITS = SummaryLimits()


@dataclass(frozen=True, slots=True)
class SafeSummary:
    """An immutable, already-serialized envelope that retains no input objects."""

    payload_json: str
    payload_bytes: int
    redacted_count: int
    truncation_count: int
    truncated: bool


class _Context:
    __slots__ = (
        "active_containers",
        "limits",
        "redacted_count",
        "redacted_paths",
        "truncation_count",
        "truncation_reasons",
        "total_items_exhausted",
        "visited_items",
    )

    def __init__(self, limits: SummaryLimits) -> None:
        self.limits = limits
        self.active_containers: list[object] = []
        self.redacted_count = 0
        self.redacted_paths: list[str] = []
        self.truncation_count = 0
        self.truncation_reasons: list[str] = []
        self.total_items_exhausted = False
        self.visited_items = 0

    def record_redaction(self, path: str) -> None:
        self.redacted_count += 1
        if len(self.redacted_paths) < self.limits.max_report_items:
            self.redacted_paths.append(path)

    def record_truncation(self, path: str, reason: str) -> None:
        self.truncation_count += 1
        if len(self.truncation_reasons) < self.limits.max_report_items:
            self.truncation_reasons.append(f"{reason}:{path}")

    def consume_item(self, path: str) -> bool:
        if self.visited_items >= self.limits.max_total_items:
            if not self.total_items_exhausted:
                self.record_truncation(path, "max_total_items")
                self.total_items_exhausted = True
            return False
        self.visited_items += 1
        return True

    def is_active(self, value: object) -> bool:
        return any(active is value for active in self.active_containers)


def safe_summary(
    payload: dict[str, object],
    *,
    limits: SummaryLimits = DEFAULT_LIMITS,
) -> SafeSummary:
    """Return a bounded JSON envelope without invoking code on payload values.

    The trusted event-conversion layer must provide an exact built-in ``dict``.
    Keys and values inside it remain untrusted. The returned object contains
    only strings, integers, and booleans; no input reference survives the call.
    """

    if type(payload) is not dict:
        raise TypeError("payload must be an exact built-in dict")
    if type(limits) is not SummaryLimits:
        raise TypeError("limits must be an exact SummaryLimits")

    context = _Context(limits)
    data: dict[str, JsonValue] = {}
    context.active_containers.append(payload)
    try:
        try:
            _summarize_mapping_entries(
                cast(dict[object, object], payload),
                data,
                path="$",
                depth=0,
                context=context,
                enforce_per_summary=True,
            )
        except Exception:
            context.record_truncation("$", "conversion_failed")
            data = {"$truncated": "conversion_failed"}
    finally:
        context.active_containers.clear()

    envelope = _build_envelope(data, context)
    payload_json = _encode_json(envelope)
    payload_bytes = _utf8_size(payload_json)
    if payload_bytes > limits.max_total_bytes:
        context.record_truncation("$", "max_total_bytes")
        data = {"$truncated": "max_total_bytes"}
        redaction_paths = len(context.redacted_paths)
        truncation_reasons = len(context.truncation_reasons)
        while True:
            envelope = _build_envelope(
                data,
                context,
                redaction_path_limit=redaction_paths,
                truncation_reason_limit=truncation_reasons,
            )
            payload_json = _encode_json(envelope)
            payload_bytes = _utf8_size(payload_json)
            if payload_bytes <= limits.max_total_bytes:
                break
            if redaction_paths > 0:
                redaction_paths -= 1
            elif truncation_reasons > 0:
                truncation_reasons -= 1
            else:
                payload_json = _encode_json(
                    {
                        "data": {},
                        "redaction": {"count": context.redacted_count},
                        "truncated": True,
                        "truncation": {"count": context.truncation_count},
                    }
                )
                payload_bytes = _utf8_size(payload_json)
                break

    return SafeSummary(
        payload_json=payload_json,
        payload_bytes=payload_bytes,
        redacted_count=context.redacted_count,
        truncation_count=context.truncation_count,
        truncated=context.truncation_count > 0,
    )


def _summarize_mapping_entries(
    value: dict[object, object],
    output: dict[str, JsonValue],
    *,
    path: str,
    depth: int,
    context: _Context,
    enforce_per_summary: bool,
) -> None:
    if len(value) > context.limits.max_items_per_container:
        context.record_truncation(path, "max_items_per_container")

    for index, (raw_key, raw_value) in enumerate(dict.items(value)):
        if index >= context.limits.max_items_per_container:
            break
        if not context.consume_item(path):
            break
        key, key_is_sensitive, key_was_truncated = _safe_key(raw_key, index)
        if key in output:
            key = f"{key}#{index}"
            key_was_truncated = True
        child_path = _child_path(path, key)
        if key_was_truncated:
            context.record_truncation(child_path, "mapping_key")
        try:
            summarized = _summarize_value(
                raw_value,
                path=child_path,
                depth=depth,
                sensitive=key_is_sensitive,
                context=context,
            )
        except Exception:
            context.record_truncation(child_path, "conversion_failed")
            summarized = {"$truncated": "conversion_failed"}
        if enforce_per_summary and _json_size(summarized) > context.limits.max_summary_bytes:
            context.record_truncation(child_path, "max_summary_bytes")
            summarized = {"$truncated": "max_summary_bytes"}
        output[key] = summarized


def _summarize_value(
    value: object,
    *,
    path: str,
    depth: int,
    sensitive: bool,
    context: _Context,
) -> JsonValue:
    if sensitive:
        context.record_redaction(path)
        return REDACTED

    value_type = type(value)
    if value is None:
        return None
    if value_type is bool:
        return cast(bool, value)
    if value_type is int:
        integer = cast(int, value)
        if _SIGNED_64_MIN <= integer <= _SIGNED_64_MAX:
            return integer
        context.record_truncation(path, "integer_bits")
        return {
            "$bits": int.bit_length(integer),
            "$truncated": True,
            "$type": "builtins.int",
        }
    if value_type is float:
        float_value = cast(float, value)
        return float_value if math.isfinite(float_value) else "<NON_FINITE_FLOAT>"
    if value_type is complex:
        complex_value = cast(complex, value)
        return {
            "$type": "builtins.complex",
            "imag": _finite_float_or_marker(complex_value.imag),
            "real": _finite_float_or_marker(complex_value.real),
        }
    if value_type is str:
        return _summarize_string(cast(str, value), path=path, context=context)
    if value_type is bytes:
        return {"$size": len(cast(bytes, value)), "$type": "builtins.bytes"}
    if value_type is bytearray:
        return {"$size": len(cast(bytearray, value)), "$type": "builtins.bytearray"}
    if value_type is range:
        try:
            size: JsonValue = len(cast(range, value))
        except OverflowError:
            size = "<TOO_LARGE>"
        return {"$size": size, "$type": "builtins.range"}
    if value_type is list:
        return _summarize_sequence(
            cast(list[object], value), path=path, depth=depth, context=context
        )
    if value_type is tuple:
        items = _summarize_sequence(
            cast(tuple[object, ...], value), path=path, depth=depth, context=context
        )
        return {"$items": items, "$type": "builtins.tuple"}
    if value_type is dict:
        return _summarize_mapping(
            cast(dict[object, object], value), path=path, depth=depth, context=context
        )
    if value_type is set or value_type is frozenset:
        size = len(cast(set[object] | frozenset[object], value))
        context.record_truncation(path, "unordered_items_omitted")
        type_name = "builtins.set" if value_type is set else "builtins.frozenset"
        return {"$size": size, "$type": type_name}

    context.record_truncation(path, "uninspected_type")
    return {"$type": _safe_type_label(value), "$uninspected": True}


def _summarize_sequence(
    value: list[object] | tuple[object, ...],
    *,
    path: str,
    depth: int,
    context: _Context,
) -> list[JsonValue] | dict[str, JsonValue]:
    if depth >= context.limits.max_depth:
        context.record_truncation(path, "max_depth")
        return {"$truncated": "max_depth", "$type": _safe_type_label(value)}
    if context.is_active(value):
        context.record_truncation(path, "cycle")
        return {"$truncated": "cycle", "$type": _safe_type_label(value)}

    length = len(value)
    if length > context.limits.max_items_per_container:
        context.record_truncation(path, "max_items_per_container")
    item_count = min(length, context.limits.max_items_per_container)
    output: list[JsonValue] = []
    context.active_containers.append(value)
    try:
        for index in range(item_count):
            if not context.consume_item(path):
                break
            item_path = f"{path}[{index}]"
            try:
                item = (
                    list.__getitem__(value, index)
                    if type(value) is list
                    else tuple.__getitem__(cast(tuple[object, ...], value), index)
                )
                output.append(
                    _summarize_value(
                        item,
                        path=item_path,
                        depth=depth + 1,
                        sensitive=False,
                        context=context,
                    )
                )
            except Exception:
                context.record_truncation(item_path, "conversion_failed")
                output.append({"$truncated": "conversion_failed"})
                break
    finally:
        context.active_containers.pop()
    return output


def _summarize_mapping(
    value: dict[object, object],
    *,
    path: str,
    depth: int,
    context: _Context,
) -> JsonValue:
    if depth >= context.limits.max_depth:
        context.record_truncation(path, "max_depth")
        return {"$truncated": "max_depth", "$type": "builtins.dict"}
    if context.is_active(value):
        context.record_truncation(path, "cycle")
        return {"$truncated": "cycle", "$type": "builtins.dict"}

    output: dict[str, JsonValue] = {}
    context.active_containers.append(value)
    try:
        _summarize_mapping_entries(
            value,
            output,
            path=path,
            depth=depth + 1,
            context=context,
            enforce_per_summary=False,
        )
    finally:
        context.active_containers.pop()
    return output


def _summarize_string(value: str, *, path: str, context: _Context) -> str:
    scan_sample = value[: context.limits.max_value_bytes + 1]
    limited, was_truncated = _limit_text(value, context.limits.max_value_bytes)
    if was_truncated:
        context.record_truncation(path, "max_value_bytes")
    if _scan_has_sensitive_content(scan_sample, truncated=was_truncated):
        context.record_redaction(path)
        return REDACTED
    return limited


def _safe_key(raw_key: object, index: int) -> tuple[str, bool, bool]:
    if type(raw_key) is not str:
        return f"<{_safe_type_label(raw_key)}:key:{index}>", True, True
    key = raw_key
    sensitive = len(key) > _MAX_SECRET_NAME_CHARS or _is_secret_name(key)
    limited, truncated = _limit_text(key, _MAX_KEY_BYTES)
    scan_sample = key[: _MAX_KEY_BYTES + 1]
    if _scan_has_sensitive_content(scan_sample, truncated=truncated):
        return f"<REDACTED_KEY:{index}>", True, truncated
    return limited, sensitive or truncated, truncated


def _is_secret_name(name: str) -> bool:
    folded = str.casefold(name)
    return any(part in folded for part in _SECRET_NAME_PARTS)


def _safe_type_label(value: object) -> str:
    value_type = type(value)
    try:
        module = _TYPE_MODULE_DESCRIPTOR.__get__(value_type, type)
        qualname = _TYPE_QUALNAME_DESCRIPTOR.__get__(value_type, type)
    except Exception:
        return "unknown"
    if type(module) is not str or type(qualname) is not str:
        return "unknown"
    if len(module) > _MAX_TYPE_BYTES or len(qualname) > _MAX_TYPE_BYTES:
        return "unknown"
    if (
        _SAFE_TYPE_PART_RE.fullmatch(module) is None
        or _SAFE_TYPE_PART_RE.fullmatch(qualname) is None
    ):
        return "unknown"
    if _is_secret_name(module) or _is_secret_name(qualname):
        return "unknown"
    if _scan_has_sensitive_content(module, truncated=False) or _scan_has_sensitive_content(
        qualname, truncated=False
    ):
        return "unknown"
    label, _ = _limit_text(f"{module}.{qualname}", _MAX_TYPE_BYTES)
    return label


def _scan_has_sensitive_content(value: str, *, truncated: bool) -> bool:
    if _SECRET_ASSIGNMENT_RE.search(value) is not None:
        return True
    if any(pattern.search(value) is not None for pattern in _CONTENT_REDACTORS):
        return True
    return truncated and _PARTIAL_SENSITIVE_RE.search(value) is not None


def _child_path(path: str, key: str) -> str:
    child, _ = _limit_text(f"{path}.{key}", _MAX_PATH_BYTES)
    return child


def _limit_text(value: str, max_bytes: int) -> tuple[str, bool]:
    sample = value[: max_bytes + 1]
    encoded = sample.encode("utf-8", errors="replace")
    truncated = len(value) > len(sample) or len(encoded) > max_bytes
    if not truncated:
        return encoded.decode("utf-8"), False
    marker_bytes = _TRUNCATED.encode("ascii")
    available = max_bytes - len(marker_bytes)
    prefix = encoded[:available].decode("utf-8", errors="ignore")
    return f"{prefix}{_TRUNCATED}", True


def _finite_float_or_marker(value: float) -> JsonValue:
    return value if math.isfinite(value) else "<NON_FINITE_FLOAT>"


def _build_envelope(
    data: dict[str, JsonValue],
    context: _Context,
    *,
    redaction_path_limit: int | None = None,
    truncation_reason_limit: int | None = None,
) -> dict[str, JsonValue]:
    paths = context.redacted_paths[
        : len(context.redacted_paths) if redaction_path_limit is None else redaction_path_limit
    ]
    reasons = context.truncation_reasons[
        : len(context.truncation_reasons)
        if truncation_reason_limit is None
        else truncation_reason_limit
    ]
    return {
        "data": data,
        "redaction": {"count": context.redacted_count, "paths": list(paths)},
        "truncated": context.truncation_count > 0,
        "truncation": {"count": context.truncation_count, "reasons": list(reasons)},
    }


def _encode_json(value: JsonValue) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _json_size(value: JsonValue) -> int:
    return _utf8_size(_encode_json(value))


def _utf8_size(value: str) -> int:
    return len(value.encode("utf-8"))
