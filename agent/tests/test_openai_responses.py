"""Tests for the API-key OpenAI Responses provider."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

import pytest

from src.providers import llm as llm_mod
from src.providers.capabilities import provider_env_names
from src.providers.openai_responses import (
    OpenAIResponsesLLM,
    responses_endpoint,
    validate_responses_base_url,
)


class _FakeResponse:
    def __init__(self, status_code: int, lines: list[str] | None = None, body: bytes = b"") -> None:
        self.status_code = status_code
        self._lines = lines or []
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body

    def iter_lines(self):
        yield from self._lines


def _fake_client(responses: list[_FakeResponse], captured: list[dict[str, object]]):
    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.append({"client": kwargs})

        def __enter__(self) -> "_FakeClient":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def stream(self, method: str, url: str, **kwargs: object) -> _FakeResponse:
            captured.append({"method": method, "url": url, **kwargs})
            return responses.pop(0)

    return _FakeClient


def test_provider_metadata_and_env_namespace() -> None:
    providers_path = Path(__file__).resolve().parents[1] / "src" / "providers" / "llm_providers.json"
    providers = json.loads(providers_path.read_text(encoding="utf-8"))
    provider = next(item for item in providers if item["name"] == "openai-responses")

    assert provider["api_key_env"] == "OPENAI_RESPONSES_API_KEY"
    assert provider["base_url_env"] == "OPENAI_RESPONSES_BASE_URL"
    assert provider_env_names("openai-responses") == (
        "OPENAI_RESPONSES_API_KEY",
        "OPENAI_RESPONSES_BASE_URL",
    )


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://api.example.test/v1", "https://api.example.test/v1"),
        ("http://127.0.0.1:8080/v1/", "http://127.0.0.1:8080/v1"),
        ("http://localhost:8080/v1", "http://localhost:8080/v1"),
    ],
)
def test_validate_responses_base_url(base_url: str, expected: str) -> None:
    assert validate_responses_base_url(base_url) == expected


@pytest.mark.parametrize(
    "base_url",
    [
        "api.example.test/v1",
        "http://api.example.test/v1",
        "https://user:pass@api.example.test/v1",
        "https://api.example.test/v1?token=secret",
        "https://api.example.test/v1#fragment",
    ],
)
def test_validate_responses_base_url_rejects_unsafe_values(base_url: str) -> None:
    with pytest.raises(ValueError):
        validate_responses_base_url(base_url)


def test_responses_endpoint_accepts_base_or_full_endpoint() -> None:
    assert responses_endpoint("https://api.example.test/v1") == (
        "https://api.example.test/v1/responses"
    )
    assert responses_endpoint("https://api.example.test/v1/responses") == (
        "https://api.example.test/v1/responses"
    )


def test_build_llm_returns_api_key_responses_adapter() -> None:
    llm_mod._dotenv_loaded = True
    env = {
        "LANGCHAIN_PROVIDER": "openai-responses",
        "LANGCHAIN_MODEL_NAME": "gpt-5.5",
        "LANGCHAIN_REASONING_EFFORT": "xhigh",
        "OPENAI_RESPONSES_API_KEY": "responses-test-key",
        "OPENAI_RESPONSES_BASE_URL": "https://api.example.test/v1",
        "MAX_RETRIES": "3",
    }
    with patch.dict(os.environ, env, clear=True):
        adapter = llm_mod.build_llm()

    assert isinstance(adapter, OpenAIResponsesLLM)
    assert adapter.responses_url == "https://api.example.test/v1/responses"
    assert adapter.reasoning_effort == "xhigh"
    assert adapter.max_retries == 3


def test_sync_provider_env_does_not_fall_back_to_stale_openai_key() -> None:
    llm_mod._dotenv_loaded = True
    env = {
        "LANGCHAIN_PROVIDER": "openai-responses",
        "LANGCHAIN_MODEL_NAME": "gpt-5.5",
        "OPENAI_API_KEY": "stale-openai-key",
        "OPENAI_RESPONSES_BASE_URL": "https://api.example.test/v1",
    }
    with patch.dict(os.environ, env, clear=True):
        llm_mod._sync_provider_env()
        assert "OPENAI_API_KEY" not in os.environ


def test_missing_responses_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_RESPONSES_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_RESPONSES_API_KEY is not configured"):
        OpenAIResponsesLLM(model="gpt-5.5", base_url="https://api.example.test/v1")


def test_stream_parses_text_tool_call_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [
        'data: {"type":"response.output_text.delta","delta":"READY"}',
        "",
        'data: {"type":"response.output_item.added","item":{"type":"function_call","call_id":"call_1","id":"fc_1","name":"read_file","arguments":""}}',
        "",
        'data: {"type":"response.function_call_arguments.done","call_id":"call_1","arguments":"{\\"path\\":\\"README.md\\"}"}',
        "",
        'data: {"type":"response.output_item.done","item":{"type":"function_call","call_id":"call_1"}}',
        "",
        'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":11,"output_tokens":7,"total_tokens":18}}}',
        "",
        "data: [DONE]",
        "",
    ]
    captured: list[dict[str, object]] = []
    responses = [_FakeResponse(200, lines=lines)]
    import src.providers.openai_responses as responses_mod

    monkeypatch.setattr(responses_mod.httpx, "Client", _fake_client(responses, captured))
    adapter = OpenAIResponsesLLM(
        model="gpt-5.5",
        api_key="responses-test-key",
        base_url="https://api.example.test/v1",
        reasoning_effort="xhigh",
    )

    message = adapter.invoke([{"role": "user", "content": "Reply and inspect README"}])

    assert message.content == "READY"
    assert message.tool_calls == [
        {"id": "call_1|fc_1", "name": "read_file", "args": {"path": "README.md"}}
    ]
    assert message.usage_metadata == {
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
    }
    request = captured[1]
    assert request["method"] == "POST"
    assert request["url"] == "https://api.example.test/v1/responses"
    assert request["json"]["stream"] is True
    assert request["json"]["store"] is False
    assert request["json"]["reasoning"] == {"effort": "xhigh"}
    assert request["headers"]["User-Agent"] == "Vibe-Trading/Responses"
    assert "chatgpt-account-id" not in request["headers"]


@pytest.mark.integration
def test_loopback_sse_server_round_trip() -> None:
    received: dict[str, object] = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            length = int(self.headers.get("Content-Length", "0"))
            received["path"] = self.path
            received["authorization"] = self.headers.get("Authorization")
            received["body"] = json.loads(self.rfile.read(length))
            payload = "\n".join([
                'data: {"type":"response.output_text.delta","delta":"READY"}',
                "",
                'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":4,"output_tokens":1,"total_tokens":5}}}',
                "",
                "data: [DONE]",
                "",
            ]).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        adapter = OpenAIResponsesLLM(
            model="gpt-5.5",
            api_key="loopback-test-key",
            base_url=f"http://{host}:{port}/v1",
            max_retries=0,
        )
        message = adapter.invoke([{"role": "user", "content": "Reply exactly READY"}])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert message.content == "READY"
    assert message.usage_metadata == {
        "input_tokens": 4,
        "output_tokens": 1,
        "total_tokens": 5,
    }
    assert received["path"] == "/v1/responses"
    assert received["authorization"] == "Bearer loopback-test-key"
    assert received["body"]["store"] is False
    assert received["body"]["stream"] is True


def test_retryable_status_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [
        'data: {"type":"response.completed","response":{"status":"completed"}}',
        "",
    ]
    captured: list[dict[str, object]] = []
    responses = [_FakeResponse(502), _FakeResponse(200, lines=lines)]
    import src.providers.openai_responses as responses_mod

    monkeypatch.setattr(responses_mod.httpx, "Client", _fake_client(responses, captured))
    monkeypatch.setattr(responses_mod.time, "sleep", lambda _seconds: None)
    adapter = OpenAIResponsesLLM(
        model="gpt-5.5",
        api_key="responses-test-key",
        base_url="https://api.example.test/v1",
        max_retries=1,
    )

    adapter.invoke([{"role": "user", "content": "hello"}])

    assert len([item for item in captured if item.get("method") == "POST"]) == 2


def test_http_error_does_not_expose_response_body(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, object]] = []
    responses = [_FakeResponse(401, body=b'{"error":"responses-test-key"}')]
    import src.providers.openai_responses as responses_mod

    monkeypatch.setattr(responses_mod.httpx, "Client", _fake_client(responses, captured))
    adapter = OpenAIResponsesLLM(
        model="gpt-5.5",
        api_key="responses-test-key",
        base_url="https://api.example.test/v1",
        max_retries=0,
    )

    with pytest.raises(RuntimeError) as exc_info:
        list(adapter.stream([{"role": "user", "content": "hello"}]))

    assert str(exc_info.value) == "OpenAI Responses HTTP 401"
    assert "responses-test-key" not in str(exc_info.value)
