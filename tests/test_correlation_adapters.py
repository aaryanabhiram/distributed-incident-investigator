from datetime import datetime, timezone

import pytest

from shared.correlation import (
    AnomalyRule,
    build_incident_context,
    detect_anomalies,
    extract_relationships,
)
from shared.correlation.adapters import parse_jaeger_traces, parse_prometheus_vector

TS = 1_767_268_800.0  # 2026-01-01T12:00:00Z


def _vector(*series: dict) -> dict:
    return {"status": "success", "data": {"resultType": "vector", "result": list(series)}}


def _series(service: str | None, value: str) -> dict:
    metric = {} if service is None else {"service": service}
    return {"metric": metric, "value": [TS, value]}


def test_prometheus_vector_maps_service_value_and_timestamp():
    samples = parse_prometheus_vector(
        _vector(_series("payment", "0.75"), _series("order", "0.1")), "error_rate"
    )

    assert [(s.service, s.metric_name, s.value) for s in samples] == [
        ("payment", "error_rate", 0.75),
        ("order", "error_rate", 0.1),
    ]
    assert samples[0].timestamp == datetime.fromtimestamp(TS, tz=timezone.utc)


def test_prometheus_skips_nan_and_series_without_service_label():
    samples = parse_prometheus_vector(
        _vector(_series("payment", "NaN"), _series(None, "1"), _series("order", "2")),
        "error_rate",
    )

    assert [s.service for s in samples] == ["order"]


def test_prometheus_rejects_failed_or_non_vector_responses():
    with pytest.raises(ValueError):
        parse_prometheus_vector({"status": "error", "error": "bad query"}, "error_rate")
    with pytest.raises(ValueError):
        parse_prometheus_vector(
            {"status": "success", "data": {"resultType": "matrix", "result": []}}, "error_rate"
        )


def _jaeger_payload() -> dict:
    def span(span_id: str, process: str, parent: str | None, micros: int) -> dict:
        refs = (
            [] if parent is None else [{"refType": "CHILD_OF", "traceID": "t1", "spanID": parent}]
        )
        return {
            "traceID": "t1",
            "spanID": span_id,
            "references": refs,
            "processID": process,
            "startTime": micros,
        }

    return {
        "data": [
            {
                "traceID": "t1",
                "processes": {
                    "p1": {"serviceName": "gateway"},
                    "p2": {"serviceName": "order"},
                    "p3": {"serviceName": "payment"},
                },
                "spans": [
                    span("a", "p1", None, 1_767_268_800_000_000),
                    span("b", "p2", "a", 1_767_268_800_100_000),
                    span("c", "p3", "b", 1_767_268_800_200_000),
                ],
            }
        ]
    }


def test_jaeger_traces_resolve_service_parent_and_start_time():
    spans = parse_jaeger_traces(_jaeger_payload())

    assert [(s.span_id, s.service, s.parent_span_id) for s in spans] == [
        ("a", "gateway", None),
        ("b", "order", "a"),
        ("c", "payment", "b"),
    ]
    assert spans[0].start_time == datetime.fromtimestamp(TS, tz=timezone.utc)


def test_parsed_payloads_feed_the_correlation_core_end_to_end():
    samples = parse_prometheus_vector(
        _vector(_series("payment", "0.9"), _series("order", "0.0")), "error_rate"
    )
    spans = parse_jaeger_traces(_jaeger_payload())

    anomalies = detect_anomalies(samples, [AnomalyRule(metric_name="error_rate", threshold=0.5)])
    context = build_incident_context(anomalies, spans, samples[0].timestamp, samples[0].timestamp)

    assert extract_relationships(spans)  # trace links survived parsing
    assert context.affected_services == ["payment"]
    assert [(r.caller, r.callee) for r in context.relationships] == [("order", "payment")]


def test_empty_jaeger_payload_yields_no_spans():
    assert parse_jaeger_traces({"data": []}) == []
