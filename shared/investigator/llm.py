"""LLM-backed investigator: one bounded inference call behind the `Investigator` protocol.

Prompt construction (`build_prompt`) is pure and provider-independent. Transport is an injected
`CompleteFn` — `(prompt, json_schema) -> raw JSON text` — so the provider can be swapped or
replaced by a test double without touching `InvestigatorInput` or `Hypothesis`. The model's
output is parsed with the existing `Hypothesis` model and checked with `validate_evidence`;
invalid output raises, it is never repaired, clamped, or replaced by a fallback.

No agent loop, tools, retries, memory, or conversation state: one prompt in, one hypothesis out.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from shared.investigator import (
    ContractViolation,
    Hypothesis,
    InvestigatorInput,
    validate_evidence,
    validate_origin,
)

SYSTEM_PROMPT = """\
You are the reasoning step of an incident investigation system. Deterministic code has already \
detected anomalies and extracted service relationships from telemetry. You receive the result \
as an incident context.

Rules:
- The incident context in the user message is the complete available evidence. Reason only from \
its anomalies and relationships.
- Do not claim evidence that is not present. Do not assume logs, traces, metrics, or service \
state beyond what is listed.
- A relationship "caller -> callee" means the caller invoked the callee. For synchronous \
requests, the caller's request latency can include time spent waiting for the callee, so an \
affected callee can contribute to latency observed in its callers. Never reverse this \
direction. It defines how to read the edge; it does not by itself show that any service is the \
root cause.
- metric_coverage states, per service and metric, whether the metric was "observed" (telemetry \
for it is in the context), "undefined" (the query returned a series for it but with no numeric \
value) or "unobserved" (the query returned no series for it). The reason is not known in either \
case. An empty list means no coverage was declared. Absence of an anomaly is not evidence of \
health for an undefined or unobserved service, and such a service must not be treated as healthy.
- unobserved_dependencies lists relationships where a caller that is anomalous on a metric \
invoked a callee whose coverage for that same metric is undefined or unobserved. It states \
only that the callee's health is unknown; it does not show that the callee is or is not the \
cause. An empty list means none were identified (or no coverage was declared).
- You cannot inspect services, query any system, or take actions. Do not propose remediation.
- Produce exactly one hypothesis, not a list of guesses. Set status to "identified" and name \
the single most likely origin in root_cause only when the evidence supports choosing it over \
the other explanations consistent with the evidence. Otherwise set status to "undetermined". \
Typical cases: an affected service calls a callee listed in unobserved_dependencies and nothing \
in the context separates the caller's own contribution from the callee's, or several services \
are equally consistent with the evidence. An unobserved or undefined callee is unknown, not \
faulty. When undetermined, root_cause must state what is established and what is unknown, must \
not present any service as the established or most likely origin, and should cite the relevant \
unobserved_dependency items.
- origin_service is the machine-readable origin. When status is "identified", set it to the \
exact service name, as written in the context, of the origin you name in root_cause. When \
status is "undetermined", set it to null. Always include the field.
- supporting_evidence must be a non-empty list of unique references. Each reference has \
kind ("anomaly", "relationship" or "unobserved_dependency") and the zero-based index shown in \
the context for an item that actually exists.
- confidence is a number from 0 to 1 expressing your confidence in the conclusion you state, \
whichever status. It is an estimate, not proof.
- Respond only with JSON matching the required schema."""


@dataclass(frozen=True)
class Prompt:
    system: str
    user: str


def response_schema() -> dict[str, Any]:
    """`Hypothesis`' JSON schema with `status` required, so a provider must state it.

    `Hypothesis` defaults `status` for old serialized data; a model reply that omits it would
    silently read as a confident "identified", so model output must always say it. The same goes
    for `origin_service`, which the model must state (null when undetermined).
    """
    schema = Hypothesis.model_json_schema()
    schema["required"] = sorted({*schema.get("required", []), "status", "origin_service"})
    return schema


class ProviderError(RuntimeError):
    """A provider answered, but not with a usable completion.

    Provider-independent: a `CompleteFn` signals "the provider refused, ran out of tokens or
    returned nothing" by raising this with a `category`, so evaluation can record it as a
    provider event rather than a verdict on the investigator's reasoning. Transport failures need
    no wrapper: `httpx` errors are recognised as they are. Never put credentials in the message.
    """

    CATEGORIES = ("refusal", "token_limit", "empty_response", "other")

    def __init__(self, message: str, category: str = "other") -> None:
        if category not in self.CATEGORIES:
            raise ValueError(f"unknown provider failure category {category!r}")
        super().__init__(message)
        self.category = category


# (prompt, JSON schema the response must follow) -> raw JSON text from the model.
# Provider-side failures raise `ProviderError` (or an `httpx` error); the investigator never
# retries, repairs or replaces them with a hypothesis.
CompleteFn = Callable[[Prompt, dict[str, Any]], str]


def build_prompt(investigator_input: InvestigatorInput) -> Prompt:
    """Render the bounded input as a prompt; evidence items carry the index to cite."""
    incident = investigator_input.incident.model_dump(mode="json")
    indexed = {
        "window_start": incident["window_start"],
        "window_end": incident["window_end"],
        "affected_services": incident["affected_services"],
        "anomalies": [{"index": i, **a} for i, a in enumerate(incident["anomalies"])],
        "relationships": [{"index": i, **r} for i, r in enumerate(incident["relationships"])],
        "metric_coverage": incident["metric_coverage"],
        "unobserved_dependencies": [
            {"index": i, **d} for i, d in enumerate(incident["unobserved_dependencies"])
        ],
    }
    user = "Incident context (complete evidence; cite items by kind and index):\n" + json.dumps(
        indexed, indent=2, sort_keys=True
    )
    return Prompt(system=SYSTEM_PROMPT, user=user)


class LLMInvestigator:
    """`Investigator` that asks an injected completion function for a `Hypothesis`."""

    def __init__(self, complete: CompleteFn) -> None:
        self._complete = complete

    def __call__(self, investigator_input: InvestigatorInput) -> Hypothesis:
        raw = self._complete(build_prompt(investigator_input), response_schema())
        # Raises pydantic.ValidationError for malformed JSON or contract violations.
        hypothesis = Hypothesis.model_validate_json(raw)
        if "status" not in hypothesis.model_fields_set:
            raise ContractViolation("model output omitted the required 'status' field")
        validate_evidence(investigator_input, hypothesis)
        validate_origin(investigator_input, hypothesis)
        return hypothesis
