"""PromQL used by the investigator evaluations, committed so runs are reproducible.

PROVENANCE: the original evaluation queries were run from throwaway scripts that were not kept
(see docs/investigator-evaluation-history.md), so these are *reconstructed* from that document's
description of each signal, not recovered verbatim. They were checked by running them against
the live Compose stack (see tests/fixtures/backends/README.md), not by diffing against the
originals.

Both aggregate per `service` (the Prometheus scrape-job label) over `window` (a PromQL duration
such as "5m") and restrict to the business endpoints only. Without that restriction, /health,
/metrics scrapes and /admin/fault calls dilute the signal.
"""

from __future__ import annotations

# Metric names given to `run_correlation(metric_name=...)` and `AnomalyRule.metric_name`.
LATENCY_METRIC = "http_server_duration_milliseconds"
ERROR_RATIO_METRIC = "http_server_error_ratio"

_BUSINESS = 'http_target=~"/checkout|/orders|/charge"'
_SUM = "http_server_duration_milliseconds_sum"
_COUNT = "http_server_duration_milliseconds_count"


def _per_service(metric: str, matchers: str, window: str) -> str:
    return f"sum by (service) (increase({metric}{{{matchers}}}[{window}]))"


def mean_latency_query(window: str) -> str:
    """Mean request latency in ms per service: increase(sum) / increase(count)."""
    return f"{_per_service(_SUM, _BUSINESS, window)} / {_per_service(_COUNT, _BUSINESS, window)}"


def error_ratio_query(window: str) -> str:
    """Share of 5xx responses per service. 0/0 (no traffic in the window) yields NaN."""
    errors = _per_service(_COUNT, f'{_BUSINESS},http_status_code=~"5.."', window)
    return f"{errors} / {_per_service(_COUNT, _BUSINESS, window)}"


def restricted_mean_latency_query(window: str, services: list[str]) -> str:
    """`mean_latency_query` limited to `services`, modelling telemetry that is not observed.

    A textual edit of the reconstructed query (the same edit Evaluations 5-7 used with
    `gateway|order`): it adds a `service=~...` matcher to every selector, so the other services
    return no series at all and read as `unobserved`.
    """
    matcher = "|".join(services)
    return mean_latency_query(window).replace("{http_target", f'{{service=~"{matcher}",http_target')
