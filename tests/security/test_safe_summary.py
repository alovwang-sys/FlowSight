from __future__ import annotations

import gc
import importlib
import json
import subprocess
import sys
import weakref
from dataclasses import fields

import pytest

from flowsight.security import REDACTED, SafeSummary, SummaryLimits, safe_summary


def _decoded(summary: SafeSummary) -> dict[str, object]:
    decoded = json.loads(summary.payload_json)
    assert type(decoded) is dict
    return decoded


def _data(summary: SafeSummary) -> dict[str, object]:
    data = _decoded(summary)["data"]
    assert type(data) is dict
    return data


@pytest.mark.parametrize(
    "name",
    [
        "password",
        "DB_PASSWD",
        "pwd_hash",
        "id_token",
        "clientSecret",
        "signing_key",
        "Authorization",
        "oauth2_auth",
        "credentials",
        "Set-Cookie",
        "session_id",
        "api_key",
        "access-token",
        "refreshToken",
    ],
)
def test_secret_like_names_redact_the_entire_value(name: str) -> None:
    raw = "fixture-secret-that-must-not-survive"

    result = safe_summary({name: raw})

    assert _data(result)[name] == REDACTED
    assert raw not in result.payload_json
    assert result.redacted_count == 1


def test_sensitive_ancestor_is_redacted_without_traversing_its_value() -> None:
    calls: list[str] = []

    class Dangerous:
        def __getattribute__(self, name: str) -> object:
            calls.append(f"getattribute:{name}")
            raise AssertionError(name)

        def __repr__(self) -> str:
            calls.append("repr")
            raise AssertionError("repr")

        def __iter__(self):
            calls.append("iter")
            raise AssertionError("iter")

    result = safe_summary({"user": {"credentials": Dangerous()}})

    assert _data(result)["user"] == {"credentials": REDACTED}
    assert calls == []


@pytest.mark.parametrize(
    "raw",
    [
        "Bearer fixture-token._~+/=",
        "bearer\tfixture-token-with-tabs",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123",
        "eyJhbGciOiJub25lIn0.eyJzdWIiOiIxMjMifQ.",
        "eyJlbmMiOiJBMTI4R0NNIn0..aXYxMjM0NTY.Y2lwaGVydGV4dA.dGFnMTIzNA",
        "Alice+tag@sub.example.co.uk",
        "+1 (415) 555-2671",
        "+86 138 0013 8000",
        "123-45-6789",
        "4111 1111 1111 1111",
        "378282246310005",
        "sk-fixtureabcdefghijkl",
        "sk_live-fixtureabcdefghijkl",
        "ghp_fixtureabcdefghijkl",
        "github_pat_fixture_abcdefghijkl",
        "AKIAABCDEFGHIJKLMNOP",
        "AIzaFixtureabcdefghijkl",
    ],
)
def test_sensitive_string_content_redacts_the_whole_string(raw: str) -> None:
    result = safe_summary({"message": f"prefix {raw} suffix"})

    assert _data(result)["message"] == REDACTED
    assert raw not in result.payload_json
    assert result.redacted_count == 1


@pytest.mark.parametrize(
    "text",
    [
        "password='fixture-password'",
        "api_key=fixture-api-key",
        "X-API-Key: fixture-header-key",
        "token=fixture-token",
        "session: fixture-session",
    ],
)
def test_secret_assignments_inside_generic_text_are_redacted(text: str) -> None:
    result = safe_summary({"statement": f"failure context {text}"})

    assert _data(result)["statement"] == REDACTED
    assert text not in result.payload_json


@pytest.mark.parametrize(
    ("shape", "payload", "raw"),
    [
        (
            "query",
            {"query": {"q": "ok", "access_token": "query-fixture-secret"}},
            "query-fixture-secret",
        ),
        (
            "header",
            {"headers": {"Authorization": "Bearer header-fixture-token"}},
            "header-fixture-token",
        ),
        (
            "sql",
            {"sql": {"statement": "SELECT 1 WHERE password='sql-fixture-secret'"}},
            "sql-fixture-secret",
        ),
        (
            "exception",
            {"exception": {"message": "failure for person@example.com"}},
            "person@example.com",
        ),
        (
            "args",
            {"args": {"username": "safe", "password": "args-fixture-secret"}},
            "args-fixture-secret",
        ),
        (
            "return",
            {"return": {"contact": "return-person@example.com"}},
            "return-person@example.com",
        ),
        (
            "span-event",
            {"span_event": {"attributes": {"session_id": "event-fixture-secret"}}},
            "event-fixture-secret",
        ),
        (
            "snapshot",
            {"snapshot": {"order_id": 7, "credential": "snapshot-fixture-secret"}},
            "snapshot-fixture-secret",
        ),
    ],
)
def test_every_future_payload_shape_uses_the_same_pre_queue_primitive(
    shape: str,
    payload: dict[str, object],
    raw: str,
) -> None:
    result = safe_summary(payload)

    assert raw not in result.payload_json, shape
    assert REDACTED in result.payload_json, shape
    assert result.redacted_count >= 1


def test_default_limits_freeze_the_phase_zero_contract() -> None:
    limits = SummaryLimits()

    assert limits.max_depth == 3
    assert limits.max_items_per_container == 50
    assert limits.max_total_items == 200
    assert limits.max_value_bytes == 1024
    assert limits.max_summary_bytes == 4096
    assert limits.max_total_bytes == 16 * 1024


def test_depth_limit_has_an_isolated_deterministic_boundary() -> None:
    limits = SummaryLimits(
        max_depth=1,
        max_items_per_container=5,
        max_total_items=20,
        max_value_bytes=64,
        max_summary_bytes=512,
        max_total_bytes=2048,
    )
    payload = {"deep": {"level": {"value": "hidden-by-depth"}}}

    first = safe_summary(payload, limits=limits)
    second = safe_summary(payload, limits=limits)

    assert first.payload_json == second.payload_json
    assert _data(first)["deep"]["level"]["$truncated"] == "max_depth"
    assert "max_depth" in first.payload_json


def test_per_container_item_limit_keeps_exactly_the_allowed_prefix() -> None:
    limits = SummaryLimits(
        max_items_per_container=2,
        max_total_items=10,
        max_value_bytes=64,
        max_summary_bytes=512,
        max_total_bytes=2048,
    )

    result = safe_summary({"items": [1, 2, 3]}, limits=limits)

    assert _data(result)["items"] == [1, 2]
    assert "max_items_per_container" in result.payload_json


def test_aggregate_item_budget_stops_nested_small_containers() -> None:
    limits = SummaryLimits(
        max_items_per_container=4,
        max_total_items=4,
        max_value_bytes=64,
        max_summary_bytes=512,
        max_total_bytes=2048,
    )

    result = safe_summary({"groups": [[1, 2], [3, 4], [5, 6]]}, limits=limits)

    assert "max_total_items" in result.payload_json
    assert result.truncated is True


def test_cycle_is_reported_without_confusing_it_with_other_limits() -> None:
    cycle: dict[str, object] = {}
    cycle["self"] = cycle

    result = safe_summary({"cycle": cycle})

    assert _data(result)["cycle"]["self"]["$truncated"] == "cycle"
    assert "max_depth" not in result.payload_json


def test_value_limit_uses_utf8_bytes_at_the_exact_boundary() -> None:
    limits = SummaryLimits(
        max_items_per_container=4,
        max_total_items=8,
        max_value_bytes=32,
        max_summary_bytes=256,
        max_total_bytes=1024,
    )
    exact = "é" * 16
    over = "é" * 17

    result = safe_summary({"exact": exact, "over": over}, limits=limits)
    data = _data(result)

    assert data["exact"] == exact
    assert type(data["over"]) is str
    assert len(data["over"].encode("utf-8")) <= limits.max_value_bytes
    assert over not in result.payload_json
    assert "max_value_bytes" in result.payload_json


def test_truncated_sensitive_prefix_is_redacted_instead_of_leaked() -> None:
    limits = SummaryLimits(
        max_items_per_container=2,
        max_total_items=4,
        max_value_bytes=32,
        max_summary_bytes=256,
        max_total_bytes=1024,
    )
    value = f"{'x' * 23}alice@example.com"

    result = safe_summary({"message": value}, limits=limits)

    assert _data(result)["message"] == REDACTED
    assert "alice@" not in result.payload_json


def test_per_summary_limit_accepts_exact_boundary_and_rejects_one_byte_over() -> None:
    limits = SummaryLimits(
        max_items_per_container=5,
        max_total_items=20,
        max_value_bytes=96,
        max_summary_bytes=96,
        max_total_bytes=1024,
    )
    result = safe_summary({"exact": "x" * 94, "over": "y" * 95}, limits=limits)
    data = _data(result)

    assert data["exact"] == "x" * 94
    assert data["over"] == {"$truncated": "max_summary_bytes"}


def test_total_envelope_limit_accepts_exact_boundary_and_rejects_one_byte_over() -> None:
    payload = {f"field_{index}": "z" * 100 for index in range(4)}
    baseline_limits = SummaryLimits(
        max_items_per_container=10,
        max_total_items=20,
        max_value_bytes=128,
        max_summary_bytes=256,
        max_total_bytes=2048,
    )
    baseline = safe_summary(payload, limits=baseline_limits)
    assert baseline.payload_bytes > 256

    exact_limits = SummaryLimits(
        max_items_per_container=10,
        max_total_items=20,
        max_value_bytes=128,
        max_summary_bytes=256,
        max_total_bytes=baseline.payload_bytes,
    )
    over_limits = SummaryLimits(
        max_items_per_container=10,
        max_total_items=20,
        max_value_bytes=128,
        max_summary_bytes=256,
        max_total_bytes=baseline.payload_bytes - 1,
    )

    exact = safe_summary(payload, limits=exact_limits)
    over = safe_summary(payload, limits=over_limits)

    assert exact.payload_json == baseline.payload_json
    assert exact.payload_bytes == exact_limits.max_total_bytes
    assert over.payload_bytes <= over_limits.max_total_bytes
    assert _data(over) == {"$truncated": "max_total_bytes"}


def test_exact_builtin_scalars_and_containers_produce_only_json_safe_values() -> None:
    result = safe_summary(
        {
            "none": None,
            "boolean": True,
            "integer": 42,
            "big_integer": 10**100,
            "float": 1.25,
            "non_finite": float("nan"),
            "complex": complex(1.0, 2.0),
            "text": "safe text",
            "bytes": b"binary-fixture-secret",
            "bytearray": bytearray(b"mutable-fixture-secret"),
            "range": range(4),
            "list": [1, "two"],
            "tuple": (3, "four"),
            "mapping": {"safe": 5},
            "set": {1, 2},
            "frozenset": frozenset({3, 4}),
        }
    )
    decoded = _decoded(result)

    assert "binary-fixture-secret" not in result.payload_json
    assert "mutable-fixture-secret" not in result.payload_json
    assert decoded == json.loads(json.dumps(decoded, allow_nan=False))


def test_subclasses_and_unknown_objects_are_type_only_without_user_code() -> None:
    calls: list[str] = []

    class DangerousMeta(type):
        def __getattribute__(cls, name: str) -> object:
            calls.append(f"metaclass:{name}")
            raise AssertionError(name)

    class Dangerous(metaclass=DangerousMeta):
        @property
        def property_value(self) -> object:
            calls.append("property")
            raise AssertionError("property")

        def __getattribute__(self, name: str) -> object:
            calls.append(f"getattribute:{name}")
            raise AssertionError(name)

        def __iter__(self):
            calls.append("iter")
            raise AssertionError("iter")

        def __repr__(self) -> str:
            calls.append("repr")
            raise AssertionError("repr")

        def model_dump(self) -> object:
            calls.append("model_dump")
            raise AssertionError("model_dump")

        def to_json(self) -> object:
            calls.append("to_json")
            raise AssertionError("to_json")

    class DangerousList(list[object]):
        def __iter__(self):
            calls.append("list_iter")
            raise AssertionError("list_iter")

        def __repr__(self) -> str:
            calls.append("list_repr")
            raise AssertionError("list_repr")

    dangerous = Dangerous()
    dangerous_list = DangerousList(["must-not-be-expanded"])
    calls.clear()

    result = safe_summary({"object": dangerous, "subclass": dangerous_list})
    data = _data(result)

    assert calls == []
    assert data["object"]["$uninspected"] is True
    assert data["subclass"]["$uninspected"] is True
    assert "must-not-be-expanded" not in result.payload_json


def test_unknown_mapping_key_is_never_stringified_and_forces_value_redaction() -> None:
    calls: list[str] = []

    class DangerousKey:
        def __hash__(self) -> int:
            calls.append("hash")
            return 7

        def __repr__(self) -> str:
            calls.append("repr")
            raise AssertionError("repr")

        def __str__(self) -> str:
            calls.append("str")
            raise AssertionError("str")

    key = DangerousKey()
    payload = {key: "unknown-key-fixture-secret"}
    calls.clear()

    result = safe_summary(payload)  # type: ignore[arg-type]

    assert calls == []
    assert "unknown-key-fixture-secret" not in result.payload_json
    assert REDACTED in result.payload_json


@pytest.mark.parametrize(
    "raw_key",
    [
        "person@example.com",
        "Bearer fixture-key-token",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123",
    ],
)
def test_sensitive_string_mapping_keys_are_replaced_before_persistence(raw_key: str) -> None:
    result = safe_summary({raw_key: "value-behind-sensitive-key"})

    assert raw_key not in result.payload_json
    assert "value-behind-sensitive-key" not in result.payload_json
    assert "REDACTED_KEY" in result.payload_json


def test_hostile_metaclass_descriptor_is_not_invoked_for_type_labels() -> None:
    calls: list[str] = []

    def explode(_instance: object) -> object:
        calls.append("metaclass_descriptor")
        raise AssertionError("metaclass descriptor")

    hostile_meta = type("HostileMeta", (type,), {"__module__": property(explode)})
    hostile_type = hostile_meta("HostileValue", (), {})
    hostile_value = hostile_type()
    calls.clear()

    result = safe_summary({"value": hostile_value})

    assert calls == []
    assert _data(result)["value"]["$uninspected"] is True


@pytest.mark.parametrize(
    "module_name",
    [
        "Bearer fixture-type-token",
        "person@example.com",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123",
        "token_fixture_secret",
        "api_key_fixture_secret",
        "x" * 100_000,
    ],
)
def test_type_metadata_is_bounded_and_cannot_persist_sensitive_content(module_name: str) -> None:
    dynamic_type = type("DynamicValue", (), {"__module__": module_name})
    result = safe_summary({"value": dynamic_type()})

    assert module_name not in result.payload_json
    assert _data(result)["value"]["$type"] == "unknown"


@pytest.mark.parametrize("type_name", ["token_fixture_secret", "api_key_fixture_secret"])
def test_sensitive_type_names_are_not_persisted(type_name: str) -> None:
    dynamic_type = type(type_name, (), {"__module__": "safe_module"})

    result = safe_summary({"value": dynamic_type()})

    assert type_name not in result.payload_json
    assert _data(result)["value"]["$type"] == "unknown"


def test_conversion_exceptions_fail_open_without_retaining_error_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    safe_summary_module = importlib.import_module("flowsight.security.safe_summary")

    def fail_conversion(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("raw conversion failure detail")

    monkeypatch.setattr(safe_summary_module, "_summarize_value", fail_conversion)
    result = safe_summary({"value": "raw-input-that-must-not-survive"})

    assert _data(result)["value"] == {"$truncated": "conversion_failed"}
    assert "raw conversion failure detail" not in result.payload_json
    assert "raw-input-that-must-not-survive" not in result.payload_json


def test_root_conversion_failure_returns_a_fixed_safe_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    safe_summary_module = importlib.import_module("flowsight.security.safe_summary")

    def fail_root(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("raw root conversion detail")

    monkeypatch.setattr(safe_summary_module, "_summarize_mapping_entries", fail_root)
    result = safe_summary({"value": "raw-root-input"})

    assert _data(result) == {"$truncated": "conversion_failed"}
    assert "raw root conversion detail" not in result.payload_json
    assert "raw-root-input" not in result.payload_json


def test_process_control_base_exceptions_are_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    safe_summary_module = importlib.import_module("flowsight.security.safe_summary")

    class ProcessStop(BaseException):
        pass

    def stop_conversion(*_args: object, **_kwargs: object) -> object:
        raise ProcessStop

    monkeypatch.setattr(safe_summary_module, "_summarize_value", stop_conversion)
    with pytest.raises(ProcessStop):
        safe_summary({"value": "safe"})


def test_cycle_detection_does_not_call_the_audited_builtin_id() -> None:
    script = """
from flowsight.security import safe_summary
import sys

def deny_id(event, _args):
    if event == "builtins.id":
        raise RuntimeError("audited id call")

sys.addaudithook(deny_id)
payload = {"items": []}
payload["items"].append(payload)
safe_summary(payload)
"""

    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)


def test_exception_text_and_raw_objects_are_not_retained_after_return() -> None:
    class Ephemeral:
        pass

    raw_object = Ephemeral()
    reference = weakref.ref(raw_object)
    raw_exception = RuntimeError("raw-exception-fixture-secret")
    payload = {"object": raw_object, "exception_object": raw_exception}

    result = safe_summary(payload)
    del payload
    del raw_exception
    del raw_object
    gc.collect()

    assert reference() is None
    assert "raw-exception-fixture-secret" not in result.payload_json
    assert {field.name for field in fields(result)} == {
        "payload_json",
        "payload_bytes",
        "redacted_count",
        "truncation_count",
        "truncated",
    }
    assert all(type(getattr(result, field.name)) in {str, int, bool} for field in fields(result))


def test_dict_subclass_is_rejected_without_invoking_its_methods() -> None:
    calls: list[str] = []

    class DangerousDict(dict[str, object]):
        def __iter__(self):
            calls.append("iter")
            raise AssertionError("iter")

        def __repr__(self) -> str:
            calls.append("repr")
            raise AssertionError("repr")

    payload = DangerousDict(value="fixture")
    calls.clear()

    with pytest.raises(TypeError, match="exact built-in dict"):
        safe_summary(payload)

    assert calls == []


@pytest.mark.parametrize(
    "limits",
    [
        SummaryLimits(max_items_per_container=2, max_total_items=2),
        SummaryLimits(max_value_bytes=96, max_summary_bytes=96),
    ],
)
def test_valid_custom_limits_are_stable(limits: SummaryLimits) -> None:
    payload = {"alpha": [1, 2, 3], "beta": "b" * 200}

    assert safe_summary(payload, limits=limits) == safe_summary(payload, limits=limits)


def test_invalid_limits_are_rejected_before_summarization() -> None:
    with pytest.raises(ValueError, match="max_total_items"):
        SummaryLimits(max_items_per_container=3, max_total_items=2)
    with pytest.raises(ValueError, match="byte limits"):
        SummaryLimits(max_value_bytes=512, max_summary_bytes=256)
    with pytest.raises(ValueError, match="built-in int"):
        SummaryLimits(max_depth=True)  # type: ignore[arg-type]
