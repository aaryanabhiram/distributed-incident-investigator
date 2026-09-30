"""Pure adapters from real backend response payloads to the correlation core's typed inputs.

These functions take the already-decoded JSON (`dict`) of a Prometheus HTTP API response or a
Jaeger `/api/traces` response and return `MetricSample` / `SpanRecord` lists. They perform no
I/O: fetching the payloads is a separate concern, so this translation layer is testable with
recorded fixtures and the correlation core stays independent of both backends.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from shared.correlation import MetricSample, SpanRecord


def _vector_result(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("status") != "success":
        raise ValueError(f"Prometheus query did not succeed: {payload.get('status')!r}")
    data = payload.get("data", {})
    if data.get("resultType") != "vector":
        raise ValueError(f"expected a vector result, got {data.get('resultType')!r}")
    return data.get("result", [])


def parse_prometheus_vector(
    payload: dict[str, Any], metric_name: str, service_label: str = "service"
) -> list[MetricSample]:
    """Convert a Prometheus instant-query response (`resultType: vector`) to MetricSamples.

    The query itself determines what the value means, so the caller names it via `metric_name`
    (matching an `AnomalyRule.metric_name`). The service comes from the `service` label that
    Prometheus attaches per scrape job (see docs/architecture.md). Series without that label
    and NaN values (e.g. a 0/0 error rate with no traffic) carry no usable evidence and are
    skipped.
    """
    samples = []
    for series in _vector_result(payload):
        service = series.get("metric", {}).get(service_label)
        timestamp, raw_value = series["value"]
        value = float(raw_value)
        if service is None or math.isnan(value):
            continue
        samples.append(
            MetricSample(
                service=service,
                metric_name=metric_name,
                timestamp=datetime.fromtimestamp(timestamp, tz=timezone.utc),
                value=value,
            )
        )
    return samples


def parse_prometheus_undefined_services(
    payload: dict[str, Any], service_label: str = "service"
) -> list[str]:
    """Services whose series is present in a vector response but has a NaN value.

    These are exactly the series `parse_prometheus_vector` skips as NaN. A NaN means the query
    evaluated to no number for that service (e.g. 0/0); it does not say why. Series without the
    service label are ignored. Sorted and de-duplicated.
    """
    return sorted(
        {
            series["metric"][service_label]
            for series in _vector_result(payload)
            if service_label in series.get("metric", {}) and math.isnan(float(series["value"][1]))
        }
    )


def parse_jaeger_traces(payload: dict[str, Any]) -> list[SpanRecord]:
    """Convert a Jaeger `/api/traces` response to SpanRecords.

    The service of each span is resolved through its `processID` into the trace's `processes`
    table. The parent is the span's `CHILD_OF` reference; a span without one is a root.
    Jaeger `startTime` is microseconds since the epoch.
    """
    spans = []
    for trace in payload.get("data", []):
        processes = trace.get("processes", {})
        for span in trace.get("spans", []):
            parent_span_id = next(
                (
                    ref["spanID"]
                    for ref in span.get("references", [])
                    if ref.get("refType") == "CHILD_OF"
                ),
                None,
            )
            spans.append(
                SpanRecord(
                    trace_id=span["traceID"],
                    span_id=span["spanID"],
                    parent_span_id=parent_span_id,
                    service=processes[span["processID"]]["serviceName"],
                    start_time=datetime.fromtimestamp(
                        span["startTime"] / 1_000_000, tz=timezone.utc
                    ),
                )
            )
    return spans
