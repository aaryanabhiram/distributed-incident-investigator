"""Handoff boundary between correlation and whatever consumes an `IncidentContext` next.

`IncidentContext` is already a pydantic model, so the boundary is pydantic's own JSON-mode
dump/validate — no new transport or schema layer. The payload is a plain JSON-safe dict
(datetimes as ISO-8601 strings) that a future consumer can log, persist, or place in a prompt.
Pure and deterministic: identical context in, identical payload out. No interpretation.
"""

from __future__ import annotations

from typing import Any

from shared.correlation import IncidentContext


def incident_context_to_payload(context: IncidentContext) -> dict[str, Any]:
    """Convert an `IncidentContext` into its JSON-safe structured representation."""
    return context.model_dump(mode="json")


def incident_context_from_payload(payload: dict[str, Any]) -> IncidentContext:
    """Rebuild an `IncidentContext` from a payload; raises `pydantic.ValidationError` if invalid."""
    return IncidentContext.model_validate(payload)
