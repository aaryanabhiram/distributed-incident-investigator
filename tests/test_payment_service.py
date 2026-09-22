import time

import pytest
from fastapi.testclient import TestClient

from services.payment.main import app, fault_injector

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clear_fault_after_test() -> None:
    yield
    fault_injector.clear_fault()


def test_health_returns_ok() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "payment"}


def test_charge_approves_positive_amount() -> None:
    response = client.post("/charge", json={"order_id": "abc", "amount": 42.0})

    assert response.status_code == 200
    assert response.json() == {"order_id": "abc", "amount": 42.0, "status": "approved"}


def test_charge_declines_non_positive_amount() -> None:
    response = client.post("/charge", json={"order_id": "abc", "amount": 0})

    assert response.status_code == 200
    assert response.json()["status"] == "declined"


def test_charge_unaffected_when_no_fault_configured() -> None:
    assert client.get("/admin/fault").json() == {
        "active": False,
        "mode": None,
        "seconds_remaining": None,
    }

    response = client.post("/charge", json={"order_id": "abc", "amount": 42.0})

    assert response.status_code == 200
    assert response.json()["status"] == "approved"


def test_injected_latency_delays_charge_response() -> None:
    client.post(
        "/admin/fault",
        json={"mode": "latency", "duration_seconds": 5, "latency_ms": 300},
    )

    start = time.monotonic()
    response = client.post("/charge", json={"order_id": "abc", "amount": 42.0})
    elapsed = time.monotonic() - start

    assert response.status_code == 200
    assert elapsed >= 0.3


def test_injected_error_fails_charge_request() -> None:
    client.post(
        "/admin/fault",
        json={"mode": "error", "duration_seconds": 5, "error_status": 503},
    )

    response = client.post("/charge", json={"order_id": "abc", "amount": 42.0})

    assert response.status_code == 503
    assert response.json()["detail"] == "fault injected"


def test_fault_expires_after_its_duration() -> None:
    client.post(
        "/admin/fault",
        json={"mode": "error", "duration_seconds": 0.1, "error_status": 503},
    )

    time.sleep(0.2)

    response = client.post("/charge", json={"order_id": "abc", "amount": 42.0})

    assert response.status_code == 200
    assert response.json()["status"] == "approved"


def test_clearing_fault_restores_normal_behavior() -> None:
    client.post(
        "/admin/fault",
        json={"mode": "error", "duration_seconds": 5, "error_status": 503},
    )
    client.delete("/admin/fault")

    response = client.post("/charge", json={"order_id": "abc", "amount": 42.0})

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
