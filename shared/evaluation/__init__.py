"""Offline scoring of investigator outputs against scenario expectations.

Pure Python: no model calls, no I/O, no statistics. The `ScenarioExpectation` manifest lives apart
from the frozen investigator payloads; `run_scenario` hands the investigator only the
`InvestigatorInput`, so labels cannot reach it (the `Investigator` protocol takes nothing else).

Two things are kept separate on purpose:
- `injected_cause`: the fault that was really injected (informational ground truth);
- `expected_status` / `expected_origin`: what the supplied evidence actually justifies. Naming the
  injected service from a context that cannot support it is an *unsupported attribution*, even
  when it happens to match the injection.

Some scenarios have no defensible correctness label (the evidence cannot say whether a leaf
service owns the fault). They register `expected_status="unscored"`: the result is still validated
and its evidence, runtime and cost are still reported, but it is never counted as correct,
incorrect or an abstention. A contract failure is still a contract failure.

A provider failure (refusal, token limit, empty reply, HTTP error, timeout, network error) is a
different thing again: an explicit, non-scored `provider_failure` event. It says nothing about the
investigator's reasoning, is never turned into an `undetermined` answer, and never counts as
correct, incorrect, abstention or contract failure. Recording it lets the run continue with the
next scenario instead of aborting; only the category and a short, redacted detail are kept.

Results are per scenario; `summarize` only counts outcomes. With a handful of scenarios no rate is
meaningful, so none is computed. Confidence is never scored.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from typing import Literal

import httpx
from pydantic import BaseModel, ValidationError

from shared.investigator import (
    ContractViolation,
    EvidenceRef,
    Hypothesis,
    Investigator,
    InvestigatorInput,
    revalidate,
    validate_evidence,
    validate_origin,
)
from shared.investigator.llm import ProviderError

Outcome = Literal[
    "correct_identification",
    "false_attribution",
    "unsupported_attribution",
    "appropriate_abstention",
    "over_abstention",
    "contract_failure",
    "unscored",
    "provider_failure",
]
OUTCOMES: tuple[str, ...] = Outcome.__args__  # type: ignore[attr-defined]


class ScenarioExpectation(BaseModel):
    """What one scenario's evidence justifies. Never given to an investigator."""

    case_id: str
    injected_cause: str | None = None  # the fault really injected; informational only
    # "unscored": no correctness label is registered (never correct, incorrect or abstention).
    expected_status: Literal["identified", "undetermined", "unscored"]
    expected_origin: str | None = None  # required iff expected_status == "identified"
    # Evidence an answer is expected to rely on; `None` means relevance is not defined.
    gold_evidence: list[EvidenceRef] | None = None

    def model_post_init(self, __context: object) -> None:
        if (self.expected_status == "identified") != (self.expected_origin is not None):
            raise ValueError("expected_origin is required exactly when expected_status=identified")


class ScenarioResult(BaseModel):
    case_id: str
    outcome: Outcome
    status: str | None = None
    origin_service: str | None = None
    # Identified origin equals the injected cause; `None` when not identified or cause unknown.
    matches_injected_cause: bool | None = None
    evidence_validity: float | None = None  # share of cited references that exist in the input
    evidence_precision: float | None = None  # share of valid citations inside the gold set
    evidence_recall: float | None = None  # share of the gold set that was cited
    error: str | None = None
    # Set only for outcome "provider_failure": a non-scored event, not a verdict on the answer.
    failure_category: str | None = None
    failure_detail: str | None = None  # short and redacted; never a body, header or credential


_SECRET = re.compile(
    r"(sk-[A-Za-z0-9_\-]{6,}|Bearer\s+\S+|(?i:api[_-]?key|x-api-key|authorization)\s*[:=]\s*\S+)"
)


def redact(text: str, limit: int = 200) -> str:
    """Strip anything shaped like an API key or auth header, then cut to `limit` characters."""
    return _SECRET.sub("[redacted]", text)[:limit]


def classify_provider_failure(exc: BaseException) -> tuple[str, str] | None:
    """(category, short detail) if `exc` is a provider-side failure, else `None`.

    Recognised: `ProviderError` (refusal, token_limit, empty_response, other); `httpx` HTTP status
    errors (`http_status`); timeouts (`timeout`); other transport errors (`network`). The detail
    uses only the category-defining fields (status code, exception class), never a response body,
    request header or URL. Everything else is not a provider failure and must propagate.
    """
    if isinstance(exc, ProviderError):
        return exc.category, redact(str(exc))
    if isinstance(exc, httpx.HTTPStatusError):
        return "http_status", f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", type(exc).__name__
    if isinstance(exc, httpx.TransportError):
        return "network", type(exc).__name__
    return None


def _text_or_none(value: object) -> str | None:
    """Keep a value only if it is a real string; a malformed result's other types are dropped,
    never converted into something that looks valid."""
    return value if isinstance(value, str) else None


def _classify(expectation: ScenarioExpectation, hypothesis: Hypothesis) -> Outcome:
    if expectation.expected_status == "unscored":
        return "unscored"
    if expectation.expected_status == "identified":
        if hypothesis.status == "undetermined":
            return "over_abstention"
        if hypothesis.origin_service == expectation.expected_origin:
            return "correct_identification"
        return "false_attribution"
    if hypothesis.status == "undetermined":
        return "appropriate_abstention"
    return "unsupported_attribution"


def _evidence_metrics(
    expectation: ScenarioExpectation, investigator_input: InvestigatorInput, hypothesis: Hypothesis
) -> tuple[float, float | None, float | None]:
    cited = hypothesis.supporting_evidence
    valid: list[EvidenceRef] = []
    for ref in cited:
        try:
            validate_evidence(
                investigator_input, hypothesis.model_copy(update={"supporting_evidence": [ref]})
            )
        except ValueError:
            continue
        valid.append(ref)
    validity = len(valid) / len(cited)
    gold = expectation.gold_evidence
    if not gold:
        return validity, None, None
    gold_keys = {(r.kind, r.index) for r in gold}
    valid_keys = {(r.kind, r.index) for r in valid}
    precision = len(valid_keys & gold_keys) / len(valid_keys) if valid_keys else 0.0
    return validity, precision, len(valid_keys & gold_keys) / len(gold_keys)


def score_hypothesis(
    expectation: ScenarioExpectation, investigator_input: InvestigatorInput, hypothesis: Hypothesis
) -> ScenarioResult:
    """Score a hypothesis. A malformed result, dangling evidence or an unknown origin is a
    contract failure (the pipeline would reject it).

    The hypothesis is revalidated from scratch first, so an instance built with `model_construct`
    or `model_copy` cannot skip the field validators. Evidence validity is still reported for a
    result that fails only on its citations or origin.
    """
    try:
        hypothesis = revalidate(hypothesis)
    except ValidationError as exc:
        return ScenarioResult(
            case_id=expectation.case_id,
            outcome="contract_failure",
            status=_text_or_none(getattr(hypothesis, "status", None)),
            origin_service=_text_or_none(getattr(hypothesis, "origin_service", None)),
            error=str(exc)[:300],
        )
    validity, precision, recall = _evidence_metrics(expectation, investigator_input, hypothesis)
    base = {
        "case_id": expectation.case_id,
        "status": hypothesis.status,
        "origin_service": hypothesis.origin_service,
        "evidence_validity": validity,
        "evidence_precision": precision,
        "evidence_recall": recall,
    }
    try:
        validate_evidence(investigator_input, hypothesis)
        validate_origin(investigator_input, hypothesis)
    except ContractViolation as exc:
        return ScenarioResult(outcome="contract_failure", error=str(exc), **base)
    matches = None
    if hypothesis.status == "identified" and expectation.injected_cause is not None:
        matches = hypothesis.origin_service == expectation.injected_cause
    return ScenarioResult(
        outcome=_classify(expectation, hypothesis), matches_injected_cause=matches, **base
    )


def run_scenario(
    expectation: ScenarioExpectation,
    investigator_input: InvestigatorInput,
    investigator: Investigator,
) -> ScenarioResult:
    """Run an investigator on the input alone and score the outcome.

    A malformed result or rejected reply (pydantic `ValidationError`, or `ContractViolation` such
    as a model reply that omits `status`) is a contract failure. A provider failure
    (`classify_provider_failure`) is recorded as a non-scored `provider_failure` event, so the
    caller can go on to the next scenario; it is never converted into a hypothesis and the call is
    not retried. Anything else propagates: a plain `ValueError`, `KeyError` or `TypeError` is a
    programming error that must not be scored as an ordinary outcome.
    """
    try:
        produced = investigator(investigator_input)
    except (ValidationError, ContractViolation) as exc:
        return ScenarioResult(
            case_id=expectation.case_id, outcome="contract_failure", error=str(exc)[:300]
        )
    except Exception as exc:
        failure = classify_provider_failure(exc)
        if failure is None:
            raise
        return ScenarioResult(
            case_id=expectation.case_id,
            outcome="provider_failure",
            failure_category=failure[0],
            failure_detail=failure[1],
        )
    if not isinstance(produced, Hypothesis | dict):
        raise TypeError(f"investigator returned {type(produced).__name__}, not a Hypothesis")
    if isinstance(produced, dict):
        try:
            produced = revalidate(produced)
        except ValidationError as exc:
            return ScenarioResult(
                case_id=expectation.case_id, outcome="contract_failure", error=str(exc)[:300]
            )
    return score_hypothesis(expectation, investigator_input, produced)


def summarize(results: Sequence[ScenarioResult]) -> dict[str, int]:
    """Counts per outcome (every outcome present, zeros included). Deliberately no rates."""
    counts = Counter(r.outcome for r in results)
    return {outcome: counts.get(outcome, 0) for outcome in OUTCOMES}
