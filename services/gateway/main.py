import os

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

ORDER_SERVICE_URL = os.environ.get("ORDER_SERVICE_URL", "http://127.0.0.1:8001")

app = FastAPI(title="gateway")


class CheckoutRequest(BaseModel):
    item: str
    amount: float


class CheckoutResponse(BaseModel):
    order_id: str
    item: str
    amount: float
    payment_status: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "gateway"}


@app.post("/checkout", response_model=CheckoutResponse)
def checkout(request: CheckoutRequest) -> CheckoutResponse:
    try:
        response = httpx.post(
            f"{ORDER_SERVICE_URL}/orders",
            json={"item": request.item, "amount": request.amount},
            timeout=5.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"order service unavailable: {exc}") from exc

    return CheckoutResponse(**response.json())
