"""Shared OpenTelemetry setup used by every service.

Each service calls `setup_telemetry(app, service_name)` once at import time. That call wires
up the three telemetry signals consistently so cross-service correlation is possible later:

- traces: OTLP export to the collector configured via OTEL_EXPORTER_OTLP_ENDPOINT (Jaeger
  locally), plus auto-instrumentation of FastAPI and outgoing httpx calls so a checkout
  request produces one connected trace across gateway -> order -> payment.
- metrics: an OpenTelemetry Prometheus reader exposed on GET /metrics for scraping. Includes
  request count, request latency, and in-flight requests, labeled by service/method/route/
  status via the FastAPI instrumentor's default semantic-convention metrics.
- logs: structured JSON to stdout, with trace_id/span_id injected into every record so a log
  line can be tied back to the trace/service that produced it.
"""

import logging
import os
import sys

from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import make_asgi_app
from pythonjsonlogger import json as jsonlogger


class _TraceContextFilter(logging.Filter):
    """Injects the service name and active trace/span id (if any) into each log record."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service_name = service_name

    def filter(self, record: logging.LogRecord) -> bool:
        record.service = self._service_name

        span = trace.get_current_span()
        span_context = span.get_span_context()
        if span_context.is_valid:
            record.trace_id = format(span_context.trace_id, "032x")
            record.span_id = format(span_context.span_id, "016x")
        else:
            record.trace_id = None
            record.span_id = None
        return True


def _configure_logging(service_name: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_TraceContextFilter(service_name))
    formatter = jsonlogger.JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s %(service)s %(trace_id)s %(span_id)s"
    )
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

    # Route uvicorn's own loggers through the same structured handler/format.
    for uvicorn_logger in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(uvicorn_logger)
        logger.handlers = [handler]
        logger.propagate = False


def setup_telemetry(app: FastAPI, service_name: str) -> None:
    """Configure tracing, metrics, and logging for one FastAPI service instance."""
    resource = Resource.create({SERVICE_NAME: service_name})

    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True))
    )
    trace.set_tracer_provider(tracer_provider)

    prometheus_reader = PrometheusMetricReader()
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[prometheus_reader]))

    _configure_logging(service_name)

    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()

    app.mount("/metrics", make_asgi_app())
