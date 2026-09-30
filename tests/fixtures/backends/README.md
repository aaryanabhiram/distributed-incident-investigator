# Backend payload fixtures

Responses from the local Docker Compose stack (Prometheus v2.55.1, Jaeger all-in-one 1.60,
services on OpenTelemetry instrumentation 0.66b0), captured on 2026-09-30 by an HTTP GET
against `localhost:9090` / `localhost:16686`. Nothing here is hand-written.
The traffic was left in the stack by earlier manual fault-injection runs (payment 1500 ms
latency fault; payment 503 error fault), the same scenarios as evaluations 1 and 4 in
`docs/investigator-evaluation-history.md`. It is not a byte-for-byte replay of those runs, so
values differ slightly (e.g. gateway mean latency 1516.7 ms here vs 1515.9 ms recorded).

| File | Source | Modification |
|---|---|---|
| `prometheus_mean_latency_vector.json` | `/api/v1/query`, `mean_latency_query("5m")`, `time=1790752560` | none |
| `prometheus_error_ratio_vector.json` | `/api/v1/query`, `error_ratio_query("5m")`, `time=1790753670` | none |
| `prometheus_error_ratio_nan_vector.json` | `/api/v1/query`, `error_ratio_query("1m")`, `time=1790756100` (series present, no traffic in window: 0/0) | none |
| `prometheus_empty_vector.json` | `/api/v1/query`, `mean_latency_query("1m")`, `time=1790700000` (before any data) | none |
| `jaeger_checkout_trace_ok.json` | `/api/traces?service=gateway&operation=POST /checkout`, one 200 checkout trace | first trace of the result only; span `tags`, `logs`, `warnings` and process `tags` removed (host IPs, peer ports); `traceID`, `spanID`, `references`, `startTime`, `processID`, `processes[].serviceName` untouched |
| `jaeger_checkout_trace_err.json` | same query, one checkout trace during the payment 503 fault | same reduction |

The Prometheus queries are reconstructions (see `shared/correlation/queries.py`). To refresh a
fixture, re-run the query against the stack and overwrite the file; the tests assert on
structure and on values that follow from the payload, so a refresh may need value updates.
