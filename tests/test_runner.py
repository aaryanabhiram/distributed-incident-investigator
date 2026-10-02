"""The experiment runner, offline.

Everything here uses fixtures and mocked transports. A "frozen live capture" is built in a temp
directory from the committed 2026-09-30 smoke fixtures plus synthetic injection evidence, only to
exercise the code paths; it is not experiment data, and a passing test is not live verification:
no real provider, schema acceptance, latency or token count is observed.
"""

import copy
import json
import subprocess
from pathlib import Path

import httpx
import pytest

from shared.evaluation import runner
from shared.evaluation import scenarios as sc
from shared.investigator import InvestigatorInput, build_investigator_input
from shared.investigator.anthropic import anthropic_complete, usage_from_response
from tests.test_scenarios import _build, _raw, _script

KEY = "sk-ant-api03-NEVERLEAKTHISKEY123456"
MODEL = "registered-model"
CLEAN = lambda exclude: ("abc123", False)  # noqa: E731
UNCHANGED = lambda commit: True  # noqa: E731


# ---------------------------------------------------------------------------- fixtures


def _write_capture(root: Path, manifest: dict, payloads: dict, raw: dict, evidence: dict) -> None:
    for name, text in {
        "manifest.json": sc.canonical_json(manifest),
        **{ref: sc.canonical_json(p) for ref, p in payloads.items()},
        **{f"raw/{n}": t for n, t in raw.items()},
        **{f"evidence/{n}": t for n, t in evidence.items()},
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _frozen_capture(tmp_path: Path, *, freeze: bool = True, name: str = "cap", **build) -> Path:
    build.setdefault("model", MODEL)
    manifest, payloads = _build(**build)
    if freeze:
        manifest.update(frozen=True, frozen_at="t", manifest_sha256=sc.manifest_digest(manifest))
    root = tmp_path / name
    from tests.test_scenarios import _evidence

    _write_capture(
        root, manifest, payloads, _raw(), _evidence() if manifest["capture"]["injection"] else {}
    )
    return root


def _registration(tmp_path: Path, env=None, **build) -> runner.Registration:
    root = _frozen_capture(tmp_path, **build)
    return runner.check_registration(
        root, env=env or {}, git_state=CLEAN, code_unchanged_since=UNCHANGED
    )


def _answer(status="undetermined", origin=None, index=0) -> str:
    return json.dumps(
        {
            "status": status,
            "origin_service": origin,
            "root_cause": "text",
            "confidence": 0.5,
            "supporting_evidence": [{"kind": "anomaly", "index": index}],
        }
    )


def _ok(status="undetermined", origin=None, usage=None, **extra) -> dict:
    body = {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": _answer(status, origin)}],
    }
    if usage is not False:
        body["usage"] = usage if usage is not None else {"input_tokens": 11, "output_tokens": 7}
    return {**body, **extra}


def _queue(*items):
    """A mock handler answering each request from `items` in order; records the requests."""
    pending, seen = list(items), []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if not pending:
            pytest.fail("more provider requests than the test scripted")
        item = pending.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item)

    handler.seen = seen
    return handler


def _run(registration, handler, *, repeats=2, **kw):
    probe = runner.LLMProbe()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    llm = runner.build_llm_investigator(registration, api_key=KEY, probe=probe, client=client)
    return runner.run_experiment(
        registration, llm_investigator=llm, probe=probe, llm_repeats=repeats, **kw
    )


def _by(runs, scenario, investigator):
    return [r for r in runs if r["scenario_id"] == scenario and r["investigator"] == investigator]


# ----------------------------------------------------------------- successful execution


def test_both_investigators_run_on_every_scenario_in_a_fixed_order(tmp_path):
    handler = _queue(
        _ok("identified", "payment"), _ok(),  # S1 (unscored)
        _ok(), _ok(),  # S2: abstain, abstain
        _ok("identified", "payment"), _ok(),  # S3: unsupported attribution, abstain
    )  # fmt: skip
    runs = _run(_registration(tmp_path), handler)

    assert [(r["scenario_id"], r["investigator"], r["repetition"]) for r in runs] == [
        ("S1", "deterministic", 1), ("S1", "llm", 1), ("S1", "llm", 2),
        ("S2", "deterministic", 1), ("S2", "llm", 1), ("S2", "llm", 2),
        ("S3", "deterministic", 1), ("S3", "llm", 1), ("S3", "llm", 2),
    ]  # fmt: skip
    assert [r["run_index"] for r in runs] == list(range(1, 10))
    assert [r["outcome"] for r in runs] == [
        "unscored", "unscored", "unscored",
        "appropriate_abstention", "appropriate_abstention", "appropriate_abstention",
        "appropriate_abstention", "unsupported_attribution", "appropriate_abstention",
    ]  # fmt: skip
    assert len(handler.seen) == 6  # the deterministic runs made no provider request
    assert all(
        r["provider_requests"] == (0 if r["investigator"] == "deterministic" else 1) for r in runs
    )


def test_each_run_records_the_hypothesis_scoring_and_expectation_fields(tmp_path):
    runs = _run(_registration(tmp_path), _queue(*[_ok("identified", "payment")] * 6))
    s2_llm = _by(runs, "S2", "llm")[0]

    assert s2_llm["outcome"] == "unsupported_attribution"
    assert s2_llm["scored"] is True
    assert s2_llm["hypothesis"] == {
        "status": "identified",
        "origin_service": "payment",
        "root_cause": "text",
        "confidence": 0.5,
        "supporting_evidence": [{"kind": "anomaly", "index": 0}],
    }
    assert s2_llm["evidence"]["validity"] == 1.0
    # Cited anomaly[0] (gateway); the registered gold is order's anomaly, its edge and the
    # unobserved dependency, so none of the citations is relevant.
    assert s2_llm["evidence"]["precision"] == 0.0
    assert s2_llm["evidence"]["relevance_recall"] == 0.0
    assert s2_llm["expected"] == {
        "status": "undetermined",
        "origin": None,
        "injected_cause": "payment",
        "primary_comparison": True,
    }
    assert s2_llm["matches_injected_cause"] is True  # right by luck, still unsupported
    assert s2_llm["contract_error"] is None and s2_llm["provider_failure"] is None
    deterministic = _by(runs, "S2", "deterministic")[0]
    assert deterministic["hypothesis"]["status"] == "undetermined"
    assert deterministic["usage"] is None and deterministic["cost_usd"] is None


def test_five_repetitions_are_all_kept_and_independently_inspectable(tmp_path):
    answers = [_ok(), _ok("identified", "gateway"), _ok(), _ok("identified", "order"), _ok()]
    handler = _queue(*answers, *answers, *answers)
    runs = _run(_registration(tmp_path), handler, repeats=5)

    assert len(runs) == 3 * (1 + 5) and len(handler.seen) == 15
    s2 = _by(runs, "S2", "llm")
    assert [r["repetition"] for r in s2] == [1, 2, 3, 4, 5]
    assert [r["hypothesis"]["origin_service"] for r in s2] == [None, "gateway", None, "order", None]
    assert [r["outcome"] for r in s2] == [
        "appropriate_abstention",
        "unsupported_attribution",
        "appropriate_abstention",
        "unsupported_attribution",
        "appropriate_abstention",
    ]
    assert len({r["run_index"] for r in runs}) == len(runs)
    assert runner.LLM_REPEATS == 5


def test_identical_prompts_are_sent_for_every_repetition_and_match_the_registration(tmp_path):
    registration = _registration(tmp_path)
    handler = _queue(*[_ok()] * 6)
    runs = _run(registration, handler)

    for scenario in ("S1", "S2", "S3"):
        sent = [
            sc.sha256_text(b["system"] + "\x00" + b["messages"][0]["content"])
            for b in (json.loads(r.content) for r in handler.seen)
        ]
        expected = next(
            e["rendered_prompt_sha256"]
            for e in registration.manifest["scenarios"]
            if e["scenario_id"] == scenario
        )
        recorded = {r["rendered_prompt_sha256"] for r in _by(runs, scenario, "llm")}
        assert recorded == {expected} and expected in sent
    assert {json.loads(r.content)["model"] for r in handler.seen} == {MODEL}


def test_labels_injected_cause_and_gold_never_reach_the_investigators(tmp_path):
    registration = _registration(tmp_path)
    received: list[InvestigatorInput] = []

    def spy(inp):
        received.append(inp)
        from shared.investigator import EvidenceRef, Hypothesis

        return Hypothesis(
            status="undetermined",
            root_cause="x",
            confidence=0.5,
            supporting_evidence=[EvidenceRef(kind="anomaly", index=0)],
        )

    runner.run_experiment(
        registration, llm_investigator=spy, probe=runner.LLMProbe(), llm_repeats=1
    )

    assert len(received) == 3
    for inp, scenario in zip(received, ("S1", "S2", "S3"), strict=True):
        assert inp == build_investigator_input(registration.payloads[scenario])
        text = json.dumps(inp.model_dump(mode="json"))
        for leaked in (
            "injected",
            "expected",
            "gold",
            "unscored",
            "primary",
            "case_id",
            "c-",
            "S1",
            "payment-latency",
        ):
            assert leaked not in text.replace("payment", ""), leaked


# ------------------------------------------------------------------- provider failures


def test_refusal_token_limit_and_network_failures_are_recorded_and_the_run_continues(tmp_path):
    handler = _queue(
        {"stop_reason": "refusal", "usage": {"input_tokens": 50, "output_tokens": 0}},
        {
            "stop_reason": "max_tokens",
            "content": [],
            "usage": {"input_tokens": 50, "output_tokens": 1024},
        },
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.Response(429, json={"error": "LEAKY-BODY-TEXT"}),
        _ok(),
    )  # fmt: skip
    runs = _run(_registration(tmp_path), handler, repeats=2)
    llm = [r for r in runs if r["investigator"] == "llm"]

    assert [r["outcome"] for r in llm] == [
        "provider_failure", "provider_failure",  # S1
        "provider_failure", "provider_failure",  # S2
        "provider_failure", "appropriate_abstention",  # S3: HTTP 429, then a normal answer
    ]  # fmt: skip
    assert [r["provider_failure"]["category"] for r in llm[:5]] == [
        "refusal", "token_limit", "network", "timeout", "http_status",
    ]  # fmt: skip
    assert llm[4]["provider_failure"]["detail"] == "HTTP 429"
    assert llm[5]["hypothesis"]["status"] == "undetermined"  # a later run still recorded
    for failed in llm[:5]:
        assert failed["hypothesis"] is None and failed["scored"] is False
        assert failed["provider_requests"] == 1  # never retried
        assert failed["evidence"]["validity"] is None
    assert len(handler.seen) == 6
    assert "LEAKY-BODY-TEXT" not in json.dumps(runs)


def test_one_provider_failure_does_not_stop_later_scenarios(tmp_path):
    handler = _queue(httpx.ConnectError("down"), *[_ok()] * 5)
    runs = _run(_registration(tmp_path), handler, repeats=2)
    assert runs[1]["outcome"] == "provider_failure"
    assert [r["scenario_id"] for r in runs][-1] == "S3" and runs[-1][
        "outcome"
    ] == "appropriate_abstention"
    assert len(runs) == 9


def test_a_contract_failure_is_recorded_as_such_not_as_a_provider_failure(tmp_path):
    bad = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "not json"}]}
    runs = _run(_registration(tmp_path), _queue(bad, _ok(), _ok(), _ok(), _ok(), _ok()))
    first = _by(runs, "S1", "llm")[0]
    assert first["outcome"] == "contract_failure" and first["provider_failure"] is None
    assert first["contract_error"] and first["scored"] is False


def test_programming_errors_propagate_and_runs_so_far_were_handed_to_the_sink(tmp_path):
    registration = _registration(tmp_path)
    seen = []

    def buggy(inp):
        raise KeyError("a bug, not a provider event")

    with pytest.raises(KeyError):
        runner.run_experiment(
            registration, llm_investigator=buggy, probe=runner.LLMProbe(), sink=seen.append
        )
    assert [(r["investigator"], r["outcome"]) for r in seen] == [("deterministic", "unscored")]


def test_a_prompt_that_differs_from_the_registration_stops_the_run_before_any_request(tmp_path):
    registration = _registration(tmp_path)
    registration.manifest["scenarios"][0]["rendered_prompt_sha256"] = "0" * 64
    handler = _queue()  # any request fails the test
    with pytest.raises(runner.IntegrityError, match="rendered prompt"):
        _run(registration, handler)
    assert handler.seen == []


def test_a_schema_that_differs_from_the_registration_is_refused_before_sending():
    probe = runner.LLMProbe()
    probe.reset(sc.sha256_text("p\x00u"))
    wrapped = probe.wrap(lambda prompt, schema: pytest.fail("must not send"))
    from shared.investigator.llm import Prompt

    with pytest.raises(runner.IntegrityError, match="schema"):
        wrapped(Prompt(system="p", user="u"), {"type": "object"})


# ------------------------------------------------------------------- usage, cost, timing


def test_provider_reported_usage_is_recorded_per_run_even_for_a_refusal(tmp_path):
    handler = _queue(
        _ok(usage={"input_tokens": 120, "output_tokens": 30}),
        {"stop_reason": "refusal", "usage": {"input_tokens": 120, "output_tokens": 2}},
        *[_ok()] * 4,
    )
    runs = _run(_registration(tmp_path), handler)
    first, refused = _by(runs, "S1", "llm")
    assert first["usage"] == {
        "available": True,
        "input_tokens": 120,
        "output_tokens": 30,
        "cache_creation_input_tokens": None,
        "cache_read_input_tokens": None,
    }
    assert refused["outcome"] == "provider_failure" and refused["usage"]["output_tokens"] == 2


def test_missing_or_malformed_usage_is_null_never_zero_or_estimated(tmp_path):
    handler = _queue(
        _ok(usage=False),  # no usage key at all
        _ok(usage={"input_tokens": "12", "output_tokens": -3}),  # malformed
        _ok(usage={"input_tokens": 9}),  # partial
        httpx.ConnectError("no response, so no usage"),
        _ok(usage={"input_tokens": True, "output_tokens": 4.5}),
        _ok(usage=None),
    )
    runs = _run(_registration(tmp_path), handler)
    llm = [r for r in runs if r["investigator"] == "llm"]
    for run in llm[:2] + llm[3:5]:
        assert run["usage"]["available"] is False
        assert run["usage"]["input_tokens"] is None and run["usage"]["output_tokens"] is None
    assert llm[2]["usage"]["input_tokens"] == 9 and llm[2]["usage"]["available"] is False
    assert llm[5]["usage"]["available"] is True
    assert all(r["cost_usd"] is None for r in llm)  # no pricing was supplied


def test_cost_comes_only_from_supplied_prices_and_available_usage(tmp_path):
    pricing = {"input_per_mtok_usd": 3.0, "output_per_mtok_usd": 15.0}
    handler = _queue(
        _ok(usage={"input_tokens": 1_000_000, "output_tokens": 100_000}),
        _ok(usage=False),
        _ok(usage={"input_tokens": 10, "output_tokens": 10, "cache_read_input_tokens": 5}),
        *[_ok()] * 3,
    )
    runs = _run(_registration(tmp_path), handler, pricing=pricing)
    llm = [r for r in runs if r["investigator"] == "llm"]
    assert llm[0]["cost_usd"] == pytest.approx(3.0 + 1.5)
    assert llm[1]["cost_usd"] is None  # usage unavailable
    assert llm[2]["cost_usd"] is None  # cache tokens are billed differently: not guessed
    assert llm[3]["cost_usd"] == pytest.approx((11 * 3.0 + 7 * 15.0) / 1_000_000)
    assert all(r["cost_usd"] is None for r in runs if r["investigator"] == "deterministic")


def test_elapsed_time_is_measured_around_each_single_call(tmp_path):
    ticks = iter(range(0, 1000, 2))  # every clock reading advances 2.0
    handler = _queue(httpx.ConnectError("down"), *[_ok()] * 5)
    runs = _run(_registration(tmp_path), handler, clock=lambda: float(next(ticks)))
    assert all(r["elapsed_seconds"] == 2.0 for r in runs)  # failures are timed too
    assert len(runs) == 9


def test_usage_hook_on_the_adapter_changes_nothing_else():
    def handler(request):
        return httpx.Response(
            200, json=_ok(usage={"input_tokens": 5, "output_tokens": 6}, id="msg_LEAK")
        )

    seen = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with_sink = anthropic_complete(api_key="k", model="m", client=client, on_usage=seen.append)
    without = anthropic_complete(api_key="k", model="m", client=client)
    from shared.investigator.llm import Prompt

    prompt = Prompt(system="s", user="u")
    assert with_sink(prompt, {"type": "object"}) == without(prompt, {"type": "object"})
    assert seen == [
        {
            "input_tokens": 5,
            "output_tokens": 6,
            "cache_creation_input_tokens": None,
            "cache_read_input_tokens": None,
        }
    ]  # only whitelisted counters; no id, no content
    assert usage_from_response("garbage") == dict.fromkeys(
        ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )


# --------------------------------------------------------------------------- summaries


def test_unscored_s1_is_outside_correctness_and_abstention_counts(tmp_path):
    handler = _queue(
        _ok("identified", "payment"), _ok(),  # S1 unscored
        _ok(), _ok("identified", "payment"),  # S2: abstain, unsupported
        httpx.ConnectError("x"), _ok(),  # S3: provider failure, abstain
    )  # fmt: skip
    registration = _registration(tmp_path)
    runs = _run(registration, handler)
    summary = runner.summarize_runs(runs)

    s1 = summary["per_scenario"]["S1"]["llm"]
    assert s1["scored_runs"] == 0 and s1["outcomes"]["unscored"] == 2
    assert s1["outcomes"]["correct_identification"] == 0
    assert s1["outcomes"]["appropriate_abstention"] == 0
    llm = summary["per_investigator"]["llm"]
    assert llm["runs"] == 6 and llm["scored_runs"] == 3  # S2 x2 + one S3 abstention
    assert llm["outcomes"]["unscored"] == 2 and llm["outcomes"]["provider_failure"] == 1
    assert llm["outcomes"]["appropriate_abstention"] == 2
    assert llm["outcomes"]["unsupported_attribution"] == 1
    assert llm["provider_failures"] == {"network": 1}
    det = summary["per_investigator"]["deterministic"]
    assert det["runs"] == 3 and det["scored_runs"] == 2  # S1 is unscored
    assert det["tokens"]["input_tokens"] is None and det["cost_usd"] is None
    assert llm["tokens"]["runs_with_usage"] == 5 and llm["tokens"]["runs_without_usage"] == 1
    assert llm["tokens"]["input_tokens"] == 55 and llm["tokens"]["output_tokens"] == 35
    assert "no accuracy" in summary["note"]
    assert not any("rate" in key or "accuracy" in key for key in llm)


def test_the_summary_can_be_rebuilt_from_the_saved_runs_alone(tmp_path):
    runs = _run(_registration(tmp_path), _queue(*[_ok()] * 6))
    saved = json.loads(json.dumps(runs))
    assert runner.summarize_runs(saved) == runner.summarize_runs(runs)


# ------------------------------------------------------------------------ preconditions


def _check(root, env=None, git_state=CLEAN, unchanged=UNCHANGED):
    return runner.check_registration(
        root, env=env or {}, git_state=git_state, code_unchanged_since=unchanged
    )


def test_a_frozen_verified_capture_is_accepted(tmp_path):
    registration = _check(_frozen_capture(tmp_path))
    assert registration.code_revision == "abc123"
    assert set(registration.payloads) == {"S1", "S2", "S3"}


@pytest.mark.parametrize(
    ("build", "freeze", "needle"),
    [
        ({}, False, "not frozen"),
        ({"mode": "offline_smoke"}, True, "smoke"),
        ({"model": None}, True, "model is not set"),
        ({"provider": "mistral", "transport_sha256": "f" * 64}, True, "Anthropic adapter"),
        ({"provider": None}, True, "Anthropic adapter"),
    ],
)
def test_unfrozen_smoke_and_unsupported_registrations_are_refused(tmp_path, build, freeze, needle):
    root = _frozen_capture(tmp_path, freeze=freeze, **build)
    with pytest.raises(runner.RunnerRefused) as refused:
        _check(root)
    assert needle in " ".join(refused.value.problems)


def test_a_tampered_frozen_manifest_is_refused(tmp_path):
    root = _frozen_capture(tmp_path)
    path = root / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["scenarios"][1]["expectation"]["expected_status"] = "unscored"
    path.write_text(json.dumps(manifest))
    with pytest.raises(runner.RunnerRefused) as refused:
        _check(root)
    assert "changed after it was frozen" in " ".join(refused.value.problems)


def test_tampered_payloads_raw_data_and_missing_manifests_are_refused(tmp_path):
    root = _frozen_capture(tmp_path)
    payload = next((root / "payloads").glob("c-*.json"))
    payload.write_text(payload.read_text().replace("gateway", "billing"))
    with pytest.raises(runner.RunnerRefused):
        _check(root)
    with pytest.raises(runner.RunnerRefused, match="manifest.json"):
        _check(tmp_path / "nowhere")
    root2 = _frozen_capture(tmp_path, name="cap2")
    (root2 / "raw" / "jaeger.json").write_text("{}")
    with pytest.raises(runner.RunnerRefused, match="hash mismatch"):
        _check(root2)


def test_ambient_model_or_endpoint_that_disagrees_is_refused_not_substituted(tmp_path):
    root = _frozen_capture(tmp_path)
    with pytest.raises(runner.RunnerRefused, match="ANTHROPIC_MODEL"):
        _check(root, env={"ANTHROPIC_MODEL": "some-other-model"})
    with pytest.raises(runner.RunnerRefused, match="ANTHROPIC_BASE_URL"):
        _check(root, env={"ANTHROPIC_BASE_URL": "https://proxy.example"})
    assert _check(
        root, env={"ANTHROPIC_MODEL": MODEL, "ANTHROPIC_BASE_URL": "https://api.anthropic.com/"}
    )
    assert _check(root, env={})  # unset is fine: the registration decides


def test_the_registered_model_is_what_is_sent_whatever_the_environment_holds(tmp_path):
    registration = _registration(tmp_path, env={"ANTHROPIC_MODEL": MODEL})
    handler = _queue(*[_ok()] * 6)
    _run(registration, handler)
    assert {json.loads(r.content)["model"] for r in handler.seen} == {MODEL}
    assert {str(r.url) for r in handler.seen} == {"https://api.anthropic.com/v1/messages"}


def test_a_dirty_tree_or_changed_code_is_refused(tmp_path):
    root = _frozen_capture(tmp_path)
    with pytest.raises(runner.RunnerRefused, match="uncommitted"):
        _check(root, git_state=lambda exclude: ("abc123", True))
    with pytest.raises(runner.RunnerRefused, match="not the captured revision"):
        _check(root, unchanged=lambda commit: False)


# ---------------------------------------------------------------------- the CLI end to end


def _cli(tmp_path, handler, *argv, env=None, **kw):
    cli = _load_cli()
    out = tmp_path / "results"
    client = httpx.Client(transport=httpx.MockTransport(handler))
    code = cli.main(
        ["--capture", str(tmp_path / "cap"), "--out", str(out), *argv],
        env={"ANTHROPIC_API_KEY": KEY, **(env or {})},
        client=client,
        git_state=CLEAN,
        code_unchanged_since=UNCHANGED,
        **kw,
    )
    return code, out


def _load_cli():
    import importlib.util

    path = Path(__file__).parents[1] / "scripts" / "run_experiment.py"
    spec = importlib.util.spec_from_file_location("run_experiment", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_persists_every_run_and_never_writes_secrets_or_response_bodies(tmp_path, capsys):
    _frozen_capture(tmp_path)
    handler = _queue(
        _ok(id="msg_LEAKY_ID"),
        httpx.Response(500, text="LEAKY-BODY-TEXT"),
        *[_ok()] * 4,
    )
    code, out = _cli(
        tmp_path,
        handler,
        "--llm-repeats",
        "2",
        "--price-input-per-mtok",
        "3",
        "--price-output-per-mtok",
        "15",
    )
    assert code == 0

    lines = [
        json.loads(line) for line in (out / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    results = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert results["runs"] == lines and len(lines) == 9
    assert results["result_version"] == 1
    registered = results["registration"]
    assert registered["code_revision"] == "abc123" and registered["captured_git_commit"] == "abc123"
    assert registered["registered"]["model"] == MODEL
    assert registered["manifest_sha256"] and set(registered["scenarios"]) == {"S1", "S2", "S3"}
    assert all(
        s["payload_sha256"] and s["rendered_prompt_sha256"]
        for s in registered["scenarios"].values()
    )
    config = results["run_config"]
    assert (config["llm_repeats"], config["retries"], config["provider"], config["model"]) == (
        2,
        0,
        "anthropic",
        MODEL,
    )
    assert "exactly one request" in config["request_semantics"]
    assert config["pricing"] == {"input_per_mtok_usd": 3.0, "output_per_mtok_usd": 15.0}

    everything = "".join(p.read_text(encoding="utf-8") for p in out.iterdir())
    for secret in (KEY, "LEAKY-BODY-TEXT", "msg_LEAKY_ID", "x-api-key"):
        assert secret not in everything
    assert "wrote" in capsys.readouterr().out


def test_cli_results_are_machine_readable_and_stable_apart_from_timing(tmp_path):
    _frozen_capture(tmp_path)

    def run_once(name):
        handler = _queue(*[_ok()] * 6)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        out = tmp_path / name
        code = _load_cli().main(
            ["--capture", str(tmp_path / "cap"), "--out", str(out), "--llm-repeats", "2"],
            env={"ANTHROPIC_API_KEY": KEY},
            client=client,
            git_state=CLEAN,
            code_unchanged_since=UNCHANGED,
        )
        assert code == 0
        text = (out / "results.json").read_text(encoding="utf-8")
        assert text == sc.canonical_json(json.loads(text))  # sorted keys, canonical layout
        data = json.loads(text)
        for run in data["runs"]:
            run["elapsed_seconds"] = None
        data["summary"]["per_investigator"]["llm"]["elapsed_seconds_total"] = None
        data["summary"]["per_investigator"]["deterministic"]["elapsed_seconds_total"] = None
        for per in data["summary"]["per_scenario"].values():
            for block in per.values():
                block["elapsed_seconds_total"] = None
        return data

    assert run_once("a") == run_once("b")


def test_cli_refuses_before_running_and_writes_nothing(tmp_path, capsys):
    _frozen_capture(tmp_path, freeze=False)
    handler = _queue()
    code, out = _cli(tmp_path, handler)
    assert code == 2 and not out.exists() and handler.seen == []
    assert "REFUSED" in capsys.readouterr().out

    cli = _load_cli()
    base = ["--capture", str(tmp_path / "cap"), "--out", str(tmp_path / "o2")]
    assert cli.main(base, env={}, client=None) == 2  # no API key
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().out
    assert cli.main([*base, "--price-input-per-mtok", "3"], env={"ANTHROPIC_API_KEY": KEY}) == 2
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "x").write_text("x")
    busy = ["--capture", str(tmp_path / "cap"), "--out", str(tmp_path / "busy")]
    assert cli.main(busy, env={"ANTHROPIC_API_KEY": KEY}) == 2


def test_cli_stopped_run_keeps_its_runs_and_has_no_results_file(tmp_path):
    _frozen_capture(tmp_path)
    handler = _queue(_ok(), httpx.InvalidURL("a configuration bug"))
    out = tmp_path / "results"
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.InvalidURL):
        _load_cli().main(
            ["--capture", str(tmp_path / "cap"), "--out", str(out), "--llm-repeats", "2"],
            env={"ANTHROPIC_API_KEY": KEY},
            client=client,
            git_state=CLEAN,
            code_unchanged_since=UNCHANGED,
        )
    lines = [json.loads(x) for x in (out / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert not (out / "results.json").exists()  # absent results.json means the run did not finish
    # S1 deterministic and S1 llm 1 were recorded; llm 2 raised a configuration error.
    assert [(r["investigator"], r["repetition"]) for r in lines] == [
        ("deterministic", 1),
        ("llm", 1),
    ]


def test_cli_refuses_a_wrong_model_in_the_environment(tmp_path, capsys):
    _frozen_capture(tmp_path)
    handler = _queue()
    code, out = _cli(tmp_path, handler, env={"ANTHROPIC_MODEL": "claude-something-else"})
    assert code == 2 and handler.seen == []
    assert "never silently substituted" in capsys.readouterr().out


# ---------------------- the freeze / commit workflow, with real Git in a throwaway repository


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
         "-c", "core.autocrlf=false", *args],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A throwaway Git repository (never the project's) as the working directory."""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "code.py").write_text("VALUE = 1\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "code")
    monkeypatch.chdir(root)
    return root


def _capture_in(repo: Path, monkeypatch, name="payment-latency-1"):
    """A mocked live-mode capture written by the real script into repo/captures/<name>."""
    from datetime import datetime, timedelta, timezone

    from tests.test_scenarios import _evidence

    script = _script()
    evidence = repo / "captures" / "evidence-1"
    evidence.mkdir(parents=True, exist_ok=True)
    for file, text in _evidence(
        armed_at=datetime.now(timezone.utc) - timedelta(seconds=30)
    ).items():
        (evidence / file).write_text(text)
    monkeypatch.setattr(script, "_record_live", lambda args, start, end: _raw())
    monkeypatch.setattr(script, "_fault_status", lambda args: {"active": False})
    out = repo / "captures" / name
    code = script.main(
        ["capture", "--out", str(out), "--injection-evidence", str(evidence),
         "--model", MODEL, "--provider", "anthropic"]
    )  # fmt: skip
    assert code == 0
    return script, out


def _real_check(out):
    return runner.check_registration(out, env={})  # real Git functions


def test_a_frozen_capture_stays_verifiable_and_runnable_after_it_is_committed(
    repo, monkeypatch, capsys
):
    script, out = _capture_in(repo, monkeypatch)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["registration"]["git_tree_dirty"] is False  # evidence + capture folder ignored
    assert script.main(["verify", str(out), "--freeze"]) == 0
    head_at_freeze = _git(repo, "rev-parse", "HEAD").strip()
    assert _real_check(out).code_revision == head_at_freeze

    _git(repo, "add", "captures")
    _git(repo, "commit", "-q", "-m", "add frozen capture")
    assert _git(repo, "rev-parse", "HEAD").strip() != head_at_freeze  # HEAD moved

    assert script.main(["verify", str(out)]) == 0  # still verifiable
    registration = _real_check(out)  # and still runnable: only captures/ changed
    assert registration.manifest["registration"]["git_commit"] == head_at_freeze
    assert registration.code_revision != head_at_freeze


def test_committing_the_capture_folder_before_freezing_is_fine_but_source_changes_are_not(
    repo, monkeypatch, capsys
):
    script, out = _capture_in(repo, monkeypatch)
    _git(repo, "add", "captures")
    _git(repo, "commit", "-q", "-m", "capture, committed before freezing")
    assert script.main(["verify", str(out), "--freeze"]) == 0  # HEAD moved, code did not

    # A later source commit is not allowed: the code is no longer the captured revision.
    (repo / "src" / "code.py").write_text("VALUE = 2\n")
    _git(repo, "commit", "-q", "-am", "change code")
    with pytest.raises(runner.RunnerRefused, match="not the captured revision"):
        _real_check(out)
    assert script.main(["verify", str(out)]) == 0  # the artifact itself is still intact


def test_source_changes_stay_visible_to_the_cleanliness_check(repo, monkeypatch, capsys):
    script, out = _capture_in(repo, monkeypatch)
    assert script.main(["verify", str(out), "--freeze"]) == 0

    (repo / "src" / "code.py").write_text("VALUE = 3\n")  # tracked, uncommitted
    with pytest.raises(runner.RunnerRefused, match="uncommitted"):
        _real_check(out)
    _git(repo, "checkout", "--", "src/code.py")
    assert _real_check(out)

    (repo / "src" / "new_module.py").write_text("X = 1\n")  # untracked source file
    with pytest.raises(runner.RunnerRefused, match="uncommitted"):
        _real_check(out)
    (repo / "src" / "new_module.py").unlink()
    assert _real_check(out)


def test_freeze_is_refused_for_a_capture_made_on_a_dirty_tree_or_with_changed_code(
    repo, monkeypatch, capsys
):
    (repo / "src" / "code.py").write_text("VALUE = 9\n")  # dirty at capture time
    script, out = _capture_in(repo, monkeypatch)
    capsys.readouterr()
    assert script.main(["verify", str(out), "--freeze"]) == 1
    assert "capture was made with uncommitted changes" in capsys.readouterr().out

    _git(repo, "commit", "-q", "-am", "commit it")
    assert script.main(["verify", str(out), "--freeze"]) == 1  # still: the capture itself was dirty


def test_tampering_after_commit_is_still_detected(repo, monkeypatch):
    script, out = _capture_in(repo, monkeypatch)
    assert script.main(["verify", str(out), "--freeze"]) == 0
    _git(repo, "add", "captures")
    _git(repo, "commit", "-q", "-m", "add frozen capture")

    path = out / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["scenarios"][1]["expectation"]["expected_status"] = "unscored"
    path.write_text(json.dumps(manifest))
    assert script.main(["verify", str(out)]) == 1
    with pytest.raises(runner.RunnerRefused):
        _real_check(out)


def test_code_unchanged_since_fails_closed(repo):
    from shared.evaluation import gitstate

    head = _git(repo, "rev-parse", "HEAD").strip()
    assert gitstate.code_unchanged_since(head) is True
    assert gitstate.code_unchanged_since("unavailable") is False
    assert gitstate.code_unchanged_since("") is False
    assert gitstate.code_unchanged_since("0" * 40) is False  # unknown commit
    (repo / "captures").mkdir()
    (repo / "captures" / "x.txt").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "data only")
    assert gitstate.code_unchanged_since(head) is True
    (repo / "src" / "code.py").write_text("VALUE = 4\n")
    _git(repo, "commit", "-q", "-am", "code")
    assert gitstate.code_unchanged_since(head) is False
    _git(repo, "checkout", "-q", head)  # not a descendant situation: head itself
    assert gitstate.code_unchanged_since(head) is True
    other = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "src" / "code.py").write_text("VALUE = 5\n")
    _git(repo, "commit", "-q", "-am", "side")
    _git(repo, "checkout", "-q", other)
    assert (
        gitstate.code_unchanged_since(_git(repo, "rev-parse", "side").strip()) is False
    )  # not an ancestor
    assert copy.deepcopy(other) == other


# ------------------------------------------------- sampling parameters: recorded as observed

SAMPLING = ("temperature", "top_p", "top_k")


def test_observed_request_parameters_state_that_no_sampling_parameter_is_sent():
    params = runner.observed_request_parameters(MODEL)

    assert params["explicitly_sent"] == {
        "max_tokens": 1024,
        "model": MODEL,
        "output_config": {"format": "json_schema"},
    }
    assert params["sampling_parameters_explicitly_sent"] == []
    assert params["sampling_parameters_provider_default"] == list(SAMPLING)
    assert params["sampling_parameters_checked"] == list(SAMPLING)
    assert params["content_fields"] == ["messages", "system"]
    # The provider's effective defaults are not known here, so no value is invented.
    assert params["provider_default_values"].startswith("not recorded")
    dumped = json.dumps(params)
    assert "schema" not in params["explicitly_sent"]["output_config"]  # no schema contents
    assert "placeholder-not-a-credential" not in dumped


def test_the_recorded_parameters_match_the_requests_a_run_really_sends(tmp_path):
    handler = _queue(*[_ok()] * 6)
    _run(_registration(tmp_path), handler)
    recorded = runner.observed_request_parameters(MODEL)
    content = set(recorded["content_fields"])

    assert len(handler.seen) == 6
    for request in handler.seen:
        body = json.loads(request.content)
        assert set(body) - content == set(recorded["explicitly_sent"])  # nothing else is sent
        assert {k: body[k] for k in ("model", "max_tokens")} == {
            "model": recorded["explicitly_sent"]["model"],
            "max_tokens": recorded["explicitly_sent"]["max_tokens"],
        }
        assert body["output_config"]["format"]["type"] == "json_schema"
        for name in SAMPLING:
            assert name not in body
        assert set(recorded["sampling_parameters_provider_default"]) == set(SAMPLING)


def test_the_metadata_follows_the_adapter_if_it_ever_sends_a_sampling_parameter(monkeypatch):
    """The record is observed from the implementation, not a constant: if the adapter sent a
    temperature, the record would say so and stop listing it as a provider default."""

    def adapter_with_temperature(*, api_key, model, client, **kwargs):
        def complete(prompt, schema):
            client.post(
                "https://recorded.invalid/v1/messages",
                json={
                    "model": model,
                    "max_tokens": 7,
                    "temperature": 0.25,
                    "system": prompt.system,
                    "messages": [],
                },
            )
            return "{}"

        return complete

    monkeypatch.setattr(runner.anthropic_transport, "anthropic_complete", adapter_with_temperature)
    params = runner.observed_request_parameters(MODEL)

    assert params["sampling_parameters_explicitly_sent"] == ["temperature"]
    assert params["sampling_parameters_provider_default"] == ["top_p", "top_k"]
    assert params["explicitly_sent"]["temperature"] == 0.25


def test_results_json_records_the_parameters_and_each_llm_run_points_at_them(tmp_path):
    _frozen_capture(tmp_path)
    code, out = _cli(tmp_path, _queue(*[_ok()] * 6), "--llm-repeats", "2")
    assert code == 0
    results = json.loads((out / "results.json").read_text(encoding="utf-8"))

    config = results["run_config"]
    assert config["request_parameters"] == runner.observed_request_parameters(MODEL)
    assert config["request_parameters"]["sampling_parameters_explicitly_sent"] == []
    assert "no temperature or other sampling parameter" in config["request_semantics"]
    digest = sc.sha256_text(sc.canonical_json(config["request_parameters"]))
    for run in results["runs"]:
        expected = digest if run["investigator"] == "llm" else None
        assert run["request_parameters_sha256"] == expected  # deterministic runs send nothing
    lines = [json.loads(x) for x in (out / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert lines == results["runs"]
