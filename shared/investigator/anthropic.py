"""Anthropic Messages API transport for `LLMInvestigator` (the one concrete provider).

Uses `httpx` (already a project dependency) rather than an SDK: one POST, no new dependency.
Structured output uses the API's native JSON-schema output format. Configuration comes from
the environment: `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` (both required), and optionally
`ANTHROPIC_BASE_URL`. Nothing is hard-coded and there are no retries; HTTP errors propagate.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from shared.investigator.llm import CompleteFn, LLMInvestigator, Prompt

DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
MAX_TOKENS = 1024
TIMEOUT_SECONDS = 60.0

# Keywords the API's schema subset does not accept. They are enforced locally by `Hypothesis`.
_UNSUPPORTED = {"minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems", "title"}


class ProviderError(RuntimeError):
    """The provider returned no usable text (refusal, truncation, or an unexpected shape)."""


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


def anthropic_complete(
    *,
    api_key: str,
    model: str,
    base_url: str = DEFAULT_BASE_URL,
    client: httpx.Client | None = None,
) -> CompleteFn:
    """Build a `CompleteFn` that makes one Messages API call per invocation."""

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
        stop_reason = data.get("stop_reason")
        if stop_reason in ("refusal", "max_tokens"):
            raise ProviderError(f"model stopped without a complete answer: {stop_reason}")
        texts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        if not texts:
            raise ProviderError("response contained no text content")
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
