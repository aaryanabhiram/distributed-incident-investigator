# Investigator evaluation history

One-off live observations of the LLM investigator (`shared/investigator/llm.py`) against real
incidents produced by the local stack. They are **experimental observations, not production
metrics, not a benchmark, and not validated generalized RCA performance**. Each row is one
independent run; nothing here is statistically meaningful. Values below are exactly as returned.
Later evaluations are appended; earlier entries are never rewritten.

The model call was made by a throwaway script outside the repository (no provider module,
dependency, or configuration exists in the repo for OpenAI). Correlation used the unchanged
`run_correlation`; thresholds are demonstration values, not alerting policy.

Model output and our grounding assessment are kept separate in each entry.

## Evaluation 1 — latency incident, gpt-4o-mini, before the relationship-semantics prompt change

- Scenario: payment-service latency fault, 1500 ms (12 checkouts, all 200, ~1.53 s end to end)
- Signal: mean request latency (`http_server_duration_milliseconds`, business endpoints), test
  threshold 500 ms
- Affected services: gateway, order, payment — anomalies: 3 — relationships: 2
  (gateway → order, order → payment)
- **Model output**
  - root_cause: "Excessive latency in the gateway service is causing a cascading effect on the
    order and payment services, leading to their elevated latency as well."
  - confidence: 0.90
  - evidence: anomaly[0], anomaly[1], anomaly[2], relationship[0], relationship[1]
- **Assessment:** causal direction reversed; unsupported gateway-as-origin claim; confidence too
  high.

## Evaluation 2 — same latency incident, gpt-4o-mini, after the relationship-semantics prompt change

- Scenario: same payment-service latency fault
- Affected services: gateway, order, payment — anomalies: 3 — relationships: 2
- **Model output**
  - root_cause: "The high latency in the gateway service is causing increased response times in
    both the order and payment services."
  - confidence: 0.90
  - evidence: anomaly[0], anomaly[1], relationship[0]
- **Assessment:** still reversed caller/callee direction; the prompt clarification alone did not
  correct the failure.

## Evaluation 3 — same latency incident, gpt-5.6-luna, same prompt as Evaluation 2

- Scenario: same payment-service latency fault
- Affected services: gateway, order, payment — anomalies: 3 — relationships: 2
- **Model output**
  - root_cause: "Elevated latency in the payment service is the most likely initiating fault,
    propagating through order to gateway via the observed synchronous call chain."
  - confidence: 0.82
  - evidence: anomaly[2], relationship[1], anomaly[1], relationship[0], anomaly[0]
- **Assessment:** causal direction correct; evidence use correct; "most likely initiating fault"
  plausible but not proven; confidence somewhat high but better calibrated than the 0.90
  gpt-4o-mini results.

## Evaluation 4 — payment error-status incident, gpt-5.6-luna

- Scenario: payment error fault (503 at payment, propagated as 502 upstream; 10 checkouts, all
  502)
- Signal: 5xx share of business-endpoint requests per service (`http_server_error_ratio`, from
  the `http_server_duration_milliseconds_count` counter), test threshold 0.5. A first attempt
  returned NaN because the 5xx counter series was born already at 10 and `increase()` had no
  earlier sample; the same fault was re-run once after the series existed.
- Affected services: gateway, order, payment — anomalies: 3 (all 1.0) — relationships: 2
- **Model output**
  - root_cause: "A failure in the payment service is the most likely root cause, propagating
    errors through order to gateway along the observed synchronous call chain."
  - confidence: 0.72
  - evidence: anomaly[2], relationship[1], anomaly[1], relationship[0], anomaly[0]
- **Assessment:** causal direction correct; conclusion plausible and appropriately hedged; no
  unsupported mechanism claims; confidence proportionate to the flatter evidence.

## Evaluation 5 — ambiguous partial-observability latency incident, gpt-5.6-luna

- Scenario: replay of the real payment-latency incident with the metric query restricted to
  gateway and order (`service=~"gateway|order"`), while the trace still contains order → payment.
  The ambiguity is produced by restricting what is observed, not by a second fault (only payment
  has fault-injection routes).
- Affected services: gateway, order — anomalies: 2 (gateway 1515.9 ms, order 1509.1 ms) —
  relationships: 2 (gateway → order, order → payment)
- **Model output**
  - root_cause: "Elevated latency in the order service is the most likely source of the incident,
    propagating to gateway through the gateway-to-order request path."
  - confidence: 0.78
  - evidence: anomaly[1], relationship[0]
- **Assessment:** failed the ambiguity check.
  - Supported by evidence: gateway and order are slow, and gateway calls order.
  - Plausible but not proven: order as origin (order is the deepest *affected* service in the
    context, but its callee payment is unobserved and could equally be the origin).
  - Not established: that the origin is unique. The answer never mentions payment, ignores
    relationship[1] (order → payment), and silently treats the absence of a payment anomaly as
    absence of a payment problem. It gives no sign of recognizing that the evidence cannot
    identify a unique origin. Confidence (0.78) is barely lower than Evaluation 3 (0.82) although
    the evidence is strictly weaker.
  - Representation note: `IncidentContext` cannot distinguish "measured healthy" from "not
    measured", so the model has no way to know payment is unobserved except by noticing that an
    edge points to a service with no anomaly. Recorded as a finding; no change was made.

## Evaluation 6 — same ambiguous incident as Evaluation 5, with explicit metric coverage, gpt-5.6-luna

- Scenario: identical to Evaluation 5 (real payment-latency incident replayed with the metric
  query restricted to gateway and order; same threshold, relationships, model and provider
  mechanism). The only deliberate change: `IncidentContext.metric_coverage` now states
  gateway = observed, order = observed, payment = unobserved, and the prompt defines
  observed/unobserved (unobserved is not healthy).
- Affected services: gateway, order — anomalies: 2 (gateway 1515.9 ms, order 1509.1 ms) —
  relationships: 2 (gateway → order, order → payment)
- **Model output**
  - root_cause: "Elevated latency in the unobserved payment dependency is the most likely source
    of the latency propagated through order to gateway, although payment telemetry is unavailable
    to confirm it."
  - confidence: 0.58
  - evidence: anomaly[0], anomaly[1], relationship[0], relationship[1]
- **Assessment:** improved, but only partly resolved the ambiguity.
  - Supported by evidence: gateway and order are slow; gateway calls order and order calls
    payment; payment has no telemetry in the context.
  - Improved versus Evaluation 5: it recognizes payment as unobserved, does not treat the missing
    payment anomaly as health, cites the order → payment edge (relationship[1]), states that
    payment telemetry cannot confirm the claim, invents no mechanism, and confidence fell from
    0.78 to 0.58.
  - Plausible but not proven: payment as the source. It is consistent with the data, but order's
    own work is equally consistent and the answer does not name it as a competing origin or say
    the origin cannot be uniquely identified.
  - Not established: that payment is *elevated* (stated as a fact about an unobserved service,
    although hedged afterwards) and that payment is more likely than order.
  - Caveat: one run; the change also moved the model's choice from order to payment, so part of
    the effect may be the unobserved-service label drawing attention rather than better
    reasoning about competing explanations.

## Observations across the six runs (not conclusions)

- Direction handling depended on the model, not the relationship-semantics sentence: gpt-4o-mini
  reversed it before and after; gpt-5.6-luna got it right in all three of its runs.
- gpt-5.6-luna's confidence moved 0.82 → 0.72 with weaker signal (latency gradient → flat error
  ratio) but did not drop on the partial-observability case (0.78).
- Representing coverage explicitly (Evaluation 6) changed the answer: the unobserved service was
  acknowledged and confidence dropped 0.78 → 0.58, but the model still selected a single origin.
- The evaluated incidents were single-fault chains from a single stack; none is a benchmark.

## Evaluation 7 — full and partial telemetry, `undetermined` status, gpt-5.6-luna

First live run of the Milestone 5 schema and prompt (`status`, `unobserved_dependency` evidence).
Run on 2026-09-30. Run with the one-off OpenAI script from
[manual-evaluation.md](manual-evaluation.md) (outside the repository), model `gpt-5.6-luna`
(present in the account's `/v1/models` response). Scenario: payment latency fault injected,
checkout traffic sent, both cases built from one time window and run through the unchanged
`LLMInvestigator`. Both preflights passed; both cases produced model responses. The fault was
cleared afterwards. Anomaly values were not printed and are not recorded here.

### Observed results

| | full (payment observed) | partial (payment unobserved) |
|---|---|---|
| Relationships | gateway → order, order → payment | gateway → order, order → payment |
| `unobserved_dependencies` | none | order → payment, `http_server_duration_milliseconds`, `unobserved` |
| `status` | `undetermined` | `undetermined` |
| `confidence` | 0.95 | 0.95 |
| Evidence | anomaly[0], [1], [2]; relationship[0], [1] | anomaly[0], [1]; relationship[1]; unobserved_dependency[0] |

- **full root_cause:** "Gateway, order, and payment all show elevated server duration, with
  gateway invoking order and order invoking payment. The evidence does not distinguish whether the
  latency originates in payment, order, gateway, or a shared factor; no service can be established
  as the root cause."
- **partial root_cause:** "Gateway and order have elevated HTTP server duration, and order invokes
  payment whose HTTP server duration is unobserved. The evidence cannot distinguish whether the
  latency originates in gateway, order, or the unknown payment dependency."
- Both replies parsed into `Hypothesis` and passed `validate_evidence`; the provider accepted the
  schema sent by the script (resolving that assumption for this model).

### Assessment (interpretation)

- **Full:** `undetermined` is defensible. The context holds per-service mean-latency anomalies,
  caller → callee edges and coverage, with no span durations, self-time or error data, so a
  payment origin, an order or gateway origin and a shared factor are all consistent with it. The
  earlier `identified` answers (Evaluations 1–4) were produced under a prompt without this option.
  Direction was read correctly. No origin was named.
- **Partial:** treated missing payment telemetry as unknown, not healthy; cited the
  `unobserved_dependency`; claimed no unique origin. Minor omission: it did not cite
  relationship[0] (gateway → order) although it discussed latency reaching gateway. Its indices
  are valid and relevant.
- **Confidence:** model-reported only. `Hypothesis.confidence` is bounded to 0–1; no validator or
  prompt rule ties it to telemetry completeness, and the schema defines it as confidence in the
  *stated conclusion, whichever status*. 0.95 on "this cannot be determined" is coherent under that
  definition. Identical values are not a verified calibration defect, and one run per case cannot
  show whether the model would vary it. The "lower confidence than full" check in the manual
  evaluation was written as if confidence meant confidence in an origin; it has been reworded.
- **No code defect was identified.** No change was made to code, prompt or schema.
- **Not established:** general diagnostic accuracy, calibrated confidence, production readiness,
  or any advantage over a deterministic baseline. Each case is a single run on a single-fault
  chain from one stack.

## Planned next experiment (superseded by Evaluation 8 below; kept as written)

Status update: the deterministic investigator (`shared/investigator/deterministic.py`, frozen rule
set `chain-v1`), the structured `origin_service` field and the offline scoring module
(`shared/evaluation/`) are now built and unit-tested; scenario capture is prepared
(`scripts/capture_payment_latency.py`, S1 correctness unscored, S2 and S3 expected
`undetermined`, tamper-evident verify/freeze, and an experiment runner that records every run, provider failures, timing and provider-reported usage) but nothing has been captured live and no
comparison has been run; the 2026-09-30 backend fixtures are offline smoke data only. The
Evaluation 7 payloads were never saved, so they cannot be re-scored. Evaluations 1-7 predate `origin_service`. Plan: implement a transparent deterministic investigator
behind the existing `Investigator` protocol and compare it with `LLMInvestigator` on *identical*
`IncidentContext` inputs, over controlled fault scenarios whose ground-truth origin is known.
Measures to record per investigator: correct identification, false attribution (naming a wrong
origin), abstention (`undetermined`) and whether it was appropriate, evidence-reference validity
and relevance, runtime, and API cost (deterministic: none). Any claim about relative performance
waits for that comparison; this entry's single runs do not support one.

## Evaluation 8 — deterministic `chain-v1` vs one-call `gpt-5.6-luna` on identical frozen evidence

Run on 2026-10-02. Observations on one capture of one stack, **not a benchmark and not validated
RCA performance**. Raw records: `captures/results/run-1/results.json` and `runs.jsonl`.

### Setup (what was registered and run)

- Capture `captures/payment-latency-1`, frozen, manifest SHA-256
  `7ffeb7070a9c41687a991ff8286a83fa8dabf914a4e971eb25e89e5b57a6cce5`. Made and run from code
  commit `d8e3acfc86d3a2be45f7df2a2f05c26df7892cb2`; the capture and results were committed after
  it (`007e815`, `58564a1`) and only touched `captures/`. `verify` passes.
- Live incident: payment latency fault, 1500 ms for 120 s, armed 07:28:01Z, 12 checkouts; window
  07:23:38Z to 07:28:38Z (5 min), threshold 500 ms (demonstration value). Jaeger returned 100
  traces, which is its cap, but all relationships were present in the captured context. The fault
  evidence is operator-saved; the response carries no service name, so "payment" is declared by
  procedure (the POST went to payment's admin port), not independently proven.
- Scenarios: S1 full telemetry (live; gateway 1517.5, order 1510.6, payment 1502.7 ms; edges
  gateway -> order, order -> payment; correctness **unscored**); S2 the same window with the PromQL
  restricted to gateway and order, so payment is unobserved (live telemetry under a restricted
  query; gateway 1517.5, order 1510.6; one unobserved dependency order -> payment; expected
  `undetermined`); S3 S1 with relationships deleted (a controlled ablation, not produced by the
  pipeline; expected `undetermined`).
- Investigators: `chain-v1` once per scenario (no request), and the unchanged `LLMInvestigator`
  five times per scenario, every run recorded separately: 3 + 15 = 18 runs, 15 provider requests,
  no retry. Both received the same `InvestigatorInput`; the labels live only in the manifest.
- Provider: OpenAI Responses API through `shared/investigator/openai.py`, model `gpt-5.6-luna`
  (each response named `gpt-5.6-luna`). Request: `max_output_tokens` 4000, `store` false, strict
  schema; no temperature, `top_p` or reasoning setting was sent, so provider defaults applied (their
  values are not recorded). The rendered prompt and the schema were hash-checked before every
  request. The adapter's live acceptance of the schema was exercised by an operator-run smoke test
  before the capture; the 15 experiment requests then all completed.

### Observed results

| | S1 (unscored) | S2 (expect `undetermined`) | S3 (expect `undetermined`) |
|---|---|---|---|
| `chain-v1` | identified **payment** | `undetermined` (appropriate abstention) | `undetermined` (appropriate abstention) |
| `gpt-5.6-luna`, 5 runs | `undetermined` x4 (confidence 0.92-0.96); identified **payment** x1 (0.72) | `undetermined` x5 (0.93-0.95) | `undetermined` x5 (0.98-0.99) |

- Outcome counts: deterministic 2 appropriate abstentions + 1 unscored; LLM 10 appropriate
  abstentions + 5 unscored. No correct identification, false attribution, unsupported attribution,
  over-abstention, contract failure or provider failure occurred (none was possible to score as
  "identification": no scenario expects `identified`).
- S1, descriptive only: `chain-v1` named the injected service (`matches_injected_cause` true), as
  its rules do for any anomalous leaf with anomalous ancestors. The LLM abstained in 4 of 5 runs and
  named payment in 1. Because S1 has no correctness label, neither is "right" or "wrong" here; the
  rules credit a leaf by topology alone, the LLM's abstentions reflect that the context has no span
  durations or self-time.
- Evidence: every citation was valid in all 18 runs (validity 1.0). Against the gold sets,
  relevance recall was 1.0 everywhere scored; precision was 1.0 on S3 for both, and on S2 0.6 for
  `chain-v1` and for four LLM runs (0.75 for one), because both cite more than the minimal gold set.
- Confidence: `chain-v1` reports a fixed placeholder (0.5). The LLM reported 0.92-0.99 when
  abstaining and 0.72 when it identified. Confidence is not scored and is the model's own number.
- Runtime: `chain-v1` took tens of microseconds in total. Each LLM call took 2.7-5.7 s (55.5 s for
  15). Tokens reported by the provider: 20,340 input, 3,485 output (1,630 of them reasoning
  tokens, counted inside the output). No prices were supplied, so cost is `null`.

### Reading (interpretation)

- On S2 and S3 both investigators abstained every time. That is the expected behaviour, but it is
  weak evidence for the LLM and none against `chain-v1`: the rules abstain on those inputs by
  construction (an unknown callee blocks attribution; a missing edge is an unconfirmed edge).
- The only difference visible in this capture is S1, where the two diverge, and S1 is deliberately
  unscored.
- Nothing here shows that either is better. The scored scenarios test abstention only.

### Disclosure: this is not a blind comparison

- Evaluations 1-7 (2026-09-30) showed `gpt-5.6-luna` outputs on this same incident family before
  the `chain-v1` rules existed in the repository (`deterministic.py` first appears in the evaluation
  runner commit, 2026-10-01). "Frozen before this comparison" is true; "written without having seen
  the LLM's outputs" is not.
- The LLM system prompt was revised after Evaluations 1, 5 and 6, i.e. in response to observed
  failures on this incident family (relationship direction, unobserved services, the `undetermined`
  status). The S2 scenario is the partial-observability case that prompt change targeted.
- The scenario labels were written after those observations. They were registered before this
  capture and run, and neither the rules, the prompt, the scoring nor the labels were changed after
  the results were seen.

### What this result does not establish

General LLM or rule-based root-cause ability; behaviour on other topologies, faults or services;
accuracy or error rates (three scenarios from one capture, five repeats); calibrated confidence;
that the S1 abstentions or the single S1 identification are correct; the effect of the unsent
reasoning or sampling settings; or cost (no prices supplied).
