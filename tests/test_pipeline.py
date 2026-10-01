import httpx
import pytest

from shared import pipeline
from shared.investigator import EvidenceRef, Hypothesis, InvestigatorInput
from tests.test_correlation_runner import END, JAEGER_BODY, RULES, START, _clients, _prom_body


def _ok_clients():
    return _clients(
        lambda r: httpx.Response(200, json=_prom_body("0.5")),
        lambda r: httpx.Response(200, json=JAEGER_BODY),
    )


def _run(investigator, clients=None, services=None):
    prom, jaeger = clients or _ok_clients()
    return pipeline.correlate_and_investigate(
        prom,
        jaeger,
        START,
        END,
        "rate(errors[5m])",
        "error_rate",
        RULES,
        "gateway",
        investigator,
        services,
    )


def _hypothesis(*refs: EvidenceRef) -> Hypothesis:
    return Hypothesis(
        origin_service="payment",
        root_cause="payment failing",
        confidence=0.7,
        supporting_evidence=list(refs),
    )


def test_correlated_context_reaches_investigator():
    seen: list[InvestigatorInput] = []

    def investigator(inp: InvestigatorInput) -> Hypothesis:
        seen.append(inp)
        return _hypothesis(EvidenceRef(kind="anomaly", index=0))

    _run(investigator)

    assert len(seen) == 1
    incident = seen[0].incident
    assert incident.affected_services == ["payment"]
    assert [a.service for a in incident.anomalies] == ["payment"]
    assert [(r.caller, r.callee) for r in incident.relationships] == [("order", "payment")]


def test_hypothesis_returned_unchanged():
    expected = _hypothesis(
        EvidenceRef(kind="anomaly", index=0), EvidenceRef(kind="relationship", index=0)
    )
    assert _run(lambda inp: expected) == expected


def test_uses_handoff_payload_boundary(monkeypatch):
    calls = []
    real = pipeline.incident_context_to_payload

    def spy(context):
        calls.append(context)
        return real(context)

    monkeypatch.setattr(pipeline, "incident_context_to_payload", spy)
    _run(lambda inp: _hypothesis(EvidenceRef(kind="anomaly", index=0)))

    assert len(calls) == 1


def test_fetch_error_propagates_and_investigator_not_called():
    called = []
    clients = _clients(
        lambda r: httpx.Response(503), lambda r: httpx.Response(200, json=JAEGER_BODY)
    )
    with pytest.raises(httpx.HTTPStatusError):
        _run(lambda inp: called.append(inp), clients)
    assert called == []


def test_evidence_outside_input_propagates():
    with pytest.raises(ValueError, match="not in incident context"):
        _run(lambda inp: _hypothesis(EvidenceRef(kind="anomaly", index=5)))


def test_invalid_hypothesis_propagates():
    with pytest.raises(ValueError):
        _run(lambda inp: {"root_cause": "", "confidence": 2, "supporting_evidence": []})


# ------------------------------------------------- no anomaly: healthy vs not observed

SERVICES = ["gateway", "order", "payment"]


def _vector(rows: list[tuple[str, str]]) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {"service": service}, "value": [1_700_000_000, value]}
                for service, value in rows
            ],
        },
    }


def _clients_for(body: dict):
    return _clients(
        lambda r: httpx.Response(200, json=body), lambda r: httpx.Response(200, json=JAEGER_BODY)
    )


def _never_called(inp: InvestigatorInput) -> Hypothesis:
    pytest.fail("the investigator must not be called when no anomaly was found")


def test_fully_observed_healthy_window_is_no_incident():
    healthy = _vector([("gateway", "0.01"), ("order", "0.02"), ("payment", "0.03")])
    result = _run(_never_called, _clients_for(healthy), SERVICES)

    assert isinstance(result, pipeline.NoIncident)
    assert (result.window_start, result.window_end) == (START, END)
    assert {c.service: c.status for c in result.metric_coverage} == dict.fromkeys(
        SERVICES, "observed"
    )


@pytest.mark.parametrize(
    ("body", "services", "reason"),
    [
        (_vector([]), SERVICES, "gateway (unobserved), order (unobserved), payment (unobserved)"),
        (
            _vector([("gateway", "NaN"), ("order", "NaN"), ("payment", "NaN")]),
            SERVICES,
            "(undefined)",
        ),
        (_vector([("gateway", "0.01")]), SERVICES, "order (unobserved), payment (unobserved)"),
        (
            _vector([("gateway", "0.01"), ("order", "0.02"), ("payment", "NaN")]),
            SERVICES,
            "payment (undefined)",
        ),
        (
            _vector([("gateway", "0.01"), ("order", "0.02"), ("payment", "0.03")]),
            None,
            "no services were declared",
        ),
        (_vector([]), None, "no services were declared"),
        (
            _vector([("gateway", "0.01"), ("order", "0.02"), ("payment", "0.03")]),
            [],
            "no services were declared",
        ),
    ],
    ids=[
        "empty-vector",
        "all-nan",
        "only-gateway-observed",
        "one-undefined",
        "healthy-but-coverage-undeclared",
        "empty-and-undeclared",
        "empty-services-list",
    ],
)
def test_insufficient_telemetry_is_no_observation_not_no_incident(body, services, reason):
    result = _run(_never_called, _clients_for(body), services)

    assert isinstance(result, pipeline.NoObservation)
    assert not isinstance(result, pipeline.NoIncident)
    assert reason in result.reason
    assert (result.window_start, result.window_end) == (START, END)


def test_no_observation_carries_the_declared_coverage():
    result = _run(_never_called, _clients_for(_vector([("gateway", "0.01")])), SERVICES)

    assert isinstance(result, pipeline.NoObservation)
    assert {c.service: c.status for c in result.metric_coverage} == {
        "gateway": "observed",
        "order": "unobserved",
        "payment": "unobserved",
    }
    undeclared = _run(_never_called, _clients_for(_vector([])), None)
    assert undeclared.metric_coverage == ()


def test_services_outside_the_declaration_do_not_make_the_window_observed():
    # Prometheus returns only an extra service; the declared ones have no series.
    result = _run(_never_called, _clients_for(_vector([("billing", "0.01")])), ["gateway"])
    assert isinstance(result, pipeline.NoObservation) and "gateway (unobserved)" in result.reason


def test_a_declared_subset_that_is_fully_observed_is_no_incident():
    result = _run(_never_called, _clients_for(_vector([("payment", "0.01")])), ["payment"])
    assert isinstance(result, pipeline.NoIncident)


def test_an_anomaly_reaches_the_investigator_even_with_incomplete_coverage():
    seen = []

    def investigator(inp: InvestigatorInput) -> Hypothesis:
        seen.append(inp)
        return _hypothesis(EvidenceRef(kind="anomaly", index=0))

    anomalous = _vector([("payment", "0.5")])  # order/gateway unobserved, payment above 0.1
    result = _run(investigator, _clients_for(anomalous), SERVICES)

    assert isinstance(result, Hypothesis) and len(seen) == 1


def test_an_anomaly_still_reaches_the_investigator_and_returns_a_hypothesis():
    expected = _hypothesis(EvidenceRef(kind="anomaly", index=0))
    result = _run(lambda inp: expected)
    assert isinstance(result, Hypothesis) and result == expected
