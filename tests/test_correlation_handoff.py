import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from shared.correlation import Anomaly, IncidentContext, MetricCoverage, ServiceRelationship
from shared.correlation.handoff import incident_context_from_payload, incident_context_to_payload

START = datetime(2023, 11, 14, 22, 0, 0, tzinfo=timezone.utc)
END = datetime(2023, 11, 14, 22, 30, 0, tzinfo=timezone.utc)


def _context() -> IncidentContext:
    return IncidentContext(
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


def test_payload_preserves_fields():
    payload = incident_context_to_payload(_context())
    assert payload["affected_services"] == ["payment"]
    assert payload["relationships"] == [{"caller": "order", "callee": "payment"}]
    assert payload["window_start"] == START.isoformat().replace("+00:00", "Z")
    assert payload["anomalies"][0]["value"] == 0.5
    assert payload["anomalies"][0]["threshold"] == 0.1


def test_payload_is_json_serializable():
    json.dumps(incident_context_to_payload(_context()))


def test_round_trip_is_lossless():
    context = _context()
    assert incident_context_from_payload(incident_context_to_payload(context)) == context


def test_empty_context():
    context = IncidentContext(
        affected_services=[], relationships=[], window_start=START, window_end=END, anomalies=[]
    )
    payload = incident_context_to_payload(context)
    assert payload["affected_services"] == []
    assert payload["anomalies"] == []
    assert incident_context_from_payload(payload) == context


def test_payload_is_deterministic():
    assert json.dumps(incident_context_to_payload(_context())) == json.dumps(
        incident_context_to_payload(_context())
    )


def test_malformed_payload_rejected():
    payload = incident_context_to_payload(_context())
    del payload["window_end"]
    with pytest.raises(ValidationError):
        incident_context_from_payload(payload)


def test_wrong_type_rejected():
    payload = incident_context_to_payload(_context())
    payload["affected_services"] = "payment"
    with pytest.raises(ValidationError):
        incident_context_from_payload(payload)


def test_coverage_survives_payload_round_trip():
    context = _context().model_copy(
        update={
            "metric_coverage": [
                MetricCoverage(metric_name="error_rate", service="order", status="unobserved"),
                MetricCoverage(metric_name="error_rate", service="payment", status="observed"),
            ]
        }
    )
    payload = incident_context_to_payload(context)
    assert payload["metric_coverage"] == [
        {"metric_name": "error_rate", "service": "order", "status": "unobserved"},
        {"metric_name": "error_rate", "service": "payment", "status": "observed"},
    ]
    json.dumps(payload)
    assert incident_context_from_payload(payload) == context
    assert payload == incident_context_to_payload(context)


def test_full_coverage_payload_without_the_field_still_loads():
    payload = incident_context_to_payload(_context())
    payload.pop("metric_coverage")
    assert incident_context_from_payload(payload).metric_coverage == []


def test_unobserved_dependencies_survive_payload_round_trip_and_default_to_empty():
    payload = incident_context_to_payload(_context())
    payload["unobserved_dependencies"] = [
        {"caller": "a", "callee": "b", "metric_name": "m", "callee_status": "undefined"}
    ]
    context = incident_context_from_payload(payload)
    assert incident_context_to_payload(context) == payload

    payload.pop("unobserved_dependencies")
    assert incident_context_from_payload(payload).unobserved_dependencies == []
