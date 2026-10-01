"""Anthropic Messages API transport for `LLMInvestigator` (the one concrete provider).

Uses `httpx` (already a project dependency) rather than an SDK: one POST, no new dependency.
Structured output uses the API's native JSON-schema output format. Configuration comes from
the environment: `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` (both required), and optionally
`ANTHROPIC_BASE_URL`. Nothing is hard-coded and there are no retries; HTTP errors propagate.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import httpx

from shared.investigator.llm import CompleteFn, LLMInvestigator, Prompt, ProviderError

__all__ = ["ProviderError", "anthropic_complete", "anthropic_investigator_from_env", "wire_schema"]

DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
MAX_TOKENS = 1024
TIMEOUT_SECONDS = 60.0

# Keywords the API's schema subset does not accept. They are enforced locally by `Hypothesis`.
_UNSUPPORTED = {"minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems", "title"}


def wire_schema(schema: Any) -> Any:
    """Copy a pydantic JSON schema, dropping unsupported keywords and closing objects."""
    if isinstance(schema, list):
        return [wire_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: wire_schema(v) for k, v in schema.items() if k not in _UNSUPPORTED}
    if out.get("type") == "object":
        out["additionalProperties"] = False
    return out


USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
UsageSink = Callable[[dict[str, int | None]], None]


def usage_from_response(data: Any) -> dict[str, int | None]:
    """The provider-reported token counts, each `None` unless the response gave a real integer.

    Only whitelisted counter fields are read; nothing else from the response is exposed. Absent
    or malformed usage is `None`, never 0 and never an estimate.
    """
    usage = data.get("usage") if isinstance(data, dict) else None
    usage = usage if isinstance(usage, dict) else {}

    def count(name: str) -> int | None:
        value = usage.get(name)
        return (
            value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        )

    return {name: count(name) for name in USAGE_FIELDS}


def anthropic_complete(
    *,
    api_key: str,
    model: str,
    base_url: str = DEFAULT_BASE_URL,
    client: httpx.Client | None = None,
    on_usage: UsageSink | None = None,
) -> CompleteFn:
    """Build a `CompleteFn` that makes one Messages API call per invocation.

    `on_usage`, if given, is called once per successful HTTP response with the provider-reported
    token counts (`usage_from_response`), before the stop reason is checked, so a refused or
    truncated reply still reports what it cost. It does not change the request, the returned
    text or any error; with no sink the behaviour is exactly as before.
    """

    def complete(prompt: Prompt, schema: dict[str, Any]) -> str:
        body = {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "system": prompt.system,
            "messages": [{"role": "user", "content": prompt.user}],
            "output_config": {"format": {"type": "json_schema", "schema": wire_schema(schema)}},
        }
        headers = {
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        http = client or httpx.Client(timeout=TIMEOUT_SECONDS)
        try:
            response = http.post(f"{base_url.rstrip('/')}/v1/messages", json=body, headers=headers)
        finally:
            if client is None:
                http.close()
        response.raise_for_status()
        data = response.json()
        if on_usage is not None:
            on_usage(usage_from_response(data))
        stop_reason = data.get("stop_reason")
        if stop_reason in ("refusal", "max_tokens"):
            raise ProviderError(
                f"model stopped without a complete answer: {stop_reason}",
                category="refusal" if stop_reason == "refusal" else "token_limit",
            )
        texts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        if not texts:
            raise ProviderError("response contained no text content", category="empty_response")
        return "".join(texts)

    return complete


def anthropic_investigator_from_env() -> LLMInvestigator:
    """Build the Anthropic-backed investigator from environment variables."""
    missing = [n for n in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL") if not os.environ.get(n)]
    if missing:
        raise RuntimeError(f"missing required environment variable(s): {', '.join(missing)}")
    return LLMInvestigator(
        anthropic_complete(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            model=os.environ["ANTHROPIC_MODEL"],
            base_url=os.environ.get("ANTHROPIC_BASE_URL", DEFAULT_BASE_URL),
        )
    )
