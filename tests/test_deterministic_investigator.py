from datetime import datetime, timezone

import pytest

from shared.correlation import (
    Anomaly,
    IncidentContext,
    MetricCoverage,
    SpanRecord,
    build_incident_context,
)
from shared.correlation.handoff import incident_context_to_payload
from shared.investigator import InvestigatorInput, investigate
from shared.investigator.deterministic import (
    CONFIDENCE,
    RULESET_VERSION,
    DeterministicInvestigator,
)

START = datetime(2023, 11, 14, 22, 0, 0, tzinfo=timezone.utc)
END = datetime(2023, 11, 14, 22, 5, 0, tzinfo=timezone.utc)
METRIC = "http_server_duration_milliseconds"
CHAIN = [("gateway", "order"), ("order", "payment")]
ALL_OBSERVED = {"gateway": "observed", "order": "observed", "payment": "observed"}


def _span(trace: str, span: str, parent: str | None, service: str) -> SpanRecord:
    return SpanRecord(
        trace_id=trace, span_id=span, parent_span_id=parent, service=service, start_time=START
    )


def _context(anomalous, edges=CHAIN, coverage=ALL_OBSERVED) -> IncidentContext:
    """Built with the real `build_incident_context`, so derived fields are realistic."""
    anomalies = [
        Anomaly(service=s, metric_name=METRIC, value=v, threshold=500.0, timestamp=END)
        for s, v in anomalous.items()
    ]
    spans = []
    for n, (caller, callee) in enumerate(edges):
        spans.append(_span("t", f"c{n}", None, caller))
        spans.append(_span("t", f"d{n}", f"c{n}", callee))
    cov = [MetricCoverage(metric_name=METRIC, service=s, status=st) for s, st in coverage.items()]
    return build_incident_context(anomalies, spans, START, END, cov)


def _run(context: IncidentContext):
    payload = incident_context_to_payload(context)
    return investigate(payload, DeterministicInvestigator())  # validates evidence and origin


def _cited(h):
    return {(r.kind, r.index) for r in h.supporting_evidence}


def _with(ctx: IncidentContext, **update) -> IncidentContext:
    return ctx.model_copy(update=update)


FULL_CHAIN = {"gateway": 1516.0, "order": 1510.0, "payment": 1503.0}


# 1. single anomalous service with sufficient observed dependency context
def test_leaf_anomaly_identifies_the_leaf():
    h = _run(_context({"payment": 1500.0}))
    assert (h.status, h.origin_service) == ("identified", "payment")


def test_single_non_leaf_anomaly_with_healthy_observed_callee_is_identified():
    h = _run(_context({"gateway": 1500.0}))
    assert (h.status, h.origin_service) == ("identified", "gateway")
    assert ("relationship", 0) in _cited(h)


def test_order_anomaly_with_healthy_payment_identifies_order():
    h = _run(_context({"gateway": 1500.0, "order": 1490.0}))
    assert (h.status, h.origin_service) == ("identified", "order")


# 2. multiple anomalous services along one call chain
def test_full_chain_anomaly_identifies_the_deepest_service_with_all_evidence():
    h = _run(_context(FULL_CHAIN))
    assert (h.status, h.origin_service) == ("identified", "payment")
    assert _cited(h) == {
        ("anomaly", 0),
        ("anomaly", 1),
        ("anomaly", 2),
        ("relationship", 0),
        ("relationship", 1),
    }
    assert h.confidence == CONFIDENCE
    assert RULESET_VERSION in h.root_cause


def test_unknown_callee_blocks_a_caller_even_when_the_caller_is_the_only_anomaly():
    h = _run(_context({"gateway": 1500.0}, coverage={**ALL_OBSERVED, "order": "unobserved"}))
    assert h.status == "undetermined" and h.origin_service is None


# 3. multiple plausible candidates
def test_two_unrelated_candidates_abstain():
    h = _run(_context({"gateway": 1500.0, "payment": 1500.0}))
    assert h.status == "undetermined"
    assert "2 candidate origins" in h.root_cause
    assert _cited(h) >= {("anomaly", 0), ("anomaly", 1)}


# 4. missing relationships
def test_missing_relationships_abstain_with_the_anomalies_cited():
    h = _run(_context(FULL_CHAIN, edges=[]))
    assert h.status == "undetermined"
    assert "relationship gateway -> order is missing" in h.root_cause
    assert _cited(h) == {("anomaly", 0), ("anomaly", 1), ("anomaly", 2)}


def test_partial_relationships_abstain():
    h = _run(_context(FULL_CHAIN, edges=[("gateway", "order")]))
    assert h.status == "undetermined"
    assert "order -> payment is missing" in h.root_cause


# 5. missing or incomplete coverage
@pytest.mark.parametrize("coverage", [{}, {"gateway": "observed"}, {"order": "undefined"}])
def test_non_leaf_candidate_needs_declared_observed_callee(coverage):
    h = _run(_context({"gateway": 1500.0}, coverage=coverage))
    assert h.status == "undetermined"


def test_leaf_candidate_does_not_need_coverage():
    h = _run(_context({"payment": 1500.0}, coverage={}))
    assert (h.status, h.origin_service) == ("identified", "payment")


# 6. an unobserved dependency prevents attribution
def test_unobserved_leaf_callee_blocks_attribution_and_is_cited():
    context = _context(
        {"gateway": 1516.0, "order": 1510.0}, coverage={**ALL_OBSERVED, "payment": "unobserved"}
    )
    assert len(context.unobserved_dependencies) == 1
    h = _run(context)
    assert h.status == "undetermined" and h.origin_service is None
    assert "unobserved dependency" in h.root_cause
    assert ("unobserved_dependency", 0) in _cited(h)


def test_undefined_callee_is_treated_like_unobserved_not_healthy():
    h = _run(_context({"order": 1500.0}, coverage={**ALL_OBSERVED, "payment": "undefined"}))
    assert h.status == "undetermined"


# 7. no anomalies / no usable evidence
def test_no_evidence_at_all_raises_instead_of_fabricating_evidence():
    empty = IncidentContext(
        affected_services=[], relationships=[], window_start=START, window_end=END, anomalies=[]
    )
    with pytest.raises(ValueError, match="no evidence"):
        DeterministicInvestigator()(InvestigatorInput(incident=empty))


def test_no_anomalies_but_other_evidence_abstains_citing_it():
    ctx = _with(_context({"gateway": 1500.0}), anomalies=[], affected_services=[])
    h = _run(ctx)
    assert h.status == "undetermined"
    assert _cited(h) == {("relationship", 0)}


# 8. malformed or contradictory context
def test_anomaly_not_above_its_threshold_is_contradictory():
    ctx = _context({"payment": 1500.0})
    bad = ctx.anomalies[0].model_copy(update={"value": 100.0})
    h = _run(_with(ctx, anomalies=[bad]))
    assert h.status == "undetermined" and "threshold" in h.root_cause


def test_affected_services_mismatch_is_contradictory():
    h = _run(_with(_context({"payment": 1500.0}), affected_services=["order"]))
    assert h.status == "undetermined" and "affected_services" in h.root_cause


def test_anomalous_service_with_unobserved_coverage_is_contradictory():
    h = _run(_context({"payment": 1500.0}, coverage={"payment": "unobserved"}))
    assert h.status == "undetermined" and "not observed" in h.root_cause


def test_reversed_or_unknown_edges_abstain():
    h = _run(_context({"payment": 1500.0}, edges=[("payment", "order")]))
    assert h.status == "undetermined" and "outside the known chain" in h.root_cause
    h = _run(_context({"payment": 1500.0}, edges=[("billing", "payment")]))
    assert h.status == "undetermined"


def test_unknown_service_and_multiple_metrics_abstain():
    h = _run(_context({"billing": 1500.0}, edges=[]))
    assert h.status == "undetermined" and "outside the gateway" in h.root_cause
    ctx = _context({"payment": 1500.0, "order": 1500.0})
    other = ctx.anomalies[1].model_copy(update={"metric_name": "other"})
    h = _run(_with(ctx, anomalies=[ctx.anomalies[0], other]))
    assert h.status == "undetermined" and "several metrics" in h.root_cause


def test_conflicting_coverage_and_inconsistent_unobserved_dependency_abstain():
    ctx = _context({"order": 1500.0})
    dup = [
        *ctx.metric_coverage,
        MetricCoverage(metric_name=METRIC, service="payment", status="unobserved"),
    ]
    assert _run(_with(ctx, metric_coverage=dup)).status == "undetermined"

    ctx = _context({"order": 1500.0}, coverage={**ALL_OBSERVED, "payment": "unobserved"})
    dep = ctx.unobserved_dependencies[0].model_copy(update={"callee_status": "undefined"})
    h = _run(_with(ctx, unobserved_dependencies=[dep]))
    assert h.status == "undetermined" and "disagrees" in h.root_cause


# general properties
def test_output_is_deterministic_and_evidence_is_unique():
    ctx = _context(FULL_CHAIN)
    first, second = _run(ctx), _run(ctx)
    assert first == second
    keys = [(r.kind, r.index) for r in first.supporting_evidence]
    assert len(keys) == len(set(keys))


def test_abstention_cites_every_available_evidence_item_once():
    ctx = _context(
        {"gateway": 1516.0, "order": 1510.0}, coverage={**ALL_OBSERVED, "payment": "unobserved"}
    )
    h = _run(ctx)
    assert _cited(h) == {
        ("anomaly", 0),
        ("anomaly", 1),
        ("relationship", 0),
        ("relationship", 1),
        ("unobserved_dependency", 0),
    }
