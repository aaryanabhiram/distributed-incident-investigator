"""Experiment runner: execute a verified, frozen registration and record every run.

`check_registration` refuses anything that is not a frozen, unmodified, non-smoke capture made from
the code that is checked out now. `run_experiment` then runs the deterministic baseline once and the
registered LLM investigator `llm_repeats` times (5 in the registration) on each registered payload
and returns one record per run. Nothing here talks to the network itself: the LLM investigator is
built by `build_llm_investigator` from the shipped Anthropic adapter and an injected, mockable
`httpx.Client`.

Exact semantics (also written into every result file):
- one scenario x investigator x repetition is one run; every run is recorded separately and in
  order, and none is overwritten, merged or dropped;
- a deterministic run makes no provider request; an LLM run makes exactly one request: no retry, no
  repair, no fallback, no second call, and the repetitions are independent calls with an identical
  prompt (the adapter sets no temperature or other sampling parameter, so the provider defaults
  apply);
- a provider failure is a recorded, non-scored event and the run continues; a programming or
  configuration error (anything `run_scenario` does not classify) propagates and stops the run, with
  the runs recorded so far already handed to the sink;
- labels, the injected cause and gold evidence are used only for scoring after the investigator has
  returned; the investigator receives the `InvestigatorInput` built from the frozen payload alone;
- elapsed time is `perf_counter` around the single investigator call (network included for the
  LLM), not around scoring;
- token usage is what the provider reported for that response, or unavailable (null); it is never
  0 or estimated. Cost is computed only from operator-supplied prices and available usage.

Result artifacts hold no credentials, headers or response bodies.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from shared.evaluation import (
    OUTCOMES,
    ScenarioExpectation,
    gitstate,
    redact,
    run_scenario,
)
from shared.evaluation import scenarios as sc
from shared.investigator import Hypothesis, InvestigatorInput, build_investigator_input
from shared.investigator import anthropic as anthropic_transport
from shared.investigator.deterministic import DeterministicInvestigator
from shared.investigator.llm import LLMInvestigator, Prompt, response_schema

RESULT_VERSION = 1
LLM_REPEATS = 5
# Outcomes that count toward correctness and abstention. `unscored`, `contract_failure` and
# `provider_failure` are recorded but are never in these denominators.
SCORED_OUTCOMES = (
    "correct_identification",
    "false_attribution",
    "unsupported_attribution",
    "appropriate_abstention",
    "over_abstention",
)
# The Messages API's sampling parameters, checked for in the request. Not an exhaustive list of
# every request field: it is what "no sampling parameter was sent" is tested against.
SAMPLING_PARAMETERS = ("temperature", "top_p", "top_k")
_CONTENT_FIELDS = ("system", "messages")  # what is asked, not how the model is configured
REQUEST_SEMANTICS = (
    "One run = one scenario x investigator x repetition, recorded separately. Deterministic: no "
    "provider request. LLM: exactly one request per run; no retry, repair, fallback or second "
    "call; repetitions are independent calls with an identical prompt; the adapter sets no "
    "temperature or other sampling parameter (provider defaults). Provider failures are recorded "
    "as non-scored events and the run continues; programming or configuration errors stop it."
)


def observed_request_parameters(model: str) -> dict[str, Any]:
    """What the shipped Anthropic adapter actually puts in a request, observed rather than assumed.

    Runs the real adapter once against an in-memory recording transport (no network, no
    credentials, a placeholder prompt) and reads the request body it built. Reports the
    explicitly sent non-content parameters, which sampling parameters were and were not sent, and
    that the effective values of the unsent ones are the provider's defaults. The provider's
    default values are deliberately not recorded: they are not known to this runner and may change.
    """
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        reply = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "{}"}]}
        return httpx.Response(200, json=reply)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    complete = anthropic_transport.anthropic_complete(
        api_key="placeholder-not-a-credential", model=model, client=client
    )
    complete(Prompt(system="s", user="u"), {"type": "object"})
    body = seen["body"]

    def summary(value: Any) -> Any:
        if isinstance(value, dict):  # name the nested options, never the schema contents
            return {
                k: (v["type"] if isinstance(v, dict) and "type" in v else v)
                for k, v in value.items()
            }
        return value

    sent = [name for name in SAMPLING_PARAMETERS if name in body]
    return {
        "source": (
            "observed by running the shipped adapter against an in-memory recording transport "
            "(no network)"
        ),
        "explicitly_sent": {k: summary(body[k]) for k in sorted(body) if k not in _CONTENT_FIELDS},
        "content_fields": sorted(k for k in body if k in _CONTENT_FIELDS),
        "sampling_parameters_checked": list(SAMPLING_PARAMETERS),
        "sampling_parameters_explicitly_sent": sent,
        "sampling_parameters_provider_default": [n for n in SAMPLING_PARAMETERS if n not in sent],
        "provider_default_values": "not recorded: not known to this runner",
        "note": "max_tokens is a generation length limit, not a sampling parameter",
    }


class RunnerRefused(Exception):
    """The registration cannot be run; `problems` says why."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class IntegrityError(RuntimeError):
    """What the adapter was about to send differs from the registration; not a provider event."""


@dataclass(frozen=True)
class Registration:
    manifest: dict[str, Any]
    payloads: dict[str, dict[str, Any]]  # scenario_id -> frozen payload
    code_revision: str


def check_registration(
    root: Path,
    *,
    env: Mapping[str, str],
    git_state: Callable[[Path | None], tuple[str, bool]] = gitstate.git_state,
    code_unchanged_since: Callable[[str], bool] = gitstate.code_unchanged_since,
) -> Registration:
    """Load and validate a capture folder; raise `RunnerRefused` unless it can be run.

    Refused: unreadable or tampered data (everything `verify_manifest` re-derives), an unfrozen or
    smoke-only manifest, an unset or unsupported model/provider, a dirty working tree, code that
    changed since the capture, and ambient settings that disagree with the registration
    (`ANTHROPIC_MODEL`, `ANTHROPIC_BASE_URL`). A frozen manifest stays runnable after its capture
    folder is committed: HEAD may move as long as nothing outside `captures/` changed.
    """
    manifest, payloads, raw, evidence, problems = sc.load_capture(root)
    if manifest is None:
        raise RunnerRefused(problems)
    problems += sc.verify_manifest(manifest, payloads=payloads, raw=raw, evidence=evidence)
    if problems:
        raise RunnerRefused(problems)

    if manifest.get("frozen") is not True:
        problems.append("the manifest is not frozen (run verify --freeze first)")
    if manifest["smoke_test_only"] is not False or manifest["capture"]["mode"] != "live":
        problems.append("smoke-test data cannot be run as an experiment")
    registration = manifest["registration"]
    model, transport = registration.get("model"), registration["transport"]
    if not model:
        problems.append("registration.model is not set")
    if transport.get("provider") != "anthropic" or transport.get("verified_by_repo") is not True:
        problems.append(
            "only the shipped, repository-verified Anthropic adapter can be run "
            f"(registered provider: {transport.get('provider')!r})"
        )
    ambient_model = env.get("ANTHROPIC_MODEL")
    if ambient_model and ambient_model != model:
        problems.append(
            f"ANTHROPIC_MODEL is {ambient_model!r} but the registration says {model!r}; unset it "
            "or change it to match (the registered model is never silently substituted)"
        )
    ambient_url = env.get("ANTHROPIC_BASE_URL")
    if ambient_url and ambient_url.rstrip("/") != anthropic_transport.DEFAULT_BASE_URL:
        problems.append("ANTHROPIC_BASE_URL differs from the registered endpoint")
    head, dirty = git_state(root)
    if dirty:
        problems.append("the working tree has uncommitted changes outside captures/")
    if not code_unchanged_since(registration["git_commit"]):
        problems.append("the code is not the captured revision (or Git could not confirm it)")
    if problems:
        raise RunnerRefused(problems)

    by_scenario = {e["scenario_id"]: payloads[e["payload_ref"]] for e in manifest["scenarios"]}
    return Registration(manifest=manifest, payloads=by_scenario, code_revision=head)


class LLMProbe:
    """Per-run state shared between the runner and the adapter hooks.

    Records the usage the provider reported, counts requests, and checks, before anything is
    sent, that the prompt and schema about to go out are exactly the registered ones.
    """

    def __init__(self) -> None:
        self.schema_sha256 = sc.sha256_text(json.dumps(response_schema(), sort_keys=True))
        self.reset(None)

    def reset(self, expected_prompt_sha256: str | None) -> None:
        self.expected_prompt_sha256 = expected_prompt_sha256
        self.usage: dict[str, int | None] | None = None
        self.requests = 0
        self.prompt_sha256: str | None = None

    def usage_sink(self, usage: dict[str, int | None]) -> None:
        self.usage = usage

    def wrap(self, complete: Callable[[Prompt, dict[str, Any]], str]):
        def checked(prompt: Prompt, schema: dict[str, Any]) -> str:
            self.prompt_sha256 = sc.sha256_text(prompt.system + "\x00" + prompt.user)
            if self.prompt_sha256 != self.expected_prompt_sha256:
                raise IntegrityError("the rendered prompt differs from the registered prompt hash")
            if sc.sha256_text(json.dumps(schema, sort_keys=True)) != self.schema_sha256:
                raise IntegrityError("the response schema differs from the registered schema")
            self.requests += 1
            return complete(prompt, schema)

        return checked


def build_llm_investigator(
    registration: Registration,
    *,
    api_key: str,
    probe: LLMProbe,
    client: httpx.Client | None = None,
) -> LLMInvestigator:
    """The shipped Anthropic adapter with the REGISTERED model and endpoint, no ambient settings."""
    complete = anthropic_transport.anthropic_complete(
        api_key=api_key,
        model=registration.manifest["registration"]["model"],
        base_url=anthropic_transport.DEFAULT_BASE_URL,
        client=client,
        on_usage=probe.usage_sink,
    )
    return LLMInvestigator(probe.wrap(complete))


class _Measured:
    """Times one investigator call and keeps what it returned, without changing either."""

    def __init__(self, inner: Callable[[InvestigatorInput], Hypothesis], clock) -> None:
        self._inner, self._clock = inner, clock
        self.elapsed: float | None = None
        self.produced: Any = None

    def __call__(self, investigator_input: InvestigatorInput) -> Hypothesis:
        start = self._clock()
        try:
            self.produced = self._inner(investigator_input)
            return self.produced
        finally:
            self.elapsed = self._clock() - start


def _hypothesis_fields(produced: Any) -> dict[str, Any] | None:
    if not isinstance(produced, Hypothesis):
        return None
    try:
        dumped = produced.model_dump(mode="json", warnings=False)
    except Exception:  # a malformed instance may not serialise; the scorer reports it
        return None
    keys = ("status", "origin_service", "root_cause", "confidence", "supporting_evidence")
    return {k: dumped.get(k) for k in keys}


def _usage_record(probe: LLMProbe) -> dict[str, Any]:
    usage = probe.usage or {}
    record: dict[str, Any] = {name: usage.get(name) for name in anthropic_transport.USAGE_FIELDS}
    record["available"] = (
        usage.get("input_tokens") is not None and usage.get("output_tokens") is not None
    )
    return record


def _cost(usage: dict[str, Any] | None, pricing: dict[str, float] | None) -> float | None:
    """USD from operator-supplied prices and PROVIDER-REPORTED usage; otherwise `None`.

    Not computed (null) when there is no pricing, no usage, or any cache-token count is non-zero
    (cache tokens are billed at other rates this runner does not know).
    """
    if not pricing or not usage or not usage["available"]:
        return None
    if usage.get("cache_creation_input_tokens") or usage.get("cache_read_input_tokens"):
        return None
    return (
        usage["input_tokens"] * pricing["input_per_mtok_usd"]
        + usage["output_tokens"] * pricing["output_per_mtok_usd"]
    ) / 1_000_000


def run_experiment(
    registration: Registration,
    *,
    llm_investigator: Callable[[InvestigatorInput], Hypothesis],
    probe: LLMProbe,
    llm_repeats: int = LLM_REPEATS,
    pricing: dict[str, float] | None = None,
    clock: Callable[[], float] = time.perf_counter,
    sink: Callable[[dict[str, Any]], None] = lambda record: None,
) -> list[dict[str, Any]]:
    """Run every registered scenario; return (and pass to `sink`) one record per run.

    Order is fixed: scenarios in manifest order; per scenario the deterministic run, then LLM
    repetitions 1..`llm_repeats`. The same `InvestigatorInput` object feeds both investigators.
    """
    if llm_repeats < 1:
        raise ValueError("llm_repeats must be at least 1")
    deterministic = DeterministicInvestigator()
    parameters_sha256 = sc.sha256_text(
        sc.canonical_json(
            observed_request_parameters(registration.manifest["registration"]["model"])
        )
    )
    runs: list[dict[str, Any]] = []
    for entry in registration.manifest["scenarios"]:
        scenario_id = entry["scenario_id"]
        expectation = ScenarioExpectation.model_validate(entry["expectation"])
        investigator_input = build_investigator_input(registration.payloads[scenario_id])
        plan = [("deterministic", 1)] + [("llm", i) for i in range(1, llm_repeats + 1)]
        for name, repetition in plan:
            is_llm = name == "llm"
            if is_llm:
                probe.reset(entry["rendered_prompt_sha256"])
            measured = _Measured(llm_investigator if is_llm else deterministic, clock)
            result = run_scenario(expectation, investigator_input, measured)
            usage = _usage_record(probe) if is_llm else None
            record = {
                "run_index": len(runs) + 1,
                "scenario_id": scenario_id,
                "case_id": expectation.case_id,
                "investigator": name,
                "repetition": repetition,
                "expected": {
                    "status": expectation.expected_status,
                    "origin": expectation.expected_origin,
                    "injected_cause": expectation.injected_cause,
                    "primary_comparison": entry["primary_comparison"],
                },
                "outcome": result.outcome,
                "scored": result.outcome in SCORED_OUTCOMES,
                "hypothesis": _hypothesis_fields(measured.produced),
                "evidence": {
                    "validity": result.evidence_validity,
                    "precision": result.evidence_precision,
                    "relevance_recall": result.evidence_recall,
                },
                "matches_injected_cause": result.matches_injected_cause,
                "contract_error": redact(result.error, 300) if result.error else None,
                "provider_failure": (
                    {"category": result.failure_category, "detail": result.failure_detail}
                    if result.outcome == "provider_failure"
                    else None
                ),
                "elapsed_seconds": round(measured.elapsed, 6)
                if measured.elapsed is not None
                else None,
                "provider_requests": probe.requests if is_llm else 0,
                "request_parameters_sha256": parameters_sha256 if is_llm else None,
                "rendered_prompt_sha256": probe.prompt_sha256 if is_llm else None,
                "usage": usage,
                "cost_usd": _cost(usage, pricing),
            }
            runs.append(record)
            sink(record)
    return runs


def summarize_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Descriptive counts from the recorded runs alone (no rerun needed). No rates are computed.

    `scored_runs` is the denominator for correctness and abstention: it excludes `unscored`,
    `contract_failure` and `provider_failure` runs. Token totals cover only runs whose provider
    reported usage; `None` means no run did.
    """

    def block(selected: list[dict[str, Any]]) -> dict[str, Any]:
        outcomes = dict.fromkeys(OUTCOMES, 0)
        failures: dict[str, int] = {}
        for run in selected:
            outcomes[run["outcome"]] += 1
            if run["provider_failure"]:
                category = run["provider_failure"]["category"]
                failures[category] = failures.get(category, 0) + 1
        with_usage = [r for r in selected if r["usage"] and r["usage"]["available"]]
        costs = [r["cost_usd"] for r in selected if r["cost_usd"] is not None]
        return {
            "runs": len(selected),
            "scored_runs": sum(r["scored"] for r in selected),
            "outcomes": outcomes,
            "provider_failures": dict(sorted(failures.items())),
            "elapsed_seconds_total": round(sum(r["elapsed_seconds"] or 0.0 for r in selected), 6),
            "tokens": {
                "runs_with_usage": len(with_usage),
                "runs_without_usage": sum(1 for r in selected if r["usage"] is not None)
                - len(with_usage),
                "input_tokens": sum(r["usage"]["input_tokens"] for r in with_usage)
                if with_usage
                else None,
                "output_tokens": sum(r["usage"]["output_tokens"] for r in with_usage)
                if with_usage
                else None,
            },
            "cost_usd": sum(costs) if costs else None,
        }

    investigators = sorted({r["investigator"] for r in runs})
    scenarios = sorted({r["scenario_id"] for r in runs})
    return {
        "per_investigator": {
            name: block([r for r in runs if r["investigator"] == name]) for name in investigators
        },
        "per_scenario": {
            sid: {
                name: block(
                    [r for r in runs if r["scenario_id"] == sid and r["investigator"] == name]
                )
                for name in investigators
            }
            for sid in scenarios
        },
        "note": (
            "Counts only. A handful of scenarios and repetitions supports no accuracy or "
            "calibration claim. Unscored, contract-failure and provider-failure runs are outside "
            "scored_runs."
        ),
    }


def build_results(
    registration: Registration,
    runs: list[dict[str, Any]],
    *,
    llm_repeats: int,
    pricing: dict[str, float] | None,
) -> dict[str, Any]:
    """The machine-readable result document: what was registered, what was run, every run."""
    manifest = registration.manifest
    entries = manifest["scenarios"]
    return {
        "result_version": RESULT_VERSION,
        "registration": {
            "manifest_sha256": manifest["manifest_sha256"],
            "registered": manifest["registration"],
            "captured_git_commit": manifest["registration"]["git_commit"],
            "code_revision": registration.code_revision,
            "code_tree_clean": True,
            "capture_window": [
                manifest["capture"]["window_start"],
                manifest["capture"]["window_end"],
            ],
            "scenarios": {
                e["scenario_id"]: {
                    "case_id": e["expectation"]["case_id"],
                    "kind": e["kind"],
                    "payload_sha256": e["payload_sha256"],
                    "rendered_prompt_sha256": e["rendered_prompt_sha256"],
                }
                for e in entries
            },
        },
        "run_config": {
            "provider": manifest["registration"]["transport"]["provider"],
            "model": manifest["registration"]["model"],
            "llm_repeats": llm_repeats,
            "deterministic_repeats": 1,
            "retries": 0,
            "request_semantics": REQUEST_SEMANTICS,
            "request_parameters": observed_request_parameters(manifest["registration"]["model"]),
            "pricing": pricing,
            "cost_basis": "operator-supplied prices x provider-reported tokens; null otherwise",
        },
        "runs": runs,
        "summary": summarize_runs(runs),
    }
