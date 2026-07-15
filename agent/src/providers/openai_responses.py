"""API-key authenticated OpenAI Responses provider.

This adapter is intentionally separate from ``openai_codex``.  The latter uses
ChatGPT OAuth credentials and is restricted to the official ChatGPT endpoint;
this module accepts ordinary API keys for OpenAI-compatible Responses gateways.
"""

from __future__ import annotations

import asyncio
import os
import time
from ipaddress import ip_address
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from src.providers.openai_codex import (
    CodexAIMessage,
    _build_responses_body,
    _events_from_lines,
    _message_chunks_from_events,
)

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore


DEFAULT_RESPONSES_BASE_URL = "https://api.openai.com/v1"
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _is_loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_responses_base_url(url: str) -> str:
    """Validate an API-key Responses base URL without weakening OAuth safety.

    HTTPS is mandatory except for explicit loopback addresses used by local
    development and isolated integration tests. Credentials, query strings and
    fragments are rejected so secrets cannot be smuggled into logs or URLs.
    """
    value = (url or DEFAULT_RESPONSES_BASE_URL).strip().rstrip("/")
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("Responses base URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Responses base URL must not contain credentials, query, or fragment")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and _is_loopback_host(parsed.hostname)
    ):
        raise ValueError("Responses base URL must use HTTPS (HTTP is allowed only for loopback)")
    return value


def responses_endpoint(base_url: str) -> str:
    """Return the concrete ``/responses`` endpoint for a validated base URL."""
    value = validate_responses_base_url(base_url)
    if urlparse(value).path.rstrip("/").endswith("/responses"):
        return value
    return f"{value}/responses"


class OpenAIResponsesLLM:
    """Minimal LangChain-compatible Responses API adapter using an API key."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.0,
        timeout: int = 120,
        max_retries: int = 2,
        tools: list[dict[str, Any]] | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        if httpx is None:
            raise RuntimeError("OpenAI Responses requires httpx. Install dependencies first.")
        resolved_key = (api_key or os.getenv("OPENAI_RESPONSES_API_KEY", "")).strip()
        if not resolved_key:
            raise RuntimeError("OPENAI_RESPONSES_API_KEY is not configured")
        self.model = model
        self.api_key = resolved_key
        self.base_url = validate_responses_base_url(base_url or DEFAULT_RESPONSES_BASE_URL)
        self.responses_url = responses_endpoint(self.base_url)
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.tools = tools or []
        self.reasoning_effort = reasoning_effort

    def bind_tools(self, tools: list[dict[str, Any]]) -> "OpenAIResponsesLLM":
        return OpenAIResponsesLLM(
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
            temperature=self.temperature,
            timeout=self.timeout,
            max_retries=self.max_retries,
            tools=tools,
            reasoning_effort=self.reasoning_effort,
        )

    def _body(self, messages: list[dict[str, Any]], *, stream: bool) -> dict[str, Any]:
        return _build_responses_body(
            model=self.model,
            messages=messages,
            tools=self.tools,
            reasoning_effort=self.reasoning_effort,
            stream=stream,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "OpenAI-Beta": "responses=experimental",
            "originator": "vibe-trading",
            "User-Agent": "Vibe-Trading/Responses",
            "accept": "text/event-stream",
            "content-type": "application/json",
        }

    def stream(
        self,
        messages: list[dict[str, Any]],
        config: Optional[dict[str, Any]] = None,
    ) -> Iterable[CodexAIMessage]:
        timeout = (config or {}).get("timeout") or self.timeout
        with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=True) as client:
            for attempt in range(self.max_retries + 1):
                with client.stream(
                    "POST",
                    self.responses_url,
                    headers=self._headers(),
                    json=self._body(messages, stream=True),
                ) as response:
                    if response.status_code != 200:
                        if (
                            response.status_code in _RETRYABLE_STATUS_CODES
                            and attempt < self.max_retries
                        ):
                            time.sleep(min(0.25 * (2**attempt), 2.0))
                            continue
                        raise RuntimeError(f"OpenAI Responses HTTP {response.status_code}")
                    try:
                        yield from _message_chunks_from_events(
                            _events_from_lines(response.iter_lines())
                        )
                    except RuntimeError:
                        raise RuntimeError("OpenAI Responses stream failed") from None
                    return

    def invoke(
        self,
        messages: list[dict[str, Any]],
        config: Optional[dict[str, Any]] = None,
    ) -> CodexAIMessage:
        accumulated: CodexAIMessage | None = None
        for chunk in self.stream(messages, config=config):
            accumulated = chunk if accumulated is None else accumulated + chunk
        return accumulated or CodexAIMessage()

    async def ainvoke(
        self,
        messages: list[dict[str, Any]],
        config: Optional[dict[str, Any]] = None,
    ) -> CodexAIMessage:
        return await asyncio.to_thread(self.invoke, messages, config)
