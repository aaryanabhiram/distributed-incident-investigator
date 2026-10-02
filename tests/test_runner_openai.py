"""The experiment runner with the OpenAI adapter registered, offline.

Same caveat as `test_runner.py`: a frozen capture is built from the committed smoke fixtures only
to exercise the code paths, and every provider reply is a mocked transport. Nothing here observes
a real OpenAI request, schema acceptance, latency or token count.
"""

import json

import httpx
import pytest

from shared.evaluation import runner
from shared.evaluation import scenarios as sc
from tests.test_runner import (
    CLEAN,
    UNCHANGED,
    _by,
    _check,
    _frozen_capture,
    _load_cli,
    _queue,
    repo,  # noqa: F401  (fixture used by name)
)
from tests.test_runner import _answer as answer

KEY = "sk-proj-NEVERLEAKTHISKEY123456"
MODEL = "gpt-5.6-luna"
ENV = {"OPENAI_API_KEY": KEY}


def _ok(status="undetermined", origin=None, usage=None, **extra) -> dict:
    body = {
        "status": "completed",
        "model": "gpt-5.6-luna-2026-snapshot",
        "output": [
            {"type": "reasoning", "summary": []},
            {
                "type": "message",
                "content": [{"type": "output_text", "text": answer(status, origin)}],
            },
        ],
    }
    if usage is not False:
        body["usage"] = usage or {
            "input_tokens": 11,
            "output_tokens": 7,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 3},
        }
    return {**body, **extra}


def _capture(tmp_path, **kw):
    return _frozen_capture(tmp_path, provider="openai", model=MODEL, **kw)


def _registration(tmp_path, env=None):
    return runner.check_registration(
        _capture(tmp_path), env=env or {}, git_state=CLEAN, code_unchanged_since=UNCHANGED
    )


def _run(registration, handler, *, repeats=2, **kw):
    probe = runner.LLMProbe()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    llm = runner.build_llm_investigator(registration, api_key=KEY, probe=probe, client=client)
    return runner.run_experiment(
        registration, llm_investigator=llm, probe=probe, llm_repeats=repeats, **kw
    )


def test_an_openai_registration_is_accepted_and_names_the_openai_variables(tmp_path):
    root = _capture(tmp_path)
    registration = _check(root)
    assert registration.manifest["registration"]["transport"]["provider"] == "openai"
    assert registration.manifest["registration"]["transport"]["verified_by_repo"] is True

    with pytest.raises(runner.RunnerRefused, match="OPENAI_MODEL"):
        _check(root, env={"OPENAI_MODEL": "some-other-model"})
    with pytest.raises(runner.RunnerRefused, match="OPENAI_BASE_URL"):
        _check(root, env={"OPENAI_BASE_URL": "https://proxy.example"})
    assert _check(root, env={"OPENAI_MODEL": MODEL, "OPENAI_BASE_URL": "https://api.openai.com/"})
    # Anthropic variables are not this registration's business.
    assert _check(root, env={"ANTHROPIC_MODEL": "other"})


def test_the_registered_model_and_endpoint_are_what_is_sent(tmp_path):
    handler = _queue(*[_ok()] * 6)
    _run(_registration(tmp_path, env={"OPENAI_MODEL": MODEL}), handler)

    assert len(handler.seen) == 6
    assert {str(r.url) for r in handler.seen} == {"https://api.openai.com/v1/responses"}
    assert {r.headers["authorization"] for r in handler.seen} == {f"Bearer {KEY}"}
    assert {json.loads(r.content)["model"] for r in handler.seen} == {MODEL}


def test_usage_response_model_and_cost_are_recorded_per_openai_run(tmp_path):
    pricing = {"input_per_mtok_usd": 2.0, "output_per_mtok_usd": 10.0}
    cached = {
        "input_tokens": 100,
        "output_tokens": 50,
        "input_tokens_details": {"cached_tokens": 64},
        "output_tokens_details": {"reasoning_tokens": 20},
    }
    handler = _queue(_ok(), _ok(usage=False), _ok(usage=cached), *[_ok()] * 3)
    runs = _run(_registration(tmp_path), handler, repeats=2, pricing=pricing)
    s1 = _by(runs, "S1", "llm")

    assert s1[0]["usage"] == {
        "input_tokens": 11,
        "output_tokens": 7,
        "cached_input_tokens": 0,
        "reasoning_tokens": 3,
        "available": True,
    }
    assert s1[0]["cost_usd"] == pytest.approx((11 * 2.0 + 7 * 10.0) / 1_000_000)
    assert s1[0]["response_model"] == "gpt-5.6-luna-2026-snapshot"
    assert s1[1]["usage"]["available"] is False and s1[1]["cost_usd"] is None
    s2 = _by(runs, "S2", "llm")
    assert s2[0]["usage"]["cached_input_tokens"] == 64
    assert s2[0]["cost_usd"] is None  # cached input is billed at another rate: not guessed
    assert all(r["response_model"] is None for r in runs if r["investigator"] == "deterministic")


def test_openai_failures_are_recorded_as_provider_events_and_the_run_continues(tmp_path):
    handler = _queue(
        {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
        httpx.Response(429, json={"error": {"message": f"slow down {KEY}"}}),
        {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]},
        *[_ok()] * 3,
    )
    runs = _run(_registration(tmp_path), handler)
    failures = [r["provider_failure"] for r in runs if r["provider_failure"]]

    assert [f["category"] for f in failures] == ["token_limit", "http_status", "refusal"]
    assert failures[1]["detail"] == "HTTP 429"
    assert len(runs) == 9 and sum(1 for r in runs if r["outcome"] == "provider_failure") == 3
    assert KEY not in json.dumps(runs)


def test_request_parameters_are_observed_from_the_openai_adapter():
    params = runner.observed_request_parameters(MODEL, "openai")

    assert params["explicitly_sent"] == {
        "max_output_tokens": 4000,
        "model": MODEL,
        "store": False,
        "text": {"format": "json_schema"},
    }
    assert params["content_fields"] == ["input", "instructions"]
    assert params["sampling_parameters_explicitly_sent"] == []
    assert params["sampling_parameters_provider_default"] == ["temperature", "top_p", "reasoning"]
    assert "placeholder-not-a-credential" not in json.dumps(params)
    assert params != runner.observed_request_parameters(MODEL, "anthropic")


def test_the_recorded_parameters_match_what_a_run_really_sends(tmp_path):
    handler = _queue(*[_ok()] * 6)
    _run(_registration(tmp_path), handler)
    recorded = runner.observed_request_parameters(MODEL, "openai")
    content = set(recorded["content_fields"])

    for request in handler.seen:
        body = json.loads(request.content)
        assert set(body) - content == set(recorded["explicitly_sent"])
        for name in ("temperature", "top_p", "reasoning"):
            assert name not in body


def test_a_stale_openai_transport_registration_is_refused(tmp_path, monkeypatch):
    from shared.investigator import openai as transport

    root = _capture(tmp_path)
    monkeypatch.setattr(transport, "MAX_OUTPUT_TOKENS", 123)
    with pytest.raises(runner.RunnerRefused, match="OpenAI transport configuration differs"):
        _check(root)


def test_cli_runs_the_openai_registration_and_asks_for_the_openai_key(tmp_path, capsys):
    _capture(tmp_path)
    cli = _load_cli()
    base = ["--capture", str(tmp_path / "cap"), "--out", str(tmp_path / "r0")]
    assert cli.main(base, env={"ANTHROPIC_API_KEY": "x"}) == 2
    assert "OPENAI_API_KEY is not set" in capsys.readouterr().out

    handler = _queue(*[_ok()] * 6)
    out = tmp_path / "results"
    code = cli.main(
        ["--capture", str(tmp_path / "cap"), "--out", str(out), "--llm-repeats", "2"],
        env=ENV,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        git_state=CLEAN,
        code_unchanged_since=UNCHANGED,
    )
    assert code == 0
    results = json.loads((out / "results.json").read_text(encoding="utf-8"))
    config = results["run_config"]
    assert (config["provider"], config["model"], config["llm_repeats"]) == ("openai", MODEL, 2)
    assert config["request_parameters"] == runner.observed_request_parameters(MODEL, "openai")
    assert "no reasoning setting" in config["request_semantics"]
    assert results["registration"]["registered"]["transport"]["verified_by_repo"] is True
    everything = "".join(p.read_text(encoding="utf-8") for p in out.iterdir())
    assert KEY not in everything and "authorization" not in everything.lower()
    assert len(results["runs"]) == 9


def test_the_end_to_end_flow_scores_openai_answers_like_any_other_provider(tmp_path):
    # S1 unscored; S2 expects undetermined; S3 expects undetermined.
    handler = _queue(
        _ok("identified", "payment"), _ok(),  # S1: descriptive only
        _ok(), _ok("identified", "payment"),  # S2: abstain, unsupported attribution
        _ok(), _ok(),  # S3: abstain, abstain
    )  # fmt: skip
    runs = _run(_registration(tmp_path), handler)

    outcomes = {(r["scenario_id"], r["investigator"], r["repetition"]): r["outcome"] for r in runs}
    assert outcomes[("S1", "llm", 1)] == "unscored"
    assert outcomes[("S2", "llm", 1)] == "appropriate_abstention"
    assert outcomes[("S2", "llm", 2)] == "unsupported_attribution"
    assert outcomes[("S3", "llm", 1)] == "appropriate_abstention"
    assert sc.sha256_text("x")  # the hashes the probe enforced came from the frozen manifest


def test_dry_run_capture_freeze_commit_and_run_with_a_real_git_repository(
    repo,  # noqa: F811  (the fixture imported from test_runner)
    monkeypatch,
    capsys,
):
    """The whole offline path for the OpenAI registration: capture (mocked live reads) -> verify
    -> freeze -> commit the capture folder -> run through the CLI with a mocked transport."""
    from datetime import datetime, timedelta, timezone

    from tests.test_runner import _git, _raw
    from tests.test_scenarios import _evidence, _script

    script = _script()
    evidence = repo / "captures" / "evidence-1"
    evidence.mkdir(parents=True)
    for file, text in _evidence(
        armed_at=datetime.now(timezone.utc) - timedelta(seconds=30)
    ).items():
        (evidence / file).write_text(text)
    monkeypatch.setattr(script, "_record_live", lambda args, start, end: _raw())
    monkeypatch.setattr(script, "_fault_status", lambda args: {"active": False})
    out = repo / "captures" / "payment-latency-1"
    assert script.main(
        ["capture", "--out", str(out), "--injection-evidence", str(evidence),
         "--model", MODEL, "--provider", "openai"]
    ) == 0  # fmt: skip
    assert script.main(["verify", str(out), "--freeze"]) == 0
    _git(repo, "add", "captures")
    _git(repo, "commit", "-q", "-m", "add frozen capture")
    capsys.readouterr()

    handler = _queue(*[_ok()] * 15)
    results_dir = repo / "captures" / "results" / "run-1"
    code = _load_cli().main(
        ["--capture", str(out), "--out", str(results_dir)],
        env={**ENV, "OPENAI_MODEL": MODEL},
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )  # real Git state functions
    assert code == 0
    assert len(handler.seen) == 15  # 3 scenarios x 5 LLM repeats; the deterministic runs send none
    results = json.loads((results_dir / "results.json").read_text(encoding="utf-8"))
    assert len(results["runs"]) == 18
    assert results["summary"]["per_investigator"]["deterministic"]["runs"] == 3
    assert results["run_config"]["llm_repeats"] == 5
