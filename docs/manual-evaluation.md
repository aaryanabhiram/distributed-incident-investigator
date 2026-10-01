# Manual live evaluation (user-triggered)

Automated tests never call a model and need no credentials. Live evaluation is a separate,
manual step: it spends paid usage and its results are one-off observations, recorded by
appending an entry to [investigator-evaluation-history.md](investigator-evaluation-history.md)
(never rewriting earlier ones).

This procedure runs the evaluation with **OpenAI** through a **one-off script kept outside the
repository**. OpenAI is not a supported provider of this project: the production package stays
provider-independent (its only shipped provider is the Anthropic transport), and nothing here adds
an OpenAI module, dependency or configuration to it. The script supplies its own `CompleteFn`, the
existing `CompleteFn(Prompt, json_schema) -> raw JSON text` seam, to the unchanged `LLMInvestigator`.

## Status

**Run once (Evaluation 7).** The `undetermined` hypothesis status and indexed
`unobserved_dependency` evidence (Milestone 5) were exercised live with this script on
`gpt-5.6-luna`; see Evaluation 7 in the history file. Evaluations 1–6 predate this schema and
prompt and are not comparable to it as-is. One run per case is an anecdote. The shipped
Anthropic transport has not made a live request.

## Query provenance

`shared/correlation/queries.py` holds *reconstructed* evaluation queries. The original scripts
were not kept, so these are not the originals (see its docstring and
`tests/fixtures/backends/README.md`). The Evaluation 5/6 restriction below is a textual edit of
the reconstructed query, mirroring that entry's description (`service=~"gateway|order"`); it was
checked to parse and to return only gateway and order against the Compose Prometheus, but it is
still a reconstruction.

## Why there is a preflight

`fetch_jaeger_spans` asks Jaeger for at most 100 traces for `service=gateway` and cannot filter by
operation. The Compose health checks and Prometheus scrapes also create gateway traces
continuously (on one stack inspected, roughly 3 per second), so the newest 100 traces can cover
well under a minute and checkout traces older than that drop out silently. The result is a
context with no relationships and no error. Trace volume varies, so no timing window is a
guarantee. The script below therefore builds the correlation context for each case, prints it, and
**aborts that case before any model call** unless the context has exactly the evidence the case
is meant to test.

## Procedure

Requires a Bash-compatible shell for the traffic and cleanup commands (Git Bash on Windows). In
Windows PowerShell 5.1 `curl` is an alias for `Invoke-WebRequest`, `seq` does not exist and the
quoting differs, so those commands need adapting (not provided or tested here). The Python script
is shell-independent.

1. **Leave your running Compose stack alone. Skip startup and teardown entirely**: do not run
   `docker compose up`, `build`, `restart` or `down`. Those recreate or stop containers, and
   Prometheus and Jaeger keep no persistent volumes, so it would discard the telemetry this
   procedure reads.
2. Inject a payment latency fault and send traffic (12 checkouts at 1500 ms, as in Evaluation 1).
   The fault expires by itself after 120 s; if you retry later, re-inject it:

   ```bash
   curl -X POST localhost:8002/admin/fault -H "Content-Type: application/json" \
     -d '{"mode": "latency", "duration_seconds": 120, "latency_ms": 1500}'
   for i in $(seq 12); do
     curl -s -X POST localhost:8000/checkout -H "Content-Type: application/json" \
       -d '{"item": "widget", "amount": 25.0}' > /dev/null
   done
   ```

3. Set your OpenAI credentials in your shell only (never in a file, never committed, never
   printed; the script reads them and prints neither): `OPENAI_API_KEY` and `OPENAI_MODEL`, e.g.
   `export OPENAI_API_KEY=...` in Bash. These two names are this one-off script's own convention,
   not a repo setting; rename them freely. The script sends the key only to the fixed URL
   `https://api.openai.com/v1/responses` and reads no base-URL variable. Pick a model that
   supports Structured Outputs.
4. Save the script below **outside the repository** (for example in your home folder; the repo
   has no ignored scratch directory, since only `.env` is ignored, so a file saved inside it would
   show up as untracked). It imports `shared` from this repo's editable install, which resolves
   from any directory when run with the project venv. **Immediately after the traffic
   finishes**, run it with the venv interpreter (`.venv/Scripts/python.exe` on Windows):
   `.venv/Scripts/python.exe C:/path/to/oneoff_eval.py`. Both cases share one time window and
   one Jaeger fetch scope; they differ only in the PromQL service filter. Each case makes one
   paid OpenAI request, and only if its preflight passes:

   ```python
   """One-off OpenAI evaluation of the investigator. Keep this file OUTSIDE the repository."""

   import os
   from datetime import datetime, timedelta, timezone
   from typing import Any

   import httpx

   from shared.correlation import AnomalyRule
   from shared.correlation.handoff import incident_context_to_payload
   from shared.correlation.queries import LATENCY_METRIC, mean_latency_query
   from shared.correlation.runner import run_correlation
   from shared.investigator import investigate
   from shared.investigator.llm import CompleteFn, LLMInvestigator, Prompt

   OPENAI_URL = "https://api.openai.com/v1/responses"  # fixed on purpose: no base-URL variable
   MAX_OUTPUT_TOKENS = 4000  # reasoning models also spend output tokens on thinking

   # Keywords dropped from the schema sent to OpenAI. None is needed by the provider: the same
   # constraints are enforced locally by `Hypothesis`, and `default` is meaningless once every
   # field is required. Dropping them avoids depending on which keywords strict mode accepts.
   _DROP = {
       "title",
       "default",
       "minimum",
       "maximum",
       "exclusiveMinimum",
       "exclusiveMaximum",
       "minLength",
       "maxLength",
       "pattern",
       "format",
       "minItems",
       "maxItems",
   }


   def to_openai_schema(schema: Any, root: bool = True) -> Any:
       """Structured-output form: every object closed with all properties required."""
       if isinstance(schema, list):
           return [to_openai_schema(item, root=False) for item in schema]
       if not isinstance(schema, dict):
           return schema
       out = {
           k: to_openai_schema(v, root=False)
           for k, v in schema.items()
           if k not in _DROP and not (root and k == "description")
       }
       if isinstance(out.get("properties"), dict):
           out["type"] = "object"
           out["required"] = list(out["properties"])
           out["additionalProperties"] = False
       return out


   def make_complete(api_key: str, model: str, client: httpx.Client) -> CompleteFn:
       """A `CompleteFn`: exactly one Responses API request per call, no retries, no fallback."""

       def complete(prompt: Prompt, schema: dict[str, Any]) -> str:
           body = {
               "model": model,
               "instructions": prompt.system,
               "input": prompt.user,
               "max_output_tokens": MAX_OUTPUT_TOKENS,
               "store": False,
               "text": {
                   "format": {
                       "type": "json_schema",
                       "name": "hypothesis",
                       "strict": True,
                       "schema": to_openai_schema(schema),
                   }
               },
           }
           response = client.post(
               OPENAI_URL, json=body, headers={"Authorization": f"Bearer {api_key}"}
           )
           if response.status_code in (400, 422):
               # A rejected request or schema: the body says why. The key is redacted before
               # the 500-character cut. Not done for 401/403, whose messages can echo the key.
               body_text = response.text.replace(api_key, "[redacted]")[:500]
               raise RuntimeError(f"OpenAI rejected the request: {body_text}")
           response.raise_for_status()  # its message carries the URL and status, not the key
           data = response.json()
           if data.get("status") != "completed":
               raise RuntimeError(
                   f"OpenAI response not completed: status={data.get('status')!r}, "
                   f"incomplete_details={data.get('incomplete_details')!r}, "
                   f"error={data.get('error')!r}"
               )
           texts = []
           for item in data.get("output", []):
               if item.get("type") != "message":
                   continue  # e.g. reasoning items
               for part in item.get("content", []):
                   if part.get("type") == "refusal":
                       raise RuntimeError(f"OpenAI model refused: {str(part.get('refusal'))[:200]}")
                   if part.get("type") == "output_text":
                       texts.append(part.get("text", ""))
           if not texts:
               raise RuntimeError("OpenAI response contained no output_text")
           return "".join(texts)

       return complete


   SERVICES = ["gateway", "order", "payment"]
   EDGES = {("gateway", "order"), ("order", "payment")}
   FULL = mean_latency_query("5m")
   # Evaluation 5/6 restriction: a textual edit of the reconstructed query.
   PARTIAL = FULL.replace("{http_target", '{service=~"gateway|order",http_target')
   # label: (query, affected services, latency coverage per service, unobserved dependencies)
   CASES = {
       "full": (
           FULL,
           {"gateway", "order", "payment"},
           {"gateway": "observed", "order": "observed", "payment": "observed"},
           set(),
       ),
       "partial": (
           PARTIAL,
           {"gateway", "order"},
           {"gateway": "observed", "order": "observed", "payment": "unobserved"},
           {("order", "payment", LATENCY_METRIC, "unobserved")},
       ),
   }


   def run(prom: httpx.Client, jaeger: httpx.Client, investigator: LLMInvestigator) -> None:
       end = datetime.now(timezone.utc)  # one window for both cases
       start = end - timedelta(minutes=5)
       for label, (query, affected, coverage, dependencies) in CASES.items():
           context = run_correlation(
               prom,
               jaeger,
               start,
               end,
               query,
               LATENCY_METRIC,
               [AnomalyRule(metric_name=LATENCY_METRIC, threshold=500)],  # demo value
               "gateway",
               SERVICES,
           )
           print(label, "relationships:", [(r.caller, r.callee) for r in context.relationships])
           print(label, "unobserved_dependencies:", context.unobserved_dependencies)
           expected = {
               "affected": affected,
               "edges": EDGES,
               "coverage": coverage,
               "dependencies": dependencies,
           }
           found = {
               "affected": set(context.affected_services),
               "edges": {(r.caller, r.callee) for r in context.relationships},
               "coverage": {
                   c.service: c.status
                   for c in context.metric_coverage
                   if c.metric_name == LATENCY_METRIC
               },
               "dependencies": {
                   (d.caller, d.callee, d.metric_name, d.callee_status)
                   for d in context.unobserved_dependencies
               },
           }
           if found != expected:
               print(
                   f"{label}: ABORTED before any model call.\n  expected {expected}\n  found    {found}"
               )
               print("  Likely the 100-trace Jaeger cap missed the checkout traces, the fault")
               print(
                   "  expired, or no traffic ran. Regenerate traffic (re-inject the fault) and rerun."
               )
               continue
           hypothesis = investigate(incident_context_to_payload(context), investigator)
           print(label, hypothesis.model_dump_json(indent=2))


   def main() -> None:
       api_key, model = os.environ.get("OPENAI_API_KEY"), os.environ.get("OPENAI_MODEL")
       if not api_key or not model:
           raise SystemExit("Set OPENAI_API_KEY and OPENAI_MODEL in your shell first.")
       with (
           httpx.Client(timeout=120) as openai,
           httpx.Client(base_url="http://localhost:9090") as prom,
           httpx.Client(base_url="http://localhost:16686") as jaeger,
       ):
           run(prom, jaeger, LLMInvestigator(make_complete(api_key, model, openai)))


   if __name__ == "__main__":
       main()
   ```

   The model receives the same context object that passed the preflight; correlation is not run a
   second time. Refusals, incomplete or failed responses, HTTP errors (a 400/422 rejection shows
   the API's message; 401/403 show only the status) and model replies without the hypothesis
   `status` field raise and stop the run; nothing is retried and nothing is converted into a
   hypothesis.
5. Clean up: clear the injected fault (it would expire on its own within its 120 s):
   `curl -X DELETE localhost:8002/admin/fault`. Do not stop or restart the stack. Delete your
   copy of the script if you do not want to keep it.

## OpenAI API facts the script relies on

*Documented* (OpenAI's Structured Outputs guide and Responses API reference, as retrieved when
this was written):
- `POST` to the Responses API with `model`, `instructions`, `input`, `max_output_tokens` and
  `store`; structured output via `text.format = {type: "json_schema", name, strict: true, schema}`.
- With `strict`, the root must be an object, every property must be listed in `required` and
  objects need `additionalProperties: false`.
- Output is an `output` array of items; message items hold `content` parts of type `output_text`
  or `refusal`; `status` is `completed` or `incomplete`, with `incomplete_details.reason` (for
  example `max_output_tokens`); a failed request carries an `error` object.

*Assumed, not confirmed*:
- The full URL `https://api.openai.com/v1/responses` and `Authorization: Bearer` auth are standard
  conventions that I did not re-read.
- Which other JSON Schema keywords strict mode accepts: the guide's "supported schemas" section
  could not be retrieved. So the script sends a conservative schema: it drops `default`, `title`,
  numeric/string/array bound keywords, `pattern`, `format` and the root description (the same
  constraints are enforced locally by `Hypothesis`), keeps `enum` and `$ref`/`$defs`, and closes
  every object with all properties required.
- That your chosen model supports Structured Outputs, and that this schema is accepted on the first
  live request. A schema rejection would surface as an HTTP error, and is a result to record.
- `MAX_OUTPUT_TOKENS = 4000` may be too low for a reasoning-heavy model; an incomplete response
  raises rather than yielding a partial answer.

## What to look for (observations, not pass/fail)

- **full** (payment observed): is the caller → callee direction correct, and is the status
  (`identified` or `undetermined`) supported by what the context contains? Elevated latency on
  every service in a chain does not by itself establish an origin, so `undetermined` can be
  correct (Evaluation 7).
- **partial** (payment unobserved, so `unobserved_dependencies` lists order → payment): does it
  return `undetermined`, cite the `unobserved_dependency`, treat payment as unknown rather than
  healthy, and avoid presenting order or payment as the established origin?
- **confidence**: record it, but note it is the model's confidence in its *stated conclusion*
  (including an `undetermined` one), not in an origin, and nothing ties it to telemetry
  completeness. Equal values across cases are an observation, not a failure; lower confidence in
  `partial` is not an expected outcome of the current schema.
- Any schema or evidence-validation exception is itself a result worth recording. A preflight
  abort is not a result: fix the setup and rerun.

Record model, date, inputs, raw output and your own assessment as a new "Evaluation 7+" entry.
One run per case is an anecdote, not a benchmark.

## Capturing scenarios for the investigator comparison (S1-S3)

Prepared, not yet run. This captures a *new* payment-latency incident (the Evaluation 7 payloads
were never saved, and the committed 2026-09-30 backend fixtures are offline smoke data only: they
are not Evaluation 7 and not a live capture). It calls no model. It only reads the stack: leave
Compose running, and do not run `docker compose up`, `build`, `restart` or `down` (Prometheus and
Jaeger keep no volume). Git Bash on Windows; the Python commands use the project venv.

**Before you start.** A manifest can only be frozen from a clean, committed tree: the capture
records the commit and whether the tree was dirty, and `--freeze` refuses a capture made on
uncommitted changes. The code under test (investigators, scorer, `chain-v1`, prompt, scripts)
therefore has to be committed first. That is your decision to make; nothing here commits for you.

1. Check the stack: `curl -s localhost:8000/health`, `localhost:8001/health`,
   `localhost:8002/health` (gateway, order, payment) and `curl -s localhost:9090/-/ready`.
2. Arm the fault **and keep the evidence**. The request body, the time just before the POST, the
   successful response and a readback are what the capture later binds the injected cause to.
   `curl -f` makes a rejected request a failure: if the POST fails, stop, clear the fault (step 6)
   and start again with a fresh evidence folder.

   ```bash
   mkdir -p captures/evidence-1
   cat > captures/evidence-1/fault-request.json <<'EOF'
   {"mode": "latency", "duration_seconds": 120, "latency_ms": 1500}
   EOF
   date -u +%Y-%m-%dT%H:%M:%SZ > captures/evidence-1/fault-armed-at.txt
   curl -sS -f -X POST localhost:8002/admin/fault -H "Content-Type: application/json" \
     -d @captures/evidence-1/fault-request.json -o captures/evidence-1/fault-response.json
   curl -sS -f localhost:8002/admin/fault -o captures/evidence-1/fault-readback-before-traffic.json
   ```

3. Send traffic and wait one or two scrape intervals:

   ```bash
   for i in $(seq 12); do
     curl -s -X POST localhost:8000/checkout -H "Content-Type: application/json" \
       -d '{"item": "widget", "amount": 25.0}' > /dev/null
   done
   sleep 15
   ```

4. **Immediately** (within about a minute: Jaeger returns at most 100 gateway traces and health
   checks keep adding them) capture into a NEW directory, naming the model and provider the
   comparison will use (`anthropic` is the shipped adapter, whose transport hash is verified here;
   for another adapter pass `--transport-config-sha256` with the hash of that adapter's request and
   schema configuration, which this repository cannot verify):

   ```bash
   .venv/Scripts/python.exe scripts/capture_payment_latency.py capture --out captures/payment-latency-1 \
     --injection-evidence captures/evidence-1 --model <model> --provider anthropic
   ```

   Before any request the script checks that the evidence shows an accepted latency fault above
   the anomaly threshold armed while the window was open; otherwise it stops and captures nothing.
   It then makes three GETs and either writes `raw/`, `evidence/`, `payloads/` and `manifest.json`
   or prints why it aborted. After an abort the directory holds only the raw responses of that
   attempt: never reuse it. The fault lasts 120 s and has probably expired, so re-arm it with new
   evidence (steps 2-3) and capture into another new directory.
5. Verify: `.venv/Scripts/python.exe scripts/capture_payment_latency.py verify captures/payment-latency-1`.
   It re-derives everything from `raw/` and `evidence/`: rebuilds the payloads, recomputes every
   label, flag, kind and gold index, the injection record, the rendered-prompt hashes and the
   registration hashes, and reports any difference (it cannot be fooled by editing the manifest).
6. Clear the fault whenever you are done, or if anything above fails; it is safe at any time:
   `curl -X DELETE localhost:8002/admin/fault`
7. Review `manifest.json` and the payloads by hand, then freeze:
   `.venv/Scripts/python.exe scripts/capture_payment_latency.py verify captures/payment-latency-1 --freeze`.
   Freezing is refused for smoke data, a manifest that fails verification, a capture made on a dirty
   tree, a dirty tree now, code that changed since the captured commit, and an unset model or
   transport. The cleanliness check ignores the capture folder and everything under `captures/`
   (where the saved evidence lives) but counts every other change, including untracked source
   files. Committing the capture folder moves HEAD but not the code, so it may be committed before
   or after freezing; any source commit after the capture makes the capture unusable. A frozen manifest stores a hash of its own content; any later edit is reported by
   `verify`. Do not edit `SYSTEM_PROMPT`, the schema, the adapter or `chain-v1` after capturing:
   their hashes are registered, and `verify` fails if they change.

What the evidence does not prove: the fault response carries no service name, so "the injected
cause is payment" rests on the procedure (the POST goes to payment's admin port). The manifest
says so (`capture.injection.attribution`).

## Running the comparison (after a frozen capture)

Prepared, not yet run, and it makes paid requests: up to scenarios x 5 = 15 requests to the
registered Anthropic model (the deterministic baseline makes none). Only `ANTHROPIC_API_KEY` is read
from the environment. The model and endpoint come from the registration; an `ANTHROPIC_MODEL` or
`ANTHROPIC_BASE_URL` set to anything else makes the runner refuse rather than substitute.

```bash
.venv/Scripts/python.exe scripts/run_experiment.py --capture captures/payment-latency-1 \
  --out captures/results/run-1
```

Optionally add `--price-input-per-mtok <usd> --price-output-per-mtok <usd>` (your own numbers) to
get a cost; without them `cost_usd` is null. `--llm-repeats` defaults to the registered 5.

The runner refuses unless the capture is frozen, verifies it again, and the tree is clean and on
the captured code (committing `captures/` is fine). It writes `runs.jsonl` as each run finishes and
`results.json` only when all runs are done; no `results.json` means the run stopped early (a
configuration or programming error; provider refusals, token limits and network errors are recorded
as `provider_failure` events and the run continues, with no retry). Every run, including each of
the five repetitions and every failure, is a separate record. Token usage is whatever the provider
reported and is `null` when it was not. `run_config.request_parameters` lists what the request
explicitly sends and states that no sampling parameter (`temperature`, `top_p`, `top_k`) is sent,
so the provider's defaults apply; their values are not recorded. The summary has counts only; a handful of scenarios and
repetitions supports no accuracy claim, and the unscored S1 is excluded from every correctness and
abstention denominator.
