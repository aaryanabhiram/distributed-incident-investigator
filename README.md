# Distributed Incident Investigator

A small checkout system (gateway, order, payment) with real telemetry and fault injection, plus a
harness that compares a rule-based incident investigator with an LLM on the same evidence.

I built it to answer one narrow question: given the same structured evidence about an incident,
does an LLM make the call the evidence supports, or does it guess? The rules and the model both read
a fixed, bounded summary of what Prometheus and Jaeger recorded, never raw telemetry.

## What I found

Two live experiments on this stack, comparing a hand-written rule set (`chain-v1`) with
`gpt-5.6-luna`. Each model scenario was run 5 times; the rules are deterministic, so once.

| Scenario (real fault, real telemetry) | Correct answer | Rules | Model (5 runs) |
|---|---|---|---|
| Latency fault in `order`, payment measured healthy | name `order` | named it | named it 5/5 |
| Same incident, payment's telemetry removed | say "can't tell" | said so | said so 5/5 |
| Latency fault in `payment`, telemetry restricted | say "can't tell" | said so | said so 5/5 |
| Same fault, call relationships deleted | say "can't tell" | said so | said so 5/5 |
| Latency fault in `payment`, full telemetry | no agreed answer | named `payment` | "can't tell" 4/5, `payment` 1/5 |

The model named a service when the evidence supported one and declined when it didn't. There were
no wrong attributions and no failed requests.

This is a small experiment, not a benchmark. Three to five scenarios from two captures cannot
support accuracy claims. The "name `order`" case was labeled using the same reasoning as the rules,
so the rules are right by construction; what the run shows is that the model reached the same call
from the same evidence. The rules and the prompt were also written after I had seen earlier model
outputs on this kind of incident, so the comparison is not blind. Full write-up and limits:
[docs/investigator-evaluation-history.md](docs/investigator-evaluation-history.md) (Evaluations 8
and 9).

## How it works

```
client -> gateway -> order -> payment          (plain HTTP, FastAPI)
              |
   OpenTelemetry: traces -> Jaeger, metrics -> Prometheus
              |
   correlation: find slow services, which service calls which, and which are unmeasured
              |
   incident context  (a small typed record: the only thing investigators see)
        /                         \
  rule-based investigator      LLM investigator (one call, no tools)
        \                         /
   scoring against labels kept in a separate file, applied after the answer
```

- `payment` and `order` can be made slow or failing on demand, so incidents are real, not simulated
  in the data.
- A capture step saves the raw Prometheus and Jaeger responses, builds the evidence, and hashes it.
  The prompt, the response schema, the rule code and the provider settings are hashed too, and the
  run refuses to start if any of them changed after the capture was frozen.
- Investigators may answer "undetermined". A missing measurement is never read as a healthy
  service.
- Labels and the injected cause never reach an investigator; they are used only for scoring.
- Every run is stored, including provider failures, which are kept separate from scores.

Design details are in [docs/architecture.md](docs/architecture.md).

## Run it

Requires Python 3.10+ and Docker.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -e ".[dev]"
pytest                          # 436 offline tests, no network, no API key
```

Start the services and the observability stack:

```bash
docker compose up --build
curl -X POST http://127.0.0.1:8000/checkout -H "Content-Type: application/json" \
  -d '{"item": "widget", "amount": 25.0}'
```

Then look at traces in Jaeger (http://localhost:16686), metrics in Prometheus
(http://localhost:9090) and the provisioned dashboard in Grafana (http://localhost:3000). All ports
bind to `127.0.0.1` only, because the fault endpoints and Grafana's admin are unauthenticated.

Inject a fault into a service, for example 1.5 s of added latency in `order` for two minutes:

```bash
curl -X POST localhost:8001/admin/fault -H "Content-Type: application/json" \
  -d '{"mode": "latency", "duration_seconds": 120, "latency_ms": 1500}'
```

Capturing evidence and running the comparison is a manual, paid-API procedure, described step by
step in [docs/manual-evaluation.md](docs/manual-evaluation.md). Tests and CI never call a model.

## Layout

```
services/   gateway, order, payment (FastAPI)
shared/     telemetry setup, fault injection, correlation, investigators, evaluation
scripts/    capture_payment_latency.py (build evidence), run_experiment.py (run the comparison)
docs/       architecture, evaluation history, manual procedure
tests/      offline tests and real captured backend payloads
```

## Limits

- One three-service chain, latency faults only in the experiments, one model, two captures.
- The rules are written for this exact chain and abstain on anything else.
- Anomaly thresholds are demonstration values, not alerting policy, and logs are not part of the
  evidence.
- The Anthropic adapter exists but has never been run against the real API.
- No dashboard or UI for the investigator's output.
