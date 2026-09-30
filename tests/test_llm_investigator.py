import json
from datetime import datetime, timezone

import httpx
import pytest
from pydantic import ValidationError

from shared.correlation import Anomaly, IncidentContext, ServiceRelationship
from shared.correlation.handoff import incident_context_to_payload
from shared.investigator import EvidenceRef, Hypothesis, build_investigator_input, investigate
from shared.investigator.anthropic import (
    ProviderError,
    anthropic_complete,
    anthropic_investigator_from_env,
    wire_schema,
)
from shared.investigator.llm import LLMInvestigator, Prompt, build_prompt

START = datetime(2023, 11, 14, 22, 0, 0, tzinfo=timezone.utc)
END = datetime(2023, 11, 14, 22, 30, 0, tzinfo=timezone.utc)


def _payload() -> dict:
    return incident_context_to_payload(
        IncidentContext(
            affected_services=["payment"],
            relationships=[ServiceRelationship(caller="order", callee="payment")],
            window_start=START,
            window_end=END,
            anomalies=[
                Anomaly(
                    service="payment",
                    metric_name="error_rate",
                    value=0.5,
                    threshold=0.1,
                    timestamp=END,
                )
            ],
        )
    )


def _answer(**overrides) -> str:
    fields = {
        "root_cause": "payment service is failing",
        "confidence": 0.7,
        "supporting_evidence": [
            {"kind": "anomaly", "index": 0},
            {"kind": "relationship", "index": 0},
        ],
    }
    fields.update(overrides)
    return json.dumps(fields)


def _fake(raw: str, calls: list | None = None):
    def complete(prompt: Prompt, schema: dict) -> str:
        if calls is not None:
            calls.append((prompt, schema))
        return raw

    return complete


def test_prompt_is_bounded_and_indexed():
    calls: list = []
    investigate(_payload(), LLMInvestigator(_fake(_answer(), calls)))
    prompt, schema = calls[0]
    assert "complete available evidence" in prompt.system
    assert "Do not claim evidence that is not present" in prompt.system
    assert "exactly one hypothesis" in prompt.system
    assert "not proof" in prompt.system
    context = json.loads(prompt.user.split("\n", 1)[1])
    assert context["anomalies"][0]["index"] == 0
    assert context["anomalies"][0]["metric_name"] == "error_rate"
    assert context["relationships"] == [{"index": 0, "caller": "order", "callee": "payment"}]
    assert context["affected_services"] == ["payment"]
    assert schema == Hypothesis.model_json_schema()


def test_prompt_defines_relationship_direction_semantics():
    system = build_prompt(build_investigator_input(_payload())).system
    assert "caller -> callee" in system
    assert "caller invoked the callee" in system
    assert "can include time spent waiting for the callee" in system
    assert "Never reverse this direction" in system
    assert "does not by itself show that any service is the root cause" in system


def test_valid_output_becomes_hypothesis_with_evidence_preserved():
    result = investigate(_payload(), LLMInvestigator(_fake(_answer())))
    assert result == Hypothesis(
        root_cause="payment service is failing",
        confidence=0.7,
        supporting_evidence=[
            EvidenceRef(kind="anomaly", index=0),
            EvidenceRef(kind="relationship", index=0),
        ],
    )
    assert json.loads(json.dumps(result.model_dump(mode="json"))) == result.model_dump(mode="json")


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        json.dumps({"root_cause": "x"}),
        _answer(root_cause=""),
        _answer(confidence=1.5),
        _answer(confidence=-0.1),
        _answer(supporting_evidence=[]),
        _answer(supporting_evidence=[{"kind": "log", "index": 0}]),
        _answer(supporting_evidence=[{"kind": "anomaly", "index": 0}] * 2),
    ],
)
def test_malformed_or_invalid_output_is_rejected(raw):
    with pytest.raises(ValidationError):
        investigate(_payload(), LLMInvestigator(_fake(raw)))


def test_evidence_outside_the_incident_is_rejected():
    raw = _answer(supporting_evidence=[{"kind": "anomaly", "index": 5}])
    with pytest.raises(ValueError, match="not in incident context"):
        investigate(_payload(), LLMInvestigator(_fake(raw)))


def test_provider_errors_propagate():
    def boom(prompt, schema):
        raise ConnectionError("provider down")

    with pytest.raises(ConnectionError, match="provider down"):
        investigate(_payload(), LLMInvestigator(boom))


def test_provider_is_substitutable():
    a = investigate(_payload(), LLMInvestigator(_fake(_answer(confidence=0.2))))
    b = investigate(_payload(), LLMInvestigator(_fake(_answer(confidence=0.9))))
    assert (a.confidence, b.confidence) == (0.2, 0.9)


def _anthropic(handler) -> LLMInvestigator:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return LLMInvestigator(anthropic_complete(api_key="k", model="m", client=client))


def test_anthropic_request_and_response():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers["x-api-key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"stop_reason": "end_turn", "content": [{"type": "text", "text": _answer()}]}
        )

    result = investigate(_payload(), _anthropic(handler))
    assert result.confidence == 0.7
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["key"] == "k"
    body = seen["body"]
    assert body["model"] == "m" and "tools" not in body
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["additionalProperties"] is False


def test_anthropic_http_error_and_empty_responses_surface():
    with pytest.raises(httpx.HTTPStatusError):
        investigate(_payload(), _anthropic(lambda r: httpx.Response(500, json={})))
    with pytest.raises(ProviderError, match="refusal"):
        investigate(
            _payload(),
            _anthropic(lambda r: httpx.Response(200, json={"stop_reason": "refusal"})),
        )
    with pytest.raises(ProviderError, match="no text"):
        investigate(
            _payload(),
            _anthropic(lambda r: httpx.Response(200, json={"stop_reason": "end_turn"})),
        )


def test_wire_schema_drops_unsupported_keywords():
    dumped = json.dumps(wire_schema(Hypothesis.model_json_schema()))
    for keyword in ("minimum", "maximum", "minLength", "minItems"):
        assert keyword not in dumped


def test_missing_configuration_fails_clearly(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY, ANTHROPIC_MODEL"):
        anthropic_investigator_from_env()


def test_prompt_exposes_observed_and_unobserved_coverage_and_its_semantics():
    payload = _payload()
    payload["metric_coverage"] = [
        {"metric_name": "error_rate", "service": "order", "status": "unobserved"},
        {"metric_name": "error_rate", "service": "payment", "status": "observed"},
    ]
    prompt = build_prompt(build_investigator_input(payload))
    context = json.loads(prompt.user.split("\n", 1)[1])
    assert context["metric_coverage"] == payload["metric_coverage"]
    assert context["affected_services"] == ["payment"]  # coverage does not redefine affected
    assert '"unobserved"' in prompt.system
    assert "must not be treated as healthy" in prompt.system
    assert "not evidence of health" in prompt.system


def test_prompt_with_no_declared_coverage_shows_an_empty_list():
    prompt = build_prompt(build_investigator_input(_payload()))
    assert json.loads(prompt.user.split("\n", 1)[1])["metric_coverage"] == []
    assert "empty list means no coverage was declared" in prompt.system.replace("\n", " ")


def test_prompt_is_deterministic_with_coverage():
    payload = _payload()
    payload["metric_coverage"] = [{"metric_name": "m", "service": "s", "status": "observed"}]
    a = build_prompt(build_investigator_input(payload))
    b = build_prompt(build_investigator_input(payload))
    assert a == b


def test_prompt_exposes_unobserved_dependencies_and_their_non_causal_semantics():
    payload = _payload()
    payload["metric_coverage"] = [
        {"metric_name": "error_rate", "service": "payment", "status": "undefined"}
    ]
    payload["unobserved_dependencies"] = [
        {
            "caller": "order",
            "callee": "payment",
            "metric_name": "error_rate",
            "callee_status": "undefined",
        }
    ]
    prompt = build_prompt(build_investigator_input(payload))
    context = json.loads(prompt.user.split("\n", 1)[1])
    assert context["unobserved_dependencies"] == payload["unobserved_dependencies"]
    system = prompt.system.replace("\n", " ")
    assert '"undefined"' in system and "no numeric value" in system
    assert "unobserved_dependencies lists relationships" in system
    assert "does not show that the callee is or is not the cause" in system


def test_prompt_with_no_unobserved_dependencies_shows_an_empty_list():
    prompt = build_prompt(build_investigator_input(_payload()))
    assert json.loads(prompt.user.split("\n", 1)[1])["unobserved_dependencies"] == []
