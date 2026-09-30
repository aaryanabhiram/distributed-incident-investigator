# Manual live evaluation (user-triggered)

Automated tests never call a model and need no credentials. Live evaluation is a separate,
manual step: it spends paid usage and its results are one-off observations, recorded by
appending an entry to [investigator-evaluation-history.md](investigator-evaluation-history.md)
(never rewriting earlier ones).

## Status

**Pending.** The `undetermined` hypothesis status and indexed `unobserved_dependency` evidence
(Milestone 5) have only been exercised with fake `CompleteFn`s. Whether a real model uses them
sensibly has not been run. The results in the history file predate this schema and prompt and
are not comparable to it as-is. The Anthropic transport has also never run live.

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
quoting differs, so those commands need adapting (not provided or tested here). The Python step is
shell-independent.

1. Stack: if the Compose stack is already running, leave it as is and skip this step (re-running
   `up --build` can recreate containers, and Prometheus and Jaeger keep no persistent volumes, so
   recreating them discards their telemetry). Otherwise start it: `docker compose up --build -d`.
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

3. Set your own credentials in your shell (do not put them in files or commit them):
   `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` (e.g. `export ANTHROPIC_API_KEY=...` in Bash).
   `ANTHROPIC_BASE_URL` is optional; if it is set in your environment, confirm it points where you
   intend before running.
4. **Immediately after the traffic finishes**, run from the repo root with the project venv
   (`.venv/Scripts/python.exe` on Windows). Both cases share one time window and one Jaeger
   fetch scope; they differ only in the PromQL service filter. Each case makes one paid model call,
   and only if its preflight passes:

   ```python
   from datetime import datetime, timedelta, timezone

   import httpx

   from shared.correlation import AnomalyRule
   from shared.correlation.handoff import incident_context_to_payload
   from shared.correlation.queries import LATENCY_METRIC, mean_latency_query
   from shared.correlation.runner import run_correlation
   from shared.investigator import investigate
   from shared.investigator.anthropic import anthropic_investigator_from_env

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

   investigator = anthropic_investigator_from_env()  # reads the env vars; makes no request
   end = datetime.now(timezone.utc)  # one window for both cases
   start = end - timedelta(minutes=5)

   with (
       httpx.Client(base_url="http://localhost:9090") as prom,
       httpx.Client(base_url="http://localhost:16686") as jaeger,
   ):
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
   ```

   The model receives the same context object that passed the preflight; correlation is not run a
   second time.
5. Clean up. Clear the fault (it would expire on its own within its 120 s):
   `curl -X DELETE localhost:8002/admin/fault`. Stop the stack only if you started it in step 1:
   `docker compose down` removes the containers and, with them, all Prometheus and Jaeger data. Do
   not run it against a stack you manage yourself unless you intend to lose that telemetry.

## What to look for (observations, not pass/fail)

- **full** (payment observed): does it stay `identified`, with correct caller → callee direction?
- **partial** (payment unobserved, so `unobserved_dependencies` lists order → payment): does it
  return `undetermined`, cite the `unobserved_dependency`, and avoid presenting order or payment
  as the established origin? Is confidence lower than the `full` run?
- Any schema or evidence-validation exception is itself a result worth recording. A preflight
  abort is not a result: fix the setup and rerun.

Record model, date, inputs, raw output and your own assessment as a new "Evaluation 7+" entry.
One run per case is an anecdote, not a benchmark.
