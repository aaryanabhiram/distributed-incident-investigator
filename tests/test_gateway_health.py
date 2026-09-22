import httpx
import pytest
from fastapi.testclient import TestClient

from services.gateway.main import app

client = TestClient(app)


def test_health_returns_ok() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "gateway"}


def test_checkout_calls_order_and_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, json: dict, timeout: float) -> httpx.Response:
        assert url.endswith("/orders")
        return httpx.Response(
            200,
            json={
                "order_id": "abc-123",
                "item": json["item"],
                "amount": json["amount"],
                "payment_status": "approved",
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("services.gateway.main.httpx.post", fake_post)

    response = client.post("/checkout", json={"item": "widget", "amount": 25.0})

    assert response.status_code == 200
    body = response.json()
    assert body["order_id"] == "abc-123"
    assert body["item"] == "widget"
    assert body["payment_status"] == "approved"


def test_checkout_propagates_downstream_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, json: dict, timeout: float) -> httpx.Response:
        return httpx.Response(502, request=httpx.Request("POST", url))

    monkeypatch.setattr("services.gateway.main.httpx.post", fake_post)

    response = client.post("/checkout", json={"item": "widget", "amount": 25.0})

    assert response.status_code == 502
    assert "order service unavailable" in response.json()["detail"]
