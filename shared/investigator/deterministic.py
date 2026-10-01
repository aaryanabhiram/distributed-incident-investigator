"""Deterministic investigator: explicit, explainable rules behind the `Investigator` protocol.

FROZEN RULE SET `chain-v1`. Written for the gateway -> order -> payment chain only; it is not a
general root-cause algorithm. It is frozen before any comparison with the LLM investigator and
must not be tuned to observed model outputs; a changed rule set gets a new `RULESET_VERSION`.

Inputs are the same `InvestigatorInput` the LLM sees: anomalies, relationships, metric coverage,
unobserved dependencies. Nothing else (no span durations, no non-anomalous values, no labels).

The investigator may say `undetermined`. It names an origin only when every step below holds:

1. No evidence at all (no anomalies, relationships or unobserved dependencies): raise
   `ValueError`. `Hypothesis` needs at least one evidence reference and none exists, so no valid
   result can be built. "No incident" is a pipeline decision, not an investigator answer.
2. No anomalies but other evidence: `undetermined`.
3. Malformed/contradictory or out-of-scope context: `undetermined`. Checked: several metrics;
   an anomaly at or below its threshold; `affected_services` differing from the anomalous
   services; a service or relationship outside the known chain (this also rejects reversed edges
   and cycles); conflicting duplicate coverage; an anomalous service whose coverage is not
   "observed"; an `unobserved_dependency` with no matching relationship, caller anomaly or
   coverage status.
4. Every known callee of every anomalous service must appear as a relationship. A missing
   edge means propagation cannot be confirmed: `undetermined`.
5. Per anomalous service, look at its known callees. A callee that is itself anomalous means the
   service's latency may be inherited (propagating). A callee declared "observed" in
   `metric_coverage` and not anomalous is healthy. Any other callee (undefined, unobserved or
   not declared) is unknown: missing telemetry is never read as health, and a service with an
   unknown callee is blocked and cannot be attributed. Any blocked service: `undetermined`.
6. A candidate is an unblocked anomalous service with no anomalous callee (a leaf has none).
   Exactly one candidate is required, and every other anomalous service must be an ancestor of
   it in the known chain. Then `identified`, origin = the candidate. Otherwise `undetermined`.

Limitations (stated, not hidden): an anomalous leaf is taken to own its latency because the chain
says it has no callee, and this does not exclude an additional fault in an ancestor; a candidate
with healthy callees is credited because it exceeded a threshold its callees did not, which shows
its own contribution, not that it is the only one; the topology is a constant, so any other
service or edge abstains; thresholds are whatever the correlation layer used. `confidence` is a
fixed placeholder, not a probability.
"""

from __future__ import annotations

from shared.correlation import IncidentContext
from shared.investigator import EvidenceRef, Hypothesis, InvestigatorInput

RULESET_VERSION = "chain-v1"

# The only topology these rules know: caller -> its direct callees.
KNOWN_CALLEES: dict[str, tuple[str, ...]] = {
    "gateway": ("order",),
    "order": ("payment",),
    "payment": (),
}

# Fixed placeholder; not a calibrated probability and not scored in evaluations.
CONFIDENCE = 0.5


def _descendants(service: str) -> set[str]:
    seen: set[str] = set()
    stack = list(KNOWN_CALLEES[service])
    while stack:
        callee = stack.pop()
        if callee not in seen:
            seen.add(callee)
            stack.extend(KNOWN_CALLEES[callee])
    return seen


def _all_refs(incident: IncidentContext) -> list[EvidenceRef]:
    return (
        [EvidenceRef(kind="anomaly", index=i) for i in range(len(incident.anomalies))]
        + [EvidenceRef(kind="relationship", index=i) for i in range(len(incident.relationships))]
        + [
            EvidenceRef(kind="unobserved_dependency", index=i)
            for i in range(len(incident.unobserved_dependencies))
        ]
    )


def _context_problem(incident: IncidentContext) -> str | None:
    """Why the context is malformed, contradictory or out of scope, or `None` if usable."""
    anomalies = incident.anomalies
    metrics = {a.metric_name for a in anomalies}
    if len(metrics) > 1:
        return "anomalies span several metrics"
    metric = next(iter(metrics))
    anomalous = {a.service for a in anomalies}
    if not anomalous <= KNOWN_CALLEES.keys():
        return "an anomalous service is outside the gateway -> order -> payment chain"
    if any(a.value <= a.threshold for a in anomalies):
        return "an anomaly does not exceed its own threshold"
    if set(incident.affected_services) != anomalous:
        return "affected_services does not match the anomalous services"
    edges = {(r.caller, r.callee) for r in incident.relationships}
    for caller, callee in edges:
        if callee not in KNOWN_CALLEES.get(caller, ()):
            return f"relationship {caller} -> {callee} is outside the known chain"

    statuses: dict[str, set[str]] = {}
    for entry in incident.metric_coverage:
        if entry.metric_name == metric:
            statuses.setdefault(entry.service, set()).add(entry.status)
    if any(len(s) > 1 for s in statuses.values()):
        return "coverage gives conflicting statuses for one service"
    for service in anomalous:
        if statuses.get(service, {"observed"}) != {"observed"}:
            return f"{service} is anomalous but its coverage is not observed"
    for dependency in incident.unobserved_dependencies:
        if dependency.metric_name != metric or dependency.caller not in anomalous:
            return "an unobserved dependency does not match an anomalous caller and metric"
        if (dependency.caller, dependency.callee) not in edges:
            return "an unobserved dependency has no matching relationship"
        if statuses.get(dependency.callee) != {dependency.callee_status}:
            return "an unobserved dependency disagrees with metric coverage"
    return None


def _undetermined(incident: IncidentContext, reason: str) -> Hypothesis:
    return Hypothesis(
        status="undetermined",
        origin_service=None,
        root_cause=f"No origin can be established from this evidence: {reason}.",
        confidence=CONFIDENCE,
        supporting_evidence=_all_refs(incident),
    )


class DeterministicInvestigator:
    """`Investigator` applying the frozen `chain-v1` rules; see the module docstring."""

    def __call__(self, investigator_input: InvestigatorInput) -> Hypothesis:
        incident = investigator_input.incident
        if not _all_refs(incident):
            raise ValueError(
                "incident context holds no evidence; handle a no-incident window before the "
                "investigator"
            )
        if not incident.anomalies:
            return _undetermined(incident, "the context contains no anomalies")
        problem = _context_problem(incident)
        if problem is not None:
            return _undetermined(incident, problem)

        metric = incident.anomalies[0].metric_name
        anomalous = {a.service for a in incident.anomalies}
        edges = {(r.caller, r.callee) for r in incident.relationships}
        coverage = {
            c.service: c.status for c in incident.metric_coverage if c.metric_name == metric
        }

        for service in sorted(anomalous):
            for callee in KNOWN_CALLEES[service]:
                if (service, callee) not in edges:
                    return _undetermined(
                        incident, f"the relationship {service} -> {callee} is missing"
                    )

        candidates: list[str] = []
        blocked: list[str] = []
        for service in sorted(anomalous):
            callees = KNOWN_CALLEES[service]
            unknown = [c for c in callees if c not in anomalous and coverage.get(c) != "observed"]
            if unknown:
                blocked.append(f"{service} (health of {', '.join(unknown)} is unknown)")
            elif not any(c in anomalous for c in callees):
                candidates.append(service)
        if blocked:
            return _undetermined(incident, "unobserved dependency: " + "; ".join(blocked))
        if len(candidates) != 1:
            return _undetermined(
                incident, f"{len(candidates)} candidate origins ({', '.join(candidates)})"
            )
        origin = candidates[0]
        if not all(origin in _descendants(s) for s in anomalous - {origin}):
            return _undetermined(incident, "an anomalous service is not upstream of the candidate")

        evidence = [EvidenceRef(kind="anomaly", index=i) for i in range(len(incident.anomalies))]
        evidence += [
            EvidenceRef(kind="relationship", index=i)
            for i, r in enumerate(incident.relationships)
            if r.caller in anomalous
        ]
        return Hypothesis(
            status="identified",
            origin_service=origin,
            root_cause=(
                f"{origin} is the only anomalous service with no anomalous or unknown callee; "
                f"anomalies upstream of it are consistent with propagation. Rule set "
                f"{RULESET_VERSION}."
            ),
            confidence=CONFIDENCE,
            supporting_evidence=evidence,
        )
