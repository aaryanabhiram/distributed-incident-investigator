"""Thin HTTP fetchers: Prometheus / Jaeger endpoint -> decoded JSON -> existing adapter.

Each function takes an `httpx.Client` whose `base_url` points at the backend (locally
`http://localhost:9090` for Prometheus and `http://localhost:16686` for Jaeger), so tests can
inject an `httpx.MockTransport`. All parsing lives in `adapters.py`; this module only does the
request, checks the HTTP status and decodes the body. HTTP failures raise
`httpx.HTTPStatusError`; a body that is not JSON raises `ValueError`.

Datetime contract: every datetime turned into a query timestamp must be timezone-aware. A naive
datetime would be read as the host's local time by `datetime.timestamp()`, silently shifting
the query window, so `require_aware` rejects it with `ValueError` before any request is made.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from shared.correlation import MetricSample, SpanRecord
from shared.correlation.adapters import parse_jaeger_traces, parse_prometheus_vector


def require_aware(name: str, value: datetime) -> None:
    """Raise `ValueError` if `value` is a naive datetime (no UTC offset)."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware, got naive datetime {value!r}")


def _get_json(client: httpx.Client, path: str, params: dict[str, str | int]) -> dict:
    response = client.get(path, params=params)
    response.raise_for_status()
    return response.json()


def fetch_prometheus_samples(
    client: httpx.Client, query: str, metric_name: str, at: datetime | None = None
) -> list[MetricSample]:
    """Run a PromQL instant query (`/api/v1/query`) and return its samples.

    `at` evaluates the query at that instant; by default Prometheus uses "now".
    """
    params: dict[str, str | int] = {"query": query}
    if at is not None:
        require_aware("at", at)
        params["time"] = at.timestamp()
    return parse_prometheus_vector(_get_json(client, "/api/v1/query", params), metric_name)


def fetch_jaeger_spans(
    client: httpx.Client,
    service: str,
    start: datetime,
    end: datetime,
    limit: int = 100,
) -> list[SpanRecord]:
    """Fetch traces involving `service` in [start, end] (`/api/traces`) and return their spans.

    Jaeger takes `start`/`end` as epoch microseconds. Every span of each matching trace is
    returned, including spans from other services, which is what yields cross-service edges.
    """
    require_aware("start", start)
    require_aware("end", end)
    params: dict[str, str | int] = {
        "service": service,
        "start": int(start.timestamp() * 1_000_000),
        "end": int(end.timestamp() * 1_000_000),
        "limit": limit,
    }
    return parse_jaeger_traces(_get_json(client, "/api/traces", params))
