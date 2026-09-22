"""A small, explicit fault-injection control usable by any service.

A fault is configured at runtime via an admin endpoint (see `install_fault_routes`) and held
as in-process state with a hard expiry, so it can never silently outlive the window it was
set for. `maybe_apply_fault` is called from business logic at the service boundary — it adds
real latency or raises a real HTTP error, so the effect (and therefore its telemetry) is
genuine rather than fabricated.
"""

import logging
import time
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


@dataclass
class _ActiveFault:
    mode: str  # "latency" or "error"
    latency_ms: int
    error_status: int
    expires_at: float


class FaultConfig(BaseModel):
    mode: str = Field(description="'latency' or 'error'")
    duration_seconds: float = Field(gt=0, le=300, description="How long the fault stays active")
    latency_ms: int = Field(default=0, ge=0, le=30_000)
    error_status: int = Field(default=503, ge=400, le=599)


class FaultStatus(BaseModel):
    active: bool
    mode: str | None = None
    seconds_remaining: float | None = None


class FaultInjector:
    """Holds at most one active, time-bounded fault for a service instance."""

    def __init__(self, service_name: str) -> None:
        self._service_name = service_name
        self._fault: _ActiveFault | None = None

    def set_fault(self, config: FaultConfig) -> None:
        if config.mode not in ("latency", "error"):
            raise ValueError(f"unsupported fault mode: {config.mode}")

        self._fault = _ActiveFault(
            mode=config.mode,
            latency_ms=config.latency_ms,
            error_status=config.error_status,
            expires_at=time.monotonic() + config.duration_seconds,
        )
        logger.warning(
            "fault injection armed",
            extra={
                "service": self._service_name,
                "fault_mode": config.mode,
                "fault_latency_ms": config.latency_ms,
                "fault_error_status": config.error_status,
                "fault_duration_seconds": config.duration_seconds,
            },
        )

    def clear_fault(self) -> None:
        self._fault = None
        logger.warning("fault injection cleared", extra={"service": self._service_name})

    def status(self) -> FaultStatus:
        fault = self._current()
        if fault is None:
            return FaultStatus(active=False)
        return FaultStatus(
            active=True,
            mode=fault.mode,
            seconds_remaining=max(0.0, fault.expires_at - time.monotonic()),
        )

    def _current(self) -> _ActiveFault | None:
        if self._fault is not None and time.monotonic() >= self._fault.expires_at:
            self._fault = None
        return self._fault

    def maybe_apply(self) -> None:
        """Call at the top of a request handler. Sleeps or raises if a fault is active."""
        fault = self._current()
        if fault is None:
            return

        if fault.mode == "latency":
            logger.warning(
                "fault injection: adding latency",
                extra={"service": self._service_name, "fault_latency_ms": fault.latency_ms},
            )
            time.sleep(fault.latency_ms / 1000)
        elif fault.mode == "error":
            logger.warning(
                "fault injection: returning error",
                extra={"service": self._service_name, "fault_error_status": fault.error_status},
            )
            raise HTTPException(status_code=fault.error_status, detail="fault injected")


def install_fault_routes(app: FastAPI, injector: FaultInjector) -> None:
    """Mount the admin fault-control endpoints (/admin/fault) on a service's FastAPI app."""

    @app.post("/admin/fault", response_model=FaultStatus)
    def set_fault(config: FaultConfig) -> FaultStatus:
        injector.set_fault(config)
        return injector.status()

    @app.delete("/admin/fault", response_model=FaultStatus)
    def clear_fault() -> FaultStatus:
        injector.clear_fault()
        return injector.status()

    @app.get("/admin/fault", response_model=FaultStatus)
    def get_fault() -> FaultStatus:
        return injector.status()
