"""End-to-end entry point: correlate a time window, then hand the incident to an investigator.

Orchestration only. Correlation stays in `shared.correlation`, the investigator contract in
`shared.investigator`; this module just runs one after the other through the existing handoff
boundary. Errors from either layer propagate unchanged.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from shared.correlation import AnomalyRule
from shared.correlation.handoff import incident_context_to_payload
from shared.correlation.runner import run_correlation
from shared.investigator import Hypothesis, Investigator, investigate


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
) -> Hypothesis:
    """Run `run_correlation`, pass its context through the handoff payload, and investigate."""
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
    return investigate(incident_context_to_payload(context), investigator)
