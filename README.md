# Distributed Incident Investigator

A small, observable distributed system built to practice and demonstrate a specific skill:
turning raw telemetry from a multi-service system into a structured, evidence-backed
incident hypothesis — first deterministically, then with an LLM reasoning over the
deterministic system's output.

This is a portfolio engineering project. It favors a design that one developer can fully
explain in an interview over one that looks impressive on paper.

## What it does (end state)

- Several simple services call each other over HTTP, producing realistic multi-service
  traffic.
- Each service emits logs, metrics, and traces via OpenTelemetry.
- Controlled faults (latency, errors, resource pressure) can be injected into a service on
  demand.
- A deterministic correlation layer watches telemetry, detects anomalies, and identifies
  which services and relationships are implicated in an incident — no LLM involved at this
  stage.
- That correlation output is packaged into a bounded, structured "incident context."
- An LLM investigator reads that incident context (not raw telemetry) and produces a
  structured root-cause hypothesis with supporting evidence, citing what it based the
  hypothesis on.

The dividing line is deliberate: **detection and correlation are deterministic and testable
without an LLM. The LLM only reasons over evidence the deterministic system already
produced.**

## Architecture at a glance

```
 client
   │
   ▼
 gateway service ──► downstream service(s)
   │                        │
   └───────────┬────────────┘
               ▼
     telemetry (logs/metrics/traces)
               │
               ▼
   deterministic correlation engine
               │
               ▼
      structured incident context
               │
               ▼
        LLM investigator (future)
               │
               ▼
   root-cause hypothesis + evidence
```

See [docs/architecture.md](docs/architecture.md) for the full design, including system
boundaries, planned services, telemetry flow, the fault-injection boundary, and explicit
non-goals.

## Technology

- **FastAPI** — service framework
- **PostgreSQL** — persistent state, once a service actually needs it
- **Docker Compose** — local orchestration
- **OpenTelemetry** — traces, logs, and metrics instrumentation
- **Prometheus** — metrics storage
- **Grafana** — visualization

No Kafka, Kubernetes, Redis, vector databases, or agent frameworks — see
[docs/architecture.md](docs/architecture.md#non-goals-and-complexity-constraints) for why.

## Repository layout

```
services/       Independently runnable FastAPI services
shared/         Shared library code (telemetry setup, correlation, fault hooks)
docs/           Architecture and design documentation
tests/          Tests
```

## Status

**Multi-service substrate.** Three real services — `gateway`, `order`, and `payment` — talk to
each other over plain HTTP: `gateway → order → payment`. Each is independently runnable and
health-checkable, and the whole chain runs together under Docker Compose. No telemetry,
fault injection, correlation, or LLM layer has been built yet.

## Getting started

```bash
python -m venv .venv
.venv\Scripts\activate       # Windows
pip install -e ".[dev]"
pytest
```

### Running the services locally (outside Docker)

Each service is independently runnable with uvicorn. Start them in three terminals, innermost
first, pointing each caller at the next service's URL via environment variable:

```bash
uvicorn services.payment.main:app --port 8002

PAYMENT_SERVICE_URL=http://127.0.0.1:8002 uvicorn services.order.main:app --port 8001

ORDER_SERVICE_URL=http://127.0.0.1:8001 uvicorn services.gateway.main:app --port 8000
```

Then exercise the full chain:

```bash
curl -X POST http://127.0.0.1:8000/checkout \
  -H "Content-Type: application/json" \
  -d '{"item": "widget", "amount": 25.0}'
```

Each service also exposes `GET /health`, e.g.
[http://127.0.0.1:8000/health](http://127.0.0.1:8000/health).

### Running with Docker Compose

```bash
docker compose up --build
```

This builds and runs `gateway` (port 8000), `order` (port 8001), and `payment` (port 8002),
wired together via compose service names, with health checks gating startup order.

## Request flow

```
client
  │  POST /checkout {item, amount}
  ▼
gateway
  │  POST /orders {item, amount}
  ▼
order            (generates order_id)
  │  POST /charge {order_id, amount}
  ▼
payment          (approves if amount > 0, else declines — deterministic, no randomness)
```

The response (order id, item, amount, payment status) flows back up through order and gateway
to the client. If a downstream service is unreachable or errors, the caller returns `502` with
a message identifying which downstream call failed, so the failure is visible at every hop
rather than swallowed.

## Planned milestones

1. **Foundation** (done) — repo structure, tooling, docs, one health-checkable service.
2. **Multi-service system** (this phase) — `gateway`, `order`, and `payment` with real
   inter-service HTTP calls, running independently or together under Docker Compose.
3. **Telemetry** — instrument all services with OpenTelemetry (traces, metrics, logs);
   Prometheus + Grafana wired up.
4. **Fault injection** — a controlled, explicit boundary for injecting latency/errors into a
   service.
5. **Deterministic correlation** — analyze telemetry to identify affected services and
   relationships during an incident; build the structured incident context.
6. **LLM investigator** — LLM reasons over the incident context to produce a root-cause
   hypothesis with cited evidence.
7. **Dashboard** — visualize services, incidents, and hypotheses.

Each milestone is implemented and reviewed on its own; later milestones are not started early.
