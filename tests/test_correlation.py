from datetime import datetime, timedelta

from shared.correlation import (
    AnomalyRule,
    MetricCoverage,
    MetricSample,
    ServiceRelationship,
    SpanRecord,
    build_incident_context,
    compute_metric_coverage,
    detect_anomalies,
    extract_relationships,
)

T0 = datetime(2026, 1, 1, 12, 0, 0)


def _sample(
    service: str, value: float, metric: str = "error_rate", offset: int = 0
) -> MetricSample:
    return MetricSample(
        service=service, metric_name=metric, timestamp=T0 + timedelta(seconds=offset), value=value
    )


def _span(
    span_id: str,
    service: str,
    parent_span_id: str | None,
    trace_id: str = "trace-1",
    offset: int = 0,
) -> SpanRecord:
    return SpanRecord(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        service=service,
        start_time=T0 + timedelta(seconds=offset),
    )


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


def test_metric_sample_requires_all_fields() -> None:
    sample = _sample("payment", 0.9)

    assert sample.service == "payment"
    assert sample.value == 0.9


def test_service_relationship_is_directed() -> None:
    relationship = ServiceRelationship(caller="gateway", callee="order")

    assert relationship.caller == "gateway"
    assert relationship.callee == "order"


# ---------------------------------------------------------------------------
# Anomaly detection
# ---------------------------------------------------------------------------


def test_detect_anomalies_flags_samples_over_threshold() -> None:
    rules = [AnomalyRule(metric_name="error_rate", threshold=0.5)]
    samples = [_sample("payment", 0.9), _sample("gateway", 0.1)]

    anomalies = detect_anomalies(samples, rules)

    assert len(anomalies) == 1
    assert anomalies[0].service == "payment"
    assert anomalies[0].threshold == 0.5


def test_detect_anomalies_returns_empty_for_no_anomalies() -> None:
    rules = [AnomalyRule(metric_name="error_rate", threshold=0.5)]
    samples = [_sample("payment", 0.1), _sample("gateway", 0.2)]

    assert detect_anomalies(samples, rules) == []


def test_detect_anomalies_ignores_metrics_without_a_rule() -> None:
    rules = [AnomalyRule(metric_name="error_rate", threshold=0.5)]
    samples = [_sample("payment", 999.0, metric="latency_ms")]

    assert detect_anomalies(samples, rules) == []


def test_detect_anomalies_handles_empty_input() -> None:
    assert detect_anomalies([], [AnomalyRule(metric_name="error_rate", threshold=0.5)]) == []


# ---------------------------------------------------------------------------
# Trace relationship extraction
# ---------------------------------------------------------------------------


def test_extract_relationships_from_gateway_order_payment_chain() -> None:
    spans = [
        _span("s1", "gateway", None),
        _span("s2", "order", "s1"),
        _span("s3", "payment", "s2"),
    ]

    relationships = extract_relationships(spans)

    assert relationships == [
        ServiceRelationship(caller="gateway", callee="order"),
        ServiceRelationship(caller="order", callee="payment"),
    ]


def test_extract_relationships_ignores_same_service_spans() -> None:
    spans = [
        _span("s1", "gateway", None),
        _span("s2", "gateway", "s1"),  # internal span, same service
        _span("s3", "order", "s2"),
    ]

    relationships = extract_relationships(spans)

    assert relationships == [ServiceRelationship(caller="gateway", callee="order")]


def test_extract_relationships_ignores_unrelated_traces_without_false_edges() -> None:
    spans = [
        _span("s1", "gateway", None, trace_id="trace-1"),
        _span("s2", "order", "s1", trace_id="trace-1"),
        _span("t1", "payment", None, trace_id="trace-2"),  # unrelated root span, no parent
    ]

    relationships = extract_relationships(spans)

    assert relationships == [ServiceRelationship(caller="gateway", callee="order")]
    assert all(r.caller != "payment" and r.callee != "payment" for r in relationships)


def test_extract_relationships_deduplicates_and_orders_deterministically() -> None:
    spans = [
        _span("s1", "gateway", None, trace_id="trace-1"),
        _span("s2", "order", "s1", trace_id="trace-1"),
        _span("s3", "gateway", None, trace_id="trace-2"),
        _span("s4", "order", "s3", trace_id="trace-2"),  # duplicate gateway->order edge
    ]

    relationships = extract_relationships(spans)

    assert relationships == [ServiceRelationship(caller="gateway", callee="order")]


def test_extract_relationships_handles_empty_input() -> None:
    assert extract_relationships([]) == []


# ---------------------------------------------------------------------------
# Incident context builder
# ---------------------------------------------------------------------------


def test_build_incident_context_combines_anomaly_and_relationships() -> None:
    spans = [
        _span("s1", "gateway", None),
        _span("s2", "order", "s1"),
        _span("s3", "payment", "s2"),
    ]
    anomalies = detect_anomalies(
        [_sample("payment", 0.9)], [AnomalyRule(metric_name="error_rate", threshold=0.5)]
    )

    context = build_incident_context(
        anomalies=anomalies, spans=spans, window_start=T0, window_end=T0 + timedelta(minutes=1)
    )

    assert context.affected_services == ["payment"]
    assert context.relationships == [ServiceRelationship(caller="order", callee="payment")]
    assert context.anomalies == anomalies
    assert context.window_start == T0


def test_build_incident_context_with_no_anomalies_has_no_affected_services() -> None:
    spans = [_span("s1", "gateway", None), _span("s2", "order", "s1")]

    context = build_incident_context(
        anomalies=[], spans=spans, window_start=T0, window_end=T0 + timedelta(minutes=1)
    )

    assert context.affected_services == []
    assert context.relationships == []
    assert context.anomalies == []


def test_build_incident_context_supports_multiple_affected_services() -> None:
    spans = [
        _span("s1", "gateway", None),
        _span("s2", "order", "s1"),
        _span("s3", "payment", "s2"),
    ]
    anomalies = detect_anomalies(
        [_sample("gateway", 0.9), _sample("payment", 0.9)],
        [AnomalyRule(metric_name="error_rate", threshold=0.5)],
    )

    context = build_incident_context(
        anomalies=anomalies, spans=spans, window_start=T0, window_end=T0 + timedelta(minutes=1)
    )

    assert context.affected_services == ["gateway", "payment"]
    assert len(context.relationships) == 2


def test_build_incident_context_is_deterministic_for_identical_input() -> None:
    spans = [
        _span("s1", "gateway", None),
        _span("s2", "order", "s1"),
        _span("s3", "payment", "s2"),
    ]
    anomalies = detect_anomalies(
        [_sample("payment", 0.9)], [AnomalyRule(metric_name="error_rate", threshold=0.5)]
    )

    first = build_incident_context(anomalies, spans, T0, T0 + timedelta(minutes=1))
    second = build_incident_context(anomalies, spans, T0, T0 + timedelta(minutes=1))

    assert first == second


def test_coverage_marks_sampled_services_observed_and_others_unobserved() -> None:
    samples = [_sample("gateway", 0.9), _sample("order", 0.0)]
    coverage = compute_metric_coverage(samples, ["payment", "order", "gateway"], "error_rate")
    assert coverage == [
        MetricCoverage(metric_name="error_rate", service="gateway", status="observed"),
        MetricCoverage(metric_name="error_rate", service="order", status="observed"),
        MetricCoverage(metric_name="error_rate", service="payment", status="unobserved"),
    ]


def test_no_anomaly_is_observed_not_unobserved() -> None:
    # order was measured below the threshold: healthy-as-observed, not "unobserved".
    samples = [_sample("order", 0.0)]
    (entry,) = compute_metric_coverage(samples, ["order"], "error_rate")
    assert entry.status == "observed"
    assert detect_anomalies(samples, [AnomalyRule(metric_name="error_rate", threshold=0.1)]) == []


def test_coverage_only_counts_samples_of_the_named_metric() -> None:
    samples = [_sample("order", 1.0, metric="latency")]
    (entry,) = compute_metric_coverage(samples, ["order"], "error_rate")
    assert entry.status == "unobserved"


def test_coverage_does_not_change_affected_services_or_anomalies() -> None:
    samples = [_sample("gateway", 0.9)]
    anomalies = detect_anomalies(samples, [AnomalyRule(metric_name="error_rate", threshold=0.1)])
    coverage = compute_metric_coverage(samples, ["gateway", "payment"], "error_rate")
    with_cov = build_incident_context(anomalies, [], T0, T0, coverage)
    without = build_incident_context(anomalies, [], T0, T0)
    assert with_cov.affected_services == without.affected_services == ["gateway"]
    assert with_cov.anomalies == without.anomalies
    assert without.metric_coverage == []  # no coverage declared
    assert compute_metric_coverage(samples, ["payment", "gateway", "payment"], "error_rate") == (
        coverage
    )  # deterministic and de-duplicated
