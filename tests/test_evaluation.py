import json
from datetime import datetime, timezone

import httpx
import pytest

from shared.correlation import Anomaly, IncidentContext, ServiceRelationship
from shared.evaluation import (
    OUTCOMES,
    ScenarioExpectation,
    ScenarioResult,
    run_scenario,
    score_hypothesis,
    summarize,
)
from shared.investigator import ContractViolation, EvidenceRef, Hypothesis, InvestigatorInput

END = datetime(2023, 11, 14, 22, 30, 0, tzinfo=timezone.utc)


def _input() -> InvestigatorInput:
    return InvestigatorInput(
        incident=IncidentContext(
            affected_services=["order", "payment"],
            relationships=[ServiceRelationship(caller="order", callee="payment")],
            window_start=END,
            window_end=END,
            anomalies=[
                Anomaly(service=s, metric_name="m", value=9.0, threshold=1.0, timestamp=END)
                for s in ("order", "payment")
            ],
        )
    )


def _expect(**kw) -> ScenarioExpectation:
    fields = {
        "case_id": "c1",
        "injected_cause": "payment",
        "expected_status": "identified",
        "expected_origin": "payment",
    }
    fields.update(kw)
    return ScenarioExpectation(**fields)


def _hyp(status="identified", origin="payment", refs=(("anomaly", 1),)) -> Hypothesis:
    return Hypothesis(
        status=status,
        origin_service=origin if status == "identified" else None,
        root_cause="text",
        confidence=0.9,
        supporting_evidence=[EvidenceRef(kind=k, index=i) for k, i in refs],
    )


def _score(expectation, hypothesis) -> ScenarioResult:
    return score_hypothesis(expectation, _input(), hypothesis)


def test_correct_identification():
    r = _score(_expect(), _hyp())
    assert r.outcome == "correct_identification" and r.matches_injected_cause is True


def test_false_attribution_to_the_wrong_service():
    r = _score(_expect(), _hyp(origin="order"))
    assert r.outcome == "false_attribution" and r.matches_injected_cause is False


def test_unsupported_attribution_even_when_it_matches_the_injected_cause():
    undetermined = _expect(expected_status="undetermined", expected_origin=None)
    r = _score(undetermined, _hyp(origin="payment"))
    assert r.outcome == "unsupported_attribution"
    assert r.matches_injected_cause is True  # kept visible: right by luck, still unsupported


def test_appropriate_abstention():
    r = _score(_expect(expected_status="undetermined", expected_origin=None), _hyp("undetermined"))
    assert r.outcome == "appropriate_abstention" and r.matches_injected_cause is None


def test_over_abstention_when_identification_was_justified():
    r = _score(_expect(), _hyp("undetermined"))
    assert r.outcome == "over_abstention"


def test_dangling_evidence_is_a_contract_failure_with_validity_reported():
    r = _score(_expect(), _hyp(refs=(("anomaly", 1), ("anomaly", 7))))
    assert r.outcome == "contract_failure"
    assert r.evidence_validity == 0.5 and "anomaly[7]" in r.error


def test_origin_outside_the_context_is_a_contract_failure():
    r = _score(_expect(), _hyp(origin="billing"))
    assert r.outcome == "contract_failure" and "billing" in r.error


def test_evidence_relevance_against_a_gold_set():
    gold = [EvidenceRef(kind="anomaly", index=1), EvidenceRef(kind="relationship", index=0)]
    r = _score(_expect(gold_evidence=gold), _hyp(refs=(("anomaly", 1), ("anomaly", 0))))
    assert r.evidence_validity == 1.0
    assert r.evidence_precision == 0.5 and r.evidence_recall == 0.5


def test_relevance_is_undefined_without_a_gold_set():
    r = _score(_expect(), _hyp())
    assert (r.evidence_validity, r.evidence_precision, r.evidence_recall) == (1.0, None, None)


def test_expectation_requires_origin_exactly_when_identified():
    with pytest.raises(ValueError, match="expected_origin"):
        ScenarioExpectation(case_id="x", expected_status="identified")
    with pytest.raises(ValueError, match="expected_origin"):
        ScenarioExpectation(case_id="x", expected_status="undetermined", expected_origin="a")


def test_run_scenario_gives_the_investigator_only_the_input():
    seen = []

    def investigator(investigator_input):
        seen.append(investigator_input)
        return _hyp()

    expectation = _expect()
    r = run_scenario(expectation, _input(), investigator)
    assert r.outcome == "correct_identification"
    assert len(seen) == 1 and type(seen[0]) is InvestigatorInput
    dumped = str(seen[0].model_dump(mode="json"))
    assert (
        "expected" not in dumped and "injected" not in dumped and expectation.case_id not in dumped
    )


def test_run_scenario_counts_rejected_output_as_contract_failure():
    def bad(_):
        raise ContractViolation("model output omitted the required 'status' field")

    r = run_scenario(_expect(), _input(), bad)
    assert r.outcome == "contract_failure" and "status" in r.error

    def invalid(_):
        return Hypothesis(
            status="identified", root_cause="x", confidence=0.5, supporting_evidence=[]
        )

    assert run_scenario(_expect(), _input(), invalid).outcome == "contract_failure"


def test_provider_errors_are_not_scored_as_outcomes():
    def down(_):
        raise ConnectionError("provider down")

    with pytest.raises(ConnectionError):
        run_scenario(_expect(), _input(), down)


def test_summary_counts_outcomes_and_reports_no_rates():
    results = [
        _score(_expect(), _hyp()),
        _score(_expect(), _hyp(origin="order")),
        _score(_expect(), _hyp("undetermined")),
    ]
    summary = summarize(results)
    assert set(summary) == set(OUTCOMES)
    assert summary["correct_identification"] == 1
    assert summary["false_attribution"] == 1 and summary["over_abstention"] == 1
    assert summary["contract_failure"] == 0
    assert all(isinstance(v, int) for v in summary.values())


# ------------------------------------------------------- revalidation and error handling


def _unvalidated(**update) -> Hypothesis:
    """A hypothesis that skipped every validator, the way `model_construct`/`model_copy` can."""
    return _hyp().model_copy(update=update)


@pytest.mark.parametrize(
    "bad",
    [
        _unvalidated(origin_service=None),  # identified without an origin
        _unvalidated(status="undetermined"),  # undetermined with an origin
        _unvalidated(confidence=7.0),
        _unvalidated(supporting_evidence=[]),
        Hypothesis.model_construct(status="identified", root_cause="x", confidence=0.5),
    ],
)
def test_unvalidated_instances_are_contract_failures_not_scored_outcomes(bad):
    assert score_hypothesis(_expect(), _input(), bad).outcome == "contract_failure"
    assert run_scenario(_expect(), _input(), lambda _: bad).outcome == "contract_failure"


def test_plain_value_errors_and_wrong_types_are_not_scored():
    def bug(_):
        raise ValueError("a programming error, not a contract outcome")

    with pytest.raises(ValueError, match="programming error"):
        run_scenario(_expect(), _input(), bug)
    with pytest.raises(TypeError):
        run_scenario(_expect(), _input(), lambda _: "not a hypothesis")


def test_dict_results_are_validated_and_scored():
    ok = _hyp().model_dump()
    assert run_scenario(_expect(), _input(), lambda _: ok).outcome == "correct_identification"
    broken = {**ok, "origin_service": None}
    assert run_scenario(_expect(), _input(), lambda _: broken).outcome == "contract_failure"


# ----------------------------------------------------------------------- unscored


def _unscored(**kw) -> ScenarioExpectation:
    return _expect(expected_status="unscored", expected_origin=None, **kw)


@pytest.mark.parametrize(
    "hypothesis",
    [_hyp(), _hyp(origin="order"), _hyp("undetermined")],
    ids=["right", "wrong", "abstain"],
)
def test_unscored_is_never_correct_incorrect_or_abstention(hypothesis):
    r = _score(_unscored(), hypothesis)
    assert r.outcome == "unscored"
    assert r.evidence_validity == 1.0  # descriptive evidence metrics are still reported


def test_unscored_still_reports_descriptive_origin_match_and_contract_failures():
    assert _score(_unscored(), _hyp()).matches_injected_cause is True
    assert _score(_unscored(), _hyp(origin="order")).matches_injected_cause is False
    assert _score(_unscored(), _hyp(origin="billing")).outcome == "contract_failure"


def test_unscored_expectation_rejects_an_origin_and_is_counted_separately():
    with pytest.raises(ValueError, match="expected_origin"):
        ScenarioExpectation(case_id="x", expected_status="unscored", expected_origin="payment")
    summary = summarize([_score(_unscored(), _hyp()), _score(_expect(), _hyp())])
    assert summary["unscored"] == 1 and summary["correct_identification"] == 1


# ------------------------------------------------ malformed field types do not crash scoring


@pytest.mark.parametrize(
    "update",
    [
        {"origin_service": 5},
        {"origin_service": ["payment"]},
        {"status": 5},
        {"status": None},
        {"status": 5, "origin_service": 7},
    ],
)
def test_non_string_status_or_origin_is_a_contract_failure_without_crashing(update):
    bad = _hyp().model_copy(update=update)  # skips every validator
    for result in (
        score_hypothesis(_expect(), _input(), bad),
        run_scenario(_expect(), _input(), lambda _: bad),
    ):
        assert result.outcome == "contract_failure"
        # Invalid values are dropped, not converted into plausible-looking strings.
        assert result.origin_service is None or isinstance(result.origin_service, str)
        assert result.status is None or isinstance(result.status, str)
        assert result.origin_service != "5" and result.origin_service != "['payment']"
        assert result.status != "5"
        assert result.error


def test_model_construct_with_wrong_types_is_also_a_contract_failure():
    bad = Hypothesis.model_construct(
        status=3, origin_service=4, root_cause="x", confidence="high", supporting_evidence=[]
    )
    result = run_scenario(_expect(), _input(), lambda _: bad)
    assert result.outcome == "contract_failure"
    assert (result.status, result.origin_service) == (None, None)


def test_unrelated_exceptions_still_propagate_from_the_scoring_path():
    def broken(_):
        raise KeyError("a bug in the investigator")

    with pytest.raises(KeyError):
        run_scenario(_expect(), _input(), broken)


# ------------------------------------------------------- provider failures are recorded events

API_KEY = "sk-ant-api03-SUPERSECRETKEY1234567890"


def _llm(handler):
    """The shipped Anthropic adapter and LLMInvestigator over a mocked transport (no network)."""
    from shared.investigator.anthropic import anthropic_complete
    from shared.investigator.llm import LLMInvestigator

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return LLMInvestigator(anthropic_complete(api_key=API_KEY, model="m", client=client))


def _reply(payload: dict):
    return lambda request: httpx.Response(200, json=payload)


def _good_reply() -> dict:
    answer = {
        "status": "identified",
        "origin_service": "payment",
        "root_cause": "payment is slow",
        "confidence": 0.7,
        "supporting_evidence": [{"kind": "anomaly", "index": 1}],
    }
    return {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(answer)}]}


def _raises(exc: Exception):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


@pytest.mark.parametrize(
    ("handler", "category", "detail"),
    [
        (_reply({"stop_reason": "refusal"}), "refusal", "refusal"),
        (_reply({"stop_reason": "max_tokens", "content": []}), "token_limit", "max_tokens"),
        (_reply({"stop_reason": "end_turn", "content": []}), "empty_response", "no text"),
        (
            lambda r: httpx.Response(401, json={"error": f"bad key {API_KEY}"}),
            "http_status",
            "HTTP 401",
        ),
        (lambda r: httpx.Response(429, json={}), "http_status", "HTTP 429"),
        (lambda r: httpx.Response(500, text="boom"), "http_status", "HTTP 500"),
        (_raises(httpx.ReadTimeout("slow")), "timeout", "ReadTimeout"),
        (_raises(httpx.ConnectTimeout("slow")), "timeout", "ConnectTimeout"),
        (_raises(httpx.ConnectError("refused")), "network", "ConnectError"),
        (_raises(httpx.RemoteProtocolError("dropped")), "network", "RemoteProtocolError"),
    ],
    ids=[
        "refusal",
        "max-tokens",
        "no-text",
        "http-401",
        "http-429",
        "http-500",
        "read-timeout",
        "connect-timeout",
        "connect-error",
        "protocol-error",
    ],
)
def test_each_provider_failure_category_is_a_non_scored_event(handler, category, detail):
    result = run_scenario(_expect(), _input(), _llm(handler))

    assert result.outcome == "provider_failure"
    assert result.failure_category == category and detail in result.failure_detail
    # Never a verdict on the answer: no status, origin, evidence or correctness fields.
    assert (result.status, result.origin_service, result.evidence_validity) == (None, None, None)
    assert result.matches_injected_cause is None and result.error is None
    assert API_KEY not in result.model_dump_json()


def test_a_provider_failure_is_never_an_abstention_and_is_counted_apart():
    failed = run_scenario(_expect(), _input(), _llm(_reply({"stop_reason": "refusal"})))
    abstain_expectation = _expect(expected_status="undetermined", expected_origin=None)
    failed_again = run_scenario(
        abstain_expectation, _input(), _llm(_raises(httpx.ConnectError("x")))
    )

    summary = summarize([failed, failed_again])
    assert summary["provider_failure"] == 2
    for outcome, count in summary.items():
        if outcome != "provider_failure":
            assert count == 0, outcome


def test_scenarios_after_a_provider_failure_are_still_recorded_and_nothing_is_retried():
    calls = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={"stop_reason": "max_tokens", "content": []})
        return httpx.Response(200, json=_good_reply())

    investigator = _llm(flaky)
    results = [run_scenario(_expect(case_id=f"c{i}"), _input(), investigator) for i in range(3)]

    assert [r.outcome for r in results] == [
        "provider_failure",
        "correct_identification",
        "correct_identification",
    ]
    assert len(calls) == 3  # exactly one request per scenario: the failure was not retried
    assert summarize(results)["provider_failure"] == 1
    assert summarize(results)["correct_identification"] == 2


def test_provider_error_categories_are_validated_and_details_are_redacted():
    from shared.investigator.llm import ProviderError

    assert ProviderError("x").category == "other"
    with pytest.raises(ValueError, match="unknown provider failure category"):
        ProviderError("x", category="whatever")

    def leaky(_):
        raise ProviderError(
            f"adapter said Bearer abc123 {API_KEY} x-api-key: zzz", category="other"
        )

    result = run_scenario(_expect(), _input(), leaky)
    assert result.outcome == "provider_failure" and result.failure_category == "other"
    for secret in (API_KEY, "abc123", "zzz"):
        assert secret not in result.failure_detail
    assert "[redacted]" in result.failure_detail

    long_detail = run_scenario(
        _expect(), _input(), lambda _: (_ for _ in ()).throw(ProviderError("a" * 999))
    )
    assert len(long_detail.failure_detail) <= 200


def test_http_status_detail_carries_no_body_header_or_url():
    handler = lambda r: httpx.Response(403, json={"echo": API_KEY, "url": "https://x.example"})  # noqa: E731
    result = run_scenario(_expect(), _input(), _llm(handler))
    assert result.failure_detail == "HTTP 403"
    assert "anthropic.com" not in result.model_dump_json()


@pytest.mark.parametrize(
    "exc",
    [
        KeyError("a bug"),
        RuntimeError("a plain runtime error is not a provider failure"),
        ValueError("a plain value error"),
        AttributeError("typo"),
        httpx.InvalidURL("bad url"),  # a configuration bug, not a provider event
    ],
)
def test_unrelated_exceptions_are_not_provider_failures_and_still_propagate(exc):
    def broken(_):
        raise exc

    with pytest.raises(type(exc)):
        run_scenario(_expect(), _input(), broken)


def test_contract_failures_are_still_contract_failures_not_provider_failures():
    bad_json = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "not json"}]}
    assert run_scenario(_expect(), _input(), _llm(_reply(bad_json))).outcome == "contract_failure"
    dangling = _good_reply()
    dangling["content"][0]["text"] = json.dumps(
        {
            "status": "identified",
            "origin_service": "payment",
            "root_cause": "x",
            "confidence": 0.5,
            "supporting_evidence": [{"kind": "anomaly", "index": 9}],
        }
    )
    assert run_scenario(_expect(), _input(), _llm(_reply(dangling))).outcome == "contract_failure"


def test_classify_provider_failure_only_recognises_provider_side_exceptions():
    from shared.evaluation import classify_provider_failure

    assert classify_provider_failure(KeyError("x")) is None
    assert classify_provider_failure(ValueError("x")) is None
    assert classify_provider_failure(httpx.ReadTimeout("t"))[0] == "timeout"
    assert classify_provider_failure(httpx.ConnectError("c"))[0] == "network"
