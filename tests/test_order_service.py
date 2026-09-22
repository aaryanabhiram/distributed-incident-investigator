import httpx
import pytest
from fastapi.testclient import TestClient

from services.order.main import app

client = TestClient(app)


def test_health_returns_ok() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "order"}


def test_create_order_calls_payment_and_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, json: dict, timeout: float) -> httpx.Response:
        assert url.endswith("/charge")
        return httpx.Response(
            200,
            json={"order_id": json["order_id"], "amount": json["amount"], "status": "approved"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("services.order.main.httpx.post", fake_post)

    response = client.post("/orders", json={"item": "widget", "amount": 25.0})

    assert response.status_code == 200
    body = response.json()
    assert body["item"] == "widget"
    assert body["amount"] == 25.0
    assert body["payment_status"] == "approved"
    assert body["order_id"]


def test_create_order_propagates_payment_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, json: dict, timeout: float) -> httpx.Response:
        return httpx.Response(500, request=httpx.Request("POST", url))

    monkeypatch.setattr("services.order.main.httpx.post", fake_post)

    response = client.post("/orders", json={"item": "widget", "amount": 25.0})

    assert response.status_code == 502
    assert "payment service unavailable" in response.json()["detail"]
