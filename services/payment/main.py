import logging

from fastapi import FastAPI
from pydantic import BaseModel

from shared.fault_injection import FaultInjector, install_fault_routes
from shared.telemetry import setup_telemetry

app = FastAPI(title="payment", telemetry={"auto_configure": False})
setup_telemetry(app, "payment")

fault_injector = FaultInjector("payment")
install_fault_routes(app, fault_injector)

logger = logging.getLogger(__name__)


class ChargeRequest(BaseModel):
    order_id: str
    amount: float


class ChargeResponse(BaseModel):
    order_id: str
    amount: float
    status: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "payment"}


@app.post("/charge", response_model=ChargeResponse)
def charge(request: ChargeRequest) -> ChargeResponse:
    fault_injector.maybe_apply()

    # Deterministic, easy-to-reason-about rule: non-positive amounts are declined.
    # This gives the system a real, reproducible failure path without fault injection.
    status = "approved" if request.amount > 0 else "declined"
    logger.info(
        "charge processed",
        extra={"order_id": request.order_id, "amount": request.amount, "status": status},
    )
    return ChargeResponse(order_id=request.order_id, amount=request.amount, status=status)
