import os
import uuid

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

PAYMENT_SERVICE_URL = os.environ.get("PAYMENT_SERVICE_URL", "http://127.0.0.1:8002")

app = FastAPI(title="order")


class OrderRequest(BaseModel):
    item: str
    amount: float


class OrderResponse(BaseModel):
    order_id: str
    item: str
    amount: float
    payment_status: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "order"}


@app.post("/orders", response_model=OrderResponse)
def create_order(request: OrderRequest) -> OrderResponse:
    order_id = str(uuid.uuid4())

    try:
        response = httpx.post(
            f"{PAYMENT_SERVICE_URL}/charge",
            json={"order_id": order_id, "amount": request.amount},
            timeout=5.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"payment service unavailable: {exc}") from exc

    payment_status = response.json()["status"]

    return OrderResponse(
        order_id=order_id,
        item=request.item,
        amount=request.amount,
        payment_status=payment_status,
    )
