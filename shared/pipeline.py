"""End-to-end entry point: correlate a time window, then hand the incident to an investigator.

Orchestration only. Correlation stays in `shared.correlation`, the investigator contract in
`shared.investigator`; this module just runs one after the other through the existing handoff
boundary. Errors from either layer propagate unchanged.

One decision lives here, for a context that holds no anomaly. A `Hypothesis` must cite at least
one evidence item and such a context has none, so no investigator could answer validly (and an LLM
would be paid to fail): the investigator is not called. What is returned instead depends on what
was observed, because an absent anomaly is only evidence of health where there was telemetry:

- `NoIncident`: every service the caller declared (`services`, the services the metric query is
  meant to cover) was `observed` and none was anomalous.
- `NoObservation`: anything else. No services declared (the required coverage is unknown), an
  empty or NaN result, or any declared service `undefined` or `unobserved`. Missing telemetry is
  never read as health; the coverage is returned so the caller can see what was missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx

from shared.correlation import AnomalyRule, IncidentContext, MetricCoverage
from shared.correlation.handoff import incident_context_to_payload
from shared.correlation.runner import run_correlation
from shared.investigator import Hypothesis, Investigator, investigate


@dataclass(frozen=True)
class NoIncident:
    """The window was fully observed and held no anomaly; no investigator was invoked."""

    window_start: datetime
    window_end: datetime
    metric_coverage: tuple[MetricCoverage, ...]  # every declared service, all "observed"


@dataclass(frozen=True)
class NoObservation:
    """No anomaly, but the telemetry was too incomplete to call the window healthy.

    No investigator was invoked. `reason` says what was missing; `metric_coverage` is what the
    context declared (empty when no services were declared).
    """

    window_start: datetime
    window_end: datetime
    metric_coverage: tuple[MetricCoverage, ...]
    reason: str


def _without_anomaly(
    context: IncidentContext,
    metric_name: str,
    services: list[str] | None,
    window_start: datetime,
    window_end: datetime,
) -> NoIncident | NoObservation:
    coverage = tuple(c for c in context.metric_coverage if c.metric_name == metric_name)
    if not services:
        return NoObservation(
            window_start,
            window_end,
            coverage,
            "no services were declared, so the required coverage is unknown",
        )
    status = {c.service: c.status for c in coverage}
    missing = sorted(
        f"{s} ({status.get(s, 'undeclared')})" for s in set(services) if status.get(s) != "observed"
    )
    if missing:
        return NoObservation(
            window_start, window_end, coverage, "not observed: " + ", ".join(missing)
        )
    return NoIncident(window_start, window_end, coverage)


def correlate_and_investigate(
    prometheus: httpx.Client,
    jaeger: httpx.Client,
    window_start: datetime,
    window_end: datetime,
    query: str,
    metric_name: str,
    rules: list[AnomalyRule],
    trace_service: str,
    investigator: Investigator,
    services: list[str] | None = None,
) -> Hypothesis | NoIncident | NoObservation:
    """Run `run_correlation`, pass its context through the handoff payload, and investigate.

    With no anomaly the investigator is not called: `NoIncident` if every declared service was
    observed, else `NoObservation` (fail closed, including when `services` is omitted). Otherwise
    the investigator's validated `Hypothesis`.
    """
    context = run_correlation(
        prometheus,
        jaeger,
        window_start,
        window_end,
        query,
        metric_name,
        rules,
        trace_service,
        services,
    )
    if not context.anomalies:
        return _without_anomaly(context, metric_name, services, window_start, window_end)
    return investigate(incident_context_to_payload(context), investigator)
