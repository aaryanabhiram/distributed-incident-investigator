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
    Hypothesis,
    InvestigatorInput,
    validate_evidence,
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
- Produce exactly one hypothesis: the single most likely root cause, not a list of guesses.
- supporting_evidence must be a non-empty list of unique references. Each reference has \
kind ("anomaly" or "relationship") and the zero-based index shown in the context for an item \
that actually exists.
- confidence is a number from 0 to 1 expressing your uncertainty. It is an estimate, not proof.
- Respond only with JSON matching the required schema."""


@dataclass(frozen=True)
class Prompt:
    system: str
    user: str


# (prompt, JSON schema the response must follow) -> raw JSON text from the model.
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
        "unobserved_dependencies": incident["unobserved_dependencies"],
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
        raw = self._complete(build_prompt(investigator_input), Hypothesis.model_json_schema())
        # Raises pydantic.ValidationError for malformed JSON or contract violations.
        hypothesis = Hypothesis.model_validate_json(raw)
        validate_evidence(investigator_input, hypothesis)
        return hypothesis
