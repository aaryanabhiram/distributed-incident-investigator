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

    kind: Literal["anomaly", "relationship"]
    index: int = Field(ge=0)


class Hypothesis(BaseModel):
    """The investigator's structured result."""

    root_cause: str = Field(min_length=1, description="Likely root cause")
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_evidence: list[EvidenceRef] = Field(min_length=1)

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


def validate_evidence(investigator_input: InvestigatorInput, hypothesis: Hypothesis) -> None:
    """Raise `ValueError` if the hypothesis cites evidence absent from the input."""
    incident = investigator_input.incident
    limits = {"anomaly": len(incident.anomalies), "relationship": len(incident.relationships)}
    for ref in hypothesis.supporting_evidence:
        if ref.index >= limits[ref.kind]:
            raise ValueError(f"evidence reference {ref.kind}[{ref.index}] not in incident context")


def investigate(payload: dict[str, Any], investigator: Investigator) -> Hypothesis:
    """Run an investigator over a handoff payload and check its cited evidence exists."""
    investigator_input = build_investigator_input(payload)
    hypothesis = Hypothesis.model_validate(investigator(investigator_input))
    validate_evidence(investigator_input, hypothesis)
    return hypothesis
