"""Deterministic incident-correlation core.

This module never talks to a live service, Prometheus, or Jaeger. It operates entirely on
typed, in-memory telemetry data supplied by a caller (a later milestone's adapter is
responsible for turning real Prometheus/Jaeger responses into these types). That separation is
what keeps this layer fully testable without any of those backends running, per
docs/architecture.md's deterministic incident-correlation layer.

Pipeline:

    MetricSample[] --> detect_anomalies() --> Anomaly[]
    SpanRecord[]   --> extract_relationships() --> ServiceRelationship[]
    (Anomaly[], SpanRecord[]) --> build_incident_context() --> IncidentContext

No narrative text and no LLM call happens anywhere in this module.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Typed telemetry input
# ---------------------------------------------------------------------------


class MetricSample(BaseModel):
    """One observed metric value for one service over one window.

    Mirrors what a Prometheus range/instant query for
    `http_server_duration_milliseconds` or a derived error-rate query would yield per service,
    normalized to the fields the anomaly rule actually needs.
    """

    service: str
    metric_name: str
    timestamp: datetime
    value: float


class SpanRecord(BaseModel):
    """One span from a trace, normalized from what Jaeger/OTel already records.

    `parent_span_id=None` marks a root span (the inbound request that started the trace, e.g.
    the client's call into `gateway`). A span whose `parent_span_id` matches another span's
    `span_id` in a different `service` is a cross-service call — that parent/child link, not
    the service names themselves, is what `extract_relationships` uses.
    """

    trace_id: str
    span_id: str
    parent_span_id: str | None
    service: str
    start_time: datetime


# ---------------------------------------------------------------------------
# Deterministic correlation output
# ---------------------------------------------------------------------------


class ServiceRelationship(BaseModel):
    """A directed caller -> callee edge observed between two services in a trace."""

    caller: str
    callee: str


class Anomaly(BaseModel):
    """A single metric sample that violated the configured detection rule."""

    service: str
    metric_name: str
    value: float
    threshold: float
    timestamp: datetime


class MetricCoverage(BaseModel):
    """Whether one service was actually observed by the anomaly metric in the window.

    "observed": the metric query returned usable telemetry for the service. "unobserved": it
    did not, i.e. the service is outside the coverage represented here. This is separate from
    anomalies: an observed service with no anomaly was measured and stayed under the threshold;
    an unobserved service says nothing about its health.
    """

    metric_name: str
    service: str
    status: Literal["observed", "unobserved"]


class IncidentContext(BaseModel):
    """Bounded, structured record of what was observed during an incident window.

    This is the correlation layer's entire output: affected services, the relationships
    between them, the time window, and the supporting evidence — never a narrative. A future
    LLM investigator reads this record; it does not exist yet and this type does not depend on
    it. Log evidence is intentionally not represented yet: the repository has no programmatic
    log store to source it from (stdout JSON only), so adding a field for it now would be
    unused and untestable.
    """

    affected_services: list[str]
    relationships: list[ServiceRelationship]
    window_start: datetime
    window_end: datetime
    anomalies: list[Anomaly]
    # Empty means no coverage was declared (the caller named no services), not "all observed".
    metric_coverage: list[MetricCoverage] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Anomaly detection
# ---------------------------------------------------------------------------


class AnomalyRule(BaseModel):
    """A deterministic, fixture-scale threshold rule for one metric.

    This is a Milestone 5 correlation-core fixture/policy, not a validated production
    threshold — the repository does not define real-world alerting thresholds anywhere, and
    inventing one would be undocumented behavior. The rule exists so anomaly detection has an
    explicit, visible, testable knob rather than a hardcoded magic number.
    """

    metric_name: str
    threshold: float = Field(description="A sample exceeding this value counts as an anomaly")


def detect_anomalies(samples: list[MetricSample], rules: list[AnomalyRule]) -> list[Anomaly]:
    """Flag every sample that exceeds its metric's configured threshold.

    Deterministic: same samples + rules always produce the same anomalies, in the same order
    (input order, filtered). No time-based or random behavior.
    """
    thresholds = {rule.metric_name: rule.threshold for rule in rules}

    anomalies = []
    for sample in samples:
        threshold = thresholds.get(sample.metric_name)
        if threshold is not None and sample.value > threshold:
            anomalies.append(
                Anomaly(
                    service=sample.service,
                    metric_name=sample.metric_name,
                    value=sample.value,
                    threshold=threshold,
                    timestamp=sample.timestamp,
                )
            )
    return anomalies


def compute_metric_coverage(
    samples: list[MetricSample], services: list[str], metric_name: str
) -> list[MetricCoverage]:
    """Mark each named service observed if `samples` hold a sample of `metric_name` for it.

    Coverage comes from the samples, never from anomalies: a service that was measured below
    the threshold is observed. Output is sorted by service for determinism.
    """
    seen = {s.service for s in samples if s.metric_name == metric_name}
    return [
        MetricCoverage(
            metric_name=metric_name,
            service=service,
            status="observed" if service in seen else "unobserved",
        )
        for service in sorted(set(services))
    ]


# ---------------------------------------------------------------------------
# Trace relationship correlation
# ---------------------------------------------------------------------------


def extract_relationships(spans: list[SpanRecord]) -> list[ServiceRelationship]:
    """Derive directed service-to-service edges from parent/child span links.

    An edge is created only when a span's parent span belongs to a *different* service than
    the span itself — that is what a real cross-service HTTP call looks like under OpenTelemetry
    auto-instrumentation (client span in the caller, server span in the callee, linked by
    trace context propagation). Spans within the same service, or with no parent, never
    produce an edge. Duplicate edges collapse to one; the result is sorted for deterministic
    output ordering regardless of input order.
    """
    spans_by_id = {span.span_id: span for span in spans}

    edges: set[tuple[str, str]] = set()
    for span in spans:
        if span.parent_span_id is None:
            continue
        parent = spans_by_id.get(span.parent_span_id)
        if parent is None or parent.service == span.service:
            continue
        edges.add((parent.service, span.service))

    return [ServiceRelationship(caller=caller, callee=callee) for caller, callee in sorted(edges)]


# ---------------------------------------------------------------------------
# Incident context builder
# ---------------------------------------------------------------------------


def build_incident_context(
    anomalies: list[Anomaly],
    spans: list[SpanRecord],
    window_start: datetime,
    window_end: datetime,
    metric_coverage: list[MetricCoverage] | None = None,
) -> IncidentContext:
    """Combine anomalies and trace-derived relationships into one structured incident context.

    Affected services are exactly the services that produced an anomaly. Relationships are
    limited to those with an anomalous service as caller or callee — the immediate neighbors of
    the anomaly, not a multi-hop blast radius (deliberately: expanding transitively would make
    the result depend on how much of the call graph the caller happened to supply, which is not
    deterministic in a useful sense). Deterministic: identical input always produces an
    identical output, independent of input ordering.
    """
    relationships = extract_relationships(spans)

    affected: set[str] = {anomaly.service for anomaly in anomalies}

    relevant_relationships = [
        relationship
        for relationship in relationships
        if relationship.caller in affected or relationship.callee in affected
    ]

    return IncidentContext(
        affected_services=sorted(affected),
        relationships=relevant_relationships,
        window_start=window_start,
        window_end=window_end,
        anomalies=sorted(anomalies, key=lambda a: (a.service, a.metric_name, a.timestamp)),
        metric_coverage=list(metric_coverage or []),
    )
