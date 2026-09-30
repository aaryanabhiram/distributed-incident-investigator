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

**Telemetry.** Three real services — `gateway`, `order`, and `payment` — talk to each other
over plain HTTP: `gateway → order → payment`. Each is instrumented with OpenTelemetry (traces,
metrics, structured logs) via a shared setup in `shared/telemetry/`, and the whole chain runs
together with a local observability stack (Jaeger, Prometheus, Grafana) under Docker Compose.

**Fault injection.** The `payment` service can be made to add latency or return errors on
demand (`shared/fault_injection/`); injected faults show up in telemetry like real problems.

**Correlation (in progress).** `shared/correlation/` is a network-free deterministic core:
threshold anomaly detection, service relationships derived from cross-service parent/child
spans, and a bounded `IncidentContext`. `shared/correlation/adapters.py` converts Prometheus
instant-query and Jaeger trace JSON into the core's typed inputs, and
`shared/correlation/fetch.py` fetches that JSON over HTTP (`httpx`).
`shared/correlation/runner.py` (`run_correlation`) ties these together for a supplied time
window: fetch → detect → correlate → `IncidentContext`, with the query, rules and clients passed
in explicitly. Unit tests use mocked HTTP; it has also been run once by hand against the live
Compose stack (real Prometheus and Jaeger payloads parsed). `shared/correlation/handoff.py`
converts an `IncidentContext` to/from a JSON-safe dict — the boundary the future investigator
will consume. `shared/investigator/` defines the investigator contract only (`InvestigatorInput`
→ `Hypothesis` with root cause, confidence and evidence references); no LLM or provider is wired
yet.

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
wired together via compose service names, with health checks gating startup order. It also
starts the local observability stack: Jaeger, Prometheus, and Grafana (see below).

## Telemetry and local observability stack

Each service calls `shared/telemetry.setup_telemetry(app, service_name)` at startup, which
configures:

- **Traces** — OpenTelemetry auto-instrumentation of FastAPI and outgoing `httpx` calls,
  exported via OTLP/gRPC to the endpoint in `OTEL_EXPORTER_OTLP_ENDPOINT` (defaults to
  `http://localhost:4317`, i.e. a locally running Jaeger). A single checkout request produces
  one connected trace spanning `gateway → order → payment`.
- **Metrics** — an OpenTelemetry Prometheus reader exposed at `GET /metrics` on each service.
  Includes `http_server_duration_milliseconds` (a histogram, giving request count and latency
  together, labeled by route and status code) and `http_server_active_requests`.
- **Logs** — structured JSON to stdout, one line per log record, including `service`,
  `trace_id`, and `span_id` so a log line can be tied back to the trace and service that
  produced it. Uvicorn's own access/error logs are routed through the same formatter.

Trace/metric export endpoints are read from environment variables at startup
(`OTEL_EXPORTER_OTLP_ENDPOINT`), so the same code runs unchanged locally or in Docker Compose
— only the endpoint differs (compose points it at the `jaeger` service).

### Starting the stack

```bash
docker compose up --build
```

Then exercise the checkout path (see above) and inspect:

- **Jaeger UI** — [http://localhost:16686](http://localhost:16686) — pick service `gateway`,
  operation `POST /checkout`, to see the full cross-service trace.
- **Prometheus** — [http://localhost:9090](http://localhost:9090) — targets page shows all
  three services being scraped; try the query
  `sum by (service) (rate(http_server_duration_milliseconds_count[1m]))`.
- **Grafana** — [http://localhost:3000](http://localhost:3000) (anonymous admin access) — the
  "Incident Investigator - Service Overview" dashboard is provisioned automatically, with
  request rate, p95 latency, error rate, and active requests, all broken out by service.
- **Logs** — `docker compose logs -f gateway order payment` — structured JSON lines
  correlated by `trace_id`.

### Why Jaeger (and not something else) for traces

Jaeger is a single container with an OTLP receiver and its own UI, which is the smallest
footprint that gives real, inspectable distributed traces locally — no extra collector process
or trace-storage backend to configure. Grafana is also wired to Jaeger as a datasource so
traces can be explored from the same place as metrics.

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
2. **Multi-service system** (done) — `gateway`, `order`, and `payment` with real inter-service
   HTTP calls, running independently or together under Docker Compose.
3. **Telemetry** (done) — instrument all services with OpenTelemetry (traces, metrics,
   logs); Prometheus + Grafana + Jaeger wired up locally.
4. **Fault injection** (done) — a controlled, explicit boundary for injecting latency/errors
   into a service.
5. **Deterministic correlation** (in progress: core, payload adapters, HTTP fetchers and
   window runner done; validated once against the live stack) — analyze telemetry to identify affected services and
   relationships during an incident; build the structured incident context.
6. **LLM investigator** (contract done; no provider wired) — LLM reasons over the incident context to produce a root-cause
   hypothesis with cited evidence.
7. **Dashboard** — visualize services, incidents, and hypotheses.

Each milestone is implemented and reviewed on its own; later milestones are not started early.
