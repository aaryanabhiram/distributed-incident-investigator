from datetime import datetime, timezone

import httpx
import pytest

from shared.correlation import MetricSample
from shared.correlation.fetch import fetch_jaeger_spans, fetch_prometheus_samples

PROM_BODY = {
    "status": "success",
    "data": {
        "resultType": "vector",
        "result": [
            {"metric": {"service": "payment"}, "value": [1_700_000_000, "0.5"]},
            {"metric": {}, "value": [1_700_000_000, "0.9"]},
        ],
    },
}

JAEGER_BODY = {
    "data": [
        {
            "processes": {"p1": {"serviceName": "order"}, "p2": {"serviceName": "payment"}},
            "spans": [
                {
                    "traceID": "t1",
                    "spanID": "a",
                    "processID": "p1",
                    "startTime": 1_700_000_000_000_000,
                },
                {
                    "traceID": "t1",
                    "spanID": "b",
                    "processID": "p2",
                    "startTime": 1_700_000_000_500_000,
                    "references": [{"refType": "CHILD_OF", "traceID": "t1", "spanID": "a"}],
                },
            ],
        }
    ]
}


def _client(handler) -> httpx.Client:
    return httpx.Client(base_url="http://backend", transport=httpx.MockTransport(handler))


def test_fetch_prometheus_builds_request_and_uses_adapter():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=PROM_BODY)

    at = datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    samples = fetch_prometheus_samples(_client(handler), "rate(x[1m])", "error_rate", at=at)

    assert seen[0].url.path == "/api/v1/query"
    assert seen[0].url.params["query"] == "rate(x[1m])"
    assert float(seen[0].url.params["time"]) == at.timestamp()
    # the series without a service label is skipped by the adapter
    assert samples == [
        MetricSample(service="payment", metric_name="error_rate", timestamp=at, value=0.5)
    ]


def test_fetch_prometheus_omits_time_by_default():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=PROM_BODY)

    fetch_prometheus_samples(_client(handler), "up", "up")
    assert "time" not in seen[0].url.params


def test_fetch_jaeger_builds_request_and_uses_adapter():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=JAEGER_BODY)

    start = datetime(2023, 11, 14, 22, 0, 0, tzinfo=timezone.utc)
    end = datetime(2023, 11, 14, 22, 30, 0, tzinfo=timezone.utc)
    spans = fetch_jaeger_spans(_client(handler), "payment", start, end, limit=10)

    params = seen[0].url.params
    assert seen[0].url.path == "/api/traces"
    assert params["service"] == "payment"
    assert int(params["start"]) == int(start.timestamp() * 1_000_000)
    assert int(params["end"]) == int(end.timestamp() * 1_000_000)
    assert params["limit"] == "10"
    assert [(s.span_id, s.parent_span_id, s.service) for s in spans] == [
        ("a", None, "order"),
        ("b", "a", "payment"),
    ]


def test_fetch_raises_on_http_error():
    client = _client(lambda request: httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_prometheus_samples(client, "up", "up")
    now = datetime.now(timezone.utc)
    with pytest.raises(httpx.HTTPStatusError):
        fetch_jaeger_spans(client, "payment", now, now)


def test_fetch_raises_on_malformed_json():
    client = _client(lambda request: httpx.Response(200, content=b"<html>not json</html>"))
    with pytest.raises(ValueError):
        fetch_prometheus_samples(client, "up", "up")


def test_fetch_surfaces_adapter_rejection_of_failed_prometheus_query():
    client = _client(lambda request: httpx.Response(200, json={"status": "error"}))
    with pytest.raises(ValueError):
        fetch_prometheus_samples(client, "bad(", "x")
