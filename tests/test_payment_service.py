from fastapi.testclient import TestClient

from services.payment.main import app

client = TestClient(app)


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
