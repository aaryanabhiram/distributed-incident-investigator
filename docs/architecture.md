# Architecture

This document describes the intended design of the Distributed Incident Investigator. It is
written before most of the system exists, so it describes the target shape and the boundaries
between components, not implementation detail that doesn't exist yet. Update it as real
decisions are made during each milestone.

## Goals

- A small set of real services producing real multi-service HTTP traffic.
- Full telemetry (logs, metrics, traces) collected from every service.
- A controlled way to inject faults into a running service.
- A deterministic engine that turns telemetry into "here's what broke and what it touched."
- A bounded, structured incident context that an LLM can reason over afterward.
- A system one developer can explain end-to-end in an interview, including *why* each piece
  exists.

## System boundaries

The system has four boundaries, each independently testable:

1. **Service boundary** — the services themselves and the HTTP calls between them. This is
   the thing being observed.
2. **Telemetry boundary** — how logs/metrics/traces leave a service and become queryable data
   (OpenTelemetry → Prometheus/Grafana, and whatever log/trace storage a later milestone
   picks). Telemetry is a side effect of normal service operation, not something the business
   logic is aware of beyond instrumentation calls.
3. **Fault-injection boundary** — a deliberate, explicit interface for making a service behave
   badly (added latency, error responses, resource pressure) on demand, for test/demo
   purposes. Faults are injected *at* a service, never inside the correlation or LLM layers.
4. **Investigation boundary** — deterministic correlation, then LLM reasoning, both operating
   only on telemetry already produced by boundary 2. Neither can reach back into a live service
   or bypass the telemetry that was actually collected.

Keeping these as separate, named boundaries is what keeps the system explainable: each one has
a single job and a clear input/output.

## Planned services

Kept intentionally small — enough to produce a real multi-hop call graph, not a simulated
microservice sprawl.

- **gateway** — entry point; receives a checkout request from the client, calls `order`
  (`services/gateway`).
- **order** — accepts an order request from the gateway, generates an order id, calls
  `payment` (`services/order`).
- **payment** — simulates payment processing with a deterministic approve/decline rule (no
  randomness); the primary target for fault injection in a later milestone, since a
  slow/broken payment service is a realistic, easy-to-reason-about incident
  (`services/payment`).

Three services is enough to have a real chain (`gateway → order → payment`) with a clear
upstream/downstream relationship for the correlation layer to reason about, without needing a
service mesh's worth of moving parts.

## Communication flow

Plain synchronous HTTP (via FastAPI/`httpx`), not a message queue. A queue would decouple
producers from consumers, which is valuable at real scale — it's not needed for a handful of
services on a laptop, and it would obscure the request chain the correlation layer relies on.
Synchronous HTTP also keeps distributed tracing trivial to reason about: one trace per client
request, spanning every service it touched.

```
client → gateway → order → payment
```

## Telemetry flow

```
service (instrumented with OpenTelemetry SDK via shared/telemetry)
    │
    ├── traces ──► OTLP/gRPC ──► Jaeger (all-in-one) ──► Jaeger UI / Grafana datasource
    ├── metrics ─► GET /metrics (OTel Prometheus reader) ──► Prometheus (scraped) ──► Grafana
    └── logs ────► structured JSON on stdout (trace_id/span_id/service fields)
```

- Every service shares one instrumentation setup (`shared/telemetry.setup_telemetry`) so
  trace/metric/log conventions are consistent across services — this is what makes
  cross-service correlation possible at all. It auto-instruments FastAPI (inbound requests)
  and `httpx` (outbound calls), so a checkout request produces one connected trace across
  `gateway → order → payment` without manual span code in business logic.
- Every log line and span carries a `trace_id`/`span_id` (when in a request context) so a
  single client request can be reconstructed across all services it touched.
- **Traces** are exported over OTLP/gRPC to **Jaeger** (`jaegertracing/all-in-one`), the
  smallest option that gives a real trace backend with its own inspection UI and no separate
  collector process. `OTEL_EXPORTER_OTLP_ENDPOINT` controls the target, so the same code path
  runs locally (`http://localhost:4317`, if a collector happens to be running) or under Docker
  Compose (`http://jaeger:4317`) without change.
- **Metrics** are pulled by Prometheus from each service's own `/metrics` endpoint (an
  OpenTelemetry `PrometheusMetricReader` mounted as an ASGI app) — no push gateway, no
  intermediate metrics pipeline. Prometheus attaches a `service` label per scrape job so
  per-service dashboards and correlation queries don't depend on trusting resource-attribute
  metrics like `target_info`. Grafana reads from Prometheus (metrics) and Jaeger (traces) as
  provisioned datasources, with one starter dashboard (request rate, p95 latency, error rate,
  active requests, all broken out by service) provisioned automatically.
- **Logs** stay on stdout as structured JSON; `docker compose logs` (or a terminal, when run
  locally with uvicorn) is the inspection path. No log aggregation backend (e.g. Loki) was
  added — stdout plus `trace_id` correlation is sufficient at this scale and avoids adding
  infrastructure that doesn't yet solve a concrete problem.

## Fault-injection boundary

A fault is injected *into a specific service instance* via an explicit control (e.g. an admin
endpoint or environment toggle read at startup) that makes that service add latency, return
errors, or hold resources for a bounded window. Concrete mechanism is decided in the
fault-injection milestone; the constraint that carries forward is:

- Faults are applied at the service boundary, not fabricated in telemetry or injected into the
  correlation/LLM layers.
- Every injected fault is itself observable (it should show up in telemetry like a real
  problem would) — the point is to produce a realistic incident, not to fake the evidence.

## Deterministic incident-correlation layer

Consumes telemetry (traces, metrics, logs) already collected — never talks to services
directly. Its job:

1. Detect an anomaly (error-rate spike, latency spike, etc.) from metrics/traces.
2. Use trace data to identify which services were on the call path during the anomaly window.
3. Identify the relationships between those services (who calls whom) from the same trace
   data.
4. Emit a structured **incident context**: a bounded, typed record of what was observed —
   affected services, the relationships between them, the relevant time window, and the
   supporting metric/trace/log evidence — not a narrative.

This layer must be fully testable without any LLM: given known telemetry input, it should
deterministically produce the same incident context.

### Implemented so far (`shared/correlation/`)

- **Core (`__init__.py`)** — pure, network-free, typed. `detect_anomalies` flags samples where
  `value > rule.threshold`; the threshold is an explicit injectable `AnomalyRule`, a fixture
  policy rather than a claimed production alerting threshold. `extract_relationships` derives
  caller → callee edges from OpenTelemetry parent/child spans, in the same trace, that cross a
  service boundary (never from service-name matching). `build_incident_context` sets `affected_services` to only
  the services that directly produced anomalies, and keeps only relationships touching one of
  them — exactly one hop, no transitive propagation. `IncidentContext` carries no log evidence
  yet: there is no programmatic log store to source it from. `IncidentContext.metric_coverage`
  (`MetricCoverage`: metric, service, `observed`/`undefined`/`unobserved`) is separate metadata
  and does not affect anomalies or `affected_services`: `observed` means the metric query
  returned a numeric sample for the service in the window (even if under the threshold);
  `undefined` means it returned a series for the service whose value was `NaN` (e.g. 0/0);
  `unobserved` means it returned no series for the service. For both of the latter the service's
  health is unknown. The payload does not say *why*: a `NaN` may be no traffic, and an absent
  series may be no traffic, a down target, stale data or a service outside the query. Those
  causes are not distinguished. An empty list means no coverage was declared, not that everything
  was observed. Absence of an anomaly is never turned into `undefined`/`unobserved`.
  `IncidentContext.unobserved_dependencies` (`UnobservedDependency`: caller, callee, metric,
  `callee_status` `undefined`/`unobserved`) lists each trace relationship whose caller has an
  anomaly on a metric for which the callee's declared coverage is not `observed`. It is derived only
  from an existing caller → callee edge plus that coverage; it states that the callee's health is
  unknown and makes no claim about causation. A callee absent from the declared coverage is
  undeclared, not listed. If one query returns several series for a service, any numeric sample
  makes it `observed` (the shipped queries aggregate to one series per service). Empty means none identified or no coverage declared.
- **Adapters (`adapters.py`)** — pure translation of decoded backend JSON into core inputs:
  `parse_prometheus_vector` (instant-query vectors; `service` label from the scrape job; NaN
  and label-less series skipped) with `parse_prometheus_undefined_services` (the services whose
  series were skipped as NaN) and `parse_jaeger_traces` (`/api/traces`; service via
  `processID`, parent via `CHILD_OF` reference). No I/O.
- **Fetchers (`fetch.py`)** — thin HTTP layer: `fetch_prometheus_samples` (`/api/v1/query`) and
  `fetch_jaeger_spans` (`/api/traces`, window as epoch microseconds). Each takes an
  `httpx.Client` with the backend `base_url` (injectable, so tests use `httpx.MockTransport`),
  raises on HTTP errors, decodes JSON and hands it to the adapter; no parsing of its own.
  `fetch_prometheus_vector` does one request and returns both the samples and the NaN services.
  Datetime contract: every datetime that becomes a query timestamp (`at`, `start`, `end`, and
  `run_correlation`'s window) must be timezone-aware; naive values raise `ValueError` before any
  request (as does `window_start` after `window_end` in `run_correlation`), because `datetime.timestamp()` would otherwise read them as host-local time. The core
  models themselves do not enforce this (they never convert to epoch).
- **Runner (`runner.py`)** — `run_correlation(prometheus, jaeger, window_start, window_end,
  query, metric_name, rules, trace_service, services=None)` is orchestration only: fetch samples (instant query
  evaluated at `window_end`; the caller's PromQL must cover the window), fetch Jaeger spans for
  `trace_service` over the window, then `detect_anomalies` → `build_incident_context`. The
  optional `services` names the services the query is meant to cover and yields
  `metric_coverage` (omitted → none declared). Clients,
  query, metric name, rules and service are all explicit arguments — no defaults, no claimed
  production policy. Fetch errors propagate. No scheduling, polling, retries or alerting.
- **Live validation (manual, one-off)** — against the Compose stack, real Prometheus and Jaeger
  payloads parsed and `run_correlation` produced the expected `gateway → order → payment`
  relationships. Automated tests still use `httpx.MockTransport`.
- **Queries and captured payloads (`queries.py`, `tests/fixtures/backends/`)** — the evaluation
  PromQL (`mean_latency_query`, `error_ratio_query`; business endpoints only, per `service`) is
  committed. It is *reconstructed* from the evaluation history's prose, since the original scripts
  were not kept; it was run against the live stack, not diffed against the originals. Six real
  responses (Prometheus vectors incl. a `NaN` and an empty one; two Jaeger checkout traces with
  tags stripped) are checked in with provenance in the fixtures README and drive
  `tests/test_backend_payloads.py` offline. Not covered by real payloads: Prometheus error
  responses, Jaeger truncation at `limit`, traces missing a parent span. The `NaN` and empty
  fixtures pin that a returned-`NaN` series reads as `undefined` and an absent series as
  `unobserved`.
- **FastAPI note** — services pass `telemetry={"auto_configure": False}` to `FastAPI()`. Newer
  FastAPI releases auto-configure OTel when `OTEL_EXPORTER_OTLP_ENDPOINT` is set and fail startup
  without the `fastapi[opentelemetry]` extra; the repo does its own explicit OTel setup.
- **Handoff (`handoff.py`)** — the explicit boundary to the investigator:
  `incident_context_to_payload` (pydantic JSON-mode dump → JSON-safe dict, datetimes as ISO
  strings) and `incident_context_from_payload` (validates, raises `pydantic.ValidationError` on
  malformed input). Pure and deterministic; no transport, no interpretation.
- **Not yet built** — scheduled/repeated runs.

## LLM investigation layer

Reads the incident context produced above — not raw telemetry, and not live service state —
and produces a structured hypothesis: likely root cause, confidence, and which specific pieces
of evidence in the incident context support it. The LLM's input is bounded by construction
(it's a fixed-shape record, not an open-ended log dump), which keeps the reasoning step cheap,
reviewable, and decoupled from how large the system's telemetry volume actually is.

**Contract (built, `shared/investigator/`).** `InvestigatorInput` wraps one `IncidentContext`
(built from a handoff payload). The result is a `Hypothesis`: `status`, `origin_service`, `root_cause`, `confidence` (0–1)
and `supporting_evidence`, a non-empty list of `EvidenceRef(kind, index)` pointing at
anomalies, relationships or unobserved dependencies in the input. `status` is `identified`
(`root_cause` names the most likely origin) or `undetermined` (the evidence cannot support
choosing one; `root_cause` states what is established and what is unknown). It defaults to
`identified`. `origin_service` is the machine-readable origin, never parsed from `root_cause`: the
model requires it non-empty when `identified` and `None` when `undetermined`. Whether it names a
service in the input needs the input, so `validate_origin` checks it (any service the context
mentions) next to `validate_evidence`; `Hypothesis` alone cannot. Hypotheses serialized before
`origin_service` existed no longer load as `identified`. `investigate(payload, investigator)` runs
any callable satisfying the `Investigator` protocol, revalidates its result from scratch (a
`Hypothesis` built with `model_construct`/`model_copy` skips field validators, so `revalidate`
re-dumps it) and rejects evidence references and origins absent from the input, for either status
(`ContractViolation`, a `ValueError` subclass). What is *not* checked: that a stated cause is correct,
or that an `undetermined` text names no origin; the schema constrains shape, not diagnosis. A
context with no anomalies, relationships or unobserved dependencies cannot yield a valid
`Hypothesis` (evidence is required), so a no-incident window is handled before the investigator,
in `shared/pipeline.py` (see below), not represented as an answer. The contract is provider-independent and contains no root-cause logic; tests use a
plain test double.

**LLM executor (built, `shared/investigator/llm.py` + `anthropic.py`).** `LLMInvestigator`
implements the protocol as a single bounded inference call: `build_prompt` (pure) renders the
`IncidentContext` with explicit anomaly/relationship/unobserved-dependency indices, evidence-boundary rules and the
caller → callee edge semantics (a callee can contribute to its callers' latency; never reversed),
the `metric_coverage` statuses and `unobserved_dependencies` (unknown health, not a causal claim),
and when to answer `undetermined` instead of naming an origin; an
injected `CompleteFn(prompt, json_schema) -> raw JSON text` does the transport; the reply is parsed
with `Hypothesis.model_validate_json` and checked with `validate_evidence`. The schema sent to
the provider marks `status` and `origin_service` required (null when undetermined), and a reply that omits it raises (it would otherwise default
to a confident `identified`). Invalid output raises —
no repair, clamping, retries, or fallback hypothesis. No tools, agent loop, memory, or state.
Two providers ship, each a `CompleteFn` called via `httpx` (an existing dependency — no SDKs).
Anthropic's Messages API with native JSON-schema output (`anthropic.py`): `ANTHROPIC_API_KEY`,
`ANTHROPIC_MODEL` (required), `ANTHROPIC_BASE_URL` (optional). OpenAI's Responses API with strict
`json_schema` text format (`openai.py`): `OPENAI_API_KEY`, `OPENAI_MODEL` (required),
`OPENAI_BASE_URL` (optional). The OpenAI request is the one used by the Evaluation 7 one-off script:
`model`, `instructions`, `input`, `max_output_tokens` 4000, `store: false` and the strict schema
(every object closed, all properties required, bound keywords dropped because `Hypothesis`
enforces them locally); no temperature, `top_p` or reasoning setting is sent, so provider defaults
apply. Its refusals, `incomplete` responses (`max_output_tokens` is a `token_limit`), failed
responses and empty replies raise `ProviderError`; HTTP errors propagate as `httpx` errors with the
status only. Usage is `input_tokens`, `output_tokens`, `cached_input_tokens` and `reasoning_tokens`
(part of the output, billed as output), `None` when absent, and the response's model id is
reported through an optional hook. Another provider means writing another `CompleteFn`.
Tests use a fake `CompleteFn` and `httpx.MockTransport`; no live calls. One-off live evaluation results are recorded in
[investigator-evaluation-history.md](investigator-evaluation-history.md) (observations, not a benchmark); the procedure for the live evaluation of the `undetermined` status (run once per case, Evaluation 7) is [manual-evaluation.md](manual-evaluation.md). Manual use:
`anthropic_investigator_from_env()` / `openai_investigator_from_env()` return an `Investigator` to
pass to `investigate(payload, ...)`.

The request (`POST /v1/messages`, `x-api-key` + `anthropic-version: 2023-06-01`, `max_tokens`,
`output_config.format` of type `json_schema`, no beta header) and response handling (text blocks;
`refusal`/`max_tokens` stop reasons rejected) were checked against the current Anthropic structured
outputs docs; the schema sent drops keywords the API does not accept, which `Hypothesis` enforces
locally. The Anthropic transport has not been run live. `MAX_TOKENS` is 1024: a model that spends output tokens on
thinking could hit `max_tokens`, which surfaces as `ProviderError` rather than a partial answer.

**Deterministic investigator (built, `shared/investigator/deterministic.py`).** `DeterministicInvestigator`
implements the same `Investigator` protocol with explicit rules, frozen as `chain-v1` before any
comparison with the LLM and not to be tuned to model outputs (a change gets a new version). It
knows only gateway → order → payment. It returns `undetermined` (citing every available evidence
item) for: missing relationships, any anomalous service with an unknown callee (undefined,
unobserved or undeclared coverage is never health), several or no unique candidate origins,
out-of-chain services or edges, and contradictory contexts; it raises `ValueError` when the context
holds no evidence at all. It names an origin only for a unique anomalous service with no anomalous
or unknown callee, with every other anomalous service upstream of it. Limitations: a leaf is
credited with its latency by topology and an upstream co-fault is not excluded; `confidence` is a
fixed placeholder. The rules and their reasoning are in the module docstring.

**Offline evaluation (built, `shared/evaluation/`).** Pure scoring, no model calls or I/O.
`ScenarioExpectation` (kept apart from the frozen investigator payloads and never passed to an
investigator) separates the injected cause from the status/origin the evidence justifies.
`run_scenario`/`score_hypothesis` revalidate the result and classify it as correct
identification, false attribution, unsupported attribution (identified where undetermined was
expected, even if it matches the injection), appropriate abstention, over-abstention or contract
failure, and report evidence validity and, given a gold set, precision/recall. A scenario can
register `expected_status="unscored"` (no defensible correctness label): the result is still
validated and its evidence reported, but it is never counted as correct, incorrect or an
abstention. `summarize` only counts outcomes: with
a handful of scenarios no rate is meaningful, and confidence is not scored. Only
`ValidationError` and `ContractViolation` become `contract_failure`. A provider-side failure is a
separate, non-scored `provider_failure` event (`failure_category`, a short redacted
`failure_detail`): `ProviderError` (now defined in `llm.py`, provider-independent, with category
`refusal`, `token_limit`, `empty_response` or `other`; the shipped adapters raise it for refusal,
token limits and an empty reply) and `httpx` errors (`http_status` with the code only, `timeout`,
`network`). It is never turned into an `undetermined` hypothesis, never counted as correct,
incorrect, abstention or contract failure, never retried, and the next scenario still runs. The
detail never holds a body, header or URL, and key-shaped text is redacted. Anything else (plain
`ValueError`, `KeyError`, `TypeError`, a bad URL) is a programming error and propagates. A custom
adapter must raise `ProviderError` for refusals and similar replies; the shipped Anthropic and
OpenAI adapters do.

**Experiment runner (built; run once live with OpenAI in Evaluation 8; `shared/evaluation/runner.py`,
`scripts/run_experiment.py`).** `check_registration` loads a capture folder and refuses anything
that is not frozen, verified (everything `verify_manifest` re-derives, including the manifest
digest), non-smoke, registered for a shipped adapter (Anthropic or OpenAI, `runner.PROVIDERS`) with a
model, made from the code
checked out now (clean tree outside `captures/`, and `code_unchanged_since` the captured commit),
or whose ambient model/base-URL variables for that provider (`ANTHROPIC_MODEL`/`ANTHROPIC_BASE_URL`
or `OPENAI_MODEL`/`OPENAI_BASE_URL`) disagree with the registration (an error,
never a substitution). `run_experiment` runs, per scenario in manifest order, the deterministic
baseline once and the LLM investigator `llm_repeats` times (5; configurable for tests) on the same
`InvestigatorInput`, one recorded run each, nothing overwritten or dropped. Exactly one provider
request per LLM run: no retry, repair, fallback or second call; repetitions are independent calls
with an identical prompt and the adapter sets no sampling parameter (provider defaults).
`run_config.request_parameters` records this from the implementation, not by assertion: the
shipped adapter is run once against an in-memory recording transport (no network) and the request
body it builds is read. It lists the explicitly sent non-content parameters (`model`,
`max_tokens`, the `json_schema` output format), the sampling parameters checked for (`temperature`,
`top_p`, `top_k`), which of those were explicitly sent (none today) and which therefore use the
provider's defaults. The default values themselves are not recorded (not known to the runner).
Each LLM run carries `request_parameters_sha256` of that record. Provider
failures are recorded as non-scored events and the run continues; programming or configuration
errors propagate and stop it, with the runs so far already in `runs.jsonl` (no `results.json` means
the run did not finish). Before every request an `LLMProbe` checks that the rendered prompt and
response schema about to be sent hash to the registered values (`IntegrityError` otherwise). Each
run records outcome, `scored`, the hypothesis fields, evidence validity/precision/recall, the
expectation, contract error, provider failure category/detail, `elapsed_seconds`
(`perf_counter` around the one investigator call, scoring excluded), `provider_requests`, the actual
rendered-prompt hash, `usage` and `cost_usd`. Usage is whatever the provider reported for that
response (the adapter's optional `on_usage` hook passes only the whitelisted counters
`input_tokens`, `output_tokens` and the two cache counters, also for a refused or truncated reply);
absent or malformed usage is `null` with `available: false`, never 0 or an estimate. Cost is
computed only from operator-supplied `--price-input-per-mtok`/`--price-output-per-mtok` and
available usage, and is `null` otherwise or when cache tokens are non-zero. `results.json`
(canonical, sorted keys) holds the registration (manifest digest, registered hashes, captured
commit, code revision, per-scenario payload and prompt hashes), the run configuration and request
semantics, every run and a counts-only summary (`scored_runs` excludes `unscored`,
`contract_failure` and `provider_failure`; no rates). No credentials, headers or response bodies
are written. Offline tests use mocked transports and a throwaway Git repository; the two live runs (Evaluations 8 and 9,
`gpt-5.6-luna`, 30 runs, no provider failure) are recorded in
[investigator-evaluation-history.md](investigator-evaluation-history.md).

**Scenarios and capture (run live: Evaluation 8 for S1-S3, Evaluation 9 for S4-S5; `shared/evaluation/scenarios.py`,
`scripts/capture_payment_latency.py`).** Three scenarios come from one payment-latency capture:
S1 full telemetry (correctness `unscored`: no span durations or self-time, and a leaf does not
establish causal origin), S2 the same window with the PromQL restricted to gateway and order
(expected `undetermined`, registered only if the captured context has the order -> payment
unobserved dependency) and S3 S1 with its relationships deleted (a controlled ablation, expected
`undetermined` only while several anomalous services remain with no edge). The script records the
raw Prometheus and Jaeger responses unmodified, rebuilds the contexts from that raw text with the
unchanged `run_correlation`, writes label-free payloads under opaque `c-<hash>` ids and a separate
`manifest.json` (injected cause, expected status/origin, gold evidence looked up in the payload,
window, threshold, queries, registration hashes). Registration binds: the system prompt, the
provider-independent response schema, the `chain-v1` version AND the source of `deterministic.py`,
the exact rendered prompt of every payload, and a transport block. Only a shipped adapter's
request configuration (Anthropic or OpenAI: constants, adapted schema, adapter source) is hashed and
verified here, each with its own fingerprint; for any other provider the operator supplies the hash
and the manifest says `verified_by_repo: false` (one provider's hash is never presented as another
adapter's). The provider and model are part of the manifest registration, so a capture is taken
for one provider; the raw responses and payloads themselves name no provider. Each LLM run also
records the model id the provider reported (`response_model`, OpenAI only).
A live capture also needs the operator's saved fault-injection evidence (request body, armed-at
time, successful response, readback); it is validated against the window and its hashes are stored,
but it cannot prove which service accepted the fault, and the manifest says so. S2 must be
comparable to S1 (same window, relationships and thresholds, shared anomaly values within a 1%
relative tolerance, because the two queries are separate requests). `verify` re-derives every
label, flag, kind, gold index, injection record and hash from `raw/` and `evidence/` and compares
them with the manifest, so editing labels or the smoke flag is reported; the smoke flag is derived
from `capture.mode`. `verify --freeze` is the only way `frozen` becomes true and stores a hash of
the manifest (excluding the freeze metadata); it is refused for smoke data, failed verification, a
capture made on a dirty tree, a dirty tree now, code changed since the captured commit, or an
unset model/transport. HEAD itself may differ from the captured commit as long as it descends from
it and only paths under `captures/` changed (`gitstate.code_unchanged_since`), so committing the
capture folder before or after freezing is fine and a frozen manifest stays verifiable and
runnable; any source commit, uncommitted change or untracked source file is not. The committed 2026-09-30 backend fixtures were used only as an offline smoke test;
the Evaluation 7 payloads were never saved and cannot be reconstructed. Synthetic gateway
scenarios are not built (no fault hook there). The `order` service now mounts the same fault
routes as `payment` (`shared/fault_injection`, added for Evaluation 9), so a second scenario family
is a genuine incident: `capture_payment_latency.py capture --fault-service order` builds S4 (full
telemetry, latency fault in order, payment observed and under the threshold: the evidence supports
one origin, so `expected_status="identified"`, origin `order`, the only scenario that expects an
identification) and S5 (the same window with the PromQL restricted to gateway and order, payment
unobserved: `undetermined`). They are one incident with and without payment's telemetry, so the
label difference comes only from the evidence. The `chain-v1` rules, the prompt, the schema and the
scoring are unchanged; a manifest without `capture.family` is a payment capture. Limitation: the S4
label shares its logic with `chain-v1` (a callee measured healthy leaves the caller as origin), so
`chain-v1` is expected to be right by construction; the open question is whether the LLM makes the
call the evidence supports or over-abstains. A co-fault in gateway is not excluded.

**End-to-end entry point (`shared/pipeline.py`).** `correlate_and_investigate(...)` takes the
`run_correlation` inputs plus an `Investigator`, runs `run_correlation`, converts the context with
`incident_context_to_payload`, and returns `investigate(payload, investigator)`, except that a
context with no anomalies never reaches the investigator (no evidence means no valid
`Hypothesis`, and an LLM would be paid to fail). Whether that is health depends on coverage:
`NoIncident(window_start, window_end, metric_coverage)` only when the caller declared `services`
(the services the metric query is meant to cover) and every one was `observed`; otherwise
`NoObservation(..., metric_coverage, reason)`: nothing declared, an empty or NaN result, or any
declared service `undefined`/`unobserved` (fail closed; missing telemetry is never health). The
return type is `Hypothesis | NoIncident | NoObservation`; the only caller in the repo is its test. Orchestration
only; errors from either layer propagate. Tested with a fake `Investigator`.

**Provider status.** The Anthropic transport exists because the first implementation task asked for a
concrete provider when none had been chosen; the OpenAI transport (`openai.py`) was added so the
Evaluation 8 comparison can run the experiment with `gpt-5.6-luna`, the model of Evaluation 7. Neither
is an architectural requirement. Both are unit-tested with mocked transports. The in-repo OpenAI adapter has been run live
(Evaluation 8, `gpt-5.6-luna`, 15 requests, no provider failure); the Anthropic adapter has not. The
provider-independent boundary is `CompleteFn(prompt, json_schema) -> raw JSON text`. Live validation to date is a one-off live smoke test: a temporary, local-only OpenAI `CompleteFn` (Responses API, `gpt-4o-mini`, kept outside the repo) ran the real `LLMInvestigator` once over a real correlation context from the Compose stack; the reply validated into `Hypothesis` with valid evidence references. It proves the plumbing only — the input used a fixture-scale threshold, so the hypothesis is not a meaningful diagnosis. That smoke test used a temporary script, not the in-repo adapter; the Anthropic transport has not been run live.

## Major data flows

1. **Request flow**: client → gateway → order → payment (and back), producing traces/logs/
   metrics at each hop.
2. **Telemetry flow**: each service → OpenTelemetry → Prometheus/trace backend → Grafana (for
   humans) and → correlation engine (for the system itself).
3. **Incident flow**: anomaly detected → correlation engine reads telemetry → incident context
   built → LLM investigator reads incident context → hypothesis produced → (future)
   surfaced on a dashboard.

## Non-goals and complexity constraints

Explicitly out of scope, and not to be added "for realism":

- **Kafka / any message broker** — synchronous HTTP is sufficient at this scale and keeps
  trace-based correlation simple.
- **Kubernetes** — Docker Compose is sufficient for a laptop-scale system; K8s would add
  operational surface area with no corresponding benefit here.
- **Redis / caching layer** — no component has a caching problem to solve yet.
- **Vector databases** — the LLM layer reasons over a small, bounded, structured incident
  context, not a large unstructured corpus requiring retrieval.
- **Complex agent frameworks** — the LLM's job is a single bounded reasoning step over
  structured input, not multi-step autonomous tool use.
- **High availability / multi-region / horizontal scaling** — this is a single-machine
  portfolio system; designing for production scale would add complexity with no audience.

If a future milestone seems to need one of these, the concrete problem it solves must be
written down here before it's added.
