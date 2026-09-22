import logging
import os

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from shared.telemetry import setup_telemetry

ORDER_SERVICE_URL = os.environ.get("ORDER_SERVICE_URL", "http://127.0.0.1:8001")

app = FastAPI(title="gateway")
setup_telemetry(app, "gateway")

logger = logging.getLogger(__name__)


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
    logger.info("checkout requested", extra={"item": request.item, "amount": request.amount})
    try:
        response = httpx.post(
            f"{ORDER_SERVICE_URL}/orders",
            json={"item": request.item, "amount": request.amount},
            timeout=5.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.error("order service call failed", extra={"error": str(exc)})
        raise HTTPException(status_code=502, detail=f"order service unavailable: {exc}") from exc

    logger.info("checkout completed", extra={"order_id": response.json().get("order_id")})
    return CheckoutResponse(**response.json())
