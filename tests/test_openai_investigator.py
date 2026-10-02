"""The OpenAI Responses API transport, offline: mocked `httpx` transports, no key, no network."""

import json

import httpx
import pytest

from shared.evaluation import classify_provider_failure
from shared.investigator import openai as oa
from shared.investigator.llm import LLMInvestigator, Prompt, ProviderError, response_schema

KEY = "sk-proj-NEVERLEAKTHISKEY123456"
PROMPT = Prompt(system="system text", user="user text")


def _message(text: str) -> dict:
    return {"type": "message", "content": [{"type": "output_text", "text": text}]}


def _completed(text: str = "{}", **extra) -> dict:
    return {
        "status": "completed",
        "model": "gpt-resolved-snapshot",
        "output": [{"type": "reasoning", "summary": []}, _message(text)],
        "usage": {
            "input_tokens": 11,
            "output_tokens": 7,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 4},
        },
        **extra,
    }


def _client(reply, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return reply if isinstance(reply, httpx.Response) else httpx.Response(200, json=reply)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _complete(reply, **kw):
    return oa.openai_complete(api_key=KEY, model="m", client=_client(reply), **kw)


def test_one_post_with_the_documented_request_body():
    seen = []
    complete = oa.openai_complete(api_key=KEY, model="gpt-x", client=_client(_completed(), seen))
    schema = response_schema()
    assert complete(PROMPT, schema) == "{}"

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST" and str(request.url) == "https://api.openai.com/v1/responses"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert set(body) == {"model", "instructions", "input", "max_output_tokens", "store", "text"}
    assert body["model"] == "gpt-x"
    assert body["instructions"] == "system text" and body["input"] == "user text"
    assert body["max_output_tokens"] == 4000 and body["store"] is False
    assert body["text"] == {
        "format": {
            "type": "json_schema",
            "name": "hypothesis",
            "strict": True,
            "schema": oa.wire_schema(schema),
        }
    }
    for name in ("temperature", "top_p", "reasoning"):
        assert name not in body  # provider defaults apply


def test_wire_schema_closes_every_object_and_requires_every_property():
    wired = oa.wire_schema(response_schema())

    def objects(node):
        if isinstance(node, dict):
            if "properties" in node:
                yield node
            for value in node.values():
                yield from objects(value)
        elif isinstance(node, list):
            for item in node:
                yield from objects(item)

    found = list(objects(wired))
    assert found
    for obj in found:
        assert obj["additionalProperties"] is False
        assert obj["required"] == list(obj["properties"])
    dumped = json.dumps(wired)
    for dropped in ("minimum", "maxItems", "minItems", "minLength", '"default"', '"title"'):
        assert dropped not in dumped
    assert "description" not in wired  # the root description is dropped
    assert "origin_service" in wired["required"]


def test_only_output_text_of_message_items_is_returned():
    reply = _completed("ignored")
    reply["output"] = [
        {"type": "reasoning", "summary": [{"type": "output_text", "text": "NO"}]},
        {"type": "message", "content": [{"type": "output_text", "text": '{"a":'}]},
        {"type": "message", "content": [{"type": "output_text", "text": "1}"}]},
    ]
    assert _complete(reply)(PROMPT, {}) == '{"a":1}'


@pytest.mark.parametrize(
    ("reply", "category"),
    [
        (
            {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
            "token_limit",
        ),
        ({"status": "incomplete", "incomplete_details": {"reason": "content_filter"}}, "refusal"),
        ({"status": "incomplete", "incomplete_details": None}, "other"),
        ({"status": "failed", "error": {"message": "boom"}}, "other"),
        (
            {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}],
            },
            "refusal",
        ),
        ({"status": "completed", "output": [{"type": "reasoning"}]}, "empty_response"),
    ],
)
def test_provider_side_failures_become_provider_errors(reply, category):
    with pytest.raises(ProviderError) as raised:
        _complete(reply)(PROMPT, {})
    assert raised.value.category == category
    assert classify_provider_failure(raised.value)[0] == category
    assert KEY not in str(raised.value)


def test_http_errors_propagate_with_the_status_only_and_never_the_key_or_body():
    reply = httpx.Response(400, json={"error": {"message": f"bad key {KEY}"}})
    with pytest.raises(httpx.HTTPStatusError) as raised:
        _complete(reply)(PROMPT, {})
    assert classify_provider_failure(raised.value) == ("http_status", "HTTP 400")
    assert KEY not in classify_provider_failure(raised.value)[1]


def test_no_retry_on_failure():
    seen = []
    client = _client(httpx.Response(500, json={}), seen)
    with pytest.raises(httpx.HTTPStatusError):
        oa.openai_complete(api_key=KEY, model="m", client=client)(PROMPT, {})
    assert len(seen) == 1


def test_usage_and_model_hooks_report_only_whitelisted_values_and_change_nothing():
    usage, models = [], []
    with_hooks = _complete(
        _completed(id="resp_LEAK"), on_usage=usage.append, on_model=models.append
    )
    without = _complete(_completed(id="resp_LEAK"))
    assert with_hooks(PROMPT, {}) == without(PROMPT, {})
    assert usage == [
        {"input_tokens": 11, "output_tokens": 7, "cached_input_tokens": 0, "reasoning_tokens": 4}
    ]
    assert models == ["gpt-resolved-snapshot"]
    assert "resp_LEAK" not in json.dumps(usage)


def test_usage_is_reported_even_for_an_incomplete_reply():
    usage = []
    reply = {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "usage": {"input_tokens": 5, "output_tokens": 4000},
    }
    with pytest.raises(ProviderError):
        _complete(reply, on_usage=usage.append)(PROMPT, {})
    assert usage[0]["input_tokens"] == 5 and usage[0]["output_tokens"] == 4000
    assert usage[0]["cached_input_tokens"] is None


@pytest.mark.parametrize("bad", [None, "x", {}, {"usage": 3}, {"usage": {"input_tokens": True}}])
def test_absent_or_malformed_usage_is_none_never_zero(bad):
    assert oa.usage_from_response(bad) == dict.fromkeys(oa.USAGE_FIELDS)
    assert oa.usage_from_response({"usage": {"input_tokens": -1}})["input_tokens"] is None


def test_the_response_model_is_none_when_missing_or_not_text():
    models = []
    _complete({**_completed(), "model": 5}, on_model=models.append)(PROMPT, {})
    assert models == [None]


def test_llm_investigator_runs_over_the_adapter():
    answer = json.dumps(
        {
            "status": "undetermined",
            "origin_service": None,
            "root_cause": "unknown",
            "confidence": 0.5,
            "supporting_evidence": [{"kind": "anomaly", "index": 0}],
        }
    )
    from shared.correlation.handoff import incident_context_to_payload
    from shared.investigator import build_investigator_input
    from tests.test_scenarios import FULL_Q, _payload, _text

    payload = _payload(_text("prometheus_mean_latency_vector.json"), FULL_Q)
    assert incident_context_to_payload  # payload is a handoff dict
    hypothesis = LLMInvestigator(_complete(_completed(answer)))(build_investigator_input(payload))
    assert hypothesis.status == "undetermined"


def test_from_env_requires_key_and_model(monkeypatch):
    for name in ("OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY, OPENAI_MODEL"):
        oa.openai_investigator_from_env()
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setenv("OPENAI_MODEL", "m")
    assert isinstance(oa.openai_investigator_from_env(), LLMInvestigator)
