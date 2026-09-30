import time

import pytest
from fastapi import HTTPException

from shared.fault_injection import FaultConfig, FaultInjector


def test_no_fault_by_default() -> None:
    injector = FaultInjector("test-service")

    injector.maybe_apply()  # should not raise or sleep

    assert injector.status().active is False


def test_latency_fault_sleeps_for_configured_duration() -> None:
    injector = FaultInjector("test-service")
    injector.set_fault(FaultConfig(mode="latency", duration_seconds=5, latency_ms=200))

    start = time.monotonic()
    injector.maybe_apply()
    elapsed = time.monotonic() - start

    # Small tolerance: on Windows time.monotonic() ticks at ~15.6 ms, so a 200 ms sleep can
    # measure slightly under 0.2 s. 0.18 s still proves the latency was really applied.
    assert elapsed >= 0.18


def test_error_fault_raises_http_exception() -> None:
    injector = FaultInjector("test-service")
    injector.set_fault(FaultConfig(mode="error", duration_seconds=5, error_status=503))

    with pytest.raises(HTTPException) as exc_info:
        injector.maybe_apply()

    assert exc_info.value.status_code == 503


def test_fault_expires_and_stops_applying() -> None:
    injector = FaultInjector("test-service")
    injector.set_fault(FaultConfig(mode="error", duration_seconds=0.1, error_status=503))

    time.sleep(0.2)

    injector.maybe_apply()  # should not raise: fault window has passed

    assert injector.status().active is False


def test_status_reports_remaining_time_within_bound() -> None:
    injector = FaultInjector("test-service")
    injector.set_fault(FaultConfig(mode="error", duration_seconds=10, error_status=503))

    status = injector.status()

    assert status.active is True
    assert status.mode == "error"
    assert 0 < status.seconds_remaining <= 10


def test_clear_fault_disables_it_immediately() -> None:
    injector = FaultInjector("test-service")
    injector.set_fault(FaultConfig(mode="error", duration_seconds=10, error_status=503))

    injector.clear_fault()

    assert injector.status().active is False
    injector.maybe_apply()  # should not raise


def test_duration_is_bounded_by_config_validation() -> None:
    with pytest.raises(ValueError):
        FaultConfig(mode="error", duration_seconds=10_000, error_status=503)


def test_unsupported_mode_is_rejected() -> None:
    injector = FaultInjector("test-service")

    with pytest.raises(ValueError):
        injector.set_fault(FaultConfig(mode="bogus", duration_seconds=1))
