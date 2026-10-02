"""OpenAI Responses API transport for `LLMInvestigator`.

Uses `httpx` (already a project dependency), no SDK: one POST per call, no retries, no fallback.
Structured output uses the API's strict `json_schema` text format. The request is the one used by
the one-off script of Evaluation 7 (`docs/manual-evaluation.md`): `model`, `instructions`,
`input`, `max_output_tokens`, `store: false` and the strict schema. No sampling or reasoning
parameter is sent, so the provider's defaults apply (they are recorded as unsent, not as values).

Configuration comes from the environment: `OPENAI_API_KEY` and `OPENAI_MODEL` (both required),
optionally `OPENAI_BASE_URL`. Nothing else is read.

Failures a provider can report are raised as `ProviderError` so evaluation records them as
non-scored provider events: a refusal, an incomplete response (`max_output_tokens` is a
`token_limit`), a failed response and an empty reply. HTTP errors propagate as `httpx` errors; an
error message never carries a response body, header or the key.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import httpx

from shared.investigator.llm import CompleteFn, LLMInvestigator, Prompt, ProviderError

__all__ = [
    "ProviderError",
    "openai_complete",
    "openai_investigator_from_env",
    "usage_from_response",
    "wire_schema",
]

DEFAULT_BASE_URL = "https://api.openai.com"
ENDPOINT = "/v1/responses"
MAX_OUTPUT_TOKENS = 4000  # reasoning models also spend output tokens on thinking
TIMEOUT_SECONDS = 120.0
STORE = False
SCHEMA_NAME = "hypothesis"

# Keywords dropped from the schema sent to OpenAI. None is needed by the provider: the same
# constraints are enforced locally by `Hypothesis`, and `default` is meaningless once every field
# is required. Dropping them avoids depending on which keywords strict mode accepts.
_DROP = {
    "title",
    "default",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "minItems",
    "maxItems",
}


def wire_schema(schema: Any, root: bool = True) -> Any:
    """Structured-output form of a pydantic JSON schema: every object closed, all required."""
    if isinstance(schema, list):
        return [wire_schema(item, root=False) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out = {
        k: wire_schema(v, root=False)
        for k, v in schema.items()
        if k not in _DROP and not (root and k == "description")
    }
    if isinstance(out.get("properties"), dict):
        out["type"] = "object"
        out["required"] = list(out["properties"])
        out["additionalProperties"] = False
    return out


USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
)
UsageSink = Callable[[dict[str, int | None]], None]
ModelSink = Callable[[str | None], None]


def usage_from_response(data: Any) -> dict[str, int | None]:
    """The provider-reported token counts, each `None` unless the response gave a real integer.

    `reasoning_tokens` is a part of `output_tokens` (billed as output), `cached_input_tokens` a
    part of `input_tokens` (billed at another rate). Only whitelisted counters are read; absent or
    malformed usage is `None`, never 0 and never an estimate.
    """
    usage = data.get("usage") if isinstance(data, dict) else None
    usage = usage if isinstance(usage, dict) else {}

    def count(container: Any, name: str) -> int | None:
        value = container.get(name) if isinstance(container, dict) else None
        return (
            value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        )

    return {
        "input_tokens": count(usage, "input_tokens"),
        "output_tokens": count(usage, "output_tokens"),
        "cached_input_tokens": count(usage.get("input_tokens_details"), "cached_tokens"),
        "reasoning_tokens": count(usage.get("output_tokens_details"), "reasoning_tokens"),
    }


def openai_complete(
    *,
    api_key: str,
    model: str,
    base_url: str = DEFAULT_BASE_URL,
    client: httpx.Client | None = None,
    on_usage: UsageSink | None = None,
    on_model: ModelSink | None = None,
) -> CompleteFn:
    """Build a `CompleteFn` that makes one Responses API call per invocation.

    `on_usage` receives the provider-reported counters and `on_model` the model id the response
    names (a snapshot may differ from the requested alias; `None` if absent), once per successful
    HTTP response and before the status is checked, so an incomplete reply still reports what it
    cost. Neither changes the request, the returned text or any error.
    """

    def complete(prompt: Prompt, schema: dict[str, Any]) -> str:
        body = {
            "model": model,
            "instructions": prompt.system,
            "input": prompt.user,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "store": STORE,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": SCHEMA_NAME,
                    "strict": True,
                    "schema": wire_schema(schema),
                }
            },
        }
        headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
        http = client or httpx.Client(timeout=TIMEOUT_SECONDS)
        try:
            response = http.post(f"{base_url.rstrip('/')}{ENDPOINT}", json=body, headers=headers)
        finally:
            if client is None:
                http.close()
        response.raise_for_status()  # its message carries the URL and status, not the key
        data = response.json()
        if on_usage is not None:
            on_usage(usage_from_response(data))
        if on_model is not None:
            reported = data.get("model") if isinstance(data, dict) else None
            on_model(reported if isinstance(reported, str) else None)
        status = data.get("status")
        if status == "incomplete":
            details = data.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else None
            category = {"max_output_tokens": "token_limit", "content_filter": "refusal"}.get(
                reason, "other"
            )
            raise ProviderError(f"response incomplete: {reason}", category=category)
        if status != "completed":
            raise ProviderError(f"response not completed: status={status!r}", category="other")
        texts = []
        for item in data.get("output", []):
            if item.get("type") != "message":
                continue  # e.g. reasoning items
            for part in item.get("content", []):
                if part.get("type") == "refusal":
                    raise ProviderError("model refused", category="refusal")
                if part.get("type") == "output_text":
                    texts.append(part.get("text", ""))
        if not texts:
            raise ProviderError("response contained no output_text", category="empty_response")
        return "".join(texts)

    return complete


def openai_investigator_from_env() -> LLMInvestigator:
    """Build the OpenAI-backed investigator from environment variables."""
    missing = [n for n in ("OPENAI_API_KEY", "OPENAI_MODEL") if not os.environ.get(n)]
    if missing:
        raise RuntimeError(f"missing required environment variable(s): {', '.join(missing)}")
    return LLMInvestigator(
        openai_complete(
            api_key=os.environ["OPENAI_API_KEY"],
            model=os.environ["OPENAI_MODEL"],
            base_url=os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL),
        )
    )
