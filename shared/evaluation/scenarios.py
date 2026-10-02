"""Experiment scenarios for the payment-latency comparison: build, verify, register.

Pure and offline: no network, no clock. `scripts/capture_payment_latency.py` does the live I/O and
calls this. Everything here works from *raw backend response text*, so a capture can be rebuilt
and re-verified later without the stack.

Three scenarios come from one capture (one fault, one window, one Jaeger response):

- S1 full telemetry (live capture). Correctness is `unscored`: the context has no span durations
  or self-time, so it cannot say whether the anomalous leaf owns the fault.
- S2 restricted telemetry (live capture with the PromQL restricted to gateway and order, so payment
  is unobserved). Expected `undetermined`, registered only if the captured context shows the
  unobserved dependency.
- S3 relationship ablation (S1 with `relationships` deleted; a context the pipeline did not
  produce). Expected `undetermined`, registered only if three anomalous services remain with no
  edge between them.

A second family (`family="order"`, Evaluation 9) comes from one latency fault injected into the
ORDER service, where payment stays observed and healthy:

- S4 full telemetry. Gateway and order are slow, payment is observed and under the threshold, so
  the evidence supports one origin: order (its own callee is measured healthy). Expected
  `identified`, origin `order`. This is the only scenario that expects an identification.
- S5 the same window with the PromQL restricted to gateway and order (payment unobserved). Order's
  callee is now unknown, so the evidence no longer supports an origin. Expected `undetermined`.

S4 and S5 are one incident seen with and without payment's telemetry; the label difference comes
only from what the evidence contains. The `chain-v1` rules and the LLM prompt are unchanged.

Labels (injected cause, expected status/origin, gold evidence) live only in the manifest entry,
never in a payload. Gold indices are looked up in the serialized payload, never assumed.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from shared.correlation import AnomalyRule
from shared.correlation.handoff import incident_context_to_payload
from shared.correlation.queries import (
    LATENCY_METRIC,
    mean_latency_query,
    restricted_mean_latency_query,
)
from shared.correlation.runner import run_correlation
from shared.evaluation import ScenarioExpectation
from shared.investigator import EvidenceRef, build_investigator_input
from shared.investigator import anthropic as anthropic_transport
from shared.investigator import deterministic as deterministic_rules
from shared.investigator import openai as openai_transport
from shared.investigator.llm import SYSTEM_PROMPT, build_prompt, response_schema

SERVICES = ["gateway", "order", "payment"]
EDGES = {("gateway", "order"), ("order", "payment")}
TRACE_SERVICE = "gateway"
DEMO_THRESHOLD_MS = 500.0  # demonstration value, not alerting policy
MANIFEST_VERSION = 2
QUERY_WINDOW = "5m"
FAMILIES = ("payment", "order")  # which service the live fault was injected into
RAW_FILES = ("prometheus_full.json", "prometheus_restricted.json", "jaeger.json")
INJECTION_FILES = ("fault-request.json", "fault-response.json", "fault-armed-at.txt")
INJECTION_OPTIONAL = ("fault-readback-before-traffic.json",)
# S1 and S2 aggregate identically per service and are evaluated at the same instant, so their
# shared anomaly values should be equal. A scrape landing between the two queries could move them
# slightly, so a relative difference up to this is accepted; anything larger means the two
# queries did not see the same data and S2 is not comparable to S1.
COMPARABLE_REL_TOL = 0.01


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(obj: Any) -> str:
    """Stable serialization used for every written payload, so file hashes are reproducible."""
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def normalized_source_sha256(module: Any) -> str:
    """Hash of a module's source with line endings normalized (CRLF checkouts hash the same)."""
    return sha256_text(inspect.getsource(module).replace("\r\n", "\n"))


def anthropic_transport_fingerprint() -> dict[str, Any]:
    """Everything that shapes a request sent by the shipped Anthropic adapter.

    Read at call time, so changing a constant, the schema adaptation or the adapter's source
    changes the fingerprint. It describes ONLY `shared/investigator/anthropic.py`; another
    provider adapter (for example a one-off script) has its own, which this cannot hash.
    """
    return {
        "provider": "anthropic",
        "base_url": anthropic_transport.DEFAULT_BASE_URL,
        "api_version": anthropic_transport.API_VERSION,
        "max_tokens": anthropic_transport.MAX_TOKENS,
        "timeout_seconds": anthropic_transport.TIMEOUT_SECONDS,
        "wire_schema": anthropic_transport.wire_schema(response_schema()),
        "adapter_source_sha256": normalized_source_sha256(anthropic_transport),
    }


def openai_transport_fingerprint() -> dict[str, Any]:
    """Everything that shapes a request sent by the shipped OpenAI adapter.

    Read at call time, like the Anthropic one: changing a constant, the schema adaptation or the
    adapter's source changes the fingerprint. The sampling and reasoning parameters are not sent,
    so they are not part of it (the runner records that they were left at provider defaults).
    """
    return {
        "provider": "openai",
        "base_url": openai_transport.DEFAULT_BASE_URL,
        "endpoint": openai_transport.ENDPOINT,
        "max_output_tokens": openai_transport.MAX_OUTPUT_TOKENS,
        "store": openai_transport.STORE,
        "schema_name": openai_transport.SCHEMA_NAME,
        "timeout_seconds": openai_transport.TIMEOUT_SECONDS,
        "wire_schema": openai_transport.wire_schema(response_schema()),
        "adapter_source_sha256": normalized_source_sha256(openai_transport),
    }


# Providers whose request configuration this repository can fingerprint itself.
VERIFIED_PROVIDERS = {
    "anthropic": anthropic_transport_fingerprint,
    "openai": openai_transport_fingerprint,
}


def transport_config_sha256(fingerprint: dict[str, Any]) -> str:
    return sha256_text(canonical_json(fingerprint))


def transport_block(provider: str | None, supplied_sha256: str | None) -> dict[str, Any]:
    """Registration of the provider path. Only the shipped adapters (Anthropic, OpenAI) are
    verifiable here; for any other provider the hash is operator-supplied and `verified_by_repo`
    is False."""
    if provider in VERIFIED_PROVIDERS:
        digest = transport_config_sha256(VERIFIED_PROVIDERS[provider]())
        return {"provider": provider, "config_sha256": digest, "verified_by_repo": True}
    return {"provider": provider, "config_sha256": supplied_sha256, "verified_by_repo": False}


def registration_hashes() -> dict[str, str]:
    """Hashes of what the experiment fixes, independent of provider.

    - `system_prompt_sha256`: the exact `SYSTEM_PROMPT` text.
    - `response_schema_sha256`: the provider-independent JSON schema of `Hypothesis` (what
      `response_schema()` returns), before any provider adaptation.
    - `deterministic_ruleset`: the rule-set version string.
    - `deterministic_source_sha256`: the source of `deterministic.py` itself, so a rule edit that
      forgets to bump the version still changes the registration.
    Provider/transport configuration is registered separately (`transport_block`), and the
    rendered user prompt per payload sits in each scenario entry.
    """
    return {
        "system_prompt_sha256": sha256_text(SYSTEM_PROMPT),
        "response_schema_sha256": sha256_text(json.dumps(response_schema(), sort_keys=True)),
        "deterministic_ruleset": deterministic_rules.RULESET_VERSION,
        "deterministic_source_sha256": normalized_source_sha256(deterministic_rules),
    }


def rendered_prompt_sha256(payload: dict[str, Any]) -> str:
    """Hash of the exact system and user prompt the LLM investigator renders for `payload`."""
    prompt = build_prompt(build_investigator_input(payload))
    return sha256_text(prompt.system + "\x00" + prompt.user)


def replay_context(
    *,
    prometheus_body: str,
    jaeger_body: str,
    query: str,
    window_start: datetime,
    window_end: datetime,
    threshold: float = DEMO_THRESHOLD_MS,
) -> dict[str, Any]:
    """Run the real `run_correlation` against recorded raw responses; return the handoff payload.

    The same code path serves a live capture (replaying what was just recorded) and an offline
    rebuild, so the payload never depends on which one produced the raw text.
    """

    def respond(body: str):
        return lambda request: httpx.Response(
            200, content=body.encode("utf-8"), headers={"content-type": "application/json"}
        )

    prometheus = httpx.Client(
        base_url="http://recorded", transport=httpx.MockTransport(respond(prometheus_body))
    )
    jaeger = httpx.Client(
        base_url="http://recorded", transport=httpx.MockTransport(respond(jaeger_body))
    )
    context = run_correlation(
        prometheus,
        jaeger,
        window_start,
        window_end,
        query,
        LATENCY_METRIC,
        [AnomalyRule(metric_name=LATENCY_METRIC, threshold=threshold)],
        TRACE_SERVICE,
        SERVICES,
    )
    return incident_context_to_payload(context)


def ablate_relationships(payload: dict[str, Any]) -> dict[str, Any]:
    """S3: a copy of `payload` with every relationship removed. Nothing else changes."""
    return {**json.loads(json.dumps(payload)), "relationships": []}


# --------------------------------------------------------------------------- verification


def _coverage(payload: dict[str, Any]) -> dict[str, str]:
    return {c["service"]: c["status"] for c in payload["metric_coverage"]}


def _edges(payload: dict[str, Any]) -> set[tuple[str, str]]:
    return {(r["caller"], r["callee"]) for r in payload["relationships"]}


def _anomalous(payload: dict[str, Any]) -> set[str]:
    return {a["service"] for a in payload["anomalies"]}


def verify_s1(payload: dict[str, Any]) -> list[str]:
    """Problems that make `payload` unusable as the full-telemetry scenario (empty = usable)."""
    problems = []
    if _anomalous(payload) != set(SERVICES):
        problems.append(f"anomalous services are {sorted(_anomalous(payload))}, not all three")
    if _edges(payload) != EDGES:
        problems.append(f"relationships are {sorted(_edges(payload))}, not the full chain")
    if _coverage(payload) != dict.fromkeys(SERVICES, "observed"):
        problems.append("coverage is not observed for every service")
    if payload["unobserved_dependencies"]:
        problems.append("unobserved dependencies are present")
    return problems


def verify_s2(payload: dict[str, Any]) -> list[str]:
    """Problems that make `payload` unusable as the restricted-telemetry scenario."""
    problems = []
    if _anomalous(payload) != {"gateway", "order"}:
        problems.append(f"anomalous services are {sorted(_anomalous(payload))}, not gateway+order")
    if _edges(payload) != EDGES:
        problems.append(f"relationships are {sorted(_edges(payload))}, not the full chain")
    if _coverage(payload) != {"gateway": "observed", "order": "observed", "payment": "unobserved"}:
        problems.append("coverage is not gateway/order observed and payment unobserved")
    expected_dep = [
        {
            "caller": "order",
            "callee": "payment",
            "metric_name": LATENCY_METRIC,
            "callee_status": "unobserved",
        }
    ]
    if payload["unobserved_dependencies"] != expected_dep:
        problems.append("the order -> payment unobserved dependency is not present")
    return problems


def verify_s3(payload: dict[str, Any]) -> list[str]:
    """Problems that make `payload` unusable as the ablation scenario.

    Abstention is justified only if several services are anomalous with no known edge between
    them: nothing then separates a chain from a shared factor. One anomalous service with no
    edges would not be ambiguous, so it is refused.
    """
    problems = []
    if len(_anomalous(payload)) < 2:
        problems.append("fewer than two anomalous services; the ablated context is not ambiguous")
    if payload["relationships"]:
        problems.append("relationships remain")
    if payload["unobserved_dependencies"]:
        problems.append("unobserved dependencies remain; they would be extra evidence")
    if _coverage(payload) != dict.fromkeys(SERVICES, "observed"):
        problems.append("coverage is not observed for every service")
    return problems


def verify_s4(payload: dict[str, Any]) -> list[str]:
    """Problems that make `payload` unusable as the order-fault, full-telemetry scenario.

    The identification is justified only if order is anomalous, its callee payment is measured
    (observed) and not anomalous, and the chain is intact.
    """
    problems = []
    if _anomalous(payload) != {"gateway", "order"}:
        problems.append(f"anomalous services are {sorted(_anomalous(payload))}, not gateway+order")
    if _edges(payload) != EDGES:
        problems.append(f"relationships are {sorted(_edges(payload))}, not the full chain")
    if _coverage(payload) != dict.fromkeys(SERVICES, "observed"):
        problems.append("coverage is not observed for every service")
    if payload["unobserved_dependencies"]:
        problems.append("unobserved dependencies are present")
    return problems


# ------------------------------------------------------------------------------ gold evidence


def _index(items: list[dict[str, Any]], match: dict[str, Any], what: str) -> int:
    for i, item in enumerate(items):
        if all(item.get(k) == v for k, v in match.items()):
            return i
    raise ValueError(f"no {what} matching {match} in the payload")


def gold_s2(payload: dict[str, Any]) -> list[EvidenceRef]:
    """Minimal justification for abstaining: order is slow and its callee payment is unobserved."""
    return [
        EvidenceRef(
            kind="anomaly", index=_index(payload["anomalies"], {"service": "order"}, "anomaly")
        ),
        EvidenceRef(
            kind="relationship",
            index=_index(
                payload["relationships"], {"caller": "order", "callee": "payment"}, "relationship"
            ),
        ),
        EvidenceRef(
            kind="unobserved_dependency",
            index=_index(
                payload["unobserved_dependencies"],
                {"caller": "order", "callee": "payment"},
                "unobserved dependency",
            ),
        ),
    ]


def gold_s4(payload: dict[str, Any]) -> list[EvidenceRef]:
    """Minimal justification for naming order: it is slow and its callee payment is measured."""
    return [
        EvidenceRef(
            kind="anomaly", index=_index(payload["anomalies"], {"service": "order"}, "anomaly")
        ),
        EvidenceRef(
            kind="relationship",
            index=_index(
                payload["relationships"], {"caller": "order", "callee": "payment"}, "relationship"
            ),
        ),
    ]


def gold_s3(payload: dict[str, Any]) -> list[EvidenceRef]:
    """Minimal justification for abstaining: the anomalies are all there is."""
    return [
        EvidenceRef(kind="anomaly", index=_index(payload["anomalies"], {"service": s}, "anomaly"))
        for s in SERVICES
    ]


# -------------------------------------------------------------------------- registration


def opaque_id(payload: dict[str, Any]) -> str:
    """Neutral case id derived from the payload bytes; says nothing about the scenario."""
    return "c-" + sha256_text(canonical_json(payload))[:10]


def verify_comparable(s1: dict[str, Any], s2: dict[str, Any]) -> list[str]:
    """Problems that make S2 not comparable to S1 (empty = comparable).

    Same window, metric, relationships, and thresholds exactly; each anomaly value S2 shares with
    S1 within `COMPARABLE_REL_TOL` (see that constant for why not exact).
    """
    problems = []
    for key in ("window_start", "window_end"):
        if s1[key] != s2[key]:
            problems.append(f"{key} differs")
    if s1["relationships"] != s2["relationships"]:
        problems.append("relationships differ")
    first = {(a["service"], a["metric_name"]): a for a in s1["anomalies"]}
    for anomaly in s2["anomalies"]:
        other = first.get((anomaly["service"], anomaly["metric_name"]))
        if other is None:
            problems.append(f"{anomaly['service']} is anomalous in S2 only")
            continue
        if anomaly["threshold"] != other["threshold"]:
            problems.append(f"{anomaly['service']} thresholds differ")
        a, b = anomaly["value"], other["value"]
        if abs(a - b) > COMPARABLE_REL_TOL * max(abs(a), abs(b)):
            problems.append(f"{anomaly['service']} values differ ({a} vs {b}) beyond tolerance")
    return problems


def register(
    s1: dict[str, Any],
    s2: dict[str, Any],
    *,
    smoke: bool,
    injected_cause: str | None = None,
) -> list[dict[str, Any]]:
    """Verify the three payloads and build one manifest entry each; raises on any problem.

    `smoke` marks entries from offline smoke data: they can never join the primary comparison.
    `injected_cause` is the label for the injection and must come from validated injection
    evidence (`validate_injection`); smoke data has none, so it is `None` there.
    """
    s3 = ablate_relationships(s1)
    problems = (
        [f"S1: {p}" for p in verify_s1(s1)]
        + [f"S2: {p}" for p in verify_s2(s2)]
        + [f"S3: {p}" for p in verify_s3(s3)]
        + [f"S1/S2: {p}" for p in verify_comparable(s1, s2)]
    )
    if problems:
        raise ValueError("; ".join(problems))
    specs = [
        (
            "S1",
            s1,
            "live_capture",
            ScenarioExpectation(
                case_id=opaque_id(s1),
                injected_cause=injected_cause,
                expected_status="unscored",
                gold_evidence=None,
            ),
            "Full telemetry. Correctness unscored: no span durations or self-time, and a leaf in "
            "the dependency graph does not establish causal origin. Descriptive output, evidence "
            "validity, runtime and cost only.",
        ),
        (
            "S2",
            s2,
            "live_capture_restricted_query",
            ScenarioExpectation(
                case_id=opaque_id(s2),
                injected_cause=injected_cause,
                expected_status="undetermined",
                gold_evidence=gold_s2(s2),
            ),
            "Same window and Jaeger response as S1; PromQL restricted to gateway and order so "
            "payment is unobserved. Abstention is justified by the unobserved dependency, not by "
            "the injection.",
        ),
        (
            "S3",
            s3,
            "controlled_ablation",
            ScenarioExpectation(
                case_id=opaque_id(s3),
                injected_cause=injected_cause,
                expected_status="undetermined",
                gold_evidence=gold_s3(s3),
            ),
            "S1 with relationships deleted (not produced by the pipeline; real Jaeger truncation "
            "can look similar). Three anomalous services with no known edge: a chain and a shared "
            "factor are indistinguishable.",
        ),
    ]
    return [
        {
            "scenario_id": sid,
            "kind": f"offline_smoke_{kind}" if smoke else kind,
            "primary_comparison": not smoke,
            "smoke_test_only": smoke,
            "payload_ref": f"payloads/{exp.case_id}.json",
            "payload_sha256": sha256_text(canonical_json(payload)),
            "rendered_prompt_sha256": rendered_prompt_sha256(payload),
            "expectation": exp.model_dump(mode="json"),
            "notes": notes,
        }
        for sid, payload, kind, exp, notes in specs
    ]


def register_order(
    s4: dict[str, Any],
    s5: dict[str, Any],
    *,
    smoke: bool,
    injected_cause: str | None = None,
) -> list[dict[str, Any]]:
    """Verify the two order-fault payloads and build one manifest entry each; raises on problems."""
    problems = (
        [f"S4: {p}" for p in verify_s4(s4)]
        + [f"S5: {p}" for p in verify_s2(s5)]
        + [f"S4/S5: {p}" for p in verify_comparable(s4, s5)]
    )
    if problems:
        raise ValueError("; ".join(problems))
    specs = [
        (
            "S4",
            s4,
            "live_capture",
            ScenarioExpectation(
                case_id=opaque_id(s4),
                injected_cause=injected_cause,
                expected_status="identified",
                expected_origin="order",
                gold_evidence=gold_s4(s4),
            ),
            "Full telemetry, latency fault in order. Gateway and order are slow and payment is "
            "observed and under the threshold, so order's own callee is measured healthy and the "
            "evidence supports order as the origin (a co-fault in gateway is not excluded).",
        ),
        (
            "S5",
            s5,
            "live_capture_restricted_query",
            ScenarioExpectation(
                case_id=opaque_id(s5),
                injected_cause=injected_cause,
                expected_status="undetermined",
                gold_evidence=gold_s2(s5),
            ),
            "Same window and Jaeger response as S4; PromQL restricted to gateway and order so "
            "payment is unobserved. Abstention is justified by the unobserved dependency, not by "
            "the injection.",
        ),
    ]
    return [
        {
            "scenario_id": sid,
            "kind": f"offline_smoke_{kind}" if smoke else kind,
            "primary_comparison": not smoke,
            "smoke_test_only": smoke,
            "payload_ref": f"payloads/{exp.case_id}.json",
            "payload_sha256": sha256_text(canonical_json(payload)),
            "rendered_prompt_sha256": rendered_prompt_sha256(payload),
            "expectation": exp.model_dump(mode="json"),
            "notes": notes,
        }
        for sid, payload, kind, exp, notes in specs
    ]


def verify_capture(entries: list[dict[str, Any]], payloads: dict[str, dict[str, Any]]) -> list[str]:
    """Re-check a written capture: hashes, case ids, scenario conditions and gold indices.

    `payloads` maps `payload_ref` to the parsed file. Returns problems (empty means verified).
    """
    problems = []
    by_id = {e["scenario_id"]: e for e in entries}
    for entry in entries:
        payload = payloads.get(entry["payload_ref"])
        if payload is None:
            problems.append(f"{entry['scenario_id']}: payload file missing")
            continue
        if sha256_text(canonical_json(payload)) != entry["payload_sha256"]:
            problems.append(f"{entry['scenario_id']}: payload hash mismatch")
        if opaque_id(payload) != entry["expectation"]["case_id"]:
            problems.append(f"{entry['scenario_id']}: case id does not match payload")
        for key in ("injected_cause", "expected_status", "expected_origin", "gold_evidence"):
            if key in payload:
                problems.append(f"{entry['scenario_id']}: label field {key!r} leaked into payload")
    checks = {
        "S1": verify_s1,
        "S2": verify_s2,
        "S3": verify_s3,
        "S4": verify_s4,
        "S5": verify_s2,
    }
    golds = {"S2": gold_s2, "S3": gold_s3, "S4": gold_s4, "S5": gold_s2}
    for sid, check in checks.items():
        entry = by_id.get(sid)
        payload = payloads.get(entry["payload_ref"]) if entry else None
        if payload is None:
            continue
        problems += [f"{sid}: {p}" for p in check(payload)]
        if sid in golds:
            try:
                registered = ScenarioExpectation.model_validate(entry["expectation"]).gold_evidence
            except ValueError as exc:
                problems.append(f"{sid}: malformed expectation ({str(exc).splitlines()[0]})")
                continue
            if registered != golds[sid](payload):
                problems.append(f"{sid}: registered gold evidence does not match the payload")
    return problems


# ------------------------------------------------------------------ timestamps and injection


def parse_iso_utc(text: str) -> datetime:
    """ISO-8601 text to an aware datetime; raises `ValueError` with an actionable message.

    Python 3.10's `fromisoformat` rejects a trailing `Z`, so it is mapped to `+00:00` first.
    """
    value = text.strip()
    if value[-1:] in ("Z", "z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"not an ISO-8601 timestamp: {text!r} (example: 2026-09-30T07:16:00+00:00)"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"timestamp {text!r} has no UTC offset; end it with Z or +00:00")
    return parsed


def _json(text: str, name: str, problems: list[str]) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except ValueError:
        problems.append(f"{name} is not valid JSON")
        return None
    if not isinstance(value, dict):
        problems.append(f"{name} is not a JSON object")
        return None
    return value


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def validate_injection(
    evidence: dict[str, str],
    *,
    window_start: datetime,
    window_end: datetime,
    threshold: float,
    service: str = "payment",
) -> tuple[dict[str, Any] | None, list[str]]:
    """Check the operator's saved fault-injection evidence against the capture window.

    `evidence` maps file name to text: `fault-request.json` (the body POSTed to the payment
    service's `/admin/fault`), `fault-response.json` (the successful response),
    `fault-armed-at.txt` (a UTC timestamp taken just before the POST) and optionally
    `fault-readback-before-traffic.json`. `service` is the service the operator POSTed to (`payment`
    or `order`). Returns the injection record to store in the manifest,
    or `None` plus problems.

    What this does and does not prove: it shows a latency fault above the anomaly threshold was
    accepted and armed in a time span that covers the window. It cannot show WHICH service
    accepted it (the response has no service name), so the service attribution is declared by
    the operator procedure (the POST goes to that service's admin port), not independently proven.
    """
    problems: list[str] = []
    missing = [n for n in INJECTION_FILES if n not in evidence]
    if missing:
        return None, [f"injection evidence missing: {', '.join(missing)}"]
    request = _json(evidence["fault-request.json"], "fault-request.json", problems)
    response = _json(evidence["fault-response.json"], "fault-response.json", problems)
    try:
        armed_at = parse_iso_utc(evidence["fault-armed-at.txt"])
    except ValueError as exc:
        armed_at = None
        problems.append(f"fault-armed-at.txt: {exc}")
    if request is None or response is None or armed_at is None:
        return None, problems

    latency, duration = request.get("latency_ms"), request.get("duration_seconds")
    if request.get("mode") != "latency":
        problems.append("fault request mode is not 'latency'")
    if not _number(latency) or latency <= threshold:
        problems.append(f"fault latency_ms must exceed the anomaly threshold ({threshold})")
    if not _number(duration) or duration <= 0:
        problems.append("fault duration_seconds must be positive")
    if response.get("active") is not True or response.get("mode") != "latency":
        problems.append("fault response does not show an active latency fault")
    remaining = response.get("seconds_remaining")
    if not _number(remaining) or remaining <= 0:
        problems.append("fault response has no positive seconds_remaining")
    if "fault-readback-before-traffic.json" in evidence:
        name = "fault-readback-before-traffic.json"
        readback = _json(evidence[name], name, problems)
        if readback is not None and (
            readback.get("active") is not True or readback.get("mode") != "latency"
        ):
            problems.append("fault readback before traffic does not show an active latency fault")
    if problems:
        return None, problems
    if armed_at > window_end or armed_at + timedelta(seconds=duration) < window_start:
        return None, ["the fault was not armed during the capture window (fault-armed-at.txt)"]
    return {
        "service": service,
        "mode": "latency",
        "latency_ms": latency,
        "duration_seconds": duration,
        "armed_at": armed_at.astimezone(timezone.utc).isoformat(),
        "evidence_sha256": {n: sha256_text(evidence[n]) for n in sorted(evidence)},
        "attribution": (
            f"operator-declared: the request was sent to {service}'s admin port; the response "
            "carries no service name, so this is not independent proof of which service"
        ),
    }, []


# ----------------------------------------------------------------------- build and verify


def queries() -> dict[str, str]:
    return {
        "full": mean_latency_query(QUERY_WINDOW),
        "restricted": restricted_mean_latency_query(QUERY_WINDOW, ["gateway", "order"]),
    }


def _rebuild(
    raw: dict[str, str], window_start: datetime, window_end: datetime, threshold: float
) -> tuple[dict[str, Any], dict[str, Any]]:
    q = queries()
    build = {"window_start": window_start, "window_end": window_end, "threshold": threshold}
    s1 = replay_context(
        prometheus_body=raw["prometheus_full.json"],
        jaeger_body=raw["jaeger.json"],
        query=q["full"],
        **build,
    )
    s2 = replay_context(
        prometheus_body=raw["prometheus_restricted.json"],
        jaeger_body=raw["jaeger.json"],
        query=q["restricted"],
        **build,
    )
    return s1, s2


def build_capture(
    *,
    raw: dict[str, str],
    window_start: datetime,
    window_end: datetime,
    threshold: float,
    mode: str,
    evidence: dict[str, str] | None,
    model: str | None,
    provider: str | None,
    transport_sha256: str | None,
    git_commit: str,
    git_dirty: bool,
    endpoints: dict[str, str] | None,
    fault_status_at_capture: Any,
    jaeger_traces: int | None,
    family: str = "payment",
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Build the manifest and payloads from raw responses; raises `ValueError` on any problem.

    `mode` is "live" or "offline_smoke". Live data needs valid injection evidence; smoke data has
    none and can never be primary or frozen.
    """
    if mode not in ("live", "offline_smoke"):
        raise ValueError(f"unknown capture mode {mode!r}")
    if family not in FAMILIES:
        raise ValueError(f"unknown scenario family {family!r}")
    smoke = mode == "offline_smoke"
    injection = None
    if not smoke:
        if evidence is None:
            raise ValueError("a live capture needs injection evidence")
        injection, problems = validate_injection(
            evidence,
            window_start=window_start,
            window_end=window_end,
            threshold=threshold,
            service=family,
        )
        if problems:
            raise ValueError("injection evidence: " + "; ".join(problems))
    s1, s2 = _rebuild(raw, window_start, window_end, threshold)
    cause = injection["service"] if injection else None
    if family == "order":
        entries = register_order(s1, s2, smoke=smoke, injected_cause=cause)
        payloads = {entries[0]["payload_ref"]: s1, entries[1]["payload_ref"]: s2}
    else:
        entries = register(s1, s2, smoke=smoke, injected_cause=cause)
        payloads = {
            entries[0]["payload_ref"]: s1,
            entries[1]["payload_ref"]: s2,
            entries[2]["payload_ref"]: ablate_relationships(s1),
        }
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "frozen": False,
        "smoke_test_only": smoke,
        "registration": {
            **registration_hashes(),
            "git_commit": git_commit,
            "git_tree_dirty": git_dirty,
            "llm_repeats_planned": 5,
            "deterministic_repeats": 1,
            "model": model,
            "transport": transport_block(provider, transport_sha256),
        },
        "capture": {
            "mode": mode,
            "family": family,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "threshold_ms": threshold,
            "metric": LATENCY_METRIC,
            "services": SERVICES,
            "trace_service": TRACE_SERVICE,
            "queries": queries(),
            "endpoints": endpoints,
            "jaeger_limit": 100,
            "jaeger_traces_returned": jaeger_traces,
            "fault_status_at_capture": fault_status_at_capture,
            "injection": injection,
            "raw_sha256": {name: sha256_text(text) for name, text in raw.items()},
        },
        "scenarios": entries,
    }
    return manifest, payloads


def manifest_digest(manifest: dict[str, Any]) -> str:
    """Deterministic hash of the manifest excluding the freeze metadata itself."""
    body = {
        k: v for k, v in manifest.items() if k not in ("frozen", "frozen_at", "manifest_sha256")
    }
    return sha256_text(canonical_json(body))


def _entry_differences(found: Any, derived: list[dict[str, Any]]) -> list[str]:
    if not isinstance(found, list) or len(found) != len(derived):
        return ["scenarios list does not match the scenarios derived from the capture"]
    problems = []
    for have, want in zip(found, derived, strict=True):
        sid = want["scenario_id"]
        if not isinstance(have, dict):
            problems.append(f"{sid}: entry is not an object")
            continue
        for key in sorted(set(have) | set(want)):
            if key == "expectation" and isinstance(have.get(key), dict):
                for sub in sorted(set(have[key]) | set(want[key])):
                    if have[key].get(sub) != want[key].get(sub):
                        problems.append(
                            f"{sid}: expectation.{sub} is {have[key].get(sub)!r} but the "
                            f"capture derives {want[key].get(sub)!r}"
                        )
            elif have.get(key) != want.get(key):
                problems.append(
                    f"{sid}: {key} is {have.get(key)!r} but the capture derives {want.get(key)!r}"
                )
    return problems


def verify_manifest(
    manifest: dict[str, Any],
    *,
    payloads: dict[str, dict[str, Any]],
    raw: dict[str, str],
    evidence: dict[str, str],
) -> list[str]:
    """Re-derive everything in `manifest` from the stored raw responses and injection evidence.

    Labels, kinds, flags, gold evidence, payload bindings, rendered-prompt hashes and registration
    hashes are recomputed and compared; any difference is a problem. Malformed data becomes a
    problem too, never a traceback. Returns problems (empty means verified).
    """
    try:
        return _verify_manifest(manifest, payloads, raw, evidence)
    except (KeyError, IndexError, TypeError, AttributeError, ValueError) as exc:
        detail = (str(exc).splitlines() or [""])[0]
        return [f"malformed manifest or capture ({type(exc).__name__}: {detail})"]


def _verify_manifest(
    manifest: dict[str, Any],
    payloads: dict[str, dict[str, Any]],
    raw: dict[str, str],
    evidence: dict[str, str],
) -> list[str]:
    problems: list[str] = []
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        problems.append(f"manifest_version is not {MANIFEST_VERSION}")
    cap = manifest["capture"]
    mode = cap["mode"]
    if mode not in ("live", "offline_smoke"):
        return [*problems, f"capture.mode {mode!r} is not 'live' or 'offline_smoke'"]
    smoke = mode == "offline_smoke"
    family = cap.get("family", "payment")  # captures made before the field existed are payment
    if family not in FAMILIES:
        return [*problems, f"capture.family {family!r} is not one of {list(FAMILIES)}"]
    if manifest["smoke_test_only"] is not smoke:
        problems.append("smoke_test_only disagrees with capture.mode (it is derived from the mode)")

    frozen = manifest.get("frozen")
    if frozen is True:
        if manifest.get("manifest_sha256") != manifest_digest(manifest):
            problems.append("the manifest changed after it was frozen (digest mismatch)")
        if smoke:
            problems.append("a smoke-test manifest is marked frozen")
    elif frozen is False:
        if "manifest_sha256" in manifest or "frozen_at" in manifest:
            problems.append("an unfrozen manifest carries freeze metadata")
    else:
        problems.append("frozen must be true or false")

    missing_raw = [name for name in RAW_FILES if name not in raw]
    problems += [f"raw/{name} is missing" for name in missing_raw]
    for name in RAW_FILES:
        if name in raw and sha256_text(raw[name]) != cap["raw_sha256"].get(name):
            problems.append(f"raw/{name}: hash mismatch")
    if missing_raw:
        return problems

    if cap["queries"] != queries():
        problems.append("capture.queries are not the registered queries")
    window_start, window_end = parse_iso_utc(cap["window_start"]), parse_iso_utc(cap["window_end"])
    s1, s2 = _rebuild(raw, window_start, window_end, cap["threshold_ms"])
    rebuilt = (
        {"S4": s1, "S5": s2}
        if family == "order"
        else {"S1": s1, "S2": s2, "S3": ablate_relationships(s1)}
    )

    injection = cap["injection"]
    if smoke:
        if injection is not None:
            problems.append("a smoke-test capture must not carry injection evidence")
    elif injection is None:
        problems.append("a live capture has no injection record")
    else:
        derived, injection_problems = validate_injection(
            evidence,
            window_start=window_start,
            window_end=window_end,
            threshold=cap["threshold_ms"],
            service=family,
        )
        problems += [f"injection: {p}" for p in injection_problems]
        if derived is not None and derived != injection:
            problems.append("the injection record does not match the evidence files")

    try:
        cause = (injection or {}).get("service")
        expected = (
            register_order(s1, s2, smoke=smoke, injected_cause=cause)
            if family == "order"
            else register(s1, s2, smoke=smoke, injected_cause=cause)
        )
    except ValueError as exc:
        problems.append(f"scenarios cannot be registered from the raw capture: {exc}")
    else:
        problems += _entry_differences(manifest["scenarios"], expected)
    for entry in manifest["scenarios"]:
        if payloads.get(entry["payload_ref"]) != rebuilt.get(entry["scenario_id"]):
            problems.append(
                f"{entry['scenario_id']}: payload file is missing or not reproducible from raw"
            )
    problems += verify_capture(manifest["scenarios"], payloads)

    registration = manifest["registration"]
    for key, value in registration_hashes().items():
        if registration.get(key) != value:
            problems.append(f"registration {key} differs from the current code")
    transport = registration["transport"]
    if transport["provider"] in VERIFIED_PROVIDERS:
        if transport != transport_block(transport["provider"], None):
            name = {"anthropic": "Anthropic", "openai": "OpenAI"}[transport["provider"]]
            problems.append(f"the {name} transport configuration differs from the current code")
    elif transport.get("verified_by_repo") is not False:
        problems.append(
            "only the shipped Anthropic adapter and the shipped OpenAI adapter can be marked "
            "verified_by_repo"
        )
    return problems


def freeze_blockers(
    manifest: dict[str, Any],
    *,
    git_commit: str,
    git_dirty: bool,
    code_unchanged_since_capture: bool | None = None,
) -> list[str]:
    """Conditions that forbid freezing (empty means freezable). Call after `verify_manifest`.

    HEAD may differ from the captured commit only if `code_unchanged_since_capture` is true: HEAD
    descends from it and nothing outside `captures/` changed (so committing the capture folder
    before freezing is fine, a source change is not).
    """
    blockers = []
    if manifest["capture"]["mode"] != "live" or manifest["smoke_test_only"] is not False:
        blockers.append("smoke-test data can never be frozen")
    registration = manifest["registration"]
    if registration.get("git_tree_dirty") is not False:
        blockers.append(
            "the capture was made with uncommitted changes (or Git state was unknown), so the "
            "registered code is not a commit; commit, then capture again"
        )
    if git_dirty:
        blockers.append("the working tree has uncommitted changes outside the capture directory")
    if git_commit == "unavailable" or (
        git_commit != registration.get("git_commit") and not code_unchanged_since_capture
    ):
        blockers.append("HEAD is not the commit recorded at capture (or code changed since)")
    if not registration.get("model"):
        blockers.append("registration.model is not set")
    transport = registration.get("transport") or {}
    if not transport.get("provider"):
        blockers.append("registration.transport.provider is not set")
    if not transport.get("config_sha256"):
        blockers.append("registration.transport.config_sha256 is not set")
    return blockers


# ------------------------------------------------------------------------------- loading


def read_if_exists(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.is_file() else None


def load_evidence(directory: Path) -> dict[str, str]:
    """The injection evidence files present in `directory` (name -> text)."""
    found = {n: read_if_exists(directory / n) for n in (*INJECTION_FILES, *INJECTION_OPTIONAL)}
    return {n: text for n, text in found.items() if text is not None}


def load_capture(
    root: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, str], dict[str, str], list[str]]:
    """Read a capture folder: (manifest, payloads, raw, evidence, problems).

    Unreadable or malformed files become problems, never tracebacks; the data is then checked by
    `verify_manifest`.
    """
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as exc:
        return None, {}, {}, {}, [f"manifest.json is missing or malformed ({exc})"]
    problems: list[str] = []
    payloads: dict[str, Any] = {}
    scenarios = manifest.get("scenarios")
    for entry in scenarios if isinstance(scenarios, list) else []:
        ref = entry.get("payload_ref") if isinstance(entry, dict) else None
        if not isinstance(ref, str):
            continue
        try:
            payloads[ref] = json.loads((root / ref).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"{ref}: unreadable ({exc})")
    raw = {n: t for n in RAW_FILES if (t := read_if_exists(root / "raw" / n)) is not None}
    return manifest, payloads, raw, load_evidence(root / "evidence"), problems
