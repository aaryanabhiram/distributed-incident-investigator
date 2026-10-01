import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from shared.correlation import Anomaly, IncidentContext, ServiceRelationship
from shared.correlation.handoff import incident_context_to_payload
from shared.investigator import (
    EvidenceRef,
    Hypothesis,
    InvestigatorInput,
    build_investigator_input,
    investigate,
)

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


def _hypothesis(**overrides) -> Hypothesis:
    fields = {
        "origin_service": "payment",
        "root_cause": "test-double cause",
        "confidence": 0.5,
        "supporting_evidence": [EvidenceRef(kind="anomaly", index=0)],
    }
    fields.update(overrides)
    return Hypothesis(**fields)


def test_handoff_payload_is_accepted_and_fields_survive():
    payload = _payload()
    investigator_input = build_investigator_input(payload)
    assert isinstance(investigator_input, InvestigatorInput)
    assert investigator_input.model_dump(mode="json") == {"incident": payload}
    incident = investigator_input.incident
    assert incident.affected_services == ["payment"]
    assert incident.relationships[0].caller == "order"
    assert incident.window_start == START and incident.window_end == END
    assert incident.anomalies[0].metric_name == "error_rate"


def test_invalid_payload_is_rejected():
    with pytest.raises(ValidationError):
        build_investigator_input({"affected_services": ["payment"]})


def test_valid_result_constructs():
    result = _hypothesis()
    assert result.confidence == 0.5


@pytest.mark.parametrize(
    "overrides",
    [
        {"root_cause": ""},
        {"confidence": 1.5},
        {"confidence": -0.1},
        {"supporting_evidence": []},
        {
            "supporting_evidence": [
                EvidenceRef(kind="anomaly", index=0),
                EvidenceRef(kind="anomaly", index=0),
            ]
        },
    ],
)
def test_invalid_results_are_rejected(overrides):
    with pytest.raises(ValidationError):
        _hypothesis(**overrides)


def test_invalid_evidence_ref_is_rejected():
    with pytest.raises(ValidationError):
        EvidenceRef(kind="log", index=0)
    with pytest.raises(ValidationError):
        EvidenceRef(kind="anomaly", index=-1)


def test_investigate_runs_test_double_and_validates_evidence():
    seen = []

    def double(investigator_input: InvestigatorInput) -> Hypothesis:
        seen.append(investigator_input)
        return _hypothesis()

    result = investigate(_payload(), double)
    assert result == _hypothesis()
    assert seen[0].incident.affected_services == ["payment"]


def test_investigate_rejects_evidence_outside_the_incident():
    def double(_: InvestigatorInput) -> Hypothesis:
        return _hypothesis(supporting_evidence=[EvidenceRef(kind="relationship", index=1)])

    with pytest.raises(ValueError, match="not in incident context"):
        investigate(_payload(), double)


def test_serialization_is_json_safe_and_deterministic():
    def double(_: InvestigatorInput) -> Hypothesis:
        return _hypothesis()

    first = investigate(_payload(), double)
    second = investigate(_payload(), double)
    dumped = json.dumps(first.model_dump(mode="json"), sort_keys=True)
    assert dumped == json.dumps(second.model_dump(mode="json"), sort_keys=True)
    assert Hypothesis.model_validate(json.loads(dumped)) == first
    input_json = json.dumps(build_investigator_input(_payload()).model_dump(mode="json"))
    assert input_json == json.dumps(build_investigator_input(_payload()).model_dump(mode="json"))


def test_status_defaults_to_identified_and_accepts_only_known_values():
    assert _hypothesis().status == "identified"
    assert _hypothesis(status="undetermined", origin_service=None).status == "undetermined"
    with pytest.raises(ValidationError):
        _hypothesis(status="unsure")


def test_validate_evidence_bounds_unobserved_dependency_references():
    payload = _payload()
    payload["unobserved_dependencies"] = [
        {"caller": "order", "callee": "payment", "metric_name": "m", "callee_status": "undefined"}
    ]
    ok = _hypothesis(supporting_evidence=[EvidenceRef(kind="unobserved_dependency", index=0)])
    assert investigate(payload, lambda _: ok) == ok

    bad = _hypothesis(supporting_evidence=[EvidenceRef(kind="unobserved_dependency", index=1)])
    with pytest.raises(ValueError, match=r"unobserved_dependency\[1\] not in incident context"):
        investigate(payload, lambda _: bad)


# ------------------------------------------------------------ structured origin


def test_identified_requires_a_non_empty_origin():
    with pytest.raises(ValidationError, match="origin_service"):
        _hypothesis(origin_service=None)
    with pytest.raises(ValidationError, match="origin_service"):
        _hypothesis(origin_service="")


def test_undetermined_requires_no_origin():
    with pytest.raises(ValidationError, match="origin_service=None"):
        _hypothesis(status="undetermined", origin_service="payment")
    assert _hypothesis(status="undetermined", origin_service=None).origin_service is None


def test_origin_is_not_inferred_from_root_cause_text():
    with pytest.raises(ValidationError):
        Hypothesis(
            root_cause="payment is the origin",
            confidence=0.5,
            supporting_evidence=[EvidenceRef(kind="anomaly", index=0)],
        )


def test_origin_must_name_a_service_in_the_input_context():
    assert investigate(_payload(), lambda _: _hypothesis(origin_service="order")).origin_service
    with pytest.raises(ValueError, match="'billing' is not a service"):
        investigate(_payload(), lambda _: _hypothesis(origin_service="billing"))


def test_origin_may_be_any_service_the_context_mentions():
    payload = _payload()
    payload["metric_coverage"] = [
        {"metric_name": "error_rate", "service": "ledger", "status": "unobserved"}
    ]
    assert investigate(payload, lambda _: _hypothesis(origin_service="ledger"))


def test_undetermined_with_no_origin_passes_origin_validation():
    undetermined = _hypothesis(status="undetermined", origin_service=None)
    assert investigate(_payload(), lambda _: undetermined) == undetermined


def test_legacy_identified_hypothesis_without_origin_no_longer_loads():
    legacy = {
        "root_cause": "x",
        "confidence": 0.5,
        "supporting_evidence": [{"kind": "anomaly", "index": 0}],
    }
    with pytest.raises(ValidationError):
        Hypothesis.model_validate(legacy)


def test_revalidate_runs_validators_on_instances_built_without_them():
    from shared.investigator import ContractViolation, revalidate

    assert revalidate(_hypothesis()) == _hypothesis()
    assert revalidate(_hypothesis().model_dump()) == _hypothesis()
    with pytest.raises(ValidationError):
        revalidate(_hypothesis().model_copy(update={"origin_service": None}))
    with pytest.raises(ValidationError):
        revalidate(Hypothesis.model_construct(status="identified", root_cause="x", confidence=0.5))
    with pytest.raises(TypeError):
        revalidate("nope")
    assert issubclass(ContractViolation, ValueError)


def test_investigate_rejects_an_unvalidated_malformed_instance():
    malformed = _hypothesis().model_copy(update={"origin_service": None})
    with pytest.raises(ValidationError):
        investigate(_payload(), lambda _: malformed)


def test_hypothesis_schema_description_states_only_the_output_contract():
    description = Hypothesis.model_json_schema()["description"]
    assert "identified" in description and "undetermined" in description
    for implementation_word in ("validate_origin", "validate_evidence", "serialized", "pydantic"):
        assert implementation_word not in description
