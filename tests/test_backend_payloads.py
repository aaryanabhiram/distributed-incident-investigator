"""Correlation pipeline against payloads captured from the real Compose stack.

Fixtures and their provenance: tests/fixtures/backends/README.md. Offline and deterministic.
"""

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import httpx

from shared.correlation import AnomalyRule, ServiceRelationship, extract_relationships
from shared.correlation.adapters import parse_jaeger_traces, parse_prometheus_vector
from shared.correlation.queries import (
    ERROR_RATIO_METRIC,
    LATENCY_METRIC,
    error_ratio_query,
    mean_latency_query,
)
from shared.correlation.runner import run_correlation

FIXTURES = Path(__file__).parent / "fixtures" / "backends"
EDGES = [
    ServiceRelationship(caller="gateway", callee="order"),
    ServiceRelationship(caller="order", callee="payment"),
]
SERVICES = ["gateway", "order", "payment"]
SEL = 'http_target=~"/checkout|/orders|/charge"'
COUNT = "http_server_duration_milliseconds_count"
SUM = "http_server_duration_milliseconds_sum"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_committed_queries_are_pinned():
    assert mean_latency_query("5m") == (
        f"sum by (service) (increase({SUM}{{{SEL}}}[5m]))"
        f" / sum by (service) (increase({COUNT}{{{SEL}}}[5m]))"
    )
    assert error_ratio_query("5m") == (
        f'sum by (service) (increase({COUNT}{{{SEL},http_status_code=~"5.."}}[5m]))'
        f" / sum by (service) (increase({COUNT}{{{SEL}}}[5m]))"
    )


def test_real_latency_vector_parses_with_float_timestamp_and_scrape_job_service():
    samples = parse_prometheus_vector(_load("prometheus_mean_latency_vector.json"), LATENCY_METRIC)

    assert {s.service for s in samples} == set(SERVICES)
    assert all(s.timestamp == datetime.fromtimestamp(1790752560, tz=timezone.utc) for s in samples)
    assert all(1400 < s.value < 1600 for s in samples)  # the 1500 ms latency fault


def test_real_error_ratio_vector_parses():
    samples = parse_prometheus_vector(
        _load("prometheus_error_ratio_vector.json"), ERROR_RATIO_METRIC
    )

    assert {s.service: s.value for s in samples} == dict.fromkeys(SERVICES, 1.0)


def test_real_nan_vector_is_skipped_and_real_empty_vector_is_empty():
    nan_body = _load("prometheus_error_ratio_nan_vector.json")
    values = [r["value"][1] for r in nan_body["data"]["result"]]
    assert values == ["NaN"] * 3 and math.isnan(float(values[0]))
    assert parse_prometheus_vector(nan_body, ERROR_RATIO_METRIC) == []

    assert parse_prometheus_vector(_load("prometheus_empty_vector.json"), LATENCY_METRIC) == []


def test_real_traces_yield_only_cross_service_edges():
    for name in ("jaeger_checkout_trace_ok.json", "jaeger_checkout_trace_err.json"):
        spans = parse_jaeger_traces(_load(name))

        # 14 spans per trace include same-service internal "http send/receive" spans, which
        # must not produce edges; Jaeger does not return spans in parent-first order.
        assert len(spans) == 14
        assert {s.service for s in spans} == set(SERVICES)
        assert sum(s.parent_span_id is None for s in spans) == 1
        assert extract_relationships(spans) == EDGES


def _run(prom_fixture: str, query: str, metric: str, threshold: float, trace_fixture: str):
    def prom(request: httpx.Request) -> httpx.Response:
        assert request.url.params["query"] == query
        return httpx.Response(200, json=_load(prom_fixture))

    def jaeger(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load(trace_fixture))

    return run_correlation(
        httpx.Client(base_url="http://prom", transport=httpx.MockTransport(prom)),
        httpx.Client(base_url="http://jaeger", transport=httpx.MockTransport(jaeger)),
        datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 30, 12, 5, tzinfo=timezone.utc),
        query,
        metric,
        [AnomalyRule(metric_name=metric, threshold=threshold)],
        "gateway",
        SERVICES,
    )


def test_latency_incident_end_to_end_from_real_payloads():
    context = _run(
        "prometheus_mean_latency_vector.json",
        mean_latency_query("5m"),
        LATENCY_METRIC,
        500,
        "jaeger_checkout_trace_ok.json",
    )

    assert context.affected_services == SERVICES
    assert context.relationships == EDGES
    assert len(context.anomalies) == 3
    assert {c.status for c in context.metric_coverage} == {"observed"}


def test_error_incident_end_to_end_and_no_traffic_reads_as_unobserved():
    context = _run(
        "prometheus_error_ratio_vector.json",
        error_ratio_query("5m"),
        ERROR_RATIO_METRIC,
        0.5,
        "jaeger_checkout_trace_err.json",
    )
    assert context.affected_services == SERVICES
    assert context.relationships == EDGES

    # Pins current behavior (a known gap, not a desired one): a real 0/0 NaN response and a
    # missing series are indistinguishable, so every service is reported unobserved.
    quiet = _run(
        "prometheus_error_ratio_nan_vector.json",
        error_ratio_query("5m"),
        ERROR_RATIO_METRIC,
        0.5,
        "jaeger_checkout_trace_err.json",
    )
    assert quiet.anomalies == []
    assert {c.status for c in quiet.metric_coverage} == {"unobserved"}
