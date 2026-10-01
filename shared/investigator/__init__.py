"""Investigator contract: bounded incident input in, structured hypothesis out.

Per docs/architecture.md, the investigator reads the incident context produced by the
deterministic correlation layer (never raw telemetry or live services) and returns a
hypothesis: likely root cause, confidence, and the evidence in that context supporting it.

This module defines only that boundary. It knows no provider, SDK, prompt format, or
transport; whatever later does the reasoning implements the `Investigator` protocol, and
tests substitute a plain callable. No root-cause logic lives here.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, model_validator

from shared.correlation import IncidentContext
from shared.correlation.handoff import incident_context_from_payload


class InvestigatorInput(BaseModel):
    """The investigator's entire, bounded input: one correlated incident context."""

    incident: IncidentContext


class EvidenceRef(BaseModel):
    """Points at one piece of evidence inside the input's `IncidentContext` by position."""

    kind: Literal["anomaly", "relationship", "unobserved_dependency"]
    index: int = Field(ge=0)


# Implementation notes (kept out of the docstring: pydantic sends the docstring to providers as
# the schema description, so it must state only the output contract).
# - `origin_service` is the machine-readable origin and is never inferred from `root_cause`.
# - This model checks only that status and origin agree. That the origin names a service in the
#   input needs the input, so `validate_origin` checks it; `validate_evidence` checks citations.
# - Nothing can check that a stated cause is correct.
# - `status` still defaults to "identified", but an identified hypothesis now also needs
#   `origin_service`, so hypotheses serialized before that field existed do not load as identified.
class Hypothesis(BaseModel):
    """The investigator's structured result.

    status "identified": origin_service and root_cause name the most likely origin. status
    "undetermined": the evidence cannot support choosing one, origin_service is null and
    root_cause states what is established and what is unknown.
    """

    status: Literal["identified", "undetermined"] = Field(
        default="identified",
        description=(
            "'identified': root_cause names the most likely origin. 'undetermined': the evidence "
            "cannot support choosing one; root_cause states what is established and what is unknown"
        ),
    )
    origin_service: str | None = Field(
        default=None,
        description=(
            "Exact name of the service identified as the origin; required when status is "
            "'identified', null when 'undetermined'"
        ),
    )
    root_cause: str = Field(min_length=1, description="Likely root cause, or what is unknown")
    confidence: float = Field(
        ge=0.0, le=1.0, description="Confidence in the stated conclusion, whichever status"
    )
    supporting_evidence: list[EvidenceRef] = Field(min_length=1)

    @model_validator(mode="after")
    def _origin_matches_status(self) -> Hypothesis:
        if self.status == "identified" and not self.origin_service:
            raise ValueError("an identified hypothesis requires a non-empty origin_service")
        if self.status == "undetermined" and self.origin_service is not None:
            raise ValueError("an undetermined hypothesis must have origin_service=None")
        return self

    @model_validator(mode="after")
    def _no_duplicate_evidence(self) -> Hypothesis:
        keys = [(ref.kind, ref.index) for ref in self.supporting_evidence]
        if len(keys) != len(set(keys)):
            raise ValueError("supporting_evidence contains duplicate references")
        return self


class Investigator(Protocol):
    """Execution boundary: anything that turns an input into a hypothesis."""

    def __call__(self, investigator_input: InvestigatorInput) -> Hypothesis: ...


def build_investigator_input(payload: dict[str, Any]) -> InvestigatorInput:
    """Build the input from a handoff payload; raises `pydantic.ValidationError` if invalid."""
    return InvestigatorInput(incident=incident_context_from_payload(payload))


class ContractViolation(ValueError):
    """An investigator result broke the contract (dangling evidence or origin, missing field).

    A `ValueError` subclass, so existing `except ValueError` callers still work, but distinct from
    a `ValueError` raised by a programming error, which evaluation must not score as an outcome.
    """


def revalidate(result: Hypothesis | dict[str, Any]) -> Hypothesis:
    """Validate an investigator result from scratch, even when it is already a `Hypothesis`.

    `Hypothesis.model_validate(instance)` does nothing for an instance, so a result built with
    `model_construct` or `model_copy(update=...)` would skip every field validator. Re-dumping it
    forces them to run. Raises `pydantic.ValidationError` for a malformed result; anything that is
    neither a `Hypothesis` nor a dict raises `TypeError` (a bug, not a contract outcome).
    """
    if isinstance(result, Hypothesis):
        return Hypothesis.model_validate(result.model_dump(warnings=False))
    if isinstance(result, dict):
        return Hypothesis.model_validate(result)
    raise TypeError(f"investigator returned {type(result).__name__}, not a Hypothesis")


def validate_evidence(investigator_input: InvestigatorInput, hypothesis: Hypothesis) -> None:
    """Raise `ContractViolation` if the hypothesis cites evidence absent from the input."""
    incident = investigator_input.incident
    limits = {
        "anomaly": len(incident.anomalies),
        "relationship": len(incident.relationships),
        "unobserved_dependency": len(incident.unobserved_dependencies),
    }
    for ref in hypothesis.supporting_evidence:
        if ref.index >= limits[ref.kind]:
            raise ContractViolation(
                f"evidence reference {ref.kind}[{ref.index}] not in incident context"
            )


def incident_services(investigator_input: InvestigatorInput) -> set[str]:
    """Every service the incident context mentions, in any field."""
    incident = investigator_input.incident
    services = set(incident.affected_services)
    services.update(a.service for a in incident.anomalies)
    for relationship in incident.relationships:
        services.update((relationship.caller, relationship.callee))
    services.update(c.service for c in incident.metric_coverage)
    for dependency in incident.unobserved_dependencies:
        services.update((dependency.caller, dependency.callee))
    return services


def validate_origin(investigator_input: InvestigatorInput, hypothesis: Hypothesis) -> None:
    """Raise `ContractViolation` if an identified origin is not a service in the input context.

    Needs the input, so it cannot live in `Hypothesis`' own validators. An undetermined
    hypothesis has no origin and always passes.
    """
    origin = hypothesis.origin_service
    if origin is not None and origin not in incident_services(investigator_input):
        raise ContractViolation(
            f"origin_service {origin!r} is not a service in the incident context"
        )


def investigate(payload: dict[str, Any], investigator: Investigator) -> Hypothesis:
    """Run an investigator over a handoff payload; check its cited evidence and origin exist."""
    investigator_input = build_investigator_input(payload)
    hypothesis = revalidate(investigator(investigator_input))
    validate_evidence(investigator_input, hypothesis)
    validate_origin(investigator_input, hypothesis)
    return hypothesis
