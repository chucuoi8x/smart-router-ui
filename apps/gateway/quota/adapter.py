"""Async facade over quota reservation backends.

InMemoryQuotaReservations provides sync methods (threading.Lock).
RedisQuotaReservations provides async methods.

This module wraps InMemory in an async facade so callers always ``await``
quota operations regardless of backend.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .reservations import (
    QuotaAdmissionResult,
    QuotaObservation,
    QuotaReservationRequest,
    QuotaResource,
    ReservationBatchResult,
    ReservationResult,
    ReconciliationResult,
)


class AsyncQuotaFacade:
    """Wraps a sync *InMemoryQuotaReservations* so all public methods are async."""

    def __init__(self, sync_backend: Any) -> None:  # type: ignore[type-arg]
        self._backend = sync_backend

    def _wrap(self, method, *args, **kwargs):
        return asyncio.get_running_loop().run_in_executor(None, lambda: method(*args, **kwargs))

    async def add_resource(self, resource: QuotaResource) -> QuotaResource:
        return self._backend.add_resource(resource)

    async def snapshot(self, resource_id: str) -> QuotaResource:
        return self._backend.snapshot(resource_id)

    async def apply_observation(self, observation: QuotaObservation) -> QuotaResource:
        return self._backend.apply_observation(observation)

    async def check_many(
        self, requests: list[QuotaReservationRequest]
    ) -> QuotaAdmissionResult:
        return self._backend.check_many(requests)

    async def reserve(
        self, *, resource_id: str, amount: int, reservation_id: str, risk_buffer: int = 0
    ) -> ReservationResult:
        return self._backend.reserve(
            resource_id=resource_id,
            amount=amount,
            reservation_id=reservation_id,
            risk_buffer=risk_buffer,
        )

    async def reserve_many(
        self, *, reservation_id: str, requests: list[QuotaReservationRequest]
    ) -> ReservationBatchResult:
        return self._backend.reserve_many(reservation_id=reservation_id, requests=requests)

    async def reconcile(
        self, reservation_id: str, actual_by_resource: dict[str, int]
    ) -> ReconciliationResult:
        return self._backend.reconcile(reservation_id, actual_by_resource)

    async def release(self, reservation_id: str) -> bool:
        return self._backend.release(reservation_id)

    @classmethod
    async def from_repository(cls, repository: Any) -> "AsyncQuotaFacade":  # type: ignore[name-defined]
        from .reservations import InMemoryQuotaReservations

        resources = await repository.list_resources()
        backend = InMemoryQuotaReservations()
        for res in resources:
            backend.add_resource(res)
        return cls(backend)
