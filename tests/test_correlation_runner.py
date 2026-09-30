from datetime import datetime, timezone

import httpx
import pytest

from shared.correlation import AnomalyRule, ServiceRelationship
from shared.correlation.runner import run_correlation

START = datetime(2023, 11, 14, 22, 0, 0, tzinfo=timezone.utc)
END = datetime(2023, 11, 14, 22, 30, 0, tzinfo=timezone.utc)
RULES = [AnomalyRule(metric_name="error_rate", threshold=0.1)]


def _prom_body(value: str) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {"service": "payment"}, "value": [1_700_000_000, value]}],
        },
    }


JAEGER_BODY = {
    "data": [
        {
            "processes": {
                "p1": {"serviceName": "gateway"},
                "p2": {"serviceName": "order"},
                "p3": {"serviceName": "payment"},
            },
            "spans": [
                {
                    "traceID": "t",
                    "spanID": "a",
                    "processID": "p1",
                    "startTime": 1_700_000_000_000_000,
                },
                {
                    "traceID": "t",
                    "spanID": "b",
                    "processID": "p2",
                    "startTime": 1_700_000_000_100_000,
                    "references": [{"refType": "CHILD_OF", "traceID": "t", "spanID": "a"}],
                },
                {
                    "traceID": "t",
                    "spanID": "c",
                    "processID": "p3",
                    "startTime": 1_700_000_000_200_000,
                    "references": [{"refType": "CHILD_OF", "traceID": "t", "spanID": "b"}],
                },
            ],
        }
    ]
}


def _clients(prom_handler, jaeger_handler):
    prom = httpx.Client(base_url="http://prom", transport=httpx.MockTransport(prom_handler))
    jaeger = httpx.Client(base_url="http://jaeger", transport=httpx.MockTransport(jaeger_handler))
    return prom, jaeger


def _run(prom, jaeger):
    return run_correlation(
        prom, jaeger, START, END, "rate(errors[5m])", "error_rate", RULES, "gateway"
    )


def test_run_passes_window_and_query_to_backends():
    seen = {}

    def prom_handler(request: httpx.Request) -> httpx.Response:
        seen["prom"] = request
        return httpx.Response(200, json=_prom_body("0.5"))

    def jaeger_handler(request: httpx.Request) -> httpx.Response:
        seen["jaeger"] = request
        return httpx.Response(200, json=JAEGER_BODY)

    _run(*_clients(prom_handler, jaeger_handler))

    assert seen["prom"].url.params["query"] == "rate(errors[5m])"
    assert float(seen["prom"].url.params["time"]) == END.timestamp()
    params = seen["jaeger"].url.params
    assert params["service"] == "gateway"
    assert int(params["start"]) == int(START.timestamp() * 1_000_000)
    assert int(params["end"]) == int(END.timestamp() * 1_000_000)


def test_run_builds_incident_context_from_fetched_data():
    prom, jaeger = _clients(
        lambda r: httpx.Response(200, json=_prom_body("0.5")),
        lambda r: httpx.Response(200, json=JAEGER_BODY),
    )
    context = _run(prom, jaeger)

    assert context.window_start == START
    assert context.window_end == END
    assert context.affected_services == ["payment"]
    assert [(a.service, a.value, a.threshold) for a in context.anomalies] == [("payment", 0.5, 0.1)]
    # only the one-hop edge touching the affected service; gateway -> order is excluded
    assert context.relationships == [ServiceRelationship(caller="order", callee="payment")]


def test_run_with_no_anomaly_yields_empty_context():
    prom, jaeger = _clients(
        lambda r: httpx.Response(200, json=_prom_body("0.05")),
        lambda r: httpx.Response(200, json=JAEGER_BODY),
    )
    context = _run(prom, jaeger)

    assert context.affected_services == []
    assert context.anomalies == []
    assert context.relationships == []


def test_run_propagates_prometheus_error():
    prom, jaeger = _clients(
        lambda r: httpx.Response(503), lambda r: httpx.Response(200, json=JAEGER_BODY)
    )
    with pytest.raises(httpx.HTTPStatusError):
        _run(prom, jaeger)


def test_run_propagates_jaeger_error():
    prom, jaeger = _clients(
        lambda r: httpx.Response(200, json=_prom_body("0.5")), lambda r: httpx.Response(500)
    )
    with pytest.raises(httpx.HTTPStatusError):
        _run(prom, jaeger)
