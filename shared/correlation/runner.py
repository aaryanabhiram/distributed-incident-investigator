"""Time-window runner: fetch Prometheus samples + Jaeger spans, then run the correlation core.

Orchestration only. Fetching lives in `fetch.py`, parsing in `adapters.py`, detection and
correlation in the core (`shared.correlation`). Clients are supplied by the caller, and the
query, metric name, anomaly rules and Jaeger service are explicit inputs — none of them is a
production alerting policy. Fetch errors propagate unchanged.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from shared.correlation import (
    AnomalyRule,
    IncidentContext,
    build_incident_context,
    compute_metric_coverage,
    detect_anomalies,
)
from shared.correlation.fetch import (
    fetch_jaeger_spans,
    fetch_prometheus_vector,
    require_aware,
)


def run_correlation(
    prometheus: httpx.Client,
    jaeger: httpx.Client,
    window_start: datetime,
    window_end: datetime,
    query: str,
    metric_name: str,
    rules: list[AnomalyRule],
    trace_service: str,
    services: list[str] | None = None,
) -> IncidentContext:
    """Build an `IncidentContext` for [window_start, window_end].

    The PromQL instant query is evaluated at `window_end`; the caller's `query` is responsible
    for aggregating over the window (e.g. a `[5m]` range selector). Jaeger traces involving
    `trace_service` in the window are fetched, and their parent/child links give the
    relationships.

    `services` optionally names the services the metric query is meant to cover; each is marked
    observed, undefined (NaN series) or unobserved (no series) in `metric_coverage`; affected
    callers of a non-observed callee are listed in `unobserved_dependencies`. Omitted, no
    coverage is declared. Anomaly and relationship semantics are unaffected.

    `window_start` and `window_end` must be timezone-aware; naive datetimes raise `ValueError`
    before any backend is queried.
    """
    require_aware("window_start", window_start)
    require_aware("window_end", window_end)
    samples, undefined_services = fetch_prometheus_vector(
        prometheus, query, metric_name, at=window_end
    )
    spans = fetch_jaeger_spans(jaeger, trace_service, window_start, window_end)
    anomalies = detect_anomalies(samples, rules)
    coverage = (
        compute_metric_coverage(samples, services, metric_name, undefined_services)
        if services
        else None
    )
    return build_incident_context(anomalies, spans, window_start, window_end, coverage)
