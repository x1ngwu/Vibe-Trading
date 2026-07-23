"""QE0 contract tests for the versioned quant-engine JSONL protocol."""

from __future__ import annotations

from copy import deepcopy

import pytest

from src.quant_engine.protocol import (
    EngineIdentity,
    ProtocolError,
    build_request,
    canonical_json,
    strict_json_loads,
    validate_request,
    validate_response,
)


ENGINE = EngineIdentity("fake", "a" * 40)


def _request() -> dict[str, object]:
    return build_request(
        request_id="qe0.protocol-1",
        engine=ENGINE,
        operation="capabilities",
        payload={"z": 1, "a": [True, None]},
    )


def test_canonical_request_is_stable_and_round_trips() -> None:
    first = _request()
    second = build_request(
        request_id="qe0.protocol-1",
        engine=ENGINE,
        operation="capabilities",
        payload={"a": [True, None], "z": 1},
    )

    assert canonical_json(first) == canonical_json(second)
    assert first["content_sha256"] == second["content_sha256"]
    assert validate_request(first, expected_engine=ENGINE) == first


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("schema_version", "2.0", "PROTOCOL_MISMATCH"),
        ("request_id", "../escape", "INVALID_SCHEMA"),
        ("operation", "Backtest", "INVALID_SCHEMA"),
        ("content_sha256", "0" * 64, "HASH_MISMATCH"),
    ],
)
def test_request_negotiation_fails_closed(field: str, value: object, code: str) -> None:
    request = _request()
    request[field] = value

    with pytest.raises(ProtocolError) as raised:
        validate_request(request, expected_engine=ENGINE)

    assert raised.value.code == code


def test_request_rejects_unknown_fields_before_execution() -> None:
    request = _request()
    request["future_field"] = True

    with pytest.raises(ProtocolError) as raised:
        validate_request(request, expected_engine=ENGINE)

    assert raised.value.code == "INVALID_SCHEMA"


def test_engine_commit_mismatch_fails_closed() -> None:
    request = _request()

    with pytest.raises(ProtocolError) as raised:
        validate_request(request, expected_engine=EngineIdentity("fake", "b" * 40))

    assert raised.value.code == "ENGINE_MISMATCH"


def test_response_must_bind_to_exact_request_and_engine() -> None:
    request = _request()
    response = {
        "protocol": "vibe.quant-engine.jsonl",
        "schema_version": "1.0",
        "request_id": request["request_id"],
        "engine": ENGINE.as_dict(),
        "request_sha256": request["content_sha256"],
        "status": "ok",
        "result": {"operations": {}},
        "error": None,
    }
    assert validate_response(response, request=request, expected_engine=ENGINE) == response

    wrong = deepcopy(response)
    wrong["request_sha256"] = "0" * 64
    with pytest.raises(ProtocolError) as raised:
        validate_response(wrong, request=request, expected_engine=ENGINE)
    assert raised.value.code == "REQUEST_MISMATCH"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_json_is_rejected(value: float) -> None:
    with pytest.raises(ProtocolError) as raised:
        canonical_json({"value": value})
    assert raised.value.code == "INVALID_JSON_VALUE"


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_strict_decoder_rejects_non_finite_response_json(token: str) -> None:
    with pytest.raises(ProtocolError) as raised:
        strict_json_loads(f'{{"result":{{"value":{token}}}}}')

    assert raised.value.code == "INVALID_JSON_VALUE"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("location", ["result", "error"])
def test_response_rejects_non_finite_values_anywhere(value: float, location: str) -> None:
    request = _request()
    response = {
        "protocol": "vibe.quant-engine.jsonl",
        "schema_version": "1.0",
        "request_id": request["request_id"],
        "engine": ENGINE.as_dict(),
        "request_sha256": request["content_sha256"],
        "status": "ok",
        "result": {"value": value},
        "error": None,
    }
    if location == "error":
        response.update(
            status="error",
            result=None,
            error={"code": "NON_FINITE", "message": value},
        )

    with pytest.raises(ProtocolError) as raised:
        validate_response(response, request=request, expected_engine=ENGINE)

    assert raised.value.code == "INVALID_JSON_VALUE"
