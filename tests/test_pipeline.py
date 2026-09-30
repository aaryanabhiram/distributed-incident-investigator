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


def _run(investigator, clients=None):
    prom, jaeger = clients or _ok_clients()
    return pipeline.correlate_and_investigate(
        prom, jaeger, START, END, "rate(errors[5m])", "error_rate", RULES, "gateway", investigator
    )


def _hypothesis(*refs: EvidenceRef) -> Hypothesis:
    return Hypothesis(root_cause="payment failing", confidence=0.7, supporting_evidence=list(refs))


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
