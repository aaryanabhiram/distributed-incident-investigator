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

- **gateway** — entry point; receives client requests, calls downstream services. Exists today
  as a health-check skeleton only (`services/gateway`).
- **order** *(planned)* — accepts an order request from the gateway, calls `payment`.
- **payment** *(planned)* — simulates payment processing; the primary target for fault
  injection, since a slow/broken payment service is a realistic, easy-to-reason-about incident.

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
service (instrumented with OpenTelemetry SDK)
    │
    ├── traces ──► OTel collector / exporter ──► trace backend
    ├── metrics ─► Prometheus (scraped or pushed)
    └── logs ────► structured stdout logs (correlated via trace_id)
```

- Every service shares one instrumentation setup (`shared/telemetry`) so trace/metric/log
  conventions are consistent across services — this is what makes cross-service correlation
  possible at all.
- Every log line and span carries a `trace_id` so a single client request can be reconstructed
  across all services it touched.
- Metrics are pulled by Prometheus; Grafana reads from Prometheus for visualization. No custom
  metrics pipeline.

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

## Future LLM investigation layer

Reads the incident context produced above — not raw telemetry, and not live service state —
and produces a structured hypothesis: likely root cause, confidence, and which specific pieces
of evidence in the incident context support it. The LLM's input is bounded by construction
(it's a fixed-shape record, not an open-ended log dump), which keeps the reasoning step cheap,
reviewable, and decoupled from how large the system's telemetry volume actually is.

This layer does not exist yet. When it's built, it should be swappable/mockable in tests
independent of the deterministic layers above it.

## Major data flows

1. **Request flow**: client → gateway → order → payment (and back), producing traces/logs/
   metrics at each hop.
2. **Telemetry flow**: each service → OpenTelemetry → Prometheus/trace backend → Grafana (for
   humans) and → correlation engine (for the system itself).
3. **Incident flow**: anomaly detected → correlation engine reads telemetry → incident context
   built → (future) LLM investigator reads incident context → hypothesis produced → (future)
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
