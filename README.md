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

**Foundation phase.** Only a minimal `gateway` service with a health check exists so far, to
validate the project skeleton, tooling, and test setup. No telemetry, correlation, fault
injection, or LLM layer has been built yet.

## Getting started

```bash
python -m venv .venv
.venv\Scripts\activate       # Windows
pip install -e ".[dev]"
pytest
```

Run the gateway service:

```bash
uvicorn services.gateway.main:app --reload
```

Then check [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health).

## Planned milestones

1. **Foundation** (this phase) — repo structure, tooling, docs, one health-checkable service.
2. **Multi-service system** — add 2-3 more services with real inter-service HTTP calls under
   Docker Compose.
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
